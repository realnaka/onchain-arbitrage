#!/usr/bin/env python3
"""Family: buy on Ethereum -> bridge to Solana -> deposit into Kraken.

The sixth family, and the one the runner had no slot for (A109, 2026-10-10).
``chain_via_bridge_to_kraken`` is wired for X Layer only: the resident runner
hands that family to ``xstocks_v2_solana_roundtrip.py``, whose buy leg is an
X Layer swap, so an ``eth->solana`` row had no way to be walked. The scan has
priced it since the pools went in; ``capability`` says every leg is ready.

    scripts/xstocks_v2_eth_via_solana.py plan --symbol MSTRx
    scripts/xstocks_v2_eth_via_solana.py paper --symbol MSTRx
    scripts/xstocks_v2_eth_via_solana.py once  --symbol MSTRx --confirm-live

Everything here is assembled from executors that already walked real money:
the Ethereum buy leg (``xstocks_v2_eth_roundtrip``), the eth -> Solana bridge
(``xstocks_v2_edge_send``), and the Solana -> Kraken deposit
(``xstocks_v2_solana_roundtrip``). The state machine, the gate, the ledger and
the HALT rules come from ``xstocks_v2.executor``, like every other family.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.xstocks_v2 import cycle as cycle_core          # noqa: E402
from arb_monitor.xstocks_v2 import executor as ex               # noqa: E402
from arb_monitor.xstocks_v2 import ledger                        # noqa: E402
from arb_monitor.xstocks_v2 import live                          # noqa: E402
from arb_monitor.xstocks_v2 import policy as policy_mod          # noqa: E402
from arb_monitor.xstocks_v2 import scan                          # noqa: E402
from arb_monitor.xstocks_v2.feeds import census as census_feed    # noqa: E402
from arb_monitor.xstocks_v2.feeds.http import Http                # noqa: E402

FAMILY = live.CHAIN_VIA_BRIDGE_TO_KRAKEN
CHAIN = "eth->solana"
SOURCE, HUB = "eth", "solana"
DATA = ROOT / "data-v2"
LEDGER = DATA / "xstocks_v2_cycles.jsonl"
HALT = DATA / "xstocks_v2_HALT"
LOCK = DATA / "xstocks_v2_cycle.lock"
SCAN = DATA / "xstocks_v2_scan.json"
ROUTE_SAMPLES = DATA / "xstocks_v2_route_samples.jsonl"


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def _load(name: str):
    """Load a sibling script the way the resident runner does."""
    spec = importlib.util.spec_from_file_location(
        "v2ethviasol" + name.replace(".py", "").replace("_", ""), ROOT / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def probe_module():
    """The chain reader the other families use (``solana_balance`` lives there)."""
    return _load("xstocks_v2_xlayer_roundtrip.py").load_probe()


def _d(value) -> Decimal | None:
    try:
        return None if value in (None, "") else Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def scan_row(symbol: str) -> dict:
    """The scan's own row for this pair, used only to read the minimum size."""
    try:
        snapshot = json.loads(SCAN.read_text())
    except (OSError, ValueError):
        return {}
    for row in snapshot.get("rows") or []:
        if (str(row.get("family")) == FAMILY and str(row.get("chain")) == CHAIN
                and str(row.get("symbol")) == symbol):
            return row
    return {}


def probe_tokens(symbol: str) -> tuple[Decimal | None, str]:
    """How much to move on a first walk: Kraken's deposit minimum for the hub.

    The row's ``probe.deposit_min_units`` is the number the scan derives from
    Kraken's deposit rail for ``(symbol, Solana)``; a walk below it is not a
    sample, it is a donation (lessons 3.4).
    """
    row = scan_row(symbol)
    plan = row.get("probe") or {}
    tokens = _d(plan.get("deposit_min_units"))
    if tokens is None or tokens <= 0:
        row_tokens = _d(row.get("notional_usdt"))
        return None, ("no deposit minimum in the scan row: run a scan round "
                      "first, or pass --tokens")
    return tokens, ""


