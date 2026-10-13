#!/usr/bin/env python3
"""Fourth family: buy on Solana, bridge to X Layer, sell on X Layer.

    scripts/xstocks_v2_solana_bridge_roundtrip.py plan
    scripts/xstocks_v2_solana_bridge_roundtrip.py simulate
    scripts/xstocks_v2_solana_bridge_roundtrip.py once --confirm-live

Why this family exists: it is the direction the scan keeps showing as positive
(``SPYx solana->x-layer``, buy on Jupiter, sell into the X Layer wrapper pool),
and until now **no executor claimed it**. The other three are:

* ``chain_to_kraken``          buy on a chain, sell into Kraken
* ``kraken_via_bridge_to_chain`` Kraken buy -> withdraw to a hub -> bridge -> sell on a chain
* ``xlayer_via_bridge_to_solana`` buy on X Layer -> bridge -> sell on Solana

Every leg here is already-proven code: the Solana buy is the same aggregator
call the reverse loop uses, the bridge is ``ccip_solana`` (27 accounts, the
dialect verified against four mainnet samples), and the sell is the X Layer
two-hop route the forward loop uses. This file only supplies the wiring.

One rule it must not break: **the bridge is not atomic**. Between the buy and
the sell the position is naked for the whole crossing (measured 2026-10-07:
solana -> x-layer about 2 minutes, the other way 22-27 minutes). A spread
measured before the crossing is a reference, not a promise, so the operator
still owns the decision to spend on it.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.xstocks_v2 import cycle, executor as ex, ledger   # noqa: E402
from arb_monitor.xstocks_v2 import (incident, live,               # noqa: E402
                                    notify as v2notify, policy as policy_mod)
from arb_monitor.xstocks_v2 import scan                            # noqa: E402

DATA = ROOT / "data-v2"
LOCK = DATA / "xstocks_v2_cycle.lock"
HALT = DATA / "xstocks_v2_HALT"
LEDGER = DATA / "xstocks_v2_cycles.jsonl"
PROXY = "http://127.0.0.1:4780"
FAMILY = live.SOLANA_VIA_BRIDGE_TO_XLAYER
USDC_SOLANA = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_SOLANA = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
WALLET = "AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The bridge and sell legs are the forward loop's legs, unchanged.
FWD = _module("v2fwd", ROOT / "scripts" / "xstocks_v2_xlayer_roundtrip.py")
# The Solana-side helpers (USDC balance, raw balance, ladder quoting) are the
# reverse loop's, unchanged.
REV = _module("v2rev", ROOT / "scripts" / "xstocks_v2_solana_roundtrip.py")
log = FWD.log


def onchainos(args: list[str]) -> dict:
    return FWD.onchainos(args)


def plan(args) -> dict:
    policy = policy_mod.V2Policy.load()
    probe = FWD.load_probe()
    http = probe.Http()
    product = probe.book_symbol(args.symbol)
    usdc_raw = int(REV.solana_usdc(http, probe))
    usdc = Decimal(usdc_raw) / Decimal(10 ** 6)
    usdt_raw = int(REV.solana_token_amount(probe, http, USDT_SOLANA))
    usdt = Decimal(usdt_raw) / Decimal(10 ** 6)
    # Operator 2026-10-07: spend the USDT. The wallet holds far more of it than
    # USDC, and the fixed bridge fee makes the bigger ticket the better one.
    quote = live.quote_asset_choice(usdc=usdc, usdt=usdt,
                                    requested=getattr(args, "quote", "auto"))
    floor = Decimal(args.assume_bridge_min)
    price = Decimal(args.price)
    balance = {"USDC": usdc, "USDT": usdt}.get(quote, Decimal("0"))
    spend = min(Decimal(args.notional), balance * Decimal("0.999"),
                policy.per_trade_quote_usdt)
    tokens = (spend / price).quantize(Decimal("0.00000001"))
    # Solana raw is scaled by the multiplier, so the ladder works in raw units.
    multiplier = (scan.load_multipliers().get(args.symbol) or Decimal("1"))
    raw_floor = int((floor * Decimal(10 ** 8) / multiplier).to_integral_value())
    return {"symbol": args.symbol, "product": product, "policy": policy,
            "probe": probe, "http": http, "usdc": usdc, "usdc_raw": usdc_raw,
            "usdt": usdt, "usdt_raw": usdt_raw, "quote": quote,
            "quote_mint": live.SOLANA_QUOTES.get(quote, USDC_SOLANA),
            "price": price, "spend": spend, "tokens": tokens, "floor": floor,
            "raw_floor": raw_floor, "multiplier": multiplier}


def run_steps(ctx) -> int:
    probe, http, product, policy = (ctx["probe"], ctx["http"], ctx["product"],
                                    ctx["policy"])
    state: dict = {}

    def buy():
        """Stablecoin (USDC or USDT, whatever the plan chose) -> xStock."""
        state["before_raw"] = REV.solana_token_amount(probe, http, product["mint"])
        amount_raw = int(ctx["spend"] * Decimal(10 ** 6))
        if amount_raw <= 0:
            return ex.Outcome(ex.REFUSED, "spend rounds to zero")
        # Quote the buy first: the reconcile gate needs a number read before the
        # trade, not a number invented after it.
        out = onchainos(["swap", "quote", "--chain", "solana", "--from", ctx["quote_mint"],
                         "--to", product["mint"], "--amount", str(amount_raw),
                         "--swap-mode", "exactIn"])
        row = (out.get("data") or [{}])
        row = row[0] if isinstance(row, list) and row else {}
        state["buy_expected_raw"] = (int(row["toTokenAmount"])
                                     if row.get("toTokenAmount") else None)
        incident.sent("onchainos:swap execute",
                      f"{ctx['symbol']} buy on solana")
        result = onchainos(["swap", "execute", "--chain", "solana",
                            "--from", ctx["quote_mint"], "--to", product["mint"],
                            "--amount", str(amount_raw), "--wallet", WALLET,
                            "--slippage", str(Decimal(policy.slippage_buffer_bps)
                                              / Decimal("100"))])
        if not result.get("ok"):
            return ex.Outcome(ex.RETRY, str(result.get("error"))[:200])
        state["buy_tx"] = (result.get("data") or {}).get("swapTxHash")
        return ex.Outcome(ex.OK, "", held_at="wallet",
                          observed={"tx": state["buy_tx"],
                                    "quote": ctx["quote"], "quote_raw": amount_raw})

    def verify_buy(outcome):
        for _ in range(18):
            time.sleep(10)
            now = REV.solana_token_amount(probe, http, product["mint"])
            if now > state["before_raw"]:
                return ex.Outcome(ex.OK, "", held_at="wallet",
                                  observed={"raw": str(now)})
        status = FWD.wallet_status(state.get("buy_tx"), "solana")
        if status == "ERROR":
            return ex.Outcome(ex.RETRY, "buy was never broadcast")
        return ex.Outcome(ex.UNKNOWN, f"buy status={status} but Solana balance unchanged",
                          held_at="wallet")

    def gate() -> tuple[bool, str]:
        allowed, reason = live.live_gate(
            policy, halt=HALT.exists(), lock=False,
            kill=(DATA / "KILL_SWITCH").exists(),
            auto_halt=(DATA / "AUTO_HALT").exists(), v1_busy=FWD.v1_busy(),
            family=FAMILY, chain="solana",
            allowed_chains=frozenset({"solana", "x-layer"}))
        return allowed, reason

    def alert(*, state: str, held_at: str, reason: str) -> None:
        v2notify.sender("alert")(v2notify.format_hold(
            symbol=ctx["symbol"], family=FAMILY, state=state,
            held_at=held_at, reason=reason))

    steps = [
        ex.Step("buy", buy, held_at="wallet", broadcasts=True,
                inventory=True, retry_once=True, verify=verify_buy),
        # Solana -> X Layer, the direction with an arrival sample and a
        # measured fee (0.000852448 SOL per message).
        ex.Step("bridge", FWD.bridge_step_runner(ctx, state), held_at="in_bridge",
                broadcasts=True, inventory=True, retry_once=True,
                verify=FWD.verify_bridge(ctx, state)),
        ex.Step("sell", FWD.sell_step_runner(ctx, state), held_at="dest_wallet",
                broadcasts=True, retry_once=True, verify=FWD.verify_sell(ctx, state)),
    ]
    lock = cycle.CycleLock(LOCK, HALT, label=f"{ctx['symbol']} {FAMILY}",
                           symbol=ctx["symbol"], family=FAMILY)
    machine = ex.Executor(symbol=ctx["symbol"], chain="x-layer", family=FAMILY,
                          mode="live", size_usdt=ctx["spend"], tokens=ctx["tokens"],
                          steps=steps, gate=gate, ledger_path=LEDGER,
                          halt_path=HALT, notify=alert, progress=lock.update,
                          reconcile=FWD.reconcile_xlayer(ctx, state))
    with lock:
        result = machine.run()
        if result["keep_lock"]:
            lock.hold = True
    print(json.dumps(result, indent=1))
    return 0 if result["state"] == ex.SOLD else 1


def cycle_once(*, symbol: str = "SPYx", notional: str = "8", price: str = "780",
               bridge_min: str = "0.0095", confirm_live: bool = False,
               quote: str = "auto") -> int:
    """Entry point for the resident live runner. Same code path as ``once``."""
    argv = [sys.argv[0], "once", "--symbol", symbol, "--notional", notional,
            "--price", price, "--assume-bridge-min", bridge_min, "--quote", quote]
    if confirm_live:
        argv.append("--confirm-live")
    saved = sys.argv
    sys.argv = argv
    try:
        return main()
    finally:
        sys.argv = saved


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("plan", "simulate", "once"))
    parser.add_argument("--symbol", default="SPYx")
    parser.add_argument("--notional", default="8")
    parser.add_argument("--price", default="780")
    parser.add_argument("--assume-bridge-min", default="0.0095",
                        help="measured LOWER bound of the bridge minimum (tokens)")
    parser.add_argument("--quote", default="auto", choices=("auto", "USDC", "USDT"),
                        help="which Solana stablecoin the buy leg spends")
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args()

    ctx = plan(args)
    log(f"family {FAMILY}: Solana {ctx['quote'] or 'NONE'} -> {args.symbol} "
        f"-> bridge -> X Layer USDG")
    log(f"mint {ctx['product']['mint']}  evm {ctx['product']['evm_xlayer']}")
    log(f"Solana balances: USDC {ctx['usdc']} / USDT {ctx['usdt']}  -> "
        f"spend {ctx['spend']} {ctx['quote']} -> {ctx['tokens']} tokens "
        f"(raw floor {ctx['raw_floor']})")
    if not ctx["quote"]:
        log("REFUSED: no Solana stablecoin balance to spend (USDC and USDT both 0)")
        return 2
    balance = ctx["usdt"] if ctx["quote"] == "USDT" else ctx["usdc"]
    if balance < ctx["floor"] * ctx["price"]:
        log(f"REFUSED: Solana {ctx['quote']} {balance} cannot cover the bridge floor "
            f"{ctx['floor']} tokens (~{(ctx['floor'] * ctx['price']).quantize(Decimal('0.01'))})")
        return 2
    if args.mode == "plan":
        return 0
    if not args.confirm_live:
        raise SystemExit("once needs --confirm-live")
    return run_steps(ctx)


if __name__ == "__main__":
    raise SystemExit(main())
