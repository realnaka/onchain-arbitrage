#!/usr/bin/env python3
"""Family 1 on Ethereum: buy in the pool, deposit into Kraken, sell the bid.

This is the executor the board kept asking for. Until 2026-10-09 a row like
``SPCXx | eth | uniswap 164.65 -> kraken 165.91`` printed "阻挡原因：无" while
the live runner never looked at eth at all (``ALLOWED_CHAINS`` was a literal
``{"solana"}``), because the chain -> Kraken executor only existed for Solana.

    scripts/xstocks_v2_eth_roundtrip.py plan --symbol SPCXx --notional 25
    scripts/xstocks_v2_eth_roundtrip.py once --symbol SPCXx --notional 25 --confirm-live
    scripts/xstocks_v2_eth_roundtrip.py once --symbol SPCXx --tokens 0.0081 \
        --probe-only --confirm-live

Three legs, all borrowed rather than copied:

* **buy** -- ``onchainos swap`` quotes and builds the calldata (the same path
  the v1 STRCx deposit walked), then our own ``eth_call`` simulation gates it;
* **deposit** -- a plain ERC-20 transfer to the address Kraken hands out for
  ``<symbol> - Ethereum`` (``legs_eth.transfer_calldata``);
* **sell** -- ``xstocks_v2_live._sell`` / ``_wait_credit``, the Kraken legs the
  Solana families already use. One signing path per venue.

The ``--probe-only`` run buys the **deposit minimum**, deposits it and stops:
Kraken's credit ratio for this (symbol, Ethereum) pair has never been measured,
and 缺样本不是拒绝理由 -- the way to learn it is to walk it once with the
smallest amount that survives the rail. The slice stays on Kraken as carried
inventory, exactly like the Solana probes.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.venues.kraken_spot import (  # noqa: E402
    KrakenError, KrakenSpotClient, load_credentials)
from arb_monitor.xstocks_v2 import cycle, executor as ex, incident  # noqa: E402
from arb_monitor.xstocks_v2 import costs  # noqa: E402
from arb_monitor.xstocks_v2 import gas_prices  # noqa: E402
from arb_monitor.xstocks_v2 import ledger, legs_eth, live  # noqa: E402
from arb_monitor.xstocks_v2 import native_reserve  # noqa: E402
from arb_monitor.xstocks_v2 import notify as v2notify  # noqa: E402
from arb_monitor.xstocks_v2 import policy as policy_mod  # noqa: E402
from arb_monitor.xstocks_v2 import scan, store  # noqa: E402
from arb_monitor.xstocks_v2.feeds import census as census_feed  # noqa: E402
from arb_monitor.xstocks_v2.feeds import evm as evm_feed  # noqa: E402
from arb_monitor.xstocks_v2.feeds.http import Http  # noqa: E402

FAMILY = live.CHAIN_TO_KRAKEN
CHAIN = "eth"
ONCHAINOS_CHAIN = "ethereum"
DATA = ROOT / "data-v2"
LOCK = DATA / "xstocks_v2_cycle.lock"
HALT = DATA / "xstocks_v2_HALT"
LEDGER = DATA / "xstocks_v2_cycles.jsonl"
SCAN = DATA / "xstocks_v2_scan.json"
DEPOSIT_SAMPLES = DATA / "xstocks_v2_eth_deposits.jsonl"
PROXY = "http://127.0.0.1:4780"
EVM_WALLET = "0xabababababababababababababababababababab"
USDC_ETH = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDC_DECIMALS = 6
#: How many extra tries the wallet backend gets when it reports an in-flight
#: limit ("in-flight transaction limit reached for delegated accounts", measured
#: on the X Layer gas top-up 2026-10-09). The retry only covers *that* error:
#: a transaction that may be in flight is never simply resent.
WALLET_RETRIES = 3
WALLET_RETRY_PAUSE_S = 20.0
#: One swap plus an approve plus a transfer, from the measured policy
#: (``evm_gas_units`` = 441,831 on 2026-10-07). Used only for the *floor*
#: check: "can this wallet pay for one action at today's gas price".
GAS_UNITS_FOR_ONE_CYCLE = Decimal("441831")
#: Keep this much native ETH untouched, so a cycle that ends early cannot leave
#: the wallet unable to pay for the leg that gets us flat again.
GAS_LIMIT_BUFFER = Decimal("2")
#: Ticket rungs, largest first. On-chain gas is a *fixed* cost, so the same
#: spread that loses at 25 USDT can pay at 100 (operator 2026-10-08: 「25 起报，
#: 过了门槛的再往上加档」). The rung that runs is the largest one whose **fresh
#: quote** still clears the net bar after gas.
LADDER = (Decimal("100"), Decimal("75"), Decimal("50"), Decimal("25"))
#: A sample buys the deposit minimum **whatever the price says**.
#:
#: 缺样本不是拒绝理由 (operator 2026-10-09, restated 17:4x: 「无条件先拿样本」): the
#: missing arrival sample is the trigger, not the spread. So the net guard does
#: not apply to ``--probe-only`` at all -- what still applies is everything that
#: says "we cannot walk this at any price": no quote, no rail, unreadable
#: balance/gas, a failed simulation, or a row whose *observation* is bad
#: (``price_outlier`` / ``stale_or_skew`` are filtered before we get here).
#: Measured 2026-10-09: TSLAx quoted -219 bps against Kraken's bid; that refusal
#: was the version before this switch.
PROBE_UNCONDITIONAL = True
#: The probe ticket is the rail minimum **plus a margin**: the execution price
#: can come out above the row's snapshot price, and buying less than Kraken's
#: credit minimum would leave the coins uncredited (lessons 3.4).
PROBE_SIZE_MARGIN = Decimal("1.05")


def _d(value, default: str | None = None) -> Decimal | None:
    try:
        if value in (None, ""):
            return Decimal(default) if default is not None else None
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default) if default is not None else None


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def live_runner():
    """The resident live runner, for its verified Kraken legs."""
    spec = importlib.util.spec_from_file_location(
        "v2liverunner_for_eth", ROOT / "scripts" / "xstocks_v2_live.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LIVE = live_runner()


def kraken_client() -> KrakenSpotClient:
    key, secret = load_credentials(ROOT / "config" / "kraken.env")
    return KrakenSpotClient(key, secret, proxy=PROXY, timeout=30)


def onchainos(args: list[str], timeout: int = 90) -> dict:
    """Run one ``onchainos`` command and return its JSON payload."""
    env = dict(os.environ)
    env["HTTPS_PROXY"] = PROXY
    env["HTTP_PROXY"] = PROXY
    completed = subprocess.run(["onchainos", *args], capture_output=True,
                               text=True, timeout=timeout, env=env)
    text = (completed.stdout or completed.stderr or "").strip()
    payload = None
    for candidate in (text, *reversed(text.splitlines())):
        candidate = candidate.strip()
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
            break
        except json.JSONDecodeError:
            continue
    if payload is None:
        raise RuntimeError(f"onchainos {' '.join(args[:3])} unparsable: {text[:300]}")
    if completed.returncode != 0 and payload.get("ok") is not True:
        raise RuntimeError(json.dumps(payload, ensure_ascii=False)[:600])
    return payload


def _rows(payload) -> dict:
    data = payload.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    if isinstance(data, dict):
        return data
    return {}


# ---------------------------------------------------------------------------
# Chain reads (idempotent, so they may be retried; a failed read is None)
# ---------------------------------------------------------------------------
def eth_rpc(http, method: str, params: list, *, tries: int = 3):
    """One JSON-RPC call. ``Http.request`` answers ``(status, body_bytes)``."""
    chain = evm_feed.CHAINS[CHAIN]
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    last = ""
    for _ in range(max(1, tries)):
        try:
            status, raw = http.request(
                chain.rpc, data=body,
                headers={"Content-Type": "application/json"})
            if status != 200:
                last = f"http_{status}"
                continue
            payload = json.loads(raw.decode("utf-8", "replace"))
            if not isinstance(payload, dict):
                last = "not_an_object"
                continue
            if payload.get("error"):
                last = str(payload["error"])[:160]
                continue
            return payload.get("result")
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}"
        time.sleep(1.0)
    log(f"eth rpc unreadable after {tries} tries: {last}")
    return None


def eth_call(http, to: str, data: str) -> str | None:
    return eth_rpc(http, "eth_call", [{"to": to, "data": data}, "latest"])


def pending_nonce(http, owner: str = EVM_WALLET) -> int | None:
    """The pending nonce, or ``None``. ``None`` can never license a refusal."""
    raw = eth_rpc(http, "eth_getTransactionCount", [owner, "pending"])
    return None if raw is None else int(raw, 16)


def native_balance(http, owner: str = EVM_WALLET) -> Decimal | None:
    raw = eth_rpc(http, "eth_getBalance", [owner, "latest"])
    return None if raw is None else Decimal(int(raw, 16)) / Decimal(10 ** 18)


def erc20_balance(http, token: str, owner: str = EVM_WALLET,
                  decimals: int = 18) -> Decimal | None:
    raw = eth_call(http, token, "0x70a08231" + "0" * 24 + owner[2:].lower())
    return None if raw is None else Decimal(int(raw, 16)) / Decimal(10 ** decimals)


def erc20_allowance(http, token: str, spender: str,
                    owner: str = EVM_WALLET) -> int | None:
    data = ("0xdd62ed3e" + "0" * 24 + owner[2:].lower()
            + "0" * 24 + spender[2:].lower())
    raw = eth_call(http, token, data)
    return None if raw is None else int(raw, 16)


def receipt(http, tx_hash: str, *, tries: int = 30, pause: float = 4.0):
    for _ in range(tries):
        row = eth_rpc(http, "eth_getTransactionReceipt", [tx_hash])
        if row:
            return row
        time.sleep(pause)
    return None


def wallet_status(tx_hash: str | None) -> str:
    """What the wallet backend says about a broadcast we cannot find on chain."""
    if not tx_hash:
        return "unknown"
    try:
        payload = onchainos(["wallet", "history", "--chain", ONCHAINOS_CHAIN,
                             "--limit", "20"])
    except Exception:  # noqa: BLE001
        return "unknown"
    for row in (payload.get("data") or []):
        if not isinstance(row, dict):
            continue
        if str(row.get("txHash") or row.get("tx_hash") or "") == tx_hash:
            return str(row.get("status") or "unknown").upper()
    return "unknown"


def simulate(http, to: str, data: str) -> tuple[bool, str]:
    try:
        row = _rows(onchainos(["gateway", "simulate", "--chain", ONCHAINOS_CHAIN,
                               "--from", EVM_WALLET, "--to", to, "--data", data,
                               "--amount", "0"]))
    except Exception as exc:  # noqa: BLE001
        return False, f"simulate:{type(exc).__name__}:{str(exc)[:120]}"
    if row.get("failReason"):
        return False, str(row["failReason"])[:160]
    return True, ""


def broadcast(to: str, data: str, *, gas: str | None = None) -> str:
    args = ["wallet", "contract-call", "--chain", ONCHAINOS_CHAIN, "--to", to,
            "--input-data", data]
    if gas:
        args += ["--gas-limit", str(gas)]
    payload = onchainos(args)
    data_out = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    tx_hash = str(data_out.get("txHash") or "")
    if not tx_hash.startswith("0x"):
        raise RuntimeError(f"broadcast did not return txHash: {payload}")
    return tx_hash


def broadcast_with_retry(to: str, data: str, *, gas: str | None = None) -> str:
    """Retry the *in-flight limit* answer only.

    Measured 2026-10-09 on the X Layer gas top-up: the wallet backend refuses a
    second transaction while the delegated account still has one in flight
    ("in-flight transaction limit reached for delegated accounts"). That refusal
    means nothing was sent, so asking again after the first one lands is safe.
    Any other error is returned to the caller untouched.
    """
    last = ""
    for attempt in range(WALLET_RETRIES):
        try:
            return broadcast(to, data, gas=gas)
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
            if "in-flight" not in last and "inflight" not in last.lower():
                raise
            log(f"wallet busy (attempt {attempt + 1}/{WALLET_RETRIES}): {last[:120]}")
            time.sleep(WALLET_RETRY_PAUSE_S)
    raise RuntimeError(last)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------
def token_row(symbol: str) -> dict | None:
    """The Ethereum pool row for this symbol: token address, quote, decimals.

    Two sources on purpose. The v4 watch carries the hooked pools; the census
    carries the plain (v3-style) ones, and a symbol can live in either — measured
    2026-10-09, when the resident eth sample stage found TSLAx on the scan and
    this lookup answered "no eth pool in the v4 watch" (its pool is v3,
    ``0x8ad3c73f…``, depth $250). The buy leg is an aggregator quote, so what it
    needs is the token, not the pool.
    """
    v4 = census_feed.load_v4()
    for row in census_feed.v4_pool_rows(v4):
        if str(row.get("symbol")) == symbol and str(row.get("chain")) == CHAIN:
            return row
    for row in census_feed.census_pools(census_feed.load_census()):
        if str(row.get("symbol")) == symbol and str(row.get("chain")) == CHAIN:
            return row
    return None


def scan_row(symbol: str) -> dict:
    """This symbol's chain -> Kraken row on eth from the last scan snapshot."""
    snapshot = store.read_json(SCAN, {}) or {}
    for row in snapshot.get("rows") or []:
        if (str(row.get("symbol")) == symbol
                and str(row.get("family")) == FAMILY
                and str(row.get("chain")) == CHAIN
                and str(row.get("buy_venue")) != "kraken"):
            return row
    return {}


