#!/usr/bin/env python3
"""One minimal-amount closed cycle: Kraken buy -> Solana -> bridge -> X Layer sell.

    scripts/xstocks_v2_xlayer_roundtrip.py plan
    scripts/xstocks_v2_xlayer_roundtrip.py once --confirm-live --assume-bridge-min 0.0095

Every leg is a real leg. The state machine, the gates, the ledger and the HALT
rules come from ``xstocks_v2.executor``; this file only supplies the runners.

The amount rule is the operator's: ``max(bridge minimum, withdrawal minimum,
sell minimum)``. **An unknown minimum is not zero**, so the bridge minimum has
to be supplied explicitly with ``--assume-bridge-min`` and the value used is
written into the ledger. 0.0095 is the amount the 2026-10-07 probe moved
through successfully, so it is a measured *lower* bound, not a contract minimum.
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import subprocess
import sys
import time
from decimal import Decimal, ROUND_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.venues.kraken_spot import (  # noqa: E402
    KrakenSpotClient, TOKENIZED, load_credentials)
from arb_monitor.xstocks_v2 import ccip_solana as C            # noqa: E402
from arb_monitor.xstocks_v2 import cycle, executor as ex       # noqa: E402
from arb_monitor.xstocks_v2 import incident                    # noqa: E402
from arb_monitor.xstocks_v2 import ledger                      # noqa: E402
from arb_monitor.xstocks_v2 import notify as v2notify          # noqa: E402
from arb_monitor.xstocks_v2 import realized as realized_core   # noqa: E402
from arb_monitor.xstocks_v2 import bridge_fees as bridge_fees_core  # noqa: E402
from arb_monitor.xstocks_v2 import paths as P                  # noqa: E402
from arb_monitor.xstocks_v2 import policy as policy_mod        # noqa: E402
from arb_monitor.xstocks_v2 import scan                        # noqa: E402
from arb_monitor.xstocks_v2 import live                        # noqa: E402
from arb_monitor.xstocks_v2 import units                       # noqa: E402
from arb_monitor.xstocks_v2 import ladder as ladder_core       # noqa: E402

PROXY = "http://127.0.0.1:4780"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
XLAYER_RPC = "https://rpc.xlayer.tech"
USDG_XLAYER = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"
WALLET = "AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev"
EVM_WALLET = "0xabababababababababababababababababababab"
DATA = ROOT / "data-v2"
LOCK = DATA / "xstocks_v2_cycle.lock"
HALT = DATA / "xstocks_v2_HALT"
LEDGER = DATA / "xstocks_v2_cycles.jsonl"

#: The Solana -> X Layer CCIP fee is paid in SOL by our own wallet, per message,
#: independent of the amount. Two samples on 2026-10-07: 744731.9 lamports in the
#: probe and 852448 lamports (0.000852448 SOL) on the executed bridge -- unknown
#: is not zero, so the higher sample is the bar.
SOL_BRIDGE_FEE = Decimal(os.environ.get("XSTOCKS_V2_SOL_BRIDGE_FEE",
                                        "0.000852448"))


def sol_balance(http, owner: str = WALLET) -> Decimal | None:
    """SOL in our Solana wallet, or ``None`` when the read does not answer.

    The bridge fee is paid from this balance, so it is the Solana half of the
    ``gas_low`` check on the X Layer side: the failure it prevents looks like
    "the bridge silently did nothing" (measured 2026-10-07 on the EVM side, where
    the chain answered ``insufficient funds for gas * price + value``).
    """
    try:
        value = (http.rpc(SOLANA_RPC, "getBalance", [owner]) or {}).get("value")
    except Exception:  # noqa: BLE001 - a read that does not answer is unknown
        return None
    if not isinstance(value, int):
        return None
    return Decimal(value) / Decimal(10 ** 9)


def sol_shortfall(http, *, fee_sol: Decimal | None = None,
                  owner: str = WALLET) -> str | None:
    """``None`` when the wallet can pay the bridge fee, else the refusal's name."""
    need = SOL_BRIDGE_FEE if fee_sol is None else Decimal(str(fee_sol))
    have = sol_balance(http, owner)
    if have is None:
        return f"sol_unknown:{owner}"
    if have < need:
        return f"gas_low:{have} < {need}"
    return None


