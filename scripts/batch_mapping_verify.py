"""Re-read issuer, vault and OKX evidence for every depth-watchlist name.

Writes data/mapping_batch.json. Does not edit config and does not trade.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from arb_monitor.core.evm import encode_call
from arb_monitor.core.mapping_evidence import (
    ONE_SHARE_18, decimal_from_word, summarize, verify_token_mapping,
)
from arb_monitor.data.xlayer_onchain import XLayerRPC
from arb_monitor.data.xstocks_registry import fetch_json

ROOT = Path(__file__).resolve().parents[1]
PROXY = "http://127.0.0.1:4780"
ASSETS_API = "https://api.backed.fi/api/v2/public/assets"


def fetch_issuer(proxy: str) -> list[dict]:
    rows, page = [], 0
    while True:
        payload = fetch_json(f"{ASSETS_API}?page={page}", proxy, retries=2)
        rows += payload.get("nodes") or []
        if not (payload.get("page") or {}).get("hasNextPage"):
            break
        page += 1
    out = []
    for asset in rows:
        native = wrapper = None
        for dep in asset.get("deployments") or []:
            if (dep.get("network") or "").lower().replace(" ", "") == "xlayer":
                native = (dep.get("address") or "").lower() or None
                wrapper = (dep.get("wrapperAddressV2") or "").lower() or None
                break
        if not wrapper:
            continue
        underlying = asset.get("underlying") or {}
        out.append({
            "symbol": str(asset.get("symbol") or ""),
            "name": str(asset.get("name") or ""),
            "isin": str(asset.get("isin") or ""),
            "underlying_symbol": str(underlying.get("symbol") or ""),
            "underlying_isin": str(underlying.get("isin") or ""),
            "listing_country": str(underlying.get("listingCountry") or "").upper(),
            "native_address": native,
            "wrapper_address": wrapper,
        })
    return out


def okx_instruments(proxy: str) -> dict[str, dict]:
    payload = fetch_json(
        "https://www.okx.com/api/v5/public/instruments?instType=SWAP", proxy, retries=2)
    return {row["instId"]: row for row in payload.get("data") or [] if row.get("instId")}


def okx_ticker(proxy: str, inst_id: str) -> dict | None:
    try:
        payload = fetch_json(
            f"https://www.okx.com/api/v5/market/ticker?instId={inst_id}", proxy, retries=1)
    except Exception as exc:  # noqa: BLE001
        return {"error": type(exc).__name__ + ":" + str(exc)[:160]}
    rows = payload.get("data") or []
    return rows[0] if rows else {"error": "empty"}


def main() -> int:
    watch = json.loads((ROOT / "data/depth_watchlist.json").read_text())
    issuer = fetch_issuer(PROXY)
    by_wrapper = {row["wrapper_address"]: row for row in issuer}
    instruments = okx_instruments(PROXY)
    rpc = XLayerRPC(proxy=PROXY)
    results = []
    for row in watch["with_depth"]:
        asset = row["asset"]
        if asset == "TER":
            results.append({"asset": asset, "ok": False, "failed": ["duplicate_of_IBM"],
                            "note": "same pool and wrapper as IBM; not a second stock"})
            continue
        wrapper = str(row.get("base_address") or "").lower()
        found = by_wrapper.get(wrapper)
        inst_id = row.get("okx_instrument") or f"{asset}-USDT-SWAP"
        instrument = instruments.get(inst_id) or {}
        if found is None:
            results.append({"asset": asset, "ok": False, "failed": ["issuer_wrapper_miss"],
                            "wrapper": wrapper, "okx_instrument": inst_id,
                            "okx_listed": bool(instrument)})
            continue
        rate_word = None
        try:
            rate_word = rpc.call(wrapper, encode_call("convertToAssets(uint256)", str(ONE_SHARE_18)))
        except Exception as exc:  # noqa: BLE001
            results.append({"asset": asset, "ok": False, "failed": ["rpc"],
                            "error": type(exc).__name__ + ":" + str(exc)[:160]})
            continue
        if not rate_word or rate_word == "0x":
            results.append({"asset": asset, "ok": False, "failed": ["convertToAssets"],
                            "wrapper": wrapper})
            continue
        shares = str(decimal_from_word(rate_word))
        spec = {
            "shares_per_token": shares,
            "native_shares_per_unit": "1",
            "shares_per_perp_unit": "1",
            "issuer_symbol": found["symbol"],
            "token_symbol": found["symbol"],
            "token_isin": found["isin"],
            "underlying_isin": found["underlying_isin"],
            "native_address": found["native_address"],
            "wrapper_address": wrapper,
            "perp_value_currency": asset,
            "hk_code": found["underlying_symbol"],
            "issuer_underlying_symbol": found["underlying_symbol"],
        }
        checks = verify_token_mapping(
            spec=spec, issuer_asset={
                "symbol": found["symbol"], "isin": found["isin"],
                "underlying": {"symbol": found["underlying_symbol"],
                               "isin": found["underlying_isin"]},
            },
            instrument=instrument, rpc=rpc)
        summary = summarize(checks)
        results.append({
            "asset": asset,
            "market": found["listing_country"] or row.get("market"),
            "symbol": found["symbol"],
            "name": found["name"],
            "okx_instrument": inst_id,
            "pool": row.get("pool"),
            "quote_symbol": row.get("quote_symbol"),
            "depth_usd": row.get("depth_usd"),
            "wrapper_address": wrapper,
            "native_address": found["native_address"],
            "token_isin": found["isin"],
            "underlying_isin": found["underlying_isin"],
            "underlying_symbol": found["underlying_symbol"],
            "shares_per_token": shares,
            "ctVal": instrument.get("ctVal"),
            "ctValCcy": instrument.get("ctValCcy"),
            "ctMult": instrument.get("ctMult"),
            **summary,
        })
        print(f"{asset} ok={summary['ok']} failed={summary['failed']} shares={shares}", flush=True)
    peg = {
        "USDC-USDT": okx_ticker(PROXY, "USDC-USDT"),
        "USDG-USDT": okx_ticker(PROXY, "USDG-USDT"),
    }
    out = {"results": results, "peg": peg,
           "passed": [r["asset"] for r in results if r.get("ok")],
           "failed": [r["asset"] for r in results if not r.get("ok")]}
    path = ROOT / "data/mapping_batch.json"
    path.write_text(json.dumps(out, indent=2, default=str))
    print(f"passed={len(out['passed'])} failed={len(out['failed'])} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
