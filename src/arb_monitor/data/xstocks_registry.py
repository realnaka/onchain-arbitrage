"""Issuer-tokenised-stock registry: discover new listings and their pools.

The issuer deploys new xStocks over time (79 HK-listed tokens on X Layer when
this was written, only 8 of them with a market). Rather than re-editing the
config by hand, this module answers two questions on a schedule:

* which tokens are new since the last scan, and
* of those, which already have a Uniswap-V3-style pool with real depth.

Only the second group is worth adding to price monitoring, and it is written to
a machine-managed file so a human edit to the main config is never clobbered.
Everything here is read-only against public endpoints.
"""
from __future__ import annotations

from decimal import Decimal
import json
import re
import subprocess
import time
import urllib.parse

from arb_monitor.core.markets import (
    HK as HK_LISTING, SCANNABLE_MARKETS, SUPPORTED_MARKETS, UNKNOWN as UNKNOWN_MARKET,
    is_supported_market, market_label, normalize_market, validate_scan_countries,
)

ASSETS_API = "https://api.backed.fi/api/v2/public/assets"
# OKX classifies its own instruments: `instCategory` "3" is the tokenised-equity
# series (AAPL-USDT-SWAP, NVDA-USDT-SWAP, …), "4" is commodities, "1" is crypto.
# Measured 2026-09-22: 486 SWAP rows → 181 category 3 (all `state="live"`),
# 8 category 4, 297 category 1.
OKX_SWAP_INSTRUMENTS = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
DEFAULT_STABLES = {
    "USDC": "0xb6ceceab302e2e4948951ee7843fc24e92933061",
    "USDT0": "0x779ded0c9e1022225f8e0630b35a9b54be713736",
    "USDG": "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8",
    "USDT": "0x1e4a5963abfd975d8c9021ce480b42188849d41d",
}
DEFAULT_FEES = (100, 500, 3000, 10000)
V3_GET_POOL = "0x1698ee82"

# The issuer states the listing venue in `underlying.listingCountry`; that is
# the field to trust. Two secondary shapes are recorded here only so that a
# missing country stays visible instead of being silently guessed:
#   * HKEX codes are pure digits, 1–5 of them (4, 966, 1810), while US tickers
#     are alphabetic (AAPL, BRK.B) — 79/79 and 741/741 held on 2026-09-15;
#   * the ISIN prefix is the *issuer's domicile*, not the listing venue — of
#     the 79 HK-listed assets sampled on 2026-09-15 only 23 carry an HK prefix
#     (KY 34, CN 19, BM 2, IT 1), so ISIN must never be used to pick the market.
HK_CODE = re.compile(r"^\d{1,5}$")


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def fetch_json(url: str, proxy: str | None = None, timeout: int = 30,
               retries: int = 2) -> object:
    # Shorter connect timeout than the market adapters: this runs inside the
    # sampling loop, and a slow issuer API must not delay price collection.
    argv = ["curl", "-sS", "--connect-timeout", "5", f"--max-time", str(timeout)]
    if proxy:
        argv += ["--proxy", proxy]
    argv.append(url)
    last: Exception | None = None
    for attempt in range(retries):
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 15).stdout
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            # The issuer API throttles repeated paging; back off and retry.
            # Keep the failure path short: this runs inside the sampling loop,
            # so a long retry chain would stall market data collection.
            last = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"unreadable response from {url}: {last}")


def fetch_issuer_assets(proxy: str | None = None) -> list[dict]:
    """Every issuer asset, with its X Layer native and wrapper addresses."""
    rows, page = [], 0
    while True:
        payload = fetch_json(f"{ASSETS_API}?page={page}", proxy)
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
        if not native:
            continue
        underlying = asset.get("underlying") or {}
        # `listingCountry` is null for a handful of US names; keep that as
        # UNKNOWN rather than inferring a venue from the ISIN.
        country = str(underlying.get("listingCountry") or "").strip().upper()
        out.append({
            "symbol": str(asset.get("symbol") or ""),
            "name": str(asset.get("name") or ""),
            "hk_code": str(asset.get("underlyingSymbol") or ""),
            "listing_country": country or None,
            "market": country or UNKNOWN_MARKET,
            "native_address": native,
            "wrapper_address": wrapper,
        })
    return out


