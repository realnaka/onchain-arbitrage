#!/usr/bin/env python3
"""Show, and refresh, the measured CCIP bridge fee table.

    scripts/xstocks_v2_bridge_fees.py show      # read-only: fee table in USD
    scripts/xstocks_v2_bridge_fees.py refresh   # re-quote OKB and SOL, rewrite prices

``show`` is what a human reads before trusting a bridge row: which edges have a
measured fee, which gas price it uses, and which edges are still unknown (and
therefore still refused by the scan as ``cost_unknown``).

``refresh`` only touches ``data-v2/xstocks_v2_gas_prices.json``. The measured
fee amounts live in ``data-v2/xstocks_v2_bridge_fees.json`` and are written from
receipts by hand -- a script must never invent a fee it has not seen.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.xstocks_v2 import bridge_fees as BF          # noqa: E402
from arb_monitor.xstocks_v2 import gas_prices                  # noqa: E402
from arb_monitor.xstocks_v2 import live_fees                   # noqa: E402
from arb_monitor.xstocks_v2.feeds.http import Http             # noqa: E402

DATA = ROOT / "data-v2"
PROXY = "http://127.0.0.1:4780"

# X Layer: the bridge fee is native OKB, and WOKB is 1:1 with it.
WOKB_XLAYER = gas_prices.WOKB_XLAYER
USDG_XLAYER = gas_prices.USDG_XLAYER
# Solana: the CCIP program charges in native SOL.
WSOL_SOLANA = gas_prices.WSOL_SOLANA
USDC_SOLANA = gas_prices.USDC_SOLANA
#: Kept as module attributes so a reader of this file still sees the shape; the
#: one implementation is ``gas_prices`` (the scan calls it directly).
QUOTER_V2_XLAYER = gas_prices.QUOTER_V2_XLAYER
USDG_WOKB_FEES = gas_prices.USDG_WOKB_FEES
JUPITER_PRICE = gas_prices.JUPITER_PRICE


def http_client() -> Http:
    return Http(proxy=PROXY)


def price_okb_from_pool() -> tuple[Decimal | None, str]:
    """The wallet-independent OKB price. One implementation: ``gas_prices``.

    It used to live here, which is why the price only moved when a human ran
    ``refresh``. The scan now calls ``gas_prices.ensure_fresh`` every round.
    """
    return gas_prices.price_okb_from_pool(http_client())


def price_sol_from_jupiter() -> tuple[Decimal | None, str]:
    """The public SOL price. One implementation: ``gas_prices``."""
    return gas_prices.price_sol_from_jupiter(http_client())


def onchainos(args: list[str], timeout: int = 120) -> dict:
    env = dict(os.environ, HTTPS_PROXY=PROXY, HTTP_PROXY=PROXY)
    done = subprocess.run(["onchainos"] + args, capture_output=True, text=True,
                          env=env, timeout=timeout)
    try:
        return json.loads(done.stdout)
    except ValueError:
        return {"ok": False, "error": (done.stdout or done.stderr)[:400]}


def quote(*, chain: str, src: str, dst: str, amount: int) -> dict:
    out = onchainos(["swap", "quote", "--chain", chain, "--from", src, "--to", dst,
                     "--amount", str(amount), "--swap-mode", "exactIn"])
    data = out.get("data") or []
    row = data[0] if isinstance(data, list) and data else {}
    return row if out.get("ok") and isinstance(row, dict) else {}


def unit_price(row: dict, *, out_symbol: str) -> Decimal | None:
    """The venue's own unit price for the token we are pricing."""
    token = row.get("toToken") or {}
    if str(token.get("tokenSymbol") or "").upper() == out_symbol:
        value = token.get("tokenUnitPrice")
        if value:
            return Decimal(str(value))
    return None


