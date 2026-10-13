"""Paper positions: list them, close them, and see what the sample can say.

```bash
PYTHONPATH=src .venv/bin/python -m arb_monitor paper-position --list
PYTHONPATH=src .venv/bin/python -m arb_monitor paper-position \
    --group shein-ead4a10622 --exit-basis-bps 1.2 --note "basis converged"
PYTHONPATH=src .venv/bin/python -m arb_monitor paper-position --stats
```

A paper position is a *model*: the entry event says the fill was assumed at the
touch. Closing one writes the observed exit basis and the arithmetic that turned
it into PnL, so the result can be audited instead of remembered. It never
pretends a real trade happened, and it never touches the live quota.
"""
from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation

from arb_monitor.core.hk import dumps
from arb_monitor.execution.ledger import ExecutionLedger
from arb_monitor.execution.live_stack import EXECUTION_LEDGER


def add_arguments(subparsers) -> None:
    parser = subparsers.add_parser(
        "paper-position",
        help="list/close modelled paper positions (never touches real quota)")
    parser.add_argument("--list", action="store_true", help="show paper positions")
    parser.add_argument("--stats", action="store_true",
                        help="what the paper sample can and cannot say")
    parser.add_argument("--group", help="the ledger group to close")
    parser.add_argument("--exit-basis-bps", help="observed exit basis, in bps")
    parser.add_argument("--note", default="", help="why this exit basis")
    parser.add_argument("--ledger", default=EXECUTION_LEDGER)
    parser.add_argument("--json", action="store_true")


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"invalid decimal: {value}") from exc


def run(args: argparse.Namespace) -> int:
    ledger = ExecutionLedger(args.ledger)
    try:
        if args.group:
            if args.exit_basis_bps in (None, ""):
                print("refused: --group needs --exit-basis-bps "
                      "(the modelled exit is an assumption, so it has to be stated)")
                return 2
            import time
            try:
                payload = ledger.close_paper_position(
                    args.group, exit_basis_bps=_decimal(args.exit_basis_bps),
                    created_ts_ms=time.time_ns() // 1_000_000, note=args.note)
            except KeyError:
                print(f"refused: no open paper position {args.group}")
                return 1
            except ValueError as exc:
                print(f"refused: {exc}")
                return 1
            print(dumps(payload) if args.json else _render_close(payload))
            return 0
        if args.stats:
            stats = ledger.paper_stats()
            print(dumps(stats) if args.json else _render_stats(stats))
            return 0
        positions = ledger.paper_positions()
        if args.json:
            print(dumps({"positions": positions}))
            return 0
        print(_render_list(positions))
        return 0
    finally:
        ledger.close()


def _render_list(positions: list[dict]) -> str:
    if not positions:
        return "paper positions: none"
    lines = [f"{'group':<22} {'state':<8} {'size_quote':>10} "
             f"{'entry_bps':>10} {'cost_bps':>8}"]
    for row in positions:
        lines.append(f"{row['group_id']:<22} {row['state']:<8} "
                     f"{row['size_quote'] or '—':>10} "
                     f"{row['entries'] or '—':>10} "
                     f"{row['entry_cost_bps'] or '—':>8}")
    return "\n".join(lines)


def _render_close(payload: dict) -> str:
    return "\n".join([
        f"纸面平仓（模型，无真实成交）: {payload['group_id']}",
        f"开仓基差 {payload['entry_basis_bps']} bps → 平仓基差 "
        f"{payload['exit_basis_bps']} bps",
        f"名义 {payload['size_quote']} · 开仓成本 {payload['entry_cost_bps']} bps/腿",
        f"模型毛盈亏 {payload['modelled_gross_pnl_quote']} · 成本 "
        f"{payload['modelled_cost_quote']} · 净 "
        f"{payload['modelled_net_pnl_quote']} (quote token)",
    ])


def _render_stats(stats: dict) -> str:
    lines = [
        f"纸面组 {stats['groups']}（未平 {stats['open_positions']} / "
        f"已平 {stats['closed_positions']}），模型名义合计 {stats['modelled_quote']}",
        "尚无数据的指标: " + ", ".join(stats["unavailable"]),
        "原因: " + stats["unavailable_reason"],
    ]
    return "\n".join(lines)
