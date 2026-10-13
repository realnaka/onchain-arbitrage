"""Read-only account board: what is held, what is free, what the rules are.

This page answers three questions and nothing else:

1. what is the position on every account (X Layer tokens, OKX perpetuals);
2. how much capital is left and how much of it is already committed;
3. which risk parameters are currently in force.

It is a viewer. It places no order, signs nothing and cannot bypass the risk
gate. A source that fails is rendered ``UNKNOWN``/``STALE`` with the error text
instead of a remembered number, because a stale balance is worse than a blank.
"""
from __future__ import annotations

import html
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from arb_monitor.core.hk import dumps
from arb_monitor.execution.ledger import ExecutionLedger
from arb_monitor.execution.pending_scan import TERMINAL_GROUP_STATES
from arb_monitor.execution.network_policy import DEFAULT_ROUTES
from arb_monitor.execution.okx_account import (OKXReadError, OKXReadOnlyClient,
                                               fee_bps, taker_fee_bps)
from arb_monitor.execution.wallet_cli import (OnchainOSRunner, WalletCliError,
                                              wallet_addresses, wallet_balances)

try:  # Python 3.11+ has tomllib in the standard library.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - only on very old interpreters
    tomllib = None

AUTOMATION_POLICY = "config/automation_policy.toml"
MONITOR_CONFIG = "config/hk_monitor.toml"
EXECUTION_LEDGER = "data/execution.db"
MONITOR_DB = "data/hk_monitor.db"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def okx_read_env(okx_env: str | os.PathLike | None = "config/okx-demo.env") -> dict:
    """Credentials from the env file, proxy resolved by the reviewed policy.

    The proxy is never guessed per request: the project's fixed route list is
    the default, and an explicit ``ARB_HTTP_PROXY`` overrides it.
    """
    from arb_monitor.execution.config import load_env_file

    env: dict[str, str] = {}
    if okx_env and Path(okx_env).exists():
        env.update(load_env_file(okx_env))
    proxy = os.environ.get("ARB_HTTP_PROXY") or DEFAULT_ROUTES[0].proxy
    if proxy:
        env.setdefault("ARB_HTTP_PROXY", proxy)
    return env