def quote_buy(http, product: dict, notional: Decimal) -> dict:
    """What the aggregator would give us right now, and the calldata to get it."""
    amount_raw = int(notional * Decimal(10 ** USDC_DECIMALS))
    quote = _rows(onchainos([
        "swap", "quote", "--chain", ONCHAINOS_CHAIN,
        "--from", product["quote_address"], "--to", product["token_address"],
        "--amount", str(amount_raw), "--swap-mode", "exactIn"]))
    tokens_raw = _d(quote.get("toTokenAmount"))
    if quote.get("action") == "block":
        return {"ok": False, "reason": f"quote_blocked:{quote.get('reason')}"}
    if tokens_raw is None or tokens_raw <= 0:
        return {"ok": False, "reason": "quote_no_output"}
    tokens = tokens_raw / (Decimal(10) ** int(product["token_decimals"]))
    return {"ok": True, "amount_raw": amount_raw, "tokens": tokens,
            "price_usd": notional / tokens,
            "impact_pct": _d(quote.get("priceImpactPercent")),
            "route": [str(item.get("dexProtocol", {}).get("dexName"))
                      for item in (quote.get("dexRouterList") or [])
                      if isinstance(item, dict)]}


def build_tx(http, product: dict, notional: Decimal, slippage_bps: Decimal) -> dict:
    amount_raw = int(notional * Decimal(10 ** USDC_DECIMALS))
    row = _rows(onchainos([
        "swap", "swap", "--chain", ONCHAINOS_CHAIN,
        "--from", product["quote_address"], "--to", product["token_address"],
        "--amount", str(amount_raw), "--wallet", EVM_WALLET,
        "--slippage", str(Decimal(slippage_bps) / Decimal(100)),
        "--swap-mode", "exactIn"]))
    tx = row.get("tx") or {}
    if not tx.get("to") or not tx.get("data"):
        raise RuntimeError(f"swap tx data missing: {json.dumps(row)[:200]}")
    return {"amount_raw": amount_raw, "to": tx["to"], "data": tx["data"],
            "gas": tx.get("gas")}


