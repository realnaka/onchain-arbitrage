#!/usr/bin/env python3
"""Build the xStocks route matrix: buy chain -> Kraken exit, and funding back.

Read-only. It answers, per (chain, symbol):

* can the token be deposited directly into Kraken from this chain?
* if not, does the issuer bridge support this chain -> Solana?
* can Kraken withdraw this token back to this chain?
* is the Kraken withdrawal address already in the address book?
* what gas / stable balance does our wallet hold on this chain?

The v4 watch reads the JSON this script writes. This script never withdraws,
deposits, signs, or submits a transaction.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import xstocks_chain_census as census  # noqa: E402
from arb_monitor.venues.kraken_spot import (  # noqa: E402
    KrakenError, KrakenSpotClient, load_credentials)

BOOK = ROOT / "data" / "xstocks_book.sqlite"
WATCH = ROOT / "data" / "xstocks_v4_watch.json"
EXIT_CACHE = ROOT / "data" / "kraken_exit_cache.json"
OUT = ROOT / "data" / "xstocks_route_matrix.json"
PROXY = "http://127.0.0.1:4780"
EVM_WALLET = "0xabababababababababababababababababababab"
SOLANA_WALLET = "AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev"

NETWORK_TO_CHAIN = {
    "Ethereum": "eth",
    "BinanceSmartChain": "bsc",
    "BNB Smart Chain": "bsc",
    "Arbitrum": "arbitrum",
    "Optimism": "optimism",
    "Monad": "monad",
    "HyperEVM": "hyperevm",
    "XLayer": "x-layer",
    "Ink": "ink",
    "Mantle": "mantle",
    "Solana": "solana",
    "Ton": "ton",
}

# Kraken DepositMethods / WithdrawMethods name fragments.
KRAKEN_CHAIN_LABELS = {
    "eth": ["ethereum"],
    "bsc": ["binance", "bnb"],
    "arbitrum": ["arbitrum"],
    "optimism": ["optimism"],
    "monad": ["monad"],
    "hyperevm": ["hyperliquid", "hyperevm"],
    "x-layer": ["xlayer", "x layer"],
    "solana": ["solana"],
    "ton": ["ton", "the open network"],
    "ink": ["ink"],
}

FUNDING_ASSETS = {
    "eth": ["USDC", "USDT"],
    "bsc": ["USDT"],
    "arbitrum": ["USDC", "USDT"],
    "optimism": ["USDC", "USDT"],
    "monad": [],
    "hyperevm": ["USDT"],
    "x-layer": ["USDG", "USDT"],
    "ink": ["USDC", "USDT"],
    "solana": ["USDT", "USDC"],
}

# EVM chain config: RPC + native gas + stablecoins.
CHAINS = {
    "eth": {
        "network": "Ethereum",
        "rpc": "https://ethereum-rpc.publicnode.com",
        "gas": "ETH",
        "stables": {
            "USDC": ("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6),
            "USDT": ("0xdac17f958d2ee523a2206206994597c13d831ec7", 6),
        },
    },
    "bsc": {
        "network": "BNB Smart Chain",
        "rpc": "https://bsc-dataseed.binance.org",
        "gas": "BNB",
        "stables": {
            "USDC": ("0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d", 18),
            "USDT": ("0x55d398326f99059ff775485246999027b3197955", 18),
        },
    },
    "arbitrum": {
        "network": "Arbitrum",
        "rpc": "https://arb1.arbitrum.io/rpc",
        "gas": "ETH",
        "stables": {
            "USDC": ("0xaf88d065e77c8cc2239327c5edb3a432268e5831", 6),
            "USDT": ("0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9", 6),
        },
    },
    "optimism": {
        "network": "Optimism",
        "rpc": "https://optimism-rpc.publicnode.com",
        "gas": "ETH",
        "stables": {
            "USDC": ("0x0b2c639c533813f4aa9d7837caf62653d097ff85", 6),
            "USDT": ("0x94b008aa00579c1307b0ef2c499ad98a8ce58e58", 6),
        },
    },
    "monad": {
        "network": "Monad",
        "rpc": "https://rpc.monad.xyz",
        "gas": "MON",
        "stables": {
            "USDC": ("0x754704bc059f8c67012fed69bc8a327a5aafb603", 6),
        },
    },
    "hyperevm": {
        "network": "HyperEVM",
        "rpc": "https://rpc.hyperliquid.xyz/evm",
        "gas": "HYPE",
        "stables": {},
    },
    "x-layer": {
        "network": "XLayer",
        "rpc": "https://rpc.xlayer.tech",
        "gas": "OKB",
        "stables": {
            "USDC": ("0xb6ceceab302e2e4948951ee7843fc24e92933061", 6),
            "USDG": ("0x4ae46a509f6b1d9056937ba4500cb143933d2dc8", 6),
        },
    },
    "ink": {
        "network": "Ink",
        "rpc": "https://rpc-gel.inkonchain.com",
        "gas": "ETH",
        "stables": {
            "USDG": ("0xe343167631d89b6ffc58b88d6b7fb0228795491d", 6),
            "USDC": ("0x2d270e6886d130d724215a266106e6832161eaed", 6),
        },
    },
}


def _decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))


def http_json(url: str, data: bytes | None = None, timeout: int = 30) -> object:
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "xstocks-route-matrix/0.1"})
    with _opener().open(req, timeout=timeout) as response:
        return json.loads(response.read())


def rpc_batch(url: str, calls: list[dict]) -> dict[str, str]:
    if not calls:
        return {}
    payload = [{"jsonrpc": "2.0", "id": item["id"], "method": item["method"],
                "params": item["params"]} for item in calls]
    body = json.dumps(payload).encode()
    rows = http_json(url, body, timeout=40)
    out: dict[str, str] = {}
    if not isinstance(rows, list):
        return out
    for row in rows:
        if row.get("result") is not None:
            out[str(row.get("id"))] = row["result"]
    return out


def load_watch_scope() -> list[tuple[str, str]]:
    try:
        payload = json.loads(WATCH.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    out: list[tuple[str, str]] = []
    for row in payload.get("rows") or []:
        chain = str(row.get("chain") or "")
        symbol = str(row.get("symbol") or "")
        if chain and symbol and (chain, symbol) not in out:
            out.append((chain, symbol))
    return out


def load_bridge_index() -> dict[tuple[str, str], bool]:
    """(chain_key, symbol) -> issuer bridge supports chain -> Solana."""
    out: dict[tuple[str, str], bool] = {}
    conn = sqlite3.connect(f"file:{BOOK}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "select source_network, destinations_json, products_json from bridges "
            "where missing_since_ms is null")
        for source, destinations, products in rows:
            chain = NETWORK_TO_CHAIN.get(str(source))
            if not chain:
                continue
            try:
                dests = {str(x) for x in (json.loads(destinations) or [])}
                product_rows = json.loads(products) or []
            except (json.JSONDecodeError, TypeError):
                continue
            has_solana_dest = any("solana" in x.lower() for x in dests)
            for product in product_rows:
                symbol = str(product.get("symbol") or "")
                if not symbol:
                    continue
                networks = {
                    str((d or {}).get("network") or "")
                    for d in (product.get("deployments") or [])
                }
                supports = has_solana_dest and str(source) in networks and "Solana" in networks
                out[(chain, symbol)] = out.get((chain, symbol), False) or supports
    finally:
        conn.close()
    return out


def direct_deposit(symbol: str, chain: str, deposits: dict) -> dict | None:
    labels = KRAKEN_CHAIN_LABELS.get(chain) or []
    if not labels:
        return None
    for row in deposits.get(symbol) or []:
        method = str(row.get("method") or "").lower()
        if any(label in method for label in labels):
            return row
    return None


def address_ready(symbol: str, chain: str, withdraw: dict | None,
                  addresses: list[dict]) -> bool:
    if not withdraw:
        return False
    labels = KRAKEN_CHAIN_LABELS.get(chain) or []
    method = str(withdraw.get("method") or "").lower()
    for row in addresses:
        if str(row.get("asset") or "") != symbol:
            continue
        row_method = str(row.get("method") or "").lower()
        if row_method == method:
            return True
        if any(label in row_method for label in labels) and any(
                label in method for label in labels):
            return True
    return False


def funding_for(chain: str, stable_methods: dict[str, list[dict]],
                addresses: list[dict]) -> dict:
    """How to send stablecoin from Kraken to this chain."""
    labels = KRAKEN_CHAIN_LABELS.get(chain) or []
    fallback: dict = {}
    for asset in FUNDING_ASSETS.get(chain) or []:
        for row in stable_methods.get(asset) or []:
            method = str(row.get("method") or "").lower()
            network = str(row.get("network") or "").lower()
            if not any(label in method or label in network for label in labels):
                continue
            fee = _decimal((row.get("fee") or {}).get("fee"))
            item = {
                "asset": asset,
                "method": row.get("method"),
                "network": row.get("network"),
                "minimum": row.get("minimum"),
                "fee_units": None if fee is None else format(fee, "f"),
                "address_book_ready": address_ready(asset, chain, row, addresses),
                "required_address": EVM_WALLET if chain != "solana" else SOLANA_WALLET,
            }
            if item["address_book_ready"]:
                return item
            if not fallback:
                fallback = item
    return fallback


def chain_inventory() -> dict:
    out: dict[str, dict] = {}
    for key, cfg in CHAINS.items():
        calls = [{"id": "gas", "method": "eth_getBalance",
                  "params": [EVM_WALLET, "latest"]}]
        for symbol, (address, _decimals) in cfg["stables"].items():
            data = "0x70a08231" + "0" * 24 + EVM_WALLET[2:].lower()
            calls.append({"id": symbol, "method": "eth_call",
                          "params": [{"to": address, "data": data}, "latest"]})
        try:
            rows = rpc_batch(cfg["rpc"], calls)
        except Exception as exc:  # noqa: BLE001
            out[key] = {"network": cfg["network"], "error": type(exc).__name__}
            continue
        gas_raw = rows.get("gas")
        gas = Decimal(int(gas_raw, 16)) / (Decimal(10) ** 18) if gas_raw else None
        stables: dict[str, str] = {}
        for symbol, (_address, decimals) in cfg["stables"].items():
            raw = rows.get(symbol)
            if raw:
                stables[symbol] = format(Decimal(int(raw, 16)) / (Decimal(10) ** decimals), "f")
        out[key] = {
            "network": cfg["network"],
            "gas_symbol": cfg["gas"],
            "gas": None if gas is None else format(gas, "f"),
            "stables": stables,
        }
    try:
        body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "getBalance",
            "params": [SOLANA_WALLET],
        }).encode()
        row = http_json("https://api.mainnet-beta.solana.com", body)
        lamports = (row or {}).get("result", {}).get("value")
        out["solana"] = {
            "network": "Solana",
            "gas_symbol": "SOL",
            "gas": None if lamports is None else format(Decimal(lamports) / Decimal(10 ** 9), "f"),
            "stables": {},
            "note": "SPL USDT/USDC not read here; the live board owns Solana inventory.",
        }
    except Exception as exc:  # noqa: BLE001
        out["solana"] = {"network": "Solana", "error": type(exc).__name__}
    return out


def build(private: bool = True) -> dict:
    scope = load_watch_scope()
    bridge = load_bridge_index()
    try:
        exit_cache = json.loads(EXIT_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        exit_cache = {}
    deposits = exit_cache.get("deposits") or {}
    withdraw_methods: dict[str, list[dict]] = {}
    stable_methods: dict[str, list[dict]] = {}
    addresses: list[dict] = []
    private_error = ""
    if private:
        try:
            key, secret = load_credentials(ROOT / "config" / "kraken.env")
            client = KrakenSpotClient(key, secret, proxy=PROXY)
            for _chain, symbol in scope:
                if symbol in withdraw_methods:
                    continue
                withdraw_methods[symbol] = client._post(
                    "/0/private/WithdrawMethods",
                    {"asset": symbol, "aclass": "tokenized_asset"}) or []
            for asset in sorted({a for rows in FUNDING_ASSETS.values() for a in rows}):
                aclass = "currency"
                try:
                    stable_methods[asset] = client._post(
                        "/0/private/WithdrawMethods",
                        {"asset": asset, "aclass": aclass}) or []
                except KrakenError:
                    stable_methods[asset] = []
            addresses = client._post("/0/private/WithdrawAddresses", {}) or []
        except (KrakenError, OSError, RuntimeError) as exc:
            private_error = f"{type(exc).__name__}: {exc}"

    routes: list[dict] = []
    for chain, symbol in scope:
        deposit = direct_deposit(symbol, chain, deposits)
        withdraw_rows = withdraw_methods.get(symbol) or []
        withdraw = None
        labels = KRAKEN_CHAIN_LABELS.get(chain) or []
        for row in withdraw_rows:
            method = str(row.get("method") or "").lower()
            network = str(row.get("network") or "").lower()
            if any(label in method or label in network for label in labels):
                withdraw = row
                break
        ready = address_ready(symbol, chain, withdraw, addresses)
        can_bridge = bool(bridge.get((chain, symbol)))
        route = "direct_deposit" if deposit else ("bridge_to_solana" if can_bridge else "no_exit")
        withdraw_fee = None
        if withdraw:
            withdraw_fee = _decimal((withdraw.get("fee") or {}).get("fee"))
        routes.append({
            "chain": chain,
            "symbol": symbol,
            "exit_route": route,
            "direct_deposit": bool(deposit),
            "deposit_method": (deposit or {}).get("method") if deposit else None,
            "deposit_minimum": (deposit or {}).get("minimum") if deposit else None,
            "bridge_to_solana_supported": can_bridge,
            "bridge_verified": chain == "x-layer",  # only X Layer MRNAx has a real sample
            "withdraw_method": (withdraw or {}).get("method") if withdraw else None,
            "withdraw_network": (withdraw or {}).get("network") if withdraw else None,
            "withdraw_minimum": (withdraw or {}).get("minimum") if withdraw else None,
            "withdraw_fee_units": None if withdraw_fee is None else format(withdraw_fee, "f"),
            "address_book_ready": ready,
            "required_address": EVM_WALLET if chain != "solana" else SOLANA_WALLET,
            "note": (
                "bridge to Solana is supported by issuer registration, but only X Layer MRNAx "
                "has a live bridge sample; treat other chains as untested"
                if can_bridge and chain != "x-layer" else ""
            ),
        })
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "generated_ms": int(time.time() * 1000),
        "wallet": {"evm": EVM_WALLET, "solana": SOLANA_WALLET},
        "private_read_error": private_error,
        "address_book": [
            {"key": row.get("key"), "asset": row.get("asset"), "method": row.get("method")}
            for row in addresses
        ],
        "chains": chain_inventory(),
        "funding": {
            chain: funding_for(chain, stable_methods, addresses)
            for chain in CHAINS
        },
        "routes": routes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--no-private", action="store_true",
                        help="skip Kraken private withdrawal/address-book reads")
    args = parser.parse_args(argv)
    payload = build(private=not args.no_private)
    tmp = Path(args.out).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    tmp.replace(Path(args.out))
    print(f"wrote {args.out} routes={len(payload['routes'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
