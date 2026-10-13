"""CLI: re-verify configured token↔share↔contract mappings against live sources."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from arb_monitor.apps.account_dashboard import load_toml, okx_read_env
from arb_monitor.core.mapping_evidence import summarize, verify_token_mapping
from arb_monitor.data.xlayer_onchain import XLayerRPC

_CATALOGUE_CACHE: dict[str, list] = {}


def add_arguments(subparsers) -> None:
    parser = subparsers.add_parser(
        "mapping-verify",
        help="re-read issuer, on-chain and exchange evidence for a configured mapping")
    parser.add_argument("--config", default="config/hk_monitor.toml")
    parser.add_argument("--asset", help="only this asset; default: every configured mapping")
    parser.add_argument("--okx-env", default="config/okx-demo.env")
    parser.add_argument("--json", action="store_true")


def _issuer_asset(symbol: str, proxy: str | None) -> dict | None:
    from arb_monitor.data.xstocks_registry import fetch_json
    try:
        return fetch_json(
            f"https://api.backed.fi/api/v2/public/assets/{symbol}", proxy=proxy, retries=2)
    except Exception:
        return None


def _okx_instrument(inst_id: str, proxy: str | None) -> dict:
    import urllib.request
    url = ("https://www.okx.com/api/v5/public/instruments"
           f"?instType=SWAP&instId={inst_id}")
    request = urllib.request.Request(url, headers={"User-Agent": "arb-monitor/0.1"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy})) if proxy else None
    fetch = opener.open if opener else urllib.request.urlopen
    with fetch(request, timeout=20) as response:
        payload = json.loads(response.read().decode())
    rows = payload.get("data") or []
    if not rows:
        raise SystemExit(f"OKX has no instrument {inst_id}")
    return rows[0]


def _venue_contract(venue: str, inst_id: str, proxy: str | None) -> dict | None:
    """The venue's own instrument row, normalised by the monitored adapter.

    Read through ``CexPublicAdapter`` rather than re-parsing the raw API here,
    so the verifier checks the *same* multiplier the hedge arithmetic will use.
    A second parser would be a second opinion, and this project has already paid
    for that mistake (Bitget's ``sizeMultiplier`` vs ccxt's ``contractSize``).
    """
    from arb_monitor.data.hk_cex import CexPublicAdapter
    # One catalogue fetch per venue per run: 21 mappings would otherwise pull the
    # same exchange directory 7 times each and take minutes.
    if venue not in _CATALOGUE_CACHE:
        _CATALOGUE_CACHE[venue] = CexPublicAdapter(venue, proxy=proxy).instruments()[0]
    contracts = _CATALOGUE_CACHE[venue]
    for contract in contracts:
        if contract.instrument_id == inst_id:
            return {
                "venue": venue,
                "instrument_id": contract.instrument_id,
                "multiplier": str(contract.multiplier),
                "min_contracts": str(contract.min_contracts),
                "min_notional": str(contract.min_notional),
                "tick_size": str(contract.tick_size),
                "active": bool(contract.active),
                "payout_model": contract.payout_model,
                "raw": contract.raw or {},
            }
    return None


def run(args: argparse.Namespace) -> int:
    config = load_toml(args.config)
    proxy = okx_read_env(args.okx_env).get("ARB_HTTP_PROXY")
    rpc_url = (config.get("onchain") or {}).get("rpc_url", "https://rpc.xlayer.tech")
    rpc = XLayerRPC(url=rpc_url, proxy=proxy)
    results = []
    for token in config.get("tokens") or []:
        if args.asset and token.get("asset") != args.asset:
            continue
        mappings = token.get("mappings") or {}
        if not mappings:
            continue
        for contract_key, spec in mappings.items():
            venue, _, inst_id = contract_key.partition(":")
            spec = {**spec, "token_symbol": spec.get("token_symbol") or token.get("symbol")}
            issuer = _issuer_asset(str(spec.get("issuer_symbol") or token.get("symbol")), proxy)
            # Hand the verifier the issuer's own underlying code and the config's
            # HK code, so a bybit mapping is checked against the *issuer's*
            # identity rather than against a number copied into the config.
            spec = {**spec,
                    "hk_code": spec.get("hk_code") or token.get("hk_code"),
                    "issuer_underlying_symbol": ((issuer or {}).get("underlying") or {}).get("symbol")}
            if venue == "okx":
                instrument, venue_contract = _okx_instrument(inst_id, proxy), None
            else:
                instrument = {}
                venue_contract = _venue_contract(venue, inst_id, proxy)
                if venue_contract is None:
                    raise SystemExit(
                        f"{venue} has no instrument {inst_id} "
                        f"(configured for {token.get('asset')})")
            checks = verify_token_mapping(spec=spec, issuer_asset=issuer,
                                          instrument=instrument, rpc=rpc,
                                          venue_contract=venue_contract)
            results.append({"asset": token.get("asset"), "contract": contract_key,
                            **summarize(checks)})
    if not results:
        print("no configured mappings to verify")
        return 1
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for result in results:
            status = "PASS" if result["ok"] else "FAIL"
            print(f'== {result["asset"]} → {result["contract"]}: {status} '
                  f'({result["passed"]}/{len(result["checks"])})')
            for check in result["checks"]:
                mark = "✅" if check["ok"] else "❌"
                print(f'   {mark} {check["name"]}')
                print(f'      {check["detail"]}')
                print(f'      来源: {check["source"]}')
    return 0 if all(result["ok"] for result in results) else 2