def ensure_allowance(http, product: dict, amount_raw: int, *,
                    force: bool = False) -> dict:
    """Approve exactly this trade's input amount, if it is not already there."""
    token = product["quote_address"]
    check = _rows(onchainos(["swap", "check-approvals", "--chain", ONCHAINOS_CHAIN,
                             "--address", EVM_WALLET, "--token", token]))
    tokens = check.get("tokens") or [{}]
    spendable = _d((tokens[0] if isinstance(tokens, list) else {}).get("spendable"), "0")
    if spendable is not None and spendable >= amount_raw:
        return {"state": "already_approved", "spendable": str(spendable)}
    row = _rows(onchainos(["swap", "approve", "--chain", ONCHAINOS_CHAIN,
                           "--token", token, "--amount", str(amount_raw)]))
    data = row.get("data") or row.get("dataHex")
    if not data:
        raise RuntimeError(f"approve calldata missing: {json.dumps(row)[:200]}")
    ok, why = simulate(http, token, data)
    if not ok:
        raise RuntimeError(f"approve simulation failed: {why}")
    tx_hash = broadcast_with_retry(token, data, gas="70000")
    row = receipt(http, tx_hash)
    if not row or str(row.get("status")) != "0x1":
        return {"state": "unknown", "tx": tx_hash}
    return {"state": "approved", "tx": tx_hash,
            "gas_used": int(str(row.get("gasUsed")), 16)}


