from __future__ import annotations

from decimal import Decimal, Decimal as D
import unittest

from arb_monitor.strategy.xstocks_screen import (
    entry_cost_bps, perp_side, pool_side_prices, screen_direction, size_plan,
)


def pool(**kwargs):
    base = {
        "address": "0xdc7f2f41b48cd4f482d8c900ac2fa1b5ad058417",
        "base_address": "0x076cf393e701839fc7a5832d2c68aafa235682ae",
        "base_decimals": 18,
        "quote_address": "0xb6ceceab302e2e4948951ee7843fc24e92933061",
        "quote_decimals": 6,
        "quote_symbol": "USDC",
        "fee": 500,
        # wXIAOx / USDC pool as read from chain: token0 = wXIAOx.
        "token0": "0x076cf393e701839fc7a5832d2c68aafa235682ae",
        "token1": "0xb6ceceab302e2e4948951ee7843fc24e92933061",
        "sqrt_price_x96": 145817780343793685746091,
        "liquidity": 1340289772061893550,
    }
    return {**base, **kwargs}


COSTS = {"spot_fee_bps": "0", "perp_fee_bps": "8", "slippage_bps": "5",
         "failure_bps": "5", "verified": False}


class PoolSideTests(unittest.TestCase):
    def test_buying_pays_more_than_selling_and_size_moves_price_against_you(self):
        small = pool_side_prices(pool(), D("100"))
        large = pool_side_prices(pool(), D("1000"))
        self.assertGreater(small["ask_usd"], small["mid_usd"])
        self.assertLess(small["bid_usd"], small["mid_usd"])
        self.assertGreater(large["ask_usd"], small["ask_usd"])
        self.assertLess(large["bid_usd"], small["bid_usd"])
        self.assertTrue(small["within_active_liquidity"])

    def test_pool_fee_is_charged_in_the_price_not_ignored(self):
        sides = pool_side_prices(pool(), D("100"))
        # Both impacts are positive "worse than mid" magnitudes, and the 0.05%
        # fee alone is 5 bps, so each side must be further from mid than that.
        self.assertGreater(sides["ask_impact_bps"], D("5"))
        self.assertGreater(sides["bid_impact_bps"], D("5"))
        self.assertLess(sides["bid_usd"], sides["mid_usd"])

    def test_empty_pool_has_no_price(self):
        sides = pool_side_prices(pool(liquidity=0), D("100"))
        self.assertIsNone(sides["ask_usd"])
        self.assertIsNone(sides["bid_usd"])
        self.assertEqual(sides["error"], "empty_or_uninitialized_pool")

    def test_quote_side_is_detected_from_token_ordering(self):
        # Same pool read with the base token holding the other slot: the price
        # inverts instead of silently reporting the quote token's price.
        inverted = pool(base_address=pool()["token1"], base_decimals=6,
                        quote_address=pool()["token0"], quote_decimals=18)
        sides = pool_side_prices(inverted, D("100"))
        self.assertAlmostEqual(float(sides["mid_usd"]), 0.29521, places=5)


class PerpSideTests(unittest.TestCase):
    def test_best_side_always_names_its_venue(self):
        quotes = [
            {"venue": "gate", "instrument": "XIAOMI_USDT", "bid": D("3.340"),
             "ask": D("3.344"), "fresh": True},
            {"venue": "okx", "instrument": "XIAOMI-USDT-SWAP", "bid": D("3.352"),
             "ask": D("3.353"), "fresh": True},
        ]
        short = perp_side(quotes, "bid")
        self.assertEqual((short["venue"], short["instrument"]), ("okx", "XIAOMI-USDT-SWAP"))
        self.assertEqual(short["price"], D("3.352"))
        # Long pays the lowest ask, not the highest bid.
        self.assertEqual(perp_side(quotes, "ask")["price"], D("3.344"))
        self.assertEqual(perp_side(quotes, "ask")["venue"], "gate")

    def test_stale_book_is_not_preferred_but_is_still_reported(self):
        quotes = [
            {"venue": "gate", "instrument": "A", "bid": D("1"), "ask": D("2"), "fresh": False},
            {"venue": "okx", "instrument": "B", "bid": D("0.9"), "ask": D("2.1"), "fresh": True},
        ]
        self.assertEqual(perp_side(quotes, "bid")["venue"], "okx")
        self.assertEqual(perp_side([quotes[0]], "bid")["venue"], "gate")


