#!/usr/bin/env python3
"""Stage 0 entry point: read-only bilateral scan. No signing, no orders.

    scripts/xstocks_v2_scan.py status
    scripts/xstocks_v2_scan.py once                  # ranked, budgeted round
    scripts/xstocks_v2_scan.py once --full-bilateral  # timed, every mint, both sides
    scripts/xstocks_v2_scan.py run --interval 120

``once`` ranks Solana with batched reference prices and then deep-quotes only
the names close to the threshold. ``--full-bilateral`` is the measurement that
decides whether the resident loop can afford to do that for every mint: the
free Jupiter tier paces at about 0.9 requests/s.

Every number this writes is an observation. Nothing here is executable while
``phase = "scan"``.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.xstocks_v2 import scan  # noqa: E402
from arb_monitor.xstocks_v2.policy import V2Policy  # noqa: E402


def status(policy: V2Policy | None = None) -> dict:
    policy = policy or V2Policy.load()
    allowed, reason = policy.live_broadcast_allowed()
    return {
        "phase": policy.phase,
        "auto_orders": policy.auto_orders,
        "broadcast": allowed,
        "reason": reason,
    }


def _print_summary(payload: dict) -> None:
    print(json.dumps({
        "round_seconds": payload["round_seconds"],
        "session": payload["session"],
        "counters": payload["counters"],
        "universe": payload["universe"],
    }, ensure_ascii=False), flush=True)
    pairs = payload.get("venue_pairs") or []
    if pairs:
        print("路径族（买 → 卖）                 行数   最好价差 bps   主要拒因")
        for item in pairs[:12]:
            top = sorted((item.get("reasons") or {}).items(), key=lambda kv: -kv[1])[:2]
            print(f"{item['family']:<17} {item['buy_venue']:<11} → "
                  f"{item['sell_venue']:<11} {item['rows']:>4} "
                  f"{str(item['best_edge_bps']):>12}   "
                  + ", ".join(f"{name}×{count}" for name, count in top))
    for row in (payload.get("preview") or [])[:10]:
        print(f"  {row['symbol']:<9} {row['family']:<16} {row['chain']:<8} "
              f"t0={str(row['edge_t0_bps']):>10} bps  "
              f"blockers={','.join(row['blockers']) or '-'}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    once = sub.add_parser("once")
    once.add_argument("--full-bilateral", action="store_true",
                      help="quote every Solana mint on both sides and record the time")
    once.add_argument("--data-dir", default=str(scan.DEFAULT_DATA))
    once.add_argument("--json", action="store_true")
    run = sub.add_parser("run")
    run.add_argument("--interval", type=float, default=120.0)
    run.add_argument("--data-dir", default=str(scan.DEFAULT_DATA))
    run.add_argument("--rounds", type=int, default=0, help="0 = forever")

    args = parser.parse_args()
    if args.cmd == "status":
        print(json.dumps(status(), ensure_ascii=False))
        return 0
    if args.cmd == "once":
        if args.full_bilateral:
            timing = scan.run_full_bilateral(data_dir=args.data_dir)
            print(json.dumps(timing, ensure_ascii=False))
            return 0
        payload = scan.run_scan(data_dir=args.data_dir)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, default=str)[:4000])
        else:
            _print_summary(payload)
        return 0
    if args.cmd == "run":
        rounds = 0
        while args.rounds == 0 or rounds < args.rounds:
            rounds += 1
            started = time.monotonic()
            try:
                payload = scan.run_scan(data_dir=args.data_dir)
                _print_summary(payload)
            except Exception as exc:  # noqa: BLE001
                # A failed round is reported, not retried in a tight loop.
                print(json.dumps({"round": rounds, "error": f"{type(exc).__name__}: {exc}"},
                                 ensure_ascii=False), flush=True)
                time.sleep(10)
                continue
            time.sleep(max(5.0, args.interval - (time.monotonic() - started)))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