def plan(symbol: str, *, tokens: Decimal | None = None,
         notional: Decimal | None = None, skip_buy: bool = False) -> dict:
    """Everything up to (not including) a signature. Never broadcasts."""
    eth_rt = _load("xstocks_v2_eth_roundtrip.py")
    edge = _load("xstocks_v2_edge_send.py")
    policy = policy_mod.V2Policy.load()
    http = Http()
    product = eth_rt.token_row(symbol)
    if product is None:
        return {"error": f"{symbol}: no Ethereum pool in the census or the v4 watch"}
    universe = census_feed.solana_universe([symbol])
    mint = (universe.get(symbol) or {}).get("token_address")
    if not mint:
        return {"error": f"{symbol}: no Solana mint; the bridge has nowhere to land"}
    probe = probe_module()
    # Two transports on purpose: the EVM legs speak ``feeds.http.Http``
    # (``.request``), the Solana legs speak the probe's own client (``.rpc``).
    # Handing one to the other is the 14:05 ``solana_balance_unreadable``.
    sol_http = probe.Http()
    hub_before = None
    try:
        hub_before = int(probe.solana_balance(sol_http, mint))
    except Exception:  # noqa: BLE001 -- an unreadable hub is a refusal later
        hub_before = None
    if skip_buy:
        # Carry what the wallet already holds: a walk that died after the swap
        # left the tokens here, and buying a second slice would double the spend.
        held = eth_rt.erc20_balance(http, product["token_address"],
                                    decimals=int(product["token_decimals"]))
        if held is None:
            return {"error": f"{symbol}: Ethereum token balance unreadable"}
        tokens = _d(held)
        if tokens is None or tokens <= 0:
            return {"error": f"{symbol}: nothing in the Ethereum wallet to carry"}
    if tokens is None:
        tokens, why = probe_tokens(symbol)
        if tokens is None and notional is None:
            return {"error": why}
    if tokens is not None:
        # Size the buy at the tokens the deposit needs: the hub's minimum is a
        # token amount, and ``spend / price`` cannot hit it exactly.
        quote = eth_rt.quote_buy(http, product, Decimal("25"))
        price = _d((quote or {}).get("price_usd"))
        if price is None or price <= 0:
            return {"error": f"{symbol}: no Ethereum buy quote ({quote.get('reason')})"}
        notional = tokens * price
    notional = _d(notional)
    if notional is None or notional <= 0:
        return {"error": "no size: pass --tokens or --notional"}
    quote = eth_rt.quote_buy(http, product, notional)
    if not quote.get("ok"):
        return {"error": f"{symbol}: buy quote refused ({quote.get('reason')})"}
    got = _d(quote.get("tokens"))
    raw = bridge_amount(quoted_tokens=got, held_tokens=tokens if skip_buy else None)
    bridge = edge.prepare(http, source=SOURCE, dest=HUB, amount=raw,
                          token=product["token_address"], symbol=symbol)
    # The buy leg guards itself on the *sell* price it would get. This family
    # sells on Kraken after the hop, so the guard price is the scan's Kraken
    # bid; for a sample the number is recorded, never consulted (``probe_only``).
    row = scan_row(symbol)
    sell_price = _d(row.get("sell_price_usd")) or _d(quote.get("price_usd"))
    gas_wei = None
    try:
        gas_wei = eth_rt.evm_feed.gas_price_wei(
            http, eth_rt.evm_feed.CHAINS["eth"])
    except Exception:  # noqa: BLE001 -- an unreadable price is not a licence
        gas_wei = None
    ctx = {
        "symbol": symbol, "http": http, "policy": policy, "product": product,
        "mint": mint, "notional": notional, "tokens": got, "raw": raw,
        "ref_buy": _d(quote.get("price_usd")), "quote": quote,
        "bridge": bridge, "bridge_fee_usdt": bridge.get("cost_usdt"),
        "row": row, "probe": probe, "hub_before": hub_before,
        "sol_http": sol_http,
        # ``buy_runner`` needs these three; missing them was the first walk's
        # ``KeyError: 'price'`` (a programmer's error, before any broadcast).
        "price": sell_price, "gas_usdt": _scan_gas_usdt(), "gas_wei": gas_wei,
        "probe_only": True,
    }
    return ctx


