#!/usr/bin/env python
"""What each asset has to show before a trade is worth doing, and what it shows now.

The threshold is not a number someone picked: it is the measured round-trip cost
plus the wear buffer plus the profit the trade has to offer. This prints that
arithmetic per asset, next to the gross basis the market is actually showing, so
"when does the signal fire" has an answer with units.

    PYTHONPATH=src .venv/bin/python scripts/threshold_report.py
    PYTHONPATH=src .venv/bin/python scripts/threshold_report.py --refresh   # sample now
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from arb_monitor.apps.hk_monitor import (  # noqa: E402
    candidate_roundtrip, costs_from_config, load_config,
)
from arb_monitor.core.hk import decimal  # noqa: E402


def latest_summary(db: str) -> dict:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT payload_json FROM monitor_events WHERE kind='run_summary'"
            " ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    if not row:
        raise SystemExit("no run_summary in the monitor database yet")
    return json.loads(row[0])


def report(summary: dict, config: dict) -> list[dict]:
    costs = costs_from_config(config)
    policy = config.get("execution_policy") or {}
    style = ("maker" if str((policy.get("orders") or {}).get("style") or "")
             == "maker_first" else "taker")
    allow = policy.get("allowlist") or {}
    allow_venues = {str(x) for x in allow.get("venues") or []}
    allow_instruments = {str(x) for x in allow.get("instruments") or []}
    strategy = config.get("strategy") or {}
    buffer_bps = decimal(str(strategy.get("edge_buffer_bps", "0")))
    profit_bps = decimal(str(strategy.get("expected_profit_bps", "0")))
    rows = []
    for row in summary.get("token_references") or []:
        direction = (row.get("directions") or {}).get("long_spot_short_perp") or {}
        perp = direction.get("perp") or {}
        if not perp.get("instrument"):
            rows.append({"asset": row.get("asset"), "status": "no_perp"})
            continue
        roundtrip = candidate_roundtrip(
            row=row, direction=direction, costs=costs,
            target=config.get("scenario", {}).get("target_exit_basis_bps"),
            min_edge_bps=decimal(config["monitor"]["min_net_edge_bps"]),
            buffer_bps=buffer_bps, expected_profit_bps=profit_bps,
            perp_style=style)
        bar = roundtrip.get("breakeven_gross_bps")
        spot_ask = row.get("onchain_spot_ask_usd")

        def basis(quote: dict):
            """(perp bid − spot ask) / spot ask, the same definition the alert uses."""
            if not quote or spot_ask in (None, 0) or quote.get("bid") is None:
                return None
            ask = decimal(str(spot_ask))
            return (decimal(str(quote["bid"])) - ask) / ask * decimal(10000)

        # The venue that shows the best price is not necessarily one this
        # project can trade on (Gate and Binance are monitoring-only), so the
        # decision number is the *executable* venue's basis, with the best one
        # beside it as information.
        quotes = [q for q in (row.get("perp_quotes") or []) if q.get("bid") is not None]
        best = max(quotes, key=lambda q: decimal(str(q["bid"]))) if quotes else None
        executable = [q for q in quotes
                      if str(q.get("venue")) in allow_venues
                      and str(q.get("instrument")) in allow_instruments]
        tradeable = (max(executable, key=lambda q: decimal(str(q["bid"])))
                     if executable else None)

        def gap_for(quote: dict):
            value = basis(quote)
            if value is None or bar is None:
                return None, None
            return value, value - decimal(str(bar))

        exe_basis, exe_gap = gap_for(tradeable)
        best_basis, best_gap = gap_for(best)
        rows.append({
            "asset": row.get("asset"), "venue": perp.get("venue"),
            "instrument": perp.get("instrument"),
            "spot_spread_bps": roundtrip.get("spot_spread_bps"),
            "perp_spread_bps": roundtrip.get("perp_spread_bps"),
            "perp_fee_bps": roundtrip.get("perp_fee_bps"),
            "total_bps": roundtrip.get("total_bps"),
            "bar_bps": bar,
            "executable_instrument": (tradeable or {}).get("instrument"),
            "executable_basis_bps": exe_basis, "executable_gap_bps": exe_gap,
            "best_venue": (best or {}).get("venue"),
            "best_instrument": (best or {}).get("instrument"),
            "best_basis_bps": best_basis, "best_gap_bps": best_gap,
            "mapping_verified": bool((row.get("mapping") or {}).get("verified")),
            "has_executable_venue": tradeable is not None,
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/hk_monitor.toml")
    parser.add_argument("--db", default="data/hk_monitor.db")
    parser.add_argument("--refresh", action="store_true",
                        help="take a fresh sample instead of reading the last cycle")
    parser.add_argument("--proxy", default=os.environ.get("ARB_HTTP_PROXY")
                        or "http://127.0.0.1:4780")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.refresh:
        from arb_monitor.apps.hk_monitor import HKMonitor
        monitor = HKMonitor(config, proxy=args.proxy)
        try:
            summary = monitor.cycle(quote_enabled=True)
        finally:
            monitor.close()
    else:
        summary = latest_summary(args.db)
    style = ("maker" if str((config.get("execution_policy") or {})
                            .get("orders", {}).get("style") or "") == "maker_first"
             else "taker")
    costs = costs_from_config(config)
    print(f'采样 {summary.get("hk_time")} · 下单风格 {style} · '
          f'永续费率 taker {costs.perp_fee_bps} / maker {costs.perp_maker_fee_bps} bps · '
          f'gas {costs.roundtrip_gas_budget_usdt} · '
          f'磨损 {config.get("strategy", {}).get("edge_buffer_bps")} + '
          f'期望获利 {config.get("strategy", {}).get("expected_profit_bps")} bps')
    print()
    header = (f'{"标的":<9}{"现货价差":>8}{"永续价差":>8}{"费率":>6}'
              f'{"往返成本":>8}{"出手门槛":>8}   {"可执行场(OKX)":<16}{"毛基差":>8}'
              f'{"还差":>8}   {"最优场":<22}{"毛基差":>8}{"还差":>8}  条件')
    print(header)
    for row in report(summary, config):
        if row.get("status") == "no_perp":
            print(f'{row["asset"]:<9}— 无永续可对冲')
            continue
        def num(value, places=1):
            return "—" if value is None else f"{float(value):.{places}f}"
        exe_name = row.get("executable_instrument") or "（无）"
        best_name = (f'{row.get("best_venue")}:{row.get("best_instrument")}'
                     if row.get("best_instrument") else "—")
        gate = []
        if not row["mapping_verified"]:
            gate.append("映射未核验")
        if not row["has_executable_venue"]:
            gate.append("该场无执行适配器")
        exe_gap = row["executable_gap_bps"]
        if not gate and exe_gap is not None and exe_gap >= 0:
            gate.append("★ 达到出手条件")
        print(f'{row["asset"]:<9}{num(row["spot_spread_bps"]):>8}'
              f'{num(row["perp_spread_bps"]):>8}{num(row["perp_fee_bps"]):>6}'
              f'{num(row["total_bps"]):>8}{num(row["bar_bps"]):>8}   '
              f'{exe_name:<16}{num(row["executable_basis_bps"]):>8}'
              f'{num(exe_gap):>8}   {best_name:<22}'
              f'{num(row["best_basis_bps"]):>8}{num(row["best_gap_bps"]):>8}  '
              f'{"、".join(gate) or "—"}')
    print()
    print("出手门槛 = 往返成本 + 磨损余量 + 期望获利；还差 = 该场毛基差 − 出手门槛（≥0 即可触发）")
    print("可执行场 = 白名单 venues ∩ 该标的已核验 mappings 的合约；最优场仅供参考，没有适配器就不能下单")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
