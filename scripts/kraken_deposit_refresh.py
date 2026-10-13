#!/usr/bin/env python
"""Refresh the Kraken xStocks reachability cache. Read-only, no orders.

For every ``tokenized_asset`` pair Kraken lists, store the deposit methods and
their minimums, plus the pair status. The board reads this file; it must never
do 350 private calls inside its quoting cycle.

    PYTHONPATH=src .venv/bin/python scripts/kraken_deposit_refresh.py

Writes data/kraken_exit_cache.json. Re-run every few hours (or before a live
session) because Kraken adds networks and changes minimums.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.venues.kraken_exit_gate import parse_asset_pairs, parse_deposit_methods  # noqa: E402
from arb_monitor.venues.kraken_spot import (  # noqa: E402
    KrakenError, KrakenSpotClient, load_credentials)

OUT = ROOT / "data" / "kraken_exit_cache.json"
PAUSE_S = 0.35
RATE_LIMIT_BACKOFF_S = 8.0


def fetch_pair_status(proxy: str) -> dict[str, dict]:
    import requests

    response = requests.get(
        "https://api.kraken.com/0/public/AssetPairs",
        params={"aclass_base": "tokenized_asset"}, timeout=60,
        proxies={"http": proxy, "https": proxy})
    response.raise_for_status()
    body = response.json()
    if body.get("error"):
        raise SystemExit(f"AssetPairs 报错：{body['error']}")
    return parse_asset_pairs(body["result"])


def refresh(client: KrakenSpotClient, symbols: list[str], *, pause_s: float = PAUSE_S) -> dict:
    deposits: dict[str, list[dict]] = {}
    failures: dict[str, str] = {}
    for index, symbol in enumerate(symbols, 1):
        for attempt in range(3):
            try:
                rows = client._post("/0/private/DepositMethods",
                                    {"asset": symbol, "aclass": "tokenized_asset"})
                deposits[symbol] = parse_deposit_methods(rows)
                break
            except KrakenError as exc:
                text = "; ".join(exc.errors)
                if "EAPI:Rate limit" in text and attempt < 2:
                    time.sleep(RATE_LIMIT_BACKOFF_S)
                    continue
                failures[symbol] = text
                break
            except Exception as exc:  # noqa: BLE001
                failures[symbol] = f"{type(exc).__name__}: {exc}"
                break
        if index % 25 == 0:
            print(f"  {index}/{len(symbols)} …", flush=True)
        time.sleep(pause_s)
    return {"deposits": deposits, "failures": failures}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Refresh Kraken xStocks exit cache")
    parser.add_argument("--proxy", default="http://127.0.0.1:4780")
    parser.add_argument("--limit", type=int, default=0, help="debug: only N symbols")
    parser.add_argument("--pause", type=float, default=PAUSE_S)
    args = parser.parse_args(argv)

    pairs = fetch_pair_status(args.proxy)
    symbols = sorted(pairs)
    if args.limit:
        symbols = symbols[:args.limit]
    print(f"Kraken 在册 {len(pairs)} 个标的，本轮核对 {len(symbols)} 个的充值方式")

    key, secret = load_credentials(ROOT / "config" / "kraken.env")
    client = KrakenSpotClient(key, secret, proxy=args.proxy)
    result = refresh(client, symbols, pause_s=args.pause)

    online = sum(1 for row in pairs.values() if row["status"] == "online")
    payload = {
        "refreshed_ms": int(time.time() * 1000),
        "source": "Kraken AssetPairs + DepositMethods",
        "pair_count": len(pairs),
        "online_count": online,
        "pairs": pairs,
        "deposits": result["deposits"],
        "failures": result["failures"],
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"写盘 {OUT}")
    print(f"  pairs {len(pairs)}（online {online}）· 充值方式 {len(result['deposits'])} 个"
          f"· 失败 {len(result['failures'])} 个")
    if result["failures"]:
        for symbol, text in list(result["failures"].items())[:5]:
            print(f"  ! {symbol}: {text[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
