"""xStocks 自动链的判定核心。**不碰网络、不签名、不起进程**——全部可离线测。

分工（operator 2026-10-06 定的计划）：

* 这里只做「读快照 → 闸门 → 排序 → 复核判定 → 回款判定 → 状态/账本/告警记录」；
* 真正的网络与钱包动作在 `scripts/xstocks_auto_live.py`（注入进来，便于测试）；
* 阈值只有一份来源：`ops/xstocks_policy.toml`，不散落在代码里；
* **LLM 不在下单路径上**：所有放行/拒绝都是这里的硬编码规则。

设计要点：

1. 候选来自看板写出的快照（`data/xstocks_gap_live.json`），人机同源。
2. 看板数字只当**候选**；下单前必须用新鲜报价与盘口重新复核（`review_ok`）。
3. 一笔在飞、失败即停、未知即停机——状态机在 `xstocks_cycle.py`，这里只提供判定。
4. 两链资金目标只做**告警**，不自动跨链调配（operator 决定补哪条链）。
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
POLICY_PATH = ROOT / "ops" / "xstocks_policy.toml"
SNAPSHOT_PATH = ROOT / "data" / "xstocks_gap_live.json"
STATE_PATH = ROOT / "data" / "xstocks_auto.json"
LEDGER_PATH = ROOT / "data" / "xstocks_cycles.jsonl"
PAUSE_PATH = ROOT / "data" / "xstocks_auto.paused"
ALERT_DIR = ROOT / "data" / "xstocks_alerts"
TRANSFER_SEEN = ROOT / "data" / "xstocks_transfer_seen.json"
KILL_SWITCH = ROOT / "data" / "KILL_SWITCH"
AUTO_HALT = ROOT / "data" / "AUTO_HALT"

MODE_OFF, MODE_PAPER, MODE_LIVE = "off", "paper", "live"


def _d(value, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value is not None else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def day_key(now: float | None = None) -> str:
    """本地日期（Asia/Shanghai 机器时区），账本按这个分天。"""
    return datetime.fromtimestamp(now if now is not None else time.time()).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class AutoPolicy:
    """自动链用到的全部阈值。唯一来源 `ops/xstocks_policy.toml`。"""

    # [execution]
    mode: str
    auto_orders: str
    live_ack: str
    # [trade] / [exit]
    per_trade_quote_usdt: Decimal
    min_net_profit_usdt: Decimal
    max_risk: str
    max_slippage_bps: int
    max_book_slip_bps: Decimal
    max_book_share: Decimal
    min_price_vs_ref: Decimal
    maker_first: bool
    maker_timeout_s: int
    # [limits]
    daily_open_quote_usdt: Decimal
    daily_trades_max: int
    loss_alert_trades: int
    loss_pause_trades: int
    # [treasury]
    asset: str
    withdraw_key: str
    solana_target_usdt: Decimal
    solana_floor_usdt: Decimal
    xlayer_target_usdg: Decimal
    xlayer_floor_usdg: Decimal
    kraken_min_before_withdraw: Decimal
    withdraw_fee_usdt: Decimal
    # [watch]
    snapshot_max_age_s: int
    poll_interval_s: int
    sol_min_alert: Decimal
    # [costs]
    buy_fee_sol: Decimal
    transfer_first_sol: Decimal
    transfer_repeat_sol: Decimal
    token_rent_sol: Decimal
    kraken_taker_bps: Decimal

    @classmethod
    def load(cls, path: Path | str = POLICY_PATH) -> "AutoPolicy":
        raw = tomllib.loads(Path(path).read_text())
        t, e, c = raw["trade"], raw["exit"], raw["capital"]
        ex, lim = raw.get("execution", {}), raw["limits"]
        tre, watch, cost = raw["treasury"], raw["watch"], raw["costs"]
        return cls(
            mode=str(ex.get("mode", MODE_PAPER)),
            auto_orders=str(ex.get("auto_orders", MODE_OFF)),
            live_ack=str(ex.get("live_ack", "") or ""),
            per_trade_quote_usdt=_d(t["per_trade_quote_usdt"]),
            min_net_profit_usdt=_d(t["min_net_profit_usdt"]),
            max_risk=str(t["max_risk"]),
            max_slippage_bps=int(t["max_slippage_bps"]),
            max_book_slip_bps=_d(e["max_book_slip_bps"]),
            max_book_share=_d(e["max_book_share"]),
            min_price_vs_ref=_d(e["min_price_vs_ref"]),
            maker_first=bool(e["maker_first"]),
            maker_timeout_s=int(e["maker_timeout_s"]),
            daily_open_quote_usdt=_d(lim["daily_open_quote_usdt"]),
            daily_trades_max=int(lim["daily_trades_max"]),
            loss_alert_trades=int(lim["loss_alert_trades"]),
            loss_pause_trades=int(lim["loss_pause_trades"]),
            asset=str(tre["asset"]),
            withdraw_key=str(tre["withdraw_key"]),
            solana_target_usdt=_d(tre["solana_target_usdt"]),
            solana_floor_usdt=_d(tre["solana_floor_usdt"]),
            xlayer_target_usdg=_d(tre["xlayer_target_usdg"]),
            xlayer_floor_usdg=_d(tre["xlayer_floor_usdg"]),
            kraken_min_before_withdraw=_d(tre["kraken_min_before_withdraw"]),
            withdraw_fee_usdt=_d(tre["withdraw_fee_usdt"]),
            snapshot_max_age_s=int(watch["snapshot_max_age_s"]),
            poll_interval_s=int(watch["poll_interval_s"]),
            sol_min_alert=_d(watch["sol_min_alert"]),
            buy_fee_sol=_d(cost["buy_fee_sol"]),
            transfer_first_sol=_d(cost["transfer_first_sol"]),
            transfer_repeat_sol=_d(cost["transfer_repeat_sol"]),
            token_rent_sol=_d(cost["token_rent_sol"]),
            kraken_taker_bps=_d(cost["kraken_taker_bps"]),
        )

    # --- 两把钥匙 ------------------------------------------------------
    def live_ok(self) -> tuple[bool, str]:
        """真钱需要三样同时成立；少一样就只做 paper/巡检。"""
        if self.auto_orders == MODE_OFF:
            return False, "auto_orders=off（只巡检，不动钱）"
        if self.mode != "live":
            return False, f"mode={self.mode}（不是 live）"
        if not self.live_ack.strip():
            return False, "live_ack 为空（operator 没落款）"
        return True, "live 已授权"

    def tradable_today(self) -> bool:
        return self.auto_orders in (MODE_PAPER, MODE_LIVE)


# ---------------------------------------------------------------------------
# 成本模型（实测值，见 docs/lessons §2.2）
# ---------------------------------------------------------------------------
def estimate_net(policy: AutoPolicy, *, size_usd: Decimal, tokens: Decimal,
                 sell_price: Decimal, sol_usd: Decimal,
                 first_transfer: bool) -> dict:
    """毛利/费用/净利：链上买入 → 充值 Kraken → 按 Kraken 买盘卖出。"""
    proceeds = tokens * sell_price
    gross = proceeds - size_usd
    sell_fee = proceeds * policy.kraken_taker_bps / Decimal(10000)
    transfer_fee = (policy.transfer_first_sol if first_transfer
                    else policy.transfer_repeat_sol)
    chain_fee = (policy.buy_fee_sol + transfer_fee) * sol_usd
    fees = sell_fee + chain_fee
    return {
        "proceeds": proceeds,
        "gross_usdt": gross,
        "sell_fee_usdt": sell_fee,
        "chain_fee_usdt": chain_fee,
        "rent_refundable_usdt": policy.token_rent_sol * sol_usd,
        "net_usdt": gross - fees,
        "first_transfer": first_transfer,
    }


# ---------------------------------------------------------------------------
# 看板快照 → 候选
# ---------------------------------------------------------------------------
def load_snapshot(path: Path | str = SNAPSHOT_PATH) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except Exception:  # noqa: BLE001
        return {}


def snapshot_age_s(snapshot: dict, now_ms: int | None = None) -> float | None:
    finished = snapshot.get("finished_ms")
    if not finished:
        return None
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    return max(0.0, (now - float(finished)) / 1000.0)


def order_candidates(snapshot: dict, policy: AutoPolicy, *, limit: int = 3) -> list[dict]:
    """按净利从高到低排出「值得复核」的候选。近失不算机会。"""
    rows = []
    for hit in snapshot.get("hits") or []:
        if hit.get("near_miss"):
            continue
        if str(hit.get("network") or "") != "Solana":
            continue                      # 本阶段自动执行只做 Solana 直买
        if (hit.get("risk") or "") != policy.max_risk:
            continue
        net = _d(hit.get("net_usdt"), "-999999")
        if net <= policy.min_net_profit_usdt:
            continue
        row = dict(hit)
        row["_net"] = net
        rows.append(row)
    rows.sort(key=lambda r: -r["_net"])
    return rows[:limit]


# ---------------------------------------------------------------------------
# 闸门
# ---------------------------------------------------------------------------
def open_gate(policy: AutoPolicy, state: "AutoState", *,
              snapshot_age: float | None, kill_switch: bool, auto_halt: bool,
              onchain_usdt: Decimal | None = None) -> tuple[bool, str]:
    """能不能开新仓。逐条硬编码，任一不过就不动钱。"""
    if kill_switch:
        return False, "KILL_SWITCH 存在（开平仓都停）"
    if state.pause_reason:
        return False, f"软暂停：{state.pause_reason}"
    if auto_halt:
        return False, "AUTO_HALT 存在（只挡开仓）"
    if not policy.tradable_today():
        return False, f"auto_orders={policy.auto_orders}（不动钱）"
    if snapshot_age is None:
        return False, "没有看板快照"
    if snapshot_age > policy.snapshot_max_age_s:
        return False, f"快照过期 {snapshot_age:.0f}s > {policy.snapshot_max_age_s}s"
    if state.trades_today >= policy.daily_trades_max:
        return False, f"今天已开 {state.trades_today} 笔，到上限 {policy.daily_trades_max}"
    opened = _d(state.opened_quote_usdt)
    if opened >= policy.daily_open_quote_usdt:
        return False, (f"今天累计开仓 {opened}，"
                       f"到上限 {policy.daily_open_quote_usdt}")
    if onchain_usdt is not None and onchain_usdt < policy.solana_floor_usdt:
        return False, (f"链上 {onchain_usdt} < floor {policy.solana_floor_usdt}"
                       "（缺钱，等人补）")
    return True, "可以开仓"


def review_ok(policy: AutoPolicy, *, size_usd: Decimal, tokens: Decimal,
              sell_price: Decimal, sol_usd: Decimal, first_transfer: bool,
              risk: str, book_ok: bool, book_reason: str,
              deposit_ok: bool, deposit_reason: str) -> tuple[bool, str, dict]:
    """下单前的固定编码复核：用**新鲜**报价与盘口重算，不看快照数字。"""
    if risk != policy.max_risk:
        return False, f"风险 {risk} 高于 {policy.max_risk}", {}
    if not deposit_ok:
        return False, f"充值不成立：{deposit_reason}", {}
    if not book_ok:
        return False, f"出口不成立：{book_reason}", {}
    stats = estimate_net(policy, size_usd=size_usd, tokens=tokens,
                         sell_price=sell_price, sol_usd=sol_usd,
                         first_transfer=first_transfer)
    if stats["net_usdt"] <= policy.min_net_profit_usdt:
        return False, (f"净利 {stats['net_usdt']:.4f} ≤ "
                       f"{policy.min_net_profit_usdt}"), stats
    return True, (f"净利 {stats['net_usdt']:.4f} @ {size_usd}U"
                  f"（毛 {stats['gross_usdt']:.4f}）"), stats


# ---------------------------------------------------------------------------
# 资金：回款与两链告警
# ---------------------------------------------------------------------------
def treasury_decision(policy: AutoPolicy, *, kraken_usd: Decimal,
                      onchain_usdt: Decimal, xlayer_usdg: Decimal | None = None
                      ) -> dict:
    """链上不足时从 Kraken 提币补到目标；两链缺钱只告警，不自动调配。"""
    need = policy.solana_target_usdt - onchain_usdt
    if need <= 0:
        return {"action": "keep", "amount": Decimal(0),
                "reason": f"链上 {onchain_usdt} ≥ 目标 {policy.solana_target_usdt}"}
    if onchain_usdt >= policy.solana_floor_usdt:
        return {"action": "keep", "amount": Decimal(0),
                "reason": (f"链上 {onchain_usdt} 在 floor "
                           f"{policy.solana_floor_usdt} 之上，先不提")}
    if kraken_usd < policy.kraken_min_before_withdraw:
        return {"action": "wait", "amount": Decimal(0),
                "reason": (f"Kraken {kraken_usd} < {policy.kraken_min_before_withdraw}，"
                           "攒够再提（提币费摊不薄）")}
    amount = min(policy.solana_target_usdt, kraken_usd - policy.withdraw_fee_usdt)
    amount = amount.quantize(Decimal("0.01"))
    if amount <= 0:
        return {"action": "wait", "amount": Decimal(0), "reason": "可提数量为 0"}
    return {
        "action": "withdraw",
        "amount": amount,
        "asset": policy.asset,
        "key": policy.withdraw_key,
        "fee": policy.withdraw_fee_usdt,
        "reason": (f"链上 {onchain_usdt} < floor {policy.solana_floor_usdt}，"
                   f"从 Kraken 提 {amount} {policy.asset} 回 Solana"),
    }


def funding_alerts(policy: AutoPolicy, *, solana_usdt: Decimal | None,
                   xlayer_usdg: Decimal | None, sol_lamports: int | None) -> list[dict]:
    """两链资金与 gas 的告警（只报告，不搬钱）。"""
    out: list[dict] = []
    if solana_usdt is not None and solana_usdt < policy.solana_floor_usdt:
        out.append({
            "key": "solana_funding_low", "audience": "alert",
            "text": (f"⚠️ Solana 钱包只剩 {solana_usdt} USDT/USDC，低于 floor "
                     f"{policy.solana_floor_usdt}；自动链已跳过买入，"
                     "请从 Kraken 提币或补钱。"),
        })
    if xlayer_usdg is not None and xlayer_usdg < policy.xlayer_floor_usdg:
        out.append({
            "key": "xlayer_funding_low", "audience": "alert",
            "text": (f"⚠️ X Layer 钱包只有 {xlayer_usdg} USDG，低于 floor "
                     f"{policy.xlayer_floor_usdg}；X Layer 策略会缺弹药"
                     "（本阶段不自动执行，仅供你决定补哪条链）。"),
        })
    if sol_lamports is not None and Decimal(sol_lamports) / Decimal(10 ** 9) < policy.sol_min_alert:
        out.append({
            "key": "sol_gas_low", "audience": "alert",
            "text": (f"⚠️ Solana 钱包 SOL 只剩 "
                     f"{Decimal(sol_lamports) / Decimal(10 ** 9):.6f}，低于 "
                     f"{policy.sol_min_alert}；买入/转账会失败，请补 gas。"),
        })
    return out


class AlertTracker:
    """同类告警每天只发一次（避免刷屏）。状态存进自动链 state 文件。"""

    def __init__(self, sent: dict | None = None):
        self.sent = dict(sent or {})

    def should_send(self, key: str, day: str) -> bool:
        return self.sent.get(key) != day

    def mark(self, key: str, day: str) -> None:
        self.sent[key] = day

    def to_json(self) -> dict:
        return dict(self.sent)


# ---------------------------------------------------------------------------
# 账本
# ---------------------------------------------------------------------------
def read_ledger(path: Path | str = LEDGER_PATH, limit: int | None = None) -> list[dict]:
    rows: list[dict] = []
    try:
        with Path(path).open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
    except FileNotFoundError:
        return []
    return rows[-limit:] if limit else rows


def append_ledger(row: dict, path: Path | str = LEDGER_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def opened_rows(rows: list[dict], day: str) -> list[dict]:
    """当天真正开了仓的行（买到了币才算，被闸门拒的不算）。"""
    out = []
    for row in rows:
        if str(row.get("day") or "") != day:
            continue
        if row.get("bought_at_ms"):
            out.append(row)
    return out


def loss_streak(rows: list[dict], *, only_auto: bool = True) -> int:
    """从最近往回数：连续几笔已完成的交易是净亏。

    只数**自动链自己**跑出来的行（带 `mode`）；历史手工单不算数，
    否则一启动就背着几天前的旧亏损。
    """
    streak = 0
    for row in reversed(rows):
        if only_auto and not row.get("mode"):
            continue
        if row.get("net_usdt") in (None, ""):
            continue                       # 没成交/没算净利的不算一笔
        try:
            net = Decimal(str(row["net_usdt"]))
        except Exception:  # noqa: BLE001
            continue
        if net < 0:
            streak += 1
        else:
            break
    return streak


def build_ledger_row(*, symbol: str, network: str, size_usd: Decimal, state: str,
                     reason: str, now: float | None = None, **extra) -> dict:
    """账本一行。除了结果，还记 txid、费用明细、入账耗时、是否回款。"""
    now = now if now is not None else time.time()
    row = {
        "day": day_key(now),
        "ts_ms": int(now * 1000),
        "symbol": symbol,
        "network": network,
        "size_usd": str(size_usd),
        "state": state,
        "reason": reason,
        "tokens": None,
        "buy_price": None,
        "sell_price": None,
        "gross_usdt": None,
        "net_usdt": None,
        "txids": {},
        "fees": {},
        "bought_at_ms": None,
        "credited_at_ms": None,
        "sold_at_ms": None,
        "credit_wait_s": None,
        "refundable_rent_usdt": None,
        "withdrew_usdt": None,
        "mode": None,
        "notes": [],
    }
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# 转账是否「首次」（决定用哪档转账费）
# ---------------------------------------------------------------------------
def load_transfer_seen(path: Path | str = TRANSFER_SEEN) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except Exception:  # noqa: BLE001
        return {}


def transfer_is_first(symbol: str, seen: dict) -> bool:
    return not bool((seen or {}).get(symbol))


def mark_transfer_seen(symbol: str, seen: dict, path: Path | str = TRANSFER_SEEN) -> dict:
    seen = dict(seen or {})
    seen[symbol] = day_key()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(seen, ensure_ascii=False, indent=1))
    tmp.replace(path)
    return seen


# ---------------------------------------------------------------------------
# 状态文件（看板读它渲染「自动链」区块）
# ---------------------------------------------------------------------------
@dataclass
class AutoState:
    updated_ms: int = 0
    day: str = ""
    phase: str = "idle"          # idle|scanning|reviewing|buying|waiting_credit|selling|paused
    mode: str = MODE_OFF
    running: bool = False
    pause_reason: str = ""
    trades_today: int = 0
    opened_quote_usdt: str = "0"
    loss_streak: int = 0
    balances: dict = field(default_factory=dict)
    positions: dict = field(default_factory=dict)
    in_flight: dict | None = None
    last_review: dict | None = None
    last_cycle: dict | None = None
    alerts_sent: dict = field(default_factory=dict)
    note: str = ""

    def to_json(self) -> dict:
        data = asdict(self)
        data["trades_today"] = int(self.trades_today)
        return data

    @classmethod
    def from_json(cls, payload: dict | None) -> "AutoState":
        payload = dict(payload or {})
        opened = payload.get("opened_quote_usdt")
        known = {f for f in cls.__dataclass_fields__}          # noqa: SLF001
        payload = {k: v for k, v in payload.items() if k in known}
        payload.pop("opened_quote_usdt", None)
        state = cls(**payload)
        state.opened_quote_usdt = str(opened if opened is not None else "0")
        return state

    def refresh_day(self, day: str, rows: list[dict]) -> None:
        """换天：清零当日计数，按账本重算连亏。"""
        self.day = day
        today = opened_rows(rows, day)
        self.trades_today = len(today)
        self.opened_quote_usdt = str(sum((_d(r.get("size_usd")) for r in today),
                                         Decimal(0)))
        self.loss_streak = loss_streak(rows)


def write_state(state: AutoState, path: Path | str = STATE_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state.to_json(), ensure_ascii=False, indent=1))
    tmp.replace(path)


def read_state(path: Path | str = STATE_PATH) -> AutoState:
    try:
        return AutoState.from_json(json.loads(Path(path).read_text()))
    except Exception:  # noqa: BLE001
        return AutoState()


# ---------------------------------------------------------------------------
# 告警落盘（看板与 heartbeat 都能读）
# ---------------------------------------------------------------------------
def write_alert(alert: dict, *, now: float | None = None,
                directory: Path | str = ALERT_DIR) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(now if now is not None else time.time())
    name = f"{stamp.strftime('%Y%m%d-%H%M%S')}-{alert.get('key', 'alert')}.json"
    path = directory / name
    path.write_text(json.dumps(alert, ensure_ascii=False, indent=1))
    return path
