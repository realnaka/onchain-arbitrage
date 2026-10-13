"""Path scoreboard. The denominator is rounds where both legs were fresh."""
from __future__ import annotations


def opportunity_rate(*, priced_rounds: int, opportunity_rounds: int) -> float | None:
    if priced_rounds <= 0:
        return None
    return opportunity_rounds / priced_rounds


_UNTRUSTED = frozenset({"stale_or_skew", "cost_unknown"})


def tally(rows: list[dict]) -> dict:
    """Stale and unknown-cost rows are untrusted. They are not opportunities.

    Rows that were not priced this round are absent from the denominator.
    """
    priced = 0
    hits = 0
    untrusted = 0
    for row in rows:
        if row.get("reason") in _UNTRUSTED:
            untrusted += 1
            continue
        if not row.get("priced"):
            continue
        priced += 1
        if row.get("hit"):
            hits += 1
    return {"priced": priced, "hits": hits, "untrusted": untrusted}
