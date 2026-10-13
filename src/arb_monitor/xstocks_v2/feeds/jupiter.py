"""Jupiter: batch reference prices and real two-way Solana quotes.

Two things this module must not do, both learned from the v1 board:

* **Never let a 429 turn into a retry storm.** One limiter is shared by every
  call; a 429 pauses all of them (2026-10-03: per-thread retries kept the
  window full until every quote failed).
* **Never treat a reference price as an execution price.** ``price/v3`` is a
  last-trade feed including extended hours. It may rank candidates. It may not
  enter net profit (spec section 5.2).

``swap/v1/quote`` is the only source of a real Solana fill price, and it answers
both directions: buy is ``USDC -> mint``, sell is ``mint -> USDC``.
"""
from __future__ import annotations

import os
import time
import urllib.parse
from decimal import Decimal, InvalidOperation
from pathlib import Path

from arb_monitor.xstocks_v2.feeds.http import PROXY, Http, RateLimiter

ROOT = Path(__file__).resolve().parents[4]
JUP_ENV = ROOT / "config" / "jupiter.env"
#: The wallet that signs Solana swaps (same address v1 and the v2 roundtrip
#: scripts use; the swap is built for it and signed by the wallet CLI).
WALLET = "AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev"

BASE_KEYED = "https://api.jup.ag"
BASE_LITE = "https://lite-api.jup.ag"
#: Where a process points at the **shared** request budget. Set by the runner's
#: start script; every process with the same value queues behind the same file,
#: so the key's 1 rps is spent once instead of once per process.
RATE_FILE_ENV = "JUPITER_RATE_FILE"
# Published limits: 0.5/s keyless, 1/s on a free key, 10/s on Developer.
# The board measures ~0.9 requests/s end to end with the free key.
RPS_FREE = 1.0
RPS_KEYLESS = 1.0
HEADROOM = 0.9
PRICE_BATCH = 50
PAUSE_S = 60.0

# Jupiter answers these when the mint has no route at all. They do not change
# from pass to pass, so re-asking every pass only burns budget.
NO_ROUTE_CODES = frozenset({
    "NO_ROUTES_FOUND", "TOKEN_NOT_TRADABLE", "COULD_NOT_FIND_ANY_ROUTE",
})


def read_env_key(path: Path | str = JUP_ENV) -> str:
    try:
        for line in Path(path).read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "JUPITER_API_KEY":
                return value.strip().strip('"')
    except OSError:
        pass
    return ""


def configure(env: dict | None = None) -> tuple[str, str, float]:
    """Return ``(base_url, api_key, rps)``. Matches the v1 board's selection."""
    env = os.environ if env is None else env
    key = (env.get("JUPITER_API_KEY") or "").strip() or read_env_key()
    base = BASE_KEYED if key else BASE_LITE
    rps = RPS_FREE if key else RPS_KEYLESS
    text = (env.get("JUPITER_RPS") or "").strip()
    if text:
        try:
            rps = float(text)
        except ValueError:
            pass
    return base, key, max(0.1, rps)


def parse_price_v3(payload: object) -> dict[str, Decimal]:
    """Price V3 rows -> ``{mint: price}``. Reference only, never net profit."""
    out: dict[str, Decimal] = {}
    if not isinstance(payload, dict):
        return out
    for mint, row in payload.items():
        if not isinstance(row, dict):
            continue
        raw = (row.get("stockData") or {}).get("price")
        if raw in (None, "", 0, "0"):
            raw = row.get("price")
        try:
            price = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if price > 0:
            out[str(mint)] = price
    return out


def parse_quote(payload: object) -> dict | None:
    """``swap/v1/quote`` -> the out amount, or None with a reason code.

    Returns ``{"out_amount": int, "impact_bps": Decimal|None, "route": str}``.
    """
    if not isinstance(payload, dict):
        return None
    raw = payload.get("outAmount")
    if raw in (None, ""):
        return None
    try:
        out_amount = int(str(raw))
    except ValueError:
        return None
    impact = None
    try:
        if payload.get("priceImpactPct") not in (None, ""):
            impact = Decimal(str(payload["priceImpactPct"])) * Decimal(10000)
    except (InvalidOperation, TypeError, ValueError):
        impact = None
    labels = [str(step.get("swapInfo", {}).get("label") or "")
              for step in (payload.get("routePlan") or [])
              if isinstance(step, dict)]
    return {
        "out_amount": out_amount,
        "impact_bps": impact,
        "route": "+".join(label for label in labels if label) or "jupiter",
    }


def error_code(payload: object) -> str:
    """Jupiter's machine-readable reason, for the no-route cache."""
    if not isinstance(payload, dict):
        return ""
    text = str(payload.get("error") or payload.get("errorCode") or "")
    for code in NO_ROUTE_CODES:
        if code in text.upper():
            return code
    return text[:60]


