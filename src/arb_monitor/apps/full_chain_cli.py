"""CLI wiring for the end-to-end spot-first run.

```bash
# plan only: prices, costs, risk gate — no quote is signed, no order is sent
PYTHONPATH=src .venv/bin/python -m arb_monitor full-chain --asset XIAOMI --size 4

# the same plan, then the two real legs (requires --confirm-live)
PYTHONPATH=src .venv/bin/python -m arb_monitor full-chain \
    --asset XIAOMI --size 4 --mode live --confirm-live
```

The default is ``dry`` on purpose: looking at the plan must never be one typo
away from sending an order.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from decimal import Decimal, InvalidOperation

from arb_monitor.execution.full_chain import FullChainError
from arb_monitor.execution.live_stack import EXECUTION_LEDGER, LiveStackError, build_live_stack
from arb_monitor.execution.order_flows import (
    HALT_STATES, FlowResult, maker_first_close as _maker_first_close,
    maker_first_open as _maker_first_open,
)
from arb_monitor.execution.pending_scan import scan_pending_orders

# States that mean a leg is stranded and a person has to look. The CLI tells
# the alert channel about them, because the run that hit one is already over.
REVIEW_STATES = frozenset(HALT_STATES)


def _read_positions(read) -> list[dict]:
    """Positions for the close gate, or [] when the venue cannot be read.

    An empty list is not "flat": the caller sees the read error in the printed
    audit and the close gate treats a missing position list as "cannot check",
    not as "no position".
    """
    try:
        return list(read.positions())
    except Exception as exc:  # noqa: BLE001 - reported, not fatal
        print(f"warning: 持仓读取失败（{type(exc).__name__}）", file=sys.stderr)
        return []


def _orphan_gate(read, ledger, policy: dict, now_ms: int) -> tuple[bool, list[str], dict]:
    """Refuse a live run while a resting order has no live group.

    Measured 2026-09-16: an unattributed post-only sell rested for sixteen
    minutes and then filled into a naked short. A live run that starts on top of
    an order nobody can account for is a second position with an unknown
    first one.
    """
    scan = scan_pending_orders(read=read, ledger=ledger, now_ms=now_ms,
                               policy=policy)
    if scan.status != "ok":
        return False, [scan.reasons[0] if scan.reasons else "pending_orders_unreadable"], scan.summary()
    if scan.orphans:
        return False, list(scan.reasons), scan.summary()
    return True, [], scan.summary()


def add_arguments(subparsers) -> None:
    parser = subparsers.add_parser(
        "full-chain",
        help="spot-first end-to-end run: plan, then (optionally) both real legs")
    parser.add_argument("--asset", default="XIAOMI")
    parser.add_argument("--size", default="4", help="quote-token notional, e.g. 4 USDG")
    parser.add_argument("--shares", help="trade exactly this many shares (whole-share hedge)")
    parser.add_argument("--instrument", default="XIAOMI-USDT-SWAP")
    parser.add_argument("--mode", choices=["dry", "paper", "live"], default="dry")
    parser.add_argument("--confirm-live", action="store_true",
                        help="required for --mode live; the automation policy still has to allow it")
    parser.add_argument("--close", action="store_true",
                        help="run the closing legs instead of the opening legs")
    parser.add_argument("--token-quantity", help="close: tokens to sell (default: plan)")
    parser.add_argument("--contract-quantity", help="close: contracts to buy back (default: plan)")
    parser.add_argument("--opened-group", help="close: ledger group that opened this position")
    parser.add_argument("--forced", action="store_true",
                        help="plumbing test: run even when the net basis is below "
                             "threshold. It skips the opportunity threshold ONLY — "
                             "kill switch, AUTO_HALT, allowlist, quote freshness, "
                             "balance and position gates still apply")
    parser.add_argument("--taker", action="store_true",
                        help="override the policy and cross the spread (taker IOC) instead of resting")
    parser.add_argument("--config", default="config/hk_monitor.toml")
    parser.add_argument("--policy", default="config/automation_policy.toml")
    parser.add_argument("--okx-env", default="config/okx-demo.env")
    parser.add_argument("--ledger", default=EXECUTION_LEDGER)
    parser.add_argument("--json", action="store_true")


def decimal_arg(value: str) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"invalid decimal: {value}") from exc


def _ensure_allowance(wallet_leg, runner, policy: dict, plan, *, amount: Decimal,
                      ledger: ExecutionLedger, token_address: str | None = None,
                      token_symbol: str | None = None, decimals: int | None = None):
    """Bounded pre-approval for whichever token this leg has to spend."""
    allowance_policy = policy.get("approval") or {}
    cap = Decimal(str(allowance_policy.get("max_allowance_quote", "0")))
    if cap <= 0:
        raise SystemExit("refused: approval cap is missing from the policy")
    outcome = wallet_leg.ensure_allowance(
        runner,
        token_address=token_address or plan.quote_token_address,
        token_symbol=token_symbol or plan.quote_symbol,
        decimals=plan.quote_decimals if decimals is None else decimals,
        amount=amount, cap=cap,
    )
    if plan.intent is not None:
        ledger.append(plan.intent.signal_id, "allowance_ready", "allowance_ready",
                      {"signal_id": plan.intent.signal_id, "allowance": outcome.as_event(),
                       "cap_quote": str(cap)}, plan.intent.created_ts_ms)
    return outcome


# The printer only needs ``state`` and ``payload``; the flows are shared with
# the unattended executor so there is one implementation of each.
_CliRun = FlowResult


def run(args: argparse.Namespace) -> int:
    try:
        stack = build_live_stack(config_path=args.config, policy_path=args.policy,
                                 okx_env=args.okx_env, ledger_path=args.ledger,
                                 live=args.mode == "live" and args.confirm_live)
    except LiveStackError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    config, policy = stack.config, stack.policy
    read, runner, ledger = stack.read, stack.runner, stack.ledger
    wallet_leg, chain = stack.wallet_leg, stack.chain
    try:
        if args.mode == "live":
            # Before anything is priced or signed: what is already resting?
            ok, reasons, scan = _orphan_gate(read, ledger, policy,
                                             int(time.time() * 1000))
            if not ok:
                print("refused: 存在无法归属的挂单，禁止新执行 "
                      f"({'；'.join(reasons)})；先撤单/对账，看板 8766 有挂单区块",
                      file=sys.stderr)
                print(json.dumps({"orphan_scan": scan}, ensure_ascii=False, default=str),
                      file=sys.stderr)
                return 4
        # Read the balance first: sizing must not ask the venue for more than
        # the wallet can pay, or the quote comes back as a funding prompt.
        balances = []
        try:
            balances = stack.balances()
        except Exception as exc:
            print(f"warning: wallet balance unavailable ({exc})", file=sys.stderr)
        quote_balance = stack.quote_balance(balances) if balances else None
        if args.close:
            if not args.contract_quantity and not args.token_quantity:
                print("refused: --close needs --contract-quantity and/or --token-quantity "
                      "(read them from the account board or the opening ledger group); "
                      "pass 0 for the leg that is already flat",
                      file=sys.stderr)
                return 2
            plan = chain.build_close_plan(
                asset=args.asset, perp_instrument=args.instrument,
                token_quantity=decimal_arg(args.token_quantity or "0"),
                contract_quantity=decimal_arg(args.contract_quantity or "0"))
        else:
            plan = chain.build_plan(asset=args.asset, size_quote=decimal_arg(args.size),
                                    perp_instrument=args.instrument, forced=args.forced,
                                    shares=decimal_arg(args.shares) if args.shares else None,
                                    balance_quote=quote_balance)
        # Only the opening path needs the open-contract count for its risk gate.
        # A close must not fail because an unrelated read hiccuped.
        open_contracts = Decimal(0) if args.close else Decimal(len(read.positions()))
        if args.close:
            positions = _read_positions(read)
            try:
                pending = list(read.pending_orders())
            except Exception as exc:  # noqa: BLE001 - the gate reports it
                print(f"warning: 挂单读取失败（{type(exc).__name__}）", file=sys.stderr)
                pending = None
            style = str((policy.get("orders") or {}).get("style") or "taker_both_legs")
            tokens = (decimal_arg(args.token_quantity) if args.token_quantity
                      else plan.expected_token_quantity)
            contracts = (decimal_arg(args.contract_quantity) if args.contract_quantity
                         else plan.perp_contract_quantity)
            if args.mode == "live" and contracts > 0 and not positions:
                # The buy-back size has to be checked against a real short. If
                # the position read failed, "no positions" and "unknown
                # positions" look the same, and one of them is a reason to stop.
                print("refused: 合约持仓读取为空/失败，平仓闸门不凭命令行数量放行",
                      file=sys.stderr)
                return 4
            approval = None
            if args.mode == "live":
                approval = _ensure_allowance(wallet_leg, runner, policy, plan,
                                             amount=decimal_arg(args.token_quantity)
                                             if args.token_quantity
                                             else plan.expected_token_quantity,
                                             ledger=ledger,
                                             token_address=plan.token_address,
                                             token_symbol=plan.token_symbol,
                                             decimals=plan.token_decimals)
                print(f'授权（现货卖出）: {approval.action} spender={approval.spender} '
                      f'{approval.spendable_before} → {approval.spendable_after}', file=sys.stderr)
            if args.mode == "live" and not args.taker and style == "maker_first":
                # Same routing rule as opening: the policy file picks the style,
                # and --taker is the explicit override that crosses the spread.
                result = _maker_first_close(
                    chain, plan, policy, ledger, contracts=contracts, tokens=tokens,
                    opened_group_id=args.opened_group,
                    positions=positions, pending_orders=pending,
                    # The wallet's own balance, so a retried spot sale can only
                    # ever sell what is still there.
                    balance_source=lambda address: wallet_leg.balance_of(address, runner))
            else:
                # The taker path runs the same gate the maker path runs, so the
                # `--taker` flag cannot become a way around the close checks.
                allowed, reasons = chain.risk_check_close(
                    plan, contracts=contracts, tokens=tokens,
                    positions=positions, pending_orders=pending)
                if not allowed:
                    print(f"refused: 平仓风控未通过 ({','.join(reasons)})",
                          file=sys.stderr)
                    return 4
                result = chain.execute_close(
                    plan, live=args.mode == "live",
                    token_quantity=(decimal_arg(args.token_quantity)
                                    if args.token_quantity else None),
                    contract_quantity=(decimal_arg(args.contract_quantity)
                                       if args.contract_quantity else None),
                    opened_group_id=args.opened_group,
                )
        else:
            if args.mode == "live":
                approval = _ensure_allowance(wallet_leg, runner, policy, plan,
                                             amount=plan.size_quote, ledger=ledger)
                print(f'授权（买入支付币）: {approval.action} spender={approval.spender} '
                      f'{approval.spendable_before} → {approval.spendable_after}', file=sys.stderr)
            style = str((policy.get("orders") or {}).get("style") or "taker_both_legs")
            if args.mode == "live" and not args.taker and style == "maker_first":
                result = _maker_first_open(chain, plan, policy, ledger,
                                           balance_quote=quote_balance,
                                           open_contracts=open_contracts)
            else:
                result = chain.execute_open(plan, live=args.mode == "live",
                                            balance_quote=quote_balance,
                                            open_contracts=open_contracts)
    except FullChainError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    finally:
        ledger.close()
    payload = result.summary()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        _print_human(payload, args)
    if args.mode == "live" and str(result.state) in REVIEW_STATES:
        _notify_manual_review(config, payload, result.state)
    return 0 if result.state in {"dry_run_complete", "hedged", "closed"} else 1


def _notify_manual_review(config: dict, payload: dict, state: str) -> None:
    """Tell a human on the alert channel when a leg could not be finished.

    A state that needs review is exactly the case where nobody is watching the
    terminal any more: the run is over and something is still stranded. The
    message is best-effort — a broken notification channel must not change the
    exit code or hide the result that was already printed.
    """
    from arb_monitor.ops.notify import FeishuNotifier, format_manual_review

    plan = payload.get("plan") or {}
    unwind = payload.get("unwind") or {}
    maker = payload.get("maker") or {}
    detail = (
        f'挂单 ordId={maker.get("order_id")} 挂 {maker.get("price")} '
        f'成交 {maker.get("filled")}/{maker.get("requested")} 状态 {maker.get("state")}'
        f'；未对冲 {payload.get("unhedged_tokens")}'
        f'；兜底 {payload.get("fallback") or "未触发"}'
        f'；回补 {unwind.get("state") or "无"}'
    )
    text = format_manual_review(
        asset=str(plan.get("asset") or "?"),
        instrument=str(plan.get("perp") or "?"), state=state,
        reason=str(payload.get("reason") or ""), detail=detail,
        group_id=payload.get("group_id"))
    try:
        # audience="alert": a stranded leg belongs in the operational room, not
        # the opportunity channel. It falls back to the signal channel (and says
        # so) when no alert destination is configured.
        delivery = FeishuNotifier(config.get("notify") or {},
                                  audience="alert").send(text)
    except Exception as exc:  # noqa: BLE001 - the printed result is the record
        print(f"人工审核通知发送失败: {type(exc).__name__}", file=sys.stderr)
        return
    print(f'人工审核通知: ok={delivery.get("ok")} channel={delivery.get("channel")} '
          f'detail={str(delivery.get("detail"))[:120]}', file=sys.stderr)


def _print_human(payload: dict, args: argparse.Namespace) -> None:
    plan = payload.get("plan") or {}
    print(f'== full-chain {args.mode} · {plan.get("asset")} · {plan.get("size_quote")} '
          f'{plan.get("quote_token")} ==')
    if args.mode == "live":
        print("⚠ MEV 保护：X Layer 私有通道未验证，本次走钱包默认广播（已在账本记录）")
    print(f'链上买入 {plan.get("spot_token")} CA={plan.get("spot_token_address")}')
    print(f'  支付币 CA={plan.get("quote_token_address")}')
    print(f'  链上价 {plan.get("spot_ask_usd")} USD/枚 → 整股买入 '
          f'{plan.get("share_count")} 股（exact-in 金额搜索，预算上限 '
          f'{plan.get("budget_quote")} {plan.get("quote_token")}，预计花 '
          f'{plan.get("size_quote")}）')
    print(f'永续 {plan.get("perp")} Bid={plan.get("perp_bid_usd")} '
          f'→ taker IOC 限价 {plan.get("perp_limit_price")}')
    print(f'  毛基差 {plan.get("gross_entry_basis_bps")} bps − 成本 '
          f'{plan.get("estimated_entry_cost_bps")} bps = 净 {plan.get("net_entry_basis_bps")} bps '
          f'(阈值 {plan.get("min_net_edge_bps")} bps)')
    print(f'  对冲数量 {plan.get("perp_contract_quantity")} 张 = 买入股数，取整残差 0')
    for warning in plan.get("warnings") or []:
        print(f'  ⚠ {warning}')
    risk = (payload.get("audit") or {}).get("risk") or {}
    print(f'风控: {"通过" if risk.get("allowed") else "拒绝 " + ",".join(risk.get("reasons") or [])}')
    print(f'结果: {payload.get("state")} {payload.get("reason") or ""}')
    if payload.get("spot"):
        spot = payload["spot"]
        print(f'  现货: tx={spot.get("tx_hash")} 状态={spot.get("receipt_status")} '
              f'数量={spot.get("token_quantity")} 实际价={spot.get("price_usd")} '
              f'gas={spot.get("gas_paid_native")}')
    if payload.get("perp_order"):
        print(f'  合约: {json.dumps(_single(payload["perp_order"]), ensure_ascii=False)}')
    if payload.get("maker"):
        maker = payload["maker"]
        label = "合约挂单(卖)" if (payload.get("audit") or {}).get("order_style") != "maker_close" \
            else "合约挂单(买)"
        print(f'  {label}: ordId={maker.get("order_id")} 挂 {maker.get("price") or ""} '
              f'请求 {maker.get("requested")} 张 → 成交 {maker.get("filled")} 张 '
              f'({maker.get("wait_ms")}ms, 状态 {maker.get("state")}'
              f'{", 已撤单" if maker.get("cancelled") else ""})')
    if payload.get("fallback"):
        print(f'  超时兜底: {json.dumps(payload["fallback"], ensure_ascii=False)}')
    if payload.get("unwind"):
        print(f'  ⚠ 现货失败后回补: {json.dumps(payload["unwind"], ensure_ascii=False)}')
    if payload.get("unhedged_tokens") not in (None, "0"):
        print(f'  未对冲: {payload.get("unhedged_tokens")} 枚 {payload.get("hedge_hint") or ""}')


def _single(order: dict) -> dict:
    return {k: order.get(k) for k in ("ordId", "sCode", "sMsg") if k in order} or order