def _decimal(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _source(status: str, *, fetched_at_ms: int | None = None,
            error: str | None = None, detail: str | None = None) -> dict:
    return {"status": status, "fetched_at_ms": fetched_at_ms,
            "error": error, "detail": detail}


def _read_with_retry(call, attempts: int = 3, backoff_seconds: float = 0.6):
    """A signed GET, retried a bounded number of times.

    Measured 2026-09-16: three consecutive account reads hit one
    ``okx_transport:URLError`` each, so a single-shot read turns a transient
    network hiccup into ``verified=false`` — and, since the unattended executor
    refuses to act on an unverified account, into a skipped cycle. Reads are
    retryable under this project's own network policy (``[network]
    read_retries``); writes are not, and nothing here writes.
    """
    last: Exception | None = None
    for attempt in range(max(1, int(attempts))):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - the last failure is re-raised
            last = exc
            if attempt + 1 < attempts:
                time.sleep(backoff_seconds * (attempt + 1))
    raise last if last is not None else RuntimeError("read_failed")


# States that a ledger group can end in without holding anything. Shared with
# the orphan scan so the same state cannot be "finished" in one place and
# "unresolved" in another.
_PRE_WRITE_STATES = frozenset({"precheck", "allowance_ready"})


def classify_group(state: str | None) -> str:
    """``historical`` when the group ended in a state that holds nothing."""
    return ("historical" if state in TERMINAL_GROUP_STATES else "unresolved")


def requires_position_check(state: str | None) -> bool:
    """A preparation state: no position is implied, but only the venue knows."""
    return state in _PRE_WRITE_STATES


def load_toml(path: str | Path) -> dict:
    if tomllib is None or not Path(path).exists():
        return {}
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def risk_parameters(automation_path: str = AUTOMATION_POLICY,
                    monitor_path: str = MONITOR_CONFIG,
                    kill_switch_file: str | None = None) -> dict:
    """The limits that are actually in force, read from the config files."""
    automation = load_toml(automation_path)
    monitor = load_toml(monitor_path)
    execution = automation.get("execution") or {}
    allowlist = automation.get("allowlist") or {}
    limits = automation.get("limits") or {}
    orders = automation.get("orders") or {}
    approval = automation.get("approval") or {}
    network = automation.get("network") or {}
    monitor_section = monitor.get("monitor") or {}
    costs = monitor.get("costs") or {}
    notify = monitor.get("notify") or {}
    kill_switch = Path(kill_switch_file or execution.get("kill_switch_file", "data/KILL_SWITCH"))
    return {
        "automation": {
            "mode": execution.get("mode"),
            # What the unattended executor is allowed to do, and where it stops
            # itself. Both are visible here because a board that shows the
            # limits but not the switch invites the wrong assumption.
            "auto_orders": execution.get("auto_orders"),
            "auto_halt_file": str(Path(execution.get("auto_halt_file", "data/AUTO_HALT"))),
            "auto_halt_active": Path(execution.get("auto_halt_file", "data/AUTO_HALT")).exists(),
            "spot_must_confirm_before_hedge": execution.get("spot_must_confirm_before_hedge"),
            "unknown_state_action": execution.get("unknown_state_action"),
            "kill_switch_file": str(kill_switch),
            "kill_switch_tripped": kill_switch.exists(),
        },
        "allowlist": {
            "assets": allowlist.get("assets") or [],
            "chains": allowlist.get("chains") or [],
            "spot_tokens": allowlist.get("spot_tokens") or [],
            "quote_tokens": allowlist.get("quote_tokens") or [],
            "venues": allowlist.get("venues") or [],
            "instruments": allowlist.get("instruments") or [],
            "wallets": allowlist.get("wallets") or [],
        },
        "limits": {
            "per_trade_quote_usdt": limits.get("per_trade_quote_usdt"),
            "daily_quote_usdt": limits.get("daily_quote_usdt"),
            "max_slippage_bps": limits.get("max_slippage_bps"),
            "max_unhedged_tokens": limits.get("max_unhedged_tokens"),
            # The long-dust tolerance for one entry, in bps of the hedged token
            # count. Shown next to `max_unhedged_tokens` because they answer
            # different questions: the token limit is "how much may be left at
            # the end", this is "how much rounding may one exact-in buy leave
            # unmatched". The shortfall direction ignores both and halts.
            "max_unhedged_bps": limits.get("max_unhedged_bps"),
            "max_open_contracts": limits.get("max_open_contracts"),
            "quote_ttl_seconds": limits.get("quote_ttl_seconds"),
        },
        "orders": {
            "style": orders.get("style"),
            "perp_order_type": orders.get("perp_order_type"),
            "perp_price_source": orders.get("perp_price_source"),
            "perp_max_slippage_bps": orders.get("perp_max_slippage_bps"),
            "allow_maker_orders": orders.get("allow_maker_orders"),
            "allow_resting_orders": orders.get("allow_resting_orders"),
            "maker_timeout_seconds": orders.get("maker_timeout_seconds"),
            "maker_max_reprices": orders.get("maker_max_reprices"),
            "maker_max_naked_seconds": orders.get("maker_max_naked_seconds"),
            "maker_unwind_on_spot_failure": orders.get("maker_unwind_on_spot_failure"),
            # Closing has its own window and fallback: a close waits, an open
            # takes what it can get.
            "maker_close_timeout_seconds": orders.get("maker_close_timeout_seconds"),
            "maker_close_fallback": orders.get("maker_close_fallback"),
            "maker_close_max_reprices": orders.get("maker_close_max_reprices"),
        },
        "approval": {
            "mode": approval.get("mode"),
            "max_allowance_quote": approval.get("max_allowance_quote"),
            "require_allowance_readback": approval.get("require_allowance_readback"),
            "allowance_revoke_after_cycle": approval.get("allowance_revoke_after_cycle"),
        },
        "network": {
            "routes": network.get("routes") or [],
            "read_retries": network.get("read_retries"),
            "write_retries": network.get("write_retries"),
            "unknown_write_action": network.get("unknown_write_action"),
        },
        "monitor": {
            "min_net_edge_bps": monitor_section.get("min_net_edge_bps"),
            "min_net_profit_quote": monitor_section.get("min_net_profit_quote"),
            "max_age_ms": monitor_section.get("max_age_ms"),
            "max_pair_skew_ms": monitor_section.get("max_pair_skew_ms"),
            "signal_cooldown_ms": monitor_section.get("signal_cooldown_ms"),
            "interval_seconds": monitor_section.get("interval_seconds"),
            "total_capital_usdt": monitor_section.get("total_capital_usdt"),
            "spot_allocation_usdt": monitor_section.get("spot_allocation_usdt"),
            "margin_allocation_usdt": monitor_section.get("margin_allocation_usdt"),
        },
        "costs": {
            "spot_fee_bps": costs.get("spot_fee_bps"),
            "perp_fee_bps": costs.get("perp_fee_bps"),
            "slippage_bps": costs.get("slippage_bps"),
            "failure_bps": costs.get("failure_bps"),
            "verified": costs.get("verified"),
            "evidence": costs.get("evidence"),
        },
        "notify": {"channel": notify.get("channel"),
                   "alert_channel": notify.get("alert_channel"),
                   "cooldown_seconds": notify.get("cooldown_seconds")},
    }


def token_universe(monitor_path: str = MONITOR_CONFIG) -> dict[str, dict]:
    """Asset → token metadata, so a raw balance can be named without guessing."""
    config = load_toml(monitor_path)
    signed = Path(monitor_path).parent / "signed_depth_mappings.toml"
    if signed.exists():
        extra = load_toml(signed).get("tokens") or []
        have = {t.get("asset") for t in config.get("tokens") or []}
        config.setdefault("tokens", [])
        config["tokens"].extend(t for t in extra if t.get("asset") not in have)
    universe: dict[str, dict] = {}
    for token in config.get("tokens") or []:
        address = (token.get("address") or "").lower()
        if address:
            universe[address] = {
                "asset": token.get("asset"), "symbol": token.get("symbol"),
                "decimals": token.get("decimals"), "mapping_verified": token.get("mapping_verified"),
            }
    quote = config.get("quote_token") or {}
    if quote.get("address"):
        universe[(quote["address"]).lower()] = {
            "asset": quote.get("symbol"), "symbol": quote.get("symbol"),
            "decimals": quote.get("decimals"), "mapping_verified": True,
        }
    return universe


def okx_section(client: OKXReadOnlyClient, inst_ids: list[str]) -> dict:
    """Balances, live positions, pending orders and the real taker rate."""
    fetched = now_ms()
    result: dict = {"simulated": client.simulated, "base_url": client.base_url}
    try:
        result["balances"] = [
            {
                "ccy": row.get("ccy"),
                "avail": row.get("availBal"),
                "equity": row.get("eq"),
                "usd": row.get("eqUsd"),
                "frozen": row.get("frozenBal"),
                "liability": row.get("liab"),
            }
            for row in _read_with_retry(client.balances)
            if _decimal(row.get("eq")) not in (None, Decimal(0)) or _decimal(row.get("avail")) not in (None, Decimal(0))
        ]
        positions = []
        for row in _read_with_retry(client.positions):
            size = _decimal(row.get("pos"))
            positions.append({
                "instrument": row.get("instId"),
                "size": row.get("pos"),
                "direction": "short" if (size or Decimal(0)) < 0 else "long",
                "avg_price": row.get("avgPx"),
                "mark_price": row.get("markPx"),
                "notional_usd": row.get("notionalUsd"),
                "unrealized_pnl": row.get("upl"),
                "leverage": row.get("lever"),
                "margin_mode": row.get("mgnMode"),
                "liquidation_price": row.get("liqPx"),
                "raw": {k: row.get(k) for k in ("adl", "imr", "mmr", "mgnRatio", "cTime", "uTime")},
            })
        result["positions"] = positions
        result["pending_orders"] = [
            {
                "instrument": row.get("instId"), "order_id": row.get("ordId"),
                "side": row.get("side"), "size": row.get("sz"),
                "filled": row.get("accFillSz"), "price": row.get("px"),
                "state": row.get("state"), "created_ms": row.get("cTime"),
            }
            for row in _read_with_retry(client.pending_orders)
        ]
        fees = []
        for inst_id in inst_ids:
            # OKX refuses instId on this endpoint for SWAP; the per-family
            # query (uly) is the documented shape (XIAOMI-USDT-SWAP → XIAOMI-USDT).
            uly = inst_id.rsplit("-", 1)[0] if inst_id.count("-") >= 2 else inst_id
            row, error = {}, None
            for kwargs in ({"inst_type": "SWAP", "uly": uly}, {"inst_type": "SWAP"}):
                try:
                    row = client.trade_fee(**kwargs)
                    error = None
                    break
                except OKXReadError as exc:
                    error = str(exc)
            rate = taker_fee_bps(row) if row else None
            fees.append({
                "instrument": inst_id, "family": uly, "level": row.get("level"),
                "taker_bps": str(rate) if rate is not None else None,
                "maker_bps": row.get("makerU") or row.get("maker"),
                "error": None if rate is not None else error,
            })
        result["fees"] = fees
        recent = []
        for row in _read_with_retry(lambda: client.fills(limit=20)):
            instrument = row.get("instId")
            recent.append({
                "instrument": instrument, "side": row.get("side"),
                "size": row.get("fillSz"), "price": row.get("fillPx"),
                "fee": row.get("fee"), "fee_ccy": row.get("feeCcy"),
                "fee_bps": str(fee_bps(row.get("fee"), row.get("fillPx"), row.get("fillSz")) or "")
                if fee_bps(row.get("fee"), row.get("fillPx"), row.get("fillSz")) is not None else None,
                "exec_type": row.get("execType"), "ts_ms": _int(row.get("ts")),
                "order_id": row.get("ordId"), "client_order_id": row.get("clOrdId"),
                "pnl": row.get("fillPnl"),
            })
        result["recent_fills"] = recent
    except OKXReadError as exc:
        return {"simulated": client.simulated, **_source("error", fetched_at_ms=fetched,
                                                        error=str(exc))}
    return {**result, **_source("ok", fetched_at_ms=fetched)}


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def wallet_section(runner: OnchainOSRunner, chain: str = "xlayer") -> dict:
    fetched = now_ms()
    try:
        balances = wallet_balances(runner, chain)
    except WalletCliError as exc:
        return _source("error", fetched_at_ms=fetched, error=str(exc))
    try:
        address = wallet_addresses(runner, chain).get("address")
    except WalletCliError as exc:
        address, address_error = None, str(exc)
    else:
        address_error = None
    return {"address": address, "chain": chain, "balances": balances,
            "address_error": address_error, **_source("ok", fetched_at_ms=fetched)}


def _asset_for_instrument(tokens: dict, venue: str, instrument: str) -> str | None:
    """Which allowlisted token an instrument belongs to, or None.

    The monitor's allowlist names instruments for venues that have no mapping
    table at all (``gate:XIAOMI_USDT``), so the link is made from the instrument
    text and the issuer's own HK code rather than from a hand-kept list:
    ``XIAOMI_USDT`` carries the asset name, ``HK1810USDT`` carries the HK code
    of XIAOMI, and ``XIAOMIHKD_USDT`` carries both.
    """
    upper = instrument.upper()
    for asset, token in tokens.items():
        name = str(token.get("name") or "")
        if asset.upper() in upper or (name and name.upper() in upper):
            return asset
        hk_code = str(token.get("hk_code") or "").lstrip("0")
        if hk_code and (f"HK{hk_code}" in upper or f"HK{int(hk_code):04d}" in upper):
            return asset
        for key in (token.get("mappings") or {}):
            if str(key).partition(":")[2] == instrument:
                return asset
    return None


def trade_universe(*, automation_path: str = AUTOMATION_POLICY,
                   monitor_path: str = MONITOR_CONFIG) -> dict:
    """What the monitor watches, and what the machine is allowed to trade.

    Two different questions, and the difference is the whole safety argument:

    * **monitored** — every venue/instrument the sampler reads. Gate and Binance
      live here for comparison; reading a price is not permission to trade it.
    * **auto-executable** — the intersection of the execution allowlist, a
      reviewed venue adapter, and a *verified* share mapping. Anything else is
      listed with the reason it is blocked, so "why didn't it trade that?" has an
      answer on the board instead of in someone's memory.
    """
    automation = load_toml(automation_path)
    monitor = load_toml(monitor_path)
    allow = automation.get("allowlist") or {}
    assets = [str(x) for x in allow.get("assets") or []]
    venues = {str(x) for x in allow.get("venues") or []}
    instruments = {str(x) for x in allow.get("instruments") or []}
    tokens = {str(t.get("asset")): t for t in (monitor.get("tokens") or [])
              if t.get("asset")}
    executable: list[dict] = []
    for asset in assets:
        token = tokens.get(asset) or {}
        for key, mapping in (token.get("mappings") or {}).items():
            venue, _, instrument = str(key).partition(":")
            if not (venue in venues and instrument in instruments):
                continue
            if not (mapping or {}).get("verified"):
                continue
            executable.append({
                "asset": asset, "venue": venue, "instrument": instrument,
                "spot_token": token.get("symbol"),
                "spot_address": token.get("address"),
                "chain_id": str(token.get("chain_id") or ""),
                "mapping_verified": True,
                "evidence": (mapping or {}).get("evidence"),
            })
    # Everything the monitor reads, with the reason each is or is not tradable.
    monitoring = automation.get("monitoring_allowlist") or {}
    executable_keys = {(row["venue"], row["instrument"]) for row in executable}
    monitor_only: list[dict] = []
    for label, key in (("USD 计价", "usd_instruments"), ("HKD 计价", "hkd_instruments")):
        for raw in monitoring.get(key) or []:
            venue, _, instrument = str(raw).partition(":")
            asset = _asset_for_instrument(tokens, venue, instrument)
            if (venue, instrument) in executable_keys:
                monitor_only.append({"asset": asset, "venue": venue,
                                     "instrument": instrument, "quote": label,
                                     "executable": True, "blocked_by": []})
                continue
            reasons: list[str] = []
            if venue not in venues:
                reasons.append("该交易所没有已接入的执行适配器")
            if instrument not in instruments:
                reasons.append("合约不在执行白名单")
            if asset is None:
                reasons.append("未匹配到已核验标的")
            else:
                if asset not in assets:
                    reasons.append("标的不在执行白名单")
                mappings = (tokens.get(asset) or {}).get("mappings") or {}
                if not any(str(k).endswith(":" + instrument)
                           and (m or {}).get("verified") for k, m in mappings.items()):
                    reasons.append("份额映射未核验")
            monitor_only.append({"asset": asset, "venue": venue,
                                 "instrument": instrument, "quote": label,
                                 "executable": False,
                                 "blocked_by": reasons or ["仅用于对比，未申请执行"]})
    return {
        "auto_executable": executable,
        "monitor_only": monitor_only,
        "counts": {"auto_executable": len(executable),
                   # Every instrument on the comparison allowlist, executable
                   # ones included (they carry `executable=True`).
                   "monitored": len(monitor_only),
                   "monitor_assets": len(monitor.get("monitor", {}).get("assets") or assets)},
        "chain_ids": [str(x) for x in allow.get("chains") or []],
        "quote_tokens": [str(x) for x in allow.get("quote_tokens") or []],
        "wallets": [str(x) for x in allow.get("wallets") or []],
        "note": ("监控 ≠ 可执行：只有「执行白名单 ∩ 已验证映射 ∩ 已接入适配器」的合约"
                 "才可能被自动下单；其余只用于对比与告警。"),
    }


def grey_scale_section(*, automation_path: str = AUTOMATION_POLICY,
                       ledger_path: str = EXECUTION_LEDGER,
                       wallet_balances_rows: list[dict] | None = None) -> dict:
    """The gray-scale ceiling, and what is left of it today.

    Operator's instruction, 2026-09-16: run small at ~10 USD per trade for a
    while, and **show the number on the board** so the size in force is never a
    guess. Three numbers answer that:

    * the ceiling (``per_trade`` / ``daily``) — what the policy allows;
    * the affordable size — ``ceiling`` capped by the wallet's own quote balance,
      which is what a trade will actually be (the plan search sizes to whole
      shares under both);
    * today's used/remaining — real fills only; paper fills are listed apart, so
      a modelled trade never looks like spent cash.
    """
    automation = load_toml(automation_path)
    limits = automation.get("limits") or {}
    execution = automation.get("execution") or {}
    quote_tokens = [str(x).lower()
                    for x in (automation.get("allowlist") or {}).get("quote_tokens") or []]
    balance = None
    for row in wallet_balances_rows or []:
        if quote_tokens and str(row.get("token_address") or "").lower() not in quote_tokens:
            continue
        value = _decimal(row.get("balance"))
        if value is not None:
            balance = (balance or Decimal(0)) + value
    per_trade = _decimal(limits.get("per_trade_quote_usdt"))
    daily = _decimal(limits.get("daily_quote_usdt"))
    affordable = None
    if per_trade is not None and balance is not None:
        affordable = min(per_trade, balance * Decimal("0.999"))
    used = paper = None
    if Path(ledger_path).exists():
        ledger = ExecutionLedger(ledger_path)
        try:
            start, end = _today_bounds()
            used = ledger.executed_quote_between(start, end, include_paper=False)
            paper = ledger.paper_quote_between(start, end)
        finally:
            ledger.close()
    remaining = (daily - used) if (daily is not None and used is not None) else None
    over_quota = remaining is not None and remaining <= 0
    return {
        "per_trade_quote_usdt": None if per_trade is None else str(per_trade),
        "daily_quote_usdt": None if daily is None else str(daily),
        "wallet_quote_balance": None if balance is None else str(balance),
        "affordable_trade_quote": None if affordable is None else str(affordable),
        "today_real_used_quote": None if used is None else str(used),
        "today_paper_used_quote": None if paper is None else str(paper),
        "today_remaining_real_quote": None if remaining is None else str(remaining),
        "today_quota_exhausted": over_quota,
        "auto_orders": execution.get("auto_orders"),
        "live_ack_set": bool(str(execution.get("live_ack") or "").strip()),
        "note": ("灰度额度：单笔/日上限是硬闸门；实际每笔取 min(上限, 钱包余额×0.999)，"
                 "按整股向下取整。今日已用只统计真实买入成交，纸面成交单列。"),
    }


def _today_bounds() -> tuple[int, int]:
    """Asia/Shanghai day bounds in ms, the same clock the quota uses."""
    moment = datetime.now(timezone(timedelta(hours=8)))
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return (int(start.timestamp() * 1000),
            int((start + timedelta(days=1)).timestamp() * 1000))


def ledger_section(path: str = EXECUTION_LEDGER) -> dict:
    if not Path(path).exists():
        return _source("empty", detail="no_execution_ledger_yet")
    ledger = ExecutionLedger(path)
    # Three layers, because they answer three different questions and the
    # 2026-09-16 review found them mixed into one number (44 "open groups", no
    # real exposure):
    #   1. account_exposure   — what the venue and the wallet actually hold
    #                           (read by `exposure_section`, not from here);
    #   2. unresolved_groups  — ledger groups whose last state is neither a
    #                           success nor a known no-op. These block
    #                           automation and need a human, but they are not
    #                           proof of a position;
    #   3. historical_groups  — finished, kept for audit.
    # A group is classified by what its state *means*, not by a blacklist of
    # strings, so a state the ledger learns later cannot silently become "open".
    try:
        groups = ledger.recover()
        unresolved, historical = [], []
        for group_id, row in groups.items():
            entry = {
                "group_id": group_id, "state": row.get("state"),
                "signal_id": row.get("signal_id"), "event_type": row.get("event_type"),
                "updated_ms": row.get("created_ts_ms"),
                "requires_position_check": requires_position_check(row.get("state")),
            }
            (historical if classify_group(row.get("state")) == "historical"
             else unresolved).append(entry)
    finally:
        ledger.close()
    unresolved.sort(key=lambda row: row.get("updated_ms") or 0, reverse=True)
    return {"unresolved_groups": unresolved, "historical_groups": historical,
            "open_groups": unresolved,          # kept for older readers
            "unresolved_count": len(unresolved),
            "historical_count": len(historical),
            "group_count": len(groups),
            "terminal_states": sorted(TERMINAL_GROUP_STATES),
            **_source("ok", fetched_at_ms=now_ms())}


def gas_banner(monitor_db: str = MONITOR_DB) -> dict:
    """Latest gas-sponsorship observation, or unknown.

    The monitor re-reads our own receipts on a clock and stores the result. The
    board never infers sponsorship from configuration: ``costs`` says gas is 0,
    and this says whether that is still true.
    """
    if not Path(monitor_db).exists():
        return {"status": "unknown", "note": "监控数据库不存在，尚无 gas 观测"}
    try:
        from arb_monitor.ledger.store import EventStore
        store = EventStore(monitor_db)
        try:
            row = store.latest_event("gas_observation")
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001 - an unreadable check is unknown
        return {"status": "unknown", "note": f"读取 gas 观测失败: {type(exc).__name__}"}
    return row or {"status": "unknown", "note": "尚无 gas 观测记录"}


def spot_open_costs(ledger_path: str = EXECUTION_LEDGER) -> dict[str, Decimal]:
    """Latest on-chain spend per asset, from the execution ledger."""
    path = Path(ledger_path)
    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT payload FROM execution_events WHERE event_type='spot_filled' "
            "ORDER BY id DESC"
        ).fetchall()
        conn.close()
    except Exception:
        return {}
    costs: dict[str, Decimal] = {}
    for (payload,) in rows:
        try:
            item = json.loads(payload)
        except Exception:
            continue
        asset = str(item.get("asset") or "")
        amount = _decimal(item.get("quote_amount"))
        if asset and amount is not None and asset not in costs:
            costs[asset] = amount
    return costs


