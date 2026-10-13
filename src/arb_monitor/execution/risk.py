from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from .intent import ExecutionIntent
from .kill_switch import KillSwitch


@dataclass(frozen=True)
class ExecutionLimits:
    max_total_capital: Decimal = Decimal("10000")
    max_single_notional: Decimal = Decimal("1000")
    max_signal_age_ms: int = 3000
    max_edge_bps: Decimal = Decimal("10000")
    max_slippage_bps: Decimal = Decimal("50")
    max_single_leg_notional: Decimal | None = None
    max_asset_exposure: Decimal | None = None
    max_venue_exposure: Decimal | None = None
    max_unhedged_quantity: Decimal | None = None
    max_unhedged_bps: Decimal | None = None
    max_daily_loss: Decimal | None = None
    max_consecutive_failures: int | None = None


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reasons: tuple[str, ...] = ()
    resized_quantity: Decimal | None = None
    audit: dict = None


class PreTradeRisk:
    def __init__(self, limits: ExecutionLimits | None = None, kill_switch: KillSwitch | None = None):
        self.limits = limits or ExecutionLimits()
        self.kill_switch = kill_switch or KillSwitch()

    def check(self, intent: ExecutionIntent, now_ms: int,
              current_capital: Decimal = Decimal("0"), *,
              asset_exposure: Decimal = Decimal("0"),
              venue_exposure: Decimal = Decimal("0"),
              unhedged_quantity: Decimal = Decimal("0"),
              daily_loss: Decimal = Decimal("0"),
              consecutive_failures: int = 0) -> RiskDecision:
        reasons: list[str] = []
        if not intent.execution_allowed:
            reasons.append("intent_execution_disabled")
        if intent.is_expired(now_ms):
            reasons.append("stale_signal")
        if now_ms - intent.created_ts_ms > self.limits.max_signal_age_ms:
            reasons.append("signal_age_exceeded")
        if intent.spot_amount > self.limits.max_single_notional:
            reasons.append("single_notional_limit")
        if current_capital + intent.spot_amount > self.limits.max_total_capital:
            reasons.append("capital_limit")
        if intent.max_slippage_bps > self.limits.max_slippage_bps:
            reasons.append("slippage_limit")
        single_leg = self.limits.max_single_leg_notional or self.limits.max_single_notional
        if intent.spot_amount > single_leg:
            reasons.append("single_leg_notional_limit")
        if self.limits.max_asset_exposure is not None and asset_exposure + intent.spot_amount > self.limits.max_asset_exposure:
            reasons.append("asset_exposure_limit")
        if self.limits.max_venue_exposure is not None and venue_exposure + intent.spot_amount > self.limits.max_venue_exposure:
            reasons.append("venue_exposure_limit")
        if self.limits.max_unhedged_quantity is not None and unhedged_quantity > self.limits.max_unhedged_quantity:
            reasons.append("unhedged_quantity_limit")
        if self.limits.max_unhedged_bps is not None and intent.net_edge_bps > self.limits.max_unhedged_bps and unhedged_quantity > 0:
            reasons.append("unhedged_bps_limit")
        if self.limits.max_daily_loss is not None and daily_loss >= self.limits.max_daily_loss:
            reasons.append("daily_loss_limit")
        if self.limits.max_consecutive_failures is not None and consecutive_failures >= self.limits.max_consecutive_failures:
            reasons.append("consecutive_failure_limit")
        if self.kill_switch.is_tripped():
            reasons.append("kill_switch")
        return RiskDecision(not reasons, tuple(reasons), audit={
            "signal_id": intent.signal_id, "now_ms": now_ms,
            "spot_amount": str(intent.spot_amount),
            "current_capital": str(current_capital),
            "asset_exposure": str(asset_exposure),
            "venue_exposure": str(venue_exposure),
            "unhedged_quantity": str(unhedged_quantity),
        })