def listing_market(asset: dict) -> str:
    """Listing venue of the underlying stock: "HK", "US", or "UNKNOWN"."""
    country = str(asset.get("listing_country") or asset.get("market") or "").strip().upper()
    return country or UNKNOWN_MARKET


def is_hk_listing(asset: dict) -> bool:
    """True only for a stock the issuer lists in Hong Kong."""
    return listing_market(asset) == HK_LISTING


def hk_shaped_code(asset: dict) -> bool:
    """True when the listing code has the numeric HKEX shape (cross-check only)."""
    return bool(HK_CODE.match(str(asset.get("hk_code") or "").strip()))


def new_tokens(assets: list[dict], known_addresses: set[str]) -> list[dict]:
    """Assets whose native *and* wrapper addresses are both unseen."""
    fresh = []
    for asset in assets:
        addresses = {a for a in (asset["native_address"], asset["wrapper_address"]) if a}
        if addresses & known_addresses:
            continue
        fresh.append(asset)
    return fresh


def match_cex_asset(asset: dict, cex_assets: set[str]) -> str | None:
    """Best-effort name match to a CEX ticker, e.g. "BYD xStock" -> "BYD".

    The issuer names never equal the exchange tickers, so this is a heuristic;
    callers must treat a miss as "no perp leg yet", not as "no such stock".
    """
    tokens = _squash(asset["symbol"]).replace("wrapped", "")
    name = _squash(asset["name"]).replace("xstock", "").replace("wrapped", "")
    for cex in sorted(cex_assets, key=len, reverse=True):
        needle = _squash(cex)
        if len(needle) < 3:
            continue
        if needle in name or needle in tokens:
            return cex
    return None


def fetch_okx_stock_perps(proxy: str | None = None) -> dict[str, dict]:
    """OKX's own list of tokenised-stock perpetuals, keyed by the ticker it is.

    This is the second list of the two the discovery sweep intersects: the
    issuer's token list and the venue's hedgeable list. It is read from the venue
    rather than from this project's configured candidates, because the question
    "can this token be hedged at all?" is the venue's to answer.

    Only ``instCategory == "3"`` is a stock. The other categories are crypto and
    commodity contracts whose tickers can collide with a stock ticker — "QNT",
    "RAM", "PURR" and "FLY" all appear as category-3 names here, so a looser
    filter would gate a stock on a memecoin.
    """
    payload = fetch_json(OKX_SWAP_INSTRUMENTS, proxy)
    out: dict[str, dict] = {}
    for row in (payload or {}).get("data") or []:
        if str(row.get("instCategory") or "") != "3":
            continue
        ticker = str(row.get("ctValCcy") or "").strip().upper()
        if not ticker or not str(row.get("instId") or "").endswith("-USDT-SWAP"):
            continue
        out[ticker] = row
    return out


def okx_hedge_ticker(asset: dict, perps: dict[str, dict]) -> str | None:
    """The OKX perp ticker that would hedge this token, or ``None``.

    The join is the issuer's ``underlyingSymbol`` — the stock's own ticker
    ("AAPL", "NVDA") — against OKX's ``ctValCcy``, the currency one contract is
    denominated in. Measured 2026-09-22 that intersection is **114** of the
    **754** US tokens the issuer lists (+SHEIN as a name-only match, 115), which
    is why discovery is gated on it: a name with no perp can only ever produce a
    row that cannot be hedged, and probing all 754 to learn that costs ~19 h of
    shared-proxy time per sweep.
    """
    code = str(asset.get("hk_code") or "").strip().upper()
    return code if code in perps else None


def discover_pools(rpc, factory: str, base_address: str,
                   stables: dict[str, str] | None = None,
                   fees=DEFAULT_FEES) -> list[dict]:
    """Every pool that exists for this base token, via factory.getPool."""
    stables = stables or DEFAULT_STABLES
    calls, keys = [], []
    for symbol, stable in stables.items():
        for fee in fees:
            data = (V3_GET_POOL + base_address[2:].rjust(64, "0")
                    + stable[2:].rjust(64, "0") + hex(fee)[2:].rjust(64, "0"))
            calls.append(("eth_call", [{"to": factory, "data": data}, "latest"]))
            keys.append((symbol, stable, fee))
    found = []
    results = rpc.batch(calls)
    for key, value in zip(keys, results):
        if isinstance(value, str) and value.startswith("0x") and int(value, 16):
            symbol, stable, fee = key
            found.append({
                "address": "0x" + value[-40:],
                "quote_symbol": symbol,
                "quote_address": stable,
                "fee": fee,
            })
    return found