def exposure_section(wallet: dict, okx: dict, universe: dict[str, dict],
                     instrument_multipliers: dict[str, str] | None = None,
                     spot_costs: dict[str, Decimal] | None = None) -> dict:
    """Holdings named by asset, and what is genuinely unhedged."""
    instrument_multipliers = instrument_multipliers or {}
    spot = []
    by_asset: dict[str, Decimal] = {}
    for row in wallet.get("balances") or []:
        address = (row.get("token_address") or "").lower()
        meta = universe.get(address)
        if not meta or meta.get("asset") == meta.get("symbol"):
            # Stablecoins and unknown tokens still get shown, but they are not
            # a stock position and are not netted against a perp.
            spot.append({"asset": meta.get("asset") if meta else "unknown",
                         "symbol": row.get("symbol"), "token_address": address or None,
                         "quantity": row.get("balance"), "usd_value": row.get("usd_value"),
                         "usd_price": row.get("price_usd"), "kind": "cash_or_unknown"})
            continue
        quantity = _decimal(row.get("balance")) or Decimal(0)
        by_asset[meta["asset"]] = by_asset.get(meta["asset"], Decimal(0)) + quantity
        spot.append({"asset": meta["asset"], "symbol": row.get("symbol"),
                     "token_address": address or None, "quantity": row.get("balance"),
                     "usd_value": row.get("usd_value"), "usd_price": row.get("price_usd"),
                     "kind": "stock_token",
                     "mapping_verified": meta.get("mapping_verified")})
    spot_costs = spot_costs or {}
    perp = []
    perp_by_asset: dict[str, Decimal] = {}
    perp_mark: dict[str, tuple[Decimal | None, Decimal | None]] = {}
    perp_without_multiplier: set[str] = set()
    for row in okx.get("positions") or []:
        instrument = row.get("instrument") or ""
        asset = instrument.split("-")[0]
        size = _decimal(row.get("size")) or Decimal(0)
        multiplier = _decimal(instrument_multipliers.get(instrument)) or Decimal(0)
        contracts = abs(size)
        tokens = contracts * multiplier if multiplier > 0 else None
        if tokens is not None:
            perp_by_asset[asset] = perp_by_asset.get(asset, Decimal(0)) + (
                tokens if size < 0 else -tokens)
        else:
            # A contract size we cannot convert is still an open position. It is
            # reported as unknown exposure rather than netted at an assumed 1:1.
            perp_without_multiplier.add(asset)
        perp_mark[asset] = (_decimal(row.get("mark_price")),
                            _decimal(row.get("unrealized_pnl")))
        perp.append({**row, "asset": asset,
                     "contract_multiplier": str(multiplier) if multiplier > 0 else None,
                     "token_equivalent": str(tokens) if tokens is not None else None})
    unhedged = []
    for asset in sorted(set(by_asset) | set(perp_by_asset) | perp_without_multiplier):
        held = by_asset.get(asset, Decimal(0))
        if asset in perp_without_multiplier:
            unhedged.append({
                "asset": asset, "token_held": str(held), "token_shorted": None,
                "net_exposure_tokens": None, "net_exposure_usd": None,
                "multiplier_known": False, "flat": False,
                "hedge_ratio_pct": None,
            })
            continue
        short = perp_by_asset.get(asset, Decimal(0))
        net = held - short
        price = next((_decimal(row.get("usd_price")) for row in spot
                      if row.get("asset") == asset and row.get("usd_price")), None)
        mark, upl = perp_mark.get(asset, (None, None))
        basis = max(held, abs(short))
        # A 2 bps rounding remainder is the hedge, not a naked short.
        flat = net == 0 or (basis > 0 and abs(net) / basis * Decimal(10000) <= 5)
        cost = spot_costs.get(asset)
        value = next((_decimal(row.get("usd_value")) for row in spot
                      if row.get("asset") == asset and row.get("kind") == "stock_token"),
                     None)
        spot_pnl = (value - cost) if value is not None and cost is not None else None
        combined = None
        if spot_pnl is not None and upl is not None:
            combined = spot_pnl + upl
        elif spot_pnl is not None:
            combined = spot_pnl
        elif upl is not None:
            combined = upl
        avg = (cost / held) if cost is not None and held > 0 else None
        unhedged.append({
            "asset": asset, "token_held": str(held), "token_shorted": str(short),
            "net_exposure_tokens": str(net),
            "net_exposure_usd": str(net * price) if price is not None else None,
            "multiplier_known": True,
            "flat": flat,
            "hedge_ratio_pct": (str(short / held * 100) if held > 0 else None),
            "spot_open_cost_usd": None if cost is None else str(cost),
            "spot_avg_price": None if avg is None else str(avg),
            "spot_floating_pnl_usd": None if spot_pnl is None else str(spot_pnl),
            "perp_floating_pnl_usd": None if upl is None else str(upl),
            "combined_pnl_usd": None if combined is None else str(combined),
        })
    by_unhedged = {row["asset"]: row for row in unhedged}
    for row in spot:
        extra = by_unhedged.get(row.get("asset")) or {}
        row["open_avg_price"] = extra.get("spot_avg_price")
        row["floating_pnl_usd"] = extra.get("spot_floating_pnl_usd")
    return {"spot": spot, "perp": perp, "unhedged": unhedged}


