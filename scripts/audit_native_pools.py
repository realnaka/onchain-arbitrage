#!/usr/bin/env python
"""Read-only: does each xStock's **native** address have a pool, or only the wrapper?

Operator request, 2026-09-24: the monitor's discovery re-probes only the wrapper
of an already-seen listing, so "the native has no depth" was never a per-name
fact.  This script re-checks both addresses for every issuer listing and prints
one row per stock: name, native contract, wrapper contract, and each side's
measured best pool depth.

Read-only: ``eth_call``/``eth_blockNumber`` only.  No wallet, no key, no
signing, no transaction.  The only write is the optional JSON report.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/audit_native_pools.py \
        --countries US --proxy http://127.0.0.1:4780 \
        --json-out data/native_pool_audit.json

Runtime note: this shares the single local proxy with the 60 s sampling loop.
It pauses ``--pause-ms`` between listings on purpose; do not lower it to 0 while
the monitor is running.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arb_monitor.apps.account_dashboard import load_toml  # noqa: E402
from arb_monitor.data.xlayer_onchain import (  # noqa: E402
    DEFAULT_RPC_URL, UNISWAP_V3_FACTORY, XLayerRPC,
)
from arb_monitor.data.xstocks_registry import (  # noqa: E402
    DEFAULT_FEES, DEFAULT_STABLES, fetch_issuer_assets,
)
from arb_monitor.ops.native_pool_audit import (  # noqa: E402
    CSV_COLUMNS, audit_row, build_rows, csv_rows, issuer_rows, probe_side,
    render_table, summarize,
)

ROOT = Path(__file__).resolve().parents[1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only native-vs-wrapper pool audit for X Layer xStocks")
    parser.add_argument("--config", default=str(ROOT / "config/hk_monitor.toml"),
                        help="reads [onchain].factory / rpc_url / min_pool_depth_usd")
    parser.add_argument("--countries", default="US,HK",
                        help="issuer listingCountry filter, comma separated; "
                             "'ALL' or '' for every market")
    parser.add_argument("--asset", action="append", default=[],
                        help="only these issuer symbols (e.g. MRNAx); repeatable")
    parser.add_argument("--limit", type=int, default=0,
                        help="probe at most N listings (0 = every match)")
    parser.add_argument("--min-depth-usd", default=None,
                        help="override [onchain].min_pool_depth_usd")
    parser.add_argument("--factory", default=None, help="override the V3 factory")
    parser.add_argument("--rpc-url", default=None, help="override the RPC endpoint")
    parser.add_argument("--proxy", default=os.environ.get("ARB_HTTP_PROXY"))
    parser.add_argument("--issuer-json", default=None,
                        help="read the issuer asset list from a saved JSON file "
                             "instead of api.backed.fi (the file may be the raw "
                             "API payload with 'nodes', or a plain list)")
    parser.add_argument("--from-json", default=None,
                        help="recompute the verdicts/table from a previous run's "
                             "JSON report, with no new chain reads "
                             "(use after changing --min-depth-usd)")
    parser.add_argument("--pause-ms", type=int, default=150,
                        help="sleep between listings so the sampling loop keeps "
                             "its bandwidth (default 150)")
    parser.add_argument("--workers", type=int, default=4,
                        help="concurrent chain reads; the sweep is pure I/O "
                             "(default 4, use 1 to serialize)")
    parser.add_argument("--json-out", default=str(ROOT / "data/native_pool_audit.json"),
                        help="where to write the JSON report")
    parser.add_argument("--csv-out", default=str(ROOT / "data/native_pool_audit.csv"),
                        help="where to write the flat CSV (name, both addresses, "
                             "both prices, verdict)")
    parser.add_argument("--no-csv", action="store_true", help="do not write the CSV")
    parser.add_argument("--no-json", action="store_true", help="do not write a report")
    parser.add_argument("--only-mismatch", action="store_true",
                        help="print only rows where the native side carries depth")
    return parser.parse_args(argv)


def countries_of(raw: str) -> set[str]:
    text = (raw or "").strip()
    if not text or text.upper() == "ALL":
        return set()
    return {part.strip().upper() for part in text.split(",") if part.strip()}


def load_assets(path: str | None, proxy: str | None) -> list[dict]:
    """Issuer universe from a saved file when given, otherwise from the API."""
    if not path:
        print("# 正在读取发行方登记表（api.backed.fi）…", file=sys.stderr)
        try:
            return fetch_issuer_assets(proxy)
        except Exception as exc:  # noqa: BLE001 - a dead network is an exit code,
            print(f"# 发行方登记表读取失败：{type(exc).__name__}: {exc}",
                  file=sys.stderr)  # not a traceback
            raise SystemExit(2)
    payload = json.loads(Path(path).read_text())
    rows = payload.get("nodes") if isinstance(payload, dict) else payload
    return [normalize_asset(row) for row in (rows or [])]


def normalize_asset(asset: dict) -> dict:
    """Accept either the API's raw shape or ``fetch_issuer_assets``' flat shape.

    The raw shape keeps the X Layer addresses inside ``deployments[]``
    (``address`` = native, ``wrapperAddressV2`` = wrapper) — the same two fields
    the live monitor reads, so a saved payload and a live fetch mean the same.
    """
    if "native_address" in asset or "wrapper_address" in asset:
        return asset
    native = wrapper = None
    for dep in asset.get("deployments") or []:
        if (dep.get("network") or "").lower().replace(" ", "") == "xlayer":
            native = (dep.get("address") or "").lower() or None
            wrapper = (dep.get("wrapperAddressV2") or "").lower() or None
            break
    underlying = asset.get("underlying") or {}
    return {
        "symbol": str(asset.get("symbol") or ""),
        "name": str(asset.get("name") or ""),
        "hk_code": str(asset.get("underlyingSymbol") or underlying.get("symbol") or ""),
        "listing_country": str(underlying.get("listingCountry") or "").upper(),
        "native_address": native,
        "wrapper_address": wrapper,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_toml(args.config)
    onchain = config.get("onchain") or {}
    factory = (args.factory or onchain.get("factory") or UNISWAP_V3_FACTORY).lower()
    rpc_url = args.rpc_url or onchain.get("rpc_url") or DEFAULT_RPC_URL
    min_depth_usd = Decimal(str(args.min_depth_usd
                                if args.min_depth_usd is not None
                                else onchain.get("min_pool_depth_usd", "50")))
    countries = countries_of(args.countries)
    wanted_symbols = {symbol.strip().lower() for symbol in args.asset if symbol.strip()}

    if args.from_json:
        saved = json.loads(Path(args.from_json).read_text())
        rows = []
        for record in saved.get("rows") or []:
            probes = {"native": record.get("native_probe") or {},
                      "wrapper": record.get("wrapper_probe") or {}}
            universe_row = {key: record.get(key) for key in
                            ("name", "symbol", "underlying", "market",
                             "native", "wrapper")}
            row = audit_row(universe_row, probes, min_depth_usd)
            row["native_probe"], row["wrapper_probe"] = probes["native"], probes["wrapper"]
            rows.append(row)
        if countries:
            rows = [row for row in rows if row["market"] in countries]
        if wanted_symbols:
            rows = [row for row in rows
                    if row["symbol"].lower() in wanted_symbols
                    or row["underlying"].lower() in wanted_symbols]
        scanned_at_ms = int(saved.get("scanned_at_ms") or 0)
        print(f"# 复用 {saved.get('scanned_at_ms')} 的报告，重算 {len(rows)} 行（无新链上读取）",
              file=sys.stderr)
        return emit(rows, int(saved.get("issuer_assets") or 0), args,
                    factory, rpc_url, min_depth_usd, countries, scanned_at_ms)

    print("# 只读审计：原生 vs wrapper 池子深度", file=sys.stderr)
    print(f"# factory={factory} rpc={rpc_url} 阈值={min_depth_usd} USD "
          f"countries={sorted(countries) or 'ALL'}", file=sys.stderr)
    assets = load_assets(args.issuer_json, args.proxy)
    universe = issuer_rows(assets)
    if countries:
        universe = [row for row in universe if row["market"] in countries]
    if wanted_symbols:
        universe = [row for row in universe
                    if row["symbol"].lower() in wanted_symbols
                    or row["underlying"].lower() in wanted_symbols]
    if args.limit > 0:
        universe = universe[: args.limit]
    if not universe:
        print("no issuer listing matched the filters", file=sys.stderr)
        return 1
    print(f"# 发行方资产 {len(assets)} 个 → 本次要查 {len(universe)} 个 listing",
          file=sys.stderr)

    rpc = XLayerRPC(url=rpc_url, proxy=args.proxy)

    def probe_for(row: dict, side: str) -> dict:
        return probe_side(rpc, factory=factory, base_address=row.get(side),
                          stables=DEFAULT_STABLES, fees=DEFAULT_FEES)

    def progress(index: int, total: int, row: dict) -> None:
        if index % 10 == 0 or index == total:
            print(f"# {index}/{total} {row['symbol']} → {row['verdict']}",
                  file=sys.stderr, flush=True)

    rows = build_rows(universe, probe_for, min_depth_usd, on_progress=progress,
                      workers=max(1, args.workers),
                      pause_s=max(0, args.pause_ms) / 1000)
    shown = rows
    if args.only_mismatch:
        shown = [row for row in rows
                 if row["verdict"] == "native_only" or row["verdict"] == "both"]

    return emit(rows, len(assets), args, factory, rpc_url, min_depth_usd, countries,
                int(time.time() * 1000), shown=shown)


def emit(rows, issuer_assets: int, args, factory, rpc_url, min_depth_usd, countries,
         scanned_at_ms: int, shown=None) -> int:
    print(render_table(shown if shown is not None else rows, min_depth_usd))
    summary = summarize(rows)
    print(f"\n结论分布：{json.dumps(summary['counts'], ensure_ascii=False)}")
    if summary["counts"].get("native_only"):
        print("原生有深度、wrapper 没有的："
              + ", ".join(summary["lists"]["native_only"]))
    if summary["counts"].get("both"):
        print("两侧都有深度的：" + ", ".join(summary["lists"]["both"]))

    if not args.no_json:
        report = {
            "scanned_at_ms": scanned_at_ms,
            "recomputed_at_ms": int(time.time() * 1000),
            "generated_by": "scripts/audit_native_pools.py",
            "read_only": True,
            "factory": factory,
            "rpc_url": rpc_url,
            "stables": DEFAULT_STABLES,
            "fees": list(DEFAULT_FEES),
            "min_depth_usd": str(min_depth_usd),
            "countries": sorted(countries) or "ALL",
            "issuer_assets": issuer_assets,
            "checked": len(rows),
            "summary": summary["counts"],
            "rows": rows,
        }
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"\nJSON 报告已写入 {out}", file=sys.stderr)
    if not args.no_csv:
        out = Path(args.csv_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
            writer.writeheader()
            writer.writerows(csv_rows(rows))
        print(f"CSV 列表已写入 {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
