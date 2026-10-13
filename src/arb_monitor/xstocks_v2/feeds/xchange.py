"""xChange soft quotes. Read-only. Nothing here signs or trades.

xChange is the issuer's atomic RFQ. The scan asks
``POST /trades/xchange/rfq/soft``: a short-lived price, no
signature, no wallet, nothing reserved. The hard RFQ that returns an on-chain
authorization is a different request and is not implemented here — a scan must
not create one.

Order bounds on this endpoint are cents (the integration guide). A missing API
key does not send a request.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from arb_monitor.xstocks_v2 import rotation as rotation_core
from arb_monitor.xstocks_v2.feeds.http import PROXY, Http, RateLimiter
from arb_monitor.xstocks_v2.spot_venue import BUY, REAL, SELL, Quote

ROOT = Path(__file__).resolve().parents[4]
ENV_FILE = ROOT / "config" / "xchange.env"
ENV_NAME = "XSTOCKS_XCHANGE_API_KEY"
API_BASE = "https://api.backed.fi/api/v2"
ASSETS_PATH = "/trades/xchange/assets"
SOFT_PATH = "/trades/xchange/rfq/soft"

#: API network name -> the chain slug the rest of v2 uses.
NETWORKS = {
    "Solana": "solana",
    "Ethereum": "eth",
    "Ink": "ink",
    "BNB Smart Chain": "bsc",
    "BinanceSmartChain": "bsc",
}
#: Chains priced by this RFQ instead of a per-token factory walk.
RFQ_INSTEAD_OF_FACTORY = ("ink", "bsc")
CHAIN_NETWORK = {chain: name for name, chain in NETWORKS.items()}
QUOTE_CHAINS = frozenset(CHAIN_NETWORK)

#: Long-tail names (Kraken-listed, not already quoted this round) per round,
#: and how many names that cleared the buffer stay on every later round.
ROTATION_SLOTS = 8
HOT_MAX = 12
#: Three closed answers in a row means the session is shut. Later names in the
#: same round are stamped closed and not asked again.
CLOSED_STREAK = 3
MAX_PAGES = 60
DEFAULT_RPS = 2.0
PAUSE_S = 2.0

CLOSED = "xchange_closed"
UNCONFIGURED = "xchange_unconfigured"
BELOW_MIN = "below_min:xchange"
ABOVE_MAX = "above_max:xchange"
EXPIRED = "xchange_expired"
UNAUTHORIZED = "xchange_unauthorized"
RATE_LIMITED = "xchange_rate_limited"
PRICE_UNAVAILABLE = "xchange_price_unavailable"


def read_api_key(env: dict | None = None, path: Path | str | None = None) -> str:
    """The key from the environment, else from ``config/xchange.env``.

    An empty string means "not configured". Callers must not send a request.
    """
    source = os.environ if env is None else env
    key = str(source.get(ENV_NAME) or "").strip()
    if key:
        return key
    try:
        text = Path(path if path is not None else ENV_FILE).read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        name, _, value = line.partition("=")
        if name.strip() == ENV_NAME:
            return value.strip().strip('"').strip("'")
    return ""


def _decimal(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def cents_to_usd(value) -> Decimal | None:
    """xChange ``minOrderFiatValue`` / ``maxOrderFiatValue`` are integer cents."""
    number = _decimal(value)
    if number is None:
        return None
    return number / Decimal(100)


def iso_ms(value) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _nodes(payload) -> tuple[list, bool]:
    """``(asset nodes, has_next_page)`` from one catalog page."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)], False
    if not isinstance(payload, dict):
        return [], False
    nodes = None
    for key in ("nodes", "data", "assets", "items"):
        if isinstance(payload.get(key), list):
            nodes = payload[key]
            break
    if nodes is None:
        nodes = []
    page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
    has_next = bool(page.get("hasNextPage") or payload.get("hasNextPage"))
    return [item for item in nodes if isinstance(item, dict)], has_next


def _symbol_of(node: dict) -> str:
    for key in ("identifier", "symbol", "asset"):
        text = str(node.get(key) or "").strip()
        if text:
            return text
    return ""