class JupiterFeed:
    """One limiter, one counter set, shared by every caller in the process."""

    def __init__(self, http: Http | None = None, *, rps: float | None = None,
                 api_key: str | None = None, base: str | None = None,
                 limiter: RateLimiter | None = None, pause_s: float = PAUSE_S,
                 shared_rate_path: Path | str | None = None):
        base_env, key_env, rps_env = configure()
        self.http = http or Http(proxy=PROXY)
        self.api_key = api_key if api_key is not None else key_env
        self.base = base or base_env
        # One budget for every process that holds this key. Set by the runner's
        # start script (``JUPITER_RATE_FILE``); unset means "this process is on
        # its own", which is what the hermetic tests want.
        shared = (shared_rate_path if shared_rate_path is not None
                  else os.environ.get(RATE_FILE_ENV) or None)
        self.limiter = limiter or RateLimiter(
            rps if rps is not None else rps_env * HEADROOM, capacity=2.0,
            shared_path=shared)
        self.pause_s = pause_s
        self.calls = 0
        self.http_429 = 0
        self.failures: dict[str, int] = {}

    # -- internals ---------------------------------------------------------
    def _headers(self) -> dict:
        return {"x-api-key": self.api_key} if self.api_key else {}

    def _note(self, reason: str) -> None:
        self.failures[reason] = self.failures.get(reason, 0) + 1

    def _get(self, path: str, params: dict) -> tuple[int, object | None]:
        url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        self.limiter.acquire()
        self.calls += 1
        try:
            status, payload, headers = self.http.get_json(url, headers=self._headers())
        except Exception as exc:  # noqa: BLE001
            self._note(f"{path}:{type(exc).__name__}")
            return 0, None
        if status == 429:
            self.http_429 += 1
            wait = self.pause_s
            try:
                wait = min(max(float(headers.get("x-ratelimit-reset", 0)) - _now(), 1.0),
                           self.pause_s)
            except (TypeError, ValueError):
                pass
            self.limiter.pause(wait)
            self._note(f"{path}:http_429")
        elif status != 200:
            self._note(f"{path}:http_{status}")
        return status, payload

    # -- public ------------------------------------------------------------
    def price_v3(self, mints, *, chunk: int = PRICE_BATCH) -> dict[str, Decimal]:
        """Batch reference prices. Only for ranking candidates."""
        unique = [mint for mint in dict.fromkeys(mints) if mint]
        out: dict[str, Decimal] = {}
        for start in range(0, len(unique), chunk):
            group = unique[start:start + chunk]
            status, payload = self._get("/price/v3", {"ids": ",".join(group)})
            if status == 200:
                out.update(parse_price_v3(payload))
        return out

    def quote(self, input_mint: str, output_mint: str, raw_amount: int,
              *, slippage_bps: int = 10, tries: int = 1,
              sleep=time.sleep) -> dict | None:
        """A real fill quote in one direction. None means no route, not zero.

        ``tries`` exists for the callers that decide something with the answer.
        A 429 is not a missing route: the three v2 processes each pace their own
        limiter but share one 1 rps key, so a decision-time quote can be
        rate-limited by a scan that is running at the same time (measured
        2026-10-08: 429 for one call, 200 for the next). The default stays 1 so
        the scan's high-volume quoting does not multiply its own load.
        """
        if raw_amount <= 0:
            return None
        tries = max(1, int(tries))
        for attempt in range(tries):
            status, payload = self._get("/swap/v1/quote", {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": int(raw_amount),
                "slippageBps": int(slippage_bps),
            })
            if status in (0, 429) and attempt + 1 < tries:
                sleep(1.0 * (attempt + 1))
                continue
            if status != 200:
                return None
            parsed = parse_quote(payload)
            if parsed is None:
                code = error_code(payload)
                if code:
                    self._note(f"no_route:{code}")
                return None
            return parsed
        return None

    def raw_quote(self, input_mint: str, output_mint: str, raw_amount: int,
                  slippage_bps: int = 10) -> dict | None:
        """The untouched ``/swap/v1/quote`` response, for building a swap.

        The executor needs the response itself (``routePlan`` and friends) to
        POST it back as ``quoteResponse``; ``quote()`` returns a summary for the
        scan. Both go through this one keyed client: measured 2026-10-08, the v1
        client the executor used to borrow was unauthenticated
        (``JUP_BASE = api.jup.ag`` with no key) and answered 401, so every row
        that cleared round 2 died at ``requote_failed``.
        """
        status, payload = self.raw_quote_with_status(
            input_mint, output_mint, raw_amount, slippage_bps)
        return payload if status == 200 else None

    def raw_quote_with_status(self, input_mint: str, output_mint: str,
                              raw_amount: int, slippage_bps: int = 10
                              ) -> tuple[int, dict | None]:
        """``(http_status, payload)``. ``0`` means the request never arrived.

        A caller has to tell "this pool has no route" (400: refusing is right)
        from "we were rate-limited" (429: the same call answers 200 a moment
        later). Collapsing both into ``{}`` is how every row that cleared round
        2 died at ``requote_failed`` (measured 2026-10-08 13:4x: the same 25
        USDC quote answered 429 once and 200 with ``outAmount 7550159`` next).
        """
        if raw_amount <= 0:
            return 0, None
        status, payload = self._get("/swap/v1/quote", {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": int(raw_amount),
            "slippageBps": int(slippage_bps),
        })
        if not isinstance(payload, dict):
            return status, None
        if status != 200:
            return status, None
        if "outAmount" not in payload:
            self._note("no_route:no_out_amount")
            return status, None
        return status, payload

    def build_swap_with_status(self, quote: dict) -> tuple[int, dict | None]:
        """``(http_status, payload)`` for ``/swap/v1/swap``; ``0`` = never arrived.

        ``build_swap`` returned ``{}`` for a rate limit and for a real failure
        alike, so a caller could not tell "ask again" from "this route is dead".
        Measured 2026-10-08 21:3x: a PFEx probe died at ``buy:swap_build_failed``
        and the very next call to the same URL answered 200 with a
        ``swapTransaction`` (A38).
        """
        url = f"{self.base}/swap/v1/swap"
        self.limiter.acquire()
        self.calls += 1
        try:
            status, payload = self.http.post_json(url, {
                "quoteResponse": quote,
                "userPublicKey": WALLET,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
            }, headers=self._headers())
        except Exception as exc:  # noqa: BLE001
            self._note(f"/swap/v1/swap:{type(exc).__name__}")
            return 0, None
        if status != 200 or not isinstance(payload, dict):
            self._note(f"/swap/v1/swap:http_{status}")
            return status, None
        return status, payload

    def build_swap(self, quote: dict) -> dict:
        """``/swap/v1/swap`` for a quote this client produced."""
        return self.build_swap_with_status(quote)[1] or {}


