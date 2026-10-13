#!/usr/bin/env python3
"""Minimal real probe of ONE Solana pool: buy the smallest lot, sell it back.

    scripts/xstocks_v2_solana_probe.py plan --symbol KLACx --usdc 2
    scripts/xstocks_v2_solana_probe.py once --symbol KLACx --usdc 2 --confirm-live

Why this exists (operator 2026-10-08: "KLACX 用最小额真走一次"): that morning
KLACx quoted a sell price of 758.50 against a Kraken bid of 194.37 -- +26,164 bps
with an empty blocker list. A quote is one observation and a second quote just
repeats it, so the only way to tell a real premium from a broken pool is to cross
it with the smallest amount that is worth a transaction and print what arrived.

No Kraken leg: the USDC starts and ends in the Solana wallet, so the probe needs
no deposit address and no Kraken balance. Everything that signs goes through the
same clients the resident chain uses (``SwapClient`` on the keyed v2 feed for the
quote and the swap transaction, v1's ``Solana``/``Wallet`` for the RPC and the
signature). The loss is bounded by the amount spent: the probe *buys*, so the
tokens are what is at risk.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from arb_monitor.xstocks_v2 import cycle, ledger                      # noqa: E402
from arb_monitor.xstocks_v2 import live                               # noqa: E402
from arb_monitor.xstocks_v2.feeds import census                       # noqa: E402
from arb_monitor.xstocks_v2.feeds.http import Http                    # noqa: E402
from arb_monitor.xstocks_v2.feeds.jupiter import (  # noqa: E402
    JupiterFeed, SwapClient, build_swap_retry)

import xstocks_auto_live as v1                                        # noqa: E402

DATA = ROOT / "data-v2"
LOCK = DATA / "xstocks_v2_cycle.lock"
HALT = DATA / "xstocks_v2_HALT"
KILL = ROOT / "data" / "KILL_SWITCH"      # the one switch that stops everything
RESULT = DATA / "xstocks_v2_solana_probe.json"
USDC_SOLANA = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
QUOTE_DECIMALS = 6
TOKEN_DECIMALS = 8
WAIT_S = 60


def round_trip_numbers(*, before_raw: int, after_raw: int, spend_raw: int,
                       decimals: int = QUOTE_DECIMALS) -> dict:
    """一次往返的三个数：花了多少、卖回多少、净赚/亏多少。

    **净利以余额差为准**（``after − before``）：买入这笔已经在余额里扣过一次，
    再减一次 ``spend`` 就是把它算两遍 —— 2026-10-08 的 KLACx / PFEx 两次 2 USDC
    往返因此各记成 ``−4``，而当天真实余额只动了 ``−0.0064 USDC``（见 A37）。
    ``received`` 是**卖腿回来的量**（含本金），所以它应当接近 ``spend``。
    """
    spend = Decimal(spend_raw) / Decimal(10 ** decimals)
    delta = Decimal(after_raw - before_raw) / Decimal(10 ** decimals)
    received = delta + spend
    return {
        "spend_usdc": format(spend, "f"),
        "received_usdc": format(received, "f"),
        "net_usdc": format(delta, "f"),
        "round_trip_bps": (format((spend - received) / spend * 10000, "f")
                           if spend > 0 else None),
    }


def log(note: str) -> None:
    print(note, flush=True)


def market(symbol: str) -> dict:
    row = census.solana_universe([symbol]).get(symbol)
    if not row or not row.get("token_address"):
        raise SystemExit(f"{symbol}: no Solana mint in the issuer table")
    return row


def plan(*, symbol: str, usdc: Decimal) -> dict:
    """Quote both legs and check the gates. Never signs."""
    if KILL.exists():
        raise SystemExit("KILL_SWITCH exists: nothing moves, including a probe")
    row = market(symbol)
    http = Http()
    sol = v1.Solana(v1.Http())
    client = SwapClient(JupiterFeed(http))
    cash = sol.raw_balance(USDC_SOLANA)
    spend_raw = min(int(usdc * 10 ** QUOTE_DECIMALS), cash)
    if spend_raw <= 0:
        raise SystemExit(f"no USDC on Solana to probe with (raw balance {cash})")
    buy = client.quote(USDC_SOLANA, row["token_address"], spend_raw, 50)
    if not buy:
        raise SystemExit(f"{symbol}: the buy leg has no route for {spend_raw} raw USDC")
    bought_raw = int(buy["outAmount"])
    sell = client.quote(row["token_address"], USDC_SOLANA, bought_raw, 50)
    spend = Decimal(spend_raw) / Decimal(10 ** QUOTE_DECIMALS)
    tokens = Decimal(bought_raw) / Decimal(10 ** TOKEN_DECIMALS)
    out = {
        "symbol": symbol, "mode": "plan", "usdc_balance": str(cash),
        "spend_raw": spend_raw, "spend_usdc": format(spend, "f"),
        "tokens": format(tokens, "f"),
        "buy_price_usd": format(spend / tokens, "f") if tokens > 0 else None,
        "buy_impact": buy.get("priceImpactPct"),
        "sell_route": bool(sell),
        "sell_usdc": None, "sell_price_usd": None, "round_trip_bps": None,
        "sol_lamports": sol.lamports(),
    }
    if sell:
        got = Decimal(int(sell["outAmount"])) / Decimal(10 ** QUOTE_DECIMALS)
        out["sell_usdc"] = format(got, "f")
        out["sell_price_usd"] = format(got / tokens, "f") if tokens > 0 else None
        out["round_trip_bps"] = format((spend - got) / spend * 10000, "f")
        out["sell_impact"] = sell.get("priceImpactPct")
    return out


def _wait_balance(sol, mint: str, before: int) -> int | None:
    for _ in range(int(WAIT_S / 3) + 1):
        now = sol.raw_balance(mint)
        if now != before:
            return now
        time.sleep(3)
    return None


def _swap(client, sol, wallet, *, src: str, dst: str, raw_in: int,
          label: str) -> dict:
    """quote -> build -> simulate -> broadcast. No retry after a broadcast."""
    quote = client.quote(src, dst, raw_in, 50)
    if not quote:
        return {"ok": False, "reason": f"{label}:no_route"}
    # A38: a single 429 on /swap used to abort a probe that had already quoted
    # both legs (measured 2026-10-08 21:3x on PFEx: the same call answered 200
    # a minute later). Retry the build; nothing has been signed yet.
    swap, note = build_swap_retry(client, quote)
    tx = swap.get("swapTransaction")
    if not tx:
        return {"ok": False, "reason": f"{label}:swap_build_failed:{note}"}
    sim = sol.simulate(tx)
    if not sim["ok"]:
        return {"ok": False, "reason": f"{label}:simulate:{sim['err']}"}
    res = wallet.swap(tx)
    if not res["ok"]:
        return {"ok": False, "reason": f"{label}:broadcast:{res['error']}",
                "unknown": True}
    return {"ok": True, "tx": res.get("tx"), "out_raw": int(quote["outAmount"]),
            "impact": quote.get("priceImpactPct")}


def once(*, symbol: str, usdc: Decimal, confirm_live: bool) -> int:
    if not confirm_live:
        raise SystemExit("once needs --confirm-live (this signs real transactions)")
    prepared = plan(symbol=symbol, usdc=usdc)
    log(json.dumps(prepared, ensure_ascii=False))
    if KILL.exists():
        raise SystemExit("KILL_SWITCH exists: refusing to move money")
    row = market(symbol)
    http = Http()
    sol = v1.Solana(v1.Http())
    wallet = v1.Wallet()
    client = SwapClient(JupiterFeed(http))
    result = {"symbol": symbol, "mode": "once", "plan": prepared, "legs": []}
    with cycle.CycleLock(LOCK, HALT, label=f"{symbol} solana probe",
                         symbol=symbol, family="solana_probe"):
        spend_raw = min(int(usdc * 10 ** QUOTE_DECIMALS),
                        sol.raw_balance(USDC_SOLANA))
        before_usdc = sol.raw_balance(USDC_SOLANA)
        before_token = sol.raw_balance(row["token_address"])
        buy = _swap(client, sol, wallet, src=USDC_SOLANA,
                    dst=row["token_address"], raw_in=spend_raw, label="buy")
        result["legs"].append({"leg": "buy", **buy})
        if not buy["ok"]:
            result["outcome"] = "stopped_before_buy"
            ledger.record(ledger.row(symbol=symbol, chain="solana",
                                     family="solana_probe", state=ledger.STOPPED,
                                     mode="live", size_usdt=format(usdc, "f"),
                                     reason=buy["reason"]),
                          DATA / "xstocks_v2_cycles.jsonl")
            RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=1))
            log(json.dumps(result, ensure_ascii=False))
            return 1
        held = _wait_balance(sol, row["token_address"], before_token)
        result["held_raw"] = held
        if held is None:
            result["outcome"] = "bought_but_unconfirmed"
            RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=1))
            log(json.dumps(result, ensure_ascii=False))
            return 1
        got_raw = held - before_token
        sell = _swap(client, sol, wallet, src=row["token_address"],
                     dst=USDC_SOLANA, raw_in=got_raw, label="sell")
        result["legs"].append({"leg": "sell", **sell})
        after_usdc = sol.raw_balance(USDC_SOLANA)
        # 净利以余额差为准；received 是卖腿回来的量（A37：以前这里把 spend 扣了两次）。
        numbers = round_trip_numbers(before_raw=before_usdc, after_raw=after_usdc,
                                     spend_raw=spend_raw)
        result.update({
            "usdc_before": str(before_usdc), "usdc_after": str(after_usdc),
            **numbers,
            "outcome": "sold" if sell["ok"] else "bought_only",
        })
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    ledger.record(ledger.row(symbol=symbol, chain="solana", family="solana_probe",
                             state=ledger.SOLD if sell["ok"] else ledger.STOPPED,
                             mode="live", size_usdt=format(spend, "f"),
                             tokens=format(Decimal(got_raw) / Decimal(10 ** TOKEN_DECIMALS), "f"),
                             net_usdt=numbers["net_usdc"],
                             reason="solana probe round trip",
                             costs={"received_usdt": numbers["received_usdc"]}),
                  DATA / "xstocks_v2_cycles.jsonl")
    log(json.dumps(result, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("plan", "once"))
    parser.add_argument("--symbol", default="KLACx")
    parser.add_argument("--usdc", default="2")
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args()
    if args.mode == "plan":
        log(json.dumps(plan(symbol=args.symbol, usdc=Decimal(args.usdc)),
                       ensure_ascii=False))
        return 0
    return once(symbol=args.symbol, usdc=Decimal(args.usdc),
                confirm_live=args.confirm_live)


if __name__ == "__main__":
    raise SystemExit(main())