def build_snapshot(*, client: OKXReadOnlyClient | None, runner: OnchainOSRunner | None,
                   monitor_path: str = MONITOR_CONFIG,
                   automation_path: str = AUTOMATION_POLICY,
                   ledger_path: str = EXECUTION_LEDGER,
                   monitor_db: str = MONITOR_DB,
                   inst_ids: list[str] | None = None,
                   chain: str = "xlayer") -> dict:
    universe = token_universe(monitor_path)
    automation = load_toml(automation_path)
    inst_ids = inst_ids or [str(x) for x in (automation.get("allowlist") or {}).get("instruments") or []]
    okx = (okx_section(client, inst_ids) if client is not None
           else _source("unconfigured", error="no_okx_credentials"))
    wallet = (wallet_section(runner, chain) if runner is not None
              else _source("unconfigured", error="no_wallet_runner"))
    multipliers = {inst_id: "1" for inst_id in inst_ids}
    ledger = ledger_section(ledger_path)
    gas = gas_banner(monitor_db)
    grey = grey_scale_section(
        automation_path=automation_path, ledger_path=ledger_path,
        wallet_balances_rows=(wallet.get("balances") or []
                             if isinstance(wallet, dict) else []))
    snapshot = {
        "generated_at_ms": now_ms(),
        "okx": okx,
        "wallet": wallet,
        "exposure": exposure_section(
            wallet, okx, universe, multipliers, spot_open_costs(ledger_path)),
        "risk": risk_parameters(automation_path, monitor_path),
        "ledger": ledger,
        "gas": gas,
        "grey_scale": grey,
        "universe": trade_universe(automation_path=automation_path,
                                   monitor_path=monitor_path),
        "instruments": inst_ids,
    }
    snapshot.update(verification(snapshot))
    return snapshot


