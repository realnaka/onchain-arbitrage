"""The order floor is the venue's own minSz, converted into tokens.

One whole share is not that floor. OKX SWAP publishes minSz and lotSz in
contracts and does not publish a USD minimum; a USD floor is applied only when
a venue actually sends one.
"""
from decimal import Decimal
import unittest

from arb_monitor.apps.hk_monitor import HKMonitor
from arb_monitor.core.hk import Contract
from arb_monitor.core.sizing import largest_hedge_tokens, minimum_hedge_tokens

D = Decimal


def contract(**overrides) -> Contract:
    fields = dict(
        venue="okx", instrument_id="CRWV-USDT-SWAP", asset="CRWV",
        category="stock", payout_model="usd_linear", multiplier=D("1"),
        lot_size=D("0.01"), min_contracts=D("0.01"), min_notional=D("0"),
        tick_size=D("0.01"), active=True, source="test", recv_ts_ms=0,
    )
    fields.update(overrides)
    return Contract(**fields)


def token_for(ctr: Contract, shares_per_token="1") -> dict:
    return {
        "asset": ctr.asset, "decimals": 18,
        "mappings": {ctr.key: {
            "shares_per_token": shares_per_token,
            "shares_per_perp_unit": "1",
        }},
    }


class MinimumHedgeTests(unittest.TestCase):
    def test_crwv_minimum_is_a_hundredth_of_a_share(self):
        # minSz = lotSz = 0.01, ctVal = ctMult = 1, no USD minimum.
        tokens = minimum_hedge_tokens(
            shares_per_token=D("1"), shares_per_perp_unit=D("1"),
            contract_multiplier=D("1"), lot_size=D("0.01"),
            min_contracts=D("0.01"), min_notional=D("0"), token_decimals=18)
        self.assertEqual(tokens, D("0.01"))

    def test_a_published_usd_floor_raises_the_contract_count(self):
        # 5 USDT at 87.58 is 0.057 contracts, which ceils to the 0.01 lot: 0.06.
        tokens = minimum_hedge_tokens(
            shares_per_token=D("1"), shares_per_perp_unit=D("1"),
            contract_multiplier=D("1"), lot_size=D("0.01"),
            min_contracts=D("0.01"), min_notional=D("5"),
            perp_price=D("87.58"), token_decimals=18)
        self.assertEqual(tokens, D("0.06"))

    def test_a_usd_floor_without_a_price_does_not_block_the_contract_floor(self):
        tokens = minimum_hedge_tokens(
            shares_per_token=D("1"), shares_per_perp_unit=D("1"),
            contract_multiplier=D("1"), lot_size=D("0.01"),
            min_contracts=D("0.01"), min_notional=D("5"), perp_price=D("0"))
        self.assertEqual(tokens, D("0.01"))

    def test_absent_usd_floor_does_not_invent_one(self):
        tokens = minimum_hedge_tokens(
            shares_per_token=D("1"), shares_per_perp_unit=D("1"),
            contract_multiplier=D("1"), lot_size=D("0.01"),
            min_contracts=D("0.01"), min_notional=D("0"),
            perp_price=D("87.58"))
        self.assertEqual(tokens, D("0.01"))

    def test_zero_minimum_is_unknown(self):
        with self.assertRaises(ValueError):
            minimum_hedge_tokens(
                shares_per_token=D("1"), shares_per_perp_unit=D("1"),
                contract_multiplier=D("1"), lot_size=D("0.01"),
                min_contracts=D("0"))

    def test_wrapper_ratio_that_is_not_one_still_has_a_probe_size(self):
        # MRVL convertToAssets is not an integer wei divisor of minSz=0.01.
        # The exact search used to raise, and the name dropped off the quote grid.
        ratio = D("1.0016650410248724")
        tokens = minimum_hedge_tokens(
            shares_per_token=ratio, shares_per_perp_unit=D("1"),
            contract_multiplier=D("1"), lot_size=D("0.01"),
            min_contracts=D("0.01"), token_decimals=18)
        scale = D(10) ** 18
        self.assertEqual(tokens * scale, (tokens * scale).to_integral_value())
        desired = tokens * ratio
        self.assertGreaterEqual(desired, D("0.01"))
        self.assertLess(desired, D("0.02"))
        sized = largest_hedge_tokens(
            notional_cap=D("100"), perp_price=D("253"),
            shares_per_token=ratio, shares_per_perp_unit=D("1"),
            contract_multiplier=D("1"), lot_size=D("0.01"),
            min_contracts=D("0.01"), token_decimals=18)
        self.assertGreater(sized, tokens)
        # 100 / 253 = 0.3952 contracts, floored to the 0.01 lot.
        self.assertEqual((sized * ratio // D("0.01")) * D("0.01"), D("0.39"))

    def test_share_ratio_above_one_still_lands_on_the_lot(self):
        tokens = minimum_hedge_tokens(
            shares_per_token=D("2"), shares_per_perp_unit=D("1"),
            contract_multiplier=D("1"), lot_size=D("0.01"),
            min_contracts=D("0.01"))
        self.assertEqual(tokens, D("0.005"))
        contracts = tokens * D("2") / D("1") / D("1")
        self.assertEqual(contracts, D("0.01"))

    def test_cap_sizes_up_without_going_under_the_floor(self):
        sized = largest_hedge_tokens(
            notional_cap=D("100"), perp_price=D("87.58"),
            shares_per_token=D("1"), shares_per_perp_unit=D("1"),
            contract_multiplier=D("1"), lot_size=D("0.01"),
            min_contracts=D("0.01"), min_notional=D("0"))
        # 100 / 87.58 = 1.1418 contracts, floored to the 0.01 lot.
        self.assertEqual(sized, D("1.14"))

    def test_a_cap_below_the_floor_still_returns_the_floor(self):
        sized = largest_hedge_tokens(
            notional_cap=D("0.10"), perp_price=D("87.58"),
            shares_per_token=D("1"), shares_per_perp_unit=D("1"),
            contract_multiplier=D("1"), lot_size=D("0.01"),
            min_contracts=D("0.01"))
        self.assertEqual(sized, D("0.01"))


class ProbeSelectionTests(unittest.TestCase):
    def _holder(self, bid=None):
        holder = HKMonitor.__new__(HKMonitor)
        holder.config = {"execution_policy": {"limits": {"per_trade_quote_usdt": "100"}}}
        holder.price_screen = {"rows": []}
        if bid is not None:
            holder.price_screen = {"rows": [{
                "asset": "CRWV", "instrument": "CRWV-USDT-SWAP", "okx_bid": bid,
            }]}
        return holder

    def test_without_a_bid_the_probe_is_the_contract_minimum(self):
        ctr = contract()
        bounds = HKMonitor._probe_bounds(self._holder(), ctr, token_for(ctr))
        self.assertEqual(bounds, (D("0.01"), None))

    def test_with_a_bid_the_probe_uses_the_cash_ceiling_and_keeps_the_fallback(self):
        ctr = contract()
        bounds = HKMonitor._probe_bounds(
            self._holder("87.58"), ctr, token_for(ctr))
        self.assertEqual(bounds[0], D("1.14"))
        self.assertEqual(bounds[1], D("0.01"))

    def test_a_non_unit_wrapper_ratio_still_gets_probe_bounds(self):
        ctr = contract(instrument_id="MRVL-USDT-SWAP", asset="MRVL")
        holder = self._holder()
        holder.price_screen = {"rows": [{
            "asset": "MRVL", "instrument": "MRVL-USDT-SWAP", "okx_bid": "253",
        }]}
        bounds = HKMonitor._probe_bounds(
            holder, ctr, token_for(ctr, "1.0016650410248724"))
        self.assertIsNotNone(bounds)
        primary, fallback = bounds
        self.assertGreater(primary, fallback)

    def test_a_one_lot_contract_still_probes_one(self):
        ctr = contract(
            instrument_id="X-USDT-SWAP", asset="X",
            lot_size=D("1"), min_contracts=D("1"))
        bounds = HKMonitor._probe_bounds(self._holder(), ctr, token_for(ctr))
        self.assertEqual(bounds, (D("1"), None))


if __name__ == "__main__":
    unittest.main()
