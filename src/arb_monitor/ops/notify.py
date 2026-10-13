"""Alert gate and delivery. Notification only; SQLite stays the source of truth.

Two rules keep this channel useful rather than noisy:

* a signal must have a positive net entry basis above the configured minimum,
  so a negative basis is never announced (it is not executable);
* the same asset+venue pair is not re-announced within a cooldown window.

Delivery supports a Feishu custom-bot webhook and the locally configured
``lark-cli`` (bot identity). Secrets are read from the environment, never from
the committed config file. Delivery failure is recorded, never raised: a broken
notification channel must not stop market sampling.
"""
from __future__ import annotations

from decimal import Decimal
import json
import os
import subprocess


class AlertGate:
    """Decides which candidates deserve a notification, and when."""

    def __init__(self, min_net_edge_bps: Decimal, cooldown_seconds: int,
                 max_per_cycle: int = 3,
                 unexecutable_cooldown_seconds: int | None = None):
        self.min_net_edge_bps = Decimal(str(min_net_edge_bps))
        self.cooldown_ms = int(cooldown_seconds) * 1000
        self.max_per_cycle = int(max_per_cycle)
        # A candidate that cannot be ordered is information, not a call to
        # action. Measured 2026-09-16: KUAISHOU cleared the basis bar while
        # sitting on Gate with an unverified mapping, and the 15-minute cooldown
        # produced seven near-identical alerts in 100 minutes. The operator's
        # instruction was to stretch that to four hours; actionable candidates
        # keep the short cooldown, because those are the ones worth acting on.
        self.unexecutable_cooldown_ms = int(
            unexecutable_cooldown_seconds if unexecutable_cooldown_seconds is not None
            else cooldown_seconds) * 1000
        self.last_sent_ms: dict[str, int] = {}
        # Kept apart from last_sent_ms so a candidate that *becomes* executable
        # is not silenced by the long cooldown of its observation phase: it only
        # has to clear the normal one.
        self.last_unexecutable_ms: dict[str, int] = {}

    @staticmethod
    def key(candidate: dict) -> str:
        return f'{candidate.get("asset")}@{candidate.get("perp_venue")}:{candidate.get("perp_instrument")}'

    def hydrate(self, rows: list[dict]) -> None:
        """Seed cooldowns from stored alerts so a restart is not a re-send."""
        for row in rows:
            key = row.get("key")
            sent = row.get("recv_ts_ms")
            if key and sent:
                self.last_sent_ms[key] = max(self.last_sent_ms.get(key, 0), int(sent))
                if row.get("execution_blockers"):
                    self.last_unexecutable_ms[key] = max(
                        self.last_unexecutable_ms.get(key, 0), int(sent))

    def evaluate(self, candidates: list[dict], now_ms: int) -> list[dict]:
        pending = []
        for candidate in candidates:
            # The bar is "this trade makes money after everything we can measure
            # plus an allowance and a margin", not "the entry basis clears a flat
            # 20 bps". The candidate carries the computed requirement; the flat
            # threshold stays as the fallback when a round-trip cost is missing.
            required = (candidate.get("roundtrip") or {}).get("breakeven_gross_bps")
            if required is not None:
                gross = candidate.get("gross_entry_basis_bps")
                if gross is None or Decimal(str(gross)) < Decimal(str(required)):
                    continue
            else:
                net = candidate.get("estimated_net_entry_bps")
                if net is None or Decimal(str(net)) <= 0:
                    continue
                if Decimal(str(net)) < self.min_net_edge_bps:
                    continue
            key = self.key(candidate)
            unexecutable = candidate.get("executable") is False
            last = self.last_sent_ms.get(key)
            if last is not None and now_ms - last < self.cooldown_ms:
                continue
            if unexecutable:
                last_observation = self.last_unexecutable_ms.get(key)
                if (last_observation is not None
                        and now_ms - last_observation < self.unexecutable_cooldown_ms):
                    continue
            pending.append({**candidate, "alert_key": key,
                            "min_net_edge_bps": str(self.min_net_edge_bps),
                            "required_gross_basis_bps": (str(required)
                                                         if required is not None else None)})
            if len(pending) >= self.max_per_cycle:
                break
        return pending

    def mark_sent(self, alert: dict, now_ms: int) -> None:
        key = alert.get("alert_key") or self.key(alert)
        self.last_sent_ms[key] = now_ms
        if alert.get("execution_blockers"):
            self.last_unexecutable_ms[key] = now_ms