# The sources whose failure means "this account's state is not established".
# Gas is in the list on purpose: a sponsorship reading of "unknown" would
# otherwise let the cost model's ``roundtrip_gas_budget_usdt = "0"`` stand in
# for a measurement.
REQUIRED_SOURCES = ("okx", "wallet", "gas", "ledger")


def verification(snapshot: dict) -> dict:
    """``verified`` plus a per-source freshness list.

    Measured 2026-09-16: the JSON said ``okx.status="error"`` and
    ``wallet.status="error"`` while the process still exited 0, so a cron or a
    health check could not tell a verified account from an outage. The verdict
    is computed once here and used by both the CLI (exit code) and the
    unattended executor (refuse to start).
    """
    checks: list[dict] = []
    for name in REQUIRED_SOURCES:
        section = snapshot.get(name) or {}
        status = str(section.get("status") or "missing")
        ok = status in {"ok", "sponsored"}
        checks.append({"source": name, "status": status, "ok": ok,
                       "fetched_at_ms": section.get("fetched_at_ms"),
                       "error": section.get("error")})
    stale = [row["source"] for row in checks if not row["ok"]]
    return {"verified": not stale,
            "unverified_sources": stale,
            "source_checks": checks}


# --- rendering ---------------------------------------------------------


# Same rule as the market board (operator, 2026-09-16: "价格保留5位小数已经足够"):
# the default is the price precision, and the columns that are *not* prices say so
# by passing their own places (balances 9, bps 2, notionals 2).
PRICE_PLACES = 5


def _fmt(value, places: int = PRICE_PLACES) -> str:
    if value in (None, ""):
        return "—"
    try:
        # Unary plus normalises exponents like 0E+18 before formatting.
        return f"{+Decimal(str(value)):.{places}f}"
    except (InvalidOperation, ValueError):
        return str(value)


def _age_text(ms: int | None, now: int) -> str:
    if not ms:
        return "—"
    return f"{(max(0, now - int(ms))) / 1000:.0f}s 前"


def _universe_html(universe: dict) -> str:
    """The "what can this thing actually trade" block, on the board.

    Operator asked for it directly (2026-09-16): list the monitored instruments
    and which of them the machine may act on, so the two are never conflated.
    """
    if not universe:
        return ""
    escape = lambda value: html.escape("—" if value in (None, "") else str(value))
    rows = []
    for row in universe.get("auto_executable") or []:
        rows.append(
            "<tr><td>✓</td>"
            f"<td>{escape(row.get('asset'))}</td>"
            f"<td>{escape(row.get('venue'))}:{escape(row.get('instrument'))}</td>"
            f"<td>{escape(row.get('spot_token'))}</td>"
            f"<td class='muted'>{escape(row.get('spot_address'))}</td>"
            "<td>可自动下单</td></tr>")
    for row in universe.get("monitor_only") or []:
        if row.get("executable"):
            continue
        rows.append(
            "<tr><td>·</td>"
            f"<td>{escape(row.get('asset'))}</td>"
            f"<td>{escape(row.get('venue'))}:{escape(row.get('instrument'))}</td>"
            f"<td>{escape(row.get('quote'))}</td><td class='muted'>—</td>"
            f"<td>{escape('；'.join(row.get('blocked_by') or []))}</td></tr>")
    counts = universe.get("counts") or {}
    return (
        '<h2>监控范围 vs 可自动执行</h2>'
        f'<p class="muted">监控 {escape(counts.get("monitored"))} 个合约，其中 '
        f'<b>{escape(counts.get("auto_executable"))}</b> 个可以自动下单；'
        f'链 {escape(",".join(universe.get("chain_ids") or []))} · '
        f'支付币 {escape(",".join(universe.get("quote_tokens") or []))} · '
        f'钱包 {escape(",".join(universe.get("wallets") or []))}<br>'
        f'{escape(universe.get("note"))}</p>'
        '<table><thead><tr><th></th><th>标的</th><th>合约（执行方）</th>'
        '<th>现货代币</th><th>代币地址</th><th>说明</th></tr></thead><tbody>'
        + "".join(rows) + '</tbody></table>')


