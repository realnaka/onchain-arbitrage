"""The Kraken sell leg: order status and book-side helpers.

Only parsing and time arithmetic live here, so the sell leg can be tested
without a venue. The rule this module exists for is lesson 3.5: reading the
order five seconds after it was placed says "still open", not "not filled".
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from arb_monitor.venues.kraken_spot import (
    TOKENIZED, Nonce, tokenized_pair)

FILLED = "filled"
OPEN = "open"
UNKNOWN = "unknown"

# Measured: a resting order can fill well after the first read. Give it two
# minutes before calling it unfilled.
SELL_POLL_TIMEOUT_MS = 120_000


def pair_for(symbol: str, quote: str = "USD") -> str:
    """``MRNAx`` -> ``MRNAx/USD``, via the v1 client rather than a copy.

    Lesson 3.1: private endpoints reject ``MRNAXUSD``, the nonce must be
    nanoseconds, and a bad signature reports ``Invalid key``. There is exactly
    one implementation of that in the repo, and v2 must not grow a second.
    """
    return tokenized_pair(symbol, quote)


def make_nonce(clock=None) -> Nonce:
    """A strictly increasing nanosecond nonce from the shared implementation."""
    return Nonce(clock) if clock else Nonce()


def order_state(payload: object) -> str:
    """Kraken order row -> one of ``filled`` / ``open`` / ``unknown``."""
    if not isinstance(payload, dict):
        return UNKNOWN
    status = str(payload.get("status") or "").lower()
    closed = str(payload.get("closed") or "")
    vol = _d(payload.get("vol"))
    filled = _d(payload.get("vol_exec"))
    if status == "closed" and vol is not None and filled is not None and filled >= vol:
        return FILLED
    if status in {"open", "pending", "canceled", "expired"}:
        return OPEN if status != "canceled" else OPEN
    if status == "" and closed:
        return UNKNOWN
    return UNKNOWN


def settle_state(*, placed_ms: int, polls: list[tuple[int, str]],
                 timeout_ms: int = SELL_POLL_TIMEOUT_MS) -> str:
    """Walk the poll history. An unfilled order inside the window is not a close.

    ``polls`` is ``(ts_ms, state)`` in order. Only a fill, or the window
    expiring with no fill, is a decision.
    """
    if not polls:
        return UNKNOWN
    for stamp, state in polls:
        if state == FILLED:
            return FILLED
        if state == UNKNOWN:
            return UNKNOWN
    last_stamp = polls[-1][0]
    if last_stamp - placed_ms >= timeout_ms:
        return OPEN
    return UNKNOWN


def credited_units(*, before: Decimal, after: Decimal, minimum: Decimal) -> bool:
    """A deposit counts only when the delta clears the minimum.

    Dust (0.000001) used to read as a credit (lessons 3.4), and the minimum is
    the venue's effective minimum rather than the public ordermin.
    """
    if before is None or after is None:
        return False
    return (after - before) >= minimum


def _d(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