def load_probe():
    """The Solana-side helpers already tested by the probe script."""
    spec = importlib.util.spec_from_file_location(
        "v2probe", ROOT / "scripts" / "xstocks_v2_ccip_solana_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def onchainos(args: list[str], timeout: int = 240) -> dict:
    env = dict(os.environ, HTTPS_PROXY=PROXY, HTTP_PROXY=PROXY)
    done = subprocess.run(["onchainos"] + args, capture_output=True, text=True,
                          env=env, timeout=timeout)
    try:
        return json.loads(done.stdout)
    except ValueError:
        return {"ok": False, "error": (done.stdout or done.stderr)[:400]}


def wallet_status(tx_hash: str, chain: str) -> str:
    """``txStatus`` from the wallet service for one hash.

    ``ok:true`` + a hash is NOT a broadcast: the backend can answer
    ``ERROR / in-flight transaction limit reached for delegated accounts`` while
    the hash never reaches a chain (measured twice on 2026-10-07). This is the
    only place that can tell the two apart before the balance moves.
    """
    if not tx_hash:
        return ""
    result = onchainos(["wallet", "history", "--tx-hash", tx_hash, "--chain", chain])
    return ex.parse_wallet_status(result, tx_hash)


def v1_busy() -> bool:
    locks = [ROOT / "data" / "xstocks_cycle.lock", ROOT / "data" / "xstocks_eth_cycle.lock"]
    if any(path.exists() for path in locks):
        return True
    for pattern in ("scripts/xstocks_auto_live.py", "scripts/xstocks_eth_auto_live.py"):
        try:
            out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
        except OSError:
            return True
        if out.returncode == 0 and out.stdout.strip():
            return True
    return False


class Kraken:
    def __init__(self):
        key, secret = load_credentials(ROOT / "config" / "kraken.env")
        self.client = KrakenSpotClient(key, secret, proxy=PROXY)

    def tokens(self, symbol: str) -> Decimal:
        return self.client.balance().get(f"{symbol}.T", Decimal("0"))

    def usd(self) -> Decimal:
        return self.client.balance().get("ZUSD", Decimal("0"))

    def buy(self, symbol: str, volume: Decimal) -> ex.Outcome:
        try:
            result = self.client.order(symbol, volume, side="buy", ordertype="market",
                                       confirm="live")
        except Exception as exc:  # noqa: BLE001
            return ex.classify_exception(exc)
        txid = (result.get("txid") or [""])[0]
        for _ in range(12):
            time.sleep(5)
            if self.tokens(symbol) >= volume * Decimal("0.999"):
                return ex.Outcome(ex.OK, "", observed={"txid": txid,
                                                       "tokens": str(self.tokens(symbol))})
        return ex.Outcome(ex.UNKNOWN, f"order {txid} not visible in balance",
                          held_at="kraken")

    def withdraw(self, symbol: str, key: str, amount: Decimal) -> ex.Outcome:
        try:
            result = self.client._post("/0/private/Withdraw",
                                      {"asset": symbol, "aclass": TOKENIZED,
                                       "key": key, "amount": str(amount)})
        except Exception as exc:  # noqa: BLE001
            return ex.classify_exception(exc)
        return ex.Outcome(ex.OK, "", held_at="kraken",
                          observed={"refid": result.get("refid")})


def plan(args) -> dict:
    symbol = args.symbol
    probe = load_probe()
    http = probe.Http()
    policy = policy_mod.V2Policy.load()
    product = probe.book_symbol(symbol)

    deposit, withdraw_rail = probe_rails(symbol)
    graph = P.full_catalog(
        symbol=symbol, measured=scan.load_measured_bridges(),
        native_chains={"solana"}, deposit_rail=deposit, withdraw_rail=withdraw_rail,
        kraken_taker_bps=policy.kraken_taker_bps)
    found = graph.enumerate(P.cash_node("kraken"), P.cash_node("x-layer", "x-layer"),
                            symbol, max_legs=5)
    if not found:
        raise SystemExit("no path kraken -> x-layer for this symbol")

    # Kraken's own minimum and fee for the Solana rail
    key, secret = load_credentials(ROOT / "config" / "kraken.env")
    client = KrakenSpotClient(key, secret, proxy=PROXY)
    rail = [row for row in client._post("/0/private/WithdrawMethods",
                                       {"asset": symbol, "aclass": TOKENIZED}) or []
            if str(row.get("network")) == "Solana"]
    withdraw_min = Decimal(str(rail[0]["minimum"])) if rail else None
    withdraw_fee = Decimal(str(rail[0]["fee"]["fee"])) if rail else None
    bridge_min = Decimal(args.assume_bridge_min) if args.assume_bridge_min else None

    # The sell leg has no minimum by construction (a Uniswap V3 exact-in swap
    # accepts any positive amount), so it is not a constraint. Every OTHER
    # minimum that is unknown stays unknown -- and unknown is not zero.
    floor, floor_reason = live.amount_floor(
        bridge_min=bridge_min, deposit_min=None, sell_min=None, pool_min=None)
    missing = live.unknown_minimums(bridge_min=bridge_min, deposit_min=withdraw_min,
                                    sell_min=None, requires_bridge=True,
                                    requires_deposit=False, requires_sell=False)
    notional = Decimal(args.notional)
    available = client.balance().get("ZUSD", Decimal("0"))
    size = min(notional, available * Decimal("0.999"), policy.per_trade_quote_usdt)
    price = Decimal(str(args.price))
    # Minimal closed loop: buy exactly the floor, not the notional. The notional
    # (and the 200 cap) only bound what we are *allowed* to spend.
    fee = withdraw_fee or Decimal("0")
    needed = (floor + fee) if floor is not None else None
    affordable = size / price
    if needed is None:
        buy_tokens = None
    else:
        buy_tokens = min(needed, affordable).quantize(Decimal("0.00000001"),
                                                     rounding=ROUND_UP)
        if buy_tokens < needed:
            missing.append("below_floor_for_balance")

    ranked = graph.rank(found, notional_usd=size, tokens=floor or Decimal("0"))
    return {
        "symbol": symbol, "product": product, "policy": policy,
        "graph_path": ranked[0], "floor": floor, "floor_reason": floor_reason,
        "missing_minimums": missing, "withdraw_min": withdraw_min,
        "withdraw_fee": withdraw_fee, "bridge_min": bridge_min,
        "notional": size, "price": price, "buy_tokens": buy_tokens,
        "rail": rail[0] if rail else None,
        "http": http, "probe": probe, "client": client,
        "withdraw_key": (withdraw_rail or {}).get("key"),
        "multiplier": (scan.load_multipliers() or {}).get(symbol),
    }


def probe_rails(symbol: str):
    deposit = None
    rows = scan.load_deposits().get(symbol) or []
    for row in rows:
        name = str(row.get("method") or "")
        if name.lower() == symbol.lower() or "solana" in name.lower():
            deposit = {"chain": "solana", "method": name,
                       "minimum": row.get("minimum"), "duration_s": 60}
            break
    withdraw = None
    try:
        cached = json.loads((DATA / "xstocks_v2_address_book.json").read_text())
        entries = cached.get("address_book") or []
    except (OSError, ValueError):
        entries = []
    for row in entries:
        net = str(row.get("network") or "").lower()
        name = str(row.get("method") or "").lower()
        if str(row.get("asset")) == symbol and "solana" in (net + " " + name):
            withdraw = {"chain": "solana", "key": row.get("key"), "duration_s": 120}
            break
    return deposit, withdraw


def agg_quote_out(chain: str, src: str, dst: str, amount_raw: int) -> int | None:
    """The aggregator's ``toTokenAmount`` for a sell we are about to make.

    Read *before* the swap so the reconcile gate compares two real numbers. A
    missing quote is unmeasured, never guessed.
    """
    out = onchainos(["swap", "quote", "--chain", chain, "--from", src, "--to", dst,
                     "--amount", str(amount_raw), "--swap-mode", "exactIn"])
    if not out.get("ok"):
        return None
    data = out.get("data") or []
    row = data[0] if isinstance(data, list) and data else {}
    amount = row.get("toTokenAmount") if isinstance(row, dict) else None
    return int(amount) if amount else None


def ladder_confirm(*, chain: str, src: str, dst: str, total_raw: int,
                   max_bps: Decimal) -> tuple[bool, str, int | None]:
    """Quote the sell route at 25/50/75/100% and check the shape.

    One quote at the full size is a point, not a curve: it cannot tell us
    whether the route still exists at a quarter of the size, or whether the last
    quarter is filled much worse. Returns ``(ok, reason, full_size_out_raw)``.
    """
    sizes = ladder_core.ladder_sizes(total_raw)
    if not sizes:
        return False, f"{ladder_core.INCOMPLETE}:zero_size", None
    points: list[tuple[int, Decimal]] = []
    outs: dict[int, int] = {}
    for size in sizes:
        out = agg_quote_out(chain, src, dst, size)
        if out is None:
            return False, f"{ladder_core.INCOMPLETE}:{size}", None
        outs[size] = out
        points.append((size, ladder_core.average_price(size, out)))
    ok, reason = ladder_core.ladder_ok(points, max_bps=max_bps)
    return ok, reason, (outs[max(sizes)] if ok else None)


def reconcile_xlayer(ctx, state):
    """Predicted vs realized USDG for the closed cycle (operator 2026-10-07)."""
    probe, http = ctx["probe"], ctx["http"]

    def check():
        expected = state.get("sell_expected_raw")
        before = state.get("before_usdg")
        if expected is None or before is None:
            return None
        after = probe.xlayer_balance(http, USDG_XLAYER,
                                     decimals=units.USDG_DECIMALS) or Decimal("0")
        predicted = units.scale_raw(int(expected), units.USDG_DECIMALS)
        realized = after - before
        return predicted, realized, max(predicted, abs(realized))
    return check


def sell_step_runner(ctx, state):
    """Sell the native xStock on X Layer for USDG through the aggregator."""

    def run() -> ex.Outcome:
        probe = ctx["probe"]
        http = ctx["http"]
        token = ctx["product"]["evm_xlayer"]
        held = probe.xlayer_balance(http, token)
        if not held or held <= 0:
            return ex.Outcome(ex.STOPPED, "no xStock left to sell", held_at="dest_wallet")
        # USDG is a 6-decimal token; xStocks are 18. Reading USDG with the
        # 18-decimal helper keeps "did it move" working and breaks every
        # magnitude comparison (2026-10-07, see units.scale_raw).
        state["before_usdg"] = probe.xlayer_balance(
            http, USDG_XLAYER, decimals=units.USDG_DECIMALS) or Decimal("0")
        # 200 USDT on the ticket means a single-point quote is not enough: check
        # the route at 25/50/75/100% before hitting it. The full-size quote is
        # also the prediction the reconcile gate compares against.
        capacity_ok, ladder_reason, full_out = ladder_confirm(
            chain="xlayer", src=token, dst=USDG_XLAYER,
            total_raw=int(held * Decimal(10 ** 18)),
            max_bps=Decimal(ctx["policy"].slippage_buffer_bps))
        state["sell_ladder"] = ladder_reason
        if not capacity_ok:
            return ex.Outcome(ex.REFUSED, f"sell ladder: {ladder_reason}")
        state["sell_expected_raw"] = full_out
        slippage = Decimal(ctx["policy"].slippage_buffer_bps) / Decimal("100")
        incident.sent("onchainos:swap execute",
                      f"{ctx['symbol']} {held} -> USDG (xlayer)")
        result = onchainos([
            "swap", "execute", "--chain", "xlayer", "--from", token, "--to", USDG_XLAYER,
            "--readable-amount", str(held), "--wallet", EVM_WALLET,
            "--slippage", str(slippage), "--gas-level", "average"])
        if not result.get("ok"):
            return ex.Outcome(ex.RETRY, str(result.get("error"))[:200])
        state["swap_tx"] = (result.get("data") or {}).get("swapTxHash")
        return ex.Outcome(ex.OK, "", held_at="dest_wallet",
                          observed={"tx": state["swap_tx"],
                                    "to_amount": (result.get("data") or {}).get("toAmount")})
    return run


def verify_sell(ctx, state):
    def check(outcome) -> ex.Outcome:
        probe, http = ctx["probe"], ctx["http"]
        for _ in range(18):
            time.sleep(10)
            now = probe.xlayer_balance(
                http, USDG_XLAYER, decimals=units.USDG_DECIMALS) or Decimal("0")
            if now > state["before_usdg"]:
                state["usdg_after"] = now
                return ex.Outcome(ex.OK, "", observed={
                    "usdg_before": str(state["before_usdg"]), "usdg_after": str(now),
                    "tx": state.get("swap_tx")})
        # Balance moved? No. Was the transaction broadcast at all?
        status = wallet_status(state.get("swap_tx"), "196")
        state["swap_status"] = status
        if status == "ERROR":
            # Not broadcast (in-flight limit). Safe to try once more: the
            # allowance already exists, so the retry goes straight to the swap.
            return ex.Outcome(ex.RETRY,
                              f"swap {state.get('swap_tx')} was never broadcast (ERROR)")
        return ex.Outcome(
            ex.UNKNOWN,
            f"swap {state.get('swap_tx')} status={status or 'unknown'} but USDG unchanged",
            held_at="dest_wallet")
    return check


def bridge_step_runner(ctx, state):
    def run() -> ex.Outcome:
        probe, http = ctx["probe"], ctx["http"]
        product = ctx["product"]
        raw = probe.solana_balance(http, product["mint"])
        if raw <= 0:
            return ex.Outcome(ex.REFUSED, "no xStock on Solana")
        # The Solana half of the gas precheck: the fee comes out of our own SOL,
        # and a wallet that cannot pay it must refuse *before* the broadcast
        # instead of looking like a bridge that silently did nothing.
        short = sol_shortfall(http)
        if short:
            return ex.Outcome(ex.REFUSED, short, held_at="wallet")
        product_row, accounts, data, args = probe.build(
            http, ctx["symbol"], raw, EVM_WALLET)
        state["bridge_raw"] = raw
        blockhash = http.rpc(SOLANA_RPC, "getLatestBlockhash",
                             [{"commitment": "finalized"}])["value"]["blockhash"]
        tx = C.build_legacy_transaction(payer=WALLET, accounts=accounts,
                                        program_id=C.SOLANA_PROGRAM, data=data,
                                        recent_blockhash=blockhash)
        value = http.rpc(SOLANA_RPC, "simulateTransaction", [
            base64.b64encode(tx).decode(),
            {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True}])["value"]
        if value.get("err"):
            return ex.Outcome(ex.REFUSED, f"simulation failed: {value['err']}")
        state["dest_before"] = probe.xlayer_balance(http, product["evm_xlayer"]) or Decimal("0")
        incident.sent("onchainos:wallet contract-call",
                      f"{ctx['symbol']} bridge x-layer -> solana")
        sent = onchainos(["wallet", "contract-call", "--chain", "solana",
                          "--to", C.SOLANA_PROGRAM, "--unsigned-tx", C.b58encode(tx),
                          "--from", WALLET])
        tx_hash = ((sent.get("data") or {}) if sent.get("ok") else {}).get("txHash")
        if not tx_hash:
            return ex.Outcome(ex.REFUSED, f"not broadcast: {str(sent.get('error'))[:160]}")
        state["bridge_tx"] = tx_hash
        return ex.Outcome(ex.OK, "", held_at="in_bridge", observed={"tx": tx_hash,
                                                                    "raw": raw})
    return run


def verify_bridge(ctx, state):
    def check(outcome) -> ex.Outcome:
        probe, http = ctx["probe"], ctx["http"]
        token = ctx["product"]["evm_xlayer"]
        for _ in range(40):
            time.sleep(45)
            now = probe.xlayer_balance(http, token) or Decimal("0")
            if now > state["dest_before"]:
                return ex.Outcome(ex.OK, "", observed={
                    "dest_before": str(state["dest_before"]), "dest_after": str(now),
                    "tx": state.get("bridge_tx")})
        # Never re-broadcast a bridge that WAS accepted: that is a second
        # message and a second fee. Only a status of ERROR means nothing left
        # the wallet.
        status = wallet_status(state.get("bridge_tx"), "solana")
        state["bridge_status"] = status
        if status == "ERROR":
            return ex.Outcome(ex.RETRY,
                              f"bridge {state.get('bridge_tx')} was never broadcast")
        return ex.Outcome(ex.UNKNOWN, "CCIP message not seen on the destination",
                          held_at="in_bridge")
    return check


def withdraw_step_runner(ctx, state):
    def run() -> ex.Outcome:
        kraken = ctx["kraken"]
        probe, http = ctx["probe"], ctx["http"]
        held = kraken.tokens(ctx["symbol"])
        if held <= (ctx["withdraw_fee"] or Decimal("0")):
            return ex.Outcome(ex.REFUSED, f"only {held} on Kraken")
        state["kraken_sent"] = held
        # Right after a fill the asset can still be settling: the funding side
        # answers EFunding:Insufficient funds for a few seconds (measured
        # 2026-10-07). That is a lag, not a refusal.
        incident.sent("kraken_withdraw",
                      f"{ctx['symbol']} {held} -> {ctx['rail_key']}")
        outcome = kraken.withdraw(ctx["symbol"], ctx["rail_key"], held)
        for _ in range(8):
            if outcome.status == ex.OK or "Insufficient funds" not in outcome.detail:
                break
            time.sleep(15)
            outcome = kraken.withdraw(ctx["symbol"], ctx["rail_key"], held)
        if outcome.status != ex.OK:
            return outcome
        state["refid"] = (outcome.observed or {}).get("refid")
        # Solana xStocks are Token-2022 with a scaled UI amount, so ``raw`` is
        # NOT ``ui * 1e8``: raw = ui * 1e8 / multiplier. Comparing raw against a
        # UI target is how the first live run was misread as "never arrived"
        # while the coins were already in the wallet (2026-10-07).
        multiplier = ctx["multiplier"] or Decimal("1")
        target_ui = held - (ctx["withdraw_fee"] or Decimal("0"))
        target_raw = int(target_ui * Decimal(10 ** 8) / multiplier)
        for _ in range(30):
            time.sleep(20)
            raw = probe.solana_balance(http, ctx["product"]["mint"])
            if raw >= target_raw * 0.99:
                state["solana_raw"] = raw
                return ex.Outcome(ex.OK, "", held_at="wallet",
                                  observed={"refid": state["refid"], "raw": raw})
        return ex.Outcome(ex.UNKNOWN, "withdrawal never showed up on Solana",
                          held_at="in_bridge", observed={"refid": state["refid"]})
    return run


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("plan", "once", "reconcile"))
    parser.add_argument("--symbol", default="SPYx")
    parser.add_argument("--notional", default="8")
    parser.add_argument("--price", default="780")
    parser.add_argument("--assume-bridge-min", default=None,
                        help="measured LOWER bound for the bridge minimum; required for 'once'")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--resolve", action="store_true",
                        help="reconcile: clear HALT+lock after the inventory is located")
    args = parser.parse_args()

    ctx = plan(args)
    path = ctx["graph_path"]
    log(f"path  : {path.route}")
    log(f"kinds : {path.kinds}")
    log(f"cost  : {path.cost_bps:.1f} bps / {path.cost_usd:.4f} USD, "
        f"{path.duration_s:.0f}s, blockers={list(path.blockers)}")
    log(f"amount: floor {ctx['floor']} ({ctx['floor_reason']}) "
        f"withdraw_min {ctx['withdraw_min']} fee {ctx['withdraw_fee']}")
    log(f"buy   : {ctx['buy_tokens']} tokens at ~{ctx['price']} = "
        f"{(ctx['buy_tokens'] * ctx['price']).quantize(Decimal('0.01'))} USD "
        f"(cap {ctx['policy'].per_trade_quote_usdt})")
    if ctx["missing_minimums"]:
        log(f"minimums unknown: {ctx['missing_minimums']}")
    if args.mode == "reconcile":
        probe, http = ctx["probe"], ctx["http"]
        kraken = Kraken()
        log(f"kraken {ctx['symbol']}: {kraken.tokens(ctx['symbol'])}")
        log(f"solana raw   : {probe.solana_balance(http, ctx['product']['mint'])}")
        log(f"x-layer      : {probe.xlayer_balance(http, ctx['product']['evm_xlayer'])}")
        log(f"HALT={HALT.exists()} lock={LOCK.exists()}")
        if not args.resolve:
            return 0
        # Resolving an unknown is a human decision: the inventory has been
        # located, so the hold is lifted and the resolution is written down.
        LOCK.unlink(missing_ok=True)
        HALT.unlink(missing_ok=True)
        ex.Executor(symbol=ctx["symbol"], chain="x-layer",
                    family=live.KRAKEN_VIA_BRIDGE_TO_CHAIN, mode="live",
                    size_usdt=ctx["notional"], tokens=ctx["buy_tokens"], steps=[],
                    gate=lambda: (True, "ok"), ledger_path=LEDGER,
                    halt_path=None)._record(
                        state=ledger.STOPPED, held_at="wallet",
                        reason="reconciled: unknown withdrawal located, inventory in wallet")
        log("HALT and lock cleared, resolution recorded")
        return 0
    if args.mode == "plan":
        return 0
    if not args.confirm_live:
        raise SystemExit("once needs --confirm-live")
    if not args.assume_bridge_min:
        raise SystemExit("once needs --assume-bridge-min (unknown is not zero)")
    if path.blockers:
        raise SystemExit(f"path is blocked: {path.blockers}")

    policy = ctx["policy"]

    def gate() -> tuple[bool, str]:
        # We hold LOCK ourselves (CycleLock took it with O_EXCL before we got
        # here), so it is not a foreign lock. A second process is rejected by
        # CycleLock itself, which raises CycleBusy.
        allowed, reason = live.live_gate(
            policy, halt=HALT.exists(), lock=False,
            kill=(DATA / "KILL_SWITCH").exists(),
            auto_halt=(DATA / "AUTO_HALT").exists(), v1_busy=v1_busy(),
            family=live.KRAKEN_VIA_BRIDGE_TO_CHAIN, chain="x-layer",
            allowed_chains=frozenset({"solana", "x-layer"}))
        return allowed, reason

    kraken = Kraken()
    ctx["kraken"] = kraken
    ctx["rail_key"] = ctx["withdraw_key"]
    state: dict = {}
    steps = [
        ex.Step("buy", kraken_buy_runner(ctx, state), held_at="kraken",
                broadcasts=True, inventory=True),
        ex.Step("withdraw", withdraw_step_runner(ctx, state), held_at="in_bridge",
                broadcasts=True, inventory=True),
        ex.Step("bridge", bridge_step_runner(ctx, state), held_at="in_bridge",
                broadcasts=True, inventory=True,
                retry_once=True, retry_reason="not broadcast (wallet status ERROR)",
                verify=verify_bridge(ctx, state)),
        ex.Step("sell", sell_step_runner(ctx, state), held_at="dest_wallet",
                broadcasts=True, retry_once=True, retry_reason="in-flight limit",
                verify=verify_sell(ctx, state)),
    ]
    def alert(*, state: str, held_at: str, reason: str) -> None:
        v2notify.sender("alert")(v2notify.format_hold(
            symbol=ctx["symbol"], family=live.KRAKEN_VIA_BRIDGE_TO_CHAIN,
            state=state, held_at=held_at, reason=reason))

    lock = cycle.CycleLock(LOCK, HALT,
                           label=f"{ctx['symbol']} {live.KRAKEN_VIA_BRIDGE_TO_CHAIN}",
                           symbol=ctx["symbol"],
                           family=live.KRAKEN_VIA_BRIDGE_TO_CHAIN)
    machine = ex.Executor(symbol=ctx["symbol"], chain="x-layer",
                          family=live.KRAKEN_VIA_BRIDGE_TO_CHAIN, mode="live",
                          size_usdt=ctx["notional"], tokens=ctx["buy_tokens"],
                          steps=steps, gate=gate, ledger_path=LEDGER, halt_path=HALT,
                          notify=alert, progress=lock.update,
                          realized=lambda: realized_xlayer(ctx, state),
                          reconcile=reconcile_xlayer(ctx, state))
    with lock:
        result = machine.run()
        if result["keep_lock"]:
            lock.hold = True
    print(json.dumps(result, indent=1))
    return 0 if result["state"] == ex.SOLD else 1


