#!/usr/bin/env python3
"""Stage 2 on Ethereum: one v4 ring, measured before it is ever broadcast.

    scripts/xstocks_v2_atomic_v4_probe.py actions
    scripts/xstocks_v2_atomic_v4_probe.py pools --symbol SPCXx
    scripts/xstocks_v2_atomic_v4_probe.py quote --symbol SPCXx --usdt 25
    scripts/xstocks_v2_atomic_v4_probe.py ring  --symbol SPCXx --usdt 25
    scripts/xstocks_v2_atomic_v4_probe.py allowances --symbol SPCXx
    scripts/xstocks_v2_atomic_v4_probe.py approve --symbol SPCXx --usdt 25 --confirm-live
    scripts/xstocks_v2_atomic_v4_probe.py once    --symbol SPCXx --usdt 25 --confirm-live

Everything except ``approve`` and ``once`` is read-only. ``once`` builds the
ring, asks OnchainOS to simulate it with the wallet's real allowances, and only
then broadcasts. One ``execute`` call or nothing: the script cannot fall back to
two swaps (``tests/test_xstocks_v2_atomic_v4.py`` guards that with a source
check, the same way the v3 executor is guarded).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.core.evm import keccak256                      # noqa: E402
from arb_monitor.xstocks_v2 import atomic_v4 as v4               # noqa: E402
from arb_monitor.xstocks_v2.feeds import census as census_feed   # noqa: E402
from arb_monitor.xstocks_v2.feeds import evm                     # noqa: E402
from arb_monitor.xstocks_v2.feeds.http import Http               # noqa: E402

PROXY = "http://127.0.0.1:4780"
RPC = "https://ethereum-rpc.publicnode.com"
WALLET = "0xabababababababababababababababababababab"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDC_DECIMALS = 6
CHAIN = "ethereum"
DATA = ROOT / "data"


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def http() -> Http:
    return Http(proxy=PROXY)


def dump(value) -> str:
    """JSON for humans: Decimal and the census rows are not plain floats."""
    return json.dumps(value, indent=1, ensure_ascii=False, default=str)


def rpc(client: Http, method: str, params: list) -> dict:
    _status, payload = client.post_json(
        RPC, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    return payload if isinstance(payload, dict) else {}


def eth_call(client: Http, to: str, data: str, *, sender: str = WALLET,
             override: dict | None = None) -> dict:
    call = {"from": sender, "to": to, "data": data}
    params = [call, "latest"] + ([override] if override else [])
    payload = rpc(client, "eth_call", params)
    if payload.get("error"):
        return {"ok": False, "error": payload["error"]}
    return {"ok": True, "result": str(payload.get("result") or "")}


def onchainos(args: list[str], timeout: int = 240) -> dict:
    env = dict(os.environ, HTTPS_PROXY=PROXY, HTTP_PROXY=PROXY)
    done = subprocess.run(["onchainos"] + args, capture_output=True, text=True,
                          env=env, timeout=timeout)
    text = (done.stdout or done.stderr or "").strip()
    for candidate in (text, *reversed(text.splitlines())):
        candidate = candidate.strip()
        if not candidate.startswith("{"):
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return {"ok": False, "error": text[:400]}


# ---------------------------------------------------------------------------
# Pools from the census (the same rows the scan quotes)
# ---------------------------------------------------------------------------
def eth_pools(symbol: str) -> list[dict]:
    pools = census_feed.v4_pool_rows(census_feed.load_v4())
    out = []
    for pool in pools:
        if pool.get("chain") != "eth" or str(pool.get("symbol")) != symbol:
            continue
        key = v4.pool_key(token=pool["token_address"],
                          stable=pool["quote_address"],
                          fee=int(pool.get("fee_key") or 0),
                          tick_spacing=int(pool.get("tick_spacing") or 60),
                          hooks=str(pool.get("hooks") or ""))
        out.append({**pool, "key": key, "computed_id": v4.pool_id(key)})
    return out


def pool_state(client: Http, pool_id: str) -> dict:
    """``PoolManager.extsload`` for this pool's ``slot0`` (tick, price, fees)."""
    # v4 StateLibrary: pools mapping lives at slot 6, slot0 is the first word.
    slot = "0x" + keccak256(bytes.fromhex(
        pool_id.removeprefix("0x") + format(6, "064x"))).hex()
    selector = "0x" + keccak256(b"extsload(bytes32)")[:4].hex()
    reply = eth_call(client, v4.POOL_MANAGER["eth"], selector + slot[2:])
    if not reply.get("ok"):
        return {"error": str(reply.get("error"))[:160]}
    raw = reply["result"].removeprefix("0x")
    if len(raw) < 64:
        return {"error": "empty"}
    word = int(raw[:64], 16)
    sqrt_price = word & ((1 << 160) - 1)
    tick = (word >> 160) & ((1 << 24) - 1)
    if tick >= (1 << 23):
        tick -= (1 << 24)
    protocol_fee = (word >> 184) & ((1 << 24) - 1)
    lp_fee = (word >> 208) & ((1 << 24) - 1)
    return {"sqrt_price_x96": sqrt_price, "tick": tick,
            "protocol_fee": protocol_fee, "lp_fee_raw": lp_fee,
            "price_usdc_per_token": float(
                (Decimal(sqrt_price) / Decimal(2 ** 96)) ** 2
                * Decimal(10) ** (18 - USDC_DECIMALS)) if sqrt_price else None}