def probe_pools_cheap(rpc, factory: str, base_address: str,
                      stables: dict[str, str] | None = None) -> list[dict]:
    """Fast re-probe: standard 0.05% tier only.

    Every pool observed on X Layer for these tokens uses the 500 (0.05%) fee
    tier, so a re-probe does not need to ask about the other three. A brand new
    listing still gets the full sweep.
    """
    return discover_pools(rpc, factory, base_address, stables, fees=(500,))


def discovered_token_entry(asset: dict, base_kind: str, base_address: str,
                           pool: dict, depth_usd: Decimal,
                           cex_asset: str | None, now_ms: int,
                           trade_scan: bool = True) -> dict:
    """Config-shaped token entry for a newly tradable listing.

    ``trade_scan`` controls the ``eth_getLogs`` trade-print evidence scan, which
    is the expensive half of the on-chain refresh (measured 2026-09-21: 43-176 s
    for 8 tokens at a 300-block lookback, because this RPC caps ``eth_getLogs``
    at 100 blocks). The caller decides it per market instead of this function
    guessing: a market adopted for screening should not spend the archive RPC on
    prints for a hundred names before one of them is executable.
    """
    return {
        "asset": cex_asset or asset["symbol"],
        "name": asset["name"].replace(" xStock", ""),
        "hk_code": asset["hk_code"],
        # Carried into the config file and the SQLite evidence so a US listing
        # can never again be announced or read back as a Hong Kong one.
        "market": listing_market(asset),
        "symbol": asset["symbol"] if base_kind == "native"
        else f'w{asset["symbol"]}',
        "address": base_address,
        "chain_id": "196",
        "decimals": 18,
        "pool": pool["address"],
        "mapping_verified": False,
        "trade_scan": bool(trade_scan),
        "auto_discovered": True,
        "discovered_at_ms": now_ms,
        "cex_match": cex_asset or "",
        "depth_at_discovery_usd": str(depth_usd),
        "pools": [{
            "address": pool["address"], "base_address": base_address,
            "base_decimals": 18, "quote_address": pool["quote_address"],
            "quote_symbol": pool["quote_symbol"], "quote_decimals": 6,
            "fee": pool["fee"],
        }],
    }


def render_discovered_toml(tokens: list[dict]) -> str:
    """Machine-managed file; hand edits belong in hk_monitor.toml instead."""
    lines = [
        "# Auto-generated by arb_monitor token-universe discovery.",
        "# New xStocks listings that already have an X Layer pool with depth.",
        "# Safe to delete: entries are the only thing lost.",
        "",
    ]
    for token in tokens:
        pool = token["pools"][0]
        lines += [
            "[[tokens]]",
            f'asset = "{token["asset"]}"',
            f'name = "{token["name"]}"',
            f'hk_code = "{token["hk_code"]}"',
            f'market = "{token.get("market") or UNKNOWN_MARKET}"',
            f'symbol = "{token["symbol"]}"',
            f'address = "{token["address"]}"',
            'chain_id = "196"',
            "decimals = 18",
            f'pool = "{token["pool"]}"',
            "mapping_verified = false",
            # Round-tripped rather than hard-coded: the writer used to force
            # ``true``, so a market adopted with the print scan off would come
            # back on after the next restart with no record of the change.
            f'trade_scan = {"true" if token.get("trade_scan", True) else "false"}',
            "auto_discovered = true",
            f'discovered_at_ms = {int(token["discovered_at_ms"])}',
            f'cex_match = "{token["cex_match"]}"',
            f'depth_at_discovery_usd = "{token["depth_at_discovery_usd"]}"',
            "pools = [",
            f'  {{ address = "{pool["address"]}", base_address = "{pool["base_address"]}", '
            f'base_decimals = 18, quote_address = "{pool["quote_address"]}", '
            f'quote_symbol = "{pool["quote_symbol"]}", quote_decimals = 6, '
            f'fee = {pool["fee"]} }},',
            "]",
            "",
        ]
    return "\n".join(lines)