def kraken_buy_runner(ctx, state):
    def run() -> ex.Outcome:
        # The cash side of the buy: what Kraken actually took out of the USD
        # balance. Without it a finished cycle can say "we bought and sold" and
        # nothing about what it cost (2026-10-07: three of four `sold` rows had
        # no net at all).
        before = ctx["kraken"].usd()
        incident.sent("kraken_buy",
                      f"{ctx['symbol']} {ctx.get('buy_tokens')}")
        outcome = ctx["kraken"].buy(ctx["symbol"], ctx["buy_tokens"])
        state["kraken_usd_before"] = before
        state["kraken_usd_after"] = ctx["kraken"].usd()
        return outcome
    return run


def bridge_fee_usd(edge: tuple[str, str]) -> Decimal | None:
    """The measured USD cost of one bridge message on this edge, or None."""
    table, _unknown = bridge_fees_core.load_table(
        data_dir=DATA, now_ms=int(time.time() * 1000))
    value = table.get((edge[0].lower(), edge[1].lower()))
    return value if isinstance(value, Decimal) else None


def gas_line_usd() -> tuple[Decimal | None, str]:
    """X Layer gas for one action, from the scan's own line (an *estimate*).

    Every row pays the same number (``gas_lines.xlayer_usdt``); it is labelled,
    never mixed into the measured costs.
    """
    try:
        snapshot = json.loads((DATA / "xstocks_v2_scan.json").read_text())
        value = ((snapshot.get("gas_lines") or {}).get("xlayer_usdt"))
    except (OSError, ValueError):
        return None, "scan_missing"
    if value in (None, ""):
        return None, "scan_missing"
    return Decimal(str(value)), "scan_gas_line"


