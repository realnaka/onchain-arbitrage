#!/usr/bin/env python3
"""Measure Lighter's order-book WebSocket as a clock. Read-only, no orders.

This is the probe behind `docs/research/lighter-ws-clock-2026-09-22.md`. It
answers the only question that matters for the freshness gate: how often does the
venue speak about each market, and therefore how often is the venue clock inside
the strategy's own 15 s budget? Run it before trusting Lighter's rows at all.

    PYTHONPATH=src .venv/bin/python scripts/lighter-ws-probe.py --seconds 180

Needs the shared local proxy (127.0.0.1:4780) and, in this sandbox, escalated
permissions (outbound sockets). It never sends a transaction: the stream only
subscribes to books and pings.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arb_monitor.data.hk_cex import now_ms  # noqa: E402
from arb_monitor.data.lighter_stream import LighterOrderBookStream  # noqa: E402

DEFAULT_MARKETS = ["201", "203", "204", "230"]  # TENCENT / XIAOMI / POPMART / SHEIN
FRESHNESS_BUDGET_MS = 15_000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markets", default=",".join(DEFAULT_MARKETS))
    parser.add_argument("--seconds", type=float, default=180.0)
    parser.add_argument("--proxy", default="http://127.0.0.1:4780")
    parser.add_argument("--sample-seconds", type=float, default=1.0)
    args = parser.parse_args()
    markets = [m.strip() for m in args.markets.split(",") if m.strip()]

    stream = LighterOrderBookStream(markets, proxy=args.proxy or None)
    stream.start()
    stats = {m: {"fresh": 0, "total": 0, "ages": [], "levels": []} for m in markets}
    deadline = time.monotonic() + args.seconds
    try:
        while time.monotonic() < deadline:
            now = now_ms()
            for market in markets:
                entry = stats[market]
                entry["total"] += 1
                clock = stream.clock_for(market, now)
                if not clock:
                    continue
                age = now - clock["ts"]
                entry["ages"].append(age)
                entry["levels"].append((clock["frame_bid_levels"],
                                        clock["frame_ask_levels"]))
                if age <= FRESHNESS_BUDGET_MS:
                    entry["fresh"] += 1
            time.sleep(args.sample_seconds)
    finally:
        health = stream.health()
        stream.close()

    print(f"frames {health['frames']}  snapshots {health['snapshots']}  "
          f"continuity_breaks {health['continuity_breaks']}  "
          f"connects {health['connects']}  reconnects {health['reconnects']}  "
          f"pongs {health['pongs']}  last_error {health['last_error']}")
    print(f"clock_offset_ms (venue - ours, median): {health['clock_offset_ms']}")
    print(f"\n{'market':8s} {'<=15s':>8s} {'median_age_ms':>14s} {'p90':>8s} "
          f"{'max':>8s} {'frame_levels bid/ask':>21s}")
    for market in markets:
        entry = stats[market]
        if not entry["ages"]:
            print(f"{market:8s} {'0.0%':>8s} {'-':>14s} {'-':>8s} {'-':>8s} {'-':>21s}")
            continue
        ages = sorted(entry["ages"])
        bids, asks = zip(*entry["levels"])
        print(f"{market:8s} {100 * entry['fresh'] / entry['total']:7.1f}% "
              f"{statistics.median(ages):14.0f} {ages[int(len(ages) * 0.9)]:8d} "
              f"{ages[-1]:8d} "
              f"{statistics.median(bids):10.0f}/{statistics.median(asks):<10.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
