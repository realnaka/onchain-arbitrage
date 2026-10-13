#!/usr/bin/env python3
"""Break a venue's above-threshold rows down by asset, with the two hard gates.

`arb-monitor venue-scoreboard` answers "which venue sees the most above-threshold
basis". It deliberately does *not* answer "which of those could this wallet
actually open", and on 2026-09-20 that gap changed the answer: binance led on
candidate rate (9.2%) but 573 of its 623 candidates were TENCENT, one share of
which costs 53.57 USDC against a 10.91 USDC wallet. `below_min_notional` —
recorded by the strategy on the row itself — is the other hard gate: bitget
KUAISHOU's 611 candidates are all 1-share rows whose perp notional is under
Bitget's minimum.

So this script reports, per (venue, asset): above-threshold count, how many of
those rows *lack* `below_min_notional`, and whether one share of the asset fits
inside `min(per_trade_quote_usdt, wallet x 0.999)`.

Read-only: opens the monitor DB with `mode=ro` through the same `load_rows` the
scoreboard uses, so the two reports can never disagree about what a row means.

    PYTHONPATH=src .venv/bin/python scripts/venue-candidate-by-asset.py \
        --hours 54 --wallet-quote-usdt 10.92387
"""
from __future__ import annotations

import argparse
import collections
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arb_monitor.ops.venue_scoreboard import (  # noqa: E402
    DATA_QUALITY_REJECTIONS,
    DEFAULT_MIN_GROSS_BPS,
    DEFAULT_MIN_NET_BPS,
    DEFAULT_MONITOR_DB,
    DEFAULT_TRADE_QUOTE_USDT,
    WALLET_SAFETY_FACTOR,
    limits_from_policy,
    load_rows,
)

BPS = Decimal(10000)
# Fallback the scoreboard uses when a row never reached the cost model.
FALLBACK_COST_BPS = Decimal("15")


def row_cost_bps(row: dict) -> Decimal:
    """The cost this row was actually charged, in bps of its own notional."""
    cost_quote, spot_open = row.get("cost_quote"), row.get("spot_open_quote")
    if cost_quote is not None and spot_open is not None and spot_open > 0:
        return cost_quote / spot_open * BPS
    return FALLBACK_COST_BPS


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=float, default=54.0)
    ap.add_argument("--db", default=DEFAULT_MONITOR_DB)
    ap.add_argument("--wallet-quote-usdt", type=Decimal, default=None)
    ap.add_argument("--trade-quote-usdt", type=Decimal, default=None,
                    help="per-trade ceiling; default: [limits] per_trade_quote_usdt "
                         "from --automation-policy (same source as venue-scoreboard)")
    ap.add_argument("--automation-policy", default="config/automation_policy.toml")
    ap.add_argument("--min-gross-bps", type=Decimal, default=DEFAULT_MIN_GROSS_BPS)
    ap.add_argument("--min-net-bps", type=Decimal, default=DEFAULT_MIN_NET_BPS)
    args = ap.parse_args()

    # Read the ceiling from the same place `venue-scoreboard` does, otherwise the
    # two reports can disagree about what the wallet can deploy. The module
    # default is 10, the configured ceiling is 100, and the wallet (10.91) binds
    # first either way -- but only by accident, and that accident should not be
    # what keeps the reports consistent.
    ceiling = args.trade_quote_usdt
    if ceiling is None:
        ceiling = limits_from_policy(args.automation_policy).get(
            "per_trade_quote_usdt", DEFAULT_TRADE_QUOTE_USDT)
    cap = Decimal(str(ceiling))
    if args.wallet_quote_usdt is not None:
        cap = min(cap, args.wallet_quote_usdt * WALLET_SAFETY_FACTOR)

    now_ms = int(time.time() * 1000)
    rows = load_rows(args.db, since_ms=now_ms - int(args.hours * 3600 * 1000))
    # Same regime filter as the scoreboard: only rows the streaming evaluator
    # stamped. Without `evaluated_at_ms` the row was judged after the whole
    # cycle's probes returned, which is the "grouped with stale prices" regime
    # the operator rejected on 2026-09-17.
    rows = [r for r in rows if r["evaluated_at_ms"] is not None]

    counts: collections.Counter = collections.Counter()
    totals: collections.Counter = collections.Counter()
    share_price: dict[str, Decimal] = {}

    for row in rows:
        gross = row["gross_bps"]
        if gross is None:
            continue
        rejections = set(row["rejections"])
        if rejections & DATA_QUALITY_REJECTIONS:
            continue
        if gross - row_cost_bps(row) < args.min_net_bps:
            continue
        venue, asset = row["venue"] or "?", row["asset"] or "?"
        totals[(venue, asset)] += 1
        counts[(venue, asset, "min_notional_blocked" if "below_min_notional" in rejections
                else "notional_ok")] += 1
        if "outside_strategy_window" in rejections:
            counts[(venue, asset, "legacy_phase_tag")] += 1
        spot_open, qty = row["spot_open_quote"], row["spot_quantity"]
        if spot_open is not None and spot_open > 0:
            share_price[asset] = spot_open / (qty if qty and qty > 0 else Decimal(1))

    print(f"窗口 : 最近 {args.hours:g} 小时；带 evaluated_at_ms 的行 {len(rows)}")
    print(f"门槛 : 净基差 ≥ {args.min_net_bps} bps（成本读该行 roundtrip_cost_usdt）；"
          f"可部署上限 {cap} USDC = min(单笔 {ceiling}, 钱包×0.999)")
    print()
    header = (f"{'场所':12}{'标的':10}{'过门槛':>7}{'名义OK':>7}"
              f"{'低于最小名义':>12}{'旧口径标签':>10}{'1股(USDC)':>11}  钱包能买1股")
    print(header)
    print("-" * len(header))
    for (venue, asset), total in sorted(totals.items(), key=lambda kv: -kv[1]):
        ok = counts[(venue, asset, "notional_ok")]
        blocked = counts[(venue, asset, "min_notional_blocked")]
        legacy = counts[(venue, asset, "legacy_phase_tag")]
        price = share_price.get(asset)
        verdict = ("价格未知" if price is None
                   else ("✅" if price <= cap else "❌ 买不起"))
        price_s = "-" if price is None else f"{price:.2f}"
        print(f"{venue:12}{asset:10}{total:>7}{ok:>7}{blocked:>12}{legacy:>10}"
              f"{price_s:>11}  {verdict}")

    print()
    print(f"{'场所':12}{'可行动合计':>10}   构成")
    print("-" * 60)
    actionable: collections.Counter = collections.Counter()
    parts: dict[str, list[str]] = collections.defaultdict(list)
    for (venue, asset), total in totals.items():
        ok = counts[(venue, asset, "notional_ok")]
        price = share_price.get(asset)
        if ok and price is not None and price <= cap:
            actionable[venue] += ok
            parts[venue].append(f"{asset} {ok}")
    for venue, n in actionable.most_common():
        print(f"{venue:12}{n:>10}   " + " + ".join(parts[venue]))
    if not actionable:
        print("（无：窗口内没有一条同时满足「买得起 1 股」与「≥ 该所最小名义」的行）")
    print()
    print("说明：`低于最小名义` 来自该行自己记录的 below_min_notional —— 它是 1 股探针下的")
    print("      永续名义与该所最小名义的比较，不是数据质量标记，所以两列要分开看。")
    print("      `旧口径标签` 计数带 outside_strategy_window 的历史行（2026-09-20 14:30 前的进程写的）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