def kraken_rail(client, symbol: str) -> dict:
    """Kraken's Ethereum deposit rail for this symbol: method, min, fee, address."""
    methods = client.deposit_methods(symbol)
    method = legs_eth.ethereum_method(methods)
    if not method:
        return {"ok": False, "reason": "no_ethereum_method",
                "methods": [str(row.get("method")) for row in methods or []]}
    name = str(method.get("method"))
    addresses = client.deposit_addresses(symbol, name)
    address = ""
    for row in addresses or []:
        if isinstance(row, dict) and row.get("address"):
            address = str(row["address"])
            break
    if not address:
        return {"ok": False, "reason": "no_deposit_address", "method": name}
    return {"ok": True, "method": name, "address": address,
            "min_units": legs_eth.deposit_min_units(method),
            "fee_units": legs_eth.deposit_fee_units(method)}


def credit_guard(symbol: str, *, units: dict | None, probe_only: bool,
                 min_units=None) -> tuple[bool, str]:
    """May this run sell, or must it stop at the sample first?

    Kraken credits a deposit in *its* units and the ratio is per (symbol,
    network). Until it is measured for this rail, the sell cannot be sized, so
    the trade path refuses and names the sample stage -- guessing 1:1 is how the
    EVM 1.0918 multiplier mistake happened on 2026-10-06.
    """
    if probe_only:
        return True, ""
    unit = (units or {}).get(symbol) or {}
    if unit.get("deposit_credit_units_per_balance_unit") is None:
        return False, (
            f"{symbol}: Kraken credit ratio for the Ethereum rail is not "
            "measured; run the sample probe first "
            f"(--probe-only, deposit min {min_units})")
    return True, ""


def rung_net(quote: dict, *, rung: Decimal, sell_price: Decimal,
             policy, gas_usdt: Decimal | None) -> tuple[Decimal | None, Decimal]:
    """``(net_usdt, edge_bps)`` for one rung, at that rung's own quote."""
    price = _d(quote.get("price_usd"))
    if price is None or price <= 0 or sell_price is None or sell_price <= 0:
        return None, Decimal(0)
    edge = (sell_price / price - 1) * Decimal(10000)
    gross = rung * edge / Decimal(10000)
    net = costs.net_after_costs(gross_usdt=gross, notional_usdt=rung,
                               fixed_cost_usd=Decimal(0), policy=policy,
                               family=FAMILY, gas_cost_usd=gas_usdt)
    return net, edge


def choose_rung(http, product: dict, *, policy, sell_price: Decimal,
                usdc_balance: Decimal | None, gas_usdt: Decimal | None,
                required_edge_bps: Decimal | None = None,
                ladder=LADDER) -> dict:
    """The largest rung that still pays, quoted at that rung.

    ``cap`` is the money the wallet can actually spend this trade (the project's
    sizing rule: ``min(单笔上限, 余额×0.999)``). Every rung is re-quoted: a size
    that clears at 25 can be full of price impact at 100, and the ledger must
    record the trade we would really make, not a size nobody priced.
    """
    if usdc_balance is None:
        return {"ok": False, "reason": "wallet_unreadable"}
    cap = min(Decimal(str(policy.per_trade_quote_usdt)),
              Decimal(str(usdc_balance)) * Decimal("0.999"))
    tried = []
    for rung in sorted(ladder, reverse=True):
        if rung > cap:
            tried.append({"rung": str(rung), "skip": "above_wallet_or_cap"})
            continue
        quote = quote_buy(http, product, rung)
        if not quote.get("ok"):
            tried.append({"rung": str(rung), "skip": quote.get("reason")})
            continue
        net, edge = rung_net(quote, rung=rung, sell_price=sell_price,
                             policy=policy, gas_usdt=gas_usdt)
        tried.append({"rung": str(rung), "edge_bps": str(edge.quantize(Decimal("0.1"))),
                      "net_usdt": None if net is None else str(net.quantize(Decimal("0.0001")))})
        if net is None or net < policy.min_net_profit_usdt:
            continue
        if required_edge_bps is not None and edge < Decimal(str(required_edge_bps)):
            continue
        return {"ok": True, "rung": rung, "quote": quote, "net_usdt": net,
                "edge_bps": edge, "cap": cap, "tried": tried}
    return {"ok": False, "reason": "no_rung_clears", "cap": cap, "tried": tried}


