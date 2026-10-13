#!/usr/bin/env python3
"""Append-only ledger for manual Ethereum xStocks trades.

The Solana/Jupiter automation writes ``data/xstocks_cycles.jsonl``.  The
Ethereum path is deliberately separate until it is automated:

* ``data/xstocks_eth_cycles.jsonl`` is the machine-readable trade history;
* ``HANDOFF.md`` / runbooks remain the narrative record.

Read-only by default. ``backfill-strcx`` writes one known historical record.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "data" / "xstocks_eth_cycles.jsonl"


def normalize(record: dict) -> dict:
    out = dict(record)
    out.setdefault("network", "Ethereum")
    out.setdefault("day", time.strftime("%Y-%m-%d"))
    out.setdefault("ts_ms", int(time.time() * 1000))
    out.setdefault("source", "manual_eth")
    return out


def append(record: dict) -> dict:
    row = normalize(record)
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False, sort_keys=True)
    with LEDGER.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return row


def read_records() -> list[dict]:
    if not LEDGER.exists():
        return []
    rows: list[dict] = []
    for line in LEDGER.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def strcx_backfill() -> dict:
    return {
        "day": "2026-10-07",
        "symbol": "STRCx",
        "network": "Ethereum",
        "state": "sold",
        "reason": "最小实盘验证，已清仓；结论：无套利",
        "size_usd": "5.2",
        "tokens": "0.05184276126657151",
        "buy_price": "100.30329930271263999411498281664392675658565546244",
        "sell_price": "99.20",
        "gross_usdt": "10.60717",
        "net_usdt": "-1.88",
        "txids": {
            "approve": "0x0000000000000000000000000000000000000000000000000000000000000000",
            "buy": "0x0000000000000000000000000000000000000000000000000000000000000000",
            "deposit": "0x0000000000000000000000000000000000000000000000000000000000000000",
            "sell": [
                "OZZG54-I3FZ6-RJR37R",
                "OFZDGL-3U2XF-OKP3VE",
            ],
        },
        "kraken_refids": {
            "withdraw_usdc": "FTVPFbO-NACZTse4SYOMjbyQsy9NG2",
            "deposit_strcx": "FTC4Ard-hGdZt4GY4aa710Bh6vvkat",
        },
        "fees": {
            "usdc_withdraw": "0.8602",
            "gas_eth": "0.000327429022597176",
            "gas_usdt_approx": "0.88",
            "kraken_orders": "0.02164",
        },
        "balances_after": {
            "ethereum_eth": "0.001672570977402824",
            "ethereum_usdc": "0.2398",
            "kraken_usd": "46.9525",
            "kraken_usdc": "5.00000000",
            "strcx": "0",
        },
        "notes": [
            "Kraken 入账 scaled 单位与 EVM balanceOf 1:1",
            "Kraken STRCx 实际最小下单量 0.05459063574070758198421675539",
            "第一次卖出只成交 0.065，剩余 0.041843 低于最小值",
            "为清仓在 Kraken 补买两次各 0.055 STRCx，最终全部卖出",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show", help="print all records")
    sub.add_parser("backfill-strcx", help="append the 2026-10-07 STRCx record once")
    sub.add_parser("summary", help="print a compact summary")
    args = parser.parse_args(argv)
    if args.command == "backfill-strcx":
        rows = read_records()
        if any(row.get("txids", {}).get("buy") ==
               strcx_backfill()["txids"]["buy"] for row in rows):
            print("already present")
            return 0
        append(strcx_backfill())
        print(f"appended to {LEDGER}")
        return 0
    rows = read_records()
    if args.command == "show":
        for row in rows:
            print(json.dumps(row, ensure_ascii=False, indent=2))
        return 0
    total = sum(Decimal(str(row.get("net_usdt") or "0")) for row in rows)
    print(json.dumps({
        "records": len(rows),
        "net_usdt": format(total, "f"),
        "symbols": sorted({str(row.get("symbol")) for row in rows}),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
