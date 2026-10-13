#!/usr/bin/env python3
"""Solana -> X Layer CCIP probe for one (source, dest, symbol) triple.

Read-only by default. Three subcommands:

    plan       decode the instruction we would send, print every account role
    simulate   build the legacy transaction and run simulateTransaction
    send       sign+broadcast through the OnchainOS TEE wallet, wait for the
               arrival on X Layer, then append the route sample

``send`` is the only mode that moves money, and it is the only mode that writes
``data-v2/xstocks_v2_route_samples.jsonl``. Nothing here touches v1's locks, the
mode / auto_orders / live_ack switches, or the halt files.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.xstocks_v2 import ccip_solana as C     # noqa: E402
from arb_monitor.xstocks_v2 import units                    # noqa: E402

PROXY = "http://127.0.0.1:4780"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
XLAYER_RPC = "https://rpc.xlayer.tech"
BOOK = ROOT / "data" / "xstocks_book.sqlite"
DATA = ROOT / "data-v2"
ROUTE_SAMPLES = DATA / "xstocks_v2_route_samples.jsonl"
CONFIG_CACHE = DATA / "xstocks_v2_ccip_solana_configs.json"

WALLET = "AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev"
EVM_WALLET = "0xabababababababababababababababababababab"


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


class Http:
    def __init__(self, timeout: float = 45.0):
        import requests

        self.session = requests.Session()
        self.session.proxies = {"http": PROXY, "https": PROXY}
        self.timeout = timeout

    def rpc(self, url: str, method: str, params: list, tries: int = 4):
        last = None
        for attempt in range(tries):
            try:
                payload = self.session.post(
                    url, json={"jsonrpc": "2.0", "id": 1, "method": method,
                               "params": params}, timeout=self.timeout).json()
                if "result" in payload:
                    return payload["result"]
                last = payload.get("error")
            except Exception as exc:  # noqa: BLE001
                last = exc
            time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"{method} failed: {last}")


# --------------------------------------------------------------------------
# book lookups
# --------------------------------------------------------------------------
def book_symbol(symbol: str) -> dict:
    """``{mint, product_id, evm_xlayer, svm_decimals}`` for one symbol."""
    conn = sqlite3.connect(f"file:{BOOK}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT products_json FROM bridges WHERE source_network = 'Solana'").fetchone()
    finally:
        conn.close()
    if not row:
        raise SystemExit("no Solana row in the bridge table")
    for product in json.loads(row[0]):
        if str(product.get("symbol")) != symbol:
            continue
        out = {
            "symbol": symbol,
            "name": product.get("name"),
            "product_id": int(product["productId"]),
            "svm_decimals": int(product.get("svmDecimals") or 8),
            "evm_decimals": int(product.get("evmDecimals") or 18),
            "mint": None,
            "evm_xlayer": None,
        }
        for deployment in product.get("deployments") or []:
            network = deployment.get("network")
            if network == "Solana":
                out["mint"] = deployment["address"]
            elif network == "XLayer":
                out["evm_xlayer"] = deployment["address"]
        if not out["mint"]:
            raise SystemExit(f"{symbol} has no Solana deployment")
        if not out["evm_xlayer"]:
            raise SystemExit(f"{symbol} has no XLayer deployment")
        return out
    raise SystemExit(f"{symbol} is not in the Solana bridge product list")


def load_config_cache() -> dict:
    try:
        return json.loads(CONFIG_CACHE.read_text())
    except (OSError, ValueError):
        return {}


def discover_token_config(http: Http, symbol: str, mint: str) -> tuple[str, bytes]:
    """``token_config`` account for a mint, cached. The account holds the mint at
    offset 16, so one filtered ``getProgramAccounts`` finds it exactly."""
    cache = load_config_cache()
    hit = cache.get(mint)
    if hit and hit.get("pubkey"):
        raw = base64.b64decode(hit["data"])
        C.token_config_custody(raw, mint=mint)
        return hit["pubkey"], raw
    log(f"looking up the token config for {symbol} ({mint[:8]}...)")
    rows = http.rpc(SOLANA_RPC, "getProgramAccounts", [
        C.SOLANA_PROGRAM,
        {"encoding": "base64",
         "filters": [{"memcmp": {"offset": C.TOKEN_CONFIG_MINT_OFFSET, "bytes": mint}}],
         "dataSlice": {"offset": 0, "length": C.TOKEN_CONFIG_LEN}},
    ])
    if not rows:
        raise SystemExit(f"no token config for {mint}; the mint may not be bridged")
    if len(rows) > 1:
        raise SystemExit(f"{len(rows)} token configs for {mint}; refusing to guess")
    pubkey = rows[0]["pubkey"]
    raw = base64.b64decode(rows[0]["account"]["data"][0])
    C.token_config_custody(raw, mint=mint)
    DATA.mkdir(exist_ok=True)
    cache[mint] = {"symbol": symbol, "pubkey": pubkey, "data": base64.b64encode(raw).decode(),
                   "checked_at_ms": int(time.time() * 1000)}
    CONFIG_CACHE.write_text(json.dumps(cache, indent=1))
    return pubkey, raw


def solana_balance(http: Http, mint: str) -> int:
    rows = http.rpc(SOLANA_RPC, "getTokenAccountsByOwner", [
        WALLET, {"mint": mint}, {"encoding": "jsonParsed"}])
    total = 0
    for row in rows.get("value") or []:
        info = row["account"]["data"]["parsed"]["info"]
        total += int(info["tokenAmount"]["amount"])
    return total


def xlayer_balance(http: Http, token: str, owner: str = EVM_WALLET,
                   decimals: int = 18) -> Decimal | None:
    """``balanceOf`` on X Layer, at the token's own decimals.

    The default is 18 because every xStock is 18-decimal. **USDG is 6.** Passing
    the wrong scale keeps working for "did the balance move" and breaks every
    magnitude comparison (found 2026-10-07; see ``units.scale_raw``).
    """
    data = "0x70a08231" + "0" * 24 + owner[2:].lower()
    payload = http.session.post(XLAYER_RPC, json={
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": token, "data": data}, "latest"]}, timeout=30).json()
    raw = payload.get("result")
    if not raw or raw == "0x":
        return None
    return units.scale_raw(int(raw, 16), decimals)


def simulate_message(http: Http, raw_tx: bytes) -> dict:
    """``simulateTransaction`` for the exact transaction we are about to send."""
    return http.rpc(SOLANA_RPC, "simulateTransaction", [
        base64.b64encode(raw_tx).decode(),
        {"encoding": "base64", "sigVerify": False,
         "replaceRecentBlockhash": True,
         "commitment": "processed"}])["value"]


def simulation_refusal(value: dict | None) -> str:
    """``""`` when the simulation passed, else a short reason to refuse on.

    ``send`` used to broadcast without simulating: the ``simulate`` subcommand
    ran the check, but nothing made the money-moving path run it (found
    2026-10-07). The same hole existed in the EVM bridge leg, where the revert
    could never be seen because the preflight ran before the approve. One rule
    for both: no dry-run, no broadcast.
    """
    err = (value or {}).get("err")
    if not err:
        return ""
    if isinstance(err, dict):
        return json.dumps(err, ensure_ascii=False)[:200]
    return str(err)[:200]


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------
def build(http: Http, symbol: str, amount: int, recipient: str):
    product = book_symbol(symbol)
    config_key, config_data = discover_token_config(http, symbol, product["mint"])
    custody = C.token_config_custody(config_data, mint=product["mint"],
                                     product_id=product["product_id"])
    accounts = C.build_accounts(signer=WALLET, mint=product["mint"],
                                token_config=config_key, custody=custody)
    problems = C.validate_accounts(accounts, signer=WALLET, mint=product["mint"],
                                   token_config=config_key, custody=custody)
    if problems:
        raise SystemExit(f"account table rejected before signing: {problems}")
    data = C.instruction_data(recipient=recipient, product_id=product["product_id"],
                              amount=amount)
    args = C.decode_args(data)
    args_problems = C.validate_args(args, product_id=product["product_id"], amount=amount)
    if args_problems:
        raise SystemExit(f"instruction args rejected before signing: {args_problems}")
    product["custody"] = C.to_b58(custody)
    product["token_config"] = config_key
    return product, accounts, data, args


def show(product: dict, accounts, args) -> None:
    print(f"symbol         {product['symbol']}  ({product['name']})")
    print(f"mint           {product['mint']}")
    print(f"product id     {product['product_id']}")
    print(f"token config   {product['token_config']}")
    print(f"custody        {product['custody']}")
    print(f"recipient      {'0x' + args.recipient.hex()}")
    print(f"receiver       {C.bytes32_to_evm(args.receiver)}  (bridge, not all zero)")
    print(f"dest selector  {args.dest_chain_selector}")
    print(f"amount         {args.amount} raw = {Decimal(args.amount) / 10 ** 8} tokens")
    print(f"payload        {C.message_payload(args.recipient, args.product_id, args.amount).hex()}")
    print(f"accounts       {len(accounts)} (EVM->Solana uses 11)")
    for index, row in enumerate(accounts):
        print(f"  {index:2} {'W' if row.is_writable else '-'}{'S' if row.is_signer else '-'} "
              f"{row.pubkey}  {row.role}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("plan", "simulate", "send"))
    parser.add_argument("--symbol", default="QQQx")
    parser.add_argument("--amount", required=True,
                        help="raw Solana units (8 decimals); 1000000 = 0.01 token")
    parser.add_argument("--recipient", default=EVM_WALLET)
    args = parser.parse_args()

    http = Http()
    product, accounts, data, decoded = build(http, args.symbol, int(args.amount),
                                             args.recipient)
    if args.mode == "plan":
        show(product, accounts, decoded)
        return 0

    log(f"wallet holds {solana_balance(http, product['mint'])} raw of {args.symbol}")
    blockhash = http.rpc(SOLANA_RPC, "getLatestBlockhash",
                         [{"commitment": "finalized"}])["value"]["blockhash"]
    raw = C.build_legacy_transaction(payer=WALLET, accounts=accounts,
                                     program_id=C.SOLANA_PROGRAM, data=data,
                                     recent_blockhash=blockhash)
    parsed = C.parse_legacy_transaction(raw)
    ccip = [item for item in parsed["instructions"]
            if item["program_id"] == C.SOLANA_PROGRAM][0]
    assert ccip["data"] == data
    assert len(ccip["accounts"]) == C.ACCOUNT_COUNT
    log(f"transaction {len(raw)} bytes, {parsed['signers']} signer slot, "
        f"instruction accounts {len(ccip['accounts'])}, "
        f"cu limit {C.DEFAULT_COMPUTE_UNIT_LIMIT}")

    if args.mode == "simulate":
        value = simulate_message(http, raw)
        print(json.dumps({"err": value.get("err"),
                          "units": value.get("unitsConsumed"),
                          "pre": value.get("preTokenBalances"),
                          "post": value.get("postTokenBalances")}, indent=1))
        for line in value.get("logs") or []:
            print(" ", line)
        return 0 if not value.get("err") else 1

    # ---------------------------------------------------------------- send
    # No dry-run, no broadcast: the simulation is what proves that the accounts,
    # the compute budget and the token program all accept this message. The
    # 2026-09-26 lesson was that a source-chain simulation does *not* prove the
    # destination accepts it -- but it does prove the send can leave, and a
    # message that cannot leave is a fee paid for nothing.
    refusal = simulation_refusal(simulate_message(http, raw))
    if refusal:
        raise SystemExit(f"simulation refused the send: {refusal}")
    before_dest = xlayer_balance(http, product["evm_xlayer"])
    before_src = solana_balance(http, product["mint"])
    log(f"on X Layer {product['evm_xlayer']} we hold {before_dest}; "
        f"on Solana we hold {before_src} raw")
    env = dict(os.environ, HTTPS_PROXY=PROXY, HTTP_PROXY=PROXY)
    completed = subprocess.run(
        ["onchainos", "wallet", "contract-call", "--chain", "solana",
         "--to", C.SOLANA_PROGRAM, "--unsigned-tx", C.b58encode(raw),
         "--from", WALLET],
        capture_output=True, text=True, env=env, timeout=240)
    try:
        result = json.loads(completed.stdout)
    except ValueError:
        result = {"ok": False, "raw": completed.stdout[:400], "err": completed.stderr[:400]}
    tx_hash = ((result.get("data") or {}) if isinstance(result, dict) else {}).get("txHash")
    log(f"broadcast: ok={result.get('ok')} tx={tx_hash} err={result.get('error')}")
    if not tx_hash:
        print(json.dumps(result, indent=1)[:1200])
        raise SystemExit("no transaction hash; nothing recorded")

    deadline = time.time() + 45 * 60
    arrived = None
    while time.time() < deadline:
        time.sleep(60)
        after_src = solana_balance(http, product["mint"])
        after_dest = xlayer_balance(http, product["evm_xlayer"])
        log(f"solana {after_src} raw (was {before_src}); xlayer {after_dest} (was {before_dest})")
        if after_dest is not None and (before_dest is None or after_dest > before_dest):
            arrived = after_dest
            break
    if arrived is None:
        raise SystemExit("no arrival seen within 45 minutes; nothing recorded")

    DATA.mkdir(exist_ok=True)
    with ROUTE_SAMPLES.open("a") as handle:
        handle.write(json.dumps({
            "source_chain": "solana",
            "dest_chain": "x-layer",
            "symbol": product["symbol"],
            "mint": product["mint"],
            "product_id": product["product_id"],
            "evm_xlayer": product["evm_xlayer"],
            "token_config": product["token_config"],
            "custody": product["custody"],
            "recipient": args.recipient,
            "amount_raw": int(args.amount),
            "solana_tx": tx_hash,
            "dest_balance_after": str(arrived),
            "arrived": True,
            "measured_ms": int(time.time() * 1000),
            "source": "ccip_solana_probe",
        }, sort_keys=True) + "\n")
    log(f"route sample written to {ROUTE_SAMPLES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
