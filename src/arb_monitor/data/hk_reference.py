"""Public reference data only. Neither a price search nor OHLCV is a fill quote."""
from __future__ import annotations

import json
import random
import re
import subprocess
import threading
from decimal import Decimal
from urllib.parse import quote, urlsplit

from arb_monitor.data.hk_cex import now_ms


_STATUS_RE = re.compile(r"returned error:\s*(\d{3})")
#: Marker for the `--write-out` trailer that carries the HTTP status and the
#: response headers. `-f` is deliberately not used: through the local proxy a
#: 429 and a 404 both surfaced as "curl exit 56", and the only way to tell them
#: apart was to guess from the stderr text.
_TRAILER = "\n__ARB_HTTP__"
#: Answering 404 again in five minutes does not make a ticker exist. The 4xx
#: family below is never fixed by a retry; 429/5xx/timeouts are.
PERMANENT_STATUSES = frozenset({400, 401, 403, 404, 410, 451})
#: A host that has failed this many times in a row is not merely unlucky: from
#: here on it gets one probe request at a time instead of the whole fan-out.
RECOVERING_AFTER = 2
#: Longest cooldown a `Retry-After` header may impose. The upstream's number is
#: honoured, but not without bound: a header of "86400" would otherwise take a
#: host out for a day on the strength of one response.
RETRY_AFTER_CAP_MS = 3600_000


def _split_trailer(stdout: str) -> tuple[str, int | None, dict]:
    """Split curl's stdout into (body, http status, headers).

    A missing trailer means curl never reached a response, which is the
    transport-failure case and is reported as such.
    """
    body, marker, trailer = stdout.rpartition(_TRAILER)
    if not marker:
        return stdout, None, {}
    code_line, _, raw_headers = trailer.partition("\n")
    try:
        status = int(code_line.strip())
    except ValueError:
        status = None
    headers: dict = {}
    try:
        parsed = json.loads(raw_headers)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        headers = {str(key).lower(): value for key, value in parsed.items()}
    return body, status, headers


def _retry_after_ms(headers: dict) -> int | None:
    """`Retry-After: <seconds>` in milliseconds, or None when absent/absurd.

    Only the numeric form is honoured. The HTTP-date form is legal but would
    make the cooldown depend on two clocks we do not control, and the header is
    advisory to begin with.
    """
    value = headers.get("retry-after")
    if value in (None, ""):
        return None
    try:
        seconds = float(str(value).strip())
    except ValueError:
        return None
    if seconds <= 0:
        return 0
    return int(min(seconds * 1000, RETRY_AFTER_CAP_MS))


class PublicHTTPError(RuntimeError):
    """A public reference read failed. Kept a RuntimeError for callers."""


class HTTPStatusError(PublicHTTPError):
    """The endpoint answered with an HTTP status instead of a body."""

    def __init__(self, status: int, url: str, detail: str = "",
                 retry_after_ms: int | None = None):
        super().__init__(f"public_http_status:{status}:{detail[:140]}")
        self.status = int(status)
        self.url = url
        self.retry_after_ms = retry_after_ms

    @property
    def permanent(self) -> bool:
        return self.status in PERMANENT_STATUSES


