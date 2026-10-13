#!/usr/bin/env python3
"""Read-only wallet checks used while walking bridge edges. Never signs.

    scripts/xstocks_v2_wallet_peek.py evm eth               # native, SPYx, USDC, wrapper shares
    scripts/xstocks_v2_wallet_peek.py evm arbitrum
    scripts/xstocks_v2_wallet_peek.py sol                   # SOL, USDT, USDC, SPYx
    scripts/xstocks_v2_wallet_peek.py solsig <signature>    # did this hash ever land?
    scripts/xstocks_v2_wallet_peek.py receipt eth <txhash>  # status, SPYx transfers, CCIP message id
    scripts/xstocks_v2_wallet_peek.py ccip <message id>     # CCIP explorer state (2 = executed)
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.xstocks_v2.feeds import evm as evm_feed            # noqa: E402
from arb_monitor.xstocks_v2.feeds.http import Http                  # noqa: E402

EVM_WALLET = "0xabababababababababababababababababababab"
SOL_WALLET = "AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev"
SPYX = "0x90a2a4c76b5d8c0bc892a69ea28aa775a8f2dd48"
SPYX_SOLANA = "XsoCS1TfEyfFhfvj8EtZ528L3CaKBDBRqRapnBbDF2W"
WRAPPER_ETH = "0xe7e553cd128f0011777323a0b44a7b96ea1cb540"
USDC = {"eth": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"}
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
SOL_MINTS = {"USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
             "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
             "SPYx": SPYX_SOLANA}
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
BRIDGE = "0x9ec0e4a4c411493773e01e2abf4d42395788846b"


def post(http, url: str, method: str, params: list):
    status, raw = http.request(url, data=json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers={"Content-Type": "application/json"})
    if status != 200:
        raise RuntimeError(f"http_{status}")
    payload = json.loads(raw.decode("utf-8", "replace"))
    if payload.get("error"):
        raise RuntimeError(str(payload["error"])[:200])
    return payload.get("result")


def erc20(http, chain: str, token: str, owner: str = EVM_WALLET) -> int:
    rpc = evm_feed.CHAINS[chain].rpc
    return int(post(http, rpc, "eth_call", [
        {"to": token, "data": "0x70a08231" + "0" * 24 + owner[2:].lower()}, "latest"]), 16)


def evm(http, chain: str) -> dict:
    rpc = evm_feed.CHAINS[chain].rpc
    out = {"native": str(Decimal(int(post(http, rpc, "eth_getBalance", [EVM_WALLET, "latest"]), 16)) / Decimal(10 ** 18)),
           "SPYx": str(Decimal(erc20(http, chain, SPYX)) / Decimal(10 ** 18)),
           "nonce_latest": int(post(http, rpc, "eth_getTransactionCount", [EVM_WALLET, "latest"]), 16),
           "nonce_pending": int(post(http, rpc, "eth_getTransactionCount", [EVM_WALLET, "pending"]), 16)}
    if chain in USDC:
        out["USDC"] = str(Decimal(erc20(http, chain, USDC[chain])) / Decimal(10 ** 6))
    if chain == "eth":
        out["wrapper_shares"] = erc20(http, chain, WRAPPER_ETH)
    return out


def sol(http) -> dict:
    out = {"SOL": str(Decimal(post(http, SOLANA_RPC, "getBalance", [SOL_WALLET])["value"]) / Decimal(10 ** 9))}
    for name, mint in SOL_MINTS.items():
        rows = post(http, SOLANA_RPC, "getTokenAccountsByOwner",
                    [SOL_WALLET, {"mint": mint}, {"encoding": "jsonParsed"}])["value"]
        out[name] = [row["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmountString"]
                     for row in rows] or "no_account"
    return out


def receipt(http, chain: str, tx_hash: str) -> dict:
    row = post(http, evm_feed.CHAINS[chain].rpc, "eth_getTransactionReceipt", [tx_hash])
    if not row:
        return {"receipt": None}
    out = {"status": row["status"], "block": int(row["blockNumber"], 16), "transfers": []}
    for log in row.get("logs") or []:
        topics = log.get("topics") or []
        if topics and topics[0] == TRANSFER and log["address"].lower() == SPYX:
            out["transfers"].append({"from": "0x" + topics[1][-40:], "to": "0x" + topics[2][-40:],
                                     "tokens": str(Decimal(int(log["data"], 16)) / Decimal(10 ** 18))})
        if log["address"].lower() == BRIDGE and len(topics) > 1:
            out["message_id"] = topics[1]
    return out


def ccip(http, message_id: str) -> dict:
    status, raw = http.request(f"https://ccip.chain.link/api/h/atlas/message/{message_id}")
    if status != 200:
        return {"http": status}
    row = json.loads(raw.decode("utf-8", "replace"))
    return {key: row.get(key) for key in ("state", "sourceNetworkName", "destNetworkName",
                                          "receiptTransactionHash", "receiptTimestamp")}


TSLAX_EVM = "0x8ad3c73f833d3f9a523ab01476625f269aeb7cf0"
ARRIVAL_CHAINS = ("eth", "bsc", "arbitrum", "optimism", "hyperevm", "ink", "x-layer")


def arrivals(http) -> dict:
    """SPYx and TSLAx on every EVM destination, plus SPYx on Solana. Unreadable is a string."""
    out = {}
    for chain in ARRIVAL_CHAINS:
        row = {}
        for name, token in (("SPYx", SPYX), ("TSLAx", TSLAX_EVM)):
            try:
                row[name] = str(Decimal(erc20(http, chain, token)) / Decimal(10 ** 18))
            except Exception as exc:  # noqa: BLE001
                row[name] = f"unreadable:{type(exc).__name__}"
        out[chain] = row
    try:
        out["solana"] = {"SPYx": sol(http)["SPYx"]}
    except Exception as exc:  # noqa: BLE001
        out["solana"] = f"unreadable:{type(exc).__name__}"
    return out


def main() -> int:
    http = Http()
    mode, rest = sys.argv[1], sys.argv[2:]
    if mode == "arrivals":
        result = arrivals(http)
    elif mode == "evm":
        result = evm(http, rest[0])
    elif mode == "sol":
        result = sol(http)
    elif mode == "solsig":
        result = post(http, SOLANA_RPC, "getSignatureStatuses",
                      [[rest[0]], {"searchTransactionHistory": True}])["value"]
    elif mode == "receipt":
        result = receipt(http, rest[0], rest[1])
    elif mode == "ccip":
        result = ccip(http, rest[0])
    else:
        raise SystemExit(__doc__)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