def price_okb_direct() -> tuple[Decimal | None, str]:
    """OKB in USD from WOKB -> USDG. Two sizes must agree, or it is not a price."""
    row = quote(chain="xlayer", src=WOKB_XLAYER, dst=USDG_XLAYER, amount=10 ** 16)
    if not row:
        return None, "no_route"
    out_raw, in_raw = row.get("toTokenAmount"), row.get("fromTokenAmount")
    if not out_raw or not in_raw:
        return None, "no_amounts"
    from_unit = Decimal(str(out_raw)) / Decimal(10 ** 6) / (
        Decimal(str(in_raw)) / Decimal(10 ** 18))
    big = quote(chain="xlayer", src=WOKB_XLAYER, dst=USDG_XLAYER, amount=10 ** 18)
    if big.get("toTokenAmount"):
        big_unit = Decimal(str(big["toTokenAmount"])) / Decimal(10 ** 6)
    else:
        big_unit = from_unit
    # A thin pool prices small and large sizes differently; anything wider than
    # 1% is a thin market, not a price we want to convert fees with.
    if abs(big_unit - from_unit) / from_unit > Decimal("0.01"):
        return None, f"size_disagreement:{from_unit}vs{big_unit}"
    labelled = unit_price(row, out_symbol="WOKB") \
        or unit_price(big, out_symbol="WOKB")
    if labelled is not None and abs(labelled - from_unit) / from_unit > Decimal("0.01"):
        return None, f"label_disagreement:{labelled}vs{from_unit}"
    return from_unit, "ok"


def price_okb_reverse() -> tuple[Decimal | None, str]:
    """OKB in USD from USDG -> WOKB: the same pool, read from the side we can pay.

    The quote API is **balance-gated**. It refuses to quote an input the wallet
    cannot cover, and ``WOKB -> USDG`` therefore stops working the moment we
    spend the WOKB. Measured 2026-10-07: after a swap left 0.0003 WOKB, every
    refresh returned ``no_route``; the OKB price went past its 3 hour limit and
    both x-layer bridge edges fell to ``gas_price_stale``, which silently blocks
    the whole chain -> hub -> Kraken family. The reverse quote needs USDG, which
    we hold. Same two-size and label checks, so the rigour does not change with
    the direction.
    """
    small = quote(chain="xlayer", src=USDG_XLAYER, dst=WOKB_XLAYER, amount=10 ** 6)
    if not small:
        return None, "no_route"
    big = quote(chain="xlayer", src=USDG_XLAYER, dst=WOKB_XLAYER, amount=10 ** 7)
    units: list[Decimal] = []
    for row in (small, big):
        out_raw, in_raw = row.get("toTokenAmount"), row.get("fromTokenAmount")
        if not out_raw or not in_raw:
            continue
        units.append((Decimal(str(in_raw)) / Decimal(10 ** 6))
                     / (Decimal(str(out_raw)) / Decimal(10 ** 18)))
    if not units:
        return None, "no_amounts"
    value = units[0]
    if len(units) == 2 and abs(units[1] - value) / value > Decimal("0.01"):
        return None, f"size_disagreement:{value}vs{units[1]}"
    labelled = unit_price(big, out_symbol="WOKB") or unit_price(small, out_symbol="WOKB")
    if labelled is not None and abs(labelled - value) / value > Decimal("0.01"):
        return None, f"label_disagreement:{labelled}vs{value}"
    return value, "ok"


def price_okb() -> tuple[Decimal | None, str]:
    """OKB in USD, direct first and reverse second. An unknown stays unknown."""
    value, reason = price_okb_direct()
    if value is not None:
        return value, reason
    reverse_value, reverse_reason = price_okb_reverse()
    if reverse_value is not None:
        return reverse_value, f"ok(reverse:{reason})"
    return None, f"{reason}|{reverse_reason}"


def price_sol() -> tuple[Decimal | None, str]:
    row = quote(chain="solana", src=WSOL_SOLANA, dst=USDC_SOLANA, amount=10 ** 7)
    out_raw, in_raw = row.get("toTokenAmount"), row.get("fromTokenAmount")
    if not out_raw or not in_raw:
        return None, "no_amounts"
    return (Decimal(str(out_raw)) / Decimal(10 ** 6)
            / (Decimal(str(in_raw)) / Decimal(10 ** 9))), "ok"