def _closed(node: dict) -> bool:
    if node.get("isTradingHalted") is True:
        return True
    for key in ("tradingOpen", "isMarketOpen", "marketOpen"):
        if node.get(key) is False:
            return True
    status = str(node.get("tradingStatus") or node.get("status") or "").lower()
    if status in {"halted", "closed", "suspended", "outside_hours"}:
        return True
    hours = node.get("tradingHours")
    if isinstance(hours, dict) and hours.get("open") is False:
        return True
    return False


def _network_names(node: dict) -> list[str]:
    names: list[str] = []
    raw = node.get("networks") or node.get("supportedNetworks")
    if isinstance(raw, list):
        names.extend(str(item) for item in raw if item)
    for dep in node.get("deployments") or []:
        if isinstance(dep, dict) and dep.get("network"):
            names.append(str(dep["network"]))
    return names


@dataclass(frozen=True)
class Asset:
    """One xStock as the xChange catalog describes it."""

    symbol: str
    halted: bool
    min_usd: Decimal | None
    max_usd: Decimal | None
    timeout_s: int | None
    chains: frozenset
    ignored_networks: frozenset

    @property
    def closed(self) -> bool:
        return self.halted


def parse_asset(node: dict) -> tuple[Asset | None, frozenset]:
    """One catalog node. Unknown networks are returned, not quoted."""
    if not isinstance(node, dict):
        return None, frozenset()
    symbol = _symbol_of(node)
    if not symbol:
        return None, frozenset()
    chains: set[str] = set()
    ignored: set[str] = set()
    named = _network_names(node)
    if not named:
        chains = set(QUOTE_CHAINS)
    else:
        for name in named:
            chain = NETWORKS.get(name)
            if chain is None:
                ignored.add(name)
            else:
                chains.add(chain)
    min_usd = _decimal(node.get("minOrderFiatValueUsd"))
    if min_usd is None:
        min_usd = cents_to_usd(node.get("minOrderFiatValue"))
    max_usd = _decimal(node.get("maxOrderFiatValueUsd"))
    if max_usd is None:
        max_usd = cents_to_usd(node.get("maxOrderFiatValue"))
    timeout = node.get("executionTimeoutSeconds")
    try:
        timeout_s = int(timeout) if timeout is not None else None
    except (TypeError, ValueError):
        timeout_s = None
    asset = Asset(
        symbol=symbol, halted=_closed(node), min_usd=min_usd, max_usd=max_usd,
        timeout_s=timeout_s, chains=frozenset(chains),
        ignored_networks=frozenset(ignored))
    return asset, frozenset(ignored)


def size_blocker(asset: Asset, cash_usd: Decimal) -> str:
    """Empty when ``cash_usd`` is inside the catalog bounds.

    A missing bound is not a refusal: the quote itself will say if the venue
    rejects the size. A known bound that the ticket misses is a refusal, and
    it does not need a request.
    """
    if asset.closed:
        return CLOSED
    if asset.min_usd is not None and cash_usd < asset.min_usd:
        return BELOW_MIN
    if asset.max_usd is not None and cash_usd > asset.max_usd:
        return ABOVE_MAX
    return ""


def error_code(payload) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("code", "errorCode"):
        if payload.get(key):
            return str(payload[key])
    err = payload.get("error")
    if isinstance(err, dict):
        return str(err.get("code") or err.get("errorCode") or "")
    if isinstance(err, str):
        return err
    return ""


def classify_error(status: int, payload) -> str:
    """A machine reason. Closed / size / auth are stable; the rest keep the status."""
    code = error_code(payload).upper()
    blob = code
    if isinstance(payload, dict):
        blob = (code + " " + json.dumps(payload, default=str)[:300]).upper()
    if status == 401 or "UNAUTHORIZED" in blob or "API_KEY" in blob:
        return UNAUTHORIZED
    if status == 429:
        return RATE_LIMITED
    if any(token in blob for token in (
            "HALT", "CLOSED", "OUTSIDE_TRADING", "MARKET_CLOSED", "NOT_TRADING")):
        return CLOSED
    if "BELOW_MINIMUM" in blob or "BELOW_MIN" in blob:
        return BELOW_MIN
    if "ABOVE_MAXIMUM" in blob or "ABOVE_MAX" in blob:
        return ABOVE_MAX
    if status == 424 or "PRICE_" in blob:
        return PRICE_UNAVAILABLE
    return f"xchange_http_{status}"