#: A swap build may be retried: no broadcast has happened yet. Only a rate
#: limit or a request that never arrived means "ask again" -- a 400 means the
#: route itself is dead, and retrying it just burns the shared key.
SWAP_BUILD_TRIES = 4
SWAP_BUILD_BACKOFF_S = 1.0
SWAP_BUILD_BACKOFF_MAX_S = 5.0


def build_swap_retry(client, quote: dict, *, tries: int = SWAP_BUILD_TRIES,
                     backoff_s: float = SWAP_BUILD_BACKOFF_S,
                     max_backoff_s: float = SWAP_BUILD_BACKOFF_MAX_S,
                     sleep=time.sleep) -> tuple[dict, str]:
    """``(swap_payload, failure_note)``; ``{}`` means it never built.

    Same rule as the quote retry (A23): retry 429 and transport failures, give
    up immediately on anything else. The executor used to lose a whole cycle to
    one transient failure right before the broadcast (A38).
    """
    last = "unknown"
    for attempt in range(max(1, int(tries))):
        status, payload = client.build_swap_status(quote)
        if status == 200 and isinstance(payload, dict) and payload.get("swapTransaction"):
            return payload, ""
        last = f"http_{status}" if status else "transport"
        if status not in (0, 429) or attempt + 1 >= max(1, int(tries)):
            break
        sleep(min(backoff_s * (attempt + 1), max_backoff_s))
    return {}, last


class SwapClient:
    """The two calls the executor needs, on top of the keyed feed.

    Interface-compatible with v1's ``Jupiter`` so ``legs()`` can hand it to
    ``execute_cycle`` without a second swap implementation.
    """

    def __init__(self, feed: JupiterFeed):
        self.feed = feed

    def quote(self, input_mint: str, output_mint: str, raw_amount: int,
              slippage_bps: int = 10) -> dict:
        return self.feed.raw_quote(input_mint, output_mint, raw_amount,
                                  slippage_bps) or {}

    def quote_status(self, input_mint: str, output_mint: str, raw_amount: int,
                     slippage_bps: int = 10) -> tuple[int, dict | None]:
        """``quote`` plus the HTTP status, for a caller that must retry 429s."""
        return self.feed.raw_quote_with_status(input_mint, output_mint,
                                               raw_amount, slippage_bps)

    def build_swap(self, quote: dict) -> dict:
        return self.feed.build_swap(quote)

    def build_swap_status(self, quote: dict) -> tuple[int, dict | None]:
        """``build_swap`` plus the HTTP status, for a caller that must retry."""
        return self.feed.build_swap_with_status(quote)


def _now() -> float:
    import time

    return time.time()
