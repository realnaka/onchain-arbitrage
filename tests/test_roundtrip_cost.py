"""The alert has to show what a full cycle costs, not just the way in.

An entry basis of +23 bps sounds tradable until you notice that getting back out
also pays a spread and a taker fee. This is the number the operator needs in the
message, computed from the same sides the trade would take.
"""
from __future__ import annotations

from decimal import Decimal
import unittest

from arb_monitor.ops.notify import format_alert
from arb_monitor.strategy.xstocks_screen import breakeven_gross_bps, roundtrip_cost_bps


class RoundTripCostTests(unittest.TestCase):
    def cost(self, **overrides):
        values = dict(spot_ask=Decimal("4.83165"), spot_bid=Decimal("4.82500"),
                      perp_bid=Decimal("4.85"), perp_ask=Decimal("4.853"),
                      perp_fee_bps=Decimal("5"), gas_bps=Decimal("0"))
        values.update(overrides)
        return roundtrip_cost_bps(**values)

    def test_every_component_is_priced_on_the_side_the_trade_takes(self):
        cost = self.cost()
        # Buying the ask and selling the bid is the spot spread, not zero.
        self.assertAlmostEqual(float(cost["spot_spread_bps"]), 13.78, places=1)
        # Selling the bid and buying the ask is the perp spread.
        self.assertAlmostEqual(float(cost["perp_spread_bps"]), 6.19, places=1)
        # Taker on both legs.
        self.assertEqual(cost["perp_fee_bps"], Decimal("10"))
        self.assertEqual(cost["gas_bps"], Decimal("0"))
        self.assertAlmostEqual(float(cost["total_bps"]), 29.97, places=1)
        self.assertTrue(cost["complete"])

    def test_a_missing_side_is_not_silently_treated_as_free(self):
        cost = self.cost(perp_ask=None)
        self.assertIsNone(cost["total_bps"])
        self.assertFalse(cost["complete"])

    def test_breakeven_adds_the_buffer_and_the_expected_profit(self):
        cost = self.cost()
        breakeven = breakeven_gross_bps(cost, buffer_bps=Decimal("5"),
                                        expected_profit_bps=Decimal("20"))
        self.assertAlmostEqual(float(breakeven), float(cost["total_bps"]) + 25, places=6)

    def test_breakeven_is_unknown_when_the_cost_is_incomplete(self):
        self.assertIsNone(breakeven_gross_bps(self.cost(spot_bid=None),
                                              buffer_bps=Decimal("5"),
                                              expected_profit_bps=Decimal("20")))


class AlertRenderingTests(unittest.TestCase):
    def test_a_maker_round_trip_does_not_price_a_spread_it_never_crosses(self):
        # Task A (2026-09-16): with maker_first both perpetual legs rest at the
        # touch, so the round trip stops paying the tick spread and pays the
        # maker fee instead. This is what moves the entry bar from ~41 bps to
        # ~38 bps on the measured costs.
        taker = roundtrip_cost_bps(
            spot_ask=Decimal("4.83165"), spot_bid=Decimal("4.825"),
            perp_bid=Decimal("4.85"), perp_ask=Decimal("4.853"),
            perp_fee_bps=Decimal("5"), gas_bps=Decimal("0"))
        maker = roundtrip_cost_bps(
            spot_ask=Decimal("4.83165"), spot_bid=Decimal("4.825"),
            perp_bid=Decimal("4.85"), perp_ask=Decimal("4.853"),
            perp_fee_bps=Decimal("5"), gas_bps=Decimal("0"),
            perp_style="maker", perp_maker_fee_bps=Decimal("1.6"))
        self.assertEqual(maker["perp_spread_bps"], Decimal("0"))
        self.assertEqual(maker["perp_fee_bps"], Decimal("3.2"))
        self.assertEqual(maker["perp_style"], "maker")
        self.assertAlmostEqual(float(taker["total_bps"] - maker["total_bps"]),
                               6.19 + 6.8, places=1)
        # The spot spread is unchanged: it is paid on the venue that has no
        # maker concept at all.
        self.assertEqual(maker["spot_spread_bps"], taker["spot_spread_bps"])

    def test_a_maker_round_trip_does_not_need_the_ask(self):
        # Resting on the bid means the closing side never reads the ask, so a
        # missing ask must not turn a maker round trip into an unknown cost.
        maker = roundtrip_cost_bps(
            spot_ask=Decimal("4.83165"), spot_bid=Decimal("4.825"),
            perp_bid=Decimal("4.85"), perp_ask=None,
            perp_fee_bps=Decimal("5"), gas_bps=Decimal("0"),
            perp_style="maker", perp_maker_fee_bps=Decimal("1.6"))
        self.assertTrue(maker["complete"])
        self.assertIsNotNone(maker["total_bps"])

    def alert(self):
        cost = roundtrip_cost_bps(
            spot_ask=Decimal("4.83165"), spot_bid=Decimal("4.825"), perp_bid=Decimal("4.85"),
            perp_ask=Decimal("4.853"), perp_fee_bps=Decimal("5"), gas_bps=Decimal("0"))
        cost["breakeven_gross_bps"] = breakeven_gross_bps(
            cost, buffer_bps=Decimal("5"), expected_profit_bps=Decimal("20"))
        cost["buffer_bps"] = "5"
        cost["expected_profit_bps"] = "20"
        return {
            "asset": "SHEIN", "perp_venue": "okx", "perp_instrument": "SHEIN-USDT-SWAP",
            "spot_ask_usd": Decimal("4.83165"), "perp_bid_usd": Decimal("4.85"),
            "gross_entry_basis_bps": Decimal("37.98"),
            "estimated_entry_cost_bps": Decimal("15"),
            "estimated_net_entry_bps": Decimal("22.98"),
            "spot_source": "X Layer 池", "pool_depth_usd": Decimal("51000"),
            "pool_fee_bps": Decimal("5"), "cost_verified": True,
            "token_symbol": "wSHEINx",
            "token_address": "0xff637d2d435d6745df3faf61272b1216e7e8b727",
            "quote_token_symbol": "USDG",
            "quote_token_address": "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8",
            "pool_address": "0xf1ef85ce4691e94a32064b59e766c42183b44497",
            "chain_id": "196", "min_net_edge_bps": "20", "roundtrip": cost,
        }

    def test_the_message_shows_the_full_cycle_cost_and_the_breakeven(self):
        text = format_alert(self.alert(), 1_789_520_000_000)
        self.assertIn("一次往返总成本", text)
        self.assertIn("永续手续费（taker × 2 腿）: 10.0 bps", text)
        self.assertIn("合计: 30.0 bps", text)
        self.assertIn("出手门槛：毛基差 ≥ 55.0 bps", text)

    def test_an_incomplete_cost_says_so_instead_of_guessing(self):
        alert = self.alert()
        alert["roundtrip"] = {"spot_spread_bps": None, "perp_fee_bps": Decimal("10"),
                              "perp_spread_bps": Decimal("6.2"), "gas_bps": Decimal("0"),
                              "total_bps": None, "complete": False}
        text = format_alert(alert, 1_789_520_000_000)
        self.assertIn("分项缺失", text)
        self.assertNotIn("出手门槛", text)


if __name__ == "__main__":
    unittest.main()
