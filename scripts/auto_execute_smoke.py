#!/usr/bin/env python
"""Run the unattended chain once, by hand, against the live market.

Why this exists: the executor only fires when the market offers a signal, and
the market does not offer one on request. This runs the *same* chain — re-quote
at execution time, risk gate, ledger, Feishu result — on a candidate built from
the current real quotes, with the entry bar marked down so the trigger passes.

What is real: every price, the whole-share sizing, the risk gate, the ledger
event, the notification, and (in paper mode) the fact that nothing is signed.
What is not real: the signal. The script says so in its own output, and it
refuses to run live orders unless the policy itself preauthorises them.

    PYTHONPATH=src .venv/bin/python scripts/auto_execute_smoke.py --mode paper
    PYTHONPATH=src .venv/bin/python scripts/auto_execute_smoke.py --mode live \
        --confirm-live     # only if [execution] already says preauthorized_program
"""
from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from arb_monitor.apps.hk_monitor import (  # noqa: E402
    HKMonitor, candidate_roundtrip, costs_from_config, load_config,
)
from arb_monitor.core.hk import decimal  # noqa: E402
from arb_monitor.execution.auto_execute import AutoExecutor  # noqa: E402
from arb_monitor.execution.config import load_env_file  # noqa: E402
from arb_monitor.ops.notify import FeishuNotifier  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["paper", "live"], default="paper")
    parser.add_argument("--asset", default="XIAOMI")
    parser.add_argument("--config", default="config/hk_monitor.toml")
    parser.add_argument("--policy", default="config/automation_policy.toml")
    parser.add_argument("--notify-env", default="config/notify.env")
    parser.add_argument("--proxy", default=os.environ.get("ARB_HTTP_PROXY")
                        or "http://127.0.0.1:4780")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--bar-offset-bps", default="5",
                        help="how far below the live gross basis the fabricated bar sits")
    parser.add_argument("--dry", action="store_true",
                        help="print the decision without touching the ledger or Feishu")
    parser.add_argument("--prefer-real-candidate", action="store_true",
                        help="use a candidate the monitor really produced, if there is one")
    args = parser.parse_args()

    if args.notify_env and os.path.exists(args.notify_env):
        for key, value in load_env_file(args.notify_env).items():
            os.environ.setdefault(key, value)

    config = load_config(args.config)
    policy = load_config_policy(args.policy, config)
    execution = policy.get("execution") or {}
    monitor = HKMonitor(config, proxy=args.proxy)
    try:
        summary = monitor.cycle(quote_enabled=True)
        rows = [row for row in summary.get("token_references") or []
                if row.get("asset") == args.asset]
        if not rows:
            print(f"no on-chain reference row for {args.asset}", file=sys.stderr)
            return 2
        direction = (rows[0].get("directions") or {}).get("long_spot_short_perp") or {}
        real = ([row for row in (summary.get("candidates") or [])
                 if row.get("asset") == args.asset]
                if args.prefer_real_candidate else [])
        candidate, roundtrip, fabricated_bar = (real[0] if real else None), None, None
        if candidate is not None:
            print("使用监控真正产生的候选（未伪造信号）")
        perp = direction.get("perp") or {}
        if not perp.get("instrument"):
            print(f"{args.asset} has no perpetual to hedge with right now",
                  file=sys.stderr)
            return 2
        costs = costs_from_config(config)
        if not real:
            roundtrip = candidate_roundtrip(
                row=rows[0], direction=direction, costs=costs,
                target=config.get("scenario", {}).get("target_exit_basis_bps"),
                min_edge_bps=decimal(config["monitor"]["min_net_edge_bps"]),
                buffer_bps=decimal(str((config.get("strategy") or {})
                                       .get("edge_buffer_bps", "0"))),
                expected_profit_bps=decimal(str((config.get("strategy") or {})
                                                .get("expected_profit_bps", "0"))),
                perp_style=("maker" if str((policy.get("orders") or {}).get("style"))
                            == "maker_first" else "taker"))
            gross = direction.get("gross_entry_basis_bps") or Decimal(0)
            # The fabricated part: the bar is placed just under the live gross
            # basis so the trigger and the re-quote both pass. Printed, never
            # hidden, and the plumbing flag below says the rest of it is real.
            fabricated_bar = Decimal(str(gross)) - Decimal(args.bar_offset_bps)
            candidate = {
                "asset": rows[0].get("asset"),
                "perp_venue": perp.get("venue"),
                "perp_instrument": perp.get("instrument"),
                "gross_entry_basis_bps": gross,
                "roundtrip": {**roundtrip, "breakeven_gross_bps": fabricated_bar},
                "mapping_verified": bool((rows[0].get("mapping") or {}).get("verified")),
                "cost_verified": costs.verified,
            }
        order_mode = "paper" if args.dry else args.mode
        if args.dry:
            print("--dry: decision only, no ledger write, no Feishu, no order")
        if order_mode == "live" and not args.confirm_live:
            print("refused: --mode live needs --confirm-live", file=sys.stderr)
            return 2
        effective = dict(policy)
        effective["execution"] = {
            **execution,
            "mode": "preauthorized_program",
            "auto_orders": "off" if args.dry else order_mode,
            "policy_path": args.policy,
        }
        notifier = None if args.dry else FeishuNotifier(config.get("notify") or {})
        if notifier is not None:
            notifier = ReportingNotifier(notifier)
        executor = AutoExecutor(policy=effective, monitor_config=config,
                                notifier=notifier,
                                force_plumbing_test=not real)
        print(f"== auto-execute smoke · {args.asset} · {order_mode} ==")
        if real:
            print(f"真实候选：毛基差 {candidate.get('gross_entry_basis_bps')} bps · "
                  f"门槛 {(candidate.get('roundtrip') or {}).get('breakeven_gross_bps')} bps")
        else:
            print(f"链上 Ask {rows[0].get('onchain_spot_ask_usd')} · "
                  f"永续 {perp.get('venue')}:{perp.get('instrument')} Bid "
                  f"{direction.get('perp_price_usd')}")
            print(f"真实毛基差 {gross} bps · 真实往返成本 {roundtrip.get('total_bps')} bps · "
                  f"本次伪造门槛 {fabricated_bar} bps（smoke test，不是市场信号；"
                  f"已开启 forced_plumbing_test）")
        print(f"policy mode={execution.get('mode')} auto_orders={execution.get('auto_orders')} "
              f"→ 本次以 {order_mode} 运行"
              + ("" if real else "（伪造门槛之下，见上一行）"))
        record = executor.maybe_execute([candidate])
        for attempt in record.get("attempts") or []:
            print(f"执行: state={attempt.get('state')} executed={attempt.get('executed')} "
                  f"halt={attempt.get('halt')} reason={attempt.get('reason')}")
            plan = attempt.get("plan") or {}
            if plan:
                print(f"  重取价: 链上 {plan.get('spot_ask_usd')} · 永续 Bid "
                      f"{plan.get('perp_bid_usd')} · 毛基差 {plan.get('gross_entry_basis_bps')} bps")
        print(f"skipped={record.get('skipped')} reason={record.get('reason')}")
        print(f"当日累计已用额度 {record.get('daily_used_quote')} / "
              f"{executor.limits.get('daily_quote_usdt')} {args.asset}")
        return 0
    finally:
        monitor.close()


def load_config_policy(path: str, config: dict) -> dict:
    """The execution policy, with the monitor's own copy serving as a fallback."""
    from arb_monitor.apps.account_dashboard import load_toml
    return load_toml(path) or config.get("execution_policy") or {}


class ReportingNotifier:
    """Wrap the real notifier so the smoke run can show what the channel said.

    The chain's notification is part of what is being verified: an execution
    nobody is told about is not finished. Printing the delivery result is how
    the smoke run proves the message left the building rather than merely
    reaching the notifier object.
    """

    def __init__(self, inner):
        self.inner = inner

    def send(self, text: str) -> dict:
        result = self.inner.send(text)
        head = text.splitlines()[0] if text else ""
        print(f"飞书投递: ok={result.get('ok')} channel={result.get('channel')} "
              f"detail={str(result.get('detail'))[:120]}")
        print(f"  首行: {head}")
        return result


if __name__ == "__main__":
    raise SystemExit(main())