def format_alert(alert: dict, evaluated_at_ms: int) -> str:
    """Absolute prices, 5 decimals, and what each notional would really get.

    Plain text on purpose: no Markdown rewriting of numbers. The CJ handbook's
    first rule is that a spread must be quoted at tradable depth, so the message
    carries the actual execution price and extra slippage per configured size
    instead of a single mid-price bps. Its second rule is that liquidation
    slippage is where these trades die, which is stated explicitly rather than
    implied.

    The on-chain leg is identified by contract address, not by ticker: one
    ticker can exist as several wrappers on several chains, and an operator
    acting on this alert has to be able to check the exact token they buy.
    """

    def num(value, places: int = 5) -> str:
        if value is None:
            return "—"
        return f"{Decimal(str(value)):.{places}f}"

    def bps(value, places: int = 1) -> str:
        if value is None:
            return "—"
        return f"{Decimal(str(value)):+.{places}f}"

    asset = alert.get("asset")
    venue = f'{alert.get("perp_venue")}:{alert.get("perp_instrument")}'
    spot_ask = alert.get("spot_ask_usd")
    perp_bid = alert.get("perp_bid_usd")
    spread = None
    if spot_ask is not None and perp_bid is not None:
        spread = Decimal(str(perp_bid)) - Decimal(str(spot_ask))
    token_symbol = alert.get("token_symbol") or "—"
    token_address = alert.get("token_address") or "—"
    chain_id = alert.get("chain_id") or "—"
    quote_symbol = alert.get("quote_token_symbol") or "—"
    quote_address = alert.get("quote_token_address") or "—"
    pool_address = alert.get("pool_address") or "—"
    lines = [
        "【套利监控】现货多 + 永续空 · 净基差达到阈值",
        f"标的 {asset}（链上代币 {token_symbol}）",
        "",
        "链上合约地址",
        f"  买入代币 {token_symbol}: {token_address}",
        f"  支付币 {quote_symbol}: {quote_address}",
        f"  池子: {pool_address}",
        f"  链: X Layer (chain {chain_id})"
        f" · https://www.oklink.com/xlayer/token/{token_address}",
        "",
        "绝对价格",
        f"  买入现货（链上 Ask）: {num(spot_ask)} USD/枚",
        f"  卖出永续（{venue} Bid）: {num(perp_bid)} USD/枚",
        f"  价差: {num(spread)} USD/枚"
        f"（{bps(alert.get('gross_entry_basis_bps'))} bps 毛，"
        f"扣开仓成本 {num(alert.get('estimated_entry_cost_bps'), 1)} bps 后 "
        f"{bps(alert.get('estimated_net_entry_bps'))} bps，"
        f"阈值 {num(alert.get('min_net_edge_bps'), 0)} bps）",
        f"  成本是否已验证: {'是' if alert.get('cost_verified') else '否（默认保守值）'}",
        f"  份额映射是否已验证: {'是（1 token = 1 share = 1 张合约）' if alert.get('mapping_verified') else '否（wrapper/share 比例未经核验）'}",
    ]
    # Whether this can actually be ordered, in the message itself. Operator's
    # question on 2026-09-16 ("是不是出了交易信号了") came from an alert that
    # cleared the basis bar while sitting on a venue with no execution adapter
    # and an unverified mapping — the message said "净基差达到阈值" and nothing
    # about that. A signal an operator cannot act on must say so, or it teaches
    # them to ignore the channel.
    blockers = alert.get("execution_blockers")
    if blockers is None:
        blockers = []
        if not alert.get("mapping_verified"):
            blockers.append("份额映射未核验")
    if blockers:
        lines += [
            "",
            "⚠ 不可执行（本条只是观察，不是下单信号）",
            f"  原因: {'；'.join(blockers)}",
            "  说明: 只有「执行白名单 ∩ 已验证映射 ∩ 已接入适配器」的合约才可能被下单；"
            "本项目的执行白名单只含 OKX 的 XIAOMI / SHEIN / POPMART 永续。",
        ]
    else:
        lines += ["", "✓ 该候选的交易所/合约/映射都在执行白名单内"]
    if alert.get("mapping_evidence"):
        lines.append(f"  · 映射依据: {alert.get('mapping_evidence')}")
    breakdown = alert.get("cost_breakdown") or {}
    if breakdown:
        # Show what the cost number is made of, so an over- or under-estimate
        # can be argued with instead of being taken on faith.
        lines += [
            "",
            "开仓成本构成（单边，逐项来源见 config/hk_monitor.toml [costs]）",
            f"  · 链上腿费率: {num(breakdown.get('spot_fee_bps'), 1)} bps"
            "（池费已并入链上 Ask，不重复计）",
            f"  · 永续腿 taker 手续费: {num(breakdown.get('perp_fee_bps'), 1)} bps",
            f"  · 滑点缓冲: {num(breakdown.get('slippage_bps'), 1)} bps"
            "（本轮实测报价=成交，0 bps，保留缓冲）",
            f"  · 失败缓冲: {num(breakdown.get('failure_bps'), 1)} bps",
            f"  · Gas: {num(breakdown.get('gas_bps'), 2)} bps"
            "（X Layer receipt 实测 effectiveGasPrice=0）",
            f"  · 合计: {num(breakdown.get('total_bps'), 1)} bps",
        ]
        if alert.get("cost_evidence"):
            lines.append(f"  · 校准依据: {alert.get('cost_evidence')}")
    lines += [
        "",
        f"深度与执行（池内深度 {num(alert.get('pool_depth_usd'), 0)} USD，"
        f"池费 {num(alert.get('pool_fee_bps'), 1)} bps）",
    ]
    roundtrip = alert.get("roundtrip") or {}
    if roundtrip:
        # Half of the cost of a basis trade is on the way out; showing only the
        # entry number made a "+23 bps" signal look better than it is.
        maker = str(roundtrip.get("perp_style") or "taker") == "maker"
        lines += [
            "",
            "一次往返总成本（开仓 + 平仓，按会成交的那一侧计价"
            + ("；永续双腿 maker 挂单）" if maker else "）"),
            f"  · 现货价差（买 Ask / 卖 Bid）: {num(roundtrip.get('spot_spread_bps'), 1)} bps",
            f"  · 永续手续费（{'maker' if maker else 'taker'} × 2 腿）: "
            f"{num(roundtrip.get('perp_fee_bps'), 1)} bps",
            f"  · 永续价差（Bid/Ask）: {num(roundtrip.get('perp_spread_bps'), 1)} bps"
            + ("（挂单不吃价差，按 0 计）" if maker else ""),
            f"  · Gas: {num(roundtrip.get('gas_bps'), 1)} bps",
            f"  · 合计: {num(roundtrip.get('total_bps'), 1)} bps",
        ]
        if roundtrip.get("breakeven_gross_bps") is not None:
            lines.append(
                f"  · 出手门槛：毛基差 ≥ {num(roundtrip.get('breakeven_gross_bps'), 1)} bps"
                f"（往返成本 + 磨损余量 {num(roundtrip.get('buffer_bps'), 1)} bps"
                f" + 期望获利 {num(roundtrip.get('expected_profit_bps'), 1)} bps）")
        if not roundtrip.get("complete", True):
            lines.append("  · 有分项缺失（缺一侧盘口），合计按不完整处理，不要当成准确值")
    plan = alert.get("size_plan") or []
    if plan:
        lines.append("  金额        实际买入价      额外滑点    占池深  扣开仓成本后(开仓口径)  单笔毛利")
        for row in plan:
            verdict = "够" if row.get("depth_ok") else "偏薄"
            lines.append(
                f"  {num(row.get('size_usd'), 0):>7} USD  {num(row.get('spot_ask_usd'))}"
                f"  {bps(row.get('extra_slippage_bps')):>9} bps"
                f"  {num(row.get('depth_share_pct'), 3):>6}%"
                f"  {bps(row.get('net_entry_basis_bps')):>14} bps"
                f"  {num(row.get('gross_profit_quote'), 2):>9} USD  ({verdict})")
        ok_sizes = [row["size_usd"] for row in plan if row.get("depth_ok")]
        if ok_sizes:
            lines.append(f"  深度结论: 到 {num(max(ok_sizes), 0)} USD 为止冲击可接受；"
                         f"再往上滑点开始吃掉基差")
        else:
            lines.append("  深度结论: 没有一档满足滑点上限，这个池子太薄，不建议交易")
        if roundtrip.get("breakeven_gross_bps") is not None:
            # The rows above subtract only the entry cost. Say plainly whether
            # any of them clears the whole cycle, because that is the decision.
            lines.append(
                f"  ⚠ 上面的净基差只扣了开仓成本；按一次往返 {num(roundtrip.get('total_bps'), 1)} bps"
                f" 计, 毛基差需 ≥ {num(roundtrip.get('breakeven_gross_bps'), 1)} bps 才值得做"
                f"（当前毛基差 {bps(alert.get('gross_entry_basis_bps'))} bps）")
    else:
        lines.append("  无池内可执行价（该标的是成交打印价或展示价），无法给出分档滑点")
    lines += [
        "",
        "提醒",
        f"  · 链上买价来源: {alert.get('spot_source') or '—'}",
        "  · 「一次往返总成本」才是这笔交易要跨过的门槛：它已含平仓那一侧的两条价差和手续费",
        "    （永续不可跨所搬迁，必须本地平仓）。",
        "  · 不是可执行报价，也不代表锁定利润。",
        f"  · 采样时间(ms): {evaluated_at_ms}",
    ]
    return "\n".join(lines)


