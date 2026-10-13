"""Pre-trade gate: can this token actually be sold on Kraken?

Three checks, all fail-closed. A token that fails any of them must not be
shown as an opportunity, because the buy side is worthless without an exit.

1. **pair**     Kraken lists it and the market is tradeable.
2. **deposit**  Kraken can receive it on a chain we can reach from where we buy.
                (MRNAx is Solana-only. X Layer buys must redeem, then bridge.)
3. **exit book**our own size does not walk the Kraken book past a slippage cap.

Everything here is pure parsing/arithmetic on payloads the caller fetched, so
it is hermetic and testable.
"""
from __future__ import annotations

from decimal import Decimal

TOKENIZED = "tokenized_asset"
DEFAULT_MAX_SLIP_BPS = Decimal("50")
DEFAULT_MAX_BOOK_SHARE = Decimal("0.2")   # take at most 20% of the in-range book

# Chains we have actually moved value on for this strategy. Anything else is
# "not verified", not "probably fine".
VERIFIED_SOURCE_CHAINS = {"Solana", "XLayer"}


def parse_asset_pairs(payload: object) -> dict[str, dict]:
    """`AssetPairs` payload -> {symbol: {wsname, status, ordermin, costmin}}.

    Keys look like ``MRNAxUSD``; the symbol is the ``wsname`` first half.
    """
    out: dict[str, dict] = {}
    for row in (payload or {}).values():
        wsname = str(row.get("wsname") or "")
        if "/" not in wsname:
            continue
        symbol = wsname.split("/", 1)[0]
        current = out.get(symbol)
        candidate = {
            "symbol": symbol,
            "wsname": wsname,
            "status": row.get("status") or "",
            "ordermin": str(row.get("ordermin") or ""),
            "costmin": str(row.get("costmin") or ""),
        }
        if current is None or candidate["status"] == "online":
            out[symbol] = candidate
    return out


def parse_deposit_methods(rows: object) -> list[dict]:
    """Normalise `DepositMethods` rows."""
    out = []
    for row in rows or []:
        out.append({
            "method": str(row.get("method") or ""),
            "minimum": str(row.get("minimum") or ""),
            "fee": str(row.get("fee") or ""),
        })
    return out


def parse_bridge_symbols(products_json_values) -> set[str]:
    """Symbols that appear in any bridge's product list (they can be bridged)."""
    import json

    found: set[str] = set()
    for value in products_json_values or []:
        try:
            products = json.loads(value) if isinstance(value, str) else value
        except (TypeError, ValueError):
            continue
        for product in products or []:
            symbol = str((product or {}).get("symbol") or "")
            nets = {str(d.get("network") or "") for d in (product or {}).get("deployments") or []}
            if symbol and "Solana" in nets:
                found.add(symbol)
    return found


def solana_deposit(methods: list[dict], symbol: str) -> dict | None:
    """The Solana method for `symbol`, if any.

    Kraken lists a chain-named method (``X - Solana``) for some assets and an
    unnamed one for others. The unnamed one resolves to the same deposit
    address as the Solana method (verified 2026-10-05 against MRNAx), so both
    count as Solana.
    """
    for method in methods:
        name = method["method"]
        if "Solana" in name or name.strip() == symbol:
            return method
    return None


def reachable(symbol: str, *, source_network: str,
              deposits: dict[str, list[dict]],
              bridge_symbols: set[str] | None = None) -> tuple[bool, str]:
    """(ok, reason). Reason is shown on the board next to a rejected row."""
    methods = deposits.get(symbol)
    if methods is None:
        return False, "充值方式未核对"
    sol = solana_deposit(methods, symbol)
    if sol is None:
        named = "、".join(m["method"] for m in methods) or "无"
        return False, f"Kraken 不收 Solana 以外的链（{named}）"
    if source_network == "Solana":
        return True, f"Solana 直充（最小 {sol['minimum']}）"
    if source_network == "XLayer":
        if bridge_symbols is not None and symbol not in bridge_symbols:
            return False, "X Layer 没有到 Solana 的桥"
        return True, f"X Layer 需 redeem→桥→充（最小 {sol['minimum']}）"
    return False, f"未验证 {source_network} 出发"


def book_exit(bids, tokens, *, max_slip_bps=DEFAULT_MAX_SLIP_BPS,
              max_book_share=DEFAULT_MAX_BOOK_SHARE) -> dict:
    """Walk the bid side for `tokens`. Returns the plan and whether it passes.

    ``bids`` is Kraken's depth list: ``[[price, volume, timestamp], ...]``.
    """
    tokens = Decimal(str(tokens))
    levels = []
    for row in bids or []:
        try:
            levels.append((Decimal(str(row[0])), Decimal(str(row[1]))))
        except (IndexError, TypeError, ValueError):
            continue
    if not levels or tokens <= 0:
        return {"ok": False, "reason": "盘口为空", "best_bid": None, "vwap": None,
                "filled": "0", "slippage_bps": None, "in_range_usd": "0",
                "book_share": None}
    best = levels[0][0]
    floor = best * (Decimal(1) - Decimal(max_slip_bps) / Decimal(10000))
    in_range = sum(p * q for p, q in levels if p >= floor)
    remaining, filled, cash = tokens, Decimal(0), Decimal(0)
    for price, qty in levels:
        if remaining <= 0:
            break
        take = min(remaining, qty)
        if take <= 0:
            continue
        filled += take
        cash += take * price
        remaining -= take
    vwap = cash / filled if filled > 0 else Decimal(0)
    slip = (best - vwap) / best * Decimal(10000) if filled > 0 else None
    notional = tokens * vwap
    share = (notional / in_range) if in_range > 0 else None
    ok = remaining <= 0 and vwap >= floor
    if fill_missing := (remaining > 0):
        ok = False
    reason = "通过"
    if fill_missing:
        reason = "盘口吃不下这个数量（跌穿 50 bps 仍不足）"
    elif share is not None and share > max_book_share:
        ok = False
        reason = f"占 50 bps 内盘口的 {share * 100:.0f}%"
    return {
        "ok": ok,
        "reason": reason,
        "best_bid": str(best),
        "vwap": str(vwap.quantize(Decimal("0.0001"))),
        "filled": str(filled),
        "slippage_bps": str(slip.quantize(Decimal("0.1"))) if slip is not None else None,
        "in_range_usd": str(in_range.quantize(Decimal("0.01"))),
        "book_share": str(share.quantize(Decimal("0.0001"))) if share is not None else None,
    }
