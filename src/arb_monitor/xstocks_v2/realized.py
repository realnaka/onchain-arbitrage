"""Realized net for a finished cycle, from what the venue actually did.

Measured 2026-10-07: of the day's four ``sold`` rows, three carried
``net_usdt = null``. The ledger could say "we bought and sold" and nothing about
what it cost -- while the three wallets had moved by ≈ -5.40 USDT. A ledger that
cannot be reconciled against balances is a diary, not an account.

The rule here matches the rest of the repo: a **missing part is unknown, never
zero**. One cash leg that could not be read makes the net ``None`` -- but the
parts that *were* read are still written, so the next reader can see exactly
which number is missing instead of guessing.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

#: Parts that must be present before a net is called measured.
REQUIRED = ("cash_in_usd", "cash_out_usd")


def _dec(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def cycle_realized(*, cash_in_usd, cash_out_usd, fees_usd: dict | None = None,
                   gas_usd=None, note: str = "") -> dict:
    """``{net_usdt, parts, missing, measured}`` for one finished cycle.

    ``cash_in_usd`` is what we got (the sell proceeds), ``cash_out_usd`` what we
    paid (the buy). ``fees_usd`` is the named cost list -- the Kraken taker fee
    is inside the order's cost, but the **withdrawal fee in units**, the bridge
    message fee and the on-chain gas are not. ``gas_usd`` may be an **estimate**
    (the scan's gas line); it is labelled, never silently mixed in with a
    measurement.
    """
    parts: dict[str, str | None] = {
        "cash_in_usd": _str(cash_in_usd),
        "cash_out_usd": _str(cash_out_usd),
    }
    missing: list[str] = []
    total_fees = Decimal(0)
    for name, value in (fees_usd or {}).items():
        parts[name] = _str(value)
        amount = _dec(value)
        if amount is None:
            missing.append(name)
        else:
            total_fees += amount
    parts["gas_usd"] = _str(gas_usd)
    gas = _dec(gas_usd)
    if gas is None:
        missing.append("gas_usd")
    else:
        total_fees += gas
    cash_in, cash_out = _dec(cash_in_usd), _dec(cash_out_usd)
    if cash_in is None:
        missing.append("cash_in_usd")
    if cash_out is None:
        missing.append("cash_out_usd")
    if missing:
        return {"net_usdt": None, "parts": parts, "missing": sorted(set(missing)),
                "measured": False, "note": note}
    net = cash_in - cash_out - total_fees
    return {"net_usdt": format(net, "f"), "parts": parts, "missing": [],
            "measured": True, "note": note,
            "fees_total_usd": format(total_fees, "f")}


def _str(value) -> str | None:
    amount = _dec(value)
    return None if amount is None else format(amount, "f")


def realized_of(entry: dict) -> Decimal | None:
    """The realized net of one ledger row, or ``None`` when it is not measured."""
    realized = ((entry or {}).get("costs") or {}).get("realized") or {}
    if not realized.get("measured"):
        return None
    return _dec(realized.get("net_usdt"))


def totals(entries, *, modes: tuple[str, ...] = ("live",)) -> dict:
    """Sum what the ledger can prove, and count what it cannot.

    Kept in one place so the board, the CLI and the reconciliation test all read
    the same definition of "realized".
    """
    net = Decimal(0)
    measured = unmeasured = 0
    by_symbol: dict[str, Decimal] = {}
    unmeasured_rows: list[dict] = []
    for entry in entries or []:
        if str((entry or {}).get("mode") or "live") not in modes:
            continue
        if str((entry or {}).get("state")) != "sold":
            continue
        value = realized_of(entry)
        if value is None:
            unmeasured += 1
            recorded = bool(((entry.get("costs") or {}).get("realized")))
            unmeasured_rows.append({
                "symbol": entry.get("symbol"), "family": entry.get("family"),
                "ts_ms": entry.get("ts_ms"),
                # A row written before this existed is not "missing a part": it
                # never recorded any. Saying which is which is the difference
                # between a bug and history.
                "missing": ((((entry.get("costs") or {}).get("realized") or {})
                             .get("missing")) or ([] if recorded
                                                  else ["not_recorded"]))})
            continue
        measured += 1
        net += value
        symbol = str(entry.get("symbol") or "?")
        by_symbol[symbol] = by_symbol.get(symbol, Decimal(0)) + value
    return {"measured_rows": measured, "unmeasured_rows": unmeasured,
            "net_usdt": format(net, "f"),
            "by_symbol": {k: format(v, "f") for k, v in sorted(by_symbol.items())},
            "unmeasured": unmeasured_rows}
