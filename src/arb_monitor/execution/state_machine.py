from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from decimal import Decimal

from .adapters import CEXAdapter, WalletAdapter, OrderRequest, OrderResult
from .intent import ExecutionIntent
from .risk import PreTradeRisk
from .mev import Broadcaster
from .network import NetworkFault


class ExecutionState(str, Enum):
    SIGNAL_RECEIVED = "signal_received"
    PRECHECK = "precheck"
    REJECTED = "rejected"
    PERP_OPENING = "perp_opening"
    SPOT_QUOTING = "spot_quoting"
    SPOT_SUBMITTED = "spot_submitted"
    OPEN = "open"
    EXIT_TRIGGERED = "exit_triggered"
    SPOT_CLOSING = "spot_closing"
    PERP_CLOSING = "perp_closing"
    CLOSED = "closed"
    MANUAL_INTERVENTION_REQUIRED = "manual_intervention_required"
    LEG1_PARTIAL = "leg1_partial"
    LEG1_FILLED = "leg1_filled"
    LEG2_SUBMITTED = "leg2_submitted"
    HEDGED = "hedged"
    UNHEDGED = "unhedged"
    CANCEL_REQUESTED = "cancel_requested"
    CANCEL_UNKNOWN = "cancel_unknown"
    OPEN_UNCONFIRMED = "open_unconfirmed"
    FILLED_UNCONFIRMED = "filled_unconfirmed"
    CLOSED_CONFIRMED = "closed_confirmed"
    PENDING_CHAIN = "pending_chain"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass
class ExecutionResult:
    state: ExecutionState
    reason: str = ""
    perp: OrderResult | None = None
    spot: OrderResult | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


