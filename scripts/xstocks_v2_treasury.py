#!/usr/bin/env python3
"""Refill one short venue from a venue that can spare the money.

    scripts/xstocks_v2_treasury.py plan
    scripts/xstocks_v2_treasury.py once --confirm-live

One move per run. A short venue does not stop the others, and a move that
was sent in the last 30 minutes is not sent again while it is still in flight.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.venues.kraken_spot import (KrakenSpotClient,  # noqa: E402
                                            load_credentials)
from arb_monitor.xstocks_v2.feeds.http import Http               # noqa: E402
from arb_monitor.xstocks_v2.treasury import plan                 # noqa: E402

DATA = ROOT / "data-v2"
LEDGER = DATA / "xstocks_v2_treasury.jsonl"
PROXY = "http://127.0.0.1:4780"
EVM_WALLET = "0xabababababababababababababababababababab"
USDG_XLAYER = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"
USDC_ETH = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
XLAYER_RPC = "https://rpc.xlayer.tech"
ETH_RPC = "https://eth.drpc.org"
#: Long enough for a Kraken deposit to credit, short enough that a failed
#: send is retried the same day.
IN_FLIGHT_S = 30 * 60
DEPOSIT_METHOD = "USDC - Ethereum (Unified)"


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def _dec(value) -> Decimal:
    return Decimal(str(value or "0"))


def _recent(rail: str, *, now_s: float) -> bool:
    if not LEDGER.exists():
        return False
    for line in LEDGER.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("rail") != rail or not row.get("sent"):
            continue
        if now_s - float(row.get("ts") or 0) < IN_FLIGHT_S:
            return True
    return False


def _remember(row: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _erc20(http, rpc: str, token: str, owner: str) -> Decimal | None:
    data = "0x70a08231" + "0" * 24 + owner[2:].lower()
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": token, "data": data}, "latest"]}).encode()
    try:
        status, raw = http.request(rpc, data=body,
                                   headers={"Content-Type": "application/json"})
    except OSError:
        return None
    if status != 200:
        return None
    result = json.loads(raw.decode("utf-8", "replace")).get("result")
    if not isinstance(result, str) or not result.startswith("0x"):
        return None
    # USDG on X Layer and USDC on Ethereum are both 6 decimals.
    return Decimal(int(result, 16)) / Decimal(10 ** 6)


def balances(client, http) -> dict:
    kraken = client.balance()
    return {
        ("kraken", "USDG"): _dec(kraken.get("USDG")),
        ("kraken", "ZUSD"): _dec(kraken.get("ZUSD")),
        ("kraken", "USDC"): _dec(kraken.get("USDC")),
        ("x-layer", "USDG"): _erc20(http, XLAYER_RPC, USDG_XLAYER, EVM_WALLET),
        ("eth", "USDC"): _erc20(http, ETH_RPC, USDC_ETH, EVM_WALLET),
    }


def _onchainos(args: list[str]) -> dict:
    env = dict(os.environ, HTTPS_PROXY=PROXY, HTTP_PROXY=PROXY)
    done = subprocess.run(["onchainos", *args], capture_output=True, text=True,
                          timeout=120, env=env)
    text = (done.stdout or done.stderr or "").strip()
    for candidate in (text, *reversed(text.splitlines())):
        candidate = candidate.strip()
        if candidate.startswith("{"):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    return {"ok": False, "error": f"unparsable: {text[:300]}"}


def _withdraw(client, move: dict) -> dict:
    result = client._post("/0/private/Withdraw", {
        "asset": move["source"][1], "key": move["key"], "amount": move["amount"]})
    return {"sent": True, "rail": move["rail"], "refid": result.get("refid"),
            "amount": move["amount"], "key": move["key"]}


def _deposit_usdc(client, move: dict) -> dict:
    rows = client._post("/0/private/DepositAddresses",
                        {"asset": "USDC", "method": DEPOSIT_METHOD}) or []
    address = str((rows[0] if rows else {}).get("address") or "")
    if not address.startswith("0x") or len(address) != 42:
        return {"sent": False, "rail": move["rail"],
                "reason": "deposit_address_unreadable"}
    raw = int(Decimal(move["amount"]) * Decimal(10 ** 6))
    data = "0xa9059cbb" + "0" * 24 + address[2:].lower() + format(raw, "064x")
    out = _onchainos(["wallet", "contract-call", "--chain", "ethereum",
                      "--to", USDC_ETH, "--input-data", data,
                      "--gas-limit", "80000", "--from", EVM_WALLET])
    tx = str(((out.get("data") or {}) if out.get("ok") else {}).get("txHash") or "")
    if not tx.startswith("0x"):
        return {"sent": False, "rail": move["rail"],
                "reason": f"wallet:{str(out.get('error') or out.get('message'))[:160]}"}
    return {"sent": True, "rail": move["rail"], "tx": tx,
            "amount": move["amount"], "to": address}


def _sell_usdc(client, move: dict) -> dict:
    result = client.order("USDCUSD", move["amount"], side="sell",
                          ordertype="market", tokenized=False, confirm="live")
    txid = (result.get("txid") or [""])[0]
    return {"sent": True, "rail": move["rail"], "txid": txid,
            "amount": move["amount"]}


def maintain(*, client, http, data_dir: Path | str = DATA,
             confirm: bool = False, now_s: float | None = None) -> dict:
    """Plan, and when ``confirm`` is set, sign the first ready move."""
    now_s = time.time() if now_s is None else now_s
    held = balances(client, http)
    # An unread chain balance stays out of the plan. Zero would look like dust
    # and would withdraw into a wallet we failed to read.
    readable = {key: value for key, value in held.items() if value is not None}
    moves = plan(readable)
    ready = next((row for row in moves if row["status"] == "ready"), None)
    if ready is None:
        return {"sent": False, "moves": moves}
    if _recent(ready["rail"], now_s=now_s):
        return {"sent": False, "moves": moves,
                "reason": f"in_flight:{ready['rail']}"}
    if not confirm:
        return {"sent": False, "moves": moves, "next": ready}
    if ready["rail"] == "kraken_withdraw":
        result = _withdraw(client, ready)
    elif ready["rail"] == "eth_usdc_deposit":
        result = _deposit_usdc(client, ready)
    elif ready["rail"] == "kraken_sell_usdc":
        result = _sell_usdc(client, ready)
    else:
        result = {"sent": False, "reason": f"no_executor:{ready['rail']}"}
    result["ts"] = now_s
    result["reason"] = ready["reason"]
    _remember(result)
    return {**result, "moves": moves}


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "plan"
    confirm = "--confirm-live" in sys.argv
    key, secret = load_credentials(ROOT / "config" / "kraken.env")
    client = KrakenSpotClient(key, secret, proxy=PROXY, timeout=40)
    result = maintain(client=client, http=Http(), confirm=confirm and mode == "once")
    log(json.dumps({k: v for k, v in result.items() if k != "moves"},
                   ensure_ascii=False))
    for move in result.get("moves") or []:
        log(f"{move['status']} {move['rail']} {move.get('amount', '')} {move['reason']}")
    return 0 if mode == "plan" or result.get("sent") or not result.get("next") else 1


if __name__ == "__main__":
    raise SystemExit(main())