# ---------------------------------------------------------------------------
# Quoting (the official v4 Quoter the scan already uses)
# ---------------------------------------------------------------------------
def quote_single(client: Http, key: dict, *, zero_for_one: bool, amount_in: int) -> dict:
    data = evm.encode_v4_quote(key, zero_for_one, amount_in)
    reply = eth_call(client, v4.V4_QUOTER["eth"], data)
    if not reply.get("ok"):
        return {"ok": False, "error": str(reply.get("error"))[:200]}
    raw = reply["result"].removeprefix("0x")
    if len(raw) < 64:
        return {"ok": False, "error": "short_result"}
    return {"ok": True, "amount_out": int(raw[:64], 16)}


def pick_pair(client: Http, symbol: str, notional_usdt: Decimal) -> dict:
    """The best positive two-pool ring for this symbol, with quotes."""
    pools = eth_pools(symbol)
    amount_in = int(notional_usdt * Decimal(10) ** USDC_DECIMALS)
    rows = []
    for pool in pools:
        key = pool["key"]
        zero_for_one_buy = key["currency0"].lower() == USDC
        buy = quote_single(client, key, zero_for_one=zero_for_one_buy,
                           amount_in=amount_in)
        rows.append({**pool, "buy": buy, "zero_for_one_buy": zero_for_one_buy})
    out = {"symbol": symbol, "notional_usdt": str(notional_usdt),
           "amount_in_raw": amount_in, "pools": [], "ring": None}
    for pool in rows:
        entry = {"pool_id": pool["pool_id"], "tier": pool.get("tier"),
                 "fee_key": pool.get("fee_key"), "hook": pool.get("hooks"),
                 "buy": pool["buy"]}
        out["pools"].append(entry)
    best = None
    for buy_pool in rows:
        if not buy_pool["buy"].get("ok") or not buy_pool["buy"]["amount_out"]:
            continue
        tokens = buy_pool["buy"]["amount_out"]
        for sell_pool in rows:
            if sell_pool["pool_id"] == buy_pool["pool_id"]:
                continue
            sell_zero_for_one = sell_pool["key"]["currency0"].lower() != USDC
            sell = quote_single(client, sell_pool["key"],
                                zero_for_one=sell_zero_for_one, amount_in=tokens)
            if not sell.get("ok") or not sell["amount_out"]:
                continue
            edge_bps = (Decimal(sell["amount_out"] - amount_in)
                        / Decimal(amount_in) * Decimal(10000))
            ring = {"buy_pool": buy_pool, "sell_pool": sell_pool,
                    "tokens": tokens, "usdc_out": sell["amount_out"],
                    "edge_bps": edge_bps}
            if edge_bps <= 0:
                continue
            if best is None or edge_bps > best["edge_bps"]:
                best = ring
    if best:
        out["ring"] = {
            "buy_pool_id": best["buy_pool"]["pool_id"],
            "sell_pool_id": best["sell_pool"]["pool_id"],
            "buy_tier": best["buy_pool"].get("tier"),
            "sell_tier": best["sell_pool"].get("tier"),
            "tokens_raw": best["tokens"],
            "usdc_out_raw": best["usdc_out"],
            "edge_bps": str(best["edge_bps"]),
            "gross_usdt": str((Decimal(best["usdc_out"] - amount_in)
                               / Decimal(10) ** USDC_DECIMALS)),
        }
        out["_ring"] = best
    return out