def plan(args) -> dict:
    policy = policy_mod.V2Policy.load()
    http = Http()
    symbol = str(args.symbol)
    product = token_row(symbol)
    if product is None:
        raise SystemExit(f"{symbol}: no {CHAIN} pool in the v4 watch")
    row = scan_row(symbol)
    client = kraken_client()
    rail = kraken_rail(client, symbol)
    if not rail.get("ok"):
        raise SystemExit(f"{symbol}: {rail.get('reason')} {rail.get('methods')}")

    probe_only = bool(getattr(args, "probe_only", False))
    eth = native_balance(http)
    gas_wei = evm_feed.gas_price_wei(http, evm_feed.CHAINS[CHAIN])
    gas_usdt = None
    if gas_wei is not None:
        price_row = ((store.read_json(DATA / "xstocks_v2_gas_prices.json", {}) or {})
                     .get("prices", {}).get("ETH", {}))
        gas_price = _d(price_row.get("usd"))
        if gas_price is None:
            # The scan keeps this fresh (gas_prices.GAS_TOKENS), but a plan run
            # must not depend on the resident process having restarted yet.
            gas_price, _source = gas_prices.price_eth_from_kraken(http)
        if gas_price is not None:
            gas_usdt = (GAS_UNITS_FOR_ONE_CYCLE * Decimal(gas_wei)
                        / Decimal(10 ** 18) * gas_price)

    # Read the bid we would actually hit. The scan row is a snapshot: measured
    # 2026-10-09, the pool moved 1.5% in five minutes while the money for this
    # ticket was on its way from Kraken, so a ladder judged on the row's old
    # price would have sized a losing trade.
    row_sell = _d(row.get("sell_price_usd")) or _d(args.price) or Decimal(0)
    sell_price = row_sell
    try:
        bids = (client.depth(symbol, count=3).get("bids") or [])
        if bids:
            sell_price = _d(bids[0][0]) or row_sell
    except KrakenError as exc:
        log(f"kraken depth unreadable ({str(exc)[:80]}); using the scan row")
    tokens = _d(args.tokens) if getattr(args, "tokens", None) else None
    notional = _d(getattr(args, "notional", None))
    if probe_only and tokens is None:
        tokens = rail["min_units"]
    if tokens is not None:
        # A probe buys exactly the amount the rail will credit, priced at the
        # row's own buy price. Anything smaller is not a sample, it is a loss.
        price = _d(row.get("buy_price_usd")) or _d(args.price) or Decimal(0)
        notional = (tokens * price * PROBE_SIZE_MARGIN).quantize(
            Decimal("0.01")) if price > 0 else None

    usdc_balance = erc20_balance(http, product["quote_address"],
                                 decimals=USDC_DECIMALS)
    rung: dict | None = None
    if notional is None:
        # No size was handed in: pick the rung that pays. Gas is a fixed cost,
        # so this is the whole difference between "never" and "at 100 USDT it
        # clears" (the live runner's path).
        rung = choose_rung(http, product, policy=policy, sell_price=sell_price,
                           usdc_balance=usdc_balance, gas_usdt=gas_usdt,
                           required_edge_bps=_d(row.get("required_edge_bps")))
        if not rung.get("ok"):
            raise SystemExit(
                f"{symbol}: {rung.get('reason')} cap={rung.get('cap')} "
                f"tried={rung.get('tried')}")
        notional, quote = rung["rung"], rung["quote"]
    else:
        quote = quote_buy(http, product, notional)
    if not quote.get("ok"):
        raise SystemExit(f"{symbol}: {quote['reason']}")
    ok, why = credit_guard(symbol, units=census_feed.kraken_symbols(),
                           probe_only=probe_only,
                           min_units=rail["min_units"])
    if not ok:
        raise SystemExit(why)
    return {
        "symbol": symbol, "chain": CHAIN, "family": FAMILY, "policy": policy,
        "product": product, "row": row, "rail": rail, "quote": quote,
        "notional": notional, "tokens": quote["tokens"], "http": http,
        "eth_balance": eth, "gas_wei": gas_wei, "gas_usdt": gas_usdt,
        "kraken": client, "probe_only": probe_only, "rung": rung,
        "usdc_balance": usdc_balance,
        "required_edge_bps": _d(row.get("required_edge_bps")),
        "price": sell_price,          # fresh Kraken bid when readable
        "row_sell_price": row_sell,   # what the scan snapshot said
        "ref_buy": _d(row.get("buy_price_usd")) or quote["price_usd"],
    }