def format_manual_review(*, asset: str, instrument: str, state: str, reason: str,
                         detail: str = "", group_id: str | None = None) -> str:
    """Ask a human to look at a stranded leg, on the alert channel.

    Different from an opportunity alert on purpose: this message is only sent
    when a leg could not be finished, and it names the exact state so the
    operator can go straight to the account board instead of re-deriving what
    happened. The three things that matter are: what is stranded, what the
    machine already tried, and what to check.
    """
    stranded = {
        "manual_intervention_required":
            "合约已平、现货仍在（多头裸露，方向温和但未完成）",
        "maker_unknown": "永续挂单状态未确认（可能仍在盘口，随时可能成交）",
        "close_perp_underfilled": "永续买回未满，现货腿已保留（两腿仍对冲）",
        "spot_not_confirmed": "现货腿未确认（未提交合约腿，无裸腿）",
        "unhedged_spot_only": "现货已买入但未对冲（裸多现货）",
        "close_unknown": "平仓现货腿状态未知",
        "maker_order_rejected": "永续挂单未建立（仓位未变动）",
    }.get(state, "执行未达终态")
    lines = [
        f"【人工审核】{asset} · {state}",
        f"标的/合约: {asset} · {instrument}",
        f"情况: {stranded}",
        f"原因: {reason or '—'}",
    ]
    if detail:
        lines.append(f"细节: {detail}")
    if group_id:
        lines.append(f"账本组: {group_id}")
    lines += [
        "",
        "请核对（账户看板 8766 / OKX / X Layer 钱包）：",
        "  1) 永续持仓与挂单是否已归零；2) 现货代币余额还剩多少；",
        "  3) 两腿是否配平；4) 处理完成后用 arb_monitor ledger-reconcile 如实补记。",
        "自动执行若在运行，已写入 data/AUTO_HALT 并停止，对账后删除该文件再恢复。",
    ]
    return "\n".join(lines)


