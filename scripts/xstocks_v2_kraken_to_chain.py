#!/usr/bin/env python3
"""Family 2: Kraken buy -> withdraw straight to the pool chain -> sell there.

Kraken lists xStocks on Solana, Ethereum and TON. When the pool we want sits on
one of those chains, the bridge that family 6 needs is pure cost: a message fee
plus 2-27 minutes of waiting. This walks the direct path instead.

Every leg is borrowed, not copied: the Kraken buy and the withdraw are the ones
``xstocks_v2_xlayer_roundtrip.py`` already runs (family 6), and the sell is the
module-level ``sell_step_runner`` from ``xstocks_v2_solana_roundtrip.py``.
Spec section 3.1: one signing path per venue.

    scripts/xstocks_v2_kraken_to_chain.py plan --symbol SPYx --notional 40
    scripts/xstocks_v2_kraken_to_chain.py once --symbol SPYx --notional 40 --confirm-live

Only Solana is a destination today: it is the one chain where we have both a
Kraken rail and a live sell venue. A symbol whose rail is Ethereum or TON
refuses with the rail it saw, instead of walking a path it cannot finish.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from decimal import ROUND_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from arb_monitor.xstocks_v2 import cycle, executor as ex, ledger, live  # noqa: E402
from arb_monitor.xstocks_v2 import notify as v2notify                   # noqa: E402
from arb_monitor.xstocks_v2 import policy as policy_mod                 # noqa: E402
from arb_monitor.xstocks_v2 import realized as realized_core            # noqa: E402
from arb_monitor.xstocks_v2 import scan                                 # noqa: E402
from arb_monitor.xstocks_v2.feeds import census as census_feed           # noqa: E402
from arb_monitor.xstocks_v2.feeds.http import Http as FeedHttp            # noqa: E402
from arb_monitor.xstocks_v2.feeds.jupiter import JupiterFeed             # noqa: E402

FAMILY = live.KRAKEN_TO_CHAIN
DEST_CHAIN = "solana"
DATA = ROOT / "data-v2"
LOCK = DATA / "xstocks_v2_cycle.lock"
HALT = DATA / "xstocks_v2_HALT"
LEDGER = DATA / "xstocks_v2_cycles.jsonl"
SCAN = DATA / "xstocks_v2_scan.json"
TOKENIZED = getattr(live, "TOKENIZED", "tokenized_asset")


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def forward():
    """Family 6's proven Kraken legs (buy, withdraw)."""
    return _module("v2fwd", ROOT / "scripts" / "xstocks_v2_xlayer_roundtrip.py")


def reverse():
    """The Jupiter sell leg (module-level since 2026-10-07)."""
    return _module("v2sol", ROOT / "scripts" / "xstocks_v2_solana_roundtrip.py")


FWD = forward()
REV = reverse()


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def ask_from_scan(symbol: str) -> Decimal | None:
    """The Kraken ask the scan already measured for this symbol.

    The candidate row is what the selector ranked, and its ``buy_price_usd`` is
    the Kraken ask per credit unit. Reading it here keeps the plan on the same
    number the decision used, instead of opening a second book snapshot that can
    disagree by the time the order lands.
    """
    try:
        snapshot = json.loads(SCAN.read_text())
    except (OSError, ValueError):
        return None
    for row in snapshot.get("rows") or []:
        if (str(row.get("symbol")) == symbol
                and row.get("family") == FAMILY
                and str(row.get("buy_venue")) == "kraken"):
            try:
                price = Decimal(str(row.get("buy_price_usd")))
            except (ArithmeticError, TypeError, ValueError):
                continue
            if price > 0:
                return price
    return None


def solana_rail(kraken, symbol: str) -> dict | None:
    """Kraken's Solana withdrawal method for this symbol (min + fee), or None."""
    try:
        rows = kraken.client._post("/0/private/WithdrawMethods",
                                   {"asset": symbol, "aclass": TOKENIZED}) or []
    except Exception:  # noqa: BLE001
        return None
    for row in rows:
        if str(row.get("network") or "") == "Solana":
            return row
    return None


def fresh_exit_price(*, symbol: str, notional: Decimal, policy,
                     http) -> Decimal | None:
    """The **second** real quote of the exit, before any Kraken money moves.

    Operator 2026-10-08: "你不是还有第二轮真实询价吗？真实询价的时候发现池子
    深度不够再拒绝套利啊". For this family the answer was no: it buys on Kraken
    first, withdraws, and only then quotes the sell -- so an exit that turns out
    to be fake *after* the buy is a naked leg. This puts that second quote where
    it belongs: in the plan, at the size we would actually sell.

    It uses the same code path as the scan (``solana_pair_quotes``), so units,
    multiplier and route come from one implementation.
    """
    universe = census_feed.solana_universe([symbol]).get(symbol) or {}
    if not universe.get("token_address"):
        return None
    multiplier = Decimal(str((scan.load_multipliers() or {}).get(symbol) or 1))
    quotes = scan.solana_pair_quotes(
        JupiterFeed(http), universe, notional, multiplier,
        recv_ms=int(time.time() * 1000),
        # Decision-time quote, shared 1 rps key: a 429 is a collision with the
        # scan/paper, not a missing pool. Measured 2026-10-08 23:17: three
        # single-shot attempts in a row reported "no exit quote" while a direct
        # call answered 99.78 — the family refused to move money it could have.
        tries=scan.REQUOTE_TRIES)
    sell = quotes.get("sell")
    return None if sell is None else sell.price_usd