# ---------------------------------------------------------------------------
# The legs
# ---------------------------------------------------------------------------
def buy_runner(ctx, state):
    def run() -> ex.Outcome:
        http, product = ctx["http"], ctx["product"]
        policy = ctx["policy"]
        before = erc20_balance(http, product["token_address"],
                               decimals=int(product["token_decimals"]))
        if before is None:
            return ex.Outcome(ex.REFUSED, "token_balance_unreadable")
        state["tokens_before"] = before
        quote = quote_buy(http, product, ctx["notional"])
        if not quote.get("ok"):
            return ex.Outcome(ex.REFUSED, quote["reason"])
        state["quote"] = quote
        # Profit is the only judge (operator 2026-10-08): the guard is the net at
        # the price we would really pay, not the distance from the snapshot the
        # decision was made on. ``max_slippage_bps`` is the *cost buffer* in this
        # policy (10 bps), so using it as a tolerance refused a trade that was
        # still worth +0.33 USDT (measured 17:04: the aggregator quoted 37.8 bps
        # worse than the 3-minute-old scan row and the net still cleared).
        net, edge = rung_net(quote, rung=ctx["notional"], sell_price=ctx["price"],
                             policy=policy, gas_usdt=ctx["gas_usdt"])
        snap_bps = (quote["price_usd"] / ctx["ref_buy"] - 1) * Decimal(10000)
        state["exec_edge_bps"] = str(edge.quantize(Decimal("0.01")))
        state["exec_net_usdt"] = None if net is None else str(net.quantize(Decimal("0.0001")))
        state["exec_vs_row_bps"] = str(snap_bps.quantize(Decimal("0.01")))
        required = _d(ctx.get("required_edge_bps"))
        if ctx["probe_only"]:
            # Unconditional sample: the price is recorded, never consulted.
            state["probe_unconditional"] = True
        else:
            if net is None or net < policy.min_net_profit_usdt:
                return ex.Outcome(ex.REFUSED,
                                  f"below_net_at_execution:{state['exec_net_usdt']}"
                                  f"@{state['exec_edge_bps']}bps"
                                  f"(row {ctx['ref_buy']})")
            if required is not None and edge < required:
                return ex.Outcome(
                    ex.REFUSED,
                    f"below_threshold_at_execution:{state['exec_edge_bps']}bps")
        tx = build_tx(http, product, ctx["notional"], policy.max_slippage_bps)
        try:
            allowance = ensure_allowance(http, product, tx["amount_raw"])
        except Exception as exc:  # noqa: BLE001
            return ex.Outcome(ex.REFUSED, f"approve:{str(exc)[:160]}")
        state["allowance"] = allowance
        if allowance.get("state") == "unknown":
            return ex.Outcome(ex.UNKNOWN, "approve_unconfirmed",
                              held_at="wallet")
        ok, why = simulate(http, tx["to"], tx["data"])
        if not ok:
            return ex.Outcome(ex.REFUSED, f"swap_simulation:{why}")
        # A94: the wallet reserves ``gas limit x its own fee cap`` before it
        # signs, and refuses with 10004 when the balance is under it. Ask the
        # same question *here*, so a thin wallet is a ``gas_short`` refusal we
        # can name instead of a wallet error we have to classify.
        short = native_reserve.shortfall(balance=native_balance(http),
                                         gas_limit=tx.get("gas"),
                                         gas_price_wei=ctx.get("gas_wei"))
        if short:
            return ex.Outcome(ex.REFUSED, short)
        nonce_before = pending_nonce(http)
        # A51/A95 and the 2026-10-10 14:17 stop: the buy leg is a broadcast,
        # so the incident file has to name it. The deposit leg did this and the
        # buy leg did not, which is why that HALT carried ``sends: []`` -- the
        # same evidence gap the finish row had (see ``executor._observed_extra``).
        incident.sent("eth_swap",
                      f"{ctx['symbol']} buy {tx['amount_raw']} usdc_raw -> {tx['to']}")
        try:
            tx_hash = broadcast_with_retry(tx["to"], tx["data"], gas=tx.get("gas"))
        except Exception as exc:  # noqa: BLE001
            return ex.broadcast_failure(exc, nonce_before=nonce_before,
                                        nonce_after=pending_nonce(http),
                                        label="swap_broadcast")
        state["buy_tx"] = tx_hash
        state["buy_nonce"] = nonce_before
        return ex.Outcome(ex.OK, "", held_at="wallet",
                          observed={"tx": tx_hash, "nonce_before": nonce_before})

    return run


def buy_verifier(ctx, state):
    def check(outcome) -> ex.Outcome:
        if outcome.status != ex.OK:
            return outcome
        http, product = ctx["http"], ctx["product"]
        want = ctx["notional"] / ctx["ref_buy"]
        for _ in range(12):
            held = erc20_balance(http, product["token_address"],
                                 decimals=int(product["token_decimals"]))
            if held is not None and held > state["tokens_before"]:
                got = held - state["tokens_before"]
                state["tokens_bought"] = got
                if got < want * Decimal("0.9"):
                    return ex.Outcome(ex.UNKNOWN,
                                      f"buy_short:{got}<{want}", held_at="wallet")
                return ex.Outcome(ex.OK, "", observed={"tokens": str(got)})
            time.sleep(5)
        status = wallet_status(state.get("buy_tx"))
        if status in ("ERROR", "FAILED"):
            return ex.Outcome(ex.RETRY, f"buy never landed ({status})")
        return ex.Outcome(ex.UNKNOWN, f"buy status={status}, balance unchanged",
                          held_at="wallet")

    return check


