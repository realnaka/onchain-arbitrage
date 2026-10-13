#!/usr/bin/env python3
"""Family 1 on TON: buy on ston.fi, deposit into Kraken, sell the bid.

Kraken credits ``<symbol> - TON`` (SPYx minimum 0.00160914, fee 0). The board
row is a direct deposit, not a bridge: buy the jetton, transfer it to the
address Kraken hands out, sell. There is no TON address of ours on that
deposit -- Kraken's address is the destination.

    scripts/xstocks_v2_ton_roundtrip.py plan --symbol SPYx --notional 25
    scripts/xstocks_v2_ton_roundtrip.py once --symbol SPYx --probe-only --confirm-live

The wallet mnemonic lives in ``config/ton.env`` (gitignored). The first run
creates it. A wallet with no TON is refused before any jetton moves.
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import sys
import time
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.venues.kraken_spot import (  # noqa: E402
    KrakenError, KrakenSpotClient, load_credentials)
from arb_monitor.xstocks_v2 import cycle, executor as ex, incident  # noqa: E402
from arb_monitor.xstocks_v2 import ledger, legs_eth, legs_ton, live  # noqa: E402
from arb_monitor.xstocks_v2 import notify as v2notify  # noqa: E402
from arb_monitor.xstocks_v2 import policy as policy_mod  # noqa: E402
from arb_monitor.xstocks_v2 import scan, store  # noqa: E402
from arb_monitor.xstocks_v2.feeds import census as census_feed  # noqa: E402
from arb_monitor.xstocks_v2.feeds import ton as ton_feed  # noqa: E402
from arb_monitor.xstocks_v2.feeds.http import Http  # noqa: E402

FAMILY = live.CHAIN_TO_KRAKEN
CHAIN = "ton"
DATA = ROOT / "data-v2"
LOCK = DATA / "xstocks_v2_cycle.lock"
HALT = DATA / "xstocks_v2_HALT"
LEDGER = DATA / "xstocks_v2_cycles.jsonl"
SCAN = DATA / "xstocks_v2_scan.json"
DEPOSIT_SAMPLES = DATA / "xstocks_v2_ton_deposits.jsonl"
ENV_PATH = ROOT / "config" / "ton.env"
PROXY = "http://127.0.0.1:4780"
TONCENTER = "https://toncenter.com/api/v2"
TONAPI = "https://tonapi.io/v2"
STON_ROUTERS = "https://api.ston.fi/v1/routers"


def _d(value, default: str | None = None) -> Decimal | None:
    try:
        if value in (None, ""):
            return Decimal(default) if default is not None else None
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal(default) if default is not None else None


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def live_runner():
    spec = importlib.util.spec_from_file_location(
        "v2liverunner_for_ton", ROOT / "scripts" / "xstocks_v2_live.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LIVE = live_runner()


def kraken_client() -> KrakenSpotClient:
    key, secret = load_credentials(ROOT / "config" / "kraken.env")
    return KrakenSpotClient(key, secret, proxy=PROXY, timeout=30)


def _json(http, url: str, payload: dict | None = None) -> dict | None:
    try:
        status, raw = http.request(
            url, data=None if payload is None else json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"})
    except Exception:  # noqa: BLE001
        return None
    if status != 200:
        return None
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return None
    return body if isinstance(body, dict) else None


def load_wallet() -> dict:
    """The project TON wallet. Created once, then only read.

    The file is ``config/ton.env``, which ``.gitignore`` excludes. A second
    call must return the same address: replacing the mnemonic would strand
    whatever the first one already holds.
    """
    if ENV_PATH.exists():
        words = ""
        for line in ENV_PATH.read_text().splitlines():
            if line.startswith("TON_MNEMONIC="):
                words = line.split("=", 1)[1].strip().strip('"')
        if not words:
            raise RuntimeError(f"{ENV_PATH} has no TON_MNEMONIC")
        return legs_ton.wallet_from_mnemonic(words)
    wallet = legs_ton.new_wallet()
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text(
        "TON_MNEMONIC=\"" + " ".join(wallet["words"]) + "\"\n"
        f"TON_ADDRESS={wallet['address']}\n")
    try:
        ENV_PATH.chmod(0o600)
    except OSError:
        pass
    log(f"created TON wallet {wallet['address']}")
    return wallet


def token_row(symbol: str) -> dict | None:
    census = census_feed.load_census(census_feed.DEFAULT_CENSUS)
    for row in census_feed.ton_pool_rows(census):
        if str(row.get("symbol")) == symbol:
            return row
    return None


def scan_row(symbol: str) -> dict:
    snapshot = store.read_json(SCAN, {}) or {}
    for row in snapshot.get("rows") or []:
        if (str(row.get("symbol")) == symbol
                and str(row.get("family")) == FAMILY
                and str(row.get("chain")) == CHAIN
                and str(row.get("buy_venue")) != "kraken"):
            return row
    return {}


def kraken_rail(client, symbol: str) -> dict:
    methods = client.deposit_methods(symbol)
    method = legs_ton.ton_method(methods)
    if not method:
        return {"ok": False, "reason": "no_ton_method",
                "methods": [str(row.get("method")) for row in methods or []]}
    name = str(method.get("method"))
    addresses = client.deposit_addresses(symbol, name)
    chosen = None
    for row in addresses or []:
        if isinstance(row, dict) and row.get("address"):
            chosen = row
            break
    if not chosen:
        return {"ok": False, "reason": "no_deposit_address", "method": name}
    return {"ok": True, "method": name, "address": str(chosen["address"]),
            "memo": legs_ton.deposit_memo(chosen),
            "min_units": legs_eth.deposit_min_units(method),
            "fee_units": legs_eth.deposit_fee_units(method)}


def native_nanoton(http, address: str) -> int | None:
    body = _json(http, f"{TONCENTER}/getAddressBalance?address={address}")
    if not body or not body.get("ok"):
        return None
    try:
        return int(body.get("result"))
    except (TypeError, ValueError):
        return None


def address_state(http, address: str) -> str | None:
    body = _json(http, f"{TONCENTER}/getAddressInformation?address={address}")
    if not body or not body.get("ok"):
        return None
    result = body.get("result") or {}
    return str(result.get("state") or "") or None


def seqno(http, address: str) -> int | None:
    """0 for a wallet that has not been deployed. Unreadable is ``None``, not 0."""
    state = address_state(http, address)
    if state is None:
        return None
    if state == "uninitialized":
        return 0
    body = _json(http, TONCENTER + "/jsonRPC", {
        "id": 1, "jsonrpc": "2.0", "method": "runGetMethod",
        "params": {"address": address, "method": "seqno", "stack": []}})
    if not body or not body.get("ok"):
        return None
    stack = ((body.get("result") or {}).get("stack") or [])
    if not stack:
        return None
    try:
        return int(stack[0][1], 16) if isinstance(stack[0][1], str) else int(stack[0][1])
    except (TypeError, ValueError, IndexError):
        return None


def jetton_account(http, owner: str, master: str) -> dict | None:
    """``{"wallet": str, "raw": int}`` or ``None`` when the account cannot be read.

    No jetton wallet yet is a real zero (``raw`` 0, ``wallet`` ""), not a
    failed read. tonapi answers 404 for an owner who has never held the jetton.
    """
    body = _json(http, f"{TONAPI}/accounts/{owner}/jettons/{master}")
    if body is None:
        # Distinguish "no such jetton" from "tonapi down" by a cheap account read.
        account = _json(http, f"{TONAPI}/accounts/{owner}")
        if account is None:
            return None
        return {"wallet": "", "raw": 0}
    wallet = ((body.get("wallet_address") or {}).get("address") or "")
    try:
        raw = int(body.get("balance") or 0)
    except (TypeError, ValueError):
        return None
    return {"wallet": str(wallet), "raw": raw}


def router_is_v2(http, router: str) -> bool | None:
    body = _json(http, f"{STON_ROUTERS}/{router}")
    if not body:
        return None
    try:
        return int(body.get("major_version")) == 2
    except (TypeError, ValueError):
        return None


def send(http, boc: bytes) -> str | None:
    encoded = base64.b64encode(boc).decode()
    body = _json(http, TONCENTER + "/jsonRPC", {
        "id": 1, "jsonrpc": "2.0", "method": "sendBoc",
        "params": {"boc": encoded}})
    if not body or not body.get("ok"):
        return None
    result = body.get("result") or {}
    return str(result.get("hash") or result.get("@extra") or "sent")


def quote_buy(http, product: dict, notional: Decimal) -> dict:
    decimals = int(product["quote_decimals"])
    amount_raw = int((notional * Decimal(10 ** decimals)).to_integral_value(ROUND_DOWN))
    payload = ton_feed.TonFeed(http).swap(
        product["quote_address"], product["token_address"], amount_raw)
    if not payload:
        return {"ok": False, "reason": "quote_no_output"}
    ask = int(payload["ask_units"])
    minimum = int(payload.get("min_ask_units") or 0)
    token_decimals = int(product["token_decimals"])
    tokens = Decimal(ask) / (Decimal(10) ** token_decimals)
    if tokens <= 0 or minimum <= 0:
        return {"ok": False, "reason": "quote_no_output"}
    needed = ("router_address", "ask_jetton_wallet")
    if any(not payload.get(key) for key in needed):
        return {"ok": False, "reason": "quote_missing_router"}
    version = router_is_v2(http, str(payload["router_address"]))
    if version is None:
        return {"ok": False, "reason": "router_version_unreadable"}
    if not version:
        return {"ok": False, "reason": "router_not_v2"}
    return {"ok": True, "amount_raw": amount_raw, "tokens": tokens,
            "min_ask_units": minimum, "price_usd": notional / tokens,
            "router": str(payload["router_address"]),
            "ask_wallet": str(payload["ask_jetton_wallet"])}


def plan(args) -> dict:
    policy = policy_mod.V2Policy.load()
    http = Http()
    symbol = str(args.symbol)
    product = token_row(symbol)
    if product is None:
        raise SystemExit(f"{symbol}: no {CHAIN} pool in the census")
    wallet = load_wallet()
    client = kraken_client()
    rail = kraken_rail(client, symbol)
    if not rail.get("ok"):
        raise SystemExit(f"{symbol}: {rail.get('reason')} {rail.get('methods', '')}")
    row = scan_row(symbol)
    notional = _d(args.notional) or policy.min_trade_quote_usdt
    tokens = _d(getattr(args, "tokens", None))
    if tokens is None and getattr(args, "probe_only", False):
        tokens = rail["min_units"]
    if tokens is not None:
        price = _d(row.get("buy_price_usd")) or _d(args.price) or Decimal(0)
        if price > 0:
            notional = (tokens * price).quantize(Decimal("0.01"))
    quote = quote_buy(http, product, notional)
    if not quote.get("ok"):
        raise SystemExit(f"{symbol}: {quote['reason']}")
    nano = native_nanoton(http, wallet["address"])
    return {
        "symbol": symbol, "chain": CHAIN, "family": FAMILY, "policy": policy,
        "product": product, "row": row, "rail": rail, "quote": quote,
        "notional": notional, "tokens": quote["tokens"], "http": http,
        "wallet": wallet, "nanoton": nano,
        "kraken": client, "probe_only": bool(getattr(args, "probe_only", False)),
        "price": _d(row.get("sell_price_usd")) or _d(args.price) or Decimal(0),
        "ref_buy": _d(row.get("buy_price_usd")) or quote["price_usd"],
    }


def gas_guard(ctx) -> tuple[bool, str]:
    nano = ctx.get("nanoton")
    if nano is None:
        return False, "ton_balance_unreadable"
    need = (legs_ton.SWAP_ATTACH_NANOTON + legs_ton.DEPOSIT_ATTACH_NANOTON
            + 50_000_000)
    if int(nano) < need:
        have = Decimal(int(nano)) / Decimal(10 ** 9)
        required = Decimal(need) / Decimal(10 ** 9)
        return False, f"gas_low:{have}<{required}"
    return True, ""


def _broadcast(ctx, destination: str, value: int, body) -> tuple[str | None, str]:
    http, wallet = ctx["http"], ctx["wallet"]
    current = seqno(http, wallet["address"])
    if current is None:
        return None, "seqno_unreadable"
    try:
        boc = legs_ton.external_boc(
            private_key=wallet["private_key"], seqno=current,
            wallet_id=wallet["wallet_id"], wallet_address=wallet["address"],
            destination=destination, value=value, body=body,
            state_init=wallet["state_init"])
    except Exception as exc:  # noqa: BLE001
        return None, f"boc:{type(exc).__name__}"
    sent = send(http, boc)
    if not sent:
        return None, "sendboc_rejected"
    return sent, ""


def buy_runner(ctx, state):
    def run() -> ex.Outcome:
        http, product, wallet = ctx["http"], ctx["product"], ctx["wallet"]
        held = jetton_account(http, wallet["address"], product["token_address"])
        if held is None:
            return ex.Outcome(ex.REFUSED, "token_balance_unreadable")
        state["tokens_before"] = held["raw"]
        quote = quote_buy(http, product, ctx["notional"])
        if not quote.get("ok"):
            return ex.Outcome(ex.REFUSED, quote["reason"])
        slip_bps = (quote["price_usd"] / ctx["ref_buy"] - 1) * Decimal(10000)
        if slip_bps > ctx["policy"].max_slippage_bps:
            return ex.Outcome(ex.REFUSED,
                              f"buy_price_worse:{slip_bps.quantize(Decimal('0.01'))}bps")
        offer = jetton_account(http, wallet["address"], product["quote_address"])
        if offer is None:
            return ex.Outcome(ex.REFUSED, "quote_balance_unreadable")
        if offer["raw"] < quote["amount_raw"] or not offer["wallet"]:
            return ex.Outcome(ex.REFUSED,
                              f"funding_short:{offer['raw']}<{quote['amount_raw']}")
        swap = legs_ton.swap_body(
            ask_jetton_wallet=quote["ask_wallet"], refund=wallet["address"],
            receiver=wallet["address"], min_ask_units=quote["min_ask_units"],
            deadline=int(time.time()) + 15 * 60)
        transfer = _swap_transfer(quote["amount_raw"], quote["router"],
                                  wallet["address"], swap)
        incident.sent("ton_swap", f"{ctx['symbol']} {ctx['notional']} via {quote['router']}")
        sent, why = _broadcast(ctx, offer["wallet"], legs_ton.SWAP_ATTACH_NANOTON, transfer)
        if not sent:
            return ex.Outcome(ex.UNKNOWN, f"swap_broadcast:{why}", held_at="wallet")
        state["buy_tx"] = sent
        state["quote"] = quote
        return ex.Outcome(ex.OK, "", held_at="wallet", observed={"tx": sent})

    return run


def _swap_transfer(amount: int, router: str, response: str, swap):
    """Jetton transfer whose forward payload is the swap cell, not a comment."""
    from pytoniq_core import Address, begin_cell

    return (begin_cell()
            .store_uint(legs_ton.JETTON_TRANSFER_OP, 32)
            .store_uint(0, 64)
            .store_coins(int(amount))
            .store_address(Address(router))
            .store_address(Address(response))
            .store_bit(False)
            .store_coins(legs_ton.SWAP_FORWARD_NANOTON)
            .store_bit(True)
            .store_ref(swap)
            .end_cell())


def buy_verifier(ctx, state):
    def check(outcome) -> ex.Outcome:
        if outcome.status != ex.OK:
            return outcome
        http, product, wallet = ctx["http"], ctx["product"], ctx["wallet"]
        for _ in range(20):
            held = jetton_account(http, wallet["address"], product["token_address"])
            if held is not None and held["raw"] > state["tokens_before"]:
                got = held["raw"] - state["tokens_before"]
                state["tokens_bought_raw"] = got
                state["jetton_wallet"] = held["wallet"]
                return ex.Outcome(ex.OK, "", observed={"raw": str(got)})
            time.sleep(3)
        return ex.Outcome(ex.UNKNOWN, "buy balance unchanged", held_at="wallet")

    return check


def deposit_runner(ctx, state):
    def run() -> ex.Outcome:
        http, product, wallet, rail = (
            ctx["http"], ctx["product"], ctx["wallet"], ctx["rail"])
        held = jetton_account(http, wallet["address"], product["token_address"])
        if held is None:
            return ex.Outcome(ex.REFUSED, "token_balance_unreadable")
        if held["raw"] <= 0 or not held["wallet"]:
            return ex.Outcome(ex.STOPPED, "nothing to deposit", held_at="wallet")
        decimals = int(product["token_decimals"])
        tokens = Decimal(held["raw"]) / (Decimal(10) ** decimals)
        minimum = rail.get("min_units")
        if minimum is not None and tokens < minimum:
            return ex.Outcome(ex.STOPPED,
                              f"deposit_gate:below_minimum({tokens}<{minimum})",
                              held_at="wallet")
        try:
            before = Decimal(str(ctx["kraken"].balance().get(f"{ctx['symbol']}.T") or 0))
        except KrakenError as exc:
            return ex.Outcome(ex.REFUSED, f"credit_unreadable:{str(exc)[:120]}")
        state["credit_before"] = before
        state["deposit_tokens"] = tokens
        body = legs_ton.jetton_transfer_body(
            amount=held["raw"], destination=rail["address"],
            response=wallet["address"],
            forward_nanoton=legs_ton.FORWARD_NOTIFY_NANOTON,
            comment=rail.get("memo") or "")
        incident.sent("ton_transfer",
                      f"{ctx['symbol']} {tokens} -> kraken {rail['address']}")
        sent, why = _broadcast(
            ctx, held["wallet"], legs_ton.DEPOSIT_ATTACH_NANOTON, body)
        if not sent:
            return ex.Outcome(ex.UNKNOWN, f"deposit_broadcast:{why}", held_at="wallet")
        state["deposit_tx"] = sent
        return ex.Outcome(ex.OK, "", held_at="kraken", observed={"tx": sent})

    return run


def deposit_verifier(ctx, state):
    def check(outcome) -> ex.Outcome:
        if outcome.status != ex.OK:
            return outcome
        expected = state["deposit_tokens"]
        credit = LIVE._wait_credit(ctx["kraken"], ctx["symbol"], expected,
                                   state["credit_before"], tries=40, pause=30.0)
        if credit is None:
            return ex.Outcome(ex.UNKNOWN, "credit_unconfirmed", held_at="kraken")
        state["credit"] = credit
        store.append_jsonl(DEPOSIT_SAMPLES, {
            "symbol": ctx["symbol"], "network": "TON",
            "method": ctx["rail"]["method"],
            "sent_tokens": format(expected, "f"),
            "credited_units": format(credit, "f"),
            "credit_per_token": format(credit / expected, "f") if expected else None,
            "tx": state.get("deposit_tx"),
            "at_ms": int(time.time() * 1000),
        })
        return ex.Outcome(ex.OK, "", observed={"credited_units": str(credit)})

    return check


def sell_runner(ctx, state):
    def run() -> ex.Outcome:
        credit = state.get("credit")
        if credit is None or credit <= 0:
            return ex.Outcome(ex.STOPPED, "no credited units to sell", held_at="kraken")
        sold = LIVE._sell(ctx["kraken"], ctx["kraken"], ctx["policy"],
                          symbol=ctx["symbol"], tokens=credit,
                          ref=Decimal(str(ctx.get("price") or 0)))
        if not sold.get("ok"):
            return ex.Outcome(ex.STOPPED, f"sell:{sold.get('reason')}", held_at="kraken")
        state["sold"] = sold
        return ex.Outcome(ex.OK, "", observed={"tx": sold.get("txid")})

    return run


def gate(ctx) -> tuple[bool, str]:
    busy, why = cycle.v1_busy(ROOT)
    allowed, reason = live.live_gate(
        ctx["policy"], halt=HALT.exists(), lock=False,
        kill=(DATA / "KILL_SWITCH").exists(),
        auto_halt=(DATA / "AUTO_HALT").exists(), v1_busy=busy,
        family=FAMILY, chain=CHAIN, allowed_chains=frozenset({CHAIN}))
    if not allowed and reason == "v1_live_busy":
        return False, f"v1_live_busy: {why}"
    return allowed, reason


def run_steps(ctx) -> dict:
    """Run the cycle and return the executor's result dict.

    Not an exit code: the resident runner's ``guard_family`` reads ``state`` /
    ``reason`` / ``held_at`` out of what this returns, so returning an ``int``
    makes every executable ton row raise
    ``AttributeError: 'int' object has no attribute 'get'`` one round after the
    probe already ran (the bug the eth executor hit and fixed on 2026-10-09 --
    this file was copied from it before the fix).
    """
    state: dict = {}
    allowed, reason = gas_guard(ctx)
    if not allowed:
        row = ledger.row(symbol=ctx["symbol"], chain=CHAIN, family=FAMILY,
                         state=ledger.REFUSED, mode="live",
                         size_usdt=ctx["notional"], reason=reason)
        ledger.record(row, LEDGER)
        print(json.dumps(row, indent=1))
        return {"symbol": ctx["symbol"], "family": FAMILY, "state": row["state"],
                "reason": reason, "held_at": "", "keep_lock": False}
    steps = [
        ex.Step("buy", buy_runner(ctx, state), held_at="wallet",
                broadcasts=True, inventory=True, verify=buy_verifier(ctx, state)),
        ex.Step("deposit", deposit_runner(ctx, state), held_at="kraken",
                broadcasts=True, inventory=True,
                verify=deposit_verifier(ctx, state)),
    ]
    if not ctx["probe_only"]:
        steps.append(ex.Step("sell", sell_runner(ctx, state), held_at="kraken",
                             broadcasts=True))

    def alert(*, state: str, held_at: str, reason: str) -> None:
        v2notify.sender("alert")(v2notify.format_hold(
            symbol=ctx["symbol"], family=FAMILY, state=state, held_at=held_at,
            reason=reason))

    lock = cycle.CycleLock(LOCK, HALT, label=f"{ctx['symbol']} {FAMILY} ton",
                           symbol=ctx["symbol"], family=FAMILY)
    machine = ex.Executor(
        symbol=ctx["symbol"], chain=CHAIN, family=FAMILY, mode="live",
        size_usdt=ctx["notional"], tokens=ctx["tokens"], steps=steps,
        gate=lambda: gate(ctx), ledger_path=LEDGER, halt_path=HALT,
        notify=alert, progress=lock.update,
        # ``probed`` -- not ``stopped``. ``stopped`` is a HOLD state, so a
        # deliberate sample probe that worked would write a HALT and stop the
        # whole chain for a human with nothing to reconcile.
        final_state=(ledger.PROBED if ctx["probe_only"] else ex.SOLD),
        final_reason=("probe: deposit minimum sent, credit sample recorded"
                      if ctx["probe_only"] else "both legs done"),
        final_held_at=("kraken" if ctx["probe_only"] else ""))
    with lock:
        result = machine.run()
        if result["keep_lock"]:
            lock.hold = True
    print(json.dumps(result, indent=1))
    return {**result, "symbol": ctx["symbol"], "family": FAMILY}


def cycle_once(*, symbol: str = "SPYx", notional: str = "25",
               price: str = "0", tokens: str | None = None,
               probe_only: bool = False, confirm_live: bool = False) -> int:
    ctx = plan(argparse.Namespace(
        symbol=symbol, notional=notional, price=price, tokens=tokens,
        probe_only=probe_only))
    log(f"ton family 1: {ctx['symbol']} {ctx['notional']} -> {ctx['tokens']} "
        f"quote {ctx['quote']['price_usd']} rail min {ctx['rail']['min_units']} "
        f"TON {ctx['nanoton']}")
    if not confirm_live:
        return 0
    result = run_steps(ctx)
    return 0 if result["state"] in (ex.SOLD, ex.STOPPED, ledger.PROBED) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "once"))
    parser.add_argument("--symbol", default="SPYx")
    parser.add_argument("--notional", default="25")
    parser.add_argument("--price", default="0")
    parser.add_argument("--tokens", default=None)
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args()
    ctx = plan(args)
    log(f"ton family 1: buy {ctx['notional']} -> {ctx['tokens']} {ctx['symbol']} "
        f"at {ctx['quote']['price_usd']} (row {ctx['ref_buy']}), "
        f"Kraken {ctx['price']}, rail {ctx['rail']['method']} "
        f"min {ctx['rail']['min_units']} memo={ctx['rail']['memo']!r}, "
        f"wallet {ctx['wallet']['address']} nano {ctx['nanoton']}")
    if args.mode == "plan":
        return 0
    if not args.confirm_live:
        raise SystemExit("once needs --confirm-live")
    result = run_steps(ctx)
    return 0 if result["state"] in (ex.SOLD, ex.STOPPED, ledger.PROBED) else 1


if __name__ == "__main__":
    raise SystemExit(main())