# ---------------------------------------------------------------------------
# Allowance slots: measured, not assumed
# ---------------------------------------------------------------------------
def _mapping_slot(key: str, base: int) -> str:
    return "0x" + keccak256(bytes.fromhex(
        key.removeprefix("0x").rjust(64, "0") + format(base, "064x"))).hex()


def _two_level_slot(owner: str, nested: str) -> str:
    return "0x" + keccak256(bytes.fromhex(
        owner.removeprefix("0x").rjust(64, "0") + nested[2:])).hex()


def permit2_slot(owner: str, token: str, spender: str, base: int) -> str:
    """``allowance[owner][token][spender]`` -- three levels, packed word."""
    return _two_level_slot(spender, _two_level_slot(
        token, _mapping_slot(owner, base)))


def discover_slots(client: Http, token: str, permit2: str, router: str) -> dict:
    """Find the storage slots for ``balanceOf``/``allowance`` of one ERC-20 and
    for Permit2's allowance word, by overriding a candidate and reading it back.

    Guess-and-verify, never guess-and-trust: each slot is accepted only when the
    public getter returns the value we wrote.
    """
    found: dict = {}
    probe_balance = 0xB0B0B0B0B0B0
    for base in range(0, 14):
        slot = _mapping_slot(WALLET, base)
        override = {token: {"stateDiff": {slot: "0x" + format(probe_balance, "064x")}}}
        reply = eth_call(client, token,
                         "0x70a08231" + v4.addr_word(WALLET), override=override)
        if reply.get("ok") and int(reply["result"] or "0x0", 16) == probe_balance:
            found["balance_slot"] = base
            break
    probe_allowance = 0xA11CE
    for base in range(0, 14):
        if base == found.get("balance_slot"):
            continue
        nested = _mapping_slot(WALLET, base)
        slot = _two_level_slot(permit2, nested)
        override = {token: {"stateDiff": {slot: "0x" + format(probe_allowance, "064x")}}}
        reply = eth_call(client, token,
                         v4.erc20_allowance_calldata(WALLET, permit2),
                         override=override)
        if reply.get("ok") and int(reply["result"] or "0x0", 16) == probe_allowance:
            found["allowance_slot"] = base
            break
    packed = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF | ((1 << 48) - 1 << 160)
    # Permit2 keeps its allowance map behind SignatureTransfer's nonce bitmap,
    # so the base slot is not 0. The range is a search, not an assumption.
    for base in range(0, 16):
        slot = permit2_slot(WALLET, token, router, base)
        override = {permit2: {"stateDiff": {slot: "0x" + format(packed, "064x")}}}
        reply = eth_call(client, permit2,
                         v4.permit2_allowance_calldata(WALLET, token, router),
                         override=override)
        if reply.get("ok"):
            got = v4.decode_permit2_allowance(reply["result"])
            if got["amount"] == 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF:
                found["permit2_slot"] = base
                break
    return found


def allowance_override(token: str, permit2: str, router: str, slots: dict,
                       *, amount_raw: int) -> dict:
    """Let the simulation pretend the two approvals are already in place."""
    if "allowance_slot" not in slots or "permit2_slot" not in slots:
        return {}
    erc20_slot = _two_level_slot(permit2, _mapping_slot(WALLET, slots["allowance_slot"]))
    packed = (amount_raw & ((1 << 160) - 1)) | (((1 << 48) - 1) << 160)
    return {
        token: {"stateDiff": {erc20_slot: "0x" + format(amount_raw, "064x")}},
        permit2: {"stateDiff": {
            permit2_slot(WALLET, token, router, slots["permit2_slot"]):
                "0x" + format(packed, "064x")}},
    }