def deposit_runner(ctx, state):
    def run() -> ex.Outcome:
        http, product, rail = ctx["http"], ctx["product"], ctx["rail"]
        held = erc20_balance(http, product["token_address"],
                             decimals=int(product["token_decimals"]))
        if held is None:
            return ex.Outcome(ex.REFUSED, "token_balance_unreadable")
        if held <= 0:
            return ex.Outcome(ex.STOPPED, "nothing to deposit", held_at="wallet")
        minimum = rail.get("min_units")
        if minimum is not None and held < minimum:
            # Below Kraken's credit minimum the coins are simply kept, and a
            # later top-up does not recover them (lessons 3.4): stay in our
            # wallet on purpose rather than send into a black hole.
            return ex.Outcome(ex.STOPPED,
                              f"deposit_gate:below_minimum({held}<{minimum})",
                              held_at="wallet")
        state["deposit_tokens"] = held
        try:
            before = Decimal(str(ctx["kraken"].balance().get(f"{ctx['symbol']}.T") or 0))
        except KrakenError as exc:
            return ex.Outcome(ex.REFUSED, f"credit_unreadable:{str(exc)[:120]}")
        state["credit_before"] = before
        raw = int(held * Decimal(10 ** int(product["token_decimals"])))
        data = legs_eth.transfer_calldata(rail["address"], raw)
        ok, why = simulate(http, product["token_address"], data)
        if not ok:
            return ex.Outcome(ex.REFUSED, f"deposit_simulation:{why}")
        # A94, same rule as the buy leg: the deposit's own limit is 90 000 gas,
        # and the balance has already lost whatever the buy paid.
        short = native_reserve.shortfall(balance=native_balance(http),
                                         gas_limit=90000,
                                         gas_price_wei=ctx.get("gas_wei"))
        if short:
            return ex.Outcome(ex.REFUSED, short, held_at="wallet")
        incident.sent("eth_transfer",
                      f"{ctx['symbol']} {held} -> kraken {rail['address']}")
        nonce_before = pending_nonce(http)
        try:
            tx_hash = broadcast_with_retry(product["token_address"], data,
                                           gas="90000")
        except Exception as exc:  # noqa: BLE001
            return ex.broadcast_failure(exc, nonce_before=nonce_before,
                                        nonce_after=pending_nonce(http),
                                        label="deposit_broadcast")
        state["deposit_tx"] = tx_hash
        return ex.Outcome(ex.OK, "", held_at="kraken",
                          observed={"tx": tx_hash, "tokens": str(held)})

    return run


def deposit_verifier(ctx, state):
    def check(outcome) -> ex.Outcome:
        if outcome.status != ex.OK:
            return outcome
        http, product = ctx["http"], ctx["product"]
        tx_hash = state.get("deposit_tx")
        confirmed = None
        for _ in range(6):
            confirmed = receipt(http, tx_hash, tries=2, pause=5.0)
            if confirmed:
                break
        state["deposit_receipt"] = (None if confirmed is None
                                    else {"status": confirmed.get("status"),
                                          "gas_used": int(str(confirmed.get("gasUsed")), 16)})
        if confirmed is None or str(confirmed.get("status")) != "0x1":
            # The transfer did not land: the tokens are still ours.
            return ex.Outcome(ex.UNKNOWN, "deposit_receipt_missing",
                              held_at="wallet")
        expected = state["deposit_tokens"]
        credit = LIVE._wait_credit(ctx["kraken"], ctx["symbol"], expected,
                                   state["credit_before"], tries=40, pause=30.0)
        if credit is None:
            # Kraken has the transfer but has not credited it yet. The money is
            # at a known address, so this is a hold for a human, not a retry.
            return ex.Outcome(ex.UNKNOWN, "credit_unconfirmed", held_at="kraken")
        state["credit"] = credit
        record = {
            "symbol": ctx["symbol"], "network": "Ethereum",
            "method": ctx["rail"]["method"],
            "sent_tokens": format(expected, "f"),
            "credited_units": format(credit, "f"),
            "credit_per_token": format(credit / expected, "f") if expected else None,
            "tx": state.get("deposit_tx"),
            "at_ms": int(time.time() * 1000),
        }
        store.append_jsonl(DEPOSIT_SAMPLES, record)
        return ex.Outcome(ex.OK, "", observed={"credited_units": str(credit)})

    return check


def sell_runner(ctx, state):
    def run() -> ex.Outcome:
        credit = state.get("credit")
        if credit is None or credit <= 0:
            return ex.Outcome(ex.STOPPED, "no credited units to sell",
                              held_at="kraken")
        # Sell the **whole** Kraken balance for this symbol, not only this
        # cycle's slice: an earlier probe deposit sits there by design and the
        # trade is supposed to carry it out (same rule as the Solana family).
        tokens = credit
        try:
            held = Decimal(str(ctx["kraken"].balance().get(f"{ctx['symbol']}.T") or 0))
            if held > 0:
                tokens = max(held, credit)
                state["sell_tokens"] = str(tokens)
        except KrakenError as exc:
            state["sell_balance_note"] = f"unreadable:{str(exc)[:80]}"
        sold = LIVE._sell(ctx["kraken"], ctx["kraken"], ctx["policy"],
                          symbol=ctx["symbol"], tokens=tokens,
                          ref=Decimal(str(ctx.get("price") or 0)))
        if not sold.get("ok"):
            return ex.Outcome(ex.STOPPED, f"sell:{sold.get('reason')}",
                              held_at="kraken")
        state["sold"] = sold
        return ex.Outcome(ex.OK, "", observed={
            "tx": sold.get("txid"), "price": str(sold.get("avg") or sold.get("price")),
            "cost": str(sold.get("cost")), "fee": str(sold.get("fee"))})

    return run


# ---------------------------------------------------------------------------
# Gate and cycle
# ---------------------------------------------------------------------------
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