class FeishuNotifier:
    """Delivery via webhook and/or the locally configured lark-cli.

    ``channel`` accepts a comma separated list, e.g. ``"webhook,lark_cli"``, so
    a group bot and a direct message can both be used. A channel without the
    credentials it needs (webhook URL, lark target) is dropped instead of
    failing the whole send.

    **Two audiences.** ``audience="signal"`` is the opportunity channel: "there
    is a basis worth looking at". ``audience="alert"`` is the operational one:
    a stranded leg, an automatic halt, an unspent gas sponsorship. The operator
    asked for these to be separate rooms on 2026-09-16, because a channel that
    mixes "maybe trade this" with "a human has to fix this now" gets skimmed.

    An alert audience with no configured destination **falls back to the signal
    channel and says so** in its delivery detail. Alerts are never silently
    dropped: a missing alert webhook degrades the routing, not the alert.
    """

    def __init__(self, settings: dict, runner=subprocess.run, http_post=None,
                 audience: str = "signal"):
        self.settings = settings or {}
        self.runner = runner
        self.http_post = http_post
        self.audience = "alert" if str(audience) == "alert" else "signal"
        self.fell_back_to_signal = False

    # --- which room ------------------------------------------------------
    def _resolved(self) -> dict:
        """The settings block for this audience, falling back when empty."""
        if self.audience != "alert":
            return self.settings
        alert = {
            key: self.settings.get(f"alert_{key}")
            for key in ("channel", "webhook_url", "lark_chat_id", "lark_user_id",
                        "lark_identity")
        }
        has_channel = str(alert.get("channel") or "").strip()
        has_webhook = bool(os.environ.get("ARB_FEISHU_ALERT_WEBHOOK")
                           or str(alert.get("webhook_url") or ""))
        has_lark = bool(alert.get("lark_chat_id") or alert.get("lark_user_id"))
        if has_channel and (has_webhook or has_lark):
            return alert
        self.fell_back_to_signal = True
        return self.settings

    @property
    def channels(self) -> list[str]:
        settings = self._resolved()
        requested = [part.strip() for part in
                     str(settings.get("channel", "off")).split(",") if part.strip()]
        usable = []
        for channel in requested:
            if channel == "webhook" and not self._webhook_url():
                continue
            if channel == "lark_cli" and not (
                settings.get("lark_chat_id") or settings.get("lark_user_id")
            ):
                continue
            if channel in {"webhook", "lark_cli"}:
                usable.append(channel)
        return usable

    @property
    def channel(self) -> str:
        usable = self.channels
        return ",".join(usable) if usable else "off"

    def _webhook_url(self) -> str:
        settings = self._resolved()
        if self.audience == "alert":
            return (os.environ.get("ARB_FEISHU_ALERT_WEBHOOK", "")
                    or str(settings.get("webhook_url") or "")
                    or os.environ.get("ARB_FEISHU_WEBHOOK", ""))
        return (os.environ.get("ARB_FEISHU_WEBHOOK", "")
                or str(settings.get("webhook_url", "")))

    def send(self, text: str) -> dict:
        channels = self.channels
        if not channels:
            return {"ok": False, "channel": "off", "detail": "no_channel_configured"}
        deliveries = []
        for channel in channels:
            result = (self._send_webhook(text) if channel == "webhook"
                      else self._send_lark_cli(text))
            deliveries.append(result)
        # "ok" means at least one channel accepted it, so the cooldown is
        # honoured; every attempt is still reported separately.
        return {
            "ok": any(d["ok"] for d in deliveries),
            "channel": ",".join(channels),
            "audience": self.audience,
            "fell_back_to_signal": self.fell_back_to_signal,
            "deliveries": deliveries,
            "detail": "; ".join(f'{d["channel"]}:{d["detail"][:120]}' for d in deliveries)[:300],
        }

    def _send_lark_cli(self, text: str) -> dict:
        settings = self._resolved()
        target = []
        if settings.get("lark_chat_id"):
            target = ["--chat-id", str(settings["lark_chat_id"])]
        elif settings.get("lark_user_id"):
            target = ["--user-id", str(settings["lark_user_id"])]
        else:
            return {"ok": False, "channel": "lark_cli", "detail": "missing_lark_target"}
        argv = ["lark-cli", "im", "+messages-send", *target, "--text", text,
                "--as", str(settings.get("lark_identity", "bot"))]
        try:
            proc = self.runner(argv, capture_output=True, text=True, timeout=20)
        except Exception as exc:  # missing binary, timeout, permissions
            return {"ok": False, "channel": "lark_cli",
                    "detail": f"{type(exc).__name__}:{str(exc)[:120]}"}
        detail = (proc.stdout or proc.stderr or "").strip()[:300]
        return {"ok": proc.returncode == 0, "channel": "lark_cli", "detail": detail}

    def _send_webhook(self, text: str) -> dict:
        payload = {"msg_type": "text", "content": {"text": text}}
        try:
            if self.http_post is not None:
                status, body = self.http_post(self._webhook_url(), payload)
            else:
                import urllib.request
                request = urllib.request.Request(
                    self._webhook_url(), data=json.dumps(payload).encode(),
                    headers={"content-type": "application/json"})
                with urllib.request.urlopen(request, timeout=15) as response:
                    status, body = response.status, response.read().decode()[:300]
        except Exception as exc:
            return {"ok": False, "channel": "webhook",
                    "detail": f"{type(exc).__name__}:{str(exc)[:120]}"}
        return {"ok": 200 <= int(status) < 300, "channel": "webhook", "detail": str(body)[:300]}