def _scan_gas_usdt() -> Decimal | None:
    """The gas line the scan charges an Ethereum row, so the net matches."""
    try:
        snapshot = json.loads(SCAN.read_text())
        return _d((snapshot.get("gas_lines") or {}).get("eth_usdt"))
    except (OSError, ValueError):
        return None


def bridge_amount(*, quoted_tokens, held_tokens=None) -> int:
    """Raw units to bridge: the quoted output, capped by what the wallet holds.

    Two rules, both measured on 2026-10-10. The CCIP message carries whole
    8-decimal units, so anything finer is truncated. And the quote round-trips
    through USDC, so it can come back **larger** than the balance -- the 14:08
    walk refused with ``token_short: 5098860835200000 < 5099180000000000``, a
    32-wei difference the wallet cannot pay.
    """
    raw = int((_d(quoted_tokens) or Decimal(0)) * Decimal(10 ** 18))
    raw -= raw % (10 ** 10)
    held = _d(held_tokens)
    if held is not None and held > 0:
        raw = min(raw, int(held * Decimal(10 ** 18)) // (10 ** 10) * (10 ** 10))
    return max(raw, 0)


def summary_line(ctx: dict) -> str:
    row = ctx.get("row") or {}
    parts = [
        f"{ctx['symbol']} {SOURCE}->{HUB}",
        f"buy {ctx['notional']:.4f} USDC -> {ctx['tokens']} tokens",
        f"@ {ctx['ref_buy']:.4f}",
        f"bridge fee {ctx['bridge_fee_usdt']} USDT",
        f"scan net {row.get('net_usdt')} ({row.get('edge_t0_bps')} bps)",
        f"blockers {row.get('blockers')}",
    ]
    return " | ".join(str(part) for part in parts)


def run_once(symbol: str, *, tokens: Decimal | None = None,
             confirm_live: bool = False, from_bridge: bool = False) -> dict:
    """One cycle: buy on Ethereum, bridge to Solana, deposit into Kraken.

    ``from_bridge`` skips the buy: a walk that died after the swap leaves the
    tokens in our own wallet, and the next run is supposed to carry them (the
    same rule the on-demand probes use), not to buy a second slice.
    """
    ctx = plan(symbol, tokens=tokens, skip_buy=from_bridge)
    if ctx.get("error"):
        log(f"refused: {ctx['error']}")
        return {"symbol": symbol, "family": FAMILY, "state": "refused",
                "reason": ctx["error"], "held_at": "", "keep_lock": False}
    if not confirm_live:
        log("plan only; pass --confirm-live to sign")
        return {"symbol": symbol, "family": FAMILY, "state": "planned",
                "reason": "", "held_at": "", "keep_lock": False}
    eth_rt = _load("xstocks_v2_eth_roundtrip.py")
    edge = _load("xstocks_v2_edge_send.py")
    sol = _load("xstocks_v2_solana_roundtrip.py")
    state: dict = {}
    steps = []
    if not from_bridge:
        steps.append(ex.Step("buy", eth_rt.buy_runner(ctx, state), held_at="wallet",
                             broadcasts=True, inventory=True,
                             verify=eth_rt.buy_verifier(ctx, state)))
    bridge = ex.Step("bridge", _bridge_runner(edge, ctx, state),
                     held_at="in_bridge", broadcasts=True, inventory=True,
                     verify=_bridge_verifier(edge, ctx, state))
    deposit = ex.Step("deposit", _deposit_runner(sol, ctx, state),
                      held_at="kraken", broadcasts=True, inventory=True,
                      verify=_deposit_verifier(sol, ctx, state))
    steps += [bridge, deposit]
    lock = cycle_core.CycleLock(LOCK, HALT, label=f"{symbol} {FAMILY}",
                               symbol=symbol, family=FAMILY)
    machine = ex.Executor(
        symbol=symbol, chain=CHAIN, family=FAMILY, mode="live",
        size_usdt=ctx["notional"], tokens=ctx["tokens"],
        steps=steps, ledger_path=LEDGER, halt_path=HALT,
        gate=lambda: _gate(ctx),
        progress=lock.update,
        final_state=ledger.PROBED,
        final_reason="probe: eth->solana walked, coins on Kraken",
        final_held_at="kraken")
    with lock:
        result = machine.run()
        if result["keep_lock"]:
            lock.hold = True
    log(json.dumps(result, ensure_ascii=False))
    return {**result, "symbol": symbol, "family": FAMILY}


def _gate(ctx) -> tuple[bool, str]:
    busy, why = cycle_core.v1_busy(ROOT)
    allowed, reason = live.live_gate(
        ctx["policy"], halt=HALT.exists(), lock=False,
        kill=(DATA / "KILL_SWITCH").exists(),
        auto_halt=(DATA / "AUTO_HALT").exists(), v1_busy=busy,
        family=FAMILY, chain=SOURCE, allowed_chains=frozenset({SOURCE}))
    if not allowed and reason == "v1_live_busy":
        return False, f"v1_live_busy: {why}"
    return allowed, reason


def _bridge_runner(edge, ctx, state):
    def run() -> ex.Outcome:
        now = _solana_raw(ctx)
        if now is None:
            return ex.Outcome(ex.REFUSED, "solana_balance_unreadable")
        state["hub_before"] = now
        if ctx.get("hub_before") is None:
            ctx["hub_before"] = now
        bridge_plan = edge.prepare(ctx["http"], source=SOURCE, dest=HUB,
                                   amount=ctx["raw"],
                                   token=ctx["product"]["token_address"],
                                   symbol=ctx["symbol"])
        state["bridge_plan"] = bridge_plan
        problems = [str(p) for p in bridge_plan.get("problems") or []]
        if any(p.startswith("allowance_short") for p in problems):
            approved = _approve_bridge(edge, ctx)
            if approved.get("state") != "ok":
                return ex.Outcome(ex.REFUSED, str(approved.get("reason"))[:160])
            problems = [p for p in problems if not p.startswith("allowance_short")]
            bridge_plan = {**bridge_plan, "problems": problems}
        if problems:
            return ex.Outcome(ex.REFUSED, f"bridge_plan:{','.join(problems)[:160]}")
        sent = edge.send(ctx["http"], bridge_plan)
        if not sent.get("sent"):
            return ex.Outcome(ex.REFUSED, f"bridge:{str(sent.get('reason'))[:160]}",
                              held_at="wallet")
        state["bridge_tx"] = sent.get("tx")
        return ex.Outcome(ex.OK, "", held_at="in_bridge",
                          observed={"tx": sent.get("tx")})

    return run


def _approve_bridge(edge, ctx) -> dict:
    """Approve exactly this message's amount to the bridge. Never a blank cheque."""
    from arb_monitor.xstocks_v2 import ccip_evm as EVM
    sol = _load("xstocks_v2_solana_roundtrip.py")
    data = sol.approve_calldata(EVM.BRIDGE_CONTRACT, ctx["raw"])
    try:
        tx = edge._contract_call(SOURCE, ctx["product"]["token_address"], data,
                                 edge.APPROVE_GAS_LIMIT)
    except Exception as exc:  # noqa: BLE001
        return {"state": "refused", "reason": f"approve:{str(exc)[:140]}"}
    receipt = edge.wait_receipt(ctx["http"], SOURCE, tx)
    if not receipt or receipt.get("status") != "0x1":
        return {"state": "unknown", "reason": "approve_not_confirmed"}
    return {"state": "ok", "tx": tx}


def _bridge_verifier(edge, ctx, state):
    def check(outcome) -> ex.Outcome:
        if outcome.status != ex.OK:
            return outcome
        blind = 0
        for _ in range(50):                     # EVM -> Solana took ~26 min
            time.sleep(45)
            now = _solana_raw(ctx)
            if now is None:
                # A hub read that never answers is not "it did not arrive".
                # The coins are in the bridge, and we can say so -- calling it
                # ``unknown`` would hide a known place behind "who knows"
                # (the 2026-10-10 walk saw the public Solana RPC answer
                # ``-32603 INTERNAL_ERROR`` for minutes on end).
                blind += 1
                if blind >= 8:
                    return ex.Outcome(ex.STOPPED,
                                      f"hub_balance_unreadable:{blind}_tries",
                                      held_at="in_bridge")
                continue
            blind = 0
            if now is not None and now > (state.get("hub_before") or 0):
                state["hub_after"] = now
                scan.record_route_sample(scan.route_sample(
                    symbol=ctx["symbol"], source_chain=SOURCE, dest_chain=HUB,
                    arrived=True, amount_raw=int(now) - int(state.get("hub_before") or 0),
                    dest_balance_after=now, source="eth_to_solana_arrival",
                    extra={"source_tx": state.get("bridge_tx"), "mint": ctx["mint"],
                           "units": "raw"}), ROUTE_SAMPLES)
                return ex.Outcome(ex.OK, "", observed={"raw": str(now)})
        return ex.Outcome(ex.UNKNOWN, "CCIP message not seen on Solana",
                          held_at="in_bridge")

    return check


def _solana_raw(ctx) -> int | None:
    try:
        return int(ctx["probe"].solana_balance(ctx["sol_http"], ctx["mint"]))
    except Exception:  # noqa: BLE001
        return None


def _deposit_runner(sol, ctx, state):
    """The Solana -> Kraken leg, borrowed whole from the X Layer roundtrip."""
    inner = {"probe": ctx["probe"], "http": ctx["sol_http"],
             "product": {"symbol": ctx["symbol"], "mint": ctx["mint"]}}
    return sol.deposit_step_runner(inner, state)


def _deposit_verifier(sol, ctx, state):
    inner = {"probe": ctx["probe"], "http": ctx["sol_http"],
             "product": {"symbol": ctx["symbol"], "mint": ctx["mint"]}}
    return sol.deposit_verifier(inner, state)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("plan", "paper", "once", "resume"))
    parser.add_argument("--symbol", default="MSTRx")
    parser.add_argument("--tokens", default=None,
                        help="xStock amount to move (default: the scan's deposit minimum)")
    parser.add_argument("--notional", default=None)
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args()
    tokens = Decimal(args.tokens) if args.tokens else None
    notional = Decimal(args.notional) if args.notional else None
    if args.mode == "plan":
        ctx = plan(args.symbol, tokens=tokens, notional=notional)
        if ctx.get("error"):
            print(json.dumps({"error": ctx["error"]}, ensure_ascii=False))
            return 1
        print(summary_line(ctx))
        return 0
    if args.mode == "paper":
        ctx = plan(args.symbol, tokens=tokens, notional=notional)
        if ctx.get("error"):
            print(json.dumps({"error": ctx["error"]}, ensure_ascii=False))
            return 1
        row = ledger.row(symbol=args.symbol, chain=CHAIN, family=FAMILY,
                         state="paper", mode="paper",
                         size_usdt=ctx["notional"], tokens=ctx["tokens"],
                         buy_price=ctx["ref_buy"],
                         sell_price=(ctx.get("row") or {}).get("sell_price_usd"),
                         edge_t0_bps=(ctx.get("row") or {}).get("edge_t0_bps"),
                         net_usdt=(ctx.get("row") or {}).get("net_usdt"),
                         reason="paper: eth->solana bridge + kraken deposit",
                         extra={"bridge_fee_usdt": ctx["bridge_fee_usdt"],
                                "raw": ctx["raw"], "mint": ctx["mint"]})
        ledger.record(row, LEDGER)
        print(summary_line(ctx))
        return 0
    result = run_once(args.symbol, tokens=tokens, confirm_live=args.confirm_live,
                      from_bridge=args.mode == "resume")
    return 0 if result.get("state") in ("planned", "probed", "sold") else 1


if __name__ == "__main__":
    raise SystemExit(main())
