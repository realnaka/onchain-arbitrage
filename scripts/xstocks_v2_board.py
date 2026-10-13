#!/usr/bin/env python3
"""Read-only board for xStocks v2 on port 8771.

    scripts/xstocks_v2_board.py --port 8771

It reads ``data-v2/*.json`` only. It never quotes, signs or writes anything
except its own log.

The page answers, in this order:

1. Is anything actually running? (heartbeat, lesson 6.3)
2. Which path families exist, and how many rows each has?
3. Which rows are closest to the gate, and what stops each one?
4. What did the paper runner decide, and how many were ``false_t0``?
"""
from __future__ import annotations

import argparse
import html
import json
import sys
import time
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.xstocks_v2 import cycle as cycle_core   # noqa: E402
from arb_monitor.xstocks_v2 import health, ledger, realized  # noqa: E402
from arb_monitor.xstocks_v2 import live as live_core     # noqa: E402
from arb_monitor.xstocks_v2 import probe as probe_core   # noqa: E402
from arb_monitor.xstocks_v2.policy import V2Policy       # noqa: E402
from arb_monitor.xstocks_v2.store import read_json  # noqa: E402

DATA = ROOT / "data-v2"
SCAN = DATA / "xstocks_v2_scan.json"
PAPER = DATA / "xstocks_v2_paper.json"
TIMING = DATA / "xstocks_v2_full_scan_timing.json"
CYCLES = DATA / "xstocks_v2_cycles.jsonl"
BALANCES = DATA / "xstocks_v2_balances.json"
LOCK = DATA / "xstocks_v2_cycle.lock"
DEFAULT_INTERVAL_S = 180.0

FAMILY_LABEL = {
    "chain_to_kraken": "链 → Kraken",
    "kraken_to_chain": "Kraken → 链",
    "dex_to_dex": "同链两口池",
    "chain_to_chain": "链A → 链B（跨链）",
    "chain_via_bridge_to_kraken": "链 → 桥 → Kraken",
    "kraken_via_bridge_to_chain": "Kraken → 桥 → 链",
    "xchange_vs_pool": "xChange ↔ 池子",
}
# Spelled out so no reader has to infer which leg is which.
FAMILY_DIRECTION = {
    "chain_to_kraken": "在链上买入，卖进 Kraken 的买一（bid）",
    "kraken_to_chain": "在 Kraken 按卖一（ask）买入，在链上池子里卖出",
    "dex_to_dex": "同一链的两口不同池子，A 池买入、B 池卖出",
    "chain_to_chain": "在 A 链买入，把币桥到 B 链，在 B 链卖出",
    "chain_via_bridge_to_kraken": "在池子链买入，桥到 Kraken 收的链，充进去卖出",
    "kraken_via_bridge_to_chain": "在 Kraken 买入，提到 Kraken 收的链，再桥到池子链卖出",
    "xchange_vs_pool": "同一条链上，池子和 xChange 对打。两笔交易，中间有裸仓，先只监控",
}

# Reason -> how the operator should read it. Only data-quality reasons are
# untrusted; economic ones are simply "not now" (lessons section C).
REASON_LABEL = {
    "phase_scan": "阶段 0 只读，未放开执行",
    "stale_or_skew": "两侧报价差超过 10 秒（不可信）",
    "price_outlier": "报价偏离参考价太多（坏数据，不是机会）",
    # Retired as a gate on 2026-10-08 (operator: 以利润为准). An old snapshot
    # can still carry it, so the label stays and says what it now means.
    "below_depth": "池子薄 / 同池往返损耗大 / 报价冲击大（已降级为证据，不再拦）",
    "kraken_depth_short": "Kraken 盘口吃不下这个票面（走完全部档位仍不够）",
    "hook_not_green": "v4 池不是 green",
    "no_executor_chain": "这条链没有接执行器：实盘只会在这条家族已经接上的链上开火",
    "no_lane": "发行方的桥没有这条边，不能签名",
    "no_selector": "这条边在登记表上，但还没有读到链选择子",
    "no_adapter": "构造器有了，这条链没有能签名的钱包",
    "no_rail": "Kraken 没有这条链的充值或提币",
    "unmeasured_route": "这条桥没有对面到账样本",
    "no_deposit_method": "Kraken 没有这条链的充值方法",
    "no_address_book": "Kraken 地址簿没有这个币",
    "no_withdraw_method": "Kraken 没有这个币在这条链上的提币方式（登记也没用）",
    "below_net": "提币费比价差还大：这个票面做不出净利",
    "atomic_route_unproven": "同链两口池必须一笔交易，现在没有原子路线证明",
    "cost_unknown": "这条桥的费用没有实测（未知不是 0）",
    "bridge_unmeasured_symbol": "这个币在这条桥上还没有到账样本（价差够就自动小样本）",
    "atomic_required": "必须一笔原子交易（阶段 2）",
    "cost_unknown": "桥费/提币费还没有实测值",
    "no_dest_pool": "目的侧没有可成交的池",
    "chain_out_of_phase": "这条链不在第一版范围",
    "false_t0": "扫描时过门槛，复价后不过",
    "below_threshold": "价差没过门槛（门槛随开市/休市变）",
    "xchange_closed": "xChange 这轮不开市或停牌",
    "xchange_unconfigured": "没有 xChange API key，这轮没有询",
    "two_tx_inventory": "同链池子和 xChange 是两笔交易，中间有裸仓",
    "below_min:xchange": "票面低于 xChange 最小额",
    "above_max:xchange": "票面高于 xChange 最大额",
    "xchange_expired": "xChange 软报价已经过期",
    "xchange_unauthorized": "xChange API key 被拒绝",
    "xchange_rate_limited": "xChange 限流，这轮没拿到价",
    "xchange_price_unavailable": "xChange 上游暂时没有价格",
}
UNTRUSTED = {"stale_or_skew", "price_outlier"}
# Blockers that do not change the observation itself. Anything else means the
# row is not a believable spread, so it must not set a family's headline number
# (a 29 USD pool once printed +4697 bps that way).
INFORMATIONAL = {"phase_scan", "chain_out_of_phase"}