class PublicHTTP:
    """Curl-backed public reads with a bounded memory of what just happened.

    Curl through the local proxy reports every HTTP status as exit code 56, so
    the status is parsed back out of stderr: "404" and "429" arrived in the
    event log as the same kind of string and they need opposite handling.

    Measured 2026-09-21 over 24 h (``data/hk_monitor.db``): 8,002
    ``source_error`` rows, of which 2,967 were permanent 404s (SHEIN's
    ``625.HK``, which is not a listed symbol, and the auto-discovered
    ``HKEXCx``) and 3,966 were 429s from GeckoTerminal. The reference fan-out is
    46 curl calls every ``reference_refresh_seconds`` (300 s) — about 13 k/day —
    for data that changes far more slowly than that.

    The memory is deliberately conservative:

    * a successful body is reused only when the caller asks for it with
      ``max_age_ms``, and it comes back carrying ``cache_hit``/``cache_age_ms``,
      so a reused sample is never dressed up as a fresh one;
    * a permanent failure is re-raised from memory for ``permanent_ttl_ms``
      without touching the network, keeping the original reason;
    * a host that rate-limits, errors or times out goes on an exponential
      cooldown, which is what stops a retry storm from eating the shared proxy.
    """

    def __init__(self, proxy: str | None = None, runner=None, clock=now_ms,
                 permanent_ttl_ms: int = 6 * 3600_000,
                 cooldown_ms: int = 60_000, cooldown_max_ms: int = 900_000,
                 max_per_host: int = 2, jitter: float = 0.25, rand=None):
        self.proxy = proxy
        self._runner = runner or self._curl
        self._clock = clock
        self.permanent_ttl_ms = int(permanent_ttl_ms)
        self.cooldown_ms = int(cooldown_ms)
        self.cooldown_max_ms = int(cooldown_max_ms)
        # Concurrency is bounded per *host*, not per refresh. The reference
        # fan-out is 46 calls to four hosts; without this every one of them can
        # be in the air at once and a host that answers 429 is answered with
        # more 429s. The semaphore blocks (briefly, bounded by curl's own
        # timeouts) instead of refusing, because "we were busy" is not evidence.
        self.max_per_host = max(1, int(max_per_host))
        # Backoff is jittered so that a refresh round does not synchronise its
        # retries with the next one.
        self.jitter = max(0.0, float(jitter))
        self._rand = rand or random.random
        self._semaphores: dict[str, threading.Semaphore] = {}
        self._recovering_probe: dict[str, bool] = {}
        self._lock = threading.Lock()
        self._fresh: dict[str, tuple[int, dict]] = {}
        self._dead: dict[str, tuple[int, str]] = {}
        self._cooling: dict[str, tuple[int, int, str]] = {}
        # Per-host health, read-only for the boards. ``_cooling`` already knows
        # *when* a host may be tried again; this adds *why* and *since when*, so
        # the panel can show "GeckoTerminal backoff, 429, 92 s left" instead of
        # a bare "5 data-source errors". Purely observational: nothing in the
        # fetch path reads it back.
        self._hosts: dict[str, dict] = {}
        self.stats = {"requests": 0, "cache_hits": 0, "suppressed": 0,
                      "admitted": 0}

    def _curl(self, args: list[str], timeout: float):
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

    def _fetch(self, url: str) -> dict:
        # Curl is used for public web data because urllib is rejected by this
        # provider in the tested environment. No cookie/key/wallet access.
        #
        # `-f` is deliberately absent. With it, curl printed nothing for a 4xx
        # and the only trace was an opaque exit code; the status and headers now
        # come back through `--write-out`, so a 429 can be read as a 429 and its
        # `Retry-After` can be honoured instead of guessed at.
        args = ["curl", "-sS", "--connect-timeout", "8", "--max-time", "20",
                "--max-filesize", "10000000", "-A", "ArbMonitor/0.1",
                "-w", _TRAILER + "%{http_code}\n%{header_json}"]
        if self.proxy:
            args += ["--proxy", self.proxy]
        started = now_ms()
        proc = self._runner(args + [url], 25)
        stderr = proc.stderr or ""
        body_text, status, headers = _split_trailer(proc.stdout or "")
        if status is None:
            # No trailer: curl never got far enough to answer. Older curl builds
            # and injected runners only leave the status in stderr, so that is
            # still read rather than discarding what we know.
            found = _STATUS_RE.search(stderr)
            if found:
                raise HTTPStatusError(int(found.group(1)), url, stderr.strip())
            raise PublicHTTPError(
                f"public_http_failed:{proc.returncode}:{stderr[:180]}")
        if not 200 <= status < 300:
            raise HTTPStatusError(status, url, f"http_status:{status}",
                                  retry_after_ms=_retry_after_ms(headers))
        if proc.returncode:
            raise PublicHTTPError(
                f"public_http_failed:{proc.returncode}:{stderr[:180]}")
        try:
            raw = json.loads(body_text, parse_float=Decimal)
        except ValueError as exc:
            raise PublicHTTPError(
                f"public_http_unparseable_body:{type(exc).__name__}")
        return {"request_ts_ms": started, "recv_ts_ms": now_ms(), "source": url,
                "raw": raw}

    def get(self, url: str, *, max_age_ms: int = 0) -> dict:
        """Read ``url``, honouring the memory described on the class."""
        host = urlsplit(url).netloc
        now = self._clock()
        with self._lock:
            dead = self._dead.get(url)
            if dead and now - dead[0] < self.permanent_ttl_ms:
                self.stats["suppressed"] += 1
                self._note_suppressed(host)
                raise PublicHTTPError("public_http_suppressed:permanent:" + dead[1])
            cooling = self._cooling.get(host)
            if cooling and now < cooling[0]:
                self.stats["suppressed"] += 1
                self._note_suppressed(host)
                raise PublicHTTPError("public_http_suppressed:cooling:" + cooling[2])
            fresh = self._fresh.get(url)
            if fresh and max_age_ms > 0 and now - fresh[0] <= max_age_ms:
                self.stats["cache_hits"] += 1
                return {**fresh[1], "cache_hit": True,
                        "cache_age_ms": now - fresh[0]}
            refusal = self._claim_host(host)
            if refusal is not None:
                self.stats["suppressed"] += 1
                self._note_suppressed(host)
                raise PublicHTTPError(refusal)
        self.stats["requests"] += 1
        # The cap is enforced outside the lock: a bounded wait is correct here
        # ("the host is busy"), whereas refusing would record "we could not
        # read" for something that was simply not asked yet.
        semaphore = self._host_semaphore(host)
        semaphore.acquire()
        try:
            try:
                receipt = self._fetch(url)
            except HTTPStatusError as exc:
                with self._lock:
                    if exc.permanent:
                        # A dead URL, not a sick host: remembering it must not
                        # put the host's other reads on a cooldown.
                        self._dead[url] = (now, str(exc))
                    else:
                        self._cool(host, now, str(exc),
                                   wait_ms=getattr(exc, "retry_after_ms", None))
                raise
            except PublicHTTPError as exc:
                with self._lock:
                    self._cool(host, now, str(exc))
                raise
        finally:
            # Any exit from a claimed probe frees it. A flag left set would
            # suppress the host forever, which is strictly worse than asking
            # once more.
            with self._lock:
                self._recovering_probe[host] = False
            semaphore.release()
        with self._lock:
            self._fresh[url] = (now, receipt)
            self._dead.pop(url, None)
            self._cooling.pop(host, None)
            self._note_success(host, now)
        return {**receipt, "cache_hit": False, "cache_age_ms": 0}

    def _host_semaphore(self, host: str) -> threading.Semaphore:
        with self._lock:
            semaphore = self._semaphores.get(host)
            if semaphore is None:
                semaphore = self._semaphores[host] = threading.Semaphore(
                    self.max_per_host)
            return semaphore

    def _claim_host(self, host: str) -> str | None:
        """Decide, atomically, whether this caller may touch the host.

        Returns ``None`` when admitted, or the suppression reason. Only the
        recovering case refuses: a host with a failure streak gets **one** probe
        request at a time, because that is what "try again once" means. Letting
        the whole 46-call fan-out fire the instant the cooldown expires would
        turn a probe into the same burst that caused the 429.
        """
        entry = self._hosts.setdefault(host, {})
        if int(entry.get("consecutive_failures", 0)) < RECOVERING_AFTER:
            return None
        if self._recovering_probe.get(host):
            return ("public_http_suppressed:recovering:"
                    + str(entry.get("last_reason") or ""))
        self._recovering_probe[host] = True
        return None

    def _cool(self, host: str, now: int, reason: str,
              wait_ms: int | None = None) -> None:
        """Back a host off. Caller holds the lock.

        ``wait_ms`` is the host's own `Retry-After`, which wins when present: it
        is the one number that is about *this* host's situation rather than our
        guess about it. Otherwise the wait doubles per consecutive failure, with
        jitter so that a round does not re-synchronise with the next.
        """
        previous = self._cooling.get(host)
        if wait_ms is not None:
            wait = max(int(wait_ms), 0)
        else:
            wait = (self.cooldown_ms if previous is None
                    else min(previous[1] * 2, self.cooldown_max_ms))
            if self.jitter:
                wait = int(wait * (1 + self.jitter * (self._rand() * 2 - 1)))
            wait = max(1, min(wait, self.cooldown_max_ms))
        self._cooling[host] = (now + wait, wait, reason)
        self._note_failure(host, now, reason, now + wait)

    def _note_success(self, host: str, now: int) -> None:
        """A read answered. Caller holds the lock."""
        entry = self._hosts.setdefault(host, {})
        entry.update(last_success_ms=now, consecutive_failures=0,
                     next_retry_ms=0, last_status="ok")

    def _note_suppressed(self, host: str) -> None:
        """A read was skipped because the host is cooling. Caller holds the lock.

        A suppression is not a new failure: it is the memory doing its job, so
        it must not extend the backoff. It is counted because "how many calls
        the cooldown saved" is the number that shows whether it is working.
        """
        entry = self._hosts.setdefault(host, {})
        entry["suppressed"] = int(entry.get("suppressed", 0)) + 1

    def _note_failure(self, host: str, now: int, reason: str,
                      next_retry_ms: int) -> None:
        """A read failed (not a suppression). Caller holds the lock."""
        entry = self._hosts.setdefault(host, {})
        entry.update(last_failure_ms=now,
                     consecutive_failures=int(entry.get("consecutive_failures", 0)) + 1,
                     next_retry_ms=int(next_retry_ms),
                     last_status="error", last_reason=str(reason)[:180])

    @staticmethod
    def _host_state(entry: dict, now: int) -> str:
        """``healthy`` / ``backoff`` / ``open`` / ``recovering``.

        Derived at read time from the same fields the fetch path already keeps,
        so the label can never drift from the behaviour it describes.
        """
        failures = int(entry.get("consecutive_failures", 0))
        until = int(entry.get("next_retry_ms", 0) or 0)
        if until and now < until:
            return "open" if failures >= 2 else "backoff"
        if failures >= 2:
            # Past the retry time with a failure streak: the next call is the
            # single probe that decides whether the host comes back.
            return "recovering"
        return "healthy"

    def host_health(self, now: int | None = None) -> dict:
        """Read-only snapshot of every host this adapter has touched."""
        moment = self._clock() if now is None else int(now)
        with self._lock:
            snapshot = {}
            for host, entry in self._hosts.items():
                until = int(entry.get("next_retry_ms", 0) or 0)
                snapshot[host] = {
                    "state": self._host_state(entry, moment),
                    "consecutive_failures": int(entry.get("consecutive_failures", 0)),
                    "last_success_ms": entry.get("last_success_ms"),
                    "last_failure_ms": entry.get("last_failure_ms"),
                    "next_retry_ms": until or None,
                    "retry_in_ms": max(0, until - moment) if until else 0,
                    "last_status": entry.get("last_status"),
                    "last_reason": entry.get("last_reason"),
                    "suppressed": int(entry.get("suppressed", 0)),
                }
        return snapshot


