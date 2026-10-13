"""Kraken public market data, both sides.

The public ``Ticker`` already answers both sides of every pair in one call:
``b`` is the best bid (what we get when we sell) and ``a`` is the best ask
(what we pay when we buy). v1 read only ``b`` because its only exit was Kraken.
That is why the any-to-any upgrade needs no extra Kraken budget for the cheap
stage of the scan.
"""
from __future__ import annotations

import time
import urllib.parse
from decimal import Decimal

from arb_monitor.xstocks_v2.feeds.http import first_decimal

BASE = "https://api.kraken.com"
TOKENIZED = "tokenized_asset"
# Kraken accepts a comma list. The v1 board sends all ~177 xStocks in one call;
# the chunk exists so a caller with more symbols than that still works.
TICKER_CHUNK = 120
DEPTH_CHUNK = 10


def _now_ms() -> int:
    return int(time.time() * 1000)


def _result_rows(payload: object):
    """Kraken's ``result`` is a dict of pair -> row. Anything else is a bad row."""
    result = (payload or {}).get("result") if isinstance(payload, dict) else None
    return result.items() if isinstance(result, dict) else []


def symbol_of(key: object, quote: str = "USD") -> str:
    """``MRNAx/USD`` and ``MRNAXUSD`` both mean MRNAx."""
    text = str(key)
    if "/" in text:
        return text.split("/", 1)[0]
    upper, tail = text.upper(), quote.upper()
    return text[: -len(tail)] if upper.endswith(tail) else text


def parse_ticker(payload: object, *, recv_ms: int | None = None,
                 quote: str = "USD") -> dict[str, dict]:
    """Ticker payload -> ``{symbol: {bid, ask, recv_ms, source}}``.

    A side that is missing or zero stays ``None``. It is not a zero price: the
    venue simply did not quote that side, and a candidate must not be built
    from a fabricated 0 (lessons 2.7).
    """
    stamp = recv_ms if recv_ms is not None else _now_ms()
    out: dict[str, dict] = {}
    for key, row in _result_rows(payload):
        if not isinstance(row, dict):
            continue
        bid = first_decimal(row.get("b"))
        ask = first_decimal(row.get("a"))
        if bid is None and ask is None:
            continue
        out[symbol_of(key, quote)] = {
            "bid": bid, "ask": ask, "recv_ms": stamp, "source": "kraken:ticker",
        }
    return out


def ticker_url(symbols, *, quote: str = "USD", tokenized: bool = True) -> str:
    pairs = ",".join(f"{symbol}/{quote}" for symbol in symbols)
    url = f"{BASE}/0/public/Ticker?pair={urllib.parse.quote(pairs)}"
    # Tokenized xStocks need the asset_class and the BASE/QUOTE spelling.
    # Native pairs such as SOL/USD reject both (verified by the v1 client).
    return url + (f"&asset_class={TOKENIZED}&assetVersion=1" if tokenized else "")


def parse_depth(payload: object, *, recv_ms: int | None = None,
                quote: str = "USD") -> dict[str, dict]:
    """Depth payload -> ``{symbol: {bids, asks, recv_ms, source}}``."""
    stamp = recv_ms if recv_ms is not None else _now_ms()
    out: dict[str, dict] = {}
    for key, row in _result_rows(payload):
        if not isinstance(row, dict):
            continue
        out[symbol_of(key, quote)] = {
            "bids": list(row.get("bids") or []),
            "asks": list(row.get("asks") or []),
            "recv_ms": stamp,
            "source": "kraken:depth",
        }
    return out


def depth_url(symbols, *, quote: str = "USD", count: int = 25) -> str:
    pairs = ",".join(f"{symbol}/{quote}" for symbol in symbols)
    return (f"{BASE}/0/public/Depth?pair={urllib.parse.quote(pairs)}"
            f"&asset_class={TOKENIZED}&assetVersion=1&count={count}")


def _chunks(items, size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def fetch_ticker(http, symbols, *, quote: str = "USD", tokenized: bool = True,
                 chunk: int = TICKER_CHUNK) -> dict[str, dict]:
    """Best bid and best ask for every symbol. One call for the normal case."""
    unique = [symbol for symbol in dict.fromkeys(symbols) if symbol]
    out: dict[str, dict] = {}
    errors: list[str] = []
    for group in _chunks(unique, chunk):
        recv_ms = _now_ms()
        try:
            status, payload, _headers = http.get_json(
                ticker_url(group, quote=quote, tokenized=tokenized))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}")
            continue
        if status != 200 or not isinstance(payload, dict) or payload.get("error"):
            errors.append(f"http_{status}")
            continue
        out.update(parse_ticker(payload, recv_ms=recv_ms, quote=quote))
    return {"ticker": out, "errors": errors}