def realized_xlayer(ctx, state) -> dict:
    """Family 6: cash out on Kraken, cash in as USDG on X Layer.

    Measured: the Kraken USD delta (buy), the USDG delta (sell), the withdrawal
    fee (Kraken charges it in units, priced at the price the plan used) and the
    bridge message fee from the measured table. Estimated: the X Layer gas line.
    A part that could not be read keeps the net ``None`` and says which one.
    """
    before_usd, after_usd = state.get("kraken_usd_before"), state.get("kraken_usd_after")
    before_usdg, after_usdg = state.get("before_usdg"), state.get("usdg_after")
    cash_out = None if None in (before_usd, after_usd) else before_usd - after_usd
    cash_in = None if None in (before_usdg, after_usdg) else after_usdg - before_usdg
    price = Decimal(str(ctx.get("price") or 0))
    fee_units = ctx.get("withdraw_fee")
    withdraw_usd = (None if fee_units is None or price <= 0
                    else Decimal(str(fee_units)) * price)
    gas, gas_source = gas_line_usd()
    return realized_core.cycle_realized(
        cash_in_usd=cash_in, cash_out_usd=cash_out,
        fees_usd={"withdraw_fee_usd": withdraw_usd,
                  "bridge_fee_usd": bridge_fee_usd(("solana", "x-layer"))},
        gas_usd=gas,
        note=f"cash deltas measured; gas from {gas_source}; "
             "withdraw fee priced at the plan's price")






def cycle_once(*, symbol: str = "SPYx", notional: str = "8", price: str = "780",
               bridge_min: str = "0.0095", confirm_live: bool = False) -> int:
    """Entry point for the resident live runner.

    It runs the same code path as ``once`` on the command line, so the resident
    process and a manual run cannot drift apart.
    """
    argv = [sys.argv[0], "once", "--symbol", symbol, "--notional", notional,
            "--price", price, "--assume-bridge-min", bridge_min]
    if confirm_live:
        argv.append("--confirm-live")
    saved = sys.argv
    sys.argv = argv
    try:
        return main()
    finally:
        sys.argv = saved

if __name__ == "__main__":
    raise SystemExit(main())