def holdings_banner_html(snapshot: dict | None) -> str:
    """Open stock tokens, with entry price and floating PnL, above either tab."""
    exposure = (snapshot or {}).get("exposure") or {}
    rows = [row for row in (exposure.get("unhedged") or [])
            if row.get("spot_avg_price") or row.get("combined_pnl_usd")]
    if not rows:
        return ""
    escape = lambda value: html.escape("—" if value in (None, "") else str(value))
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(x)}</td>" for x in (
            row.get("asset"),
            _fmt(row.get("token_held"), 6),
            _fmt(row.get("spot_avg_price"), 5),
            _fmt(row.get("spot_floating_pnl_usd"), 4),
            _fmt(row.get("perp_floating_pnl_usd"), 4),
            _fmt(row.get("combined_pnl_usd"), 4),
        )) + "</tr>"
        for row in rows)
    return (
        '<div style="padding:8px 16px 0">'
        '<table><thead><tr><th>持仓</th><th>数量</th><th>现货开仓均价</th>'
        '<th>现货浮动盈亏</th><th>永续浮动盈亏</th><th>两腿合计浮动盈亏</th>'
        '</tr></thead><tbody>' + body + '</tbody></table></div>')


def account_html(snapshot: dict | None) -> str:
    escape = lambda value: html.escape("—" if value in (None, "") else str(value))
    s = snapshot or {}
    generated = _int(s.get("generated_at_ms")) or now_ms()
    okx = s.get("okx") or {}
    wallet = s.get("wallet") or {}
    exposure = s.get("exposure") or {}
    risk = s.get("risk") or {}
    limits = risk.get("limits") or {}
    orders = risk.get("orders") or {}
    automation = risk.get("automation") or {}
    monitor = risk.get("monitor") or {}
    costs = risk.get("costs") or {}
    gas = s.get("gas") or {}
    grey = s.get("grey_scale") or {}
    universe = s.get("universe") or {}

    def source_badge(section: dict, label: str) -> str:
        status = section.get("status") or "unknown"
        css = {"ok": "ok", "error": "bad", "unconfigured": "warn",
               "empty": "warn"}.get(status, "warn")
        detail = section.get("error") or section.get("detail") or ""
        return (f'<span class="badge {css}">{escape(label)}：{escape(status)}'
                f'{" · " + escape(detail) if detail else ""}'
                f'{" · " + escape(_age_text(section.get("fetched_at_ms"), generated)) if section.get("fetched_at_ms") else ""}'
                '</span>')

    balance_rows = "".join(
        "<tr>" + "".join(f"<td>{escape(x)}</td>" for x in (
            row.get("ccy"), _fmt(row.get("avail"), 6), _fmt(row.get("equity"), 6),
            _fmt(row.get("usd"), 2), _fmt(row.get("frozen"), 6))) + "</tr>"
        for row in (okx.get("balances") or [])
    ) or '<tr><td colspan="5" class="muted">无余额数据</td></tr>'

    position_rows = "".join(
        "<tr>" + "".join(f"<td>{escape(x)}</td>" for x in (
            row.get("instrument"), row.get("direction"), row.get("size"),
            _fmt(row.get("avg_price"), 5), _fmt(row.get("mark_price"), 5),
            _fmt(row.get("notional_usd"), 2), _fmt(row.get("unrealized_pnl"), 4),
            escape(row.get("leverage")), _fmt(row.get("liquidation_price"), 5))) + "</tr>"
        for row in (okx.get("positions") or [])
    ) or '<tr><td colspan="9" class="muted">无持仓</td></tr>'

    order_rows = "".join(
        "<tr>" + "".join(f"<td>{escape(x)}</td>" for x in (
            row.get("instrument"), row.get("side"), row.get("size"),
            row.get("filled"), _fmt(row.get("price"), 5), row.get("state"))) + "</tr>"
        for row in (okx.get("pending_orders") or [])
    ) or '<tr><td colspan="6" class="muted">无挂单</td></tr>'

    wallet_rows = "".join(
        "<tr>" + "".join(f"<td>{escape(x)}</td>" for x in (
            # Balances keep their high precision (a token balance is a quantity);
            # the price is a price, so it follows the 5-decimal rule like every
            # other price on the boards.
            row.get("symbol"), _fmt(row.get("balance"), 9),
            _fmt(row.get("open_avg_price"), 5), _fmt(row.get("price_usd"), 5),
            _fmt(row.get("usd_value"), 2), _fmt(row.get("floating_pnl_usd"), 4),
            row.get("token_address"))) + "</tr>"
        for row in (wallet.get("balances") or [])
    ) or '<tr><td colspan="7" class="muted">无余额数据</td></tr>'

    unhedged_rows = "".join(
        f'<tr class="{"flat" if row.get("flat") else "exposed"}">' + "".join(
            f"<td>{escape(x)}</td>" for x in (
                row.get("asset"), _fmt(row.get("token_held"), 9),
                _fmt(row.get("token_shorted"), 9), _fmt(row.get("net_exposure_tokens"), 9),
                _fmt(row.get("net_exposure_usd"), 2), _fmt(row.get("hedge_ratio_pct"), 2),
                _fmt(row.get("spot_avg_price"), 5),
                _fmt(row.get("spot_open_cost_usd"), 4),
                _fmt(row.get("spot_floating_pnl_usd"), 4),
                _fmt(row.get("perp_floating_pnl_usd"), 4),
                _fmt(row.get("combined_pnl_usd"), 4))) + "</tr>"
        for row in (exposure.get("unhedged") or [])
    ) or '<tr><td colspan="11" class="muted">无数据</td></tr>'

    fill_rows = "".join(
        "<tr>" + "".join(f"<td>{escape(x)}</td>" for x in (
            row.get("instrument"), row.get("side"), row.get("size"),
            _fmt(row.get("price"), 5), _fmt(row.get("fee"), 8),
            (f'{_fmt(row.get("fee_bps"), 2)} bps' if row.get("fee_bps") else "—"),
            row.get("exec_type"), row.get("ts_ms"))) + "</tr>"
        for row in (okx.get("recent_fills") or [])
    ) or '<tr><td colspan="8" class="muted">无成交记录</td></tr>'

    fee_rows = "".join(
        "<tr>" + "".join(f"<td>{escape(x)}</td>" for x in (
            row.get("instrument"), row.get("level"), _fmt(row.get("taker_bps"), 2),
            row.get("error"))) + "</tr>"
        for row in (okx.get("fees") or [])
    ) or '<tr><td colspan="4" class="muted">未取得费率</td></tr>'

    open_groups = s.get("ledger", {}).get("open_groups") or []
    ledger_rows = "".join(
        "<tr>" + "".join(f"<td>{escape(x)}</td>" for x in (
            row.get("group_id"), row.get("state"), row.get("signal_id"),
            row.get("event_type"), row.get("updated_ms"))) + "</tr>"
        for row in open_groups
    ) or '<tr><td colspan="5" class="muted">无未结执行组</td></tr>'

    allow = risk.get("allowlist") or {}
    network = risk.get("network") or {}
    approval = risk.get("approval") or {}
    kill = automation.get("kill_switch_tripped")
    kill_css = "bad" if kill else "ok"
    gas_status = gas.get("status") or "unknown"
    gas_line = {
        "paid": f'⚠️ 已检测到实际支付 gas：{gas.get("note")}',
        "paid_other": f'⚠️ 有非交易类交易支付了 gas：{gas.get("note")}',
        "sponsored": f'X Layer 仍在赞助 gas：{gas.get("note") or "—"}（成本模型里的 gas=0 仍然成立）',
        "unknown": f'gas 状态未知：{gas.get("note") or "尚无观测"}（不要假设仍然是 0）',
    }.get(gas_status, f'gas 状态未知：{gas_status}')
    gas_banner_html = (
        '<div class="gasbanner %s">%s</div>' % (
            {"paid": "paid", "paid_other": "other", "sponsored": "sponsored"}.get(
                gas_status, "unknown"),
            escape(gas_line))
    )
    gas_css = {"paid": "bad", "paid_other": "warn", "sponsored": "ok"}.get(gas_status, "warn")
    rules = [
        ("执行模式", automation.get("mode"), ""),
        ("自动执行", {"live": "live（真实下单）", "paper": "paper（不产生成交）"}
         .get(str(automation.get("auto_orders")), automation.get("auto_orders")),
         "bad" if str(automation.get("auto_orders")) == "live" else ""),
        # The gray-scale ceiling, on the board because the operator asked for it
        # ("在面板上显示出来这个参数"): what the policy allows, what the wallet can
        # actually afford, and what is left of today's quota.
        ("灰度单笔上限（quote）", grey.get("per_trade_quote_usdt"), ""),
        ("灰度每日上限（quote）", grey.get("daily_quote_usdt"), ""),
        ("灰度实际单笔（min(上限, 钱包)）", grey.get("affordable_trade_quote"), ""),
        ("钱包可用（quote）", grey.get("wallet_quote_balance"), ""),
        ("今日已用（真实成交）", grey.get("today_real_used_quote"), ""),
        ("今日剩余（真实额度）",
         (f'{grey.get("today_remaining_real_quote")}'
          + ("（已超额：自动链今日不再开新仓，人工演练不受此限）"
             if grey.get("today_quota_exhausted") else "")),
         "warn" if grey.get("today_quota_exhausted") else ""),
        ("今日已用（纸面，不计额度）", grey.get("today_paper_used_quote"), ""),
        ("现货先确认再对冲", automation.get("spot_must_confirm_before_hedge"), ""),
        ("未知状态处理", automation.get("unknown_state_action"), ""),
        ("Kill switch", "已触发（停止一切执行）" if kill else "未触发", kill_css),
        ("Kill switch 文件", automation.get("kill_switch_file"), ""),
        ("单笔上限（USDT）", limits.get("per_trade_quote_usdt"), ""),
        ("每日上限（USDT）", limits.get("daily_quote_usdt"), ""),
        ("最大滑点（bps）", limits.get("max_slippage_bps"), ""),
        ("最大未对冲数量", limits.get("max_unhedged_tokens"), ""),
        ("最大开放合约", limits.get("max_open_contracts"), ""),
        ("报价有效期（秒）", limits.get("quote_ttl_seconds"), ""),
        ("下单风格", orders.get("style"), ""),
        ("永续腿订单类型", orders.get("perp_order_type"), ""),
        ("永续腿限价来源", orders.get("perp_price_source"), ""),
        ("永续腿最大让价（bps）", orders.get("perp_max_slippage_bps"), ""),
        ("允许 maker 单", orders.get("allow_maker_orders"), ""),
        ("允许挂单留存", orders.get("allow_resting_orders"), ""),
        ("挂单最长等待（秒）", orders.get("maker_timeout_seconds"), ""),
        ("最多重挂次数", orders.get("maker_max_reprices"), ""),
        ("裸空窗口上限（秒）", orders.get("maker_max_naked_seconds"), ""),
        ("现货失败即回补", orders.get("maker_unwind_on_spot_failure"), ""),
        ("授权模式", approval.get("mode"), ""),
        ("单次授权上限（USDT）", approval.get("max_allowance_quote"), ""),
        ("授权额度回读", approval.get("require_allowance_readback"), ""),
        ("信号阈值（净基差 bps）", monitor.get("min_net_edge_bps"), ""),
        ("最小净利润（USDT）", monitor.get("min_net_profit_quote"), ""),
        ("盘口最大年龄（ms）", monitor.get("max_age_ms"), ""),
        ("双腿同时性上限（ms）", monitor.get("max_pair_skew_ms"), ""),
        ("同标的提醒冷却（ms）", monitor.get("signal_cooldown_ms"), ""),
        ("总资金上限（USDT）", monitor.get("total_capital_usdt"), ""),
        ("现货/保证金分配（USDT）",
         f'{monitor.get("spot_allocation_usdt")} / {monitor.get("margin_allocation_usdt")}', ""),
        ("成本：链上腿费率（bps）", costs.get("spot_fee_bps"), ""),
        ("成本：永续腿费率（bps）", costs.get("perp_fee_bps"), ""),
        ("成本：滑点缓冲（bps）", costs.get("slippage_bps"), ""),
        ("成本：失败缓冲（bps）", costs.get("failure_bps"), ""),
        ("成本是否已验证", "是" if costs.get("verified") else "否（保守默认值）",
         "" if costs.get("verified") else "warn"),
        ("Gas 现状（实测回执）", gas_line, gas_css),
        ("网络线路顺序", " → ".join(network.get("routes") or []), ""),
        ("读重试 / 写重试", f'{network.get("read_retries")} / {network.get("write_retries")}', ""),
        ("白名单资产", "、".join(allow.get("assets") or []), ""),
        ("白名单合约", "、".join(allow.get("instruments") or []), ""),
        ("白名单链", "、".join(allow.get("chains") or []), ""),
        ("白名单交易所", "、".join(allow.get("venues") or []), ""),
    ]
    rule_rows = "".join(
        f'<tr class="{css}"><td>{escape(k)}</td><td>{escape(v)}</td></tr>'
        for k, v, css in rules
    )
    return (
        '<!doctype html><html lang="zh"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>账户与风控看板</title>'
        '<style>body{max-width:1280px;margin:28px auto;padding:0 20px;'
        'font:14px system-ui;background:#0f161d;color:#e6edf3}h1{font-size:24px}'
        'h2{font-size:17px;margin-top:30px;border-left:3px solid #78dcca;padding-left:8px}'
        '.badge{padding:8px 12px;margin:6px 6px 6px 0;display:inline-block;background:#243747;'
        'border-radius:8px;font-size:13px}.badge.ok{border-left:3px solid #3fb950}'
        '.badge.bad{border-left:3px solid #e5534b}.badge.warn{border-left:3px solid #e9ae62}'
        'table{width:100%;border-collapse:collapse;background:#16222c;margin-top:6px}'
        'td,th{padding:8px 10px;text-align:left;border-bottom:1px solid #2b3b49;font-size:13px}'
        'td{font-family:ui-monospace,monospace}th{color:#78dcca}.muted{color:#93a4b3}'
        'small{color:#93a4b3}tr.ok td:first-child{border-left:3px solid #3fb950}'
        'tr.warn td:first-child{border-left:3px solid #e9ae62}'
        'tr.bad td:first-child{border-left:3px solid #e5534b}'
        'tr.exposed{background:#3a2624}tr.flat{background:#1b2b22}'
        '.grid{display:grid;grid-template-columns:1fr 1fr;gap:24px}'
        '@media(max-width:900px){.grid{grid-template-columns:1fr}}'
        '.gasbanner{padding:12px 14px;margin:14px 0;border-radius:8px;font-weight:600}'
        '.gasbanner.paid{background:#5a1f1c;border-left:4px solid #ff4d4f;color:#ffe6e6}'
        '.gasbanner.other{background:#3a3320;border-left:4px solid #e9ae62;color:#f7e9c8}'
        '.gasbanner.sponsored{background:#16301f;border-left:4px solid #3fb950;color:#d6f5df}'
        '.gasbanner.unknown{background:#3a3320;border-left:4px solid #e9ae62;color:#f7e9c8}'
        '</style>'
        '<h1>账户与风控看板</h1>'
        + gas_banner_html +
        f'<p class="muted">只读视图 · 生成于 {escape(_age_text(generated, now_ms()))}'
        f'（{escape(generated)} ms）· 每 20 秒自动重新读取一次 · 不下单、不签名</p>'
        + source_badge(okx, "OKX") + source_badge(wallet, "X Layer 钱包")
        + source_badge(s.get("ledger") or {}, "执行账本")
        + '<h2>资金</h2><div class="grid">'
        '<div><table><thead><tr><th>OKX 币种</th><th>可用</th><th>权益</th><th>USD</th>'
        '<th>冻结</th></tr></thead><tbody>' + balance_rows + '</tbody></table></div>'
        f'<div><table><thead><tr><th>链上代币</th><th>余额</th><th>开仓均价</th>'
        f'<th>现价 USD</th><th>USD 估值</th><th>现货浮动盈亏</th>'
        f'<th>合约地址</th></tr></thead><tbody>' + wallet_rows + '</tbody></table>'
        f'<p class="muted">钱包地址：{escape(wallet.get("address"))}</p></div></div>'
        '<h2>持仓与敞口</h2>'
        '<table><thead><tr><th>合约</th><th>方向</th><th>张数</th><th>开仓均价</th>'
        '<th>标记价</th><th>名义 USD</th><th>未实现盈亏</th><th>杠杆</th><th>强平价</th>'
        '</tr></thead><tbody>' + position_rows + '</tbody></table>'
        '<table style="margin-top:14px"><thead><tr><th>标的</th><th>持有代币</th>'
        '<th>已对冲代币</th><th>净敞口代币</th><th>净敞口 USD</th><th>对冲比例 %</th>'
        '<th>现货开仓均价</th><th>现货开仓成本 USD</th>'
        '<th>现货浮动盈亏</th><th>永续浮动盈亏</th><th>两腿合计浮动盈亏</th>'
        '</tr></thead><tbody>'
        + unhedged_rows + '</tbody></table>'
        '<p class="muted">「已对冲代币」按合约张数 × 合约乘数折算；乘数未知时留空，'
        '不假设 1 张 = 1 枚。净敞口 = 持有代币 − 已对冲代币。'
        '现货开仓均价 = 账本里最近一笔现货成交金额 ÷ 当前持有数量。'
        '现货浮动盈亏 = 钱包现价估值 − 开仓成本。永续浮动盈亏是交易所的未实现盈亏。'
        '两腿合计是这两项相加。'
        '净敞口非零的行标红。</p>'
        '<h2>挂单</h2><table><thead><tr><th>合约</th><th>方向</th><th>数量</th>'
        '<th>已成交</th><th>价格</th><th>状态</th></tr></thead><tbody>' + order_rows
        + '</tbody></table>'
        '<h2>最近成交与真实费率</h2>'
        '<table><thead><tr><th>合约</th><th>方向</th><th>数量</th><th>成交价</th>'
        '<th>手续费</th><th>费率</th><th>类型</th><th>时间(ms)</th></tr></thead><tbody>'
        + fill_rows + '</tbody></table>'
        '<table style="margin-top:14px"><thead><tr><th>合约</th><th>费率等级</th>'
        '<th>taker bps</th><th>错误</th></tr></thead><tbody>' + fee_rows + '</tbody></table>'
        + _universe_html(universe) +
        '<h2>风控参数（当前生效值）</h2>'
        '<table><thead><tr><th>参数</th><th>值</th></tr></thead><tbody>' + rule_rows
        + '</tbody></table>'
        f'<p class="muted">{escape(costs.get("evidence") or "")}</p>'
        '<h2>执行账本（未结组）</h2>'
        '<p class="muted">「未结组」只包含仍在进行中的执行组；一次 dry-run 计划、'
        '被拒绝的请求、已平仓的组合都不会留在这里。</p>'
        '<table><thead><tr><th>组</th><th>状态</th><th>信号</th><th>事件</th><th>时间(ms)</th>'
        '</tr></thead><tbody>' + ledger_rows + '</tbody></table>'
        '<p class="muted">本页只是读数视图：任何下单都必须经过 RiskGate 与人工授权，'
        '页面不提供下单入口。数据源失败时显示 UNKNOWN，不会回退到旧值。</p>'
        '<script>setTimeout(function(){location.reload()},20000)</script></html>'
    )


