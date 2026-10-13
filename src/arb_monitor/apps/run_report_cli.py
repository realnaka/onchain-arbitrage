"""Turn an execution-ledger group into a readable report and a Feishu message.

The ledger is the source of truth; this only formats it. Prices, fees and
contract addresses are printed as recorded, and a group that ended in
``reconciled`` says so instead of pretending the automated path finished.
"""
from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from arb_monitor.apps.account_dashboard import load_toml
from arb_monitor.core.hk import dumps
from arb_monitor.execution.ledger import ExecutionLedger
from arb_monitor.ops.notify import FeishuNotifier

CONFIG = "config/hk_monitor.toml"


def add_arguments(subparsers) -> None:
    parser = subparsers.add_parser(
        "run-report", help="render one execution ledger group as a report / Feishu message")
    parser.add_argument("--group", required=True, nargs="+", help="execution group id(s)")
    parser.add_argument("--ledger", default="data/execution.db")
    parser.add_argument("--config", default=CONFIG)
    parser.add_argument("--send", action="store_true", help="deliver via the configured channels")
    parser.add_argument("--json", action="store_true")


def _num(value, places: int = 8) -> str:
    if value in (None, ""):
        return "—"
    try:
        text = f"{+Decimal(str(value)):.{places}f}"
    except (InvalidOperation, ValueError):
        return str(value)
    # Trim trailing zeros only when there is a fractional part, so "20" never
    # becomes "2".
    return text.rstrip("0").rstrip(".") if "." in text else text


def _bps(value, places: int = 1) -> str:
    if value in (None, ""):
        return "—"
    return f"{Decimal(str(value)):+.{places}f}"


def collect(ledger_path: str, group_ids: list[str]) -> dict:
    ledger = ExecutionLedger(ledger_path)
    try:
        groups = []
        for group_id in group_ids:
            events = ledger.events(group_id)
            if not events:
                raise SystemExit(f"unknown ledger group: {group_id}")
            groups.append({"group_id": group_id, "events": events,
                           "state": events[-1]["state"]})
        return {"groups": groups, "open_groups": ledger.recover()}
    finally:
        ledger.close()


def plan_payload(group: dict) -> dict:
    for event in group["events"]:
        plan = (event.get("payload") or {}).get("plan")
        if plan:
            return plan
    return {}


def last_event(group: dict, event_type: str) -> dict:
    for event in reversed(group["events"]):
        if event["event_type"] == event_type:
            return event
    return {}


