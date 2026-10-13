"""Evidence rules for TWO independent hypotheses; no external IO."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Sequence

from arb_monitor.core.hk import BPS, SpotQuote
from arb_monitor.core.hk_calendar import HK, HKCalendar


@dataclass(frozen=True)
class AnchorReference:
    previous_close_per_token: Decimal
    independent_current_per_token: Decimal
    previous_session: date
    observed_ts_ms: int
    valid_until_ms: int
    normalized_and_actions_checked: bool
    source: str
    asset: str
    quote_currency: str


def assess_probe(
    quotes: Sequence[SpotQuote], calendar: HKCalendar,
    special_route_ids: set[str], reference: AnchorReference | None,
    tolerance_bps: Decimal = Decimal("20"), min_move_bps: Decimal = Decimal("50"),
    min_observations: int = 3,
) -> dict:
    result = {
        "channel": "unknown", "yesterday_close_anchor": "inconclusive",
        "reason": "no_observations", "sample_count": 0,
        "note": "AMM availability does not establish RFQ availability; consistency is not proof of intent",
    }
    # Each hypothesis applies to ONE asset, side, size, day. Never mix groups.
    if not quotes:
        return result
    identities = {(q.asset, q.side, q.requested_amount, q.quote_currency,
                   q.chain_id, q.token_address, q.quote_address, q.source,
                   datetime.fromtimestamp(q.request_ts_ms // 1000, HK).date()) for q in quotes}
    if len(identities) != 1:
        raise ValueError("probe must be grouped by asset/side/amount/currency/HK date")
    rows = [q for q in quotes if calendar.phase(q.request_ts_ms) == "preopen_0900_0930"]
    if not rows:
        return {**result, "channel": "outside_0900_window", "reason": "no_trading_day_window_samples"}
    successful = [q for q in rows if q.status == "ok" and q.price is not None
                  and q.request_ts_ms <= q.recv_ts_ms < q.expires_ts_ms]
    if not successful:
        errors = {q.status for q in rows}
        reason = "measurement_failed:" + ",".join(sorted(errors))
        return {**result, "reason": reason}
    if not special_route_ids:
        return {**result, "channel": "quote_available_special_route_unknown",
                "reason": "special_liquidity_source_not_identified"}
    special = [q for q in successful if special_route_ids.intersection(q.routes)]
    if not special:
        return {**result, "channel": "special_route_not_observed",
                "reason": "aggregator_fallback_does_not_prove_channel_closed"}
    result.update(channel="special_route_observed", sample_count=len(special))
    if any(not set(q.routes).issubset(special_route_ids) for q in special):
        return {**result, "reason": "mixed_or_unidentified_route_cannot_attribute_anchor"}
    if not reference or not reference.normalized_and_actions_checked:
        return {**result, "reason": "missing_verified_close_fx_wrapper_corporate_action_reference"}
    if reference.asset != special[0].asset or reference.quote_currency != special[0].quote_currency:
        return {**result, "reason": "reference_identity_mismatch"}
    day = datetime.fromtimestamp(special[0].request_ts_ms // 1000, HK).date()
    if reference.previous_session != calendar.previous_session(day):
        return {**result, "reason": "reference_is_not_previous_trading_session"}
    special = [q for q in special if reference.observed_ts_ms <= q.recv_ts_ms <= reference.valid_until_ms]
    # Replayed/duplicated snapshots are not independent measurements.
    special = list({q.request_ts_ms: q for q in special}.values())
    result["sample_count"] = len(special)
    if len(special) < min_observations:
        return {**result, "reason": "insufficient_distinct_fresh_measurements"}
    old, current = reference.previous_close_per_token, reference.independent_current_per_token
    if min(old, current) <= 0:
        return {**result, "reason": "invalid_reference"}
    overnight = (current - old) / old * BPS
    old_errors = [abs(q.price / old - 1) * BPS for q in special]
    result.update(overnight_move_bps=overnight, old_close_distance_bps=old_errors)
    if abs(overnight) < min_move_bps:
        return {**result, "reason": "overnight_move_too_small_to_discriminate"}
    if all(x <= tolerance_bps for x in old_errors):
        return {**result, "yesterday_close_anchor": "consistent_with_old_close",
                "reason": "quotes_near_previous_close_despite_independent_price_move"}
    if all(x > tolerance_bps for x in old_errors):
        return {**result, "yesterday_close_anchor": "not_anchored_to_old_close_in_sample",
                "reason": "all_observed_quotes_outside_old_close_tolerance"}
    return {**result, "reason": "mixed_quote_behavior"}
