from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path
import json

from arb_monitor.core.models import FeeModel, Level, OrderBook
from arb_monitor.ledger.store import EventStore
from arb_monitor.ops.dedup import SignalDeduplicator
from arb_monitor.strategy.spread import CrossVenueSpreadStrategy


def demo() -> int:
    now = 1_700_000_000_000
    books = [
        OrderBook(
            "demo_buy",
            "BTC/USDT",
            bids=(Level(Decimal("59990"), Decimal("0.5")),),
            asks=(Level(Decimal("60000"), Decimal("0.05")),),
            venue_ts_ms=now,
            recv_ts_ms=now + 40,
            sequence=1,
        ),
        OrderBook(
            "demo_sell",
            "BTC/USDT",
            bids=(Level(Decimal("60250"), Decimal("0.05")),),
            asks=(Level(Decimal("60260"), Decimal("0.5")),),
            venue_ts_ms=now,
            recv_ts_ms=now + 45,
            sequence=1,
        ),
    ]
    strategy = CrossVenueSpreadStrategy(
        "demo_buy",
        "demo_sell",
        FeeModel(
            buy_bps=Decimal("10"),
            sell_bps=Decimal("10"),
            slippage_bps=Decimal("3"),
            failure_buffer_bps=Decimal("2"),
        ),
        max_notional_quote=Decimal("500"),
        min_net_edge_bps=Decimal("12"),
        min_profit_quote=Decimal("0.50"),
    )
    dedup = SignalDeduplicator(cooldown_ms=3000)
    store = EventStore(Path("data/arb_monitor.db"))
    try:
        intents = list(strategy.on_snapshot(books, now))
        for intent in intents:
            if dedup.accept(intent):
                row_id = store.record_intent(intent)
                print(
                    f"signal #{row_id}: {intent.symbol} "
                    f"net={intent.net_edge_bps}bps "
                    f"profit={intent.expected_profit_quote} USDT"
                )
        if not intents:
            print("no executable opportunity")
    finally:
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arb-monitor")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("demo", help="run deterministic local demo")
    replay_parser = sub.add_parser("replay", help="replay a JSONL event file locally")
    replay_parser.add_argument("path")
    account_parser = sub.add_parser(
        "account-status", help="read-only one-shot account/position/risk snapshot")
    account_parser.add_argument("--okx-env", default="config/okx-demo.env")
    account_parser.add_argument("--json", action="store_true")
    account_board = sub.add_parser(
        "account-dashboard", help="serve the read-only account and risk board")
    account_board.add_argument("--port", type=int, default=8766)
    account_board.add_argument("--okx-env", default="config/okx-demo.env")
    account_board.add_argument("--ttl", type=int, default=15,
                               help="seconds between upstream re-reads")
    board = sub.add_parser(
        "board",
        help="serve one page with two tabs: market board + account/risk board")
    board.add_argument("--port", type=int, default=8765)
    board.add_argument("--okx-env", default="config/okx-demo.env")
    board.add_argument("--ttl", type=int, default=15,
                       help="seconds between upstream account re-reads")
    from arb_monitor.apps.full_chain_cli import add_arguments as add_full_chain
    add_full_chain(sub)
    from arb_monitor.apps.reconcile_cli import add_arguments as add_reconcile
    add_reconcile(sub)
    from arb_monitor.apps.run_report_cli import add_arguments as add_run_report
    add_run_report(sub)
    from arb_monitor.apps.mapping_cli import add_arguments as add_mapping
    add_mapping(sub)
    from arb_monitor.apps.paper_cli import add_arguments as add_paper
    add_paper(sub)
    from arb_monitor.apps.triage_cli import add_arguments as add_triage
    add_triage(sub)
    from arb_monitor.ops.venue_scoreboard import add_arguments as add_scoreboard
    add_scoreboard(sub)
    from arb_monitor.ops.retention import add_arguments as add_retention
    add_retention(sub)
    from arb_monitor.apps.hk_monitor import add_arguments, run_command
    add_arguments(sub)
    args = parser.parse_args(argv)
    if args.command == "demo":
        return demo()
    if args.command == "full-chain":
        from arb_monitor.apps.full_chain_cli import run as run_full_chain
        return run_full_chain(args)
    if args.command == "ledger-reconcile":
        from arb_monitor.apps.reconcile_cli import run as run_reconcile
        return run_reconcile(args)
    if args.command == "run-report":
        from arb_monitor.apps.run_report_cli import run as run_report
        return run_report(args)
    if args.command == "mapping-verify":
        from arb_monitor.apps.mapping_cli import run as run_mapping
        return run_mapping(args)
    if args.command == "paper-position":
        from arb_monitor.apps.paper_cli import run as run_paper
        return run_paper(args)
    if args.command == "ledger-triage":
        from arb_monitor.apps.triage_cli import run as run_triage
        return run_triage(args)
    if args.command == "venue-scoreboard":
        from arb_monitor.ops.venue_scoreboard import run as run_scoreboard
        return run_scoreboard(args)
    if args.command == "db-retention":
        from arb_monitor.ops.retention import run as run_retention
        return run_retention(args)
    if args.command in {"account-status", "account-dashboard"}:
        from arb_monitor.apps.account_dashboard import (account_html, build_snapshot,
                                                        okx_read_env, serve_dashboard,
                                                        verification)
        from arb_monitor.execution.okx_account import OKXReadOnlyClient
        from arb_monitor.execution.wallet_cli import OnchainOSRunner
        if args.command == "account-dashboard":
            return serve_dashboard(port=args.port, ttl_seconds=args.ttl,
                                   okx_env=args.okx_env)
        client = OKXReadOnlyClient.from_env(okx_read_env(args.okx_env))
        snapshot = build_snapshot(
            client=client if client.configured else None,
            runner=OnchainOSRunner(),
        )
        # The verdict is attached here as well as inside the snapshot builder:
        # a caller that hands this command a snapshot from somewhere else still
        # gets `verified` in the output, and the exit code below can never
        # disagree with the JSON the operator reads.
        snapshot.update(verification(snapshot))
        if args.json:
            from arb_monitor.core.hk import dumps
            print(dumps(snapshot))
        else:
            print(account_html(snapshot))
        # Reviewed 2026-09-16: this command reported
        # ``okx.status="error"``/``wallet.status="error"`` and still exited 0,
        # so nothing calling it from a script could tell "verified" from "the
        # account could not be read". Non-zero is the honest answer, and the
        # JSON still carries the detail for a human. The snapshot always
        # contains ``verified``; recomputed here so the exit code and the
        # unattended gate cannot drift apart.
        verdict = verification(snapshot)
        if not verdict["verified"]:
            print("account-status: UNVERIFIED "
                  f"({','.join(verdict['unverified_sources'])}) — "
                  "JSON says verified=false; do not treat this as a good account",
                  file=sys.stderr)
            return 1
        return 0
    if args.command == "board":
        from arb_monitor.apps.board import serve_board
        return serve_board(port=args.port, ttl_seconds=args.ttl,
                           okx_env=args.okx_env)
    if args.command == "replay":
        from arb_monitor.execution.replay import load_events
        events = load_events(args.path)
        for event in events:
            print(json.dumps({"ts_ms": event.ts_ms, "payload": event.payload},
                             ensure_ascii=False, sort_keys=True))
        return 0
    if args.command and (args.command.startswith("hk-") or args.command == "hk-depth-census"):
        try:
            return run_command(args)
        except (ValueError, OSError, RuntimeError) as exc:
            print(f"monitor error: {exc}", file=sys.stderr)
            return 2
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
