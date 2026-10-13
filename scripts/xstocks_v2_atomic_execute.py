#!/usr/bin/env python3
"""Stage 2: one transaction, two pools, or nothing.

The rule (architecture section 6): a same-chain buy and sell must be **one**
transaction. Two swaps are not this trade -- the gap between them is a naked
position, and if the second leg fails the first one cannot be undone. The only
shape that proves "one call" is a circular route: same token in and out, with
both pool ids present in the router's own path (`atomic.route_is_single_call`).

    scripts/xstocks_v2_atomic_execute.py plan
    scripts/xstocks_v2_atomic_execute.py plan --symbol SPYx
    scripts/xstocks_v2_atomic_execute.py once --symbol SPYx --confirm-live

Read-only unless ``once --confirm-live``. Today it refuses almost everything,
and that is the honest answer: our toolchain cannot produce a circular quote
(the aggregator refuses ``from == to``), so no row carries a route proof, and on
top of that X Layer has only one liquid pool per xStock (the second one is
``liquidity == 0``). The calldata and the simulation are ready for the day a row
does appear; what this script must never do is fall back to two swaps.
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

from arb_monitor.xstocks_v2 import atomic, costs          # noqa: E402
from arb_monitor.xstocks_v2 import policy as policy_mod   # noqa: E402
from arb_monitor.xstocks_v2.feeds import evm              # noqa: E402
from arb_monitor.xstocks_v2.feeds.http import Http        # noqa: E402

DATA = ROOT / "data-v2"
SCAN = DATA / "xstocks_v2_scan.json"
PROXY = "http://127.0.0.1:4780"
EVM_WALLET = "0xabababababababababababababababababababab"
GAS_TOKEN_XLAYER = "0xe538905cf8410324e03a5a23c1c177a474d59b2b"   # WOKB


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def atomic_rows(symbol: str | None = None) -> list[dict]:
    try:
        snapshot = json.loads(SCAN.read_text())
    except (OSError, ValueError):
        return []
    return [row for row in snapshot.get("rows") or []
            if row.get("family") == "dex_to_dex"
            and (symbol is None or str(row.get("symbol")) == symbol)]


def build_plan(row: dict, *, policy, available_usdt=None,
               gas_usdt: Decimal | None = Decimal(0)) -> dict:
    """The atomic plan for one scan row, including its calldata, or a refusal.

    Pure: everything it needs is in the row, the policy and the arguments, so the
    "must be one call" rule can be tested without a chain.
    """
    symbol = str(row.get("symbol") or "")
    chain = str(row.get("chain") or "")
    buy_pool = str(row.get("buy_pool") or row.get("pool_id") or "")
    sell_pool = str(row.get("sell_pool") or "")
    notional = costs.judged_notional(row.get("notional_usdt"), available_usdt)
    plan = atomic.plan(
        symbol=symbol, chain=chain, buy_pool=buy_pool, sell_pool=sell_pool,
        edge_bps=row.get("edge_t0_bps"), notional_usdt=notional,
        gas_usdt=gas_usdt, policy=policy,
        route_proof=row.get("route_proof"),
        from_token=str(row.get("from_token") or row.get("quote_address") or ""),
        to_token=str(row.get("to_token") or row.get("quote_address") or ""))
    if not plan.get("executable"):
        return plan
    hops = row.get("hops")
    if not hops:
        return {**plan, "executable": False, "blockers": ["no_hops"],
                "reason": "no_hops"}
    router = atomic.SWAP_ROUTER_02.get(chain, "")
    if not router:
        return {**plan, "executable": False, "blockers": ["no_router"],
                "reason": "no_router"}
    amount_in = int(Decimal(str(notional)) * Decimal(10 ** 6))   # USDG, 6 dp
    calldata = atomic.ring_calldata(
        start=row.get("quote_address") or "",
        hops=[(token, int(fee)) for token, fee in hops],
        router=router, owner=EVM_WALLET, amount_in=amount_in,
        amount_out_min=int(amount_in * (1 - Decimal(str(
            getattr(policy, "max_slippage_bps", 10))) / Decimal(10000))))
    return {**plan, "calldata": calldata, "router": router,
            "amount_in_raw": amount_in}


def _rpc(http, method: str, params: list) -> dict:
    _status, payload = http.post_json("https://rpc.xlayer.tech", {
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    return payload if isinstance(payload, dict) else {}


def simulate(http, calldata: dict) -> dict:
    """``eth_call`` the whole ring: one call, or the transaction does not exist."""
    payload = _rpc(http, "eth_call", [
        {"from": EVM_WALLET, "to": calldata["to"], "data": calldata["data"]},
        "latest"])
    if payload.get("error"):
        return {"ok": False, "reason": str(payload["error"])[:200]}
    return {"ok": True, "result": str(payload.get("result"))[:80]}


def onchainos(args: list[str], timeout: int = 240) -> dict:
    env = dict(os.environ, HTTPS_PROXY=PROXY, HTTP_PROXY=PROXY)
    done = subprocess.run(["onchainos"] + args, capture_output=True, text=True,
                          env=env, timeout=timeout)
    try:
        return json.loads(done.stdout)
    except ValueError:
        return {"ok": False, "error": (done.stdout or done.stderr)[:400]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("plan", "once"))
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args()
    policy = policy_mod.V2Policy.load()
    rows = atomic_rows(args.symbol)
    if not rows:
        log("没有 dex_to_dex 行：X Layer 每个 xStock 只有一口有流动性的池，"
            "第二口 liquidity = 0（2026-10-07 实测）")
        return 0
    for row in rows:
        plan = build_plan(row, policy=policy)
        log(f"{row.get('symbol')} {row.get('chain')} executable={plan.get('executable')} "
            f"blockers={plan.get('blockers')}")
        if not plan.get("executable"):
            continue
        if args.mode == "plan":
            print(json.dumps(plan, indent=1, ensure_ascii=False)[:1200])
            continue
        if not args.confirm_live:
            raise SystemExit("once needs --confirm-live")
        http = Http(proxy=PROXY)
        sim = simulate(http, plan["calldata"])
        log(f"simulate: {sim}")
        if not sim["ok"]:
            return 1
        out = onchainos(["wallet", "contract-call", "--chain", "xlayer",
                         "--to", plan["calldata"]["to"],
                         "--input-data", plan["calldata"]["data"],
                         "--from", EVM_WALLET])
        log(f"broadcast: ok={out.get('ok')} "
            f"tx={(out.get('data') or {}).get('txHash')}")
        return 0 if out.get("ok") else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