class ReferenceAdapter:
    def __init__(self, http: PublicHTTP, *, daily_ttl_ms: int = 900_000,
                 reference_ttl_ms: int = 600_000):
        self.http = http
        # A daily candle and a pool's reserve change on their own clock, not on
        # ours. These are reuse windows, not freshness claims: every reused row
        # carries `cache_hit`/`cache_age_ms` and the original observation time.
        self.daily_ttl_ms = int(daily_ttl_ms)
        self.reference_ttl_ms = int(reference_ttl_ms)

    def token(self, token: dict) -> dict:
        receipt = self.http.get(
            "https://web3.okx.com/priapi/v1/dx/market/v2/search?keyword=" + quote(token["symbol"]),
            max_age_ms=self.reference_ttl_ms)
        matches = [
            r for r in receipt["raw"].get("data", [])
            if r.get("chainId") == token["chain_id"]
            and r.get("tokenContractAddress", "").lower() == token["address"].lower()
        ]
        if len(matches) != 1:
            raise ValueError("token identity absent/ambiguous; ticker match is insufficient")
        r = matches[0]
        if int(r["decimal"]) != token["decimals"]:
            raise ValueError("token decimals changed")
        return {**receipt, "asset": token["asset"], "kind": "reference_not_executable",
                "price_usd": Decimal(r["price"]), "venue_ts_ms": None,
                "liquidity_usd": Decimal(r["liquidity"]), "token": r}

    def pool(self, token: dict) -> dict:
        address = token.get("pool")
        path = f"pools/{address}" if address else f"tokens/{token['address']}/pools"
        return self.http.get(
            f"https://api.geckoterminal.com/api/v2/networks/x-layer/{path}"
            "?include=base_token,quote_token",
            max_age_ms=self.reference_ttl_ms)

    def pool_spot(self, token: dict) -> dict:
        """Best public pool reference, explicitly non-executable."""
        receipt = self.http.get(
            f"https://api.geckoterminal.com/api/v2/networks/x-layer/tokens/{token['address']}/pools"
            "?include=base_token,quote_token",
            max_age_ms=self.reference_ttl_ms)
        rows = receipt["raw"].get("data", [])
        candidates = []
        for row in rows:
            attr = row.get("attributes", {})
            rel = row.get("relationships", {})
            base = rel.get("base_token", {}).get("data", {}).get("id", "").lower()
            if not base.endswith(token["address"].lower()):
                continue
            quote = attr.get("base_token_price_quote_token")
            price = attr.get("base_token_price_usd")
            if price is None or quote is None:
                continue
            try:
                candidates.append((Decimal(attr.get("reserve_in_usd", "0")), Decimal(price), row["id"]))
            except Exception:
                continue
        if not candidates:
            raise ValueError("no_public_xlayer_pool_reference")
        reserve, price, pool_id = max(candidates, key=lambda x: x[0])
        return {**receipt, "asset": token["asset"], "pool_price_usd": price,
                "pool_id": pool_id, "pool_reserve_usd": reserve,
                "kind": "pool_reference_not_executable"}

    def stock_daily(self, token: dict) -> dict:
        suffix = "" if token.get("listing_country") == "US" else ".HK"
        symbol = str(token.get("stock_symbol") or token.get("hk_code") or "")
        if suffix and symbol.isdigit() and len(symbol) < 4:
            # HKEX codes are quoted with four digits, and the rest of this repo
            # already writes them that way (``account_dashboard`` uses
            # ``f"{int(code):04d}"``). The issuer's own field is unpadded, so
            # SHEIN arrives as ``625`` and HKEXCx as ``388``.
            # Measured 2026-09-21: 625.HK → 404, 0625.HK → 200; 388.HK → 404,
            # 0388.HK → 200. Every 404 of that day (2,967 rows) came from these
            # two names — SHEIN had never once had a daily reference.
            symbol = symbol.zfill(4)
        return self.http.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            + quote(symbol + suffix) + "?range=1mo&interval=1d",
            max_age_ms=self.daily_ttl_ms)

    def hkd_daily(self) -> dict:
        return self.http.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/HKD=X?range=1mo&interval=1d",
            max_age_ms=self.daily_ttl_ms)
