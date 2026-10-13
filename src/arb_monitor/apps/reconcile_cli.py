"""Append an externally confirmed settlement to the execution ledger.

Some legs finish outside the process that started them (a manual close, a CLI
crash after broadcast, a wallet action). The ledger must still describe what
happened — but never by pretending the automated path produced it. This command
takes the confirmed facts from the operator (tx hash, fills, balances) and
appends a ``reconciled`` event to the group, marked with its source.

Usage::

    arb-monitor ledger-reconcile --group xiaomi-7b3b98035b --facts facts.json
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from arb_monitor.execution.ledger import ExecutionLedger


def add_arguments(subparsers) -> None:
    parser = subparsers.add_parser(
        "ledger-reconcile",
        help="append externally confirmed fills to an execution ledger group")
    parser.add_argument("--group", required=True, help="execution group id")
    parser.add_argument("--facts", required=True, help="JSON file with the confirmed facts")
    parser.add_argument("--ledger", default="data/execution.db")
    parser.add_argument("--state", default="closed",
                        help="state to record (default: closed)")
    parser.add_argument("--event-type", default="reconciled",
                        help="event type to record (default: reconciled)")
    parser.add_argument("--source", default="reconciled_from_chain_and_cex_readback")


def run(args: argparse.Namespace) -> int:
    facts = json.loads(Path(args.facts).read_text(encoding="utf-8"))
    if not isinstance(facts, dict):
        print("facts must be a JSON object")
        return 2
    ledger = ExecutionLedger(args.ledger)
    try:
        events = ledger.events(args.group)
        if not events:
            print(f"unknown group: {args.group}")
            return 2
        payload = {
            "signal_id": events[0].get("signal_id"),
            "source": args.source,
            "reconciled_at_ms": time.time_ns() // 1_000_000,
            "note": facts.get("note", ""),
            **{k: v for k, v in facts.items() if k != "note"},
        }
        row_id = ledger.append(args.group, args.state, args.event_type, payload,
                               time.time_ns() // 1_000_000)
        print(json.dumps({"group": args.group, "event_id": row_id,
                          "state": args.state, "source": args.source,
                          "previous_state": events[-1].get("state"),
                          "events_before": len(events)}, ensure_ascii=False))
    finally:
        ledger.close()
    return 0
