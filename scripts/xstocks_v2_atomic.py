#!/usr/bin/env python3
"""Stage-2 planner: same-chain two-pool round trips, one transaction or nothing.

    scripts/xstocks_v2_atomic.py plan [--symbol SPYx] [--chain x-layer] [--probe]

``plan`` is read-only. It lists the same-chain two-pool candidates the scanner
found and runs them through ``arb_monitor.xstocks_v2.atomic``. Every one of them
comes back refused, because the proof a single transaction needs -- a circular
route that visits both pools -- does not exist yet.

``--probe`` asks the aggregator for that circular route and prints its answer,
so the reason is evidence rather than an assumption. Measured 2026-10-07: the
aggregator refuses ``USDG -> USDG`` outright ("Cannot swap a token to itself"),
which is why this family needs a router contract before it can trade at all.
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

from arb_monitor.xstocks_v2 import atomic, scan                  # noqa: E402
from arb_monitor.xstocks_v2.policy import V2Policy               # noqa: E402

DATA = ROOT / "data-v2"
PROXY = "http://127.0.0.1:4780"
USDG_XLAYER = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"


def onchainos(args: list[str], timeout: int = 120) -> dict:
    env = dict(os.environ, HTTPS_PROXY=PROXY, HTTP_PROXY=PROXY)
    done = subprocess.run(["onchainos"] + args, capture_output=True, text=True,
                          env=env, timeout=timeout)
    try:
        return json.loads(done.stdout)
    except ValueError:
        return {"ok": False, "error": (done.stdout or done.stderr)[:400]}


def candidates(*, symbol: str | None, chain: str | None) -> list[dict]:
    snapshot = json.loads((DATA / "xstocks_v2_scan.json").read_text())
    rows = [row for row in (snapshot.get("rows") or [])
            if row.get("family") == "dex_to_dex"]
    if symbol:
        rows = [row for row in rows if row.get("symbol") == symbol]
    if chain:
        rows = [row for row in rows if str(row.get("chain")) == chain]
    rows.sort(key=lambda row: Decimal(str(row.get("edge_t0_bps") or 0)), reverse=True)
    return rows


def probe_circular(token: str, notional_usd: Decimal, *, unit: int) -> dict:
    """Ask for quote-token -> quote-token and report exactly what comes back."""
    return onchainos(["swap", "quote", "--chain", "xlayer", "--from", token,
                      "--to", token, "--amount", str(int(notional_usd * unit)),
                      "--swap-mode", "exactIn"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("plan",), nargs="?", default="plan")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--chain", default=None)
    parser.add_argument("--notional", default="200")
    parser.add_argument("--gas-usdt", default=None,
                        help="measured gas for the pair; unknown is a refusal")
    parser.add_argument("--probe", action="store_true",
                        help="ask the aggregator for the circular route (read-only)")
    args = parser.parse_args()

    policy = V2Policy.load()
    rows = candidates(symbol=args.symbol, chain=args.chain)
    print(f"same-chain two-pool candidates: {len(rows)}")
    for row in rows:
        print(f"  {row.get('symbol'):8s} {row.get('chain'):8s} "
              f"{str(row.get('buy_pool'))[:10]} -> {str(row.get('sell_pool'))[:10]} "
              f"edge {row.get('edge_t0_bps')} bps  blockers={row.get('blockers')}")
    if not rows:
        return 0

    proof = None
    if args.probe:
        answer = probe_circular(USDG_XLAYER, Decimal(args.notional), unit=10 ** 6)
        print(f"circular probe ok={answer.get('ok')} error={str(answer.get('error'))[:160]}")
        data = answer.get("data") or []
        proof = data[0] if isinstance(data, list) and data else None

    top = rows[0]
    plan = atomic.plan(
        symbol=str(top.get("symbol")), chain=str(top.get("chain")),
        buy_pool=str(top.get("buy_pool")), sell_pool=str(top.get("sell_pool")),
        edge_bps=top.get("edge_t0_bps"),
        notional_usdt=Decimal(args.notional),
        gas_usdt=None if args.gas_usdt is None else Decimal(args.gas_usdt),
        policy=policy, route_proof=proof,
        from_token=USDG_XLAYER, to_token=USDG_XLAYER)
    print(json.dumps(plan, ensure_ascii=False, indent=1))
    if not plan["executable"]:
        print("stage 2 不能以两条腿执行：要么一次交易，要么不做。"
              "当前没有『一次调用走两口池』的证明，所以拒绝。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