def native_usd(http, tokens) -> dict[str, Decimal]:
    """USD price of each gas token, from Kraken's public book.

    One implementation on purpose. The paper runner grew this to price a chain
    transfer fee; the live runner needs the same number for the same row, and
    two sources for one cost is exactly how the board and the gate end up
    publishing two nets for one trade (2026-10-08).

    An unknown price stays *out* of the dict, so the caller sees "unknown" and
    can refuse. It never becomes zero.
    """
    wanted = [token for token in dict.fromkeys(tokens) if token]
    if not wanted:
        return {}
    result = fetch_ticker(http, wanted, tokenized=False)
    out: dict[str, Decimal] = {}
    for token, row in (result.get("ticker") or {}).items():
        price = row.get("bid") or row.get("ask")
        if price and price > 0:
            out[token] = price
    return out


def fetch_depth(http, symbols, *, quote: str = "USD", count: int = 25,
                chunk: int = DEPTH_CHUNK) -> dict[str, dict]:
    """Level-2 book for the few names that reached the deep stage."""
    unique = [symbol for symbol in dict.fromkeys(symbols) if symbol]
    out: dict[str, dict] = {}
    errors: list[str] = []
    for group in _chunks(unique, chunk):
        recv_ms = _now_ms()
        try:
            status, payload, _headers = http.get_json(
                depth_url(group, quote=quote, count=count))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}")
            continue
        if status != 200 or not isinstance(payload, dict) or payload.get("error"):
            errors.append(f"http_{status}")
            continue
        out.update(parse_depth(payload, recv_ms=recv_ms, quote=quote))
    return {"depth": out, "errors": errors}


def book_side(levels, *, side: str) -> Decimal | None:
    """Best price on one side of a Kraken book. ``side`` is buy or sell."""
    picked = [first_decimal(level) for level in levels or []]
    picked = [price for price in picked if price is not None]
    if not picked:
        return None
    # Kraken sorts bids high-to-low and asks low-to-high, but do not rely on it.
    return max(picked) if side == "sell" else min(picked)


def book_fill(levels, units, *, side: str = "sell") -> dict:
    """Walk one side of the book for ``units`` and return the **fill price**.

    Pure arithmetic, no economic judgement (operator 2026-10-08, AGENTS.md
    「第一原则：赚钱优先」): the orderbook does not decide whether a trade is
    good, it only answers "what average price does this amount get". The profit
    test decides. So there is no slippage cap and no book-share cap here --
    both of those lived in ``venues/kraken_exit_gate.py`` and were vetoes.

    ``side="sell"`` walks the bids (we sell into them), ``side="buy"`` walks the
    asks. ``units`` is in the venue's quote unit (Kraken prices xStocks per
    credit unit). Levels are sorted here on purpose: Kraken returns bids
    high-to-low and asks low-to-high, but a caller that hands over an unsorted
    list must not get a silently wrong VWAP.

    ``ok`` is False when the book cannot fill ``units`` at all -- that is a
    fact about whether the trade can happen, not a price opinion. A partial fill
    is reported as ``filled`` / ``short`` with the VWAP of what did fill, so the
    caller can say which of the two it is instead of hiding the row.
    """
    want = first_decimal(units)
    out = {"ok": False, "filled": "0", "short": "0", "vwap": None, "best": None,
           "levels_used": 0, "slip_bps": None, "notional_usd": None,
           "top_notional_usd": None, "side": side}
    if want is None or want <= 0:
        return out
    parsed: list[tuple[Decimal, Decimal]] = []
    for row in levels or []:
        try:
            price = Decimal(str(row[0]))
            qty = Decimal(str(row[1]))
        except (IndexError, TypeError, ValueError):
            continue
        if price > 0 and qty > 0:
            parsed.append((price, qty))
    if not parsed:
        return out
    parsed.sort(key=lambda item: item[0], reverse=(side == "sell"))
    best, best_qty = parsed[0]
    remaining, cash, filled, used = want, Decimal(0), Decimal(0), 0
    for price, qty in parsed:
        if remaining <= 0:
            break
        take = min(remaining, qty)
        filled += take
        cash += take * price
        remaining -= take
        used += 1
    if filled <= 0:
        return out
    vwap = cash / filled
    slip = (((best - vwap) if side == "sell" else (vwap - best)) / best
            * Decimal(10000))
    out.update({
        "ok": remaining <= 0,
        "filled": format(filled, "f"),
        "short": format(max(remaining, Decimal(0)), "f"),
        "vwap": vwap,
        "best": best,
        "levels_used": used,
        "slip_bps": slip,
        "notional_usd": filled * vwap,
        "top_notional_usd": best * best_qty,
    })
    return out
