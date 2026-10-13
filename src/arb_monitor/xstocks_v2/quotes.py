"""Quote freshness. Age and skew use local receive time."""
from __future__ import annotations

REFERENCE_SOURCES = frozenset({"reference", "mid", "dexscreener"})


def quotes_fresh(*, now_ms: int, recv_a_ms: int, recv_b_ms: int,
                 max_age_ms: int, max_pair_skew_ms: int) -> bool:
    if now_ms - recv_a_ms > max_age_ms or now_ms - recv_b_ms > max_age_ms:
        return False
    return abs(recv_a_ms - recv_b_ms) <= max_pair_skew_ms


def executable_prices(source: str, buy: object, sell: object) -> tuple[object, object] | None:
    """Reference and mid prices never enter net profit."""
    if source in REFERENCE_SOURCES:
        return None
    return buy, sell