class DirectionTests(unittest.TestCase):
    def test_direction_a_buys_spot_ask_and_sells_perp_bid(self):
        result = screen_direction(
            "long_spot_short_perp",
            {"price": D("100")},
            {"venue": "gate", "instrument": "X_USDT", "price": D("101")},
            COSTS,
        )
        self.assertTrue(result["executable_pair"])
        self.assertAlmostEqual(float(result["gross_entry_basis_bps"]), 100.0, places=6)
        # Costs are subtracted, never hidden inside the price.
        self.assertAlmostEqual(float(result["estimated_net_entry_bps"]), 82.0, places=6)
        self.assertEqual(result["perp"]["venue"], "gate")

    def test_negative_net_basis_is_never_a_signal(self):
        result = screen_direction(
            "long_spot_short_perp",
            {"price": D("100")},
            {"venue": "gate", "instrument": "X_USDT", "price": D("100.10")},
            COSTS, min_edge_bps=D("20"),
        )
        self.assertEqual(result["judgement"], "not_executable_negative_net")
        self.assertFalse(result["is_candidate"])
        self.assertLess(float(result["estimated_net_entry_bps"]), 0)

    def test_thin_positive_net_basis_is_below_threshold_not_a_candidate(self):
        # +10 bps gross - 28 bps cost is negative; make it slightly positive by
        # using cheap costs, then check the minimum-edge gate still rejects it.
        cheap = {"spot_fee_bps": "1", "perp_fee_bps": "1", "slippage_bps": "1",
                 "failure_bps": "1", "verified": False}
        result = screen_direction(
            "long_spot_short_perp",
            {"price": D("100")},
            {"venue": "gate", "instrument": "X_USDT", "price": D("100.10")},
            cheap, min_edge_bps=D("20"),
        )
        self.assertGreater(float(result["estimated_net_entry_bps"]), 0)
        self.assertEqual(result["judgement"], "below_threshold")
        self.assertFalse(result["is_candidate"])

    def test_candidate_requires_clearing_the_configured_minimum_edge(self):
        result = screen_direction(
            "long_spot_short_perp",
            {"price": D("100")},
            {"venue": "gate", "instrument": "X_USDT", "price": D("101")},
            COSTS, min_edge_bps=D("20"),
        )
        self.assertEqual(result["judgement"], "candidate")
        self.assertTrue(result["is_candidate"])

    def test_mirror_direction_math_is_kept_testable_but_is_not_offered(self):
        # The mirror trade is not modelled in the monitor: X Layer has no
        # borrow market for these tokens, so the short-spot leg is not
        # openable. The arithmetic is pinned here anyway so a future change
        # cannot silently turn it into the same calculation as the long-spot
        # direction.
        result = screen_direction(
            "short_spot_long_perp",
            {"price": D("101")},
            {"venue": "binance", "instrument": "XUSDT", "price": D("100")},
            COSTS,
        )
        self.assertAlmostEqual(float(result["gross_entry_basis_bps"]), 99.0099, places=3)
        self.assertEqual(result["label"], "卖现货 + 多永续")

    def test_direction_a_is_negative_when_the_perp_bid_is_below_the_token_ask(self):
        result = screen_direction(
            "long_spot_short_perp",
            {"price": D("366.43")},
            {"venue": "gate", "instrument": "TSLA_USDT", "price": D("365.68")},
            COSTS,
        )
        self.assertLess(float(result["gross_entry_basis_bps"]), 0)
        self.assertLess(float(result["estimated_net_entry_bps"]), 0)

    def test_missing_side_stays_missing_rather_than_using_the_other_side(self):
        no_ask = screen_direction("long_spot_short_perp", None,
                                  {"venue": "gate", "instrument": "T", "price": D("1")}, COSTS)
        self.assertFalse(no_ask["executable_pair"])
        self.assertEqual(no_ask["reason"], "missing_onchain_spot_side")
        self.assertNotIn("gross_entry_basis_bps", no_ask)

    def test_cost_model_reports_each_component_and_gas_share(self):
        cost = entry_cost_bps(COSTS, gas_share_usd=D("0.05"), notional=D("100"))
        self.assertAlmostEqual(float(cost["total_bps"]), 23.0, places=6)
        self.assertFalse(cost["verified"])


class SizePlanTests(unittest.TestCase):
    def plan(self, **kwargs):
        pool_state = pool(liquidity=Decimal("1.34e18"),
                          depth_usd=D("300000"), quote_symbol="USDC")
        return size_plan(pool_state, [D("100"), D("500"), D("1000")],
                         D("3.45"), D("28"), D("20"), **kwargs)

    def test_every_size_gets_its_own_execution_price_and_slippage(self):
        rows = self.plan()
        prices = [float(r["spot_ask_usd"]) for r in rows]
        self.assertEqual(prices, sorted(prices))  # buying more must cost more
        self.assertLess(rows[0]["extra_slippage_bps"], rows[2]["extra_slippage_bps"])
        # The pool fee is reported separately from the extra price impact.
        self.assertEqual(float(rows[0]["pool_fee_bps"]), 5.0)

    def test_net_basis_and_profit_are_reported_in_absolute_terms(self):
        rows = self.plan()
        for row in rows:
            expected = (D("3.45") - row["spot_ask_usd"]) / row["spot_ask_usd"] * D("10000")
            self.assertAlmostEqual(float(row["gross_entry_basis_bps"]), float(expected), places=6)
            self.assertAlmostEqual(float(row["net_entry_basis_bps"]),
                                   float(expected - D("28")), places=6)
            self.assertGreater(float(row["gross_profit_quote"]), 0)
            self.assertAlmostEqual(float(row["token_quantity"]),
                                   float(row["size_usd"] / row["spot_ask_usd"]), places=9)

    def test_thin_pool_is_flagged_rather_than_silently_quoted(self):
        # 1% of the pool in one clip: impact must exceed the 20 bps ceiling.
        thin = pool(liquidity=Decimal("1.0e16"), depth_usd=D("3000"), quote_symbol="USDC")
        rows = size_plan(thin, [D("1000")], D("3.45"), D("28"), D("20"))
        self.assertFalse(rows[0]["depth_ok"])
        self.assertGreater(float(rows[0]["impact_vs_mid_bps"]), 20)

    def test_depth_share_is_reported_against_measured_depth(self):
        rows = self.plan()
        self.assertAlmostEqual(float(rows[2]["depth_share_pct"]), 1000 / 300000 * 100, places=6)


if __name__ == "__main__":
    unittest.main()
