"""Hard-coded decision core for the Ethereum xStocks auto chain.

This module does not touch the network and does not sign anything.  It turns
the v4 watch output into candidates and applies the operator-owned policy
gates.  The resident orchestration lives in ``scripts/xstocks_eth_auto_live.py``.
"""
from __future__ import annotations

import json
import time
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
POLICY_PATH = ROOT / "ops" / "xstocks_eth_policy.toml"
WATCH_PATH = ROOT / "data" / "xstocks_v4_watch.json"
ROUTE_PATH = ROOT / "data" / "xstocks_route_matrix.json"
STATE_PATH = ROOT / "data" / "xstocks_eth_auto.json"
LEDGER_PATH = ROOT / "data" / "xstocks_eth_cycles.jsonl"
LOCK_PATH = ROOT / "data" / "xstocks_eth_cycle.lock"
PAUSE_PATH = ROOT / "data" / "xstocks_eth_auto.paused"
ALERT_DIR = ROOT / "data" / "xstocks_eth_alerts"
KILL_SWITCH = ROOT / "data" / "KILL_SWITCH"
AUTO_HALT = ROOT / "data" / "AUTO_HALT"

MODE_OFF, MODE_PAPER, MODE_LIVE = "off", "paper", "live"


def _d(value, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value is not None else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def day_key(now: float | None = None) -> str:
    return datetime.fromtimestamp(now if now is not None else time.time()).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class EthPolicy:
    mode: str
    auto_orders: str
    live_ack: str
    per_trade_quote_usdt: Decimal
    min_net_profit_usdt: Decimal
    allowed_tier: str
    max_slippage_bps: int
    kraken_fee_bps: Decimal
    slippage_buffer_bps: Decimal
    max_book_slip_bps: Decimal
    max_book_share: Decimal
    daily_open_quote_usdt: Decimal
    daily_trades_max: int
    loss_alert_trades: int
    loss_pause_trades: int
    snapshot_max_age_s: int
    poll_interval_s: int
    eth_usdc_floor_usdt: Decimal
    eth_usdc_target_usdt: Decimal
    eth_gas_floor_eth: Decimal
    kraken_min_before_withdraw: Decimal
    withdraw_fee_usdt: Decimal

    @classmethod
    def load(cls, path: Path | str = POLICY_PATH) -> "EthPolicy":
        raw = tomllib.loads(Path(path).read_text())
        t, e = raw["trade"], raw["exit"]
        lim, ex = raw["limits"], raw.get("execution", {})
        watch, cap, cost = raw["watch"], raw["capital"], raw["costs"]
        return cls(
            mode=str(ex.get("mode", MODE_OFF)),
            auto_orders=str(ex.get("auto_orders", MODE_OFF)),
            live_ack=str(ex.get("live_ack", "") or ""),
            per_trade_quote_usdt=_d(t["per_trade_quote_usdt"]),
            min_net_profit_usdt=_d(t["min_net_profit_usdt"]),
            allowed_tier=str(t.get("allowed_tier", "green")),
            max_slippage_bps=int(t["max_slippage_bps"]),
            kraken_fee_bps=_d(e["kraken_fee_bps"]),
            slippage_buffer_bps=_d(e.get("slippage_buffer_bps", "10")),
            max_book_slip_bps=_d(e["max_book_slip_bps"]),
            max_book_share=_d(e["max_book_share"]),
            daily_open_quote_usdt=_d(lim["daily_open_quote_usdt"]),
            daily_trades_max=int(lim["daily_trades_max"]),
            loss_alert_trades=int(lim["loss_alert_trades"]),
            loss_pause_trades=int(lim["loss_pause_trades"]),
            snapshot_max_age_s=int(watch["snapshot_max_age_s"]),
            poll_interval_s=int(watch["poll_interval_s"]),
            eth_usdc_floor_usdt=_d(cap["eth_usdc_floor_usdt"]),
            eth_usdc_target_usdt=_d(cap["eth_usdc_target_usdt"]),
            eth_gas_floor_eth=_d(cap["eth_gas_floor_eth"]),
            kraken_min_before_withdraw=_d(cap["kraken_min_before_withdraw"]),
            withdraw_fee_usdt=_d(cost["withdraw_fee_usdt"]),
        )

    def live_ok(self) -> tuple[bool, str]:
        if self.auto_orders == MODE_OFF:
            return False, "auto_orders=off（只巡检，不动钱）"
        if self.mode != MODE_LIVE:
            return False, f"mode={self.mode}（不是 live）"
        if not self.live_ack.strip():
            return False, "live_ack 为空（operator 没落款）"
        return True, "live 已授权"

    def tradable_today(self) -> bool:
        return self.auto_orders in (MODE_PAPER, MODE_LIVE)


@dataclass
class EthState:
    day: str = field(default_factory=day_key)
    phase: str = "idle"
    running: bool = False
    updated_ms: int = 0
    trades_today: int = 0
    opened_quote_usdt: str = "0"
    loss_streak: int = 0
    pause_reason: str = ""
    note: str = ""
    balances: dict = field(default_factory=dict)
    last_candidates: list = field(default_factory=list)
    last_cycle: dict | None = None

    def to_json(self) -> dict:
        return asdict(self)


def load_watch(path: Path | str = WATCH_PATH) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def snapshot_age_s(watch: dict, now_ms: int | None = None) -> float | None:
    stamp = watch.get("generated_ms")
    if not stamp:
        return None
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    return max(0.0, (now - float(stamp)) / 1000.0)


def order_candidates(watch: dict, policy: EthPolicy, *, limit: int = 3) -> list[dict]:
    """Candidate rows that are eligible for a fresh ETH re-check."""
    rows: list[dict] = []
    for row in watch.get("rows") or []:
        if row.get("chain") != "eth":
            continue
        if row.get("tier") != policy.allowed_tier:
            continue
        if not row.get("direct_deposit"):
            continue
        if not row.get("deposit_credit_verified"):
            continue
        if row.get("ref_stale") or row.get("exit_cache_stale"):
            continue
        net = _d(row.get("best_net_usdt"), "-999999")
        if net <= policy.min_net_profit_usdt:
            continue
        largest = _d(row.get("largest_profitable_size_usd"), "0")
        best = _d(row.get("best_size_usd"), "0")
        size = min(policy.per_trade_quote_usdt, largest or best)
        if size <= 0:
            continue
        item = dict(row)
        item["_net"] = net
        item["_size_usd"] = size
        rows.append(item)
    rows.sort(key=lambda row: (-row["_net"], str(row.get("symbol"))))
    return rows[:limit]


def estimate_net(policy: EthPolicy, *, size_usd: Decimal, kraken_units: Decimal,
                 sell_price: Decimal, gas_usd: Decimal) -> dict:
    proceeds = kraken_units * sell_price
    gross = proceeds - size_usd
    sell_fee = proceeds * policy.kraken_fee_bps / Decimal(10000)
    buffer = size_usd * policy.slippage_buffer_bps / Decimal(10000)
    net = gross - sell_fee - buffer - gas_usd
    return {
        "proceeds": proceeds,
        "gross_usdt": gross,
        "sell_fee_usdt": sell_fee,
        "slippage_buffer_usdt": buffer,
        "gas_usdt": gas_usd,
        "net_usdt": net,
    }


def open_gate(policy: EthPolicy, state: EthState, *, snapshot_age: float | None,
              kill_switch: bool, auto_halt: bool, eth_usdc: Decimal | None,
              eth_gas: Decimal | None) -> tuple[bool, str]:
    if kill_switch:
        return False, "KILL_SWITCH 存在（开平仓都停）"
    if state.pause_reason:
        return False, f"软暂停：{state.pause_reason}"
    if auto_halt:
        return False, "AUTO_HALT 存在（只挡开仓）"
    if not policy.tradable_today():
        return False, f"auto_orders={policy.auto_orders}（不动钱）"
    if snapshot_age is None:
        return False, "没有 v4 watch 快照"
    if snapshot_age > policy.snapshot_max_age_s:
        return False, f"快照过期 {snapshot_age:.0f}s > {policy.snapshot_max_age_s}s"
    if state.trades_today >= policy.daily_trades_max:
        return False, f"今天已开 {state.trades_today} 笔，到上限 {policy.daily_trades_max}"
    opened = _d(state.opened_quote_usdt)
    if opened >= policy.daily_open_quote_usdt:
        return False, f"今天累计开仓 {opened}，到上限 {policy.daily_open_quote_usdt}"
    if eth_gas is not None and eth_gas < policy.eth_gas_floor_eth:
        return False, f"ETH gas {eth_gas} < floor {policy.eth_gas_floor_eth}"
    if eth_usdc is not None and eth_usdc < policy.eth_usdc_floor_usdt:
        return False, f"ETH USDC {eth_usdc} < floor {policy.eth_usdc_floor_usdt}"
    return True, "可以开仓"


def review_ok(policy: EthPolicy, *, tier: str, direct_deposit: bool,
              deposit_unit_verified: bool, size_usd: Decimal,
              kraken_units: Decimal, sell_price: Decimal, order_min: Decimal,
              book_ok: bool, book_reason: str, gas_usd: Decimal) -> tuple[bool, str, dict]:
    if tier != policy.allowed_tier:
        return False, f"tier={tier}，policy 只允许 {policy.allowed_tier}", {}
    if not direct_deposit:
        return False, "不是 Kraken 直达充值路径", {}
    if not deposit_unit_verified:
        return False, "Kraken 入账单位未验证", {}
    if size_usd <= 0:
        return False, "尺寸为 0", {}
    if size_usd > policy.per_trade_quote_usdt:
        return False, f"尺寸 {size_usd} 超过单笔上限 {policy.per_trade_quote_usdt}", {}
    if kraken_units < order_min:
        return False, f"数量 {kraken_units} 低于 Kraken 最小 {order_min}", {}
    if not book_ok:
        return False, f"出口不成立：{book_reason}", {}
    stats = estimate_net(policy, size_usd=size_usd, kraken_units=kraken_units,
                         sell_price=sell_price, gas_usd=gas_usd)
    if stats["net_usdt"] <= policy.min_net_profit_usdt:
        return False, f"净利 {stats['net_usdt']} ≤ {policy.min_net_profit_usdt}", stats
    return True, "复核通过", stats


def treasury_decision(policy: EthPolicy, *, kraken_usd: Decimal,
                      eth_usdc: Decimal) -> dict:
    if eth_usdc >= policy.eth_usdc_target_usdt:
        return {"action": "keep", "reason": "ETH USDC 足够"}
    needed = policy.eth_usdc_target_usdt + policy.withdraw_fee_usdt
    if kraken_usd < max(policy.kraken_min_before_withdraw, needed):
        return {"action": "wait", "reason": "Kraken USD 还不够"}
    return {
        "action": "withdraw",
        "asset": "USDC",
        "key": "ethusdc1",
        "amount": policy.eth_usdc_target_usdt,
        "fee": policy.withdraw_fee_usdt,
    }