def format_report(report: dict, config: dict) -> str:
    tokens = {t.get("asset"): t for t in config.get("tokens") or []}
    lines: list[str] = []
    opens = [g for g in report["groups"] if last_event(g, "hedged")]
    closes = [g for g in report["groups"] if "reconciled" in
              [e["event_type"] for e in g["events"]] or last_event(g, "closed")]
    plan = plan_payload(opens[0] if opens else report["groups"][0])
    asset = plan.get("asset") or "—"
    token = tokens.get(asset) or {}
    lines += [
        f"【套利全链路执行报告】{asset} · 现货多 + 永续空",
        f"模式: {'强制链路测试（跳过净基差阈值）' if plan.get('forced_plumbing_test') else '信号驱动'}",
        "",
        "链上合约地址",
        f"  买入代币 {plan.get('spot_token')}: {plan.get('spot_token_address')}",
        f"  支付币 {plan.get('quote_token')}: {plan.get('quote_token_address')}",
        f"  链: X Layer (chain {plan.get('chain_id')})",
        f"  https://www.oklink.com/xlayer/token/{plan.get('spot_token_address')}",
    ]
    for group in opens:
        approval = last_event(group, "allowance_ready").get("payload", {}).get("allowance") or {}
        spot = last_event(group, "spot_filled").get("payload", {})
        opened = last_event(group, "hedged").get("payload", {})
        lines += [
            "",
            f"开仓（账本组 {group['group_id']}，状态 {group['state']}）",
            f"  授权: {approval.get('action')} 上限 {approval.get('approved_amount')} "
            f"最小单位（spender {approval.get('spender')}）" if approval else "  授权: —",
            f"  现货: {_num(spot.get('quote_amount'), 6)} {plan.get('quote_token')} → "
            f"{_num(spot.get('token_quantity'), 18)} {plan.get('spot_token')} "
            f"@ {_num(spot.get('price_usd'), 8)}（tx {str(spot.get('tx_hash'))[:20]}…，"
            f"gas {_num(spot.get('gas_paid_native'), 8)}）",
            f"  合约: 空 {_num(opened.get('filled_contracts'), 0)} 张 "
            f"{plan.get('perp')} @ {_num(opened.get('average_price'), 5)}"
            f"（taker IOC，手续费 {_num(opened.get('fees'), 8)} USDT）",
            f"  未对冲: {_num(opened.get('unhedged_tokens'), 18)} 枚"
            f"（现货比合约多出的部分）",
        ]
    for group in closes:
        perp = last_event(group, "perp_closed").get("payload", {})
        recon = last_event(group, "reconciled").get("payload", {})
        spot = recon.get("spot_sell") or {}
        lines += [
            "",
            f"平仓（账本组 {group['group_id']}，状态 {group['state']}）",
            f"  合约: 平 {_num(perp.get('filled'), 0)} 张 @ {_num(perp.get('average'), 5)}"
            f"（reduce-only taker，手续费 {_num(perp.get('fees'), 8)} USDT）",
            f"  现货: {_num(spot.get('token_quantity'), 18)} {plan.get('spot_token')} → "
            f"{_num(spot.get('quote_received'), 6)} {plan.get('quote_token')} "
            f"@ {_num(spot.get('realised_price_usd'), 8)}（tx {str(spot.get('tx_hash'))[:20]}…）",
        ]
        if recon.get("balances_after"):
            after = recon["balances_after"]
            lines += [
                "",
                "结果",
                f"  OKX USDT 可用: {after.get('okx_usdt_available')}；"
                f"钱包 {plan.get('quote_token')}: {after.get('wallet_usdg')}",
                f"  最终持仓: OKX {after.get('okx_positions')}，"
                f"链上代币 {after.get('wallet_tokens')}",
            ]
        if recon.get("cycle_result"):
            cycle = recon["cycle_result"]
            lines += [
                f"  本轮成本: {cycle.get('total_cost_usdt')} USDT"
                f"（USDT {cycle.get('usdt_delta')}，USDG {cycle.get('usdg_delta')}）",
                f"  现货往返实测 {cycle.get('spot_round_trip_bps')} bps，"
                f"模型预测 {cycle.get('model_predicted_spot_round_trip_bps')} bps",
            ]
    lines += [
        "",
        "说明",
        f"  · 本次{'不是策略信号成交：' if plan.get('forced_plumbing_test') else ''}"
        f"当时净基差 {_bps(plan.get('net_entry_basis_bps'))} bps（阈值 "
        f"{_num(plan.get('min_net_edge_bps'), 0)} bps）",
        # Whatever the plan actually warned about, and nothing that it did not.
        *[f"  · {warning}" for warning in (plan.get("warnings") or [])],
        "  · 双腿均为 taker：链上 exact-in swap + 永续 IOC 限价",
        f"  · 账本: {', '.join(g['group_id'] for g in report['groups'])}",
    ]
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    config = load_toml(args.config)
    report = collect(args.ledger, args.group)
    message = format_report(report, config)
    if args.json:
        print(dumps({**report, "feishu_message": message}))
    else:
        print(message)
    if args.send:
        notifier = FeishuNotifier(config.get("notify") or {})
        result = notifier.send(message)
        print("\n---\n飞书发送结果: " + json.dumps(
            {"ok": result.get("ok"), "channel": result.get("channel"),
             "detail": result.get("detail")}, ensure_ascii=False))
        return 0 if result.get("ok") else 1
    return 0
