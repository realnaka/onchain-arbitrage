"""Triage the unresolved ledger groups, one by one.

```bash
PYTHONPATH=src .venv/bin/python -m arb_monitor ledger-triage
PYTHONPATH=src .venv/bin/python -m arb_monitor ledger-triage --check-account
```

The board answers "is there exposure right now" from the venue; the ledger
answers "which runs did not finish". This command reads the second and, with
``--check-account``, asks the first so the two can be compared: a group whose
state *could* hold a position but whose venue side is flat is a bookkeeping
entry, not a risk — provided the comparison is stated.

It never writes. Archiving a group is a human action through
``ledger-reconcile``, with the confirmed facts in hand.
"""
from __future__ import annotations

import argparse
import time

from arb_monitor.apps.account_dashboard import (classify_group, load_toml,
                                                okx_read_env,
                                                requires_position_check)
from arb_monitor.core.hk import dumps
from arb_monitor.execution.ledger import ExecutionLedger
from arb_monitor.execution.pending_scan import (EXPOSURE_GROUP_STATES,
                                                TERMINAL_GROUP_STATES)

# What the operator should do next, per state. Kept next to the reader that
# produces the state so the two cannot drift silently.
NEXT_ACTION = {
    "precheck": "闸门已跑完但未发单。核对 OKX positions/pending 后 "
                "ledger-reconcile --state reconciled_manual，理由写「未发单」",
    "allowance_ready": "只写了授权事件，没有下单。核对 allowance 与实际余额后 "
                       "ledger-reconcile --state reconciled_manual",
    "execution_error": "自动执行在该组上停机过一次。确认两腿已归零、"
                       "AUTO_HALT 已清，再 ledger-reconcile 归档",
    "spot_filled": "现货腿已成交、对冲未确认：**先读 OKX positions/pending 与钱包余额**，"
                   "再决定补对冲或卖出，最后 ledger-reconcile",
    "perp_submitted": "对冲已提交、回读未完成：按 ordId 查订单终态，再决定",
    "hedged": "该组记录为已对冲但未平：按正常平仓流程处理（full-chain --close）",
    "perp_closed": "合约已平、现货未确认：核对钱包余额后再卖现货",
    "maker_partial": "挂单部分成交未收尾：查挂单是否仍在，必要时撤单后重新平仓",
    "maker_unknown": "挂单状态未知（可能仍在盘口）：先查 orders-pending 并撤单",
    "close_unknown": "现货卖出结果未知：按 txHash 查询，禁止重发同一笔卖出",
    "manual_intervention_required": "人工接管：按 manual review 消息里的两条腿逐项核对",
    "spot_short_of_hedge": "裸空：立刻核对合约空头与代币余额，补买现货或买回合约",
    "spot_over_hedge": "未对冲多头超容差：核对代币余额后卖出多余部分",
    "naked_short_unconfirmed": "裸空且回补未确认：最高优先级，先查合约持仓与挂单",
    "perp_unknown": "合约订单终态未知：按 ordId 查询，禁止重发",
    "unhedged_spot_only": "有现货无对冲：核对后补对冲或卖出",
}


def add_arguments(subparsers) -> None:
    parser = subparsers.add_parser(
        "ledger-triage",
        help="list unresolved execution ledger groups with the next action")
    parser.add_argument("--ledger", default="data/execution.db")
    parser.add_argument("--check-account", action="store_true",
                        help="cross-check against live positions and pending orders")
    parser.add_argument("--json", action="store_true")


def _account_check() -> dict:
    from arb_monitor.execution.okx_account import OKXReadOnlyClient

    client = OKXReadOnlyClient.from_env(okx_read_env("config/okx-demo.env"))
    if not client.configured:
        return {"status": "unconfigured"}
    out: dict = {"status": "ok"}
    try:
        out["positions"] = [
            {"instId": row.get("instId"), "pos": row.get("pos"),
             "avgPx": row.get("avgPx")}
            for row in client.positions("SWAP")
            if str(row.get("pos") or "0") not in {"0", ""}]
    except Exception as exc:  # noqa: BLE001 - unknown is reported as unknown
        out["positions"] = None
        out["positions_error"] = f"{type(exc).__name__}:{str(exc)[:120]}"
        out["status"] = "partial"
    try:
        out["pending"] = [
            {"instId": row.get("instId"), "clOrdId": row.get("clOrdId"),
             "side": row.get("side"), "sz": row.get("sz"),
             "state": row.get("state")}
            for row in client.pending_orders("SWAP")]
    except Exception as exc:  # noqa: BLE001 - unknown is reported as unknown
        out["pending"] = None
        out["pending_error"] = f"{type(exc).__name__}:{str(exc)[:120]}"
        out["status"] = "partial"
    return out


