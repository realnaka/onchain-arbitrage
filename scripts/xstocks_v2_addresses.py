#!/usr/bin/env python3
"""Refresh v2's own Kraken address-book cache.

    scripts/xstocks_v2_addresses.py            # print what is registered

Why a v2-local file: ``data/xstocks_route_matrix.json`` is a v1 artifact and v2
never writes v1's data directory. The Kraken API cannot add an address (lessons
3.2), so this only *reads* and caches.

Tokenized assets are invisible to an unfiltered ``WithdrawAddresses`` call --
the 2026-10-07 ``solanspyx1`` entry only showed up when the request carried
``aclass=tokenized_asset``. That is why this loops the symbols we care about.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.venues.kraken_spot import (  # noqa: E402
    KrakenSpotClient, TOKENIZED, load_credentials)

PROXY = "http://127.0.0.1:4780"
OUT = ROOT / "data-v2" / "xstocks_v2_address_book.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="",
                        help="comma separated; default = every symbol with a chain deposit rail")
    parser.add_argument("--pause", type=float, default=0.8,
                        help="seconds between private calls (Kraken counts them)")
    parser.add_argument("--replace", action="store_true",
                        help="drop existing entries for symbols this run did not ask about")
    args = parser.parse_args()

    key, secret = load_credentials(ROOT / "config" / "kraken.env")
    client = KrakenSpotClient(key, secret, proxy=PROXY)

    if args.symbols:
        symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
    else:
        symbols = []
        for row in client.balance():
            if row.endswith(".T"):
                symbols.append(row[:-2])
        # the balance list only shows what we hold; ask for the ones on the rails
        for row in client._post("/0/private/WithdrawMethods", {"asset": "SPYx",
                                                               "aclass": TOKENIZED}) or []:
            pass
        symbols = sorted(set(symbols) | {"SPYx", "TSLAx", "QQQx", "MSTRx", "AAPLx"})

    rows: list[dict] = []
    seen: set[str] = set()
    failed: set[str] = set()
    for symbol in symbols:
        try:
            networks = {}
            for method in client._post("/0/private/WithdrawMethods",
                                      {"asset": symbol, "aclass": TOKENIZED}) or []:
                name = str(method.get("method") or "").strip().lower()
                net = str(method.get("network") or "").strip()
                if name:
                    networks.setdefault(name, net)
            for row in client._post("/0/private/WithdrawAddresses",
                                    {"asset": symbol, "aclass": TOKENIZED}) or []:
                method_name = str(row.get("method") or "")
                rows.append({
                    "asset": symbol,
                    "key": row.get("key"),
                    "address": row.get("address"),
                    "method": method_name,
                    # the Solana rail for an xStock is named just ``SPYx`` with
                    # no network word, so the network has to come from
                    # WithdrawMethods (same lesson as the deposit rails).
                    "network": networks.get(method_name.strip().lower(), ""),
                    "verified": bool(row.get("verified")),
                })
            seen.add(symbol)
        except Exception as exc:  # noqa: BLE001
            failed.add(symbol)
            print(f"  {symbol}: {type(exc).__name__}: {str(exc)[:80]}")
        time.sleep(args.pause)

    # A rate-limited or failed symbol must not erase what is already cached
    # (2026-10-07: a partial run replaced 6 good entries with 12 others and
    # silently dropped solanspyx1, ethstrcx1, ethtlax1 ...).
    keep_from_file = not args.replace
    if keep_from_file:
        try:
            previous = json.loads(OUT.read_text()).get("address_book") or []
        except (OSError, ValueError):
            previous = []
        for row in previous:
            asset = str(row.get("asset") or "")
            if asset and asset not in seen and asset not in failed:
                rows.append(row)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(
        {"checked_at_ms": int(time.time() * 1000), "address_book": rows}, indent=1))
    print(f"{len(rows)} entries -> {OUT}"
          + (f" (failed: {', '.join(sorted(failed))})" if failed else ""))
    for row in rows:
        print(f"  {row['key']:14} asset={row['asset']:7} method={row['method']} "
              f"verified={row['verified']}")
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
