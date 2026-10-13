#!/usr/bin/env python3
"""Ledger maintenance for v2.

    scripts/xstocks_v2_ledger.py stats
    scripts/xstocks_v2_ledger.py prune [--days 2] [--dry-run]

``prune`` drops only *paper* rows older than ``--days``; every real-money row is
kept forever, because those are the rows that say where the coins are. The paper
runner calls the same function best-effort once an hour, so the file stops
growing ~7k rows a day without anyone remembering to run this.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.xstocks_v2 import ledger, realized  # noqa: E402


def stats(path: Path) -> dict:
    counts: dict[str, int] = {}
    modes: dict[str, int] = {}
    malformed = 0
    total = 0
    for line in (path.read_text().splitlines() if path.exists() else []):
        if not line.strip():
            continue
        total += 1
        try:
            entry = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if not isinstance(entry, dict):
            malformed += 1
            continue
        mode = str(entry.get("mode") or "live")
        modes[mode] = modes.get(mode, 0) + 1
        state = str(entry.get("state") or "?")
        counts[state] = counts.get(state, 0) + 1
    return {"path": str(path), "rows": total, "malformed": malformed,
            "by_mode": dict(sorted(modes.items())),
            "by_state": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
            "bytes": path.stat().st_size if path.exists() else 0}


def realized_report(path: Path) -> dict:
    """What the ledger can prove about money, and what it cannot.

    The reconciliation the 2026-10-07 patrol asked for: the sum of realized nets
    has to be read next to the three wallets' own change, and every ``sold`` row
    that could not be priced is listed with the part it is missing.
    """
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    totals = realized.totals(rows)
    return {"path": str(path), **totals}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("stats", "prune", "realized"))
    parser.add_argument("--days", type=int, default=2,
                        help="keep paper rows from the last N days")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--path", default=str(ledger.DEFAULT_PATH))
    args = parser.parse_args()
    path = Path(args.path)

    if args.mode == "stats":
        print(json.dumps(stats(path), indent=1, ensure_ascii=False))
        return 0
    if args.mode == "realized":
        print(json.dumps(realized_report(path), indent=1, ensure_ascii=False))
        return 0
    result = ledger.prune(path, keep_paper_days=args.days,
                          dry_run=args.dry_run)
    result["stats_after"] = stats(path)
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