def refresh() -> int:
    now = int(time.time() * 1000)
    # The wallet-independent sources are the ones the scan uses every round
    # (``gas_prices``). The aggregator below is the one source it cannot use:
    # it is a CLI that needs the local proxy, and it is balance-gated -- it
    # fails exactly when we need a price the most (2026-10-07: every x-layer
    # bridge went cost_unknown because a quote for its fee token answered
    # no_route).
    result = gas_prices.refresh(DATA, now_ms=now, http=http_client())
    prices: dict[str, dict] = dict(result.get("refreshed") or {})
    for asset, fallback, note in (
            ("OKB", price_okb,
             "WOKB->USDG on X Layer, or USDG->WOKB when the wallet cannot cover "
             "the direct side"),
            ("SOL", price_sol, "WSOL->USDC on Solana")):
        if asset in prices:
            continue
        reason = (result.get("failed") or {}).get(asset, "not_attempted")
        print(f"{asset}: {reason}; falling back to the aggregator")
        value, fallback_reason = fallback()
        if value is None:
            print(f"{asset}: refresh failed ({reason}; {fallback_reason}); "
                  "leaving the old price alone")
            continue
        prices[asset] = {
            "usd": format(value.quantize(Decimal("0.0001")), "f"),
            "source": f"onchainos swap quote {note}",
            "as_of_ms": now,
        }
    if not prices:
        print("nothing refreshed; the fee table keeps refusing those edges")
        return 1
    path = gas_prices.write_prices(DATA, prices, now_ms=now)
    print(f"wrote {path} with {sorted(prices)}")
    return 0


def show() -> int:
    now = int(time.time() * 1000)
    fees = BF.load_fees(DATA / BF.FEES_NAME)
    prices = BF.load_prices(DATA / BF.PRICES_NAME)
    print(f"fees  : {len(fees)} measured edge(s)")
    for edge, row in sorted(fees.items()):
        amount, reason = BF.fee_usd(edge, fees=fees, prices=prices, now_ms=now)
        age_h = (now - int(row["measured_ms"])) / 3_600_000
        where = "OK" if amount is not None else reason
        printed = f"{amount:.6f} USD" if amount is not None else "-"
        print(f"  {BF.edge_key(*edge):20s} {row['fee_amount']} {row['fee_asset']}"
              f" -> {printed:>14s}  [{where}]  {row['samples']} sample(s), "
              f"{age_h:.1f} h old")
    for asset, row in sorted(prices.items()):
        age_m = (now - int(row["as_of_ms"])) / 60_000
        print(f"  price {asset:4s} {row['usd']} ({row['source'][:60]}, {age_m:.0f} min old)")
    unknown = BF.unknown_reasons(fees=fees, prices=prices, now_ms=now)
    if unknown:
        print(f"still refused (cost_unknown): {unknown}")
    # Since A3 the chain is asked every round, and its answer is what the scan
    # uses. Print it next to the receipts so the two can be compared by eye.
    try:
        rows, notes = live_fees.quote_edges(http=http_client(), now_ms=now)
    except Exception as exc:  # noqa: BLE001 - a read that does not answer
        print(f"live quote: unavailable ({type(exc).__name__})")
        return 0
    for key, note in sorted(notes.items()):
        if note.get("ok"):
            print(f"  live  {key:20s} {note['fee_amount']} {note['fee_asset']}"
                  f"  [{note['source']}]")
        else:
            print(f"  live  {key:20s} -  [{note.get('reason')}]")
    cross = live_fees.compare_with_evidence(rows, fees)
    for key, item in sorted(cross.items()):
        print(f"  check {key:20s} live {item['live']} vs receipt "
              f"{item['measured']} = {item['mismatch_bps']} bps")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("show", "refresh"), nargs="?", default="show")
    args = parser.parse_args()
    return refresh() if args.mode == "refresh" else show()


if __name__ == "__main__":
    raise SystemExit(main())