def serve_dashboard(port: int = 8766, *, ttl_seconds: int = 15,
                    monitor_path: str = MONITOR_CONFIG,
                    automation_path: str = AUTOMATION_POLICY,
                    ledger_path: str = EXECUTION_LEDGER,
                    okx_env: str | None = "config/okx-demo.env") -> int:
    client = OKXReadOnlyClient.from_env(okx_read_env(okx_env))
    runner = OnchainOSRunner()
    cache: dict = {"at_ms": 0, "snapshot": None}

    def snapshot(force: bool = False) -> dict:
        if not force and cache["snapshot"] is not None and now_ms() - cache["at_ms"] < ttl_seconds * 1000:
            return cache["snapshot"]
        value = build_snapshot(client=client if client.configured else None, runner=runner,
                               monitor_path=monitor_path, automation_path=automation_path,
                               ledger_path=ledger_path)
        cache.update(at_ms=now_ms(), snapshot=value)
        return value

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # keep the console readable
            pass

        def do_GET(self) -> None:
            if self.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if self.path.split("?")[0] not in ("/", "/api/account"):
                self.send_error(404)
                return
            payload = dumps(snapshot(force="refresh=1" in self.path))
            if self.path.startswith("/api/account"):
                content, mime = payload, "application/json"
            else:
                content, mime = account_html(snapshot()), "text/html"
            body = content.encode()
            self.send_response(200)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Read-only account dashboard: http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