def gas_guard(ctx) -> tuple[bool, str]:
    """Can this wallet pay for the cycle at today's gas price?

    Unknown gas price or unknown balance is a refusal, not a free action. The
    buffer is there so a cycle that ends early still leaves enough to get flat.
    """
    balance, gas_wei = ctx.get("eth_balance"), ctx.get("gas_wei")
    if balance is None:
        return False, "eth_balance_unreadable"
    if gas_wei is None:
        return False, "gas_price_unreadable"
    needed = (GAS_UNITS_FOR_ONE_CYCLE * Decimal(gas_wei) / Decimal(10 ** 18)
              * GAS_LIMIT_BUFFER)
    if balance < needed:
        return False, f"gas_low:{balance}<{needed}"
    return True, ""


def run_steps(ctx) -> dict:
    """Run the cycle and return the executor's own result dict.

    A dict, not an exit code: the resident runner's ``guard_family`` reads
    ``state``/``reason``/``held_at`` straight out of it. Returning ``0`` here
    cost the chain a round of ``AttributeError: 'int' object has no attribute
    'get'`` on 2026-10-09 -- the probe had already run, and the runner could not
    read what happened.
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
                broadcasts=True, inventory=True,
                verify=buy_verifier(ctx, state)),
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

    lock = cycle.CycleLock(LOCK, HALT, label=f"{ctx['symbol']} {FAMILY} eth",
                           symbol=ctx["symbol"], family=FAMILY)
    machine = ex.Executor(
        symbol=ctx["symbol"], chain=CHAIN, family=FAMILY, mode="live",
        size_usdt=ctx["notional"], tokens=ctx["tokens"], steps=steps,
        gate=lambda: gate(ctx), ledger_path=LEDGER, halt_path=HALT,
        notify=alert, progress=lock.update,
        # ``probed`` -- not ``stopped``. The route was walked on purpose and the
        # coins are ours on Kraken, waiting for the next trade to carry them:
        # ``stopped``/``unknown`` are HOLD states, so ending a deliberate probe
        # there writes a HALT and stops the whole chain for a human who has
        # nothing to reconcile (measured 2026-10-09, first eth probe).
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


def cycle_summary(*, symbol: str = "SPCXx", notional: str | None = None,
                  price: str = "0", tokens: str | None = None,
                  probe_only: bool = False, confirm_live: bool = True) -> dict:
    """The resident runner's entry point: plan, then run, and hand back the dict."""
    ctx = plan(argparse.Namespace(symbol=symbol, notional=notional, price=price,
                                  tokens=tokens, probe_only=probe_only))
    log(f"eth family 1: {ctx['symbol']} {ctx['notional']} USDC -> {ctx['tokens']} "
        f"(quote {ctx['quote']['price_usd']}, row {ctx['ref_buy']}, "
        f"kraken {ctx['price']}, gas {ctx['gas_usdt']}, "
        f"rail min {ctx['rail']['min_units']})")
    if not confirm_live:
        return {"symbol": symbol, "family": FAMILY, "state": "planned",
                "reason": "", "held_at": "", "keep_lock": False}
    return run_steps(ctx)


def cycle_once(*, symbol: str = "SPCXx", notional: str | None = None,
               price: str = "0", tokens: str | None = None,
               probe_only: bool = False, confirm_live: bool = False) -> int:
    """Entry point for the resident live runner: the same path as the CLI."""
    ctx = plan(argparse.Namespace(symbol=symbol, notional=notional, price=price,
                                  tokens=tokens, probe_only=probe_only))
    log(f"eth family 1: {ctx['symbol']} {ctx['notional']} USDC -> "
        f"{ctx['tokens']} (quote {ctx['quote']['price_usd']}, "
        f"row {ctx['ref_buy']}, kraken {ctx['price']}, "
        f"gas {ctx['gas_usdt']}, rail min {ctx['rail']['min_units']}, "
        f"ETH {ctx['eth_balance']})")
    if not confirm_live:
        return 0
    result = run_steps(ctx)
    return 0 if result["state"] in (ex.SOLD, ex.STOPPED, ledger.PROBED) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "once"))
    parser.add_argument("--symbol", default="SPCXx")
    # No default: an absent ``--notional`` means "pick the rung that pays".
    parser.add_argument("--notional", default=None)
    parser.add_argument("--price", default="0")
    parser.add_argument("--tokens", default=None)
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args()
    ctx = plan(args)
    rung_note = ("" if not ctx.get("rung") else
                 f" (ladder picked {ctx['rung']['rung']} of cap "
                 f"{ctx['rung']['cap']}, tried {ctx['rung']['tried']})")
    log(f"eth family 1: buy {ctx['notional']} USDC -> {ctx['tokens']} "
        f"{ctx['symbol']} at {ctx['quote']['price_usd']} "
        f"(row {ctx['ref_buy']}, route {ctx['quote']['route']}, "
        f"impact {ctx['quote']['impact_pct']}%), sell into Kraken {ctx['price']}, "
        f"rail {ctx['rail']['method']} min {ctx['rail']['min_units']} "
        f"fee {ctx['rail']['fee_units']}, eth gas {ctx['gas_usdt']}, "
        f"wallet {ctx['eth_balance']} ETH / {ctx['usdc_balance']} USDC"
        f"{rung_note}")
    if args.mode == "plan":
        return 0
    if not args.confirm_live:
        raise SystemExit("once needs --confirm-live")
    result = run_steps(ctx)
    return 0 if result["state"] in (ex.SOLD, ex.STOPPED, ledger.PROBED) else 1


if __name__ == "__main__":
    raise SystemExit(main())
