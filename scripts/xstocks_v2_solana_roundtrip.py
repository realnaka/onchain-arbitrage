#!/usr/bin/env python3
"""Reverse closed loop: X Layer USDG -> buy -> CCIP to Solana -> sell on Solana.

    scripts/xstocks_v2_solana_roundtrip.py plan
    scripts/xstocks_v2_solana_roundtrip.py simulate     # eth_call only, no money
    scripts/xstocks_v2_solana_roundtrip.py once --confirm-live

This uses no Kraken money: it starts from the USDG already sitting on X Layer.
The EVM -> Solana leg (``ccip_evm``) is the dialect that had no code until
2026-10-07, so every step here is defensive:

* the **destination ATA is checked before the bridge**, because the 2026-09-26
  loss was Token-2022 ``IncorrectProgramId`` on a missing ATA: the message is
  fixed once it is sent, and the coins stay locked;
* the calldata is **decoded and validated** (11 accounts, mint present, our ATA
  present, receiver non-zero, bitmap 772, slot 3 is this source chain) *before*
  it is signed -- a source-chain simulation only proves the message can leave;
* ``msg.value`` is never assumed: an unknown or zero fee is a refusal.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from decimal import Decimal, InvalidOperation

ROOT = Path = None  # replaced below; keeps import order obvious

from pathlib import Path as _Path

ROOT = _Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.xstocks_v2 import ccip_evm as EVM       # noqa: E402
from arb_monitor.xstocks_v2 import ccip_solana as C      # noqa: E402
from arb_monitor.xstocks_v2 import (cycle, executor as ex, incident, ledger,  # noqa: E402
                                    live, notify as v2notify)
from arb_monitor.xstocks_v2 import policy as policy_mod  # noqa: E402
from arb_monitor.xstocks_v2 import scan                  # noqa: E402
from arb_monitor.xstocks_v2 import ladder as ladder_core  # noqa: E402

USDG_XLAYER = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"
USDC_SOLANA = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WALLET = "AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev"
EVM_WALLET = "0xabababababababababababababababababababab"
# Solana xStocks are Token-2022 with a scaled UI: raw = UI x 10^8 / multiplier.
TOKEN_DECIMALS = 8
DATA = ROOT / "data-v2"
LOCK = DATA / "xstocks_v2_cycle.lock"
HALT = DATA / "xstocks_v2_HALT"
LEDGER = DATA / "xstocks_v2_cycles.jsonl"
ROUTE_SAMPLES = DATA / "xstocks_v2_route_samples.jsonl"
FAMILY = "xlayer_via_bridge_to_solana"
# The second ending for the same crossing. ``jupiter`` sells the token on
# Solana; ``kraken`` deposits it and sells into the Kraken bid, which is the
# ``chain_via_bridge_to_kraken`` family the scan has been showing all along and
# nothing could walk. Everything before the exit is identical.
JUPITER_EXIT = "jupiter"
KRAKEN_EXIT = "kraken"
KRAKEN_FAMILY = live.CHAIN_VIA_BRIDGE_TO_KRAKEN


def forward():
    """Reuse the forward-direction helpers (already exercised live)."""
    spec = importlib.util.spec_from_file_location(
        "v2fwd", ROOT / "scripts" / "xstocks_v2_xlayer_roundtrip.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def live_runner():
    """The resident live runner, loaded by path.

    This file's Kraken ending (``deposit`` / ``sell_kraken`` / ``verify_deposit``)
    uses the runner's own legs -- ``legs()``, ``_wait_credit``, ``_sell`` --
    instead of writing a second Kraken client (spec 3.1: one signing path). It
    used to call a module-level ``LIVE`` that was never bound anywhere.

    That was not theoretical: the first real family-5 run (2026-10-07) bought
    0.01106987 SPYx, bridged it (0.000791 OKB paid, tx ``0x46d630aa…``), saw the
    arrival on Solana at 22:49:14 -- and then died in the deposit step with
    ``credit_unreadable:NameError``. The coins are in our Solana wallet, a HALT
    is written and the lock is held. What was missing was the reference.
    """
    spec = importlib.util.spec_from_file_location(
        "v2liverunner", ROOT / "scripts" / "xstocks_v2_live.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FWD = forward()
#: The resident runner's Kraken legs (``legs()`` / ``_wait_credit`` / ``_sell``).
LIVE = live_runner()
log = FWD.log

#: X Layer RPC reads. ``rpc.xlayer.tech`` dropped a connection mid-handshake on
#: 2026-10-09 10:43 and the SSL error propagated out of ``usdg_balance`` (a call
#: that had not moved a single wei), so the whole probe became ``unknown`` and
#: the chain halted. A read is idempotent, so retrying cannot double-spend: three
#: tries with a short backoff, and a read that still fails returns ``None``.
#: ``None`` means "could not read", which is **not** zero (lessons A/D, 2.7).
XLAYER_RPC = "https://rpc.xlayer.tech"
XLAYER_RPC_TRIES = 3


def xlayer_rpc(http, method: str, params: list, *, tries: int = XLAYER_RPC_TRIES,
               timeout: int = 30) -> str | None:
    """One X Layer JSON-RPC read, retried. ``None`` = could not read."""
    last: Exception | None = None
    for attempt in range(max(1, int(tries))):
        try:
            payload = http.session.post(XLAYER_RPC, json={
                "jsonrpc": "2.0", "id": 1, "method": method,
                "params": params}, timeout=timeout).json()
        except Exception as error:  # noqa: BLE001 - transport/SSL/JSON
            last = error
            time.sleep(0.5 * (attempt + 1))
            continue
        raw = (payload or {}).get("result")
        return None if not raw or raw == "0x" else str(raw)
    log(f"xlayer rpc unreadable after {tries} tries: "
        f"{type(last).__name__}: {str(last)[:120]}")
    return None


def usdg_balance(http, owner: str = EVM_WALLET) -> Decimal | None:
    """USDG on X Layer is **6 decimals**; ``probe.xlayer_balance`` divides by 1e18
    because it was written for the 18-decimal xStocks. Using it for USDG read
    63.4 as 6.3e-11 (found 2026-10-07)."""
    data = "0x70a08231" + "0" * 24 + owner[2:].lower()
    raw = xlayer_rpc(http, "eth_call", [{"to": USDG_XLAYER, "data": data}, "latest"])
    if raw is None:
        return None
    return Decimal(int(raw, 16)) / Decimal(10 ** 6)


APPROVE_SELECTOR = "095ea7b3"      # keccak("approve(address,uint256)")[:4]


def approve_calldata(spender: str, amount: int) -> str:
    """Exact approval, never unlimited (spec section 3.1)."""
    return "0x" + APPROVE_SELECTOR + EVM.address_word(spender).hex() \
        + amount.to_bytes(32, "big").hex()


def allowance(http, token: str, owner: str, spender: str) -> int | None:
    data = "0xdd62ed3e" + "0" * 24 + owner[2:].lower() + "0" * 24 + spender[2:].lower()
    raw = xlayer_rpc(http, "eth_call", [{"to": token, "data": data}, "latest"])
    return None if raw is None else int(raw, 16)


#: X Layer's USDG carries 6 decimals and the wallet refuses an ``--amount`` of
#: 0 outright, so nothing below one micro-unit can be expressed at all.
USDG_DECIMALS = 6


def dust_reason(spend, *, decimals: int = USDG_DECIMALS) -> str:
    """``""`` when ``spend`` can be sent, else the named refusal.

    Measured 2026-10-10 09:07: the X Layer wallet was down to **0.000001 USDG**
    after a day of gas top-ups, so ``spend = min(tokens x price, available x
    0.999)`` came out at 9.99e-7 and ``int(spend * 10**6)`` is 0. The wallet
    answered ``--amount must be greater than zero`` from *inside a
    broadcast-capable step*, so the runner classified it ``unknown`` and wrote a
    HALT + kept the lock -- for a plan that could never have sent anything.

    Nothing can move in this case, so it is a refusal, and it has a name the
    board can print (AGENTS.md: 不允许的沉默).
    """
    try:
        amount = Decimal(str(spend))
    except (InvalidOperation, TypeError, ValueError):
        return "spend_unreadable"
    smallest = Decimal(1) / Decimal(10 ** decimals)
    if int(amount * Decimal(10 ** decimals)) <= 0:
        return f"below_min_notional:{amount:f}<{smallest:f}"
    return ""


def native_balance(http, owner: str = EVM_WALLET) -> Decimal | None:
    """Native OKB on X Layer, or ``None`` when the read does not answer.

    The bridge fee is ``msg.value``, so it can only be paid in the source chain's
    gas token. On 2026-10-07 the chain answered the dry-run with "insufficient
    funds for gas * price + value: have 605621410468124 want 791242725185203",
    which is the failure that looks like "the bridge silently did nothing".
    """
    raw = xlayer_rpc(http, "eth_getBalance", [owner, "latest"])
    if not isinstance(raw, str) or not raw.startswith("0x") or raw == "0x":
        return None
    try:
        return Decimal(int(raw, 16)) / Decimal(10 ** 18)
    except ValueError:
        return None


def gas_shortfall(http, *, fee_wei, owner: str = EVM_WALLET) -> str | None:
    """``None`` when the wallet can pay the fee, else the refusal's name.

    A read that does not answer is ``gas_unknown``, a refusal: an unknown
    balance is not a funded one, and the alternative is buying a token the
    wallet then cannot move.
    """
    if not fee_wei:
        return None
    have = native_balance(http, owner)
    if have is None:
        return f"gas_unknown:{owner}"
    want = Decimal(int(fee_wei)) / Decimal(10 ** 18)
    if have < want:
        return f"gas_low:{have} < {want}"
    return None


def solana_token_amount(probe, http, mint: str) -> int:
    """Raw Token-2022 amount we hold on Solana.

    ``probe`` is the probe module itself (``FWD.load_probe()``); calling
    ``load_probe()`` on it was the 2026-10-07 crash that halted the first
    reverse run *before* the bridge broadcast.
    """
    return probe.solana_balance(http, mint)


def ata_exists(fwd, http, ata: str) -> bool:
    value = http.rpc(fwd.SOLANA_RPC, "getAccountInfo",
                     [ata, {"encoding": "base64"}]).get("value")
    return bool(value)


def plan(args) -> dict:
    policy = policy_mod.V2Policy.load()
    probe = FWD.load_probe()
    http = probe.Http()
    product = probe.book_symbol(args.symbol)
    config_key, config_data = probe.discover_token_config(
        http, args.symbol, product["mint"])
    custody = C.token_config_custody(config_data, mint=product["mint"],
                                    product_id=product["product_id"])
    ata = C.to_b58(C.associated_token_address(WALLET, product["mint"]))
    # Never size a trade off "the balance did not answer" (lessons 2.7 / A42):
    # ``usdg_balance(...) or 0`` made an unreadable wallet look empty and would
    # silently shrink every plan to zero.
    available = usdg_balance(http)
    if available is None:
        log("USDG balance on X Layer unreadable: refusing to size off '0'")
        return {"error": "usdg_unreadable", "symbol": args.symbol}
    floor = Decimal(args.assume_bridge_min) if args.assume_bridge_min else None
    price = Decimal(args.price)
    spend = min(Decimal(args.notional), available * Decimal("0.999"),
                policy.per_trade_quote_usdt)
    if args.tokens:
        # An explicit size is what a probe needs: the floor that makes the coins
        # usable at Kraken is a token amount, not a dollar amount, and
        # ``spend / price`` cannot hit it exactly.
        tokens = Decimal(args.tokens)
        spend = min(tokens * price, available * Decimal("0.999"))
    elif floor is None:
        tokens = spend / price
    else:
        tokens = min(spend / price, floor)
    # A ticket that cannot be expressed in USDG's smallest unit is refused
    # *here*, before any step exists: measured 2026-10-10, a drained wallet
    # sized a probe at 9.99e-7 and the wallet's own "amount must be greater than
    # zero" reached the executor from inside the buy step, which turned a
    # dust plan into ``unknown`` + HALT.
    dust = dust_reason(spend)
    if dust:
        log(f"{args.symbol}: {dust} (USDG available {available})")
        return {"error": dust, "symbol": args.symbol}
    return {"policy": policy, "probe": probe, "http": http, "product": product,
            "token_config": config_key, "config_data": config_data,
            "custody": C.to_b58(custody), "ata": ata, "available": available,
            "floor": floor, "price": price, "spend": spend,
            "ref_price": Decimal(args.ref_price) if args.ref_price else price,
            "tokens": tokens.quantize(Decimal("0.00000001")),
            "raw_in": int((tokens * Decimal(10 ** 8) / (scan.load_multipliers().get(
                args.symbol, Decimal("1")) or Decimal("1"))).to_integral_value())}


def precheck(ctx, raw_amount: int, fee_wei: int | None):
    """Everything that must be true before a single wei moves."""
    http, product = ctx["http"], ctx["product"]
    problems: list[str] = []
    if not ata_exists(ctx["probe"], http, ctx["ata"]):
        problems.append(f"destination_ata_missing:{ctx['ata']}")
    fee_ok, fee_reason = EVM.fee_guard(fee_wei)
    if not fee_ok:
        problems.append(fee_reason)
    else:
        # The fee is paid in the source chain's gas token. Checking it here
        # refuses *before* the buy leg, so a cycle can never buy a token it will
        # not be able to move (measured 2026-10-07: the chain itself answered
        # "insufficient funds for gas * price + value").
        short = gas_shortfall(http, fee_wei=fee_wei)
        if short:
            problems.append(short)
    accounts = EVM.build_accounts(
        source_chain="x-layer", mint=product["mint"],
        token_config=ctx["token_config"], custody=ctx["custody"], owner=WALLET,
        destination_ata=ctx["ata"])
    data, call = EVM.build_call(source_chain="x-layer",
                                token=product["evm_xlayer"],
                                amount=raw_amount, receiver=WALLET, accounts=accounts)
    allowed = allowance(http, product["evm_xlayer"], EVM_WALLET,
                        EVM.BRIDGE_CONTRACT)
    if allowed is None:
        problems.append("allowance_unreadable")
    elif allowed < raw_amount:
        problems.append("allowance_too_low")
    problems += EVM.validate_call(call, source_chain="x-layer",
                                  mint=product["mint"], custody=ctx["custody"],
                                  owner=WALLET, amount=raw_amount)
    return data, call, accounts, problems


def simulate(ctx, data: str, fee_wei: int):
    """``eth_call`` on the source chain: can the message leave at all?"""
    http = ctx["http"]
    params = [{"from": EVM_WALLET, "to": EVM.BRIDGE_CONTRACT,
               "value": hex(fee_wei), "data": data}, "latest"]
    last: Exception | None = None
    for attempt in range(XLAYER_RPC_TRIES):
        try:
            payload = http.session.post(XLAYER_RPC, json={
                "jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": params}, timeout=40).json()
        except Exception as error:  # noqa: BLE001 - transport/SSL/JSON
            last = error
            time.sleep(0.5 * (attempt + 1))
            continue
        return payload.get("result"), payload.get("error")
    # A simulation that never answered is not a simulation that passed.
    return None, f"transport:{type(last).__name__}:{str(last)[:120]}"


#: The one revert that is *expected* before the approve step has run. Everything
#: else is a real refusal, and the bridge must not be broadcast on it.
ALLOWANCE_REVERT = "insufficient allowance"


def simulate_outcome(ctx, data: str, fee_wei: int) -> tuple[bool, str]:
    """``(ok, reason)`` for the source-chain dry-run.

    ``ok`` is the only thing that may license a broadcast; ``reason`` is the
    decoded revert text (``ccip_evm.decode_revert_reason``), so a caller can
    tell "the allowance is not set yet" from "this call would fail anyway".
    """
    _, error = simulate(ctx, data, fee_wei)
    if not error:
        return True, ""
    return False, EVM.decode_revert_reason(error)


def dry_run_note(*, ok: bool, reason: str) -> str:
    """What the preflight prints. An allowance revert is expected, not an error."""
    if ok:
        return ("eth_call ok — the message can leave; it does NOT prove Solana "
                "accepts it")
    if ALLOWANCE_REVERT in reason.lower():
        return ("dry-run deferred: the allowance is still zero. The approve step "
                "runs first, and the bridge dry-runs again before broadcasting.")
    return f"eth_call error: {reason}"


def bridge_fee_wei(*, fallback: int, dest: str = "solana") -> int:
    """The fee this lane wants **now**, never below the measured constant.

    A45 (2026-10-09) taught the *cost model* to read the fee off the chain every
    round, but the leg that pays it still used the 2026-09-26 constant. Measured
    2026-10-09 12:28: the bridge dry-run reverted with
    ``InsufficientFee(provided=791242725185203, required=79x…)`` -- the constant
    was ~0.7% low -- so the probe stopped with the coins in our wallet.
    Underpaying stops a cycle; overpaying is refunded by the Router. So take the
    chain's number, keep the measured constant as a floor, and when the read
    fails say so and pay the floor (the dry-run still gates the broadcast).
    """
    # ``fee_quote`` speaks the feed's ``Http`` (``.request``), not the probe's
    # ``Http`` (``.session``): asking with the wrong one is the ``rpc:
    # AttributeError`` this function used to swallow.
    try:
        from arb_monitor.xstocks_v2.feeds.http import Http as FeedHttp
        quoted, why = EVM.fee_quote(http=FeedHttp(), rpc=XLAYER_RPC,
                                    source_chain="x-layer", sender=EVM_WALLET,
                                    dest_chain=dest)
    except Exception as error:  # noqa: BLE001 - a read that fails is not a licence
        log(f"bridge fee: chain read raised {type(error).__name__}; floor {fallback}")
        return int(fallback)
    if quoted is None:
        log(f"bridge fee: chain read failed ({why}); floor {fallback}")
        return int(fallback)
    wei = int(Decimal(str(quoted)) * Decimal(10 ** 18))
    if wei < int(fallback):
        log(f"bridge fee: chain says {wei} wei < floor {fallback}; paying the floor")
        wei = int(fallback)
    # +2% headroom. The fee moves with the destination chain's gas price: a quote
    # taken a minute before the send was 2.6e9 wei (0.0003%) short of what the
    # very same call then demanded. CCIP refunds an overpayment of the native
    # fee; an underpayment stops the cycle with the coins in our wallet.
    padded = wei + wei // 50
    log(f"bridge fee: chain says {wei} wei (floor {fallback}), paying {padded}")
    return padded


def bridge_step_runner(ctx, state, fee_wei):
    """The bridge leg: precheck -> dry-run -> broadcast.

    The dry-run used to exist only in the ``once`` preflight, where it could
    never pass: with the allowance still zero it reverts with ``ERC20:
    insufficient allowance`` on every run (measured 2026-10-07), and the
    preflight ignored the revert. So the one leg that moves the money across
    chains and pays a fee went out un-simulated.

    It now runs *here*, after the approve step has set the real allowance, and a
    revert refuses instead of broadcasting: nothing leaves the wallet, the coins
    stay where the executor can see them, and the reason is written down.
    ``eth_call`` also catches a source-chain balance too small for the fee, which
    is the failure that looks like "the bridge silently does nothing".
    """
    probe, http, product = ctx["probe"], ctx["http"], ctx["product"]

    def run() -> ex.Outcome:
        held = probe.xlayer_balance(http, product["evm_xlayer"]) or Decimal("0")
        amount_raw = int(held * Decimal(10 ** 18))
        # The fee the contract wants **now**, not the one it wanted on 2026-09-26.
        fee = bridge_fee_wei(fallback=fee_wei)
        data, _call, _accounts, problems = precheck(ctx, amount_raw, fee)
        if problems:
            return ex.Outcome(ex.REFUSED, f"precheck: {problems}")
        try:
            ok, why = simulate_outcome(ctx, data, fee)
        except Exception as exc:  # noqa: BLE001
            # An RPC that does not answer is not a licence to broadcast. One
            # retry (the step declares ``retry_once``); if it persists the
            # executor writes ``unknown`` + HALT with the coins in our wallet,
            # which is the state a human has to look at.
            return ex.Outcome(ex.RETRY, f"dry_run:{type(exc).__name__}")
        if not ok:
            return ex.Outcome(ex.REFUSED, f"dry_run: {why}",
                              held_at="dest_wallet")
        before = Decimal(str(solana_token_amount(probe, http, product["mint"])))
        state["sol_before"] = before
        out = broadcast(["wallet", "contract-call", "--chain", "xlayer",
                         "--to", EVM.BRIDGE_CONTRACT, "--input-data", data,
                         "--amt", str(fee), "--from", EVM_WALLET])
        if not out.get("ok"):
            return ex.Outcome(ex.RETRY, str(out.get("error"))[:200])
        state["bridge_tx"] = (out.get("data") or {}).get("txHash")
        return ex.Outcome(ex.OK, "", held_at="in_bridge",
                          observed={"tx": state["bridge_tx"], "raw": amount_raw})

    return run


_KRAKEN_BOX: dict = {}


def kraken_box() -> object:
    """v1's proven Kraken + wallet legs, built once per process.

    One signing path (spec section 3.1): the same ``KrakenIO`` and ``Wallet``
    the resident live runner uses, not a second client.
    """
    if not _KRAKEN_BOX:
        _KRAKEN_BOX["legs"] = LIVE.legs()
    return _KRAKEN_BOX["legs"]


def deposit_step_runner(ctx, state, *, legs=None):
    """The deposit leg: send our whole Solana balance of this token to Kraken.

    Extracted from the nested function so it can be tested without a chain, a
    wallet or a Kraken key -- the leg that halted the first family-5 run with a
    ``NameError`` (2026-10-07 22:49) had no test at all before this.

    The whole balance goes, not only this cycle's leg: an earlier probe put a
    slice there and the trade is supposed to carry it, not strand it.
    """
    probe, http, product = ctx["probe"], ctx["http"], ctx["product"]
    box = legs if legs is not None else kraken_box

    def run() -> ex.Outcome:
        raw = solana_token_amount(probe, http, product["mint"])
        if raw <= 0:
            return ex.Outcome(ex.STOPPED, "nothing on Solana to deposit",
                              held_at="wallet")
        tokens = Decimal(raw) / Decimal(10 ** TOKEN_DECIMALS)
        state["deposit_raw"] = raw
        state["deposit_tokens"] = tokens
        try:
            before = Decimal(str(box().kraken.balance().get(
                f"{product['symbol']}.T") or 0))
        except Exception as exc:                      # noqa: BLE001
            # Every failure mode is named, including the programmer kind: the
            # 22:49 run printed ``credit_unreadable:NameError``, which is how
            # the missing ``LIVE`` binding was found.
            return ex.Outcome(ex.REFUSED, f"credit_unreadable:{type(exc).__name__}")
        state["credit_before"] = before
        try:
            deposit_plan = box().kraken.deposit_plan(
                product["symbol"], product["mint"], tokens)
        except Exception as exc:                      # noqa: BLE001
            return ex.Outcome(ex.REFUSED, f"deposit_plan:{type(exc).__name__}")
        if not deposit_plan.ok:
            # Below the minimum the coins are simply not credited, and a later
            # top-up does not recover them (lessons 3.4). Stay in our wallet.
            return ex.Outcome(ex.STOPPED, f"deposit_gate:{deposit_plan.reason}",
                              held_at="wallet")
        state["deposit_destination"] = deposit_plan.destination
        incident.sent("solana_transfer",
                      f"{product['symbol']} {tokens} -> kraken "
                      f"{deposit_plan.destination}")
        sent = box().wallet.send(product["mint"], deposit_plan.destination,
                                 deposit_plan.raw_amount)
        if not sent["ok"]:
            # Not retried on purpose: a transfer that reports an error may still
            # be in flight, and a second one double-sends.
            return ex.Outcome(ex.STOPPED,
                              f"transfer:{str(sent.get('error'))[:160]}",
                              held_at="wallet")
        state["deposit_tx"] = sent["tx"]
        return ex.Outcome(ex.OK, "", held_at="kraken",
                          observed={"tx": sent["tx"],
                                    "raw": deposit_plan.raw_amount,
                                    "destination": deposit_plan.destination})

    return run


def deposit_verifier(ctx, state, *, legs=None):
    """Credit is a quantity increase, and dust does not count (lessons 3.4)."""
    product = ctx["product"]
    box = legs if legs is not None else kraken_box

    def check(outcome) -> ex.Outcome:
        multiplier = (scan.load_multipliers().get(product["symbol"])
                      or Decimal("1"))
        want = state["deposit_tokens"] * multiplier
        credit = LIVE._wait_credit(box().kraken, product["symbol"], want,
                                   state["credit_before"])
        if credit is None:
            return ex.Outcome(ex.UNKNOWN, "credit_unconfirmed", held_at="kraken")
        state["credit"] = credit
        return ex.Outcome(ex.OK, "", observed={"credited_units": str(credit)})

    return check


def sell_kraken_runner(ctx, state, *, legs=None, sell_fn=None):
    """Kraken 收尾：把入账的枚数吃进买一（只 taker，不挂 maker）。

    从嵌套函数提到模块级，理由和 ``deposit_step_runner`` 一样：family 5 与
    family 6 都收在这里，而这条腿到 2026-10-08 还**一条 hermetic 用例都没有**
    （2026-10-07 22:49 那一轮就是人工补完的）。这条腿静默失败最贵：币已经在
    Kraken 上，卖不出去就是库存。

    行为与提取前逐条一致：
    * 没有入账枚数（或 ≤0）→ ``STOPPED`` / ``held_at=kraken``，不去猜数量；
    * ``LIVE._sell`` 拒绝（盘口/闸门）→ ``STOPPED``，原因原样带 ``sell:`` 前缀；
    * 成交 → 记 ``sold``（txid / 均价 / 成交额 / 手续费）供对账步骤用。

    ``sell_fn`` 可注入：生产路径用 v1 那条**已验证过的** ``LIVE._sell``，
    测试注入假实现，不碰网络也不读密钥。
    """
    product = ctx["product"]
    box = legs if legs is not None else kraken_box
    sell = sell_fn if sell_fn is not None else LIVE._sell

    def run() -> ex.Outcome:
        credit = state.get("credit")
        if credit is None or credit <= 0:
            return ex.Outcome(ex.STOPPED, "no credited units to sell",
                              held_at="kraken")
        ref = Decimal(str(state.get("ref_price") or "0"))
        sold = sell(box().kraken, box().kraken.client, ctx["policy"],
                    symbol=product["symbol"], tokens=credit, ref=ref)
        if not sold["ok"]:
            return ex.Outcome(ex.STOPPED, f"sell:{sold['reason']}", held_at="kraken")
        state["sold"] = sold
        return ex.Outcome(ex.OK, "", observed={
            "tx": sold.get("txid"),
            "price": str(sold.get("avg") or sold.get("price")),
            "cost": str(sold.get("cost")), "fee": str(sold.get("fee"))})

    return run


def sell_step_runner(ctx, state):
    """Sell the whole Solana balance of this token for USDC (Jupiter).

    Module level on purpose: family 2 (Kraken -> Solana, no bridge) walks the
    same sell leg, and a second copy of "quote a ladder, then swap" is how the
    two directions drift apart.
    """
    probe, http, product = ctx["probe"], ctx["http"], ctx["product"]

    def run() -> ex.Outcome:
        raw = solana_token_amount(probe, http, product["mint"])
        if raw <= 0:
            return ex.Outcome(ex.STOPPED, "nothing on Solana to sell",
                              held_at="wallet")
        state["sol_raw"] = raw
        state["before_usdc"] = solana_usdc(http, probe)
        # A single quote at the full size is a point, not a curve. Check 25/50/
        # 75/100% before hitting the route, and keep the full-size quote as the
        # prediction the reconcile gate compares against.
        capacity_ok, ladder_reason, full_out = ladder_confirm(
            probe=probe, http=http, src=product["mint"], dst=USDC_SOLANA,
            total_raw=raw,
            max_bps=Decimal(ctx["policy"].slippage_buffer_bps))
        state["sell_ladder"] = ladder_reason
        if not capacity_ok:
            return ex.Outcome(ex.REFUSED, f"sell ladder: {ladder_reason}")
        state["expected_usdc_raw"] = full_out
        out = broadcast(["swap", "execute", "--chain", "solana",
                         "--from", product["mint"], "--to", USDC_SOLANA,
                         "--amount", str(raw), "--wallet", WALLET,
                         "--slippage", "0.5"])
        if not out.get("ok"):
            return ex.Outcome(ex.RETRY, str(out.get("error"))[:200])
        state["sell_tx"] = (out.get("data") or {}).get("swapTxHash")
        return ex.Outcome(ex.OK, "", held_at="wallet",
                          observed={"tx": state["sell_tx"]})

    return run


def sell_verifier(ctx, state):
    """USDC balance up = sold. A hash is not a broadcast (see §15.5)."""
    probe, http = ctx["probe"], ctx["http"]

    def check(outcome) -> ex.Outcome:
        for _ in range(18):
            time.sleep(10)
            now = solana_usdc(http, probe)
            if now > state["before_usdc"]:
                state["after_usdc"] = now
                return ex.Outcome(ex.OK, "", observed={
                    "usdc_before": str(state["before_usdc"]),
                    "usdc_after": str(now)})
        status = FWD.wallet_status(state.get("sell_tx"), "solana")
        if status == "ERROR":
            return ex.Outcome(ex.RETRY, "sell was never broadcast")
        return ex.Outcome(ex.UNKNOWN, f"sell status={status} but USDC unchanged",
                          held_at="wallet")

    return check


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("plan", "simulate", "once"))
    parser.add_argument("--symbol", default="SPYx")
    parser.add_argument("--notional", default="8")          # USDG to spend
    parser.add_argument("--price", default="780")
    parser.add_argument("--assume-bridge-min", default=None,
                        help="measured LOWER bound of the bridge minimum (tokens)")
    parser.add_argument("--tokens", default=None,
                        help="buy this exact token amount instead of notional/price")
    parser.add_argument("--ref-price", default=None,
                        help="the Kraken bid the decision used; the reconcile gate "
                             "compares the fill against this, never against a guess")
    parser.add_argument("--exit", choices=(JUPITER_EXIT, KRAKEN_EXIT),
                        default=JUPITER_EXIT,
                        help="jupiter = sell the token on Solana; kraken = deposit "
                             "it and sell into the Kraken bid")
    parser.add_argument("--fee-wei", default=None,
                        help="msg.value for the bridge; the 2026-09-26 sample was "
                             "791242725185203 wei of OKB")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--probe-only", action="store_true",
                        help="stop once the tokens land on Solana: no sell, the "
                             "coins stay in our wallet and the arrival is written "
                             "to the route ledger")
    parser.add_argument("--resolve", action="store_true")
    args = parser.parse_args()

    ctx = plan(args)
    if ctx.get("error"):
        # A refusal the log alone knows about is a silent one (AGENTS.md
        # 不允许的沉默): the resident runner reads ``see_ledger`` and the board
        # reads the ledger, so a plan that never reached a step must still say
        # why. Measured 2026-10-10 09:32: the dust refusal (A101) printed to the
        # log and left no row, so the live snapshot showed an empty reason.
        reason = str(ctx["error"])
        log(f"refused: {reason}")
        # ``once`` is the money path; a ``plan``/``simulate`` run is a read and
        # must not leave a live row behind.
        if args.mode == "once":
            ledger.record(
                ledger.row(symbol=str(args.symbol), chain="x-layer", family=FAMILY,
                           state=ledger.REFUSED, mode="live",
                           size_usdt=args.notional, reason=reason),
                LEDGER)
        return 1
    fee_wei = int(args.fee_wei) if args.fee_wei else None
    raw_in = ctx["raw_in"]
    data, call, accounts, problems = precheck(ctx, raw_in, fee_wei)
    log(f"symbol {args.symbol}: mint {ctx['product']['mint']}")
    log(f"token config {ctx['token_config']}  custody {ctx['custody']}")
    log(f"our Solana ATA {ctx['ata']}  exists={ata_exists(ctx['probe'], ctx['http'], ctx['ata'])}")
    log(f"X Layer USDG {ctx['available']}  spend {ctx['spend']}  "
        f"-> {ctx['tokens']} tokens (raw {raw_in})")
    log(f"bridge: dest={EVM.SOLANA_CHAIN_SELECTOR} bitmap={call.bitmap} "
        f"accounts={len(call.accounts)} fee={fee_wei}")
    # ``once`` fixes a missing allowance with its own approve step first; for
    # plan/simulate it stays a refusal so the gap is visible.
    blocking = [p for p in problems
                if p != "allowance_too_low" or args.mode != "once"]
    if blocking:
        log(f"PRECHECK REFUSES: {blocking}")
        return 2
    if "allowance_too_low" in problems:
        log("allowance is zero: the approve step will set exactly this amount")
    log("precheck ok (11 accounts, mint and ATA present, receiver non-zero, "
        "bitmap 772, slot 3 = x-layer)")
    if args.mode == "plan":
        return 0
    # The preflight dry-run is informational and, before the approve step, it can
    # only ever revert on the allowance -- which is why ``once`` re-runs it
    # inside the bridge step, where it does gate the broadcast.
    ok, why = simulate_outcome(ctx, data, fee_wei)
    log(dry_run_note(ok=ok, reason=why))
    if args.mode == "simulate":
        return 0 if ok or ALLOWANCE_REVERT in why.lower() else 1
    if not args.confirm_live:
        raise SystemExit("once needs --confirm-live")
    return run_steps(ctx, raw_in, fee_wei, probe_only=args.probe_only,
                     exit_kind=args.exit)


_LAST_BROADCAST = {"at": 0.0}
_MIN_GAP_S = 45.0          # space wallet broadcasts out
_QUIET_WAIT_S = 240.0


def wallet_quiet(chain: str, address: str) -> bool:
    """True when the wallet service shows no unconfirmed transaction for us.

    The delegated account refuses a new broadcast while one is in flight
    (``in-flight transaction limit reached``), and it refuses *after* handing
    back an ``ok:true`` and a txHash that never reaches a chain. Waiting for
    quiet is cheaper than discovering that from ``wallet history`` later.
    """
    result = FWD.onchainos(["wallet", "history", "--chain", chain,
                            "--address", address])
    for row in result.get("data") or []:
        if str(row.get("txStatus") or "").upper() in {"PENDING", "CONFIRMING", "IN_FLIGHT"}:
            return False
    return True


def broadcast(args: list[str], *, chain: str = "196", address: str = EVM_WALLET):
    """Throttled broadcast: wait for quiet, keep a minimum gap, then send."""
    waited = 0.0
    while waited < _QUIET_WAIT_S and not wallet_quiet(chain, address):
        log("wallet has a transaction in flight; waiting")
        time.sleep(15)
        waited += 15
    gap = _MIN_GAP_S - (time.time() - _LAST_BROADCAST["at"])
    if gap > 0:
        time.sleep(gap)
    _LAST_BROADCAST["at"] = time.time()
    # Written *before* the send, and deliberately: a CLI call that times out may
    # still have reached the chain, so "we asked to send" is the honest floor
    # for "money may have moved" (2026-10-09). Every on-chain leg of this file
    # goes through here -- buy, approve, bridge, swap -- so one call covers them.
    incident.sent("onchainos:" + " ".join(args[:2]), " ".join(args[2:8]))
    return FWD.onchainos(args)


def run_steps(ctx, raw_in: int, fee_wei: int, *, probe_only: bool = False,
              exit_kind: str = JUPITER_EXIT) -> int:
    probe, http, product = ctx["probe"], ctx["http"], ctx["product"]
    state: dict = {"ref_price": ctx.get("ref_price")}

    def buy():
        before = probe.xlayer_balance(http, product["evm_xlayer"]) or Decimal("0")
        state["before_src"] = before
        spend_raw = int(ctx["spend"] * Decimal(10 ** 6))
        # Defense in depth: ``plan`` refuses dust, and this step refuses it
        # again. The wallet's own answer to a zero amount used to arrive from
        # inside a broadcast-capable step, which is what made it ``unknown``.
        dust = dust_reason(ctx["spend"])
        if dust:
            return ex.Outcome(ex.REFUSED, dust)
        out = broadcast(["swap", "execute", "--chain", "xlayer",
                             "--from", USDG_XLAYER, "--to", product["evm_xlayer"],
                             "--amount", str(spend_raw), "--wallet", EVM_WALLET,
                             "--slippage", "0.5", "--gas-level", "average"])
        if not out.get("ok"):
            return ex.Outcome(ex.RETRY, str(out.get("error"))[:200])
        state["buy_tx"] = (out.get("data") or {}).get("swapTxHash")
        return ex.Outcome(ex.OK, "", held_at="dest_wallet",
                          observed={"tx": state["buy_tx"]})

    def verify_buy(outcome):
        for _ in range(18):
            time.sleep(10)
            now = probe.xlayer_balance(http, product["evm_xlayer"]) or Decimal("0")
            if now > state["before_src"]:
                state["src_after_buy"] = now
                return ex.Outcome(ex.OK, "", observed={"spyx": str(now)})
        status = FWD.wallet_status(state.get("buy_tx"), "196")
        if status == "ERROR":
            return ex.Outcome(ex.RETRY, "buy was never broadcast")
        return ex.Outcome(ex.UNKNOWN, f"buy status={status} but balance unchanged",
                          held_at="dest_wallet")

    def verify_bridge(outcome):
        for _ in range(50):                       # EVM -> Solana took ~26 min
            time.sleep(45)
            now = Decimal(str(solana_token_amount(probe, http, product["mint"])))
            if now > state["sol_before"]:
                state["sol_after"] = now
                # The crossing is proven *here* and nowhere else: the balance on
                # the far side rose. Writing it down is what turns one landing
                # into a reusable fact. Until 2026-10-07 this proof died with the
                # process, so the scan kept printing "这个币在这条桥上还没有到账
                # 样本" for a route we had already walked (operator).
                scan.record_route_sample(scan.route_sample(
                    symbol=product["symbol"], source_chain="x-layer",
                    dest_chain="solana", arrived=True,
                    # What actually landed, not what we asked for: the two can
                    # differ (the scaled UI multiplier is not 1). ``state
                    # ["raw"]`` never existed -- the bridge step puts it in
                    # ``observed``, so the first two rows recorded ``null``
                    # (found 2026-10-07 by reading the ledger back).
                    amount_raw=(int(now) - int(state.get("sol_before") or 0)),
                    dest_balance_after=now,
                    source="xlayer_to_solana_arrival",
                    extra={"x_layer_tx": state.get("bridge_tx"),
                           "mint": product["mint"],
                           # The two SPYx rows carry a scaled-UI string here.
                           # Rather than guess, say which units this row is in.
                           "units": "raw"}), ROUTE_SAMPLES)
                return ex.Outcome(ex.OK, "", observed={"raw": str(int(now))})
        status = FWD.wallet_status(state.get("bridge_tx"), "196")
        if status == "ERROR":
            return ex.Outcome(ex.RETRY, "bridge was never broadcast")
        return ex.Outcome(ex.UNKNOWN, "CCIP message not seen on Solana",
                          held_at="in_bridge")

    def reconcile():
        """Predicted vs realized USDC for the closed cycle (operator 2026-10-07).

        Prediction = the aggregator quote read before the swap. Realization =
        the USDC balance delta. Denominator = the notional, so a cent of noise
        on a small trade is not reported as a blow-up. No quote means the cycle
        is written down as unmeasured, never compared against a guess.
        """
        expected = state.get("expected_usdc_raw")
        before = state.get("before_usdc")
        if expected is None or before is None:
            return None
        after = solana_usdc(http, probe)
        predicted = Decimal(str(expected)) / Decimal(10 ** 6)
        realized = (after - before) / Decimal(10 ** 6)
        return predicted, realized, max(predicted, abs(realized))

    policy = ctx["policy"]
    family = KRAKEN_FAMILY if exit_kind == KRAKEN_EXIT else FAMILY

    # ---- the Kraken ending ------------------------------------------------
    # Everything above is the same crossing the Jupiter ending uses. This is the
    # piece the scan kept asking for: the tokens are on Solana, Kraken credits
    # this symbol there, so deposit and sell into the bid.
    def reconcile_kraken():
        """The Kraken fill against the price the decision used.

        Prediction = that price times the credited units. Realization = the
        fill's gross cost. The taker fee is its own cost line, so including it
        here would charge it twice.
        """
        credit, price = state.get("credit"), state.get("ref_price")
        sold = state.get("sold") or {}
        if credit is None or price is None or sold.get("cost") in (None, ""):
            return None
        predicted = Decimal(str(price)) * Decimal(str(credit))
        realized = Decimal(str(sold["cost"]))
        return predicted, realized, max(predicted, abs(realized))

    def gate() -> tuple[bool, str]:
        allowed, reason = live.live_gate(
            policy, halt=HALT.exists(), lock=False,
            kill=(DATA / "KILL_SWITCH").exists(),
            auto_halt=(DATA / "AUTO_HALT").exists(), v1_busy=FWD.v1_busy(),
            family=family, chain="x-layer",
            allowed_chains=frozenset({"solana", "x-layer"}))
        return allowed, reason

    def alert(*, state: str, held_at: str, reason: str) -> None:
        v2notify.sender("alert")(v2notify.format_hold(
            symbol=ctx["product"]["symbol"], family=family, state=state,
            held_at=held_at, reason=reason))

    def approve():
        held = probe.xlayer_balance(http, product["evm_xlayer"]) or Decimal("0")
        amount_raw = int(held * Decimal(10 ** 18))
        # The bridge pulls with transferFrom, so it needs an allowance first
        # (eth_call said "ERC20: insufficient allowance" -- found 2026-10-07).
        # Exact approval only: the amount of this one message.
        out = broadcast(["wallet", "contract-call", "--chain", "xlayer",
                             "--to", product["evm_xlayer"],
                             "--input-data", approve_calldata(EVM.BRIDGE_CONTRACT,
                                                              amount_raw),
                             "--from", EVM_WALLET])
        if not out.get("ok"):
            return ex.Outcome(ex.REFUSED, str(out.get("error"))[:200])
        state["approve_tx"] = (out.get("data") or {}).get("txHash")
        for _ in range(12):
            time.sleep(5)
            seen = allowance(http, product["evm_xlayer"], EVM_WALLET,
                             EVM.BRIDGE_CONTRACT)
            if seen is not None and seen >= amount_raw:
                return ex.Outcome(ex.OK, "", observed={"tx": state["approve_tx"],
                                                      "amount": amount_raw})
        return ex.Outcome(ex.UNKNOWN, "approve not visible", held_at="dest_wallet")

    steps = [
        ex.Step("buy", buy, held_at="dest_wallet", broadcasts=True,
                inventory=True, retry_once=True, verify=verify_buy),
        ex.Step("approve", approve, held_at="dest_wallet", broadcasts=True,
                inventory=True),
        ex.Step("bridge", bridge_step_runner(ctx, state, fee_wei),
                held_at="in_bridge", broadcasts=True,
                inventory=True, retry_once=True, verify=verify_bridge),
    ]
    if not probe_only:
        if exit_kind == KRAKEN_EXIT:
            steps.append(ex.Step("deposit", deposit_step_runner(ctx, state),
                                 held_at="kraken", broadcasts=True,
                                 inventory=True,
                                 verify=deposit_verifier(ctx, state)))
            steps.append(ex.Step("sell", sell_kraken_runner(ctx, state),
                                 held_at="kraken", broadcasts=True,
                                 retry_once=False))
        else:
            steps.append(ex.Step("sell", sell, held_at="wallet", broadcasts=True,
                                 retry_once=True, verify=verify_sell))
    if probe_only:
        reconcile_fn = None
    elif exit_kind == KRAKEN_EXIT:
        reconcile_fn = reconcile_kraken
    else:
        reconcile_fn = reconcile
    lock = cycle.CycleLock(LOCK, HALT, label=f"{ctx['product']['symbol']} {family}",
                           symbol=ctx["product"]["symbol"], family=family)
    machine = ex.Executor(symbol=ctx["product"]["symbol"], chain="x-layer",
                          family=family, mode="live", size_usdt=ctx["spend"],
                          tokens=ctx["tokens"], steps=steps, gate=gate,
                          ledger_path=LEDGER, halt_path=HALT, notify=alert,
                          progress=lock.update,
                          reconcile=reconcile_fn,
                          # A probe ends holding the token in our own wallet.
                          # ``sold`` and ``stopped`` are both untrue, and the
                          # next trade deposits this slice with its own leg.
                          final_state=(ledger.PROBED if probe_only
                                       else ledger.SOLD),
                          final_reason=("probe arrived; route sample written"
                                        if probe_only else "both legs done"),
                          # A verified arrival is no longer "in_bridge": the
                          # bridge step's held_at is its in-flight worst case.
                          final_held_at=("dest_wallet" if probe_only else ""))
    with lock:
        result = machine.run()
        if result["keep_lock"]:
            lock.hold = True
    print(json.dumps(result, indent=1))
    ok = ex.SOLD if not probe_only else ledger.PROBED
    return 0 if result["state"] == ok else 1


def solana_usdc(http, probe) -> Decimal:
    rows = http.rpc(FWD.SOLANA_RPC, "getTokenAccountsByOwner",
                    [WALLET, {"mint": USDC_SOLANA}, {"encoding": "jsonParsed"}])
    total = Decimal("0")
    for row in (rows.get("value") or []):
        total += Decimal(str(row["account"]["data"]["parsed"]["info"]
                             ["tokenAmount"]["amount"]))
    return total


def quote_out(probe, http, *, src: str, dst: str, amount_raw: int,
              chain: str = "solana") -> int | None:
    """The aggregator's ``toTokenAmount`` for a sell we are about to make.

    This is the only number the reconcile gate may call a *prediction*: it is
    read before the swap from the same venue that will fill it. A hand-typed
    reference price is not a prediction, so a missing quote stays unmeasured
    rather than being compared against a guess.
    """
    out = FWD.onchainos(["swap", "quote", "--chain", chain, "--from", src,
                         "--to", dst, "--amount", str(amount_raw),
                         "--swap-mode", "exactIn"])
    if not out.get("ok"):
        return None
    data = out.get("data") or []
    row = data[0] if isinstance(data, list) and data else {}
    amount = row.get("toTokenAmount") if isinstance(row, dict) else None
    return int(amount) if amount else None


def ladder_confirm(*, probe, http, src: str, dst: str, total_raw: int,
                   max_bps: Decimal, chain: str = "solana"
                   ) -> tuple[bool, str, int | None]:
    """Quote the sell route at 25/50/75/100% and check the shape.

    Returns ``(ok, reason, full_size_out_raw)``. A size the route cannot price
    is an incomplete ladder, not a smaller trade.
    """
    sizes = ladder_core.ladder_sizes(total_raw)
    if not sizes:
        return False, f"{ladder_core.INCOMPLETE}:zero_size", None
    points: list[tuple[int, Decimal]] = []
    outs: dict[int, int] = {}
    for size in sizes:
        out = quote_out(probe, http, src=src, dst=dst, amount_raw=size, chain=chain)
        if out is None:
            return False, f"{ladder_core.INCOMPLETE}:{size}", None
        outs[size] = out
        points.append((size, ladder_core.average_price(size, out)))
    ok, reason = ladder_core.ladder_ok(points, max_bps=max_bps)
    return ok, reason, (outs[max(sizes)] if ok else None)


def cycle_once(*, symbol: str = "SPYx", notional: str = "8", price: str = "780",
               bridge_min: str | None = None,
               fee_wei: str = "791242725185203",
               confirm_live: bool = False, probe_only: bool = False,
               tokens: str | None = None, exit_kind: str = JUPITER_EXIT,
               ref_price: str | None = None) -> int:
    """Entry point for the resident live runner.

    It drives the same ``once`` code path as the command line, so the resident
    process and a manual run cannot drift apart. The bridge fee is a required
    argument on purpose: an unknown fee is a refusal, not a zero.
    """
    argv = [sys.argv[0], "once", "--symbol", symbol, "--notional", notional,
            "--price", price, "--fee-wei", fee_wei]
    if bridge_min:
        argv += ["--assume-bridge-min", bridge_min]
    if tokens:
        argv += ["--tokens", tokens]
    if ref_price:
        argv += ["--ref-price", ref_price]
    if exit_kind != JUPITER_EXIT:
        argv += ["--exit", exit_kind]
    if probe_only:
        argv.append("--probe-only")
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