def run(args: argparse.Namespace) -> int:
    ledger = ExecutionLedger(args.ledger)
    try:
        groups = ledger.recover()
        clordids = ledger.client_order_ids()
    finally:
        ledger.close()
    now = time.time_ns() // 1_000_000
    rows = []
    for group_id, row in groups.items():
        state = row.get("state")
        if classify_group(state) == "historical":
            continue
        age_s = int((now - int(row.get("created_ts_ms") or now)) / 1000)
        cids = sorted(cid for cid, gid in clordids.items() if gid == group_id)
        rows.append({
            "group_id": group_id,
            "state": state,
            "event_type": row.get("event_type"),
            "signal_id": row.get("signal_id"),
            "age_seconds": age_s,
            # A recorded client order id means an order this group placed may
            # still be resting, whatever the state name suggests. Measured
            # 2026-09-21: `auto-shein` was `execution_error`, reported
            # `may_hold_position: false`, and the order its halt left behind
            # filled three minutes later.
            "may_hold_position": state in EXPOSURE_GROUP_STATES or bool(cids),
            "preparation_only": requires_position_check(state),
            "clordids": cids,
            "next_action": NEXT_ACTION.get(state, "状态未登记：人工判断后再归档"),
        })
    rows.sort(key=lambda r: (not r["may_hold_position"], -r["age_seconds"]))
    account = _account_check() if args.check_account else None
    verdict = _verdict(rows, account)
    payload = {"checked_at_ms": now, "unresolved_count": len(rows),
               "may_hold_position_count": sum(1 for r in rows if r["may_hold_position"]),
               "historical_count": len(groups) - len(rows),
               "account": account, "verdict": verdict, "groups": rows}
    if args.json:
        print(dumps(payload))
    else:
        print(_render(payload))
    return 0


def _verdict(rows: list[dict], account: dict | None) -> dict:
    """Whether the ledger's unfinished business has a venue side to match."""
    exposure = [row for row in rows if row["may_hold_position"]]
    out = {"may_hold_position_groups": [row["group_id"] for row in exposure],
           "account_checked": account is not None}
    if account is None:
        out["conclusion"] = ("未做账户交叉核对：带仓状态的组必须先在 OKX/钱包核实，"
                             "再逐组归档")
        return out
    if account.get("status") != "ok":
        out["conclusion"] = ("账户读取不完整（未知即未知）：不能据此判定无敞口，"
                             "请重跑 --check-account")
        return out
    positions = account.get("positions") or []
    pending = account.get("pending") or []
    out["live_positions"] = len(positions)
    out["live_pending_orders"] = len(pending)
    if exposure:
        out["conclusion"] = (f"账本有 {len(exposure)} 个带仓状态的组，"
                             "逐组按 next_action 处理后再归档")
    elif positions or pending:
        out["conclusion"] = ("账本无带仓未结组，但账户上有持仓/挂单："
                             "先按 orders-pending/positions 处理，再回头对账账本")
    else:
        out["conclusion"] = ("账本未结组均为准备态/停机记录，且 OKX 实时无持仓、"
                             "无挂单：可逐组 ledger-reconcile 归档")
    return out


def _render(payload: dict) -> str:
    lines = [f"未结组 {payload['unresolved_count']}（可能带仓 "
             f"{payload['may_hold_position_count']}）/ 历史 {payload['historical_count']}"]
    if payload.get("account"):
        account = payload["account"]
        lines.append(f"账户交叉核对: status={account.get('status')} "
                     f"positions={account.get('positions')} "
                     f"pending={account.get('pending')}")
    lines.append(f"结论: {payload['verdict']['conclusion']}")
    for row in payload["groups"]:
        tag = "带仓" if row["may_hold_position"] else ("准备态" if row["preparation_only"]
                                                 else "记录")
        lines.append("")
        lines.append(f"[{tag}] {row['group_id']} · {row['state']} · "
                     f"{row['age_seconds']}s 前 · {row['event_type']}")
        if row["clordids"]:
            lines.append(f"    clOrdId: {', '.join(row['clordids'])}")
        lines.append(f"    建议: {row['next_action']}")
    return "\n".join(lines)
