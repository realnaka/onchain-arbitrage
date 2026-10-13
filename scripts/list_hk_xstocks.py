#!/usr/bin/env python
"""Enumerate the issuer's xStocks universe and what is actually tradable.

Answers a recurring question: X Layer carries far more HK xStocks than the few
we monitor, but most of them have no market. Three layers, all read-only:

1. issuer asset list (Backed/xStocks public API) -> every token plus its X Layer
   native address and wrapper address;
2. on-chain liquidity -> which tokens have a pool with real depth;
3. CEX perps -> which of those have a USD-linear perpetual to hedge with
   (checked separately with ``arb_monitor hk-list``).

Usage:
    PYTHONPATH=src .venv/bin/python scripts/list_hk_xstocks.py --liquidity
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.parse

ASSETS_API = "https://api.backed.fi/api/v2/public/assets"
OKX_SEARCH = "https://web3.okx.com/priapi/v1/dx/market/v2/search"
STABLES = "USDC / USDT0 / USDG"


def fetch(url: str, proxy: str | None, retries: int = 3) -> dict:
    argv = ["curl", "-sS", "--connect-timeout", "8", "--max-time", "30"]
    if proxy:
        argv += ["--proxy", proxy]
    argv.append(url)
    last: Exception | None = None
    for attempt in range(retries):
        out = subprocess.run(argv, capture_output=True, text=True, timeout=60).stdout
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"unreadable response from {url}: {last}")


def issuer_assets(proxy: str | None, country: str | None) -> list[dict]:
    rows, page = [], 0
    while True:
        payload = fetch(f"{ASSETS_API}?page={page}", proxy)
        rows += payload.get("nodes") or []
        if not (payload.get("page") or {}).get("hasNextPage"):
            break
        page += 1
    if country:
        rows = [r for r in rows
                if ((r.get("underlying") or {}).get("listingCountry") == country)]
    return rows


def xlayer_addresses(asset: dict) -> tuple[str | None, str | None]:
    for dep in asset.get("deployments") or []:
        if (dep.get("network") or "").lower().replace(" ", "") == "xlayer":
            native = (dep.get("address") or "").lower() or None
            wrapper = (dep.get("wrapperAddressV2") or "").lower() or None
            return native, wrapper
    return None, None


def dex_liquidity(symbol: str, native: str | None, wrapper: str | None,
                  proxy: str | None) -> tuple[float, str | None]:
    """Liquidity, plus which address variant actually has a market."""
    try:
        rows = fetch(f"{OKX_SEARCH}?keyword={urllib.parse.quote(symbol)}", proxy).get("data") or []
    except Exception:
        return 0.0, None
    best: tuple[float, str | None] = (0.0, None)
    for row in rows:
        if str(row.get("chainId")) != "196":
            continue
        address = (row.get("tokenContractAddress") or "").lower()
        if address not in {native, wrapper}:
            continue
        liquidity = float(row.get("liquidity") or 0)
        if liquidity > best[0]:
            best = (liquidity, "wrapper" if address == wrapper else "native")
    return best


def cex_perp_names(proxy: str | None) -> set[str]:
    """HK-related names that have a perpetual on Gate/OKX/Binance.

    Reuses the monitor's own discovery so there is one definition of "candidate"
    instead of a second, drifting list.
    """
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "src")}
    argv = [os.environ.get("ARB_PYTHON", "python3"), "-m", "arb_monitor",
            "hk-list", "--json"]
    if proxy:
        argv += ["--proxy", proxy]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=120, env=env)
    try:
        payload = json.loads(result.stdout)
    except Exception:
        return set()
    return {c["asset"] for c in payload.get("candidates", [])}


def gap_report(assets: list[dict], perp_names: set[str]) -> None:
    """CEX perp names with no matching issuer token => no spot leg exists.

    Matching is a name heuristic; treat a hit as "worth a manual look", and a
    miss as "this perp cannot be paired with a tokenized spot right now".
    """
    import re

    def squash(text: str) -> str:
        return re.sub(r"[^a-z0-9]", "", text.lower())

    haystack = [squash(str(a.get("name") or "") + str(a.get("symbol") or ""))
                for a in assets]
    missing = [name for name in sorted(perp_names)
               if not any(squash(name) and squash(name) in entry for entry in haystack)]
    print(f"\nCEX perp candidate names checked: {len(perp_names)}")
    print("no matching issuer token found (perps exist, spot leg does not):")
    for name in missing:
        print(f"  {name}")
    if not missing:
        print("  (none)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--country", default="HK",
                        help="underlying listing country filter (HK or US)")
    parser.add_argument("--liquidity", action="store_true",
                        help="also query on-chain DEX liquidity (one HTTP call per token)")
    parser.add_argument("--perps", action="store_true",
                        help="also run the CEX discovery and report perps with no token")
    parser.add_argument("--proxy", default=os.environ.get("ARB_HTTP_PROXY"))
    args = parser.parse_args()

    # One fetch of the full universe, then filter locally: the "does a token
    # exist" check must look at every country, otherwise US tickers look like
    # they have no token when we are only listing HK.
    all_assets = issuer_assets(args.proxy, None)
    assets = [a for a in all_assets
              if not args.country
              or ((a.get("underlying") or {}).get("listingCountry") == args.country)]
    print(f"issuer assets with listingCountry={args.country}: {len(assets)}")
    print(f"{'HK code':8s} {'symbol':10s} {'name':36s} {'native':44s} {'wrapper':44s}"
          + ("  liquidity  variant" if args.liquidity else ""))
    liquid = []
    for asset in sorted(assets, key=lambda a: str(a.get("underlyingSymbol") or "")):
        native, wrapper = xlayer_addresses(asset)
        line = (f"{str(asset.get('underlyingSymbol','')):8s} {str(asset.get('symbol','')):10s} "
                f"{str(asset.get('name') or '')[:36]:36s} {native or '—':44s} {wrapper or '—':44s}")
        if args.liquidity:
            liquidity, variant = dex_liquidity(str(asset.get("symbol", "")), native, wrapper,
                                               args.proxy)
            line += f"  {liquidity:10.0f}  {variant or '—'}"
            if liquidity > 0:
                liquid.append((asset, native, wrapper, liquidity, variant))
        print(line)
    if args.liquidity:
        print(f"\ntokens with real on-chain liquidity: {len(liquid)} / {len(assets)}")
        print(f"quote legs seen on these pools: {STABLES}")
        print("note: the address below is which OKX DEX entry carries the liquidity;")
        print("      the pool actually read by the monitor is resolved on-chain with")
        print("      factory.getPool(token, stable, fee) and appears in config as 'pools'.")
        for asset, native, wrapper, liquidity, variant in sorted(liquid, key=lambda x: -x[3]):
            address = wrapper if variant == "wrapper" else native
            print(f"  {str(asset.get('underlyingSymbol')):8s} {str(asset.get('symbol')):10s} "
                  f"{liquidity:12.0f}  {variant}: {address}")
    if args.perps:
        perp_names = cex_perp_names(args.proxy)
        if perp_names:
            gap_report(all_assets, perp_names)
        else:
            print("\nCEX discovery returned nothing; run "
                  "'python -m arb_monitor hk-list' to see the error")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
