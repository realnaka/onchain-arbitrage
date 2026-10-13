"""The reviewed order flows, in one place.

The interactive CLI and the unattended executor have to run *the same* flow:
same risk gate, same maker parameters read from the same policy section, same
ledger events. Two copies of this logic would eventually disagree about a
timeout or a fallback, and the copy that runs unattended is the one nobody is
watching. So both call these functions.

Every function returns a :class:`FlowResult`: a state name and the payload the
caller renders or records. Unknown and manual states are values here, not
exceptions: an unreadable order is an outcome the caller has to handle, and
turning it into a stack trace would only hide it.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import uuid

from .maker_first import MakerFirstCloser, MakerFirstOpener

# States that mean "a human has to look before anything else is automated".
HALT_STATES = frozenset({
    "maker_unknown",              # order state unreadable: may be live
    "manual_intervention_required",  # spot leg left open, deliberately not retried
    "close_unknown",              # spot sale not confirmed
    "spot_not_confirmed",         # spot receipt not filled before the hedge
    "unhedged_spot_only",         # shares held with no contract behind them
    "close_perp_underfilled",     # spot kept because the perp is still short
    # Added 2026-09-16 with the P0 review. The first three were reachable before
    # this change and the unattended executor was calling one of them a success:
    # ``spot_short_of_hedge`` was literally in its ``executed`` set.
    "spot_short_of_hedge",        # fewer tokens arrived than the contracts need
    "spot_over_hedge",            # unhedged long dust beyond the policy tolerance
    "naked_short_unconfirmed",    # unwind of a naked short did not complete
    "perp_unknown",               # perp order state unreadable: size unknown
    "maker_partial",              # close partially filled: short reduced, spot kept
})


# States where the account is provably unchanged (nothing was sent) or provably
# flat (whatever was opened was fully bought back). Neither is a failure to page
# a human about, and neither is a completed trade.
NEUTRAL_STATES = frozenset({
    "maker_unfilled", "maker_order_rejected", "rejected", "dry_run_complete",
    "unwound_after_spot_failure",
    # Refused *before* the perpetual leg was rested: the on-chain leg's
    # allowance or balance could not pay for it. Nothing was sent, so nothing is
    # owed — and the group is a skip, not a stop (2026-09-18: this used to be
    # discovered after the fill, which cost an unwind every time).
    "spot_preflight_blocked",
    # A partial close that sold exactly the spot its filled contracts covered is
    # not a stranded leg: both legs shrank together and the remainder is still
    # hedged. It is unfinished, so it is not a success either — a second close
    # run (or the taker fallback, when configured) finishes it.
    "closed_partial",
})

# What "this direction finished" means. Kept apart from NEUTRAL_STATES so that a
# new state name cannot be counted as a success by accident: it has to be listed.
OPEN_SUCCESS_STATES = frozenset({"hedged"})
CLOSE_SUCCESS_STATES = frozenset({"closed", "perp_only_closed"})


def classify_flow(direction: str, state: str,
                  residual_tokens: Decimal | None = None) -> tuple[bool, bool]:
    """``(executed, halt)`` for one flow result.

    The unattended executor used to decide this from a literal set written at
    the call site, which is exactly how ``spot_short_of_hedge`` ended up in the
    ``executed`` set. It is a rule here instead:

    * executed — only a state on the success list for that direction;
    * halt     — any state in ``HALT_STATES``, any state that is neither a
                 success nor a known no-op, or any result carrying a residual.
    """
    success = OPEN_SUCCESS_STATES if direction == "open" else CLOSE_SUCCESS_STATES
    executed = state in success
    halt = state in HALT_STATES or (not executed and state not in NEUTRAL_STATES)
    if residual_tokens is not None and Decimal(residual_tokens) != 0 and not executed:
        # A state name that forgot to mention its residual does not make the
        # residual go away.
        halt = True
    return executed, halt


@dataclass
class FlowResult:
    """What the printer, the ledger and the notifier all need."""

    state: str
    payload: dict

    def summary(self) -> dict:
        return self.payload

    @property
    def executed(self) -> bool:
        return bool(self.payload.get("executed"))

    @property
    def halt(self) -> bool:
        return bool(self.payload.get("halt"))


def _position_identity(plan) -> dict:
    """Asset and contract, written onto every ledger row of a live order.

    ``group_instrument`` reads these fields back when an exit has to name the
    contract. A legs row that only says ``asset`` cannot be closed: the exit
    path refuses to guess which instrument to send. Measured 2026-09-24: three
    hedged CRWV groups recorded the asset and the fills, and none of them
    recorded ``CRWV-USDT-SWAP``, so the 3 bps close rule skipped all three.
    """
    identity: dict = {}
    asset = getattr(plan, "asset", None)
    instrument = getattr(plan, "perp_instrument", None)
    venue = getattr(plan, "perp_venue", None)
    if asset:
        identity["asset"] = asset
    if instrument:
        identity["instrument"] = str(instrument)
    if venue and instrument:
        identity["perp"] = f"{venue}:{instrument}"
    return identity


def maker_first_open(chain, plan, policy: dict, ledger, *,
                     balance_quote, open_contracts) -> FlowResult:
    """Rest the hedge on the offer, then buy exactly what filled.

    The risk gate runs first, unchanged. Everything after it is the bounded
    naked window described in ``execution/maker_first.py``.
    """
    orders = policy.get("orders") or {}
    allowed, reasons = chain.risk_check(plan, open_contracts=open_contracts,
                                        balance_quote=balance_quote)
    payload = {"plan": plan.summary(),
               "audit": {"risk": {"allowed": allowed, "reasons": reasons},
                         "order_style": "maker_first"}}
    if not allowed:
        return FlowResult("rejected", {**payload, "reason": ",".join(reasons),
                                       "state": "rejected", "executed": False,
                                       "halt": False})
    opener = MakerFirstOpener(okx=chain.okx, wallet_leg=chain.wallet, ledger=ledger)
    client_order_id = "arbm" + uuid.uuid4().hex[:18]

    def fresh_ask() -> Decimal:
        """Read the book at submit time, not from a plan built seconds ago."""
        return Decimal(str(chain.okx.ticker(plan.perp_instrument)["askPx"]))

    result = opener.open(
        plan=plan, client_order_id=client_order_id,
        timeout_seconds=int(orders.get("maker_timeout_seconds", 45)),
        poll_seconds=float(orders.get("maker_poll_seconds", 1)),
        price_source=fresh_ask,
        max_reprices=int(orders.get("maker_max_reprices", 0)),
        residual_tolerance_bps=_residual_tolerance_bps(policy),
        residual_tolerance_quote=_residual_tolerance_quote(policy),
        # The balance the caller already read for sizing. Checked again in front
        # of the order, together with the allowance, because both are static for
        # the next few seconds and both are what the spot leg can fail on.
        balance_quote=balance_quote)
    legs = result.legs_as_event()
    executed, halt = classify_flow("open", result.state, result.residual_tokens)
    if plan.intent is not None:
        for event in result.events:
            ledger.append(plan.intent.signal_id, result.state,
                          event.get("event", "maker_event"),
                          {**event, **_position_identity(plan)},
                          plan.intent.created_ts_ms)
        # The two legs as measured, in one place and in named units: this is the
        # record a reconciliation reads instead of re-deriving the residual from
        # the plan.
        ledger.append(plan.intent.signal_id, result.state, "legs",
                      {**legs, **_position_identity(plan)},
                      plan.intent.created_ts_ms)
        if result.unwind:
            ledger.append(plan.intent.signal_id, result.state, "maker_unwind",
                          result.unwind, plan.intent.created_ts_ms)
    spot = result.spot.as_event() if result.spot is not None else None
    warnings = [
        "maker-first：合约先挂单后成交，成交到现货买入之间是裸空窗口"
        "（已由超时/撤单/回补限制）",
    ]
    if result.residual_tokens > 0:
        warnings.append(
            f"现货多出 {result.residual_tokens} 枚未被对冲（{result.residual_bps} bps，"
            f"容差 {result.residual_tolerance_bps} bps）")
    if result.state == "spot_short_of_hedge":
        warnings.append(
            f"现货到账少于对冲所需：空头裸露 {abs(result.residual_tokens)} 枚，"
            "已停止自动执行，必须人工对账")
    if result.state == "spot_preflight_blocked":
        warnings.append(
            "现货腿前置条件不满足，永续单没有挂出（无需对平）："
            f"{result.reason}。补足授权/余额后会自行恢复，不需要解除停机")
    if result.state == "naked_short_unconfirmed":
        warnings.append(
            f"现货腿失败且回补未完成（{result.unwind}）：空头规模未确认，"
            "禁止继续自动化")
    if result.state == "spot_over_hedge":
        warnings.append(
            f"未对冲多头 {result.residual_tokens} 枚（{result.residual_bps} bps）"
            f"超过容差 {result.residual_tolerance_bps} bps")
    if result.state in HALT_STATES:
        warnings.append("该结果需要人工对账后才能继续（自动执行会停止）")
    if result.state == "maker_unknown":
        warnings.append(
            "挂单状态未确认：该订单可能仍在盘口，且随时可能成交。"
            "先查挂单并撤单（看板 8766 或 OKX），确认两腿状态后再继续")
    return FlowResult(result.state, {
        **payload, "reason": result.reason,
        "state": result.state,
        "executed": executed,
        "halt": halt,
        "maker": result.maker.as_event(),
        "spot": spot,
        "unwind": result.unwind,
        "legs": legs,
        "unhedged_tokens": str(result.residual_tokens),
        "naked_short_tokens": str(min(result.residual_tokens, Decimal(0)) * -1),
        "warnings": warnings,
    })


def _residual_tolerance_bps(policy: dict) -> Decimal | None:
    """How much unhedged long dust this run may leave behind.

    ``[limits] max_unhedged_bps`` is the reviewed number (5 bps in both config
    files). Absent means "no tolerance configured", which is reported as such
    rather than silently becoming unlimited: the shortfall direction halts
    regardless of this value.
    """
    limits = policy.get("limits") or {}
    raw = limits.get("max_unhedged_bps")
    if raw in (None, ""):
        return None
    return Decimal(str(raw))


def _residual_tolerance_quote(policy: dict) -> Decimal | None:
    """Cash value of unhedged long dust this run may leave behind.

    ``[limits] max_unhedged_quote_usdt`` is the operator's number. Absent
    means the bps tolerance is the only long-dust gate. A naked short does
    not read this value.
    """
    limits = policy.get("limits") or {}
    raw = limits.get("max_unhedged_quote_usdt")
    if raw in (None, ""):
        return None
    return Decimal(str(raw))


def maker_first_close(chain, plan, policy: dict, ledger, *, contracts: Decimal,
                      tokens: Decimal, opened_group_id: str | None = None,
                      balance_source=None, positions=None,
                      pending_orders=None) -> FlowResult:
    """Rest the bid, sell what was bought back; taker fallback on timeout.

    The mirror of ``maker_first_open`` and deliberately not the same
    parameters: opening has 120 seconds and may re-join, closing has ten
    minutes and does not chase. Both numbers come from ``[orders]`` so the
    policy file, not this function, decides how long a close may wait.

    ``balance_source(token_address) -> Decimal`` is the wallet's own token
    balance. The closer uses it to retry a failed spot sale safely: each
    attempt sells only what is still held, so an attempt whose result was lost
    cannot be repeated into a double sale.
    """
    orders = policy.get("orders") or {}
    # Closing is not the emergency path, so it gets the same gate as opening —
    # not a smaller one. Until 2026-09-16 the CLI went straight from
    # ``build_close_plan`` to the order with no kill switch, no halt check, no
    # allowlist re-read and no quote-age check.
    allowed, reasons = chain.risk_check_close(
        plan, contracts=contracts, tokens=tokens,
        positions=positions, pending_orders=pending_orders)
    if not allowed:
        return FlowResult("rejected", {
            "plan": plan.summary(), "state": "rejected",
            "reason": ",".join(reasons), "executed": False, "halt": False,
            "audit": {"order_style": "maker_close", "direction": "close",
                      "risk": {"allowed": False, "reasons": reasons}}})
    closer = MakerFirstCloser(
        okx=chain.okx, wallet_leg=chain.wallet, ledger=ledger,
        token_balance=balance_source,
        spot_attempts=int(orders.get("close_spot_retry_attempts", 5)),
        spot_backoff_seconds=float(orders.get("close_spot_retry_backoff_seconds", 2)))
    client_order_id = "arbc" + uuid.uuid4().hex[:18]

    def fresh_bid() -> Decimal:
        """Read the book at submit time: a plan price can be stale by then."""
        return Decimal(str(chain.okx.ticker(plan.perp_instrument)["bidPx"]))

    def fresh_taker_cap() -> Decimal:
        """Taker fallback cap from the live ask, rounded up to the tick."""
        ticker = chain.okx.ticker(plan.perp_instrument)
        plan.perp_ask_usd = Decimal(str(ticker["askPx"]))
        return plan.perp_limit_price_for_close()

    result = closer.close(
        plan=plan, client_order_id=client_order_id, contracts=contracts,
        token_quantity=tokens,
        timeout_seconds=int(orders.get("maker_close_timeout_seconds", 600)),
        poll_seconds=float(orders.get("maker_close_poll_seconds", 2)),
        fallback=str(orders.get("maker_close_fallback", "taker")),
        price_source=fresh_bid,
        max_reprices=int(orders.get("maker_close_max_reprices", 0)),
        taker_price_source=(fresh_taker_cap if contracts > 0 else None))
    legs = result.legs_as_event()
    executed, halt = classify_flow("close", result.state, result.residual_tokens)
    if plan.intent is not None:
        for event in result.events:
            ledger.append(plan.intent.signal_id, result.state,
                          event.get("event", "maker_event"),
                          {**event, **_position_identity(plan)},
                          plan.intent.created_ts_ms)
        ledger.append(plan.intent.signal_id, result.state, "legs",
                      {**legs, **_position_identity(plan)},
                      plan.intent.created_ts_ms)
    if result.state == "closed" and opened_group_id and result.spot is not None:
        ledger.append(opened_group_id, "closed_by", "closed_by", {
            "closing_group_id": plan.intent.signal_id if plan.intent else None,
            "spot_tx": result.spot.tx_hash,
            "quote_received": str(result.spot.quote_amount),
            "order_style": "maker_close",
        }, plan.intent.created_ts_ms if plan.intent else 0)
    warnings = [
        "maker 平仓：合约在 bid1 挂 post-only 买单等待成交，成交后才卖现货；"
        f"超时 {orders.get('maker_close_timeout_seconds', 600)} 秒后按 "
        f"{orders.get('maker_close_fallback', 'taker')} 兜底",
    ]
    if result.state == "maker_partial":
        warnings.append(
            f"平仓只买回部分合约（成交 {result.filled_contracts}/{contracts} 张）："
            "现货腿未变，该组出现未对冲多头，需人工决定继续平仓还是补回空头")
    if result.state == "close_unknown":
        warnings.append(
            "现货卖出结果未知：不得重发同一笔卖出，先按 txHash/余额查询再决定")
    if result.state == "manual_intervention_required":
        warnings.append(
            f"现货腿连续 {orders.get('close_spot_retry_attempts', 5)} 次卖出失败："
            "合约已平、现货仍在（多头裸露），已停止并通知人工审核；"
            "人工处理后用 ledger-reconcile 如实补记账本")
    if result.state in HALT_STATES:
        warnings.append("该结果需要人工对账（自动执行会停止）")
    return FlowResult(result.state, {
        "plan": plan.summary(), "state": result.state, "reason": result.reason,
        "executed": executed, "halt": halt,
        "requires_manual_review": result.state == "manual_intervention_required",
        "audit": {"order_style": "maker_close", "direction": "close"},
        "maker": result.maker.as_event(),
        "spot": result.spot.as_event() if result.spot is not None else None,
        "unwind": result.unwind,
        "fallback": result.fallback,
        "legs": legs,
        "unhedged_tokens": str(result.residual_tokens),
        "warnings": warnings,
    })
