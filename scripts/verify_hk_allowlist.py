#!/usr/bin/env python3
"""Verify the HK token↔stock↔contract chain for every monitored name.

Read-only. For each monitored token it prints:

  * the issuer's own identifiers (token ISIN → underlying HK code + ISIN),
  * every perpetual the monitor has actually seen quotes for,
  * whether that contract's underlying is the same stock, and
  * how the contract is quoted (USD-linear needs the FX snapshot; HKD-quoted
    must NOT be converted, or the comparison double-counts the rate).

Usage:
    PYTHONPATH=src .venv/bin/python scripts/verify_hk_allowlist.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arb_monitor.apps.account_dashboard import load_toml  # noqa: E402
from arb_monitor.core.fx_store import FxStore  # noqa: E402


def issuer_lookup(proxy: str | None, symbols: list[str]) -> dict:
    """Per-asset issuer records.

    The bulk registry normalises rows down to addresses, dropping the ISINs —
    and the ISIN chain is exactly what proves the token is this stock. The
    per-asset endpoint keeps them.
    """
    from arb_monitor.data.xstocks_registry import fetch_json
    out = {}
    for symbol in symbols:
        for candidate in (symbol, symbol.lstrip("wW"), symbol.lstrip("wW").upper()):
            try:
                record = fetch_json(
                    f"https://api.backed.fi/api/v2/public/assets/{candidate}",
                    proxy=proxy, retries=2)
            except Exception:
                continue
            if isinstance(record, dict) and record.get("underlying"):
                out[candidate] = record
                break
    return out


def latest_references(db: str) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT payload_json FROM monitor_events WHERE kind='run_summary' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return {}
    summary = json.loads(row["payload_json"])
    return {r["asset"]: r for r in summary.get("token_references") or []}


def underlying_of_contract(venue: str, instrument: str) -> str | None:
    """The HK code a contract settles against, from its own naming/metadata."""
    name = instrument.upper()
    if venue == "binance" and name.startswith("HK"):
        return name[2:].split("USDT")[0].lstrip("0") or None
    return None


def find_issuer(issuers: dict, token: dict) -> dict:
    """The issuer's record for a config token like ``wXIAOx`` → ``XIAOx``.

    The config names the wrapper; the issuer names the native token. Both are
    the same asset, and the HK code is the tie-breaker if the symbol changed.
    """
    symbol = str(token.get("symbol") or "")
    for key in (symbol, symbol.lstrip("wW"), symbol.lstrip("wW").upper()):
        if key in issuers:
            return issuers[key]
    code = str(token.get("hk_code") or "").lstrip("0")
    if code:
        for record in issuers.values():
            if str(record.get("underlyingSymbol") or "").lstrip("0") == code:
                return record
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/hk_monitor.toml")
    parser.add_argument("--db", default="data/hk_monitor.db")
    parser.add_argument("--fx-db", default="data/fx.sqlite")
    parser.add_argument("--proxy", default="http://127.0.0.1:4780")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    config = load_toml(args.config)
    monitored = {t["asset"]: t for t in config.get("tokens") or []}
    if (config.get("monitor") or {}).get("assets"):
        monitored = {a: t for a, t in monitored.items()
                     if a in config["monitor"]["assets"] + list(monitored)}
    references = latest_references(args.db)
    issuers = issuer_lookup(args.proxy, [t["symbol"] for t in monitored.values()])
    fx = FxStore(args.fx_db)
    rate = fx.latest("USD/HKD")

    rows = []
    for asset, token in sorted(monitored.items()):
        if not token.get("pools"):
            continue
        issuer = find_issuer(issuers, token)
        underlying = issuer.get("underlying") or {}
        reference = references.get(asset) or {}
        contracts = []
        for quote in reference.get("perp_quotes") or []:
            instrument = quote.get("instrument") or ""
            venue = quote.get("venue") or ""
            venue_underlying = underlying_of_contract(venue, instrument)
            same_stock = None
            if venue_underlying is not None:
                same_stock = venue_underlying == str(
                    issuer.get("underlyingSymbol", "")).lstrip("0")
            contracts.append({
                "venue": venue, "instrument": instrument,
                "payout_model": quote.get("payout_model"),
                "bid": quote.get("bid"), "ask": quote.get("ask"),
                "fresh": quote.get("fresh"),
                "venue_underlying": venue_underlying,
                "same_stock": same_stock,
            })
        rows.append({
            "asset": asset,
            "token_symbol": token["symbol"],
            "token_address": token["address"],
            "hk_code": token.get("hk_code"),
            "issuer_symbol": issuer.get("symbol"),
            "issuer_token_isin": issuer.get("isin"),
            "underlying_symbol": issuer.get("underlyingSymbol"),
            "underlying_isin": underlying.get("isin"),
            "listing_country": underlying.get("listingCountry"),
            "contracts": contracts,
        })

    report = {"usd_hkd": str(rate.rate) if rate else None,
              "usd_hkd_source": rate.source if rate else None,
              "assets": rows}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(f'USD/HKD 本地快照: {report["usd_hkd"]}（{report["usd_hkd_source"]}）')
    print(f'{"标的":10} {"代币":10} {"HK代码":6} {"发行方 ISIN":14} {"正股 ISIN":14} 合约')
    for row in rows:
        print(f'{row["asset"]:10} {str(row["token_symbol"]):10} {str(row["hk_code"]):6} '
              f'{str(row["issuer_token_isin"]):14} {str(row["underlying_isin"]):14} '
              f'{len(row["contracts"])} 个')
        for contract in row["contracts"]:
            mark = {True: "同股✓", False: "❌不一致", None: "未判定"}[contract["same_stock"]]
            quote_kind = {"usd_linear": "USD 计价（需按 FX 折 HKD）",
                          "hkd_quanto": "HKD 计价（不要再折算）"}.get(
                              contract["payout_model"], str(contract["payout_model"]))
            print(f'            {contract["venue"]}:{contract["instrument"]:20} '
                  f'{quote_kind:26} {mark}  bid/ask={contract["bid"]}/{contract["ask"]}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
