"""Pending order attribution: an order that outlives its group is live risk.

Measured 2026-09-16 11:08:35: a post-only sell whose cancel was never confirmed
was reported as "unfilled". It rested on the book for sixteen minutes and then
filled into a three-contract naked short (~10 USDT), and nobody knew until a
human read the account board at 11:24:46. The flow that placed it had already
exited, so no code was watching for it.

This module is the thing that watches. It asks the venue which orders are
resting, attributes each one to a ledger group through the ``clOrdId`` that the
flows recorded when they placed it, and calls a verdict:

``no_group_in_ledger``      the order was not placed by a group we know about
``group_already_terminal``  the group finished, so this order has no owner
``age_beyond_ttl``          it has rested longer than the flow was allowed to
``direction_mismatch``      a buy resting for an opening group (or the reverse)
``filled_while_idle``       already filled while no execution window is open

Every verdict except "attributed to an active group inside its window" blocks
new automated execution, because the alternative is opening a second position
on top of an order whose fate nobody has established. A pending order that
*cannot be read at all* also blocks: unknown is never treated as "no orders".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

ORPHAN_VERDICTS = frozenset({
    "no_group_in_ledger", "group_already_terminal", "age_beyond_ttl",
    "direction_mismatch", "filled_while_idle", "orders_unreadable",
})

# What side a resting order may have, given what the group was doing. Opening
# rests a *sell* on the offer; closing rests a *buy* on the bid. A group whose
# last state is anything else (a plan, a rejection, an unknown) has no business
# resting an order at all, so any side is a mismatch.
_OPENING_STATES = frozenset({
    "precheck", "allowance_ready", "spot_filled", "perp_submitted", "hedged",
})
_CLOSING_STATES = frozenset({"perp_closed", "perp_only_closed", "closing"})

# Groups that finished by definition. A resting order attributed to one of
# these is the 11:08 case.
TERMINAL_GROUP_STATES = frozenset({
    "closed", "closed_by", "perp_closed", "perp_only_closed", "rejected",
    "failed", "dry_run_complete", "paper_hedged", "paper_closed",
    "reconciled", "reconciled_manual", "incident_reconciled",
    "recovery_reconciled", "maker_unfilled", "auto_skipped",
    "requote_below_breakeven", "requote_expired", "daily_quota_reached",
})

# States a group can only reach *after* something was sent, so a group whose
# latest state is one of these may be holding a position even if no event
# follows it (a crash between the two legs writes exactly this shape). These are
# the groups that must be reconciled by a human before automation resumes;
# `precheck`, `allowance_ready` and `rejected` are preparation and hold nothing,
# which is why they are listed separately rather than treated as exposure.
EXPOSURE_GROUP_STATES = frozenset({
    "hedged", "spot_filled", "perp_submitted", "perp_closed", "maker_partial",
    "maker_unknown", "close_perp_underfilled", "unhedged_spot_only",
    "spot_short_of_hedge", "spot_over_hedge", "naked_short_unconfirmed",
    "perp_unknown", "close_unknown", "manual_intervention_required",
    "spot_not_confirmed", "unwound_after_spot_failure",
})

# Preparation states: the flow was still deciding. They hold nothing, but only
# the venue can prove that, so the board flags them for a position check.
PRE_WRITE_GROUP_STATES = frozenset({"precheck", "allowance_ready"})


def is_exposure_state(state: str | None) -> bool:
    """Might this group be holding something? Checked against the venue too."""
    return state in EXPOSURE_GROUP_STATES and state not in TERMINAL_GROUP_STATES


@dataclass
class PendingVerdict:
    order_id: str
    client_order_id: str
    instrument: str
    side: str
    size: Decimal
    filled: Decimal
    state: str
    group_id: str | None
    age_ms: int | None
    verdict: str
    reason: str = ""

    def as_event(self) -> dict:
        return {
            "order_id": self.order_id, "client_order_id": self.client_order_id,
            "instrument": self.instrument, "side": self.side,
            "size": str(self.size), "filled": str(self.filled),
            "state": self.state, "group_id": self.group_id,
            "age_ms": self.age_ms, "verdict": self.verdict, "reason": self.reason,
        }


@dataclass
class PendingScan:
    status: str                       # ok | unreadable
    checked_at_ms: int
    orders: list[PendingVerdict] = field(default_factory=list)
    orphans: list[PendingVerdict] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def blocked(self) -> bool:
        return bool(self.orphans) or self.status != "ok"

    def summary(self) -> dict:
        return {"status": self.status, "checked_at_ms": self.checked_at_ms,
                "order_count": len(self.orders),
                "orphan_count": len(self.orphans),
                "blocked": self.blocked, "reasons": list(self.reasons),
                "error": self.error,
                "orders": [row.as_event() for row in self.orders],
                "orphans": [row.as_event() for row in self.orphans]}


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else "0"))
    except Exception:  # noqa: BLE001 - an unparseable size is reported as 0
        return Decimal(0)


def expected_side(state: str | None) -> str | None:
    if state in _CLOSING_STATES:
        return "buy"
    if state in _OPENING_STATES:
        return "sell"
    return None


def scan_pending_orders(*, read, ledger, now_ms: int, policy: dict | None = None,
                        inst_type: str = "SWAP",
                        ttl_seconds: int | None = None,
                        client_order_index: dict[str, str] | None = None,
                        group_states: dict[str, str] | None = None) -> PendingScan:
    """Read resting orders once and attribute every one of them.

    ``client_order_index`` and ``group_states`` let a caller pass what it
    already read from the ledger; otherwise both are read here.
    """
    orders = (policy or {}).get("orders") or {}
    if ttl_seconds is None:
        ttl_seconds = int(max(
            int(orders.get("maker_timeout_seconds", 120)),
            int(orders.get("maker_close_timeout_seconds", 600)))) + 60
    try:
        rows = list(read.pending_orders(inst_type=inst_type))
    except Exception as exc:  # noqa: BLE001 - unknown must block, not pass
        return PendingScan(status="unreadable", checked_at_ms=now_ms,
                           reasons=[f"pending_orders_unreadable:{type(exc).__name__}"],
                           error=f"{type(exc).__name__}:{str(exc)[:160]}")
    index = (client_order_index if client_order_index is not None
             else ledger.client_order_ids())
    states = (group_states if group_states is not None
              else {group: (row.get("state") or "")
                    for group, row in ledger.recover().items()})
    scan = PendingScan(status="ok", checked_at_ms=now_ms)
    for row in rows:
        client_id = str(row.get("clOrdId") or "")
        order_id = str(row.get("ordId") or "")
        side = str(row.get("side") or "")
        filled = _decimal(row.get("accFillSz"))
        created = row.get("cTime") or row.get("uTime")
        age_ms = None
        try:
            if created not in (None, ""):
                age_ms = now_ms - int(created)
        except (TypeError, ValueError):
            age_ms = None
        group_id = index.get(client_id)
        verdict, reason = _judge(client_id=client_id, side=side, group_id=group_id,
                                 states=states, age_ms=age_ms,
                                 ttl_seconds=ttl_seconds, filled=filled)
        row_verdict = PendingVerdict(
            order_id=order_id, client_order_id=client_id,
            instrument=str(row.get("instId") or ""), side=side,
            size=_decimal(row.get("sz")), filled=filled,
            state=str(row.get("state") or ""), group_id=group_id, age_ms=age_ms,
            verdict=verdict, reason=reason)
        scan.orders.append(row_verdict)
        if verdict in ORPHAN_VERDICTS:
            scan.orphans.append(row_verdict)
            scan.reasons.append(f"{verdict}:{client_id or order_id}")
    return scan


def _judge(*, client_id: str, side: str, group_id: str | None,
           states: dict[str, str], age_ms: int | None, ttl_seconds: int,
           filled: Decimal) -> tuple[str, str]:
    """One order's verdict, most severe question first."""
    if not client_id:
        return "no_group_in_ledger", "order_carries_no_clordid"
    if group_id is None:
        return "no_group_in_ledger", f"clordid_{client_id}_not_in_ledger"
    state = states.get(group_id)
    if state is None:
        return "no_group_in_ledger", f"group_{group_id}_not_in_ledger"
    if state in TERMINAL_GROUP_STATES:
        return "group_already_terminal", f"group_{group_id}_ended_in_{state}"
    if age_ms is not None and age_ms > ttl_seconds * 1000:
        return "age_beyond_ttl", f"rested_{age_ms}ms_beyond_{ttl_seconds}s"
    want = expected_side(state)
    if want is None:
        return "direction_mismatch", f"group_{group_id}_in_{state}_rests_no_order"
    if side and side != want:
        return "direction_mismatch", f"group_{group_id}_in_{state}_expects_{want}"
    if filled > 0:
        # Partially filled while its group is not in an execution window: the
        # flow that placed it is gone, so nobody is watching the remainder.
        return "filled_while_idle", f"filled_{filled}_while_group_idle"
    return "attributed", f"group_{group_id}_active"
