#!/usr/bin/env python3
"""Generic bridge route for pairs the named roundtrips do not already drive.

    scripts/xstocks_v2_route.py plan --source eth --dest x-layer
    scripts/xstocks_v2_route.py once --source x-layer --dest bsc --confirm-live

``plan`` never signs. ``once`` broadcasts only when the edge is ``ready``, the
quoted cost is known, and that cost is at most 2 USDT. A ``no_lane`` edge has
no broadcast call.
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.xstocks_v2 import capability as cap          # noqa: E402
from arb_monitor.xstocks_v2 import routes                      # noqa: E402
from arb_monitor.xstocks_v2.ccip_evm import build_call_evm     # noqa: E402
from arb_monitor.xstocks_v2.ccip_solana import instruction_for_dest  # noqa: E402

EVM_RECEIVER = "0xabababababababababababababababababababab"
FEE_TOKEN = "0x90a2a4c76b5d8c0bc892a69ea28aa775a8f2dd48"
# One xStock unit at 18 decimals, small enough that a reverted bridge cannot
# move a position. The proof still pays the message fee, which is what the
# 2 USDT cap is measured against.
MIN_AMOUNT = 10 ** 15


def construct(*, source: str, dest: str, token: str = FEE_TOKEN,
              amount: int = MIN_AMOUNT, receiver: str = EVM_RECEIVER,
              accounts: tuple = ()) -> dict:
    """Build the bridge payload, or refuse. ``broadcast`` is always false here."""
    status = cap.bridge_status(source, dest)
    kind = routes.dialect(source, dest)
    base = {"status": status, "dialect": kind, "broadcast": False,
            "payload": None, "reason": ""}
    if status != cap.READY:
        base["reason"] = status
        return base
    if kind == "evm_to_evm":
        data, _call = build_call_evm(
            source_chain=source, dest_chain=dest, token=token,
            amount=int(amount), receiver=receiver)
        base["payload"] = data
        return base
    if kind == "solana_source":
        base["payload"] = instruction_for_dest(
            dest_chain=dest, recipient=receiver, product_id=1,
            amount=int(amount)).hex()
        return base
    if kind == "evm_to_solana":
        if not accounts:
            base["reason"] = "accounts_required"
            return base
        from arb_monitor.xstocks_v2.ccip_evm import build_call
        data, _call = build_call(
            source_chain=source, token=token, amount=int(amount),
            receiver=receiver, accounts=accounts)
        base["payload"] = data
        return base
    base["status"] = cap.NO_LANE
    base["reason"] = cap.NO_LANE
    return base


def candidates(snapshot: dict, *, limit: int = 3) -> list[dict]:
    """chain → chain rows this route may look at. The two covered pairs stay out."""
    informational = {"phase_scan", "chain_out_of_phase", "unmeasured_route",
                     "bridge_unmeasured_symbol", "cost_unknown", "below_threshold"}
    out = []
    for row in snapshot.get("rows") or []:
        if row.get("family") != "chain_to_chain":
            continue
        source, dest = str(row.get("source_chain") or ""), str(row.get("dest_chain") or "")
        if (routes.norm(source), routes.norm(dest)) in cap.COVERED_PAIRS:
            continue
        if cap.bridge_status(source, dest) != cap.READY:
            continue
        blockers = set(row.get("blockers") or []) - informational
        if blockers:
            continue
        out.append(row)
    out.sort(key=lambda row: Decimal(str(row.get("edge_t0_bps") or "0")), reverse=True)
    return out[:limit]


#: The stablecoin a paper requote spends and receives. Decimals are the token's
#: own. A chain absent from this table cannot be requoted, and a paper cycle
#: refuses it instead of guessing a token.
PAPER_QUOTES = {
    "x-layer": ("0x4ae46a509f6b1d9056937ba4500cb143933d2dc8", 6),
    "eth": ("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6),
    "bsc": ("0x55d398326f99059ff775485246999027b3197955", 18),
}
ONCHAINOS_CHAIN = {"x-layer": "xlayer", "eth": "ethereum", "bsc": "bsc"}
PAPER_LEDGER = ROOT / "data-v2" / "xstocks_v2_chain_paper.jsonl"


def token_on_evm(symbol: str) -> str:
    """The EVM deployment address. xStocks use one address on every EVM chain."""
    import sqlite3
    conn = sqlite3.connect(f"file:{ROOT / 'data' / 'xstocks_book.sqlite'}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT products_json FROM bridges WHERE source_network = 'Solana'").fetchone()
    finally:
        conn.close()
    for product in json.loads(row[0]):
        if str(product.get("symbol")) != symbol:
            continue
        for deployment in product.get("deployments") or []:
            if deployment.get("network") == "XLayer":
                return str(deployment["address"]).lower()
    raise ValueError(f"{symbol} has no EVM deployment")


def _default_quote(chain: str, src: str, dst: str, amount: int) -> int | None:
    import os
    import subprocess
    env = dict(os.environ, HTTPS_PROXY="http://127.0.0.1:4780", HTTP_PROXY="http://127.0.0.1:4780")
    done = subprocess.run(
        ["onchainos", "swap", "quote", "--chain", ONCHAINOS_CHAIN[chain],
         "--from", src, "--to", dst, "--amount", str(amount), "--swap-mode", "exactIn"],
        capture_output=True, text=True, timeout=60, env=env)
    payload = None
    for line in reversed((done.stdout or "").splitlines()):
        if line.strip().startswith("{"):
            payload = json.loads(line)
            break
    if not payload or not payload.get("ok"):
        return None
    rows = payload.get("data") or [{}]
    row = rows[0] if isinstance(rows, list) else rows
    got = row.get("toTokenAmount")
    return int(got) if got else None


def paper_once(*, symbol: str, source: str, dest: str, notional: str = "25",
               bridge_fee_usd=None, gas_usd=None, quote_fn=None,
               token: str | None = None) -> dict:
    """Requote one chain → chain row. Never signs.

    Buy ``notional`` of the source stablecoin, sell the tokens the quote
    returns, then subtract the bridge fee and gas the scan already priced.
    """
    source, dest = routes.norm(source), routes.norm(dest)
    summary = {"symbol": symbol, "source": source, "dest": dest, "state": "refused",
               "reason": "", "held_at": "", "broadcast": False, "notional": notional}
    buy_quote, sell_quote = PAPER_QUOTES.get(source), PAPER_QUOTES.get(dest)
    if buy_quote is None or sell_quote is None:
        summary["reason"] = "no_paper_quote_token"
        return summary
    status = cap.bridge_status(source, dest)
    if status != cap.READY:
        summary["reason"] = status
        return summary
    try:
        spend = Decimal(str(notional))
        fee = Decimal(str(bridge_fee_usd)) if bridge_fee_usd not in (None, "") else None
        gas = Decimal(str(gas_usd)) if gas_usd not in (None, "") else Decimal(0)
    except (ArithmeticError, ValueError):
        summary["reason"] = "bad_number"
        return summary
    if fee is None:
        summary["reason"] = "cost_unknown"
        return summary
    try:
        token = token or token_on_evm(symbol)
    except (OSError, ValueError, TypeError) as exc:
        summary["reason"] = f"no_token:{exc}"
        return summary
    quote = quote_fn or _default_quote
    bought = quote(source, buy_quote[0], token, int(spend * Decimal(10 ** buy_quote[1])))
    if not bought:
        summary["reason"] = "buy_quote_failed"
        return summary
    sold = quote(dest, token, sell_quote[0], int(bought))
    if not sold:
        summary["reason"] = "sell_quote_failed"
        return summary
    proceeds = Decimal(sold) / Decimal(10 ** sell_quote[1])
    net = proceeds - spend - fee - gas
    summary.update(state="paper", reason=f"net:{format(net.quantize(Decimal('0.0001')), 'f')}",
                   proceeds=format(proceeds, "f"),
                   bridge_fee_usd=format(fee, "f"), gas_usd=format(gas, "f"),
                   net_usdt=format(net, "f"), tokens_raw=str(bought))
    try:
        PAPER_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with PAPER_LEDGER.open("a") as handle:
            handle.write(json.dumps({"ts": int(__import__("time").time()), **summary}) + "\n")
    except OSError:
        pass
    return summary


def cycle_once(*, symbol: str, source: str, dest: str,
               confirm_live: bool = False, cost_usdt=None,
               broadcast=None) -> dict:
    """One route. Signs only inside ``capability.attempt``."""
    built = construct(source=source, dest=dest)
    steps = cap.compose(source, dest)
    summary = {
        "symbol": symbol, "source": routes.norm(source), "dest": routes.norm(dest),
        "state": "refused", "reason": built["reason"] or built["status"],
        "held_at": "", "status": built["status"], "steps": list(steps["steps"]),
        "dialect": built["dialect"], "broadcast": False,
    }
    if built["status"] != cap.READY or not built.get("payload"):
        return summary
    if not confirm_live:
        summary["state"] = "planned"
        summary["reason"] = "plan_only"
        return summary

    def _send():
        if broadcast is not None:
            broadcast(built["payload"])
        summary["broadcast"] = True

    result = cap.attempt(status=built["status"], cost_usdt=cost_usdt, broadcast=_send)
    summary["broadcast"] = bool(result["sent"])
    if result["sent"]:
        summary["state"] = "sent"
        summary["reason"] = "min_proof"
        summary["held_at"] = "in_bridge"
    else:
        summary["reason"] = result["reason"]
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "once"))
    parser.add_argument("--source", required=True)
    parser.add_argument("--dest", required=True)
    parser.add_argument("--symbol", default="SPYx")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--cost-usdt", default=None)
    args = parser.parse_args()
    summary = cycle_once(
        symbol=args.symbol, source=args.source, dest=args.dest,
        confirm_live=args.mode == "once" and args.confirm_live,
        cost_usdt=args.cost_usdt)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0 if summary["state"] in ("planned", "sent", "refused") else 1


if __name__ == "__main__":
    raise SystemExit(main())
