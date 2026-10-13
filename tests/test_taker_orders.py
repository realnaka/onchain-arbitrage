from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import unittest

from arb_monitor.apps.account_dashboard import risk_parameters
from arb_monitor.execution.intent import (ExecutionIntent, LiquidityRole, OrderType,
                                          taker_limit_price)

ROOT = Path(__file__).resolve().parents[1]


def intent(**overrides) -> ExecutionIntent:
    values = dict(
        signal_id="taker-1", asset="XIAOMI", chain_id="196",
        token_address="0x076cf393e701839fc7a5832d2c68aafa235682ae",
        quote_token_address="0x4ae46a509f6b1d9056937ba4500cb143933d2dc8",
        spot_amount=Decimal("4"), expected_token_quantity=Decimal("1.17"),
        perp_contract_quantity=Decimal("1"), perp_venue="okx",
        perp_instrument="XIAOMI-USDT-SWAP", net_edge_bps=Decimal("25"),
        expected_profit_quote=Decimal("0.1"), created_ts_ms=1, expires_ts_ms=2,
        max_slippage_bps=Decimal("20"), gas_budget_quote=Decimal("0"),
        perp_limit_price=Decimal("3.3665"),
    )
    values.update(overrides)
    return ExecutionIntent(**values)


class TakerOrderTests(unittest.TestCase):
    def test_both_legs_are_takers_by_construction(self):
        perp, spot = intent().legs()
        self.assertEqual(perp.liquidity_role, LiquidityRole.TAKER)
        self.assertEqual(spot.liquidity_role, LiquidityRole.TAKER)
        self.assertEqual(perp.order_type, OrderType.IOC)

    def test_the_perp_leg_carries_a_bounded_limit_price(self):
        perp, _ = intent().legs()
        # An IOC without a price could not be submitted, and an unbounded price
        # is not a taker order, it is an open cheque.
        self.assertEqual(perp.limit_price, Decimal("3.3665"))

    def test_selling_takes_the_bid_and_gives_up_at_most_the_band(self):
        self.assertEqual(taker_limit_price(Decimal("3.37"), Decimal("20"), side="sell"),
                         Decimal("3.37") * (Decimal(1) - Decimal("0.002")))
        with self.assertRaises(ValueError):
            taker_limit_price(Decimal("0"), Decimal("20"))
        with self.assertRaises(ValueError):
            taker_limit_price(Decimal("3.37"), Decimal("-1"))

    def test_buy_side_band_is_the_mirror_image(self):
        self.assertGreater(taker_limit_price(Decimal("3.37"), Decimal("20"), side="buy"),
                           Decimal("3.37"))

    def test_policy_now_rests_the_hedge_instead_of_crossing_it(self):
        risk = risk_parameters(str(ROOT / "config/automation_policy.toml"),
                               str(ROOT / "config/hk_monitor.toml"))
        # Changed 2026-09-16: the perpetual's spread (a function of its tick)
        # was the dominant cost, so the hedge rests on the offer as a maker.
        self.assertEqual(risk["orders"]["style"], "maker_first")
        self.assertEqual(risk["orders"]["perp_order_type"], "post_only")
        self.assertEqual(risk["orders"]["perp_price_source"], "best_ask")
        self.assertTrue(risk["orders"]["allow_maker_orders"])
        self.assertTrue(risk["orders"]["allow_resting_orders"])


if __name__ == "__main__":
    unittest.main()