class ExecutionCoordinator:
    def __init__(self, cex: CEXAdapter, wallet: WalletAdapter, risk: PreTradeRisk,
                 broadcaster: Broadcaster | None = None, ledger=None):
        self.cex, self.wallet, self.risk, self.broadcaster, self.ledger = cex, wallet, risk, broadcaster, ledger
        self.recovery_required = False

    def require_reconciliation(self) -> None:
        self.recovery_required = True

    def reconcile_before_trading(self) -> None:
        self.recovery_required = False

    def recover_from_ledger(self) -> dict[str, dict[str, Any]]:
        """Restore durable state and fail closed if any group is unresolved."""
        if self.ledger is None:
            self.recovery_required = True
            return {}
        groups = self.ledger.recover()
        terminal = {
            ExecutionState.CLOSED.value, ExecutionState.CLOSED_CONFIRMED.value,
            ExecutionState.REJECTED.value, ExecutionState.FAILED.value,
        }
        self.recovery_required = any(
            item.get("state") not in terminal for item in groups.values()
        )
        return groups

    def _record(self, intent, state, event, payload, now_ms):
        if self.ledger:
            self.ledger.append(intent.signal_id, state.value, event,
                               {"signal_id": intent.signal_id, **payload}, now_ms)

    def execute_open(self, intent: ExecutionIntent, now_ms: int, current_capital=None) -> ExecutionResult:
        result = ExecutionResult(ExecutionState.PRECHECK)
        self._record(intent, result.state, "start", {}, now_ms)
        if self.recovery_required:
            result.state, result.reason = ExecutionState.RECONCILIATION_REQUIRED, "reconciliation_required"
            return result
        decision = self.risk.check(intent, now_ms, current_capital or 0)
        result.events.append({"state": ExecutionState.PRECHECK.value, "reasons": decision.reasons})
        self._record(intent, result.state, "risk_decision", {
            "allowed": decision.allowed,
            "reasons": decision.reasons,
            "audit": decision.audit or {},
        }, now_ms)
        if not decision.allowed:
            result.state, result.reason = ExecutionState.REJECTED, ",".join(decision.reasons)
            return result
        try:
            spec = self.cex.get_instrument(intent.perp_instrument)
            if not spec.linear:
                raise ValueError("perpetual is not linear")
            # Core strategy invariant: spot liquidity is the scarce leg.  Never
            # open the hedge before the spot fill is positively confirmed.
            result.state = ExecutionState.SPOT_QUOTING
            self._record(intent, result.state, "spot_quote", {}, now_ms)
            quote = self.wallet.quote_exact_in(
                intent.quote_token_address, intent.token_address, intent.spot_amount
            )
            minimum = quote.get("amount_out_minimum", intent.expected_token_quantity)
            result.state = ExecutionState.SPOT_SUBMITTED
            result.spot = self.wallet.swap_exact_in(quote, minimum)
            if result.spot.status == "pending":
                result.state = ExecutionState.PENDING_CHAIN
                result.reason = "chain_confirmation_pending"
                self._record(intent, result.state, "chain_pending", {
                    "spot_order_id": result.spot.order_id,
                }, now_ms)
                return result
            if result.spot.filled_quantity <= 0:
                raise RuntimeError("spot swap not filled")
            # Only after confirmed spot fill may the CEX hedge be submitted.
            result.state = ExecutionState.PERP_OPENING
            self._record(intent, result.state, "perp_order", {
                "spot_order_id": result.spot.order_id,
                "spot_filled_quantity": str(result.spot.filled_quantity),
            }, now_ms)
            result.perp = self.cex.place_order(OrderRequest(
                intent.perp_instrument, "sell", result.spot.filled_quantity, "ioc",
                price=intent.perp_limit_price,
                client_order_id=f"{intent.signal_id}-perp",
            ))
            if result.perp.filled_quantity <= 0:
                raise RuntimeError("perpetual order not filled after spot confirmation")
            if result.perp.filled_quantity < result.spot.filled_quantity:
                result.state = ExecutionState.UNHEDGED
                result.reason = "legs_quantity_mismatch"
                return result
            result.state = ExecutionState.HEDGED
            result.state = ExecutionState.OPEN
            self._record(intent, result.state, "opened", {
                "perp_order_id": result.perp.order_id, "spot_order_id": result.spot.order_id
            }, now_ms)
            return result
        except NetworkFault as exc:
            result.state = (ExecutionState.OPEN_UNCONFIRMED
                            if exc.may_have_reached_remote
                            else ExecutionState.MANUAL_INTERVENTION_REQUIRED)
            result.reason = str(exc)
            self._record(intent, result.state, "network_fault", {
                "reason": result.reason, "network_state": exc.state.value,
                "may_have_reached_remote": exc.may_have_reached_remote,
            }, now_ms)
            return result
        except Exception as exc:
            result.state = ExecutionState.MANUAL_INTERVENTION_REQUIRED
            result.reason = str(exc)
            self._record(intent, result.state, "open_failed", {"reason": result.reason}, now_ms)
            return result

    def execute_close(self, intent: ExecutionIntent, now_ms: int,
                      perp_quantity: Any | None = None, token_quantity: Any | None = None) -> ExecutionResult:
        """Close the complete arbitrage group, never just one leg.

        Both legs are attempted and the group is terminal only after both are
        positively filled. Any single-leg result is an abnormal/unhedged state.
        """
        result = ExecutionResult(ExecutionState.EXIT_TRIGGERED)
        try:
            result.state = ExecutionState.SPOT_CLOSING
            quote = self.wallet.quote_exact_in(intent.token_address, intent.quote_token_address,
                                               token_quantity or intent.expected_token_quantity)
            result.spot = self.wallet.sell_exact_in(quote, quote.get("amount_out_minimum", Decimal("0")))
            result.state = ExecutionState.PERP_CLOSING
            result.perp = self.cex.place_order(OrderRequest(
                intent.perp_instrument, "buy", perp_quantity or intent.perp_contract_quantity,
                "ioc", price=intent.perp_limit_price, reduce_only=True,
                client_order_id=f"{intent.signal_id}-close-perp"))
            if result.spot.filled_quantity <= 0 or result.perp.filled_quantity <= 0:
                raise RuntimeError("close_underfilled")
            result.state = ExecutionState.CLOSED
            self._record(intent, result.state, "closed", {}, now_ms)
        except Exception as exc:
            result.state = ExecutionState.MANUAL_INTERVENTION_REQUIRED
            result.reason = str(exc)
            self._record(intent, result.state, "close_failed", {"reason": result.reason}, now_ms)
        return result
