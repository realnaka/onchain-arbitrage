#!/usr/bin/env python3
"""Cache Kraken's WithdrawMethods so the offline scan can join the address book.

    PYTHONPATH=src .venv/bin/python scripts/xstocks_v2_kraken_methods.py

Why this is needed: the address book is keyed by ``(asset, method)``, and for
stablecoins the method is just the ticker (``USDC``). Which *network* that is
lives in ``WithdrawMethods``, not in the address book. Without the join, a
registered USDC address looks unregistered.

Read-only: only ``WithdrawMethods`` and ``WithdrawAddresses`` are called.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.venues.kraken_spot import (  # noqa: E402
    KrakenError, KrakenSpotClient, load_credentials)
from arb_monitor.xstocks_v2.feeds import census as census_feed  # noqa: E402

PROXY = "http://127.0.0.1:4780"
OUT = ROOT / "data-v2" / "xstocks_v2_withdraw_methods.json"
# Stablecoins are the ones we rebalance with, and they are the ones whose method
# name carries no network word.
STABLES = ("USDC", "USDT", "USDG", "DAI", "PYUSD", "USD1")


def scope_symbols(all_symbols: bool) -> list[str]:
    """Default to names we can actually price, not all 177.

    A symbol with no registered address is blocked either way, so asking Kraken
    about all 177 only spends the private rate budget. ``--all`` is there for
    the day the address book is fuller.
    """
    if all_symbols:
        return list(census_feed.kraken_symbols().keys())
    ours = set(census_feed.kraken_symbols().keys())
    pools = census_feed.census_pools(census_feed.load_census())
    pools += census_feed.v4_pool_rows(census_feed.load_v4())
    symbols = {str(pool["symbol"]) for pool in pools}
    return sorted((symbols | set(STABLES)) & (ours | set(STABLES)))


def main() -> int:
    key, secret = load_credentials(ROOT / "config" / "kraken.env")
    client = KrakenSpotClient(key, secret, proxy=PROXY)
    wanted = scope_symbols("--all" in sys.argv)
    known = set(census_feed.kraken_symbols().keys())
    out: dict[str, list] = {}
    failures: dict[str, str] = {}
    for symbol in wanted:
        aclass = "tokenized_asset" if symbol in known else "currency"
        try:
            out[symbol] = client._post("/0/private/WithdrawMethods",
                                       {"asset": symbol, "aclass": aclass}) or []
        except KrakenError as exc:
            failures[symbol] = "; ".join(str(item) for item in exc.errors)[:80]
        except Exception as exc:  # noqa: BLE001
            failures[symbol] = f"{type(exc).__name__}: {exc}"[:80]
        time.sleep(0.4)   # private endpoints are counter-limited, not free
    try:
        addresses = client._post("/0/private/WithdrawAddresses", {}) or []
    except Exception:  # noqa: BLE001
        addresses = []

    payload = {
        "fetched_ms": int(time.time() * 1000),
        "assets": len(out),
        "scope": wanted,
        "failures": failures,
        "withdraw_methods": out,
        # The address book is cached alongside so one file answers the whole
        # question: given this (asset, chain), can we withdraw?
        "address_book": [{"key": row.get("key"), "asset": row.get("asset"),
                          "method": row.get("method")} for row in addresses],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(json.dumps({"assets": len(out), "addresses": len(addresses),
                      "failures": len(failures), "out": str(OUT)},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