def read_allowances(client: Http, token: str, permit2: str, router: str) -> dict:
    erc20 = eth_call(client, token, v4.erc20_allowance_calldata(WALLET, permit2))
    p2 = eth_call(client, permit2, v4.permit2_allowance_calldata(WALLET, token, router))
    return {
        "erc20_to_permit2": (int(erc20["result"], 16) if erc20.get("ok") else None),
        "permit2_to_router": (v4.decode_permit2_allowance(p2["result"])
                              if p2.get("ok") else None),
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_actions(client: Http) -> int:
    """What the deployed router answers to: action bytes and param length."""
    key = v4.pool_key(token="0x68fa48b1c2fe52b3d776e1953e0e782b5044ce28",
                      stable=USDC, fee=2500, tick_spacing=50)
    layouts = {}
    for layout in (v4.SWAP_LAYOUT_V1, v4.SWAP_LAYOUT_V2):
        params = v4.swap_exact_in_single_params(
            key, zero_for_one=False, amount_in=0, amount_out_min=0, layout=layout)
        data = v4.execute_calldata(bytes([v4.COMMAND_V4_SWAP]),
                                   [v4.v4_swap_input([v4.A_SWAP_EXACT_IN_SINGLE],
                                                     [params])])
        layouts[layout] = eth_call(client, v4.UNIVERSAL_ROUTER["eth"], data)
    probes = {
        "0x0b (Currency,uint256)": [v4.A_SETTLE, v4.settle_all_params(USDC, max_amount=0)],
        "0x0c (Currency,uint256)": [v4.A_SETTLE_ALL, v4.settle_all_params(USDC, max_amount=0)],
        "0x0e (Currency,uint256)": [v4.A_TAKE, v4.take_all_params(USDC, min_amount=0)],
        "0x0f (Currency,uint256)": [v4.A_TAKE_ALL, v4.take_all_params(USDC, min_amount=0)],
    }
    results = {}
    for label, (action, params) in probes.items():
        data = v4.execute_calldata(bytes([v4.COMMAND_V4_SWAP]),
                                   [v4.v4_swap_input([action], [params])])
        results[label] = eth_call(client, v4.UNIVERSAL_ROUTER["eth"], data)
    print(json.dumps({
        "selectors": {"execute": v4.EXECUTE_SELECTOR,
                      "UnsupportedAction": v4.selector("UnsupportedAction(uint256)"),
                      "SliceOutOfBounds": "0x3b99b53d",
                      "SwapAmountCannotBeZero": v4.selector("SwapAmountCannotBeZero()")},
        "swap_layout": {k: _tidy(v) for k, v in layouts.items()},
        "actions": {k: _tidy(v) for k, v in results.items()},
    }, indent=1, ensure_ascii=False))
    return 0


def _tidy(reply: dict) -> dict:
    if reply.get("ok"):
        return {"ok": True, "result": reply["result"][:40] or "0x"}
    error = reply.get("error")
    if isinstance(error, dict):
        return {"ok": False, "code": error.get("code"),
                "data": str(error.get("data") or "")[:74],
                "message": str(error.get("message") or "")[:90]}
    return {"ok": False, "error": str(error)[:120]}


def cmd_pools(client: Http, symbol: str) -> int:
    rows = []
    for pool in eth_pools(symbol):
        state = pool_state(client, pool["pool_id"])
        rows.append({"pool_id": pool["pool_id"], "tier": pool.get("tier"),
                     "fee_key": pool.get("fee_key"),
                     "tick_spacing": pool.get("tick_spacing"),
                     "hook": pool.get("hooks"),
                     "depth_usd": str(pool.get("depth_usd")),
                     "id_matches_census": pool["computed_id"] == pool["pool_id"],
                     "state": state})
    print(dump(rows))
    return 0


def cmd_quote(client: Http, symbol: str, usdt: Decimal) -> int:
    picked = pick_pair(client, symbol, usdt)
    picked.pop("_ring", None)
    print(dump(picked))
    return 0


def build_ring(picked: dict, ring: dict, *, mode: str, tolerance_bps: Decimal,
               profit_floor_raw: int = 1) -> dict:
    """The plan for one ring. ``mode`` decides whether a loss is expressible."""
    amount_in = picked["amount_in_raw"]
    sell_min = int(Decimal(ring["usdc_out"]) * (1 - tolerance_bps / Decimal(10000)))
    if mode == v4.RING_MODE_PROFIT:
        return v4.ring_calldata(
            buy_key=ring["buy_pool"]["key"], sell_key=ring["sell_pool"]["key"],
            token_address=ring["buy_pool"]["token_address"],
            quote_address=USDC, amount_in=amount_in,
            sell_amount_out_min=sell_min, profit_floor=profit_floor_raw,
            mode=mode)
    return v4.ring_calldata(
        buy_key=ring["buy_pool"]["key"], sell_key=ring["sell_pool"]["key"],
        token_address=ring["buy_pool"]["token_address"], quote_address=USDC,
        amount_in=amount_in, sell_amount_out_min=sell_min,
        profit_floor=0, loss_cap=0, mode=mode)


def cmd_ring(client: Http, symbol: str, usdt: Decimal, *, tolerance_bps: Decimal,
             mode: str) -> int:
    picked = pick_pair(client, symbol, usdt)
    ring = picked.pop("_ring", None)
    print(dump(picked))
    if ring is None:
        log("没有正价差的环：不动")
        return 1
    plan = build_ring(picked, ring, mode=mode, tolerance_bps=tolerance_bps)
    if not plan.get("executable"):
        log(f"plan 拒绝：{plan.get('reason')}")
        return 1
    log(f"ring {plan['buy_pool_id'][:12]} -> {plan['sell_pool_id'][:12]} "
        f"mode={mode} in {plan['amount_in_raw']} "
        f"sell_min {plan['sell_amount_out_min_raw']} "
        f"profit_floor {plan['profit_floor_raw']} calldata {len(plan['data'])//2} B")
    slots = discover_slots(client, USDC, v4.PERMIT2["eth"], plan["to"])
    log(f"storage slots: {slots}")
    override = allowance_override(USDC, v4.PERMIT2["eth"], plan["to"], slots,
                                  amount_raw=max(plan["amount_in_raw"], 1))
    if not override:
        log("没有找到 allowance 槽位：无法做免签模拟")
        return 1
    reply = eth_call(client, plan["to"], plan["data"], override=override)
    log(f"eth_call(ring, allowances faked): {_tidy(reply)}")
    return 0 if reply.get("ok") else 1


def cmd_allowances(client: Http, symbol: str) -> int:
    router = v4.UNIVERSAL_ROUTER["eth"]
    token = eth_pools(symbol)[0]["token_address"]
    print(json.dumps({"wallet": WALLET, "usdc": USDC, "token": token,
                      "permit2": v4.PERMIT2["eth"], "router": router,
                      "allowances": read_allowances(client, USDC, v4.PERMIT2["eth"], router)},
                     indent=1, ensure_ascii=False))
    return 0


def broadcast(to: str, data: str) -> dict:
    return onchainos(["wallet", "contract-call", "--chain", CHAIN, "--to", to,
                      "--input-data", data])


def gateway_simulate(to: str, data: str) -> dict:
    payload = onchainos(["gateway", "simulate", "--chain", CHAIN, "--from", WALLET,
                         "--to", to, "--data", data, "--amount", "0"])
    row = payload.get("data") or [{}]
    row = row[0] if isinstance(row, list) and row else row
    return row if isinstance(row, dict) else {"raw": payload}


def cmd_approve(client: Http, symbol: str, usdt: Decimal, confirm: bool) -> int:
    router = v4.UNIVERSAL_ROUTER["eth"]
    amount = int(usdt * Decimal(10) ** USDC_DECIMALS) * 4      # headroom for retries
    now = int(time.time())
    steps = [
        ("USDC -> Permit2", USDC, v4.erc20_approve_calldata(v4.PERMIT2["eth"], amount)),
        ("Permit2 -> UniversalRouter", v4.PERMIT2["eth"],
         v4.permit2_approve_calldata(USDC, router, amount, now + 30 * 24 * 3600)),
    ]
    for label, to, data in steps:
        log(f"{label}: to={to} data={data[:18]}…")
        if not confirm:
            continue
        sim = gateway_simulate(to, data)
        log(f"  simulate: {json.dumps(sim, ensure_ascii=False)[:200]}")
        if sim.get("failReason"):
            log("  模拟失败，停止")
            return 1
        out = broadcast(to, data)
        log(f"  broadcast: {json.dumps(out, ensure_ascii=False)[:200]}")
        if not out.get("ok"):
            return 1
    log(f"allowances now: {read_allowances(client, USDC, v4.PERMIT2['eth'], router)}")
    return 0


def cmd_once(client: Http, symbol: str, usdt: Decimal, tolerance_bps: Decimal,
             mode: str, confirm: bool) -> int:
    picked = pick_pair(client, symbol, usdt)
    ring = picked.pop("_ring", None)
    print(dump(picked))
    if ring is None:
        log("没有正价差的环：不动")
        return 1
    plan = build_ring(picked, ring, mode=mode, tolerance_bps=tolerance_bps)
    if not plan.get("executable"):
        log(f"plan 拒绝：{plan.get('reason')}")
        return 1
    log(f"1 笔 execute：mode={mode} in {plan['amount_in_raw']} raw / "
        f"sell_min {plan['sell_amount_out_min_raw']} raw / "
        f"profit_floor {plan['profit_floor_raw']} raw / "
        f"dirs buy={plan['buy_zero_for_one']} sell={plan['sell_zero_for_one']}")
    allowances = read_allowances(client, USDC, v4.PERMIT2["eth"], plan["to"])
    log(f"allowances: {allowances}")
    if mode == v4.RING_MODE_GROSS and not (
            (allowances["erc20_to_permit2"] or 0) >= plan["amount_in_raw"]
            and (allowances["permit2_to_router"] or {}).get("amount", 0) >= plan["amount_in_raw"]):
        log("gross 形态需要两步授权，先跑 approve（或改用 --mode profit）")
        return 1
    if not confirm:
        log("dry run：加 --confirm-live 才会广播")
        return 0
    sim = gateway_simulate(plan["to"], plan["data"])
    log(f"simulate: {json.dumps(sim, ensure_ascii=False)[:300]}")
    if sim.get("failReason"):
        log("模拟失败：不广播")
        return 1
    out = broadcast(plan["to"], plan["data"])
    log(f"broadcast: {json.dumps(out, ensure_ascii=False)[:300]}")
    tx = str(((out.get("data") or {}) if isinstance(out.get("data"), dict) else {}).get("txHash") or "")
    if not tx.startswith("0x"):
        return 1
    log(f"tx {tx}")
    for _ in range(30):
        receipt = rpc(client, "eth_getTransactionReceipt", [tx]).get("result")
        if receipt:
            print(json.dumps(receipt, ensure_ascii=False)[:600])
            return 0 if receipt.get("status") == "0x1" else 1
        time.sleep(3)
    log("receipt 未在 90 s 内出现：状态未知，按未知处理")
    return 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("actions", "pools", "quote", "ring",
                                            "allowances", "approve", "once"))
    parser.add_argument("--symbol", default="SPCXx")
    parser.add_argument("--usdt", default="25")
    parser.add_argument("--tolerance-bps", default="5")
    parser.add_argument("--mode", default=v4.RING_MODE_PROFIT,
                        choices=(v4.RING_MODE_PROFIT, v4.RING_MODE_GROSS))
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args()
    client = http()
    usdt = Decimal(args.usdt)
    tolerance = Decimal(args.tolerance_bps)
    if args.command == "actions":
        return cmd_actions(client)
    if args.command == "pools":
        return cmd_pools(client, args.symbol)
    if args.command == "quote":
        return cmd_quote(client, args.symbol, usdt)
    if args.command == "ring":
        return cmd_ring(client, args.symbol, usdt, tolerance_bps=tolerance,
                        mode=args.mode)
    if args.command == "allowances":
        return cmd_allowances(client, args.symbol)
    if args.command == "approve":
        return cmd_approve(client, args.symbol, usdt, args.confirm_live)
    return cmd_once(client, args.symbol, usdt, tolerance, args.mode,
                    args.confirm_live)


if __name__ == "__main__":
    raise SystemExit(main())
