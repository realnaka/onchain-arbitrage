"""Measured CCIP bridge fees, per edge, with the evidence attached.

A bridge fee is **per message**, paid in the *source chain's gas token*, and it
does not scale with the amount. Guessing it at zero is how a "+1200 bps" path
becomes a loss, so the scan refuses every bridge edge whose fee has not been
measured (``cost_unknown``).

Two files turn a measurement into a USD cost, and **both** are needed:

* ``xstocks_v2_bridge_fees.json`` -- what we measured, in the fee's own asset,
  with the transaction hashes that prove it. Written from a receipt, never
  invented.
* ``xstocks_v2_gas_prices.json`` -- that gas token's USD price, with its source
  and timestamp. Refreshed by ``scripts/xstocks_v2_bridge_fees.py refresh``.

A fee with no price, or a price older than the allowance, is **unknown**, and
unknown stays a refusal. Deriving the price from the fee's own "≈ $0.10" line
would make the check circular, so it is not done here.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

FEES_NAME = "xstocks_v2_bridge_fees.json"
PRICES_NAME = "xstocks_v2_gas_prices.json"

#: A gas price older than this is not a price. The fee itself is per message and
#: barely moves, but its USD value is only as good as the conversion.
MAX_PRICE_AGE_MS = 3 * 60 * 60 * 1000

OK = "ok"
FEE_UNMEASURED = "fee_unmeasured"
GAS_PRICE_UNKNOWN = "gas_price_unknown"
GAS_PRICE_STALE = "gas_price_stale"


def norm(chain) -> str:
    """Chain keys are compared in lower case, the way ``routes`` does it."""
    return str(chain or "").strip().lower()


def edge_key(source, dest) -> str:
    return f"{norm(source)}->{norm(dest)}"


def _dec(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _load_json(path: Path | str) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def load_fees(path: Path | str) -> dict[tuple[str, str], dict]:
    """``(source, dest) -> {fee_asset, fee_amount, samples, evidence, measured_ms}``.

    A row without an amount is dropped: an edge whose fee we cannot name is the
    same as an edge we never measured.
    """
    out: dict[tuple[str, str], dict] = {}
    for key, row in (_load_json(path).get("edges") or {}).items():
        if not isinstance(row, dict):
            continue
        source, _, dest = str(key).partition("->")
        amount = _dec(row.get("fee_amount"))
        asset = str(row.get("fee_asset") or "").strip().upper()
        if not source or not dest or amount is None or not asset:
            continue
        out[(norm(source), norm(dest))] = {
            "fee_asset": asset,
            "fee_amount": amount,
            "samples": int(row.get("samples") or 0),
            "evidence": [str(item) for item in (row.get("evidence") or [])],
            "measured_ms": int(row.get("measured_ms") or 0),
        }
    return out


def load_prices(path: Path | str) -> dict[str, dict]:
    """``ASSET -> {usd, source, as_of_ms}``. A price with no timestamp is dropped."""
    out: dict[str, dict] = {}
    for asset, row in (_load_json(path).get("prices") or {}).items():
        if not isinstance(row, dict):
            continue
        usd = _dec(row.get("usd"))
        as_of = int(row.get("as_of_ms") or 0)
        if usd is None or usd <= 0 or as_of <= 0:
            continue
        out[str(asset).strip().upper()] = {
            "usd": usd,
            "source": str(row.get("source") or ""),
            "as_of_ms": as_of,
        }
    return out


def fee_usd(edge, *, fees: dict, prices: dict, now_ms: int,
            max_price_age_ms: int = MAX_PRICE_AGE_MS) -> tuple[Decimal | None, str]:
    """The USD cost of one bridge message, or ``(None, why it is unknown)``."""
    row = fees.get((norm(edge[0]), norm(edge[1])))
    if row is None:
        return None, FEE_UNMEASURED
    price = prices.get(row["fee_asset"])
    if price is None:
        return None, GAS_PRICE_UNKNOWN
    if now_ms - int(price["as_of_ms"]) > max_price_age_ms:
        return None, GAS_PRICE_STALE
    return row["fee_amount"] * price["usd"], OK


def fee_table(*, fees: dict, prices: dict, now_ms: int,
              max_price_age_ms: int = MAX_PRICE_AGE_MS) -> dict[tuple[str, str], Decimal]:
    """Every edge whose fee we can price in USD right now."""
    out: dict[tuple[str, str], Decimal] = {}
    for edge in fees:
        amount, reason = fee_usd(edge, fees=fees, prices=prices, now_ms=now_ms,
                                 max_price_age_ms=max_price_age_ms)
        if amount is not None:
            out[edge] = amount
    return out


def unknown_reasons(*, fees: dict, prices: dict, now_ms: int,
                    max_price_age_ms: int = MAX_PRICE_AGE_MS) -> dict[str, str]:
    """``edge -> reason`` for every measured edge that still cannot be priced."""
    out: dict[str, str] = {}
    for edge in fees:
        amount, reason = fee_usd(edge, fees=fees, prices=prices, now_ms=now_ms,
                                 max_price_age_ms=max_price_age_ms)
        if amount is None:
            out[edge_key(*edge)] = reason
    return out


def load_table(*, data_dir: Path | str, now_ms: int,
               max_price_age_ms: int = MAX_PRICE_AGE_MS,
               live_fees: dict | None = None) -> tuple[dict, dict]:
    """``(fee_table, unknown_reasons)`` straight from a data directory.

    ``live_fees`` are this round's own readings of the same edges (A3). They win
    over the file: the file is a receipt, the chain is the price. An edge the
    chain could not answer keeps using the file, and an edge in neither stays
    unknown.
    """
    directory = Path(data_dir)
    fees = load_fees(directory / FEES_NAME)
    if live_fees:
        fees = {**fees, **live_fees}
    prices = load_prices(directory / PRICES_NAME)
    return (fee_table(fees=fees, prices=prices, now_ms=now_ms,
                      max_price_age_ms=max_price_age_ms),
            unknown_reasons(fees=fees, prices=prices, now_ms=now_ms,
                            max_price_age_ms=max_price_age_ms))


def live_row(*, fee_amount, fee_asset: str, source: str,
             now_ms: int | None = None) -> dict:
    """One row in the shape ``load_fees`` returns, for a fee read off a chain.

    ``samples`` is 0 on purpose: this is a quote, not an arrival. ``evidence``
    carries the tag that says where the number came from, so a board reader can
    tell a dry run from a receipt.
    """
    return {"fee_asset": str(fee_asset).strip().upper(),
            "fee_amount": _dec(fee_amount),
            "samples": 0,
            "evidence": [str(source)],
            "measured_ms": int(now_ms or 0)}


def price_report(*, data_dir: Path | str, now_ms: int,
                 max_price_age_ms: int = MAX_PRICE_AGE_MS) -> dict:
    """What each gas price is, where it came from, and whether it is still usable.

    "The cost is unknown" must be readable as *which* number is missing: a stale
    price silently turned every x-layer bridge into ``cost_unknown`` on
    2026-10-07, and nothing on the board said so.
    """
    directory = Path(data_dir)
    prices = load_prices(directory / PRICES_NAME)
    out: dict[str, dict] = {}
    stale: list[str] = []
    for asset, row in sorted(prices.items()):
        age_ms = max(0, now_ms - int(row["as_of_ms"]))
        is_stale = age_ms > max_price_age_ms
        if is_stale:
            stale.append(asset)
        out[asset] = {
            "usd": format(row["usd"], "f"),
            "source": row.get("source") or "",
            "as_of_ms": row["as_of_ms"],
            "age_s": round(age_ms / 1000, 1),
            "stale": is_stale,
        }
    return {"prices": out, "stale": stale,
            "max_age_s": round(max_price_age_ms / 1000, 1),
            "missing": sorted({str(a).upper() for a in (feeds_needed() or [])}
                              - set(prices))}


def feeds_needed() -> tuple[str, ...]:
    """The gas tokens the measured fee table actually spends."""
    return ("OKB", "SOL")
