#!/usr/bin/env python3
"""Resident Ethereum xStocks auto chain.

    scripts/xstocks-eth-auto-ctl.sh start
    scripts/xstocks-eth-auto-ctl.sh start-live
    scripts/xstocks-eth-auto-ctl.sh once

The decision core is ``arb_monitor.execution.xstocks_eth_auto``.  The
execution primitives are in ``scripts/xstocks_eth_live.py``.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import xstocks_eth_live as eth  # noqa: E402
from arb_monitor.execution import xstocks_eth_auto as auto  # noqa: E402
from arb_monitor.execution.xstocks_cycle import CycleBusy, run_cycle  # noqa: E402
from arb_monitor.ops.notify import FeishuNotifier  # noqa: E402
from arb_monitor.xstocks_v2.units import units_config_path  # noqa: E402

PROXY = "http://127.0.0.1:4780"
KRAKEN_UNITS = units_config_path(ROOT)


def load_env_file(path: Path) -> None:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _d(value, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value is not None else default))
    except Exception:  # noqa: BLE001
        return Decimal(default)


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def load_state() -> auto.EthState:
    try:
        raw = json.loads(auto.STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        raw = {}
    allowed = set(auto.EthState().__dict__)
    return auto.EthState(**{key: value for key, value in raw.items() if key in allowed})


def save_state(state: auto.EthState) -> None:
    state.updated_ms = int(time.time() * 1000)
    tmp = auto.STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state.to_json(), ensure_ascii=False, indent=2))
    tmp.replace(auto.STATE_PATH)


def notify(audience: str, text: str) -> None:
    auto.ALERT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (auto.ALERT_DIR / f"{stamp}-{audience}.json").write_text(
        json.dumps({"audience": audience, "text": text, "at": stamp}, ensure_ascii=False))
    settings = {
        "channel": "webhook",
        "webhook_url": os.environ.get("ARB_FEISHU_WEBHOOK", ""),
        "alert_channel": "webhook",
        "alert_webhook_url": os.environ.get("ARB_FEISHU_ALERT_WEBHOOK", ""),
    }
    if not settings["webhook_url"] and not settings["alert_webhook_url"]:
        return
    try:
        FeishuNotifier(settings, audience=audience).send(text)
    except Exception as exc:  # noqa: BLE001
        log(f"notify failed: {type(exc).__name__}: {exc}")


def load_kraken_units() -> dict:
    try:
        import tomllib

        return tomllib.loads(KRAKEN_UNITS.read_text()).get("kraken_units") or {}
    except Exception:  # noqa: BLE001
        return {}


def eth_usd_price() -> Decimal:
    url = "https://api.kraken.com/0/public/Ticker?pair=ETHUSD"
    request = urllib.request.Request(url, headers={"User-Agent": "xstocks-eth-auto/0.1"})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    payload = json.loads(opener.open(request, timeout=20).read())
    row = next(iter((payload.get("result") or {}).values()))
    return Decimal(str((row.get("c") or ["0"])[0]))


def eth_balances() -> tuple[Decimal, Decimal]:
    gas_raw = eth.rpc("eth_getBalance", [eth.WALLET, "latest"])
    usdc_raw = eth.call(eth.USDC, "0x70a08231" + "0" * 24 + eth.WALLET[2:].lower())
    gas = Decimal(int(gas_raw, 16)) / Decimal(10) ** 18
    usdc = Decimal(int(usdc_raw, 16)) / Decimal(10) ** 6
    return gas, usdc


def best_bid_vwap(symbol: str, kraken_units: Decimal) -> tuple[Decimal | None, Decimal | None, str]:
    client = eth.kraken_client()
    depth = client.depth(symbol, count=25)
    left = kraken_units
    gross = Decimal(0)
    for row in depth.get("bids") or []:
        price, size = Decimal(str(row[0])), Decimal(str(row[1]))
        take = min(left, size)
        gross += take * price
        left -= take
        if left <= 0:
            return gross, gross / kraken_units, "通过"
    return None, None, f"盘口不够：剩余 {left}"


def review_candidate(candidate: dict, policy: auto.EthPolicy) -> tuple[bool, str, dict]:
    symbol = str(candidate.get("symbol"))
    size_usd = Decimal(str(candidate.get("_size_usd")))
    quote = eth.swap_quote(symbol, size_usd)
    if quote.get("action") == "block":
        return False, f"quote blocked: {quote.get('reason')}", {}
    if quote.get("action") == "warn":
        return False, f"quote warn: {quote.get('reason')}", {}
    raw_out = Decimal(str(quote.get("toTokenAmount") or "0"))
    kraken_units = raw_out / Decimal(10) ** 18
    units_cfg = load_kraken_units().get(symbol) or {}
    order_min = Decimal(str(units_cfg.get("effective_min_units") or "0"))
    gross, vwap, book_reason = best_bid_vwap(symbol, kraken_units)
    if gross is None or vwap is None:
        return False, book_reason, {}
    gas_usd = Decimal(str(candidate.get("gas_usdt") or "0.9"))
    ok, why, stats = auto.review_ok(
        policy,
        tier=str(candidate.get("tier")),
        direct_deposit=bool(candidate.get("direct_deposit")),
        deposit_unit_verified=bool(candidate.get("deposit_credit_verified")),
        size_usd=size_usd,
        kraken_units=kraken_units,
        sell_price=vwap,
        order_min=order_min,
        book_ok=True,
        book_reason=book_reason,
        gas_usd=gas_usd,
    )
    if not ok:
        return False, why, {"quote": quote, "stats": stats, "vwap": vwap}
    return True, "复核通过", {
        "quote": quote,
        "stats": stats,
        "vwap": vwap,
        "kraken_units": kraken_units,
        "size_usd": size_usd,
    }


def paper_cycle(candidate: dict, policy: auto.EthPolicy) -> dict:
    ok, why, detail = review_candidate(candidate, policy)
    record = {
        "day": auto.day_key(),
        "source": "eth_auto_paper",
        "symbol": candidate.get("symbol"),
        "network": "Ethereum",
        "state": "paper" if ok else "refused",
        "size_usd": str(candidate.get("_size_usd")),
        "reason": why,
        "kraken_units": None if not detail.get("kraken_units") else str(detail["kraken_units"]),
        "sell_price": None if not detail.get("vwap") else str(detail["vwap"]),
        "net_usdt": None if not (detail.get("stats") or {}).get("net_usdt")
        else str(detail["stats"]["net_usdt"]),
        "notes": ["paper: quote + simulate only, no broadcast"],
    }
    eth.ledger.append(record)
    return record


def live_cycle(candidate: dict, policy: auto.EthPolicy) -> dict:
    symbol = str(candidate.get("symbol"))
    size_usd = Decimal(str(candidate.get("_size_usd")))

    def buy_fn() -> dict:
        token = eth.token_address(symbol)
        before = eth.erc20_balance(token)
        amount_raw = int(size_usd * Decimal(10) ** 6)
        events = eth.ensure_allowance(eth.USDC, amount_raw)
        plan = eth.swap_tx(symbol, size_usd, "0.5")
        sim = eth.simulate(plan["tx"]["to"], plan["tx"]["data"])
        if sim.get("failReason"):
            return {"ok": False, "reason": f"simulation: {sim.get('failReason')}"}
        tx_hash = eth.broadcast(plan["tx"]["to"], plan["tx"]["data"],
                                gas=str(plan["tx"].get("gas") or ""))
        receipt = eth.wait_receipt(tx_hash)
        if not receipt or receipt.get("status") != "0x1":
            return {"ok": False, "broadcast": True, "confirmed": False,
                    "reason": f"receipt unknown {tx_hash}"}
        after = eth.erc20_balance(token)
        tokens = after - before
        return {"ok": tokens > 0, "tokens": str(tokens), "price": str(size_usd / tokens),
                "broadcast": True, "confirmed": True, "tx": tx_hash,
                "approve_events": events,
                "gas_used": int(receipt.get("gasUsed"), 16)}

    def deposit_fn(tokens: Decimal) -> dict:
        client = eth.kraken_client()
        methods = client.deposit_methods(symbol)
        method = next((m for m in methods if "ethereum" in str(m.get("method")).lower()), None)
        if not method:
            return {"ok": False, "reason": "no Ethereum deposit method"}
        addresses = client.deposit_addresses(symbol, method["method"])
        address = (addresses[0] or {}).get("address") if addresses else None
        if not address:
            return {"ok": False, "reason": "deposit address missing"}
        raw = int(tokens * Decimal(10) ** 18)
        token = eth.token_address(symbol)
        data = eth.transfer_calldata(address, raw)
        sim = eth.simulate(token, data)
        if sim.get("failReason"):
            return {"ok": False, "reason": f"simulation: {sim.get('failReason')}"}
        tx_hash = eth.broadcast(token, data, gas="80000")
        receipt = eth.wait_receipt(tx_hash)
        if not receipt or receipt.get("status") != "0x1":
            return {"ok": True, "sent": True, "credited": False, "tx": tx_hash,
                    "reason": "receipt unknown"}
        deadline = time.time() + 900
        while time.time() < deadline:
            balance = client.balance()
            if (balance.get(symbol) or balance.get(f"{symbol}.T") or Decimal(0)) > 0:
                return {"ok": True, "sent": True, "credited": True, "tx": tx_hash}
            time.sleep(20)
        return {"ok": True, "sent": True, "credited": False, "tx": tx_hash,
                "reason": "Kraken credit timeout"}

    def sell_fn(tokens: Decimal) -> dict:
        client = eth.kraken_client()
        balance = client.balance().get(symbol) or client.balance().get(f"{symbol}.T")
        if not balance:
            return {"ok": False, "reason": "no Kraken balance"}
        units_cfg = load_kraken_units().get(symbol) or {}
        minimum = Decimal(str(units_cfg.get("effective_min_units") or "0"))
        if balance < minimum:
            return {"ok": False, "reason": f"balance {balance} below min {minimum}"}
        depth = client.depth(symbol, count=5)
        bids = depth.get("bids") or []
        if not bids:
            return {"ok": False, "reason": "no Kraken bid"}
        price = str(bids[0][0])
        client.validate_order(symbol, balance, side="sell", ordertype="limit", price=price)
        order = client.order(symbol, balance, side="sell", ordertype="limit",
                             price=price, confirm="live")
        order_id = (order.get("txid") or [None])[0]
        deadline = time.time() + 900
        while time.time() < deadline and order_id:
            rows = client._post("/0/private/QueryOrders", {"txid": order_id}) or {}
            row = rows.get(order_id) or {}
            if row.get("status") == "closed":
                cost = Decimal(str(row.get("cost") or "0"))
                fee = Decimal(str(row.get("fee") or "0"))
                return {"ok": True, "price": str(row.get("price") or price),
                        "net": str(cost - fee - size_usd), "order": order_id}
            if row.get("status") in {"canceled", "expired"}:
                return {"ok": False, "reason": f"order {row.get('status')}"}
            time.sleep(10)
        return {"ok": False, "reason": "sell timeout"}

    result = run_cycle(symbol=symbol, size_usd=size_usd,
                       buy_fn=buy_fn, deposit_fn=deposit_fn, sell_fn=sell_fn,
                       ledger=auto.LEDGER_PATH, lock_path=auto.LOCK_PATH)
    return result.to_json()


def run_once(policy: auto.EthPolicy, state: auto.EthState, confirm_live: bool) -> dict:
    if state.day != auto.day_key():
        state.day = auto.day_key()
        state.trades_today = 0
        state.opened_quote_usdt = "0"
    watch = auto.load_watch()
    gas, usdc = eth_balances()
    state.balances = {"eth": str(gas), "usdc": str(usdc)}
    age = auto.snapshot_age_s(watch)
    kill = auto.KILL_SWITCH.exists()
    halt = auto.AUTO_HALT.exists()
    gate_ok, gate_reason = auto.open_gate(
        policy, state, snapshot_age=age, kill_switch=kill, auto_halt=halt,
        eth_usdc=usdc, eth_gas=gas)
    candidates = auto.order_candidates(watch, policy)
    state.last_candidates = [{
        "symbol": row.get("symbol"),
        "net_usdt": row.get("best_net_usdt"),
        "size_usd": str(row.get("_size_usd")),
    } for row in candidates]
    if not candidates:
        state.phase = "idle"
        state.note = "没有合格 ETH 候选"
        save_state(state)
        return {"phase": state.phase, "reason": state.note}
    candidate = candidates[0]
    if not gate_ok:
        state.phase = "blocked"
        state.note = gate_reason
        save_state(state)
        return {"phase": state.phase, "reason": gate_reason, "candidate": candidate["symbol"]}
    live_ok, live_reason = policy.live_ok()
    if not (live_ok and confirm_live):
        if policy.auto_orders == auto.MODE_PAPER:
            record = paper_cycle(candidate, policy)
            state.phase = "paper"
            state.last_cycle = record
        else:
            state.phase = "off"
            record = None
        state.note = live_reason if not live_ok else "missing --confirm-live"
        save_state(state)
        return {"phase": state.phase, "reason": state.note,
                "record": record}
    state.phase = "buying"
    save_state(state)
    try:
        result = live_cycle(candidate, policy)
    except CycleBusy as exc:
        state.phase = "blocked"
        state.note = str(exc)
        save_state(state)
        return {"phase": state.phase, "reason": str(exc)}
    state.phase = str(result.get("state") or "unknown")
    state.last_cycle = result
    state.trades_today += 1
    state.opened_quote_usdt = str(_d(state.opened_quote_usdt) + _d(candidate.get("_size_usd")))
    net = result.get("net_usdt")
    if net is not None and _d(net) < 0:
        state.loss_streak += 1
        notify("alert", f"ETH auto cycle loss: {json.dumps(result, ensure_ascii=False)}")
        if state.loss_streak >= policy.loss_pause_trades:
            state.pause_reason = f"连续 {state.loss_streak} 笔净亏"
    else:
        state.loss_streak = 0
    state.note = str(result.get("reason") or state.phase)
    save_state(state)
    return {"phase": state.phase, "result": result}


def main(argv: list[str] | None = None) -> int:
    load_env_file(ROOT / "config" / "notify.env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--confirm-live", action="store_true",
                        help="allow the policy live gates to broadcast real transactions")
    args = parser.parse_args(argv)
    policy = auto.EthPolicy.load()
    state = load_state()
    state.running = True
    save_state(state)
    while True:
        try:
            outcome = run_once(policy, state, args.confirm_live)
            log(json.dumps(outcome, ensure_ascii=False)[:1000])
        except Exception as exc:  # noqa: BLE001
            state.phase = "unknown"
            state.note = f"{type(exc).__name__}: {exc}"
            save_state(state)
            notify("alert", f"ETH auto unknown: {state.note}")
            log(state.note)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(max(15, policy.poll_interval_s))


if __name__ == "__main__":
    raise SystemExit(main())