def exit_clears(*, symbol: str, notional: Decimal, ask: Decimal,
                policy, http) -> tuple[bool, str]:
    """May this row be bought at ``ask`` given a *fresh* exit quote?"""
    price = fresh_exit_price(symbol=symbol, notional=notional, policy=policy,
                             http=http)
    if price is None:
        return False, "exit_requote_failed"
    gross = scan.edge_bps(ask, price)
    required = policy.required_edge_bps(scan._session_label(int(time.time() * 1000)))
    if gross is None:
        return False, "exit_requote_no_edge"
    if gross < required:
        return False, (f"exit_requote_below_bar:{gross}b/<{required}b"
                       f"@ask{ask}/exit{price}")
    return True, f"exit_requote_ok:{gross}b"


def plan(args) -> dict:
    """Everything the three legs need, or an exception naming what is missing."""
    policy = policy_mod.V2Policy.load()
    probe = REV.FWD.load_probe()
    http = probe.Http()
    # The buy-before-exit check quotes Jupiter, and ``probe.Http`` is a Solana
    # RPC helper: it has ``rpc`` but **no ``get_json``**, so ``JupiterFeed``
    # raised AttributeError inside its own try and every plan died at
    # ``exit_requote_failed`` (found 2026-10-08 23:17, while moving ZUSD back to
    # Solana). Two clients, each for what it can do.
    feed_http = FeedHttp()
    product = probe.book_symbol(args.symbol)
    kraken = FWD.Kraken()

    rail = solana_rail(kraken, args.symbol) or {}
    network = str(rail.get("network") or "")
    if not rail:
        raise SystemExit(
            f"{args.symbol}: Kraken lists no Solana rail for it, and Solana is "
            "the only destination with a sell venue here")
    if "solana" not in network.lower():
        raise SystemExit(f"{args.symbol}: rail network is {network!r}")
    withdraw_min = Decimal(str(rail.get("minimum") or 0))
    fee = rail.get("fee") or {}
    withdraw_fee = Decimal(str(fee.get("fee") if isinstance(fee, dict) else fee or 0))

    entry = None
    try:
        book = json.loads((DATA / "xstocks_v2_address_book.json").read_text())
        for row in book.get("address_book") or []:
            net = str(row.get("network") or "").lower()
            method = str(row.get("method") or "").lower()
            if str(row.get("asset")) == args.symbol and "solana" in (net + method):
                entry = row
                break
    except (OSError, ValueError):
        entry = None
    if not entry:
        raise SystemExit(
            f"{args.symbol}: no registered Solana withdrawal address "
            "(scripts/xstocks_v2_addresses.py or the Kraken UI)")

    price = Decimal(str(args.price)) if args.price and Decimal(str(args.price)) > 0 \
        else ask_from_scan(args.symbol)
    if price is None or price <= 0:
        raise SystemExit(f"{args.symbol}: no Kraken ask in the scan to price against")

    available = kraken.usd()
    notional = Decimal(str(args.notional))
    size = min(notional, available * Decimal("0.999"))
    floor = withdraw_min + withdraw_fee
    wanted = Decimal(str(args.tokens)) + withdraw_fee if args.tokens else size / price
    buy_tokens = min(wanted, size / price)
    if buy_tokens < floor:
        raise SystemExit(
            f"{args.symbol}: {buy_tokens} is below the rail floor {floor} "
            f"(Kraken USD {available})")
    # Second real quote, BEFORE the Kraken order: the scan's exit price is one
    # observation, and a one-sided pool can quote several times the market.
    clears, note = exit_clears(symbol=args.symbol, notional=size, ask=price,
                               policy=policy, http=feed_http)
    log(f"family {FAMILY}: exit re-quote {note}")
    if not clears:
        raise SystemExit(f"{args.symbol}: refusing before the buy -- {note}")
    return {
        "policy": policy, "probe": probe, "http": http, "product": product,
        "kraken": kraken, "symbol": args.symbol, "notional": size, "price": price,
        "tokens": buy_tokens, "buy_tokens": buy_tokens.quantize(
            Decimal("0.00000001"), rounding=ROUND_UP),
        "multiplier": (scan.load_multipliers() or {}).get(args.symbol),
        "withdraw_fee": withdraw_fee, "withdraw_min": withdraw_min,
        "rail_key": entry.get("key"), "rail_network": network,
        "available_usd": available,
    }


def gate(ctx) -> tuple[bool, str]:
    return live.live_gate(
        ctx["policy"], halt=HALT.exists(), lock=False,
        kill=(DATA / "KILL_SWITCH").exists(),
        auto_halt=(DATA / "AUTO_HALT").exists(), v1_busy=FWD.v1_busy(),
        family=FAMILY, chain=DEST_CHAIN, allowed_chains=frozenset({DEST_CHAIN}))


