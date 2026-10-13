#!/usr/bin/env python3
"""Manual Ethereum xStocks execution helpers.

This is deliberately not a daemon.  Every state-changing subcommand requires
``--confirm-live``.  The script builds calldata with OnchainOS, runs the
official simulation, then asks the OnchainOS wallet to sign and broadcast.

Examples:

    scripts/xstocks_eth_live.py quote --symbol STRCx --size-usd 5.2
    scripts/xstocks_eth_live.py buy --symbol STRCx --size-usd 5.2 --confirm-live
    scripts/xstocks_eth_live.py deposit --symbol STRCx --confirm-live
    scripts/xstocks_eth_live.py sell --symbol STRCx --confirm-live
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import xstocks_eth_ledger as ledger  # noqa: E402
from arb_monitor.venues.kraken_spot import (  # noqa: E402
    KrakenError, KrakenSpotClient, load_credentials)

STATE = ROOT / "data" / "xstocks_eth_state.json"
PROXY = "http://127.0.0.1:4780"
CHAIN = "ethereum"
RPC = "https://ethereum-rpc.publicnode.com"
WALLET = "0xabababababababababababababababababababab"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
TOKENS = {
    "STRCx": "0x1aad217b8f78dba5e6693460e8470f8b1a3977f3",
}
TOKEN_DECIMALS = {"USDC": 6, "STRCx": 18}


def _decimal(value, default: str | None = None) -> Decimal | None:
    try:
        if value in (None, ""):
            return Decimal(default) if default is not None else None
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(default) if default is not None else None


def token_address(symbol: str, override: str | None = None) -> str:
    if override:
        return override.lower()
    if symbol == "USDC":
        return USDC
    if symbol in TOKENS:
        return TOKENS[symbol]
    raise SystemExit(f"unknown token {symbol}; pass the full contract address")


def token_decimals(symbol: str, address: str) -> int:
    return TOKEN_DECIMALS.get(symbol, 18 if address.startswith("0x") else 0)


def run_cli(args: list[str], timeout: int = 90) -> dict:
    env = dict(os.environ)
    env["HTTPS_PROXY"] = PROXY
    completed = subprocess.run(
        ["onchainos", *args], capture_output=True, text=True, timeout=timeout, env=env)
    text = (completed.stdout or completed.stderr or "").strip()
    payload = None
    for candidate in (text, *reversed(text.splitlines())):
        candidate = candidate.strip()
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
            break
        except json.JSONDecodeError:
            continue
    if payload is None:
        raise RuntimeError(f"onchainos {' '.join(args[:3])} unparsable: {text[:300]}")
    if completed.returncode != 0 and payload.get("ok") is not True:
        raise RuntimeError(json.dumps(payload, ensure_ascii=False)[:600])
    return payload


def rpc(method: str, params: list):
    import urllib.request

    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    request = urllib.request.Request(
        RPC, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "xstocks-eth-live/0.1"})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    payload = json.loads(opener.open(request, timeout=25).read())
    if payload.get("error"):
        raise RuntimeError(str(payload["error"])[:300])
    return payload.get("result")


def call(to: str, data: str) -> str:
    return rpc("eth_call", [{"to": to, "data": data}, "latest"])


def erc20_balance(token: str, wallet: str = WALLET) -> Decimal:
    raw = call(token, "0x70a08231" + "0" * 24 + wallet[2:].lower())
    return Decimal(int(raw, 16)) / (Decimal(10) ** 18)


def approve_calldata(spender: str, amount_raw: int) -> str:
    return ("0x095ea7b3" + "0" * 24 + spender[2:].lower()
            + format(amount_raw, "064x"))


def transfer_calldata(to: str, amount_raw: int) -> str:
    return ("0xa9059cbb" + "0" * 24 + to[2:].lower()
            + format(amount_raw, "064x"))


def wait_receipt(tx_hash: str, attempts: int = 20, sleep: float = 3.0) -> dict | None:
    for _ in range(attempts):
        row = rpc("eth_getTransactionReceipt", [tx_hash])
        if row:
            return row
        time.sleep(sleep)
    return None


def simulate(to: str, data: str) -> dict:
    payload = run_cli([
        "gateway", "simulate", "--chain", CHAIN, "--from", WALLET,
        "--to", to, "--data", data, "--amount", "0",
    ])
    row = (payload.get("data") or [{}])
    return row[0] if isinstance(row, list) and row else (row if isinstance(row, dict) else {})


def broadcast(to: str, data: str, gas: str | None = None, force: bool = False) -> str:
    args = ["wallet", "contract-call", "--chain", CHAIN, "--to", to,
            "--input-data", data]
    if gas:
        args += ["--gas-limit", str(gas)]
    if force:
        args.append("--force")
    payload = run_cli(args)
    result = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    tx_hash = str(result.get("txHash") or "")
    if not tx_hash.startswith("0x"):
        raise RuntimeError(f"broadcast did not return txHash: {payload}")
    return tx_hash


def kraken_client() -> KrakenSpotClient:
    key, secret = load_credentials(ROOT / "config" / "kraken.env")
    return KrakenSpotClient(key, secret, proxy=PROXY, timeout=30)


def swap_quote(symbol: str, size_usd: Decimal) -> dict:
    token = token_address(symbol)
    payload = run_cli([
        "swap", "quote", "--chain", CHAIN, "--from", USDC, "--to", token,
        "--amount", str(int(size_usd * Decimal(10) ** 6)), "--swap-mode", "exactIn",
    ])
    row = (payload.get("data") or [{}])
    return row[0] if isinstance(row, list) and row else {}


def swap_tx(symbol: str, size_usd: Decimal, slippage: str = "0.5") -> dict:
    token = token_address(symbol)
    payload = run_cli([
        "swap", "swap", "--chain", CHAIN, "--from", USDC, "--to", token,
        "--amount", str(int(size_usd * Decimal(10) ** 6)),
        "--wallet", WALLET, "--slippage", slippage, "--swap-mode", "exactIn",
    ])
    row = (payload.get("data") or [{}])
    row = row[0] if isinstance(row, list) and row else {}
    tx = row.get("tx") or {}
    if not tx.get("to") or not tx.get("data"):
        raise RuntimeError(f"swap tx data missing: {row}")
    return {"quote": row.get("routerResult") or {}, "tx": tx}


def ensure_allowance(token: str, amount_raw: int, force: bool = False) -> list[dict]:
    payload = run_cli([
        "swap", "check-approvals", "--chain", CHAIN,
        "--address", WALLET, "--token", token,
    ])
    row = (payload.get("data") or [{}])
    row = row[0] if isinstance(row, list) and row else {}
    spendable = _decimal(((row.get("tokens") or [{}])[0]).get("spendable"), "0")
    if spendable is not None and spendable >= amount_raw:
        return []
    approve = run_cli([
        "swap", "approve", "--chain", CHAIN, "--token", token,
        "--amount", str(amount_raw),
    ])
    approve_row = (approve.get("data") or [{}])
    approve_row = approve_row[0] if isinstance(approve_row, list) else {}
    data = approve_row.get("data")
    if not data:
        raise RuntimeError("approve calldata missing")
    sim = simulate(token, data)
    if sim.get("failReason"):
        raise RuntimeError(f"approve simulation failed: {sim.get('failReason')}")
    tx_hash = broadcast(token, data, gas="70000", force=force)
    receipt = wait_receipt(tx_hash)
    if not receipt or receipt.get("status") != "0x1":
        raise RuntimeError(f"approve not confirmed: {tx_hash}")
    return [{"state": "approved", "tx": tx_hash, "gas_used": int(receipt.get("gasUsed"), 16)}]


def cmd_quote(args) -> int:
    quote = swap_quote(args.symbol, _decimal(args.size_usd))
    print(json.dumps(quote, ensure_ascii=False, indent=2))
    return 0


def cmd_buy(args) -> int:
    size_usd = _decimal(args.size_usd)
    if size_usd is None or size_usd <= 0:
        raise SystemExit("--size-usd must be positive")
    quote = swap_quote(args.symbol, size_usd)
    if quote.get("action") == "block":
        raise SystemExit(f"quote blocked: {quote.get('reason')}")
    if quote.get("action") == "warn" and not args.allow_warn:
        raise SystemExit(f"quote warning; rerun with --allow-warn: {quote.get('reason')}")
    plan = swap_tx(args.symbol, size_usd, args.slippage)
    if not args.confirm_live:
        print(json.dumps(plan, ensure_ascii=False, indent=2)[:2000])
        print("dry plan only; add --confirm-live to broadcast")
        return 0
    token = token_address(args.symbol)
    before = erc20_balance(token)
    amount_raw = int(size_usd * Decimal(10) ** token_decimals("USDC", USDC))
    events = ensure_allowance(USDC, amount_raw, force=args.force)
    sim = simulate(plan["tx"]["to"], plan["tx"]["data"])
    if sim.get("failReason"):
        raise SystemExit(f"swap simulation failed: {sim.get('failReason')}")
    tx_hash = broadcast(plan["tx"]["to"], plan["tx"]["data"],
                        gas=str(plan["tx"].get("gas") or ""),
                        force=args.force)
    receipt = wait_receipt(tx_hash)
    if not receipt or receipt.get("status") != "0x1":
        raise SystemExit(f"swap not confirmed: {tx_hash}")
    after = erc20_balance(token)
    record = ledger.append({
        "symbol": args.symbol,
        "network": "Ethereum",
        "state": "bought",
        "size_usd": str(size_usd),
        "tokens": str(after - before),
        "token_balance_after": str(after),
        "txids": {"buy": tx_hash},
        "events": events,
        "notes": [f"receipt gas {int(receipt.get('gasUsed'), 16)}"],
    })
    print(json.dumps(record, ensure_ascii=False))
    return 0


def cmd_deposit(args) -> int:
    token = token_address(args.symbol)
    balance = erc20_balance(token)
    amount = _decimal(args.amount) if args.amount else balance
    if amount is None or amount <= 0:
        raise SystemExit(f"no {args.symbol} balance")
    client = kraken_client()
    methods = client.deposit_methods(args.symbol)
    method = next((m for m in methods if "ethereum" in str(m.get("method")).lower()), None)
    if not method:
        raise SystemExit(f"Kraken has no Ethereum deposit method for {args.symbol}")
    addresses = client.deposit_addresses(args.symbol, method["method"])
    deposit_address = (addresses[0] or {}).get("address") if addresses else None
    if not deposit_address:
        raise SystemExit("Kraken deposit address missing")
    raw = int(amount * Decimal(10) ** token_decimals(args.symbol, token))
    data = transfer_calldata(deposit_address, raw)
    if not args.confirm_live:
        print(json.dumps({"to": token, "data": data, "amount": str(amount),
                          "kraken_method": method["method"],
                          "deposit_address": deposit_address}, indent=2))
        print("dry plan only; add --confirm-live to broadcast")
        return 0
    sim = simulate(token, data)
    if sim.get("failReason"):
        raise SystemExit(f"transfer simulation failed: {sim.get('failReason')}")
    tx_hash = broadcast(token, data, gas="80000", force=args.force)
    receipt = wait_receipt(tx_hash)
    if not receipt or receipt.get("status") != "0x1":
        raise SystemExit(f"transfer not confirmed: {tx_hash}")
    record = ledger.append({
        "symbol": args.symbol,
        "network": "Ethereum",
        "state": "deposited",
        "tokens": str(amount),
        "txids": {"deposit": tx_hash},
        "kraken_method": method["method"],
        "deposit_address": deposit_address,
        "notes": [f"receipt gas {int(receipt.get('gasUsed'), 16)}"],
    })
    print(json.dumps(record, ensure_ascii=False))
    return 0


def cmd_sell(args) -> int:
    client = kraken_client()
    balance = client.balance().get(args.symbol) or client.balance().get(f"{args.symbol}.T")
    if not balance:
        raise SystemExit(f"no Kraken balance for {args.symbol}")
    depth = client.depth(args.symbol, count=5)
    bids = depth.get("bids") or []
    if not bids:
        raise SystemExit("no Kraken bid")
    price = str(bids[0][0])
    client.validate_order(args.symbol, balance, side="sell",
                          ordertype="limit", price=price)
    if not args.confirm_live:
        print(json.dumps({"symbol": args.symbol, "volume": str(balance), "price": price},
                         ensure_ascii=False))
        print("dry plan only; add --confirm-live to broadcast")
        return 0
    order = client.order(args.symbol, balance, side="sell", ordertype="limit",
                         price=price, confirm="live")
    record = ledger.append({
        "symbol": args.symbol,
        "network": "Ethereum",
        "state": "sell_sent",
        "tokens": str(balance),
        "sell_price": price,
        "txids": {"sell": order.get("txid") or []},
        "notes": ["Kraken limit sell submitted"],
    })
    print(json.dumps(record, ensure_ascii=False))
    return 0


def cmd_status(args) -> int:
    print(json.dumps({"ledger": ledger.read_records()}, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("quote", "buy", "deposit", "sell"):
        command = sub.add_parser(name)
        command.add_argument("--symbol", required=True, choices=sorted(TOKENS))
        if name in {"quote", "buy"}:
            command.add_argument("--size-usd", required=True)
        if name == "deposit":
            command.add_argument("--amount")
        if name in {"buy", "deposit", "sell"}:
            command.add_argument("--confirm-live", action="store_true")
            command.add_argument("--force", action="store_true",
                                 help="only after the first call returned confirming")
        if name == "buy":
            command.add_argument("--slippage", default="0.5")
            command.add_argument("--allow-warn", action="store_true")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    return {
        "quote": cmd_quote,
        "buy": cmd_buy,
        "deposit": cmd_deposit,
        "sell": cmd_sell,
        "status": cmd_status,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
