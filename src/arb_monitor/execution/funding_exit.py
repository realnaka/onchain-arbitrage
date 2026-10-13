"""Funding-cost exit rule: stop paying funding once it costs more than the trade.

Operator's rule, 2026-09-16:

> 如果持仓期间资金费率为负（我们持有的空头需要支付资金费率），而支付的资金费
> 累计达到 1 美元，那么在下一次资金费率收取之前的倒数一分钟，如果仓位还存在，
> 不管当前套利组合的损益情况，直接平掉仓位。

Why the direction is what it is: on OKX a **negative** funding rate means shorts
pay longs (a positive rate means shorts receive). So "the rate is negative" and
"we are paying" are the same statement for this strategy's short leg.

The rule has three facts and one clock:

* ``funding_rate``     — the rate being accrued, from the venue's public endpoint
* ``settlement_at_ms`` — when that rate is charged
* ``paid_quote``       — funding we have actually paid so far, from the account's
                         own bill lines (type=8), not from a model
* the clock            — the window opens ``lead_seconds`` before the settlement

and three outcomes:

``none``       the rate is not costing us, or the cumulative paid is below the
               threshold: nothing to do
``armed``      the threshold is reached and the window has not opened yet: the
               deadline is known, so it is recorded and alerted rather than
               acted on early
``close_now``  the threshold is reached and we are inside the last minute
               before settlement (or we already missed it — then the earliest
               opportunity is now, because waiting means paying again)
``unknown``    the venue would not answer: **no action** and an alert. Acting on
               unread funding data would be the same mistake as acting on an
               unread order.

Every number that goes into the decision is returned with it, so the ledger
event and the alert can be audited without re-reading the venue.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

# OKX bill type for funding fees. Documented as 8 ("资金费"); the rows carry the
# signed amount in `pnl` (negative = we paid) and an `fee` mirror in some
# revisions, so both are read.
FUNDING_BILL_TYPE = "8"


class FundingDataUnavailable(RuntimeError):
    """The funding facts could not be read: unknown, never 'no cost'."""


@dataclass
class FundingObservation:
    instrument: str
    observed_at_ms: int
    readable: bool = True
    error: str | None = None
    # The rate being accrued right now, and when it settles.
    funding_rate: Decimal | None = None
    settlement_at_ms: int | None = None
    next_funding_rate: Decimal | None = None
    # What this position has actually paid / received, from the venue's bills.
    paid_quote: Decimal = Decimal(0)
    received_quote: Decimal = Decimal(0)
    bill_rows: list[dict] = field(default_factory=list)
    position_notional_quote: Decimal | None = None
    source: str = "okx"

    @property
    def pays_us(self) -> bool:
        """A short pays on a negative rate and receives on a positive one."""
        return self.funding_rate is not None and self.funding_rate < 0

    def projected_next_payment_quote(self) -> Decimal | None:
        """What the imminent settlement would cost, if we still hold."""
        if not self.pays_us or self.position_notional_quote is None:
            return None
        return abs(self.position_notional_quote) * abs(self.funding_rate)

    def as_event(self) -> dict:
        return {
            "instrument": self.instrument,
            "readable": self.readable,
            "error": self.error,
            "funding_rate": (None if self.funding_rate is None
                             else str(self.funding_rate)),
            "settlement_at_ms": self.settlement_at_ms,
            "next_funding_rate": (None if self.next_funding_rate is None
                                  else str(self.next_funding_rate)),
            "paid_quote": str(self.paid_quote),
            "received_quote": str(self.received_quote),
            "position_notional_quote": (None if self.position_notional_quote is None
                                        else str(self.position_notional_quote)),
            "projected_next_payment_quote": (None if self.projected_next_payment_quote() is None
                                             else str(self.projected_next_payment_quote())),
            "observed_at_ms": self.observed_at_ms,
            "source": self.source,
        }


@dataclass(frozen=True)
class FundingExitPolicy:
    enabled: bool = True
    threshold_quote: Decimal = Decimal("1")
    lead_seconds: int = 60
    require_negative_rate: bool = True

    @classmethod
    def from_policy(cls, policy: dict) -> "FundingExitPolicy":
        section = (policy or {}).get("funding_exit") or {}
        raw_enabled = section.get("enabled", True)
        return cls(
            enabled=str(raw_enabled).strip().lower() not in {"false", "0", "no", "off"},
            threshold_quote=Decimal(str(section.get("threshold_quote", "1"))),
            lead_seconds=int(section.get("lead_seconds", 60)),
            require_negative_rate=str(
                section.get("require_negative_rate", True)).strip().lower()
            not in {"false", "0", "no", "off"},
        )


@dataclass
class FundingExitDecision:
    action: str                 # none | armed | close_now | unknown | disabled
    reason: str
    close_at_ms: int | None = None
    observation: FundingObservation | None = None

    @property
    def alerts(self) -> bool:
        return self.action in {"armed", "close_now", "unknown"}

    def as_event(self) -> dict:
        return {"action": self.action, "reason": self.reason,
                "close_at_ms": self.close_at_ms,
                "observation": (self.observation.as_event()
                                if self.observation is not None else None)}


def read_funding_state(*, instrument: str, position_start_ms: int, now_ms: int,
                       public_get: Callable[[str, str], Any],
                       signed_get: Callable[[str, str], Any],
                       position_notional_quote: Decimal | None = None,
                       ) -> FundingObservation:
    """The three facts, read once. Unreadable is ``readable=False``, not zero."""
    obs = FundingObservation(instrument=instrument, observed_at_ms=now_ms,
                             position_notional_quote=position_notional_quote)
    try:
        rows = list(public_get("/api/v5/public/funding-rate",
                               f"instId={instrument}"))
        if not rows:
            raise FundingDataUnavailable("empty_funding_rate_response")
        row = rows[0]
        rate = row.get("fundingRate")
        if rate in (None, ""):
            raise FundingDataUnavailable("funding_rate_missing")
        obs.funding_rate = Decimal(str(rate))
        next_rate = row.get("nextFundingRate")
        obs.next_funding_rate = (Decimal(str(next_rate)) if next_rate not in (None, "")
                                 else None)
        # `fundingTime` is when the rate being accrued is charged; a caller that
        # gets `nextFundingTime` instead would close a full interval early, so
        # the field is named explicitly here rather than guessed.
        settlement = row.get("fundingTime") or row.get("nextFundingTime")
        if settlement in (None, ""):
            raise FundingDataUnavailable("funding_time_missing")
        obs.settlement_at_ms = int(settlement)
    except Exception as exc:  # noqa: BLE001 - unreadable is a state
        obs.readable = False
        obs.error = f"funding_rate:{type(exc).__name__}:{str(exc)[:120]}"
        return obs
    try:
        bills = list(signed_get(
            "/api/v5/account/bills",
            f"instType=SWAP&instId={instrument}&type={FUNDING_BILL_TYPE}"
            f"&begin={int(position_start_ms)}&end={int(now_ms)}&limit=100"))
    except Exception as exc:  # noqa: BLE001 - unreadable is a state
        obs.readable = False
        obs.error = f"funding_bills:{type(exc).__name__}:{str(exc)[:120]}"
        return obs
    paid = Decimal(0)
    received = Decimal(0)
    for row in bills:
        # The signed amount is what moved: negative means we paid.
        amount = row.get("pnl")
        if amount in (None, ""):
            amount = row.get("fee")
        if amount in (None, ""):
            continue
        value = Decimal(str(amount))
        if value < 0:
            paid += -value
        else:
            received += value
    obs.paid_quote = paid
    obs.received_quote = received
    obs.bill_rows = bills
    return obs


def decide(policy: FundingExitPolicy, observation: FundingObservation,
           now_ms: int) -> FundingExitDecision:
    """Apply the operator's rule to one observation."""
    if not policy.enabled:
        return FundingExitDecision("disabled", "rule_disabled", observation=observation)
    if not observation.readable:
        return FundingExitDecision(
            "unknown", f"funding_data_unreadable:{observation.error}",
            observation=observation)
    if observation.settlement_at_ms is None:
        return FundingExitDecision("unknown", "settlement_time_unknown",
                                   observation=observation)
    if policy.require_negative_rate and not observation.pays_us:
        return FundingExitDecision(
            "none",
            f"rate_not_costing_us:{observation.funding_rate}", observation=observation)
    if observation.paid_quote < policy.threshold_quote:
        return FundingExitDecision(
            "none",
            f"paid_{observation.paid_quote}_below_{policy.threshold_quote}",
            observation=observation)
    close_at = observation.settlement_at_ms - policy.lead_seconds * 1000
    if now_ms >= close_at:
        return FundingExitDecision(
            "close_now",
            (f"paid_{observation.paid_quote}>=threshold_{policy.threshold_quote}"
             + ("_inside_final_{}s".format(policy.lead_seconds)
                if now_ms < observation.settlement_at_ms
                else "_settlement_window_missed_close_immediately")),
            close_at_ms=close_at, observation=observation)
    return FundingExitDecision(
        "armed",
        (f"paid_{observation.paid_quote}>=threshold_{policy.threshold_quote}"
         f"_close_at_{close_at}"),
        close_at_ms=close_at, observation=observation)