def _loads(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8", "replace"), parse_float=Decimal)
    except (ValueError, AttributeError, UnicodeError):
        return None


def quote_from_soft(*, symbol: str, chain: str, side: str, payload: dict,
                    recv_ms: int, now_ms: int) -> tuple[Quote | None, str]:
    """A real quote, or ``("", reason)``. An expired soft quote is not a price."""
    if not isinstance(payload, dict):
        return None, "xchange_bad_payload"
    expires = iso_ms(payload.get("expiresAt"))
    if expires is not None and expires <= int(now_ms):
        return None, EXPIRED
    cash = _decimal(payload.get("cashAmount"))
    qty = _decimal(payload.get("quantity"))
    price = _decimal(payload.get("price"))
    if cash is not None and qty is not None and qty > 0 and cash > 0:
        price_usd = cash / qty
    elif price is not None and price > 0:
        price_usd = price
    else:
        return None, "xchange_bad_payload"
    return Quote(
        venue="xchange", chain=chain, symbol=symbol,
        side=BUY if side == "Buy" else SELL, kind=REAL,
        price_usd=price_usd, recv_ms=recv_ms,
        qty_token=qty, qty_quote=cash, source="xchange:soft",
        depth_usd=cash, pool_id=f"xchange:{chain}",
        extra={
            "soft_id": str(payload.get("id") or ""),
            "expires_at_ms": expires,
            "api_price": None if price is None else format(price, "f"),
        },
    ), ""


class XchangeFeed:
    """One limiter, catalog + soft quotes. Hard RFQ is not a method on this class."""

    def __init__(self, http: Http | None = None, *, api_key: str | None = None,
                 base: str = API_BASE, rps: float = DEFAULT_RPS,
                 limiter: RateLimiter | None = None):
        self.http = http or Http(proxy=PROXY)
        self.api_key = read_api_key() if api_key is None else str(api_key).strip()
        self.base = base.rstrip("/")
        self.limiter = limiter or RateLimiter(rps)
        self.calls = 0
        self.http_429 = 0
        self.ignored_networks: set[str] = set()
        self.session_closed = False
        self._closed_streak = 0
        self._stop = ""

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict:
        return {"X-API-KEY": self.api_key, "Content-Type": "application/json"}

    def _exchange(self, path: str, *, payload: dict | None = None
                  ) -> tuple[int, object | None]:
        if not self.configured or self._stop:
            return 0, None
        url = f"{self.base}{path}"
        body = None if payload is None else json.dumps(payload).encode()
        self.limiter.acquire()
        self.calls += 1
        try:
            status, raw = self.http.request(url, data=body, headers=self._headers())
        except Exception:  # noqa: BLE001 -- a dead feed must not kill the scan
            return 0, None
        if status == 429:
            self.http_429 += 1
            self.limiter.pause(PAUSE_S)
        return status, _loads(raw)

    def catalog(self) -> tuple[dict[str, Asset], str]:
        """``({symbol: Asset}, note)``. No key → empty and ``xchange_unconfigured``."""
        if not self.configured:
            return {}, UNCONFIGURED
        status, payload = self._exchange(ASSETS_PATH)
        if status == 401:
            self._stop = UNAUTHORIZED
            return {}, UNAUTHORIZED
        if status != 200:
            return {}, classify_error(status, payload) if status else "xchange_transport"
        assets: dict[str, Asset] = {}
        pages = 0
        previous: frozenset[str] | None = None
        page = 1
        while True:
            nodes, has_next = _nodes(payload)
            current: set[str] = set()
            for node in nodes:
                asset, ignored = parse_asset(node)
                self.ignored_networks.update(ignored)
                if asset is None:
                    continue
                assets[asset.symbol] = asset
                current.add(asset.symbol)
            pages += 1
            frozen = frozenset(current)
            if frozen and frozen == previous:
                break
            previous = frozen
            if not has_next or pages >= MAX_PAGES:
                break
            status, payload = self._exchange(f"{ASSETS_PATH}?page={page}")
            page += 1
            if status != 200:
                break
        if not assets:
            return {}, "xchange_catalog_empty"
        return assets, "ok"

    def _soft(self, *, symbol: str, chain: str, side: str, cash_usd: Decimal,
              recv_ms: int, now_ms: int) -> tuple[Quote | None, str]:
        if self._stop:
            return None, self._stop
        if self.session_closed:
            return None, CLOSED
        network = CHAIN_NETWORK[chain]
        body = {
            "identifier": symbol,
            "side": side,
            "network": network,
            "cashAmount": format(cash_usd, "f"),
        }
        reason = ""
        for attempt in range(2):
            status, payload = self._exchange(SOFT_PATH, payload=body)
            if status == 429 and attempt == 0:
                continue
            if status == 200 and isinstance(payload, dict):
                quote, reason = quote_from_soft(
                    symbol=symbol, chain=chain, side=side, payload=payload,
                    recv_ms=recv_ms, now_ms=now_ms)
                if quote is not None:
                    self._closed_streak = 0
                    return quote, ""
                return None, reason or "xchange_bad_payload"
            reason = classify_error(status, payload) if status else "xchange_transport"
            break
        if reason == CLOSED:
            self._closed_streak += 1
            if self._closed_streak >= CLOSED_STREAK:
                self.session_closed = True
        elif reason == UNAUTHORIZED:
            self._stop = UNAUTHORIZED
        return None, reason

    def pair_entry(self, asset: Asset, chain: str, cash_usd: Decimal, *,
                   recv_ms: int, now_ms: int | None = None) -> dict | None:
        """One ``chain_quotes`` entry: buy and sell at the same cash ticket.

        ``None`` means this asset is not offered on ``chain``. A closed market,
        a ticket outside the catalog bounds, or a failed request still returns
        an entry so the scan can print the reason instead of dropping the name.
        """
        if chain not in QUOTE_CHAINS or chain not in asset.chains:
            return None
        now = int(recv_ms if now_ms is None else now_ms)
        cash = Decimal(cash_usd)
        entry = {
            "symbol": asset.symbol, "chain": chain,
            "pool_id": f"xchange:{chain}", "dex": "xchange",
            "source": "xchange:soft", "kind": "xchange",
            "errors": [], "xchange_blockers": [],
        }
        blocked = ""
        if not self.configured:
            blocked = UNCONFIGURED
        elif self._stop:
            blocked = self._stop
        elif self.session_closed or asset.closed:
            blocked = CLOSED
        else:
            blocked = size_blocker(asset, cash)
        if blocked:
            entry["errors"] = [blocked]
            entry["xchange_blockers"] = [blocked]
            return entry
        buy, buy_reason = self._soft(
            symbol=asset.symbol, chain=chain, side="Buy", cash_usd=cash,
            recv_ms=recv_ms, now_ms=now)
        sell, sell_reason = None, ""
        # A session-level refusal (closed, unauthorized) applies to both sides.
        # Asking the sell after the buy already proved the market is shut only
        # burns the budget.
        if buy is None and buy_reason in {CLOSED, UNAUTHORIZED, RATE_LIMITED}:
            entry["errors"] = [buy_reason]
            entry["xchange_blockers"] = [buy_reason]
            return entry
        if buy is not None:
            entry["buy"] = buy
        elif buy_reason:
            entry["errors"].append(f"buy:{buy_reason}")
        if not self.session_closed and not self._stop:
            sell, sell_reason = self._soft(
                symbol=asset.symbol, chain=chain, side="Sell", cash_usd=cash,
                recv_ms=recv_ms, now_ms=now)
        elif self.session_closed or self._stop:
            sell_reason = self._stop or CLOSED
        if sell is not None:
            entry["sell"] = sell
        elif sell_reason:
            entry["errors"].append(f"sell:{sell_reason}")
        if entry.get("buy") is None and entry.get("sell") is None and entry["errors"]:
            # No price on either side: the row has to name why, or the symbol
            # disappears. Prefixed ``buy:`` / ``sell:`` notes collapse to the
            # reason; distinct reasons are both kept.
            reasons: list[str] = []
            for item in entry["errors"]:
                text = str(item)
                reason = text.split(":", 1)[-1] if text.startswith(("buy:", "sell:")) else text
                if reason not in reasons:
                    reasons.append(reason)
            entry["xchange_blockers"] = reasons
        return entry


def load_state(path: Path | str) -> tuple[dict, list]:
    """``(rotation rails, hot symbols)``. A missing file is an empty state."""
    try:
        payload = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}, []
    if not isinstance(payload, dict):
        return {}, []
    rails = payload.get("rails") if isinstance(payload.get("rails"), dict) else {}
    hot = [str(item) for item in (payload.get("hot") or []) if item]
    return {str(k): v for k, v in rails.items() if isinstance(v, dict)}, hot


