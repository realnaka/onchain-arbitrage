"""Deterministic approval+swap orchestration.

Approval is a prerequisite, never the business outcome. A swap group remains
incomplete until the swap receipt is confirmed successful.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from decimal import Decimal


class SwapPhase(str, Enum):
    CREATED = "created"
    APPROVAL_PENDING = "approval_pending"
    APPROVED = "approved"
    SWAP_PENDING = "swap_pending"
    COMPLETED = "completed"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass
class SwapTransaction:
    group_id: str
    amount: Decimal
    max_allowance: Decimal
    phase: SwapPhase = SwapPhase.CREATED
    approval_tx: str | None = None
    swap_tx: str | None = None

    def approval_confirmed(self, tx_hash: str, allowance: Decimal) -> None:
        if allowance < self.amount or allowance > self.max_allowance:
            raise ValueError("allowance outside bounded policy")
        self.approval_tx = tx_hash
        self.phase = SwapPhase.APPROVED

    def swap_broadcast(self, tx_hash: str) -> None:
        if self.phase is not SwapPhase.APPROVED:
            raise RuntimeError("swap requires confirmed bounded approval")
        self.swap_tx = tx_hash
        self.phase = SwapPhase.SWAP_PENDING

    def swap_result(self, success: bool) -> None:
        if self.phase is not SwapPhase.SWAP_PENDING:
            raise RuntimeError("swap result received in invalid phase")
        self.phase = SwapPhase.COMPLETED if success else SwapPhase.RECOVERY_REQUIRED
