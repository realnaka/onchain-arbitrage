"""Hong Kong entry point for the generic token-perp basis strategy.

The arithmetic never was Hong Kong specific, so the implementation lives in
``strategy.token_perp_basis`` and this module pins the Hong Kong defaults that
the existing configuration and tests were written against:

* ``listing_market = "HK"`` — the row is reported as a Hong Kong listing;
* the calendar phase gate is on (an unrecognised phase is still refused);
* no stablecoin conversion object is required, because each HK mapping carries
  its own reviewed ``quote_to_usdt`` with an evidence string and an expiry.

Nothing else changes: the class name, the strategy name used in Intents and
dedupe keys, and every rejection reason stay exactly as they were.
"""
from __future__ import annotations

from arb_monitor.core.markets import HK
from arb_monitor.strategy.token_perp_basis import (  # noqa: F401  (re-export)
    CALENDAR_GATED_MARKETS, DEFAULT_MAX_PEG_DEVIATION_BPS, STRATEGY_PHASES,
    TokenPerpBasisStrategy,
)

__all__ = ["HKBasisStrategy", "TokenPerpBasisStrategy", "STRATEGY_PHASES"]


class HKBasisStrategy(TokenPerpBasisStrategy):
    """``TokenPerpBasisStrategy`` with the reviewed Hong Kong defaults."""

    name = "hk_spot_long_perp_short"

    def __init__(self, *args, listing_market: str = HK,
                 require_quote_conversion: bool | None = False,
                 calendar_phase_gate: bool | None = True, **kwargs):
        super().__init__(*args, listing_market=listing_market,
                         require_quote_conversion=require_quote_conversion,
                         calendar_phase_gate=calendar_phase_gate, **kwargs)