def save_state(path: Path | str, rails: dict, hot, *, now_ms: int) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "saved_ms": int(now_ms),
        "rails": rails,
        "hot": [str(item) for item in hot],
    }, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    return target


def plan_extra_symbols(*, online, catalog_symbols, already, hot, rails,
                       now_ms: int, slots: int = ROTATION_SLOTS,
                       hot_max: int = HOT_MAX) -> tuple[list, list, list]:
    """``(hot_to_quote, rotation_to_quote, waiting_queue)``.

    ``already`` were xChange-quoted on Solana in this round's normal batches.
    Hot names from the previous round are asked again, up to ``hot_max``. The
    rest of the Kraken-online catalog rotates ``slots`` names, longest wait
    first. A name in neither set is still in the queue for a later round.
    """
    listed = {str(symbol) for symbol in catalog_symbols}
    online_set = {str(symbol) for symbol in online}
    done = {str(symbol) for symbol in already}
    hot_pick: list[str] = []
    for symbol in hot or []:
        symbol = str(symbol)
        if symbol in listed and symbol in online_set and symbol not in done:
            hot_pick.append(symbol)
        if len(hot_pick) >= max(0, int(hot_max)):
            break
    covered = done | set(hot_pick)
    candidates = [symbol for symbol in online_set
                  if symbol in listed and symbol not in covered]
    queue = rotation_core.waiting(rails, candidates=candidates, now_ms=now_ms)
    picks = [str(item["symbol"]) for item in queue[:max(0, int(slots))]]
    return hot_pick, picks, queue


