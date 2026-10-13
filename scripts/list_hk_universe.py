#!/usr/bin/env python
"""Every HK xStock that could actually be arbitraged, and which venues quote it.

Operator's request, 2026-09-17: "我需要的是全部可以套利的港股币对和合约".

`scripts/list_hk_xstocks.py` answers "what does the issuer list" by asking OKX's
DEX search endpoint once per token, which is slow enough that it did not finish in
20 minutes on 2026-09-17. This script asks the same question the monitor itself
trusts: the issuer's own asset list for the identifiers, then `factory.getPool`
on X Layer for existence and a real on-chain balance read for depth. Both legs of
the question are answered from primary sources:

    spot leg   issuer API (ISINs, X Layer native + wrapper addresses)
               -> on-chain V3 factory.getPool -> pool depth in USD
    hedge leg  the exchange's own instrument catalogue, via the same
               CexPublicAdapter the monitor uses

A pair is *arbitrageable* only when both exist. Everything else in the issuer's
list is a token with no market, or a perp with no token.

Read-only. Usage:

    PYTHONPATH=src .venv/bin/python scripts/list_hk_universe.py --json out.json
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arb_monitor.data.hk_cex import CexPublicAdapter  # noqa: E402
from arb_monitor.data.xlayer_onchain import XLayerOnchainAdapter, XLayerRPC  # noqa: E402
from arb_monitor.data.xstocks_registry import (DEFAULT_STABLES, discover_pools,  # noqa: E402
                                               fetch_issuer_assets, is_hk_listing,
                                               match_cex_asset)

VENUES = ("gate", "okx", "binance", "bybit", "bitget")
# Only the 0.05% tier: every X Layer xStocks pool observed to date uses fee=500.
# A different tier would be found by the monitor's own full sweep; for a
# universe listing the cheap probe is the right trade-off.
FEES = (500,)


def cex_catalogue(proxy: str | None) -> dict[str, dict[str, str]]:
    """venue -> {asset ticker: instrument id}, USD-linear only.

    The HKD-quoted siblings (gate `XIAOMIHKD_USDT`, binance `HK1810USDT`) carry
    the *same* asset name, so filtering has to happen on the venue's own pay-out
    model, not on the ticker: subtracting an HKD-quoted perp from a USD token
    price is the mistake `docs/research/hk-basis-2026-09-12.md` already made
    once (a free-looking FX gap that is really the exchange rate).
    """
    out: dict[str, dict[str, str]] = {}
    for venue in VENUES:
        try:
            contracts, _raw, _ms = CexPublicAdapter(venue, proxy=proxy).instruments()
        except Exception as exc:  # a venue being down must not hide the others
            print(f"  ! {venue}: {type(exc).__name__}: {str(exc)[:100]}", file=sys.stderr)
            out[venue] = {}
            continue
        mapping: dict[str, str] = {}
        for contract in contracts:
            if str(getattr(contract, "payout_model", "")).lower() != "usd_linear":
                continue
            # First instrument wins: same ticker on the same venue is one market.
            mapping.setdefault(str(contract.asset), contract.instrument_id)
        out[venue] = mapping
    return out


def onchain_candidates(proxy: str | None, factory: str, rpc_url: str,
                       min_depth: Decimal) -> tuple[list[dict], list[dict]]:
    """Issuer HK tokens that actually have a pool, with depth when it is real."""
    assets = [a for a in fetch_issuer_assets(proxy) if is_hk_listing(a)]
    rpc = XLayerRPC(rpc_url, proxy=proxy)

    tokens: list[dict] = []
    seen_pools: set[str] = set()
    for asset in assets:
        for kind, address in (("native", asset["native_address"]),
                              ("wrapper", asset["wrapper_address"])):
            if not address:
                continue
            pools = discover_pools(rpc, factory, address, DEFAULT_STABLES, fees=FEES)
            for pool in pools:
                # One pool is one market: native/wrapper sweeps can both return
                # it, and a duplicate row would double every depth total.
                if pool["address"].lower() in seen_pools:
                    continue
                seen_pools.add(pool["address"].lower())
                tokens.append({
                    "asset": asset["symbol"],
                    "name": asset["name"],
                    "hk_code": asset["hk_code"],
                    "base_kind": kind,
                    "address": address,
                    "pool": pool["address"],
                    "quote_symbol": pool["quote_symbol"],
                    "quote_address": pool["quote_address"],
                    "fee": pool["fee"],
                    "base_decimals": 18,
                    "quote_decimals": 6,
                    "source_asset": asset,
                    "pools": [{
                        "address": pool["address"], "base_address": address,
                        "base_decimals": 18, "quote_address": pool["quote_address"],
                        "quote_symbol": pool["quote_symbol"], "quote_decimals": 6,
                        "fee": pool["fee"],
                    }],
                })
    if not tokens:
        return [], assets
    adapter = XLayerOnchainAdapter({"min_pool_depth_usd": str(min_depth)},
                                   tokens, rpc)
    for row in adapter.pool_states():
        for token in tokens:
            if token["pool"] == row.get("address"):
                token["price_usd"] = (None if row.get("price_usd") is None
                                      else str(row["price_usd"]))
                token["depth_usd"] = (None if row.get("depth_usd") is None
                                      else str(row["depth_usd"]))
                token["pool_error"] = row.get("error")
                break
    return tokens, assets


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", default=os.environ.get("ARB_HTTP_PROXY"))
    parser.add_argument("--factory", default="0x4b2ab38dbf28d31d467aa8993f6c2585981d6804")
    parser.add_argument("--rpc", default="https://rpc.xlayer.tech")
    parser.add_argument("--min-depth-usd", default="1000")
    parser.add_argument("--json", help="also write the raw rows here")
    args = parser.parse_args()
    min_depth = Decimal(args.min_depth_usd)

    print("CEX 合约目录（交易所自己的字段）…", file=sys.stderr)
    catalogue = cex_catalogue(args.proxy)
    print("X Layer 池子（issuer 地址 + factory.getPool + 链上余额）…", file=sys.stderr)
    tokens, assets = onchain_candidates(args.proxy, args.factory, args.rpc, min_depth)

    rows = []
    for token in tokens:
        depth = token.get("depth_usd")
        depth = None if depth is None else Decimal(depth)
        venues = {}
        for venue, mapping in catalogue.items():
            match = match_cex_asset(token["source_asset"], set(mapping))
            if match:
                venues[venue] = mapping.get(match, match)
        rows.append({
            "hk_code": token["hk_code"],
            "symbol": token["asset"],
            "name": token["name"],
            "base_kind": token["base_kind"],
            "pool": token["pool"],
            "quote": token["quote_symbol"],
            "depth_usd": None if depth is None else str(depth),
            "deep_enough": bool(depth is not None and depth >= min_depth),
            "venues": venues,
            "pool_error": token.get("pool_error"),
        })

    rows.sort(key=lambda r: (not r["deep_enough"], -(Decimal(r["depth_usd"]) if r["depth_usd"] else Decimal(0))))
    both = [r for r in rows if r["deep_enough"] and r["venues"]]
    print(f"issuer HK 资产: {len(assets)}；发现池子: {len(rows)}；"
          f"池子够深: {sum(1 for r in rows if r['deep_enough'])}；"
          f"两腿都在（可套利）: {len(both)}")
    print()
    print(f"{'HK代码':<8}{'代币':<12}{'池深(USD)':>14}  {'计价':<7}两腿齐全的场所")
    for row in rows:
        if not row["deep_enough"] and not row["venues"]:
            continue
        depth = f"{Decimal(row['depth_usd']):,.0f}" if row["depth_usd"] else "—"
        venues = " ".join(f"{v}:{i}" for v, i in sorted(row["venues"].items())) or "—"
        print(f"{row['hk_code']:<8}{row['symbol']:<12}{depth:>14}  "
              f"{row['quote']:<7}{venues}")
    print()
    print(f"只有池子、没有任何我们所监控的永续（不能套利）："
          f"{sum(1 for r in rows if r['deep_enough'] and not r['venues'])} 个")
    for row in rows:
        if row["deep_enough"] and not row["venues"]:
            print(f"  {row['hk_code']:<8}{row['symbol']:<12}"
                  f"{Decimal(row['depth_usd']):>14,.0f}")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"rows": rows, "issuer_hk_assets": len(assets),
             "cex_catalogue": {k: len(v) for k, v in catalogue.items()}},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nwritten: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
