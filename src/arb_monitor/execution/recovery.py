"""Recovery workflows for ambiguous orders and fail-closed operation."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .network import RemoteOrderState, Resolution


class RecoveryState(str, Enum):
    CANCEL_REQUESTED = "cancel_requested"
    CANCEL_UNKNOWN = "cancel_unknown"
    OPEN_UNCONFIRMED = "open_unconfirmed"
    FILLED_UNCONFIRMED = "filled_unconfirmed"
    CLOSED_CONFIRMED = "closed_confirmed"
    MANUAL_INTERVENTION_REQUIRED = "manual_intervention_required"


@dataclass
class OrderRecovery:
    client_order_id: str
    state: RecoveryState = RecoveryState.CANCEL_REQUESTED
    attempts: int = 0
    max_attempts: int = 3
    reason: str = ""

    def cancel_failed(self, reason: str = "cancel_unknown") -> None:
        self.state = RecoveryState.CANCEL_UNKNOWN
        self.reason = reason

    def resolve(self, result: Resolution) -> RecoveryState:
        self.attempts += 1
        if result.state is RemoteOrderState.OPEN:
            self.state = RecoveryState.OPEN_UNCONFIRMED
        elif result.state in (RemoteOrderState.FILLED, RemoteOrderState.PARTIALLY_FILLED):
            self.state = RecoveryState.FILLED_UNCONFIRMED
        elif result.state in (RemoteOrderState.CANCELED, RemoteOrderState.REJECTED):
            self.state = RecoveryState.CLOSED_CONFIRMED
        else:
            self.state = RecoveryState.CANCEL_UNKNOWN
        if self.attempts >= self.max_attempts and self.state is RecoveryState.CANCEL_UNKNOWN:
            self.state = RecoveryState.MANUAL_INTERVENTION_REQUIRED
        return self.state

    @property
    def allows_new_orders(self) -> bool:
        return self.state is RecoveryState.CLOSED_CONFIRMED


def recover_cancel(
    recovery: OrderRecovery,
    query: Callable[[str], Resolution],
) -> RecoveryState:
    """Query by client id; never submit a replacement order."""
    return recovery.resolve(query(recovery.client_order_id))


def recover_cancel_loop(
    recovery: OrderRecovery,
    query: Callable[[str], Resolution],
) -> RecoveryState:
    """Bounded recovery loop; unknown state escalates, never resubmits."""
    while recovery.attempts < recovery.max_attempts:
        state = recover_cancel(recovery, query)
        if state is not RecoveryState.CANCEL_UNKNOWN:
            return state
    return recovery.state
