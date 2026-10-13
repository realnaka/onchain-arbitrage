"""The candidate row must carry the round-trip cost, not just the entry cost.

This is what turns "SHEIN net entry basis +23 bps" into "and getting back out
costs 62 bps", which is the difference between an alert worth acting on and an
alert that would have lost money.
"""
from __future__ import annotations

from decimal import Decimal
import unittest

from arb_monitor.apps.hk_monitor import candidate_roundtrip
from arb_monitor.core.hk import RoundTripCosts


def costs(**overrides) -> RoundTripCosts:
    values = dict(perp_fee_bps=Decimal("5"), spot_fee_bps=Decimal("0"),
                  slippage_bps=Decimal("5"), failure_bps=Decimal("5"),
                  roundtrip_gas_budget_usdt=Decimal("0"))
    values.update(overrides)
    return RoundTripCosts(**values)


ROW = {
    "asset": "SHEIN",
    "onchain_spot_ask_usd": Decimal("4.831650097225057"),
    "onchain_spot_bid_usd": Decimal("4.826776299348344"),
    "perp_quotes": [
        {"venue": "okx", "instrument": "SHEIN-USDT-SWAP",
         "bid": Decimal("4.84"), "ask": Decimal("4.86")},
        {"venue": "gate", "instrument": "SHEIN_USDT",
         "bid": Decimal("4.83"), "ask": Decimal("4.85")},
    ],
}
DIRECTION = {"perp": {"venue": "okx", "instrument": "SHEIN-USDT-SWAP",
                      "price": Decimal("4.84")}}


class CandidateRoundtripTests(unittest.TestCase):
    def test_it_uses_the_spreads_of_the_chosen_venue_only(self):
        result = candidate_roundtrip(row=ROW, direction=DIRECTION, costs=costs(),
                                     target=Decimal("3"), min_edge_bps=Decimal("20"),
                                     buffer_bps=Decimal("5"), expected_profit_bps=Decimal("20"))
        # spot 10.1 bps + okx perp (4.86/4.84) 41.3 bps + 10 bps fees
        self.assertAlmostEqual(float(result["spot_spread_bps"]), 10.1, places=1)
        self.assertAlmostEqual(float(result["perp_spread_bps"]), 41.3, places=1)
        self.assertEqual(result["perp_fee_bps"], Decimal("10"))
        self.assertAlmostEqual(float(result["total_bps"]), 61.4, places=1)
        # The point of the number: a +23 bps entry basis cannot pay for it.
        # cost 61.4 + buffer 5 + expected profit 20
        self.assertAlmostEqual(float(result["breakeven_gross_bps"]), 86.4, places=1)
        self.assertEqual(result["assets"], "SHEIN okx:SHEIN-USDT-SWAP")

    def test_a_tighter_perpetual_book_costs_less(self):
        row = {**ROW, "perp_quotes": [{"venue": "okx", "instrument": "SHEIN-USDT-SWAP",
                                       "bid": Decimal("4.840"), "ask": Decimal("4.842")}]}
        result = candidate_roundtrip(row=row, direction=DIRECTION, costs=costs(),
                                     target=Decimal("3"), min_edge_bps=Decimal("20"),
                                     buffer_bps=Decimal("5"), expected_profit_bps=Decimal("20"))
        self.assertLess(float(result["total_bps"]), 26)

    def test_a_missing_ask_makes_the_total_unknown_not_cheaper(self):
        row = {**ROW, "perp_quotes": [{"venue": "okx", "instrument": "SHEIN-USDT-SWAP",
                                       "bid": Decimal("4.84")}]}
        result = candidate_roundtrip(row=row, direction=DIRECTION, costs=costs(),
                                     target=Decimal("3"), min_edge_bps=Decimal("20"),
                                     buffer_bps=Decimal("5"), expected_profit_bps=Decimal("20"))
        self.assertIsNone(result["total_bps"])
        self.assertFalse(result["complete"])

    def test_an_unverified_gas_budget_is_not_counted_as_zero(self):
        result = candidate_roundtrip(
            row=ROW, direction=DIRECTION,
            costs=costs(roundtrip_gas_budget_usdt=None), target=Decimal("3"),
            min_edge_bps=Decimal("20"), buffer_bps=Decimal("5"),
            expected_profit_bps=Decimal("20"))
        # With no gas budget configured the total must stay incomplete rather
        # than silently pretending gas is free.
        self.assertFalse(result["complete"])

    def test_the_configured_order_style_decides_how_the_perp_legs_are_priced(self):
        # The alert must be priced the way the trade would be executed: with
        # maker_first the perpetual legs rest, so the tick spread disappears and
        # the maker fee applies. Otherwise the bar stays ~3 bps too high.
        taker = candidate_roundtrip(
            row=ROW, direction=DIRECTION, costs=costs(), target=Decimal("3"),
            min_edge_bps=Decimal("20"), buffer_bps=Decimal("5"),
            expected_profit_bps=Decimal("20"))
        maker = candidate_roundtrip(
            row=ROW, direction=DIRECTION, costs=costs(perp_maker_fee_bps=Decimal("1.6")),
            target=Decimal("3"), min_edge_bps=Decimal("20"),
            buffer_bps=Decimal("5"), expected_profit_bps=Decimal("20"),
            perp_style="maker")
        self.assertEqual(maker["perp_style"], "maker")
        self.assertEqual(maker["perp_spread_bps"], Decimal("0"))
        self.assertEqual(maker["perp_fee_bps"], Decimal("3.2"))
        self.assertAlmostEqual(
            float(taker["breakeven_gross_bps"] - maker["breakeven_gross_bps"]),
            41.3 + 6.8, places=1)


if __name__ == "__main__":
    unittest.main()
