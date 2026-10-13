#!/usr/bin/env python3
"""Print the whole any-to-any path catalogue for one symbol.

    scripts/xstocks_v2_paths.py SPYx                 # every reachable exit
    scripts/xstocks_v2_paths.py SPYx --from kraken   # only one entry venue
    scripts/xstocks_v2_paths.py SPYx --notional 200

Read-only: it loads the bridge registration, the measured-route ledger and the
Kraken rails, builds the graph and ranks. It never signs and never broadcasts.
Add a venue by adding a builder in ``xstocks_v2/paths.py`` -- this script needs
no change.
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.xstocks_v2 import paths as P          # noqa: E402
from arb_monitor.xstocks_v2 import routes as R         # noqa: E402
from arb_monitor.xstocks_v2 import scan                # noqa: E402
from arb_monitor.xstocks_v2.feeds import census        # noqa: E402

BOOK = ROOT / "data" / "xstocks_book.sqlite"
CENSUS = ROOT / "data" / "xstocks_chain_census.json"
V2_ADDRESS_BOOK = ROOT / "data-v2" / "xstocks_v2_address_book.json"


def native_chains() -> set[str]:
    """Chains whose NATIVE token has a pool in the census."""
    try:
        data = json.loads(CENSUS.read_text())
    except (OSError, ValueError):
        return {"solana"}
    out = set()
    for row in data.get("candidates") or []:
        if str(row.get("form") or "native") != "native":
            continue
        chain = census.chain_key(row.get("slug") or row.get("network"))
        if chain:
            out.add(chain)
    return out or {"solana"}


def kraken_rails(symbol: str) -> tuple[dict | None, dict | None]:
    """Deposit / withdrawal rails for this symbol from the cached tables.

    Both files are produced by the scan/refresh steps; a missing file means the
    rail is unknown, and unknown is not an edge.
    """
    deposit = None
    try:
        rows = scan.load_deposits()
        for row in rows.get(symbol) or []:
            name = str(row.get("method") or "")
            if "solana" in name.lower() or name.lower() == symbol.lower():
                deposit = {"chain": "solana", "method": name,
                           "minimum": row.get("minimum"), "duration_s": 60}
                break
    except Exception:  # noqa: BLE001
        pass
    withdraw = None
    try:
        rows = json.loads(V2_ADDRESS_BOOK.read_text()).get("address_book") or []
    except (OSError, ValueError):
        rows = []
    try:
        for row in list(rows) + list(scan.load_address_book()):
            net = str(row.get("network") or "").lower()
            name = str(row.get("method") or "").lower()
            if str(row.get("asset")) == symbol and "solana" in (net + " " + name):
                withdraw = {"chain": "solana", "key": row.get("key"),
                            "fee_usd": None, "minimum": None, "duration_s": 120}
                break
    except Exception:  # noqa: BLE001
        pass
    return deposit, withdraw


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("symbol", nargs="?", default="SPYx")
    parser.add_argument("--from", dest="entry", default=None)
    parser.add_argument("--notional", default="25")
    parser.add_argument("--tokens", default=None)
    parser.add_argument("--max-legs", type=int, default=5)
    args = parser.parse_args()

    measured = scan.load_measured_bridges()
    deposit, withdraw = kraken_rails(args.symbol)
    graph = P.full_catalog(
        symbol=args.symbol, measured=measured, native_chains=native_chains(),
        deposit_rail=deposit, withdraw_rail=withdraw)

    notional = Decimal(args.notional)
    tokens = Decimal(args.tokens) if args.tokens else notional / Decimal("780")

    entries = [("kraken", P.cash_node("kraken"))]
    for chain in P.ALL_CHAINS:
        if R.norm(chain) == "ton":
            continue
        entries.append((chain, P.cash_node(chain, chain)))
    starts = [row for row in entries if not args.entry or row[0] == args.entry]

    print(f"# {args.symbol}: notional {notional} USD, {tokens:.6f} tokens, "
          f"measured bridges {sorted(measured)}")
    print(f"# deposit rail {deposit and deposit['method']}  "
          f"withdraw rail {withdraw and withdraw['key']}")
    print(f"# native pools on: {sorted(native_chains())}")
    print()
    print("| 买入场所 | 卖出场所 | 路径 | 成本 bps | 成本 USD | 时长 s | 可执行 | 阻塞 |")
    print("|---|---|---|---:|---:|---:|---|---|")
    for name, start in starts:
        for dest in entries:
            if dest[0] == name:
                continue
            found = graph.enumerate(start, dest[1], args.symbol, max_legs=args.max_legs)
            if not found:
                continue
            ranked = graph.rank(found, notional_usd=notional, tokens=tokens)
            best = ranked[0]
            print(f"| {name} | {dest[0]} | {best.route} | {best.cost_bps:.1f} | "
                  f"{best.cost_usd:.4f} | {best.duration_s:.0f} | "
                  f"{'YES' if best.executable else 'no'} | "
                  f"{','.join(best.blockers) or '-'} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