def _d(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _fmt(value, digits: int = 2) -> str:
    number = _d(value)
    if number is None:
        return "–"
    return f"{number:,.{digits}f}"


#: Which of the three ``below_depth`` signals fired. The board used to print one
#: label for all of them, so five x-layer pools 313k-433k deep read as "深度不够"
#: when the real cause was a one-sided book (measured 2026-10-08).
DEAD_DETAIL_LABEL = {
    "pool_too_thin": "池深 × 20% 吃不下最小票面",
    "same_pool_round_trip": "同一口池买完再卖，损耗超上限",
    "price_impact": "报价自报的价格冲击超上限",
}


def _dead_detail(detail) -> str:
    name = str(detail or "")
    if not name:
        return "未记录（旧条目）"
    return DEAD_DETAIL_LABEL.get(name, name)


def _wait_label(item, *, absolute: bool = False) -> str:
    """How long a Solana rail has been waiting for a quote slot.

    ``absolute`` prints the moment of the last slot instead, so the reader can
    tell "waited 40 minutes" from "waited 40 minutes but only ever once".
    """
    item = item if isinstance(item, dict) else {}
    last = item.get("last_slot_ms")
    if item.get("never_quoted") or not isinstance(last, (int, float)):
        # The timestamp column already reads 「从未」; a duration would repeat it.
        return "从未" if absolute else "—"
    if absolute:
        return time.strftime("%m-%d %H:%M", time.localtime(last / 1000))
    seconds = item.get("waited_s")
    seconds = float(seconds) if isinstance(seconds, (int, float)) else 0.0
    if seconds < 90:
        return "不到 2 分钟"
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分钟"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f} 小时"
    return f"{seconds / 86400:.1f} 天"


#: Amounts below this are chain dust, not a position. They are still shown --
#: hidden money is a worse bug than a noisy row -- but folded away.
DUST = Decimal("1e-9")
#: Assets the board may add together into one line. All are USD pegs.
CASH_ASSETS = {"USDT", "USDC", "USDG", "ZUSD", "USDT0", "DAI"}


def _fmt_amount(value) -> str | None:
    """A wallet amount at a width a human can scan.

    The first version printed every balance with 8 decimals, so a dust leftover
    and a real position looked equally important. Trailing zeros go away; a
    number below one millionth of a unit keeps scientific notation.
    """
    number = _d(value)
    if number is None:
        return None
    if number == 0:
        return "0"
    magnitude = abs(number)
    if magnitude >= 1000:
        return f"{number:,.2f}"
    if magnitude >= 1:
        return f"{number:,.6f}".rstrip("0").rstrip(".")
    if magnitude >= Decimal("0.000001"):
        return f"{number:.8f}".rstrip("0").rstrip(".")
    return f"{number:.3E}"


def _short_account(account: str) -> str:
    """``0xabab…ababab`` -- enough of an address to tell two wallets apart."""
    text = str(account or "")
    if len(text) <= 16:
        return text
    return f"{text[:6]}…{text[-6:]}"


def _cash_asset(asset: str) -> bool:
    """USD pegs are the only balances that may be summed without a price.

    Everything else (xStocks, OKB, SOL) needs a quote to become a number, and a
    made-up total is worse than no total.
    """
    return str(asset or "").strip().upper() in CASH_ASSETS


def _probe_ready(row: dict) -> bool:
    """A probe we may actually run: every input known, and a spread worth it.

    This is ``probe.verdict`` -- the **same** function the live gate calls.
    Until 2026-10-09 the board checked only ``waiting_on`` and the spread while
    the gate also checked the row's blockers, so a row could print "能跑" here
    and be refused there (operator: 避免「板面说能跑、实盘拒」). A row with
    ``below_net`` is now refused by both: the board reads a file, and it must
    not offer to spend money on a row whose own number says the trade does not
    pay.
    """
    return probe_core.ready(row)


def _probe_sample(row: dict) -> bool:
    """The sample stage: 缺样本不是拒绝理由 (operator 2026-10-09).

    Same function the live gate calls (``probe.sample_verdict``), so the board
    cannot offer a probe the runner would refuse.
    """
    return probe_core.sample_ready(row)
def collect(*, data_dir: Path = DATA, interval_s: float = DEFAULT_INTERVAL_S,
            now_ms: int | None = None) -> dict:
    now_ms = now_ms or int(time.time() * 1000)
    scan = read_json(data_dir / SCAN.name, {}) or {}
    paper_state = read_json(data_dir / PAPER.name, {}) or {}
    timing = read_json(data_dir / TIMING.name, {}) or {}
    cycles = ledger.recent(data_dir / CYCLES.name, limit=400)
    # The money number reads the *whole* ledger: a realized total over the last
    # 400 rows would change as paper rounds scroll past it.
    realized_total = realized.totals(ledger.recent(data_dir / CYCLES.name,
                                                   limit=None))
    balances_snapshot = read_json(data_dir / BALANCES.name, {}) or {}

    rows = scan.get("rows") or []
    counts: dict[str, dict] = {}
    for row in rows:
        family = str(row.get("family") or "?")
        item = counts.setdefault(family, {"rows": 0, "blockers": {}, "best": None})
        item["rows"] += 1
        for blocker in row.get("blockers") or ["ok"]:
            item["blockers"][blocker] = item["blockers"].get(blocker, 0) + 1
        edge = _d(row.get("edge_t0_bps"))
        believable = not (set(row.get("blockers") or []) - INFORMATIONAL)
        if believable and edge is not None and (item["best"] is None or edge > item["best"]):
            item["best"] = edge
            item["best_row"] = row
        if believable:
            item["believable"] = item.get("believable", 0) + 1

    ranked = sorted(
        [row for row in rows if _d(row.get("edge_t0_bps")) is not None],
        key=lambda row: _d(row["edge_t0_bps"]), reverse=True)
    clean = [row for row in ranked
             if not (set(row.get("blockers") or []) - INFORMATIONAL)]
    traps = [row for row in ranked
             if set(row.get("blockers") or []) - INFORMATIONAL]

    states: dict[str, int] = {}
    for entry in cycles:
        key = str(entry.get("state") or "?")
        states[key] = states.get(key, 0) + 1

    report = health.check(
        paths={"scan": data_dir / SCAN.name, "paper": data_dir / PAPER.name},
        now_ms=now_ms, interval_s=interval_s)
    # The probe list is one list with two stages (operator 2026-10-09). The
    # budget is the same counter the runner charges, so the board shows the day
    # as the gate sees it -- and says which cap stopped a row instead of going
    # quiet. A broken policy file must not take the board down.
    try:
        probe_policy = V2Policy.load()
    except Exception:  # noqa: BLE001 - the board renders what it can read
        probe_policy = None
    probe_budget = probe_core.budget_load(data_dir / probe_core.BUDGET_NAME)
    probe_candidates: list[dict] = []
    probe_samples: list[dict] = []
    for row in ranked:
        trade, sample = _probe_ready(row), _probe_sample(row)
        if not (trade or sample):
            continue
        size = live_core.probe_size_usdt(row)
        item = {**row, "stage": "trade" if trade else "sample",
                "size_usdt": None if size is None else format(size, "f"),
                "budget_ok": None, "budget_reason": ""}
        if probe_policy is not None:
            ok, why, _state = probe_core.budget_decide(
                state=probe_budget, now_ms=now_ms, size_usdt=item["size_usdt"],
                max_per_day=probe_policy.probe_max_per_day,
                max_usdt_per_probe=probe_policy.probe_max_usdt)
            item["budget_ok"], item["budget_reason"] = bool(ok), why
        (probe_candidates if trade else probe_samples).append(item)
    by_edge = lambda item: _d(item.get("edge_t0_bps")) or Decimal("-1e9")
    probe_candidates.sort(key=by_edge, reverse=True)
    probe_samples.sort(key=by_edge, reverse=True)
    # Per-chain coverage. Measured 2026-10-09: BSC was quoted every round with
    # zero errors and the board still never mentioned it -- only rows that pass
    # a "clean" filter are printed anywhere, so a chain whose rows are all
    # blocked is invisible. A chain must never be able to disappear silently.
    chain_coverage: list[dict] = []
    for chain in sorted({str(row.get("chain") or "") for row in ranked if row.get("chain")}):
        subset = [row for row in ranked if str(row.get("chain")) == chain]
        blockers: dict[str, int] = {}
        for row in subset:
            for name in row.get("blockers") or []:
                blockers[str(name)] = blockers.get(str(name), 0) + 1
        can_run, note = live_core.executor_chain(live_core.CHAIN_TO_KRAKEN, chain)
        chain_coverage.append({
            "chain": chain,
            "rows": len(subset),
            "symbols": sorted({str(row.get("symbol")) for row in subset})[:6],
            "best_edge_bps": max(by_edge(row) for row in subset),
            "executable": bool(can_run),
            "executor_note": note,
            "top_blockers": sorted(blockers.items(), key=lambda kv: -kv[1])[:4],
        })
    chain_coverage.sort(key=lambda item: item["best_edge_bps"], reverse=True)
    return {
        "now_ms": now_ms,
        "balances": balances_snapshot,
        "inflight": cycle_core.status(data_dir / LOCK.name, now_ms=now_ms),
        "health": report,
        "health_alert": health.alert_text(report),
        "scan": {
            "generated_ms": scan.get("generated_ms"),
            "age_s": (None if not scan.get("generated_ms")
                      else round((now_ms - scan["generated_ms"]) / 1000, 1)),
            "phase": scan.get("phase"),
            "session": scan.get("session"),
            "round_seconds": scan.get("round_seconds"),
            "counters": scan.get("counters") or {},
            "universe": scan.get("universe") or {},
            "threshold_bps": scan.get("threshold_bps"),
            "min_net_profit_usdt": scan.get("min_net_profit_usdt"),
            "dead_pools": scan.get("dead_pools") or {},
            "gas_prices": scan.get("gas_prices") or {},
            # The per-chain cost of one on-chain action. It was missing from
            # this projection, so the paragraph that prints it (X Layer *and*
            # Ethereum since 2026-10-09) never rendered at all.
            "gas_lines": scan.get("gas_lines") or {},
            # A1/A3: what the round refreshed, and the fee it read off the chain
            # (the receipt file is the fallback + the cross-check now).
            "bridge_fees": scan.get("bridge_fees") or {},
        },
        "timing": timing,
        "address_requests": scan.get("address_requests") or [],
        "address_requests_deferred": scan.get("address_requests_deferred") or [],
        "rail_coverage": scan.get("rail_coverage") or {},
        "rotation": scan.get("rotation") or {},
        "families": [
            {"family": family, "label": FAMILY_LABEL.get(family, family),
             "direction": FAMILY_DIRECTION.get(family, ""),
             "rows": item["rows"], "believable": item.get("believable", 0),
             "best_edge_bps": item["best"],
             "best_row": item.get("best_row"),
             "blockers": sorted(item["blockers"].items(), key=lambda kv: -kv[1])[:4]}
            for family, item in sorted(counts.items())],
        "clean": clean[:20],
        "top": clean[:20],
        "traps": traps[:8],
        # Real spreads on chains no executor reaches (operator 2026-10-09:
        # 「SPCXx eth uniswap … kraken … 76.26 这个怎么不执行」). Kept in their
        # own list so the question "why is nothing happening on a row this
        # good" is answered on the page, not in a hand-off note.
        # Only a positive edge belongs in "there is a spread we cannot trade".
        # A negative edge is not a missed trade; showing it here (and painting
        # the number green) is how -8000 bps read as an opportunity.
        "no_executor": [row for row in ranked
                        if live_core.NO_EXECUTOR_CHAIN in (row.get("blockers") or [])
                        and (_d(row.get("edge_t0_bps")) or Decimal(0)) > 0][:5],
        # Rows whose only missing piece is a route sample, with a spread that
        # clears the bar. These are the ones the runner may buy the minimum of
        # (operator, 2026-10-07): the arrival is what unlocks the full size, so
        # there is no reason to pre-sample a coin on a bridge nobody wants.
        # "ready" can be run right now; "waiting" is the same row missing a
        # number (usually the bridge fee), which is shown rather than hidden.
        "probe_candidates": probe_candidates[:20],
        # The sample stage (operator 2026-10-09: 缺样本不是拒绝理由). Separate
        # list on purpose: a trade-shaped probe is the first leg of a trade we
        # would make anyway, a sample probe is money spent to buy information.
        "probe_samples": probe_samples[:20],
        "probe_budget": {
            "day": probe_budget.get("day"),
            "count": probe_budget.get("count"),
            "spent_usdt": probe_budget.get("spent_usdt"),
            "max_per_day": (None if probe_policy is None
                            else probe_policy.probe_max_per_day),
            "max_usdt_per_probe": (None if probe_policy is None
                                   else format(probe_policy.probe_max_usdt, "f")),
        },
        "chain_coverage": chain_coverage,
        # "Waiting" means one thing: the plan is real and it is missing a
        # number (today, the bridge fee). A row that carries a plan but is
        # vetoed for a different reason -- ``below_net`` above all -- is not
        # waiting for anything, and listing it here is how the board used to
        # promise a probe the gate would refuse (A4, 2026-10-09).
        "probe_waiting": sorted(
            [row for row in ranked
             if probe_core.verdict(row).get("reason") == "probe_inputs_unknown"],
            key=lambda row: _d(row.get("edge_t0_bps")), reverse=True)[:20],
        # Stage 2 gets its own list: these rows are deliberately never in
        # ``clean``, because a same-chain two-pool row may only be executed as
        # ONE transaction and nothing can prove that yet.
        "same_chain": sorted(
            [row for row in ranked if row.get("family") == "dex_to_dex"],
            key=lambda row: _d(row.get("edge_t0_bps")), reverse=True)[:20],
        "paper": {
            "updated_ms": paper_state.get("updated_ms"),
            "phase": paper_state.get("phase"),
            "auto_orders": paper_state.get("auto_orders"),
            "broadcast": paper_state.get("broadcast", False),
            "gate": paper_state.get("gate") or {},
            "candidates": paper_state.get("candidates"),
            "near": paper_state.get("near"),
            "states": paper_state.get("states") or {},
            "decisions": paper_state.get("decisions") or [],
        },
        "cycles": {"total": len(cycles), "by_state": states},
        "realized": realized_total,
        "open_positions": [entry.get("symbol") for entry in
                           ledger.open_positions(data_dir / CYCLES.name)],
        # The two kinds of leftover inventory need different words: ``probed``
        # is held on purpose (operator 2026-10-07: 留着当样本库存) and the next
        # trade of that symbol carries it; ``stopped``/``unknown`` is a real
        # position that needs a human.
        "position_details": [
            {"symbol": entry.get("symbol"),
             "state": entry.get("state"),
             "held_at": ledger.effective_held_at(entry),
             "reason": entry.get("reason"),
             "probe": entry.get("state") == ledger.PROBED}
            for entry in ledger.open_positions(data_dir / CYCLES.name)],
        "halt": (data_dir / "xstocks_v2_HALT").exists(),
        # The alert watcher's own state. Read, never written: the board is a
        # reader, and a page that cannot read it says so instead of guessing.
        "investigations": _investigations_snapshot(data_dir),
        # Operator 2026-10-10: a refusal that only says "funding_short" is a
        # silent one. This is the same answer with the chain and the number on
        # it, taken from the rows themselves.
        "funding_gap": _funding_gaps(cycles),
    }


def _investigations_snapshot(data_dir: Path) -> dict:
    """``alert_state.json`` + ``state.json`` from ``data-v2/investigations``."""
    out: dict = {"alert_state": {}, "state": {}, "present": False}
    for key, name in (("alert_state", "alert_state.json"),
                      ("state", "state.json")):
        try:
            out[key] = json.loads(
                (data_dir / "investigations" / name).read_text())
            out["present"] = True
        except (OSError, ValueError):
            out[key] = {}
    return out


def _funding_gaps(cycles: list[dict]) -> list[dict]:
    """Which chain is short of how much, from the rows that were denied for it.

    Operator 2026-10-10: ``funding_short`` must not be one word. Measured the
    day before: 4,882 of the day's 5,703 ledger rows ended on it, and the board
    said nothing about which chain, which ticket, or how far off the wallet was
    -- so "the machine is busy" and "the machine is 3 USDT away from a trade"
    looked identical.

    The numbers come from whoever denied the row (``live.funding_gap``); the
    board only groups them. It never re-derives money, and a row written before
    2026-10-10 (no number) is counted but kept out of the arithmetic.
    """
    by_chain: dict[str, dict] = {}
    for entry in cycles:
        gap = (entry.get("extra") or {}).get("funding_gap") or {}
        if not isinstance(gap, dict) or not gap:
            continue
        chain = str(entry.get("chain") or "?")
        item = by_chain.setdefault(chain, {
            "chain": chain, "rows": 0, "priced_rows": 0, "short_usdt": None,
            "need_usdt": None, "have_usdc": None, "have_usdt": None,
            "symbol": ""})
        item["rows"] += 1
        short = _d(gap.get("short_usdt"))
        if short is None:
            continue
        item["priced_rows"] += 1
        # The smallest gap is the interesting one: it is the row a small
        # top-up would actually unlock.
        if item["short_usdt"] is None or short < item["short_usdt"]:
            item["short_usdt"] = short
            item["need_usdt"] = _d(gap.get("need_usdt"))
            item["have_usdc"] = _d(gap.get("have_usdc"))
            item["have_usdt"] = _d(gap.get("have_usdt"))
            item["symbol"] = str(entry.get("symbol") or "")
    return sorted(by_chain.values(),
                  key=lambda item: (item["short_usdt"] is None,
                                    item["short_usdt"] or Decimal(0)))


def _funding_gap_section(gaps: list[dict]) -> str:
    """The gap table, or nothing at all when no row names one.

    Operator 2026-10-10: 「让板面把沉默的原因说出来」. A refusal the board
    renders as one word is a silent one, so this prints the chain, the ticket
    and the missing amount -- taken from the denying row, never recomputed.
    """
    if not gaps:
        return ""
    out = ["<h3>资金缺口（哪些行只差钱，不缺价差）</h3>",
           "<p class='small'>这些行的价差与数据质量都过，唯一挡住它们的是那条链上的"
           "钱包余额。「还缺」= 要在那条链上多出多少 USDT，这一行才成交得了"
           "（取最近 400 行里缺口最小的那一笔）。</p>",
           "<table><tr><th>链</th><th>卡住的行数</th><th>票面 USDT</th>"
           "<th>现在有（USDC / USDT）</th><th>还缺 USDT</th>"
           "<th>最接近的一行</th></tr>"]
    for item in gaps:
        have = "–"
        if item.get("have_usdc") is not None or item.get("have_usdt") is not None:
            wallet = str(item.get("wallet_chain") or "")
            have = ((f"{wallet} " if wallet else "")
                    + f"{_fmt(item.get('have_usdc'), 4)} / "
                    f"{_fmt(item.get('have_usdt'), 4)}")
        note = f"{item.get('rows')} 行"
        if item.get("priced_rows") != item.get("rows"):
            note += f"（其中 {item.get('priced_rows')} 行有数字）"
        out.append(
            f"<tr><td><code>{html.escape(str(item.get('chain')))}</code></td>"
            f"<td>{html.escape(note)}</td>"
            f"<td>{_fmt(item.get('need_usdt'), 4)}</td>"
            f"<td class='small'>{html.escape(have)}</td>"
            f"<td class='warn'>{_fmt(item.get('short_usdt'), 4)}</td>"
            f"<td class='small'>{html.escape(str(item.get('symbol') or '–'))}</td>"
            "</tr>")
    out.append("</table>")
    return "".join(out)


def _investigations_section(snap: dict, now_ms: int) -> str:
    """What the watcher saw, and what Codex said about it.

    Operator 2026-10-09: alerts should not wait for a periodic patrol. This is
    the visible half of that: the alert list the watcher used, the investigations
    it started, and the conclusion of the last one that finished.
    """
    if not snap.get("present"):
        return ("<h2>报警自检（Codex）</h2><p class='small'>还没有跑过。"
                "启动：<code>scripts/xstocks-v2-ctl.sh watch</code></p>")
    alert_state = snap.get("alert_state") or {}
    state = snap.get("state") or {}
    counts = (alert_state.get("counts") or {})
    by_kind = counts.get("by_kind") or {}
    age = ""
    if alert_state.get("updated_ms"):
        age = f"，{max(0, (now_ms - int(alert_state['updated_ms'])) / 1000):.0f} s 前看过"
    parts = ["<h2>报警自检（Codex）</h2>"]
    parts.append(f"<p class='small'>报警 {counts.get('total', 0)} 条"
                 f"（会停机 {counts.get('stops', 0)} 条）{html.escape(age)}；"
                 f"今日已自动发起 {state.get('launched_today', 0)}/"
                 f"{alert_state.get('max_per_day', '?')} 次。"
                 "只读排查：不动开关、不下单。</p>")
    if by_kind:
        parts.append("<p class='small'>" + " · ".join(
            f"{html.escape(str(k))} {v}" for k, v in sorted(by_kind.items()))
            + "</p>")
    running = state.get("running") or []
    if running:
        rows = []
        for item in running:
            spent = max(0, (now_ms - int(item.get("started_ms") or now_ms)) / 1000)
            rows.append("<tr><td>" + html.escape(str(item.get("kind"))) + "</td><td>"
                        + html.escape(str(item.get("symbol") or "—")) + "</td><td>"
                        + f"{spent:.0f} s</td><td class='small'>"
                        + html.escape(str(item.get("report") or "")) + "</td></tr>")
        parts.append("<p class='small'>正在自检：</p><table>"
                     "<tr><th>报警</th><th>标的</th><th>已跑</th><th>报告</th></tr>"
                     + "".join(rows) + "</table>")
    done = state.get("done") or []
    if done:
        last = done[0]
        answer = str(last.get("summary") or "").strip() or "（报告为空，见 log）"
        parts.append(
            "<p class='small'>最近一次完成："
            + html.escape(f"{last.get('kind')} {last.get('symbol') or ''} "
                          f"（{last.get('outcome')}）") + "</p>")
        parts.append("<pre class='small' style='white-space:pre-wrap;margin:4px 0'>"
                     + html.escape(answer[:1200]) + "</pre>")
        parts.append("<p class='small'>报告：" + html.escape(str(last.get("report") or ""))
                     + "</p>")
    if alert_state.get("skipped"):
        parts.append("<p class='small'>本轮跳过：" + "、".join(
            html.escape(f"{s.get('kind')}({s.get('reason')})")
            for s in alert_state["skipped"][:8]) + "</p>")
    return "".join(parts)


def _balances_section(snapshot: dict, now_ms: int) -> str:
    """Account / chain balances, one aligned row per chain.

    A failed read prints 「读不到」, never 0: an unknown wallet is not an empty
    wallet (lesson A6). Zero and dust rows fold into a ``<details>`` -- the first
    version listed all 27 X Layer rows, 23 of them ``0.00000000``, and buried
    the two rows that mattered.

    Layout (operator 2026-10-09: 「按 A…但颜色、大小、对齐都很乱…宁愿不要那么复杂」):
    one chain per row on a single surface, three fixed columns so every number
    lands on the same vertical line -- 链 / 持仓 / 稳定币合计 / ≈USD. No per-chain
    colour, no chip blocks, no bars: two text tones, mono tabular figures, and
    one amber for 「读不到」. The words and numbers are unchanged; only the
    arrangement is.

    Order (operator 2026-10-09: 「余额多的，排在上面」): biggest balance first --
    the wallet service's USD total when it priced the chain, else the stablecoin
    sum we can read ourselves (Kraken publishes no total), else 0. A chain we
    could not read ranks as 0 and keeps its own 「读不到」; nothing is dropped.

    The numbers come from ``data-v2/xstocks_v2_balances.json``, written by the
    scan/paper round (``balances.refresh``); this handler never calls a venue.
    """
    parts = ["<h2 id='balances'>账户余额（按链 / 账户）</h2>"]
    groups = snapshot.get("groups") or []
    if not groups:
        parts.append("<p class='small bad'>读不到余额快照："
                     + html.escape(str(snapshot.get("error")
                                       or "data-v2/xstocks_v2_balances.json 缺失")) +
                     "（余额为「读不到」，不是 0）</p>")
        return "".join(parts)

    as_of = snapshot.get("as_of_ms")
    age_s = None if not as_of else max(0.0, (now_ms - as_of) / 1000)
    stale = age_s is not None and age_s > 15 * 60
    age_text = ("未知" if age_s is None else
                f"{age_s:.0f} 秒前" if age_s < 90 else f"{age_s / 60:.1f} 分钟前")

    total_cash = Decimal("0")
    unreadable_total = 0
    usd_total = Decimal("0")
    usd_unreadable = 0
    usd_chains = 0
    #: ``(rank, markup)``: the row is sorted at the end by how much money the
    #: chain holds, so the eye starts where the money is.
    ranked_rows: list[tuple[Decimal, str]] = []

    for entry in groups:
        chain = str(entry.get("chain") or "")
        label = str(entry.get("label") or chain)
        account = str(entry.get("account") or "")
        unreadable, zeros, dust, live = [], [], [], []
        for item in entry.get("rows") or []:
            amount = _d(item.get("amount"))
            if amount is None:
                unreadable.append(item)
            elif amount == 0:
                zeros.append(item)
            elif abs(amount) < DUST:
                dust.append(item)
            else:
                live.append(item)
        # Cash first, then the biggest position. The eye lands on the top rows.
        live.sort(key=lambda item: (not _cash_asset(item.get("asset")),
                                    -abs(_d(item.get("amount")) or Decimal(0))))
        unreadable_total += len(unreadable)

        cash_rows = [item for item in live if _cash_asset(item.get("asset"))]
        cash_sum = sum((_d(item.get("amount")) for item in cash_rows),
                       Decimal("0"))
        cash_broken = [item for item in unreadable
                       if _cash_asset(item.get("asset"))]
        if not cash_broken:
            total_cash += cash_sum

        if cash_broken:
            cash_class = "bal-cash-v bad"
            cash_value = "读不到"
        else:
            cash_class = "bal-cash-v"
            cash_value = str(_fmt_amount(cash_sum))
        cash_note = ("+".join(html.escape(str(item.get("asset")))
                              for item in cash_rows) or "没有稳定币")
        if cash_broken:
            cash_note = "｜".join(
                [html.escape(str(item.get("asset"))) for item in cash_broken]
            ) + " 读不到"

        rows_html: list[str] = []
        for item in unreadable + live:
            asset = html.escape(str(item.get("asset")))
            note = str(item.get("note") or "") or (
                "现金" if _cash_asset(item.get("asset")) else "")
            raw = item.get("raw")
            if item.get("amount") is None:
                rows_html.append(
                    "<span class='bal-item bad'>"
                    f"<span class='bal-asset'>{asset}</span>"
                    "<span class='bal-amt'>读不到</span>"
                    f"<span class='bal-note'>{html.escape(str(item.get('error') or '读取失败')[:70])}</span>"
                    "</span>")
                continue
            title = (f" title='raw {html.escape(str(raw))[:28]}'"
                     if raw is not None else "")
            cls = ("bal-item cash" if _cash_asset(item.get("asset"))
                   else "bal-item")
            rows_html.append(
                f"<span class='{cls}'>"
                f"<span class='bal-asset'>{asset}</span>"
                f"<span class='bal-amt'{title}>{_fmt_amount(item.get('amount'))}</span>"
                f"<span class='bal-note'>{html.escape(note)}</span>"
                "</span>")

        fold_bits = []
        if zeros:
            fold_bits.append(f"{len(zeros)} 项为 0")
        if dust:
            fold_bits.append(f"{len(dust)} 项为 dust（&lt; 1e-9）")
        fold = ""
        if fold_bits:
            chips = [html.escape(str(item.get("asset"))) for item in zeros]
            chips += [f"<span class='dust'>{html.escape(str(item.get('asset')))}</span>"
                      for item in dust]
            fold = ("<details class='bal-fold'><summary>其余 "
                    + "，".join(fold_bits) + "</summary>"
                    "<div class='bal-fold-list'>" + " · ".join(chips)
                    + "</div></details>")

        # The wallet service's own USD total for this chain. Labelled as such:
        # it is a venue quote, not one of our prices, and it is the only number
        # on this page that can see an xStock or a gas token at all.
        chain_usd = _d(entry.get("usd_total"))
        if entry.get("source") == "indexer":
            # Only the chains the wallet service prices are expected to carry a
            # USD total; Kraken's card never did and that is not a failure.
            usd_chains += 1
            if chain_usd is None:
                usd_unreadable += 1
            else:
                usd_total += chain_usd
        usd_html = (f"<div class='bal-usd'>≈ {_fmt_amount(chain_usd)} USD</div>"
                    if chain_usd is not None else "")
        note_html = ""
        if str(entry.get("note") or ""):
            note_html = ("<div class='small bal-group-note'>"
                         + html.escape(str(entry["note"])) + "</div>")

        # Ranked at the end, so a chain with more money is read first. Sorting
        # the rendered rows reuses the very numbers this panel prints.
        ranked_rows.append((chain_usd if chain_usd is not None else cash_sum,
                            "<div class='bal-row'>"
            "<div class='bal-who'>"
            f"<span class='bal-name'>{html.escape(label)}</span>"
            f"<button class='bal-acct' data-copy='{html.escape(account, quote=True)}'"
            f" title='{html.escape(account, quote=True)}（点击复制）'>"
            f"{html.escape(_short_account(account))}</button>"
            "</div>"
            "<div class='bal-hold'>" + "".join(rows_html) + fold
            + note_html + "</div>"
            "<div class='bal-cash'>"
            "<div class='bal-cash-k'>稳定币合计"
            f"<span class='bal-cash-sub'>按 1 USD 计</span>"
            f" · {cash_note}</div>"
            f"<div class='{cash_class}'>{cash_value}</div>"
            "</div>"
            + usd_html +
            "</div>"))

    total_note = ""
    if unreadable_total:
        total_note = (f"<b class='bad'>有 {unreadable_total} 项读不到，"
                      "合计可能偏低</b>")
    usd_line = ""
    if usd_chains:
        usd_line = (f"<span>全部持仓 <b>≈ {_fmt_amount(usd_total)} USD</b>"
                    "（指数报价，含 xStocks 与 gas）"
                    + (f"<b class='bad'>另有 {usd_unreadable} 条链读不到，"
                       "合计偏低</b>" if usd_unreadable else "")
                    + "</span>")
    parts.append(
        "<div class='bal-bar'>"
        f"<span class='bal-pill{' warn' if stale else ''}'>快照 {html.escape(age_text)}"
        + ("（可能已过期）" if stale else "") + "</span>"
        f"<span>稳定币合计 <b>{_fmt_amount(total_cash)}</b>"
        "（按 1 USD 计，不含 xStocks / gas）</span>"
        f"{usd_line}"
        f"{total_note}</div>")
    # 「余额多的，排在上面」: biggest first. A stable sort keeps equal-ranked
    # chains in their declared order.
    ordered = [markup for _rank, markup in
               sorted(ranked_rows, key=lambda item: item[0], reverse=True)]
    parts.append("<div class='bal-led'>" + "".join(ordered) + "</div>")
    parts.append("<p class='small'>读不到 ≠ 0：红色的行是读取失败。"
                 "0 与 dust 折叠在每条链的行内。"
                 "每条链右侧的 ≈ USD 与顶部的「全部持仓」来自钱包服务的指数报价"
                 "（它也是唯一能发现我们持有 xStock 与 gas 币的那一路），"
                 "不是我们自己的价格；稳定币合计仍只按 1 USD 相加。"
                 "鼠标悬停数量格可看链上 raw 值。</p>")
    return "".join(parts)


def _kraken_price_cell(row: dict, value, *, side: str) -> str:
    """Print a Kraken price with the basis it was measured on (2026-10-08).

    A best bid is a one-tick price and can be a 2 USD order (MCDx 235.00 on
    2026-10-08); a ``book_vwap`` is what our own ticket really gets. Without the
    label the two look identical on the board, which is how a fake +203 bps row
    read as "阻挡原因：无".
    """
    text = _fmt(value, 4)
    if row.get("sell_venue" if side == "sell" else "buy_venue") != "kraken":
        return text
    basis = row.get("kraken_price_basis")
    label = {"best_bid": "买一档", "best_ask": "卖一档",
             "book_vwap": "按票面走盘口"}.get(str(basis or ""), "")
    if not label:
        return text
    extra = ""
    if basis == "book_vwap" and row.get("book_slip_bps") is not None:
        extra = f" 滑点 {_fmt(row.get('book_slip_bps'), 1)} bps"
    return (f"{text} <span class='small'>（{html.escape(label)}"
            f"{html.escape(extra)}）</span>")


def render(data: dict) -> str:
    scan = data["scan"]
    health_row = data["health"]
    banner_class = {"ok": "ok", "stale": "warn", "never": "bad"}.get(
        health_row.get("status"), "warn")
    banner = data["health_alert"] or "扫描与纸面进程心跳正常"

    parts: list[str] = []
    parts.append(f"<div class='banner {banner_class}'>{html.escape(banner)}</div>")
    # In-flight visibility: a 25-minute bridge and a stuck process used to look
    # identical ("the lock file exists" was all the board could say).
    inflight = data.get("inflight") or {}
    if inflight.get("state") == "running":
        who = str(inflight.get("label") or inflight.get("symbol") or "")
        age = inflight.get("age_s")
        parts.append(
            f"<div class='banner ok'>在飞：{html.escape(who)} · 步骤 "
            f"<b>{html.escape(str(inflight.get('step') or '?'))}</b>"
            + (f" · 已 {age:,.0f} s" if isinstance(age, (int, float)) else "")
            + f" · pid {inflight.get('pid')}</div>")
    elif inflight.get("state") == "stale":
        parts.append(
            f"<div class='banner bad'>锁还在，但 pid {inflight.get('pid')} 已经不在"
            "（上一次 cycle 没正常收尾）：先对账，别直接删锁</div>")
    if data["halt"]:
        parts.append("<div class='banner bad'>存在 xstocks_v2_HALT：已停开仓，"
                     "需要人工对账后删除该文件</div>")
    details = data.get("position_details") or [
        {"symbol": item, "state": "", "probe": False}
        for item in data.get("open_positions") or []]
    probe_hold = [row for row in details if row.get("probe")]
    real_hold = [row for row in details if not row.get("probe")]
    if probe_hold:
        parts.append("<div class='banner ok'>样本库存（按设计持有，不是卡单）："
                     + html.escape("、".join(str(row["symbol"]) for row in probe_hold))
                     + "。这些币在我们自己的钱包里，等该标的下一次机会顺手带走；"
                     "要清掉就手工卖出，不要等自动链"
                     + "</div>")
    if real_hold:
        parts.append("<div class='banner warn'>未平仓（需人工对账）："
                     + html.escape("、".join(
                        f"{row['symbol']}（{row.get('state')} @ {row.get('held_at')}）"
                        for row in real_hold))
                     + "</div>")
    parts.append(_investigations_section(data.get("investigations") or {},
                                         int(data.get("now_ms") or 0)))

    counters = scan["counters"]
    universe = scan["universe"]
    parts.append("<div class='cards'>")
    for label, value, note in (
        ("扫描时间", f"{scan['round_seconds']} s" if scan["round_seconds"] else "–",
         f"数据 {scan['age_s']} s 前" if scan["age_s"] is not None else "无数据"),
        ("Kraken 在册", f"{universe.get('kraken_priced', '–')}",
         f"在册 {universe.get('kraken_listed', universe.get('kraken_online', '–'))}"),
        ("Solana 可报价", f"{universe.get('solana_mints', '–')}",
         f"真报价 {counters.get('real_quotes', '–')} 条"),
        ("EVM 池", f"{universe.get('evm_pools', '–')}",
         f"其中 v4 {universe.get('v4_pools', '–')}"),
        ("Jupiter 调用", f"{counters.get('jupiter_calls', '–')}",
         f"429 × {counters.get('http_429', 0)}"),
        ("xChange 报价", f"{counters.get('xchange_quoted', '–')}",
         (f"调用 {counters.get('xchange_calls', 0)}"
          f" · 跳过 {counters.get('xchange_skipped', 0)}"
          + (" · 未配置 key" if counters.get("xchange_unconfigured") else ""))),
        ("门槛", f"{scan['threshold_bps']} bps",
         f"且净利 ≥ {scan['min_net_profit_usdt']} USDT"),
        ("全量双边标定", f"{data['timing'].get('round_seconds', '–')} s",
         "一次性实测" if data["timing"] else "未跑"),
        ("纸面", "关闭广播" if not data["paper"]["broadcast"] else "⚠️ 会广播",
         f"phase={data['paper'].get('phase')} auto_orders={data['paper'].get('auto_orders')}"),
        # Since 2026-10-08 (operator: 全部报价、只按轮转补) these pools are still
        # quoted; the list is evidence for the board, not a skip list.
        ("薄池 / 异常池（仍然报价）",
         f"{(scan.get('dead_pools') or {}).get('count', 0)}",
         f"本轮标记 {(scan.get('dead_pools') or {}).get('marked_this_round', 0)} 口"
         f" · 不再跳过报价"),
    ):
        parts.append(f"<div class='card'><div class='k'>{html.escape(label)}</div>"
                     f"<div class='v'>{html.escape(str(value))}</div>"
                     f"<div class='n'>{html.escape(str(note))}</div></div>")
    parts.append("</div>")

    parts.append("<h2>路径族</h2>")
    parts.append("<p class='small'>价差一律读作「<b>卖出得到的价格</b> 比 "
                 "<b>买入付出的价格</b> 高多少 bps」。正值才是我们能赚的方向。</p>")
    parts.append("<table><tr><th>买入口 → 卖出口</th><th>方向说明</th><th>行数</th>"
                 "<th>可信行</th><th>可信最好价差 bps</th><th>最好那一行的两侧价格</th>"
                 "<th>主要拒因</th></tr>")
    for item in data["families"]:
        best = item["best_edge_bps"]
        cls = "pos" if best is not None and best > 0 else ""
        reasons = "、".join(
            f"{html.escape(REASON_LABEL.get(name, name))} × {count}"
            for name, count in item["blockers"])
        across = "–"
        best_row = item.get("best_row")
        if best_row:
            across = (f"{html.escape(str(best_row.get('buy_venue')))}"
                      f"({html.escape(str(best_row.get('chain')))}) "
                      f"买入 {_fmt(best_row.get('buy_price_usd'), 4)}"
                      f" → {html.escape(str(best_row.get('sell_venue')))}"
                      f" 卖出 {_fmt(best_row.get('sell_price_usd'), 4)}"
                      f"　[{html.escape(str(best_row.get('symbol')))}]")
        parts.append(f"<tr><td><b>{html.escape(item['label'])}</b></td>"
                     f"<td class='small'>{html.escape(item['direction'])}</td>"
                     f"<td>{item['rows']}</td>"
                     f"<td>{item.get('believable', 0)}</td>"
                     f"<td class='{cls}'>{_fmt(best)}</td>"
                     f"<td class='small'>{across}</td>"
                     f"<td class='small'>{reasons}</td></tr>")
    parts.append("</table>")

    parts.append("<h2>可信行（按价差排序）</h2>")
    parts.append(
        "<p class='small'>「阻挡原因：无」= 数据质量与价格都过，<b>并且</b>这条链上"
        "有执行器。没有执行器的链会写明"
        "「没有接执行器」，不再伪装成可跑。"
        "实盘执行器当前覆盖：chain → Kraken 跑 "
        + "、".join(sorted(live_core.EXECUTOR_CHAINS[live_core.CHAIN_TO_KRAKEN]))
        + "。</p>")
    parts.append("<table><tr><th>标的</th><th>链</th>"
                 "<th>买入口（付出价）</th><th>卖出口（得到价）</th>"
                 "<th>卖出 − 买入 bps</th><th>阻挡原因</th></tr>")
    for row in data["top"]:
        blockers = row.get("blockers") or []
        cls = "pos" if _d(row.get("edge_t0_bps")) and _d(row["edge_t0_bps"]) > 0 else ""
        reasons = "、".join(html.escape(REASON_LABEL.get(name, name)) for name in blockers) or "无"
        parts.append(
            f"<tr><td>{html.escape(str(row.get('symbol')))}</td>"
            f"<td>{html.escape(str(row.get('chain')))}</td>"
            f"<td>{html.escape(str(row.get('buy_venue')))} "
            f"{_kraken_price_cell(row, row.get('buy_price_usd'), side='buy')}</td>"
            f"<td>{html.escape(str(row.get('sell_venue')))} "
            f"{_kraken_price_cell(row, row.get('sell_price_usd'), side='sell')}</td>"
            f"<td class='{cls}'>{_fmt(row.get('edge_t0_bps'))}</td>"
            f"<td class='small'>{reasons}</td></tr>")
    parts.append("</table>")

    if data.get("no_executor"):
        parts.append("<h2>有价差、但没有执行器的行（监控中，不接单）</h2>")
        parts.append(
            "<p class='small'>这些行数据质量和价格都过（有些价差还很大），但实盘"
            "执行器到不了这条链。板面以前把它们算进「可信行」并写「阻挡原因：无」，"
            "看起来像能跑 —— 2026-10-09 operator 就是拿着这样一行问"
            "「76.26 这个怎么不执行」。要接上就得先有 eth/ton 的执行器（见"
            " `docs/xstocks-v2-todo.md` A58）。</p>")
        parts.append("<table><tr><th>标的</th><th>链</th>"
                     "<th>买入口（付出价）</th><th>卖出口（得到价）</th>"
                     "<th>卖出 − 买入 bps</th><th>原因</th></tr>")
        for row in data["no_executor"]:
            parts.append(
                f"<tr><td>{html.escape(str(row.get('symbol')))}</td>"
                f"<td>{html.escape(str(row.get('chain')))}</td>"
                f"<td>{html.escape(str(row.get('buy_venue')))} "
                f"{_kraken_price_cell(row, row.get('buy_price_usd'), side='buy')}</td>"
                f"<td>{html.escape(str(row.get('sell_venue')))} "
                f"{_kraken_price_cell(row, row.get('sell_price_usd'), side='sell')}</td>"
                f"<td class='{'pos' if (_d(row.get('edge_t0_bps')) or 0) > 0 else ''}'>"
                f"{_fmt(row.get('edge_t0_bps'))}</td>"
                f"<td class='small'>"
                f"{html.escape(REASON_LABEL.get(live_core.NO_EXECUTOR_CHAIN, live_core.NO_EXECUTOR_CHAIN))}"
                "</td></tr>")
        parts.append("</table>")

    if data["traps"]:
        parts.append("<h2>同链两口池（阶段 2：必须一笔原子交易）</h2>")
        parts.append(
            "<p class='small'>同一条链上 A 池买入、B 池卖出。这笔交易<b>必须一笔完成</b>："
            "两条腿之间是裸仓，任何一条腿单独失败都修不回（钱已经动了）。"
            "现在拿不到「一次调用走两口池」的证明，所以这些行一律<b>不可执行</b>，"
            "下面列出来是为了让你看见机会有多大。</p>")
        if data.get("same_chain"):
            parts.append("<table><tr><th>标的</th><th>链</th><th>A 池（买入）</th>"
                         "<th>B 池（卖出）</th><th>卖出 − 买入 bps</th>"
                         "<th>毛 USDT</th><th>能不能执行</th></tr>")
            for row in data["same_chain"]:
                edge = _d(row.get("edge_t0_bps"))
                cls = "pos" if edge is not None and edge > 0 else ""
                # The family gate always comes first: every row here needs one
                # atomic call, so dropping it (as this cell used to, whenever a
                # second blocker existed) hid the reason the whole family is
                # parked. Extra blockers follow it.
                others = [name for name in (row.get("blockers") or [])
                          if name != "atomic_route_unproven"]
                can = "否：" + "、".join(
                    [html.escape("缺原子路线证明")]
                    + [html.escape(REASON_LABEL.get(name, name))
                       for name in others])
                parts.append(
                    f"<tr><td>{html.escape(str(row.get('symbol')))}</td>"
                    f"<td>{html.escape(str(row.get('chain')))}</td>"
                    f"<td class='small'>{html.escape(str(row.get('buy_venue')))} "
                    f"{_fmt(row.get('buy_price_usd'), 4)}<br>"
                    f"<code>{html.escape(str(row.get('buy_pool') or '')[:14])}</code></td>"
                    f"<td class='small'>{html.escape(str(row.get('sell_venue')))} "
                    f"{_fmt(row.get('sell_price_usd'), 4)}<br>"
                    f"<code>{html.escape(str(row.get('sell_pool') or '')[:14])}</code></td>"
                    f"<td class='{cls}'>{_fmt(row.get('edge_t0_bps'))}</td>"
                    f"<td>{_fmt(row.get('gross_usdt'), 4)}</td>"
                    f"<td class='small'>{can}</td></tr>")
            parts.append("</table>")
        else:
            parts.append("<p class='small'>本轮没有同链两口池的正价差。</p>")

        parts.append("<h3>被闸门拦下的最高价差（含坏数据的假价，以及有真价差但我们跑不了的行）</h3>")
        parts.append("<table><tr><th>标的</th><th>路径</th><th>链</th>"
                     "<th>t0 bps</th><th>阻挡原因</th></tr>")
        for row in data["traps"]:
            reasons = "、".join(html.escape(REASON_LABEL.get(name, name))
                               for name in row.get("blockers") or [])
            parts.append(
                f"<tr><td>{html.escape(str(row.get('symbol')))}</td>"
                f"<td>{html.escape(FAMILY_LABEL.get(row.get('family'), str(row.get('family'))))}</td>"
                f"<td>{html.escape(str(row.get('chain')))}</td>"
                f"<td class='bad'>{_fmt(row.get('edge_t0_bps'))}</td>"
                f"<td class='small'>{reasons}</td></tr>")
        parts.append("</table>")

    parts.append("<h2>自动小样本（缺样本就走一次最小额）</h2>")
    parts.append(
        "<p class='small'>这些行缺的是「这个币在这条边到账过」的样本。operator "
        "2026-10-09 的口径：<b>缺样本不是拒绝理由</b>——用最小可行额走一次就是触发条件。"
        "「交易」阶段 = 价差本来就过门槛，探针是这笔交易的第一段；"
        "「拿样本」阶段 = 价差还不够，但那两个数（价差、净利）本来就要靠这次过桥才能"
        "换成真的。两类都走最小额、都不卖不充值，币留在自己钱包。"
        "花钱的部分有硬上限：每天 3 次、每次 ≤2 USDT。</p>")
    budget = data.get("probe_budget") or {}
    if budget.get("max_per_day") is not None:
        spent = budget.get("spent_usdt")
        parts.append(
            "<p class='small'>今日探针预算：已用 <b>"
            f"{html.escape(str(budget.get('count') if budget.get('count') is not None else 0))}"
            f" / {html.escape(str(budget.get('max_per_day')))}</b> 次"
            f"（花费 {html.escape(str(spent if spent is not None else '0'))} USDT，"
            f"单次上限 {html.escape(str(budget.get('max_usdt_per_probe')))} USDT，"
            f"UTC 日 {html.escape(str(budget.get('day') or '–'))}）。</p>")
    probe_rows = (list(data.get("probe_candidates") or [])
                  + list(data.get("probe_samples") or [])
                  + list(data.get("probe_waiting") or []))
    if probe_rows:
        parts.append("<table><tr><th>标的</th><th>阶段</th><th>路线</th><th>价差 t0 bps</th>"
                     "<th>门槛 bps</th><th>充值最小额（币）</th><th>桥费 USDT</th>"
                     "<th>金额 USDT</th><th>能不能跑</th></tr>")
        for row in probe_rows:
            plan = row.get("probe") or {}
            waiting = plan.get("waiting_on") or []
            can = ("<b class='pos'>能</b>" if plan.get("runnable")
                   else "<span class='small'>等：" + "、".join(
                       html.escape(name) for name in waiting) + "</span>")
            if row.get("budget_ok") is False:
                can = ("<span class='small'>预算挡："
                       + html.escape(str(row.get("budget_reason") or "")) + "</span>")
            stage = ("交易" if row.get("stage") == "trade"
                     else "拿样本" if row.get("stage") == "sample"
                     else "等输入")
            parts.append(
                f"<tr><td>{html.escape(str(row.get('symbol')))}</td>"
                f"<td class='small'>{stage}</td>"
                f"<td>{html.escape(FAMILY_LABEL.get(row.get('family'), str(row.get('family'))))} "
                f"<code>{html.escape(str(row.get('chain')))}</code></td>"
                f"<td class='pos'>{_fmt(row.get('edge_t0_bps'))}</td>"
                f"<td>{html.escape(str(plan.get('required_edge_bps') or '–'))}</td>"
                f"<td>{html.escape(str(plan.get('deposit_min_units') or '未知'))}</td>"
                f"<td>{html.escape(str(plan.get('bridge_fee_usd') or '未知'))}</td>"
                f"<td>{html.escape(str(row.get('size_usdt') or '–'))}</td>"
                f"<td class='small'>{can}</td></tr>")
        parts.append("</table>")
    else:
        parts.append("<p class='small'>现在没有缺样本的行。</p>")

    coverage = data.get("chain_coverage") or []
    if coverage:
        parts.append("<h2>各链覆盖（这一轮每条链有没有行、最好的价差、被谁挡）</h2>")
        parts.append(
            "<p class='small'>一条链只要没有一行通过上面那张表的筛选，就不会出现在看板上 —— "
            "这张表保证它不会静默消失。`可执行` = 这条链有没有接实盘执行器"
            "（chain → Kraken 现已接上 solana、eth、ton、x-layer）。</p>")
        parts.append("<table><tr><th>链</th><th>本轮行数</th><th>最好价差 t0 bps</th>"
                     "<th>标的</th><th>主要阻挡</th><th>可执行</th></tr>")
        for item in coverage:
            blockers = "、".join(
                f"{html.escape(str(name))}×{count}"
                for name, count in (item.get("top_blockers") or [])) or "—"
            best = item.get("best_edge_bps")
            cls = "pos" if (best or 0) > 0 else "small"
            parts.append(
                f"<tr><td><code>{html.escape(str(item.get('chain')))}</code></td>"
                f"<td>{item.get('rows')}</td>"
                f"<td class='{cls}'>{_fmt(best)}</td>"
                f"<td class='small'>{html.escape('、'.join(item.get('symbols') or []))}</td>"
                f"<td class='small'>{blockers}</td>"
                f"<td class='small'>{'是' if item.get('executable') else '否'}</td></tr>")
        parts.append("</table>")

    paper = data["paper"]
    parts.append("<h2>纸面（链 → Kraken）</h2>")
    parts.append(f"<p class='small'>闸门：{html.escape(str(paper['gate'].get('reason')))}"
                 f"　候选 {paper.get('candidates', '–')} 条，"
                 f"接近门槛 {paper.get('near', '–')} 条。"
                 f"最近一轮 {_fmt(paper.get('updated_ms') and (data['now_ms'] - paper['updated_ms']) / 1000, 1)} 秒前。</p>")
    if paper["states"]:
        parts.append("<table><tr><th>状态</th><th>条数</th><th>含义</th></tr>")
        for key, count in sorted(paper["states"].items(), key=lambda kv: -kv[1]):
            state, _, reason = key.partition(":")
            label = REASON_LABEL.get(reason, reason) or "通过"
            parts.append(f"<tr><td>{html.escape(state)}</td><td>{count}</td>"
                         f"<td class='small'>{html.escape(label)}</td></tr>")
        parts.append("</table>")
    else:
        parts.append("<p class='small'>还没有纸面记录。</p>")
    parts.append(_funding_gap_section(data.get("funding_gap") or []))
    if paper["decisions"]:
        parts.append("<h3>最近判定</h3>"
                     "<table><tr><th>标的</th><th>链</th><th>状态</th>"
                     "<th>买入价</th><th>Kraken 买一</th><th>复价后 bps</th>"
                     "<th>净利 USDT</th><th>原因</th></tr>")
        for entry in list(reversed(paper["decisions"]))[:20]:
            reason = entry.get("reason") or ""
            parts.append(
                f"<tr><td>{html.escape(str(entry.get('symbol')))}</td>"
                f"<td>{html.escape(str(entry.get('chain')))}</td>"
                f"<td>{html.escape(str(entry.get('state')))}</td>"
                f"<td>{_fmt(entry.get('buy_price_usd'), 4)}</td>"
                f"<td>{_fmt(entry.get('sell_price_usd'), 4)}</td>"
                f"<td>{_fmt(entry.get('edge_after_requote_bps'))}</td>"
                f"<td>{_fmt(entry.get('net_usdt'), 4)}</td>"
                f"<td class='small'>{html.escape(REASON_LABEL.get(reason, reason) or '通过')}</td></tr>")
        parts.append("</table>")

    parts.append(_balances_section(data.get("balances") or {}, data["now_ms"]))
    parts.append("<h2>累计账本</h2>")
    states = data["cycles"]["by_state"]
    parts.append("<p class='small'>"
                 + ("　".join(f"{html.escape(name)} × {count}"
                              for name, count in sorted(states.items(), key=lambda kv: -kv[1]))
                    or "还没有记录")
                 + f"（共 {data['cycles']['total']} 条）</p>")
    money = data.get("realized") or {}
    if money:
        # The reconciliation the patrol asked for: what the ledger can prove,
        # and how many finished cycles it cannot price yet.
        parts.append(
            f"<p class='small'>已实现净利（live）：<b>{money.get('net_usdt')}</b> USDT，"
            f"来自 {money.get('measured_rows')} 笔可定价的 `sold`；"
            f"另有 <b>{money.get('unmeasured_rows')}</b> 笔没能定价"
            "（每笔缺哪一项见 `scripts/xstocks_v2_ledger.py realized`）。"
            "这个数要和三个钱包的余额变化对着看，差额本身就是一条发现。</p>")

    requests = data.get("address_requests") or []
    deferred = data.get("address_requests_deferred") or []
    parts.append("<h2>需要你在 Kraken 网页登记的提币地址</h2>")
    parts.append("<p class='small'>Kraken 没有写地址簿的 API（REST 五个名字全 404，"
                 "WebSocket 四个名字全是 Unsupported event，同一把 key 读权限正常）。"
                 "下面这些 (币种, 网络) 只要登记一次，方向「Kraken → 链」就会变成可执行。"
                 "登记时用你现有的钱包地址即可。</p>")
    if requests:
        parts.append("<table><tr><th>币种</th><th>提币网络（Kraken 侧）</th>"
                     "<th>建议 key</th><th>地址（要在这条网络上）</th>"
                     "<th>毛价差 t0 bps</th><th>净利 USDT</th>"
                     "<th>净利 ≥ 门槛？</th></tr>")
        for item in requests[:40]:
            passes = item.get("clears_threshold")
            if item.get("net_usdt") is None:
                mark = "<span class='small'>没算（缺一条腿的费用）</span>"
            elif passes:
                mark = "<b class='pos'>是</b>"
            else:
                mark = "<span class='small'>否，登记了也不做</span>"
            if item.get("address_ready") is False:
                shown = ("<b>缺地址</b><br><span class='small'>"
                         + html.escape(str(item.get("address_note") or ""))
                         + "</span>")
            else:
                shown = ("<code>"
                         + html.escape(str(item.get("address"))) + "</code>")
            parts.append(
                f"<tr><td>{html.escape(str(item.get('symbol')))}</td>"
                f"<td>{html.escape(str(item.get('network_label')))}</td>"
                f"<td><code>{html.escape(str(item.get('kraken_key_hint')))}</code></td>"
                f"<td class='small'>{shown}</td>"
                f"<td>{_fmt(item.get('edge_t0_bps'))} bps</td>"
                f"<td>{_fmt(item.get('net_usdt'), 4)}</td>"
                f"<td>{mark}</td></tr>")
        parts.append("<p class='small'>毛价差是「卖出得到的价格比买入付出的价格高多少 bps」，"
                     "不含任何费用；右边两列才是净利。两者不是同一个数。</p>")
        parts.append("</table>")
    else:
        parts.append("<p class='small'>现在没有这一类请求。</p>")

    if deferred:
        # Registering costs a web form plus an e-mail confirmation, and none of
        # these would be tradable afterwards (thin pool, negative spread). They
        # are listed anyway so no (asset, network) can hide -- and underlined as
        # "not now" so the main list keeps meaning "this unblocks a trade".
        parts.append("<h3>也要登记、但现在没有价差（先别登记）</h3>")
        parts.append("<p class='small'>这些 (币种, 网络) 同样缺地址，但还压着经济原因"
                     "（薄池假价 / 价差不够）。列出来是为了不留盲点；"
                     "等它们出现真价差再登记。</p>")
        parts.append("<table><tr><th>币种</th><th>提币网络（Kraken 侧）</th>"
                     "<th>建议 key</th><th>还差什么</th>"
                     "<th>毛价差 t0 bps</th><th>净利 USDT</th></tr>")
        for item in deferred[:20]:
            reason = REASON_LABEL.get(str(item.get("reason") or ""),
                                      str(item.get("reason") or "未知"))
            parts.append(
                f"<tr><td>{html.escape(str(item.get('symbol')))}</td>"
                f"<td>{html.escape(str(item.get('network_label')))}</td>"
                f"<td><code>{html.escape(str(item.get('kraken_key_hint')))}</code></td>"
                f"<td class='small'>{html.escape(reason)}</td>"
                f"<td>{_fmt(item.get('edge_t0_bps'))} bps</td>"
                f"<td>{_fmt(item.get('net_usdt'), 4)}</td></tr>")
        parts.append("</table>")

    coverage = data.get("rail_coverage") or {}
    if coverage.get("missing_count"):
        # "Not sampled" is not "no opportunity" (lessons section C). The board
        # has to say how much of the rail universe it did not even look at.
        parts.append("<h3>有 rail、但本轮没有报价（不是没机会）</h3>")
        parts.append(
            f"<p class='small'>Kraken 有 <b>{coverage.get('rails')}</b> 条 "
            f"(币种, 网络) 提币 rail；本轮扫描只报价了 <b>{coverage.get('quoted')}</b> 条，"
            f"剩下 <b>{coverage.get('missing_count')}</b> 条本轮**没有任何行**——"
            "不代表没有价差，只代表这一轮没有去看（报价预算优先给了排名靠前的名字）。"
            "有价差时它们会自己浮上来；这里列出来是为了不留盲点。</p>")
        parts.append("<details><summary>展开这 %d 条</summary>" %
                     coverage.get("missing_count"))
        parts.append("<table><tr><th>币种</th><th>网络</th><th>地址已登记？</th></tr>")
        for item in (coverage.get("missing") or [])[:40]:
            mark = "<b class='pos'>是</b>" if item.get("has_address") else \
                "<span class='small'>否（登记只在有价差时才值得做）</span>"
            parts.append(
                f"<tr><td>{html.escape(str(item.get('symbol')))}</td>"
                f"<td>{html.escape(str(item.get('network_label')))}</td>"
                f"<td>{mark}</td></tr>")
        parts.append("</table></details>")

    rotation = data.get("rotation") or {}
    if rotation.get("slots"):
        # The list above says which rails nobody quoted. This says what the scan
        # does about it: a few slots every round go to the longest wait, so the
        # queue below drains instead of standing still (2026-10-08).
        picked = rotation.get("picked") or []
        names = "、".join(html.escape(str(item.get("symbol"))) for item in picked)
        budget = (scan.get("universe") or {}).get("stage2_max")
        # A queue whose head has never been quoted has no wait to report; the
        # count on its left already says it.
        oldest = (rotation.get("waiting") or [{}])[0]
        parts.append("<h3>轮转名额：给最久没报价的 rail 补的名额</h3>")
        parts.append(
            f"<p class='small'>Solana 每轮只有 <b>{budget if budget is not None else '?'}</b> "
            f"个真报价名额，却有 <b>{rotation.get('candidates')}</b> 条 "
            "(币种, Solana) rail 在抢；排序只看当时的毛价差，所以排不上的 rail "
            f"永远不会有行。现在每轮留 <b>{rotation.get('slots')}</b> 个名额给"
            "<b>最久没报过价</b>的 rail（<code>[watch] rotation_slots</code>），"
            "名额从同一个预算里让出，不增加总报价数，轮次时长不变。<br>"
            f"本轮补了 <b>{len(picked)}</b> 条：{names or '没有（队列已空）'}。<br>"
            f"队列里还有 <b>{rotation.get('waiting_count')}</b> 条没轮到，其中 "
            f"<b>{rotation.get('never_quoted_count')}</b> 条从未报过价"
            + (f"，最久的已经等了 {_wait_label(oldest)}" if oldest.get("waited_s")
               else "")
            + "。</p>")
        if rotation.get("waiting"):
            parts.append("<details><summary>展开队列前 %d 条</summary>"
                         % len(rotation["waiting"]))
            parts.append("<table><tr><th>币种</th><th>网络</th>"
                         "<th>上次拿到名额</th><th>等了多久</th>"
                         "<th>累计拿到过几次</th></tr>")
            for item in rotation["waiting"]:
                parts.append(
                    f"<tr><td>{html.escape(str(item.get('symbol')))}</td>"
                    "<td>Solana</td>"
                    f"<td class='small'>{_wait_label(item, absolute=True)}</td>"
                    f"<td>{_wait_label(item)}</td>"
                    f"<td>{html.escape(str(item.get('slots')))}</td></tr>")
            parts.append("</table></details>")

    dead = scan.get("dead_pools") or {}
    if dead.get("count"):
        parts.append("<h3>薄池 / 异常池的证据（仍然报价）</h3>")
        parts.append(
            "<p class='small'>operator 2026-10-08：「以利润为准…即使我们的交易占池子超过 20% "
            "也是可以的」+「全部报价、只按轮转补」。所以 <code>max_depth_share</code>(0.2) 与 "
            "<code>max_price_impact_bps</code>(300) <b>不再是门槛</b>，这里列的是「这口池在我们这个"
            "票面上有多薄」的证据（我们的票面占池子多少 / 报价自报的冲击 / 同池往返），"
            "<b>池子照常报价</b>，能不能做由净利判。凭据要连续命中 "
            f"{dead.get('confirm_hits', 2)} 轮才记录，每轮还会重看最久没看的 "
            f"{dead.get('rechecked_this_round', 0)} 口，本轮复活 "
            f"{dead.get('revived_this_round', 0)} 口。唯一还硬的是数据质量：报价与参考价呈整倍数"
            "或偏离 ≥ 3000 bps 的按 <code>price_outlier</code> 拦（那是数据错了，不是便宜）。</p>")
        parts.append("<details><summary>展开 %d 个池</summary>" % dead.get("count"))
        parts.append("<table><tr><th>池</th><th>原因</th><th>细节</th><th>见过几轮</th>"
                     "<th>当时深度</th><th>我们的票面占比</th><th>报价冲击</th>"
                     "<th>同池往返</th></tr>")
        for pool_key, entry in list((dead.get("entries") or {}).items()):
            parts.append(
                f"<tr><td class='small'><code>{html.escape(str(pool_key))}</code></td>"
                f"<td>{html.escape(str(entry.get('reason')))}</td>"
                f"<td class='small'>{html.escape(_dead_detail(entry.get('detail')))}</td>"
                f"<td>{html.escape(str(entry.get('hits')))}</td>"
                f"<td>{_fmt(entry.get('depth_usd'), 4) if entry.get('depth_usd') else '未知'}</td>"
                f"<td>{_fmt(entry.get('pool_share'), 4) if entry.get('pool_share') else '未知'}</td>"
                f"<td>{_fmt(entry.get('impact_bps'), 1) if entry.get('impact_bps') else '未报'}</td>"
                f"<td>{_fmt(entry.get('round_trip_bps'), 1) + ' bps' if entry.get('round_trip_bps') else '未测'}</td></tr>")
        parts.append("</table></details>")

    gas = scan.get("gas_prices") or {}
    if gas.get("stale") or gas.get("missing"):
        # A stale gas price is why a whole family reads `cost_unknown`: say so
        # instead of letting the rows look like "no opportunity".
        parts.append("<div class='banner warn'>桥费定价缺输入："
                     + ("过期 " + "、".join(gas.get("stale") or []) + " " if gas.get("stale") else "")
                     + ("没有 " + "、".join(gas.get("missing") or []) if gas.get("missing") else "")
                     + f"（允许 {gas.get('max_age_s', '?')} s）。这些边会保持 cost_unknown。"
                     "扫描每轮会自己取价（A1，2026-10-09 起）；这一轮没取到，"
                     f"原因写在本轮快照的 counters：{html.escape(json.dumps((scan.get('bridge_fees') or {}).get('price_refresh') or {}, ensure_ascii=False))}</div>")
    if gas.get("prices"):
        parts.append("<p class='small'>gas 代币价：" + "、".join(
            f"{asset} {row.get('usd')}（{row.get('source') or '未知来源'}，"
            f"{row.get('age_s')} s 前）"
            for asset, row in gas["prices"].items()) + "</p>")
    live = (scan.get("bridge_fees") or {}).get("live") or {}
    if live:
        # Since A3 the fee comes off the chain every round; the receipt file is
        # the fallback and the cross-check. Print both so a drifting fee is
        # visible instead of averaged away.
        cross = (scan.get("bridge_fees") or {}).get("live_vs_evidence") or {}
        parts.append("<p class='small'>桥费（本轮链上读取）：" + "、".join(
            f"{html.escape(key)} <b>{html.escape(str(item.get('fee_amount')))}"
            f" {html.escape(str(item.get('fee_asset') or ''))}</b>"
            + (f"（与实测档案差 {_fmt(entry.get('mismatch_bps'), 1)} bps）"
               if (entry := cross.get(key)) else "")
            for key, item in live.items()) + "</p>")
    lines = scan.get("gas_lines") or {}
    if lines.get("xlayer_usdt"):
        parts.append(
            f"<p class='small'>链上 gas 线（已计入净利）：X Layer 一次动作 "
            f"<b>{lines['xlayer_usdt']}</b> USDT"
            f"（gas price {lines.get('xlayer_gas_price_wei')} wei，"
            f"来源 {lines.get('source')}）；Ethereum 一次动作 "
            f"<b>{lines.get('eth_usdt')}</b> USDT"
            f"（gas price {lines.get('eth_gas_price_wei')} wei，"
            f"来源 {lines.get('eth_source')}）。含哪条链的腿就收哪条线；"
            "没有定价的 EVM 链会在行上标 <code>gas_note=unknown:…</code>。"
            "主网 gas 是固定成本，所以 25 USDT 票面的 eth 行常常过不了门槛 —— "
            "这是真的成本，不是没机会。</p>")

    return PAGE.format(body="\n".join(parts))


PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>arb v2 · xStocks any-to-any（只读）</title>
<meta http-equiv="refresh" content="30">
<style>
:root {{ color-scheme: dark; }}
body {{ background:#14161a; color:#e6e6e6; font:14px/1.5 -apple-system,"PingFang SC",sans-serif;
  margin:0; padding:24px 28px; }}
h1 {{ font-size:19px; margin:0 0 4px; }}
h2 {{ font-size:16px; margin:26px 0 8px; border-left:3px solid #4b7bec; padding-left:8px; }}
h3 {{ font-size:14px; margin:16px 0 6px; color:#b9c0cc; }}
.sub {{ color:#8b93a1; font-size:12px; margin-bottom:16px; }}
.banner {{ padding:9px 12px; border-radius:6px; margin-bottom:10px; font-weight:600; }}
.banner.ok {{ background:#14351f; color:#7be495; }}
.banner.warn {{ background:#3a2f11; color:#f7c948; }}
.banner.bad {{ background:#3d1a1a; color:#ff8080; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:10px; }}
.card {{ background:#1c1f25; border:1px solid #262a31; border-radius:8px; padding:10px 12px; }}
.card .k {{ color:#8b93a1; font-size:11px; }}
.card .v {{ font-size:19px; font-weight:700; margin:2px 0; }}
.card .n {{ color:#8b93a1; font-size:11px; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }}
th,td {{ border-bottom:1px solid #262a31; padding:5px 8px; text-align:left; }}
th {{ color:#8b93a1; font-weight:600; font-size:12px; }}
td.pos {{ color:#7be495; font-weight:700; }}
td.bad {{ color:#ff9f6e; }}
td.small,.small {{ color:#9aa2b1; font-size:12px; }}
/* Balances: one chain per row, numbers on fixed rails.
   Operator 2026-10-09:「颜色、大小、对齐都很乱…宁愿不要那么复杂」. So this block
   is closed: **three text tones** (ink / secondary / faint) + one amber for
   「读不到」, and **three sizes** (13 numbers and names, 12 holdings and bar,
   11 labels, notes and addresses). No per-chain colour, no chip blocks, no bars,
   no fourth white. */
.bal-bar {{ display:flex; flex-wrap:wrap; align-items:baseline; gap:4px 16px;
  margin:0 0 10px; font-size:12px; color:#98a1b0; }}
.bal-bar b {{ color:#e6e8ec; font-size:13px; }}
.bal-pill {{ background:#1c2230; border:1px solid #2b3546; border-radius:999px;
  padding:2px 9px; font-size:11px; color:#98a1b0; }}
.bal-pill.warn {{ background:#3a2f11; border-color:#6b5410; color:#f7c948; }}
.bal-bar .bad, .bal-bar b.bad {{ color:#ff9f6e; }}
.bal-led {{ border:1px solid #23272f; border-radius:10px; overflow:hidden;
  background:#171a20; color:#e6e8ec; font-size:13px; }}
.bal-row {{ display:grid; align-items:start;
  grid-template-columns:148px minmax(0,1fr) 172px 104px; gap:2px 18px;
  padding:9px 14px; border-top:1px solid #20242b; font-size:12px; }}
.bal-row:first-child {{ border-top:0; }}
.bal-row:hover {{ background:#1a1e25; }}
.bal-who {{ min-width:0; }}
.bal-name {{ display:block; font-size:13px; font-weight:600; color:#e6e8ec; }}
.bal-acct {{ background:none; border:0; padding:0; cursor:copy; color:#6b7280;
  font-size:11px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
.bal-acct:hover {{ color:#e6e8ec; text-decoration:underline dotted; }}
.bal-hold {{ display:flex; flex-wrap:wrap; align-items:baseline; gap:3px 16px;
  min-width:0; font-size:12px; color:#98a1b0; }}
.bal-item {{ display:inline-flex; align-items:baseline; gap:6px; }}
.bal-item .bal-asset {{ color:#98a1b0; }}
.bal-item .bal-amt {{ color:#e6e8ec; font-variant-numeric:tabular-nums;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
.bal-note {{ font-size:11px; color:#6b7280; }}
.bal-item.bad .bal-asset, .bal-item.bad .bal-amt {{ color:#ff9f6e; }}
.bal-item.bad .bal-note {{ color:#ff9f6e; opacity:.7; max-width:230px;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.bal-cash {{ text-align:right; min-width:0; }}
.bal-cash-k {{ font-size:11px; color:#6b7280; white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis; }}
.bal-cash-sub {{ margin-left:5px; }}
.bal-cash-v {{ font-size:13px; font-weight:600; color:#e6e8ec;
  font-variant-numeric:tabular-nums;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
.bal-cash-v.bad {{ color:#ff9f6e; }}
.bal-usd {{ text-align:right; font-size:13px; color:#e6e8ec;
  font-variant-numeric:tabular-nums;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
.bal-group-note {{ margin:2px 0 0; font-size:11px; color:#6b7280; }}
.bal-fold {{ margin:2px 0 0; flex-basis:100%; }}
.bal-fold summary {{ cursor:pointer; font-size:11px; color:#6b7280; }}
.bal-fold-list {{ padding:4px 0 2px; line-height:1.7; color:#6b7280;
  font-size:11px; word-break:break-all; }}
.bal-fold-list .dust {{ color:#6b7280; opacity:.75; }}
.toast {{ position:fixed; left:50%; bottom:22px; transform:translateX(-50%);
  background:#2b6cb0; color:#fff; border-radius:6px; padding:6px 14px;
  font-size:12px; opacity:0; transition:opacity .18s; pointer-events:none; }}
.toast.on {{ opacity:1; }}
</style></head><body>
<h1>arb v2 · xStocks any-to-any（只读）</h1>
<div class="sub">不签名、不下单。价差是买卖两侧的观测值，成本与复价后的净利才进门禁。
参考价只用于排序。数据目录 <code>data-v2/</code>。</div>
{body}
<div class="toast" id="toast"></div>
<script>
// Click an account address to copy it. The board stays read-only; this only
// touches the clipboard, and a failure prints the address instead of hiding it.
document.addEventListener('click', function (event) {{
  var node = event.target.closest('[data-copy]');
  if (!node) {{ return; }}
  var text = node.getAttribute('data-copy');
  var toast = document.getElementById('toast');
  var show = function (message) {{
    toast.textContent = message;
    toast.classList.add('on');
    setTimeout(function () {{ toast.classList.remove('on'); }}, 1400);
  }};
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(text).then(
      function () {{ show('已复制 ' + text); }},
      function () {{ show(text); }});
  }} else {{
    show(text);
  }}
}});
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    interval_s = DEFAULT_INTERVAL_S
    data_dir = DATA

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                body = render(collect(data_dir=self.data_dir,
                                      interval_s=self.interval_s)).encode()
                kind = "text/html; charset=utf-8"
            elif path == "/json":
                body = json.dumps(collect(data_dir=self.data_dir,
                                          interval_s=self.interval_s),
                                  ensure_ascii=False, default=str).encode()
                kind = "application/json; charset=utf-8"
            else:
                self.send_error(404)
                return
        except Exception as exc:  # noqa: BLE001
            self.send_error(500, f"{type(exc).__name__}: {exc}")
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--data-dir", default=str(DATA))
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S)
    parser.add_argument("--once", action="store_true", help="print the JSON once and exit")
    args = parser.parse_args()

    if args.once:
        print(json.dumps(collect(data_dir=Path(args.data_dir), interval_s=args.interval),
                         ensure_ascii=False, default=str, indent=1)[:6000])
        return 0
    Handler.interval_s = args.interval
    Handler.data_dir = Path(args.data_dir)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"http://{args.host}:{args.port}/ (只读)", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