def hot_from_rows(rows, *, floor_bps: Decimal, limit: int = HOT_MAX) -> list[str]:
    """Symbols whose xChange-vs-Kraken spread cleared the stage-2 buffer.

    Highest edge first, at most ``limit`` names. The next round quotes these
    every time instead of waiting for the rotation.
    """
    ranked: list[tuple[Decimal, str]] = []
    for row in rows or []:
        if row.get("family") not in {"chain_to_kraken", "kraken_to_chain"}:
            continue
        if row.get("buy_venue") != "xchange" and row.get("sell_venue") != "xchange":
            continue
        edge = _decimal(row.get("edge_t0_bps"))
        symbol = str(row.get("symbol") or "")
        if edge is None or not symbol or edge < floor_bps:
            continue
        ranked.append((edge, symbol))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    out: list[str] = []
    for _edge, symbol in ranked:
        if symbol in out:
            continue
        out.append(symbol)
        if len(out) >= max(0, int(limit)):
            break
    return out


def without_counterparty(*, catalog_symbols, kraken_online, pool_symbols) -> list[str]:
    """Catalog names with no Kraken listing and no pool. They are not quoted."""
    other = {str(s) for s in kraken_online} | {str(s) for s in pool_symbols}
    return sorted({str(s) for s in catalog_symbols} - other)
