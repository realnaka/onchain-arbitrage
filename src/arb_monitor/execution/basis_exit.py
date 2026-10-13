"""Basis-target exit: close when the spread has come back to the operator's number.

Operator's rule, 2026-09-21 (verbatim):

> 我们开仓的时候，应该是要求现货和期货之间的价格差达到 20 个 BPS 的价差才开仓；
> 平仓的时候，要求这个价差恢复到 3 个 BPS。

The entry half already exists as ``[monitor] min_net_edge_bps = 20``, and 3 bps
is already the number every entry plan *assumes*: each ``basis_evaluation``
carries ``assumed_exit_basis_bps = 3`` and the conditional profit is computed on
exactly that assumption. What did not exist was anything in the unattended loop
that acts when the assumption comes true - ``full-chain --close`` was the only
way to take the exit, and it needs a human at the keyboard.

Two properties are deliberate:

* the observation is taken on the venue we are *allowed to trade*, never on
  whichever venue shows the best basis in the table. Closing a gate price
  against an okx position would price a trade that cannot happen;
* anything unreadable or flagged by the data-quality set is ``unknown``: no
  action, an alert. Acting on a stale basis is the same mistake as acting on a
  stale quote, and this rule is the one that spends money when it fires.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


def _truthy(value) -> bool:
    return str(value).strip().lower() not in {"false", "0", "no", "off", ""}


@dataclass(frozen=True)
class BasisExitPolicy:
    enabled: bool = False
    target_exit_basis_bps: Decimal = Decimal("3")
    max_quote_age_ms: int = 15000
    cooldown_seconds: int = 300

    @classmethod
    def from_policy(cls, policy: dict | None) -> "BasisExitPolicy":
        section = (policy or {}).get("auto_close") or {}
        return cls(
            enabled=_truthy(section.get("enabled", False)),
            target_exit_basis_bps=Decimal(str(section.get("target_exit_basis_bps", "3"))),
            max_quote_age_ms=int(section.get("max_quote_age_ms", 15000)),
            cooldown_seconds=int(section.get("cooldown_seconds", 300)),
        )


@dataclass(frozen=True)
class BasisObservation:
    """The exit basis for one position, on the venue we may trade."""

    asset: str
    instrument: str
    exit_basis_bps: Decimal | None = None
    observed_at_ms: int | None = None
    trusted: bool = False
    detail: str = ""

    def as_event(self) -> dict:
        return {
            "asset": self.asset,
            "instrument": self.instrument,
            "exit_basis_bps": (str(self.exit_basis_bps)
                               if self.exit_basis_bps is not None else None),
            "observed_at_ms": self.observed_at_ms,
            "trusted": self.trusted,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class BasisExitDecision:
    #: disabled | no_observation | unknown | stale | above_target | close_now
    action: str
    reason: str
    observation: BasisObservation | None = None

    def as_event(self) -> dict:
        return {"action": self.action, "reason": self.reason,
                "observation": (self.observation.as_event()
                                if self.observation is not None else None)}


def parse_basis(raw) -> Decimal | None:
    """A basis out of an evaluation row, or None when it is not a number."""
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def decide(policy: BasisExitPolicy, observation: BasisObservation | None,
           now_ms: int) -> BasisExitDecision:
    """What to do about the exit basis right now."""
    if not policy.enabled:
        return BasisExitDecision("disabled", "auto_close_disabled", observation)
    if observation is None:
        return BasisExitDecision("no_observation", "no_evaluation_for_position")
    if not observation.trusted:
        return BasisExitDecision(
            "unknown", observation.detail or "untrusted_evaluation", observation)
    if observation.exit_basis_bps is None:
        return BasisExitDecision("unknown", "exit_basis_absent", observation)
    if not observation.observed_at_ms:
        return BasisExitDecision("unknown", "observation_without_timestamp", observation)
    age = int(now_ms) - int(observation.observed_at_ms)
    if age > policy.max_quote_age_ms:
        return BasisExitDecision("stale", f"evaluation_age_ms_{age}", observation)
    if observation.exit_basis_bps <= policy.target_exit_basis_bps:
        return BasisExitDecision(
            "close_now",
            f"exit_basis_{observation.exit_basis_bps}_at_or_below_target_"
            f"{policy.target_exit_basis_bps}",
            observation)
    return BasisExitDecision(
        "above_target",
        f"exit_basis_{observation.exit_basis_bps}_above_target_"
        f"{policy.target_exit_basis_bps}",
        observation)