def reconcile(ctx, state):
    """Prediction = the ladder's full-size quote; realization = USDC delta."""
    def run():
        expected = state.get("expected_usdc_raw")
        before = state.get("before_usdc")
        if expected is None or before is None:
            return None
        after = REV.solana_usdc(ctx["http"], ctx["probe"])
        predicted = Decimal(str(expected)) / Decimal(10 ** 6)
        realized = (after - before) / Decimal(10 ** 6)
        return predicted, realized, max(predicted, abs(realized))
    return run


def realized_family2(ctx, state) -> dict:
    """Family 2's realized net: no bridge, so no bridge fee.

    Cash out = the Kraken USD delta (the buy), cash in = the USDC delta from the
    Jupiter sell. The withdrawal fee is Kraken's, charged in units and priced at
    the plan's price. Gas is 0 on purpose: both legs are Kraken plus Solana, and
    Solana's network fee is inside the measured transfer line, not an EVM gas
    market (see ``costs.gas_cost_usd``).
    """
    before_usd, after_usd = state.get("kraken_usd_before"), state.get("kraken_usd_after")
    before_usdc, after_usdc = state.get("before_usdc"), state.get("after_usdc")
    cash_out = None if None in (before_usd, after_usd) else before_usd - after_usd
    cash_in = None if None in (before_usdc, after_usdc) else after_usdc - before_usdc
    price = Decimal(str(ctx.get("price") or 0))
    fee_units = ctx.get("withdraw_fee")
    withdraw_usd = (None if fee_units is None or price <= 0
                    else Decimal(str(fee_units)) * price)
    return realized_core.cycle_realized(
        cash_in_usd=cash_in, cash_out_usd=cash_out,
        fees_usd={"withdraw_fee_usd": withdraw_usd},
        gas_usd=Decimal(0),
        note="no bridge leg: nothing to pay for a message; withdraw fee priced "
             "at the plan's price")


def run_steps(ctx) -> int:
    state: dict = {}
    steps = [
        ex.Step("buy", FWD.kraken_buy_runner(ctx, state), held_at="kraken",
                broadcasts=True, inventory=True),
        ex.Step("withdraw", FWD.withdraw_step_runner(ctx, state),
                held_at="in_bridge", broadcasts=True, inventory=True),
        ex.Step("sell", REV.sell_step_runner(ctx, state), held_at="wallet",
                broadcasts=True, retry_once=True,
                verify=REV.sell_verifier(ctx, state)),
    ]

    def alert(*, state: str, held_at: str, reason: str) -> None:
        v2notify.sender("alert")(v2notify.format_hold(
            symbol=ctx["symbol"], family=FAMILY, state=state, held_at=held_at,
            reason=reason))

    lock = cycle.CycleLock(LOCK, HALT, label=f"{ctx['symbol']} {FAMILY}",
                           symbol=ctx["symbol"], family=FAMILY)
    machine = ex.Executor(symbol=ctx["symbol"], chain=DEST_CHAIN, family=FAMILY,
                          mode="live", size_usdt=ctx["notional"],
                          tokens=ctx["buy_tokens"],
                          steps=steps, gate=lambda: gate(ctx),
                          ledger_path=LEDGER, halt_path=HALT, notify=alert,
                          progress=lock.update,
                          realized=lambda: realized_family2(ctx, state),
                          reconcile=reconcile(ctx, state))
    with lock:
        result = machine.run()
        if result["keep_lock"]:
            lock.hold = True
    print(json.dumps(result, indent=1))
    return 0 if result["state"] == ex.SOLD else 1


def cycle_once(*, symbol: str = "SPYx", notional: str = "40", price: str = "0",
               tokens: str | None = None, confirm_live: bool = False) -> int:
    """Entry point for the resident live runner: the same path as the CLI."""
    ctx = plan(argparse.Namespace(symbol=symbol, notional=notional, price=price,
                                  tokens=tokens))
    if not confirm_live:
        log(f"plan only: {notional} USDT -> {ctx['buy_tokens']} {symbol} "
            f"(ask {ctx['price']}, rail {ctx['rail_network']})")
        return 0
    return run_steps(ctx)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("plan", "once"))
    parser.add_argument("--symbol", default="SPYx")
    parser.add_argument("--notional", default="40")
    parser.add_argument("--price", default="0")
    parser.add_argument("--tokens", default=None)
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args()

    ctx = plan(args)
    log(f"family {FAMILY}: Kraken ask {ctx['price']} -> withdraw "
        f"{ctx['buy_tokens']} {args.symbol} to {ctx['rail_network']} "
        f"(fee {ctx['withdraw_fee']}, rail min {ctx['withdraw_min']}, "
        f"Kraken USD {ctx['available_usd']})")
    if args.mode == "plan":
        return 0
    if not args.confirm_live:
        raise SystemExit("once needs --confirm-live")
    return run_steps(ctx)


if __name__ == "__main__":
    raise SystemExit(main())
