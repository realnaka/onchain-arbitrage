#!/usr/bin/env python3
"""Refresh the v2 balances snapshot that the board renders.

    scripts/xstocks_v2_balances.py          # collect, write, print a summary
    scripts/xstocks_v2_balances.py show     # print the last snapshot only

Read-only against every venue: no order, no transfer, no signature. A balance
that cannot be read is written as ``amount: null`` plus the error, so the board
can print 「读不到」 instead of a 0 that looks like an empty wallet.
"""
from __future__ import annotations

import argparse
import json

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.venues.kraken_spot import (KrakenSpotClient,  # noqa: E402
                                            load_credentials)
from arb_monitor.xstocks_v2 import balances, scan               # noqa: E402
from arb_monitor.xstocks_v2.feeds.http import Http              # noqa: E402
from arb_monitor.xstocks_v2.store import write_json             # noqa: E402

DATA = ROOT / "data-v2"
OUT = DATA / "xstocks_v2_balances.json"
PROXY = "http://127.0.0.1:4780"


def kraken_client():
    key, secret = load_credentials(ROOT / "config" / "kraken.env")
    return KrakenSpotClient(key, secret, proxy=PROXY)


def summary(snapshot: dict) -> str:
    lines = []
    for entry in snapshot.get("groups") or []:
        parts = []
        for item in entry.get("rows") or []:
            if item.get("amount") is None:
                parts.append(f"{item['asset']}=读不到")
            else:
                parts.append(f"{item['asset']}={item['amount']}")
        tail = ""
        if entry.get("usd_total") is not None:
            tail += f"  ≈{entry['usd_total']} USD"
        if entry.get("note"):
            tail += f"  [{entry['note']}]"
        lines.append(f"{entry.get('label')}: " + "  ".join(parts) + tail)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", default="refresh",
                        choices=("refresh", "show"))
    args = parser.parse_args()
    if args.mode == "show":
        if not OUT.exists():
            print(f"{OUT} 不存在（还没有采集过）")
            return 1
        print(summary(json.loads(OUT.read_text())))
        return 0
    try:
        client = kraken_client()
    except Exception as exc:  # noqa: BLE001
        print(f"kraken client unavailable: {type(exc).__name__}: {exc}"[:160])
        client = None
    snapshot = balances.collect(http=Http(), kraken_client=client,
                                wrappers=balances.watched_forms(),
                                now_ms=int(time.time() * 1000))
    write_json(OUT, snapshot)
    print(summary(snapshot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
