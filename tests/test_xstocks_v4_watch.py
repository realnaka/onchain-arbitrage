"""Hermetic checks for the read-only Uniswap v4 watch."""
from __future__ import annotations

import importlib.util
import unittest
from decimal import Decimal
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "xstocks_v4_watch.py"
    spec = importlib.util.spec_from_file_location("xstocks_v4_watch", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HookPermissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_decodes_the_documented_univ4_example_address(self):
        perms = self.mod.permission_names("0x0000000000000000000000000000000000002400")
        self.assertEqual(perms, ["beforeInitialize", "afterAddLiquidity"])

    def test_zero_hook_has_no_permissions(self):
        self.assertEqual(self.mod.permission_names("0x" + "0" * 40), [])

    def test_scan_skips_push_data(self):
        found = self.mod.scan_opcodes("0x6032600055")
        self.assertFalse(found["ORIGIN"])
        self.assertTrue(found["SSTORE"])

    def test_classifies_dynamic_fee_as_amber(self):
        got = self.mod.hook_classification(
            "0x0000000000000000000000000000000000000080", 0x800000, [], {})
        self.assertEqual(got["tier"], "amber")
        self.assertTrue(got["dynamic_fee"])

    def test_classifies_return_delta_as_red(self):
        got = self.mod.hook_classification(
            "0x0000000000000000000000000000000000000088", 500, [], {})
        self.assertEqual(got["tier"], "red")
        self.assertTrue(got["hook_fee_capable"])

    def test_simulation_guard_is_red(self):
        got = self.mod.hook_classification(
            "0x0000000000000000000000000000000000000080",
            500, ["字节码使用 ORIGIN，能分辨真实交易和模拟"], {})
        self.assertEqual(got["tier"], "red")
        self.assertTrue(got["simulation_guard"])


class QuoteDecodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_decodes_amount_and_gas(self):
        raw = "0x" + format(1234, "064x") + format(56789, "064x")
        self.assertEqual(self.mod.decode_quote_result(raw), (1234, 56789))

    def test_classifies_not_enough_liquidity(self):
        text = "0x6190b2b0000000000000000000000000000000000000000000000000000000000000002000000000000000000000000000000000000000000000000000000000000000247a5ed7349"
        self.assertIn("活跃流动性", self.mod.classify_quote_error(text))


class KrakenUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_strcx_uses_the_verified_one_to_one_unit(self):
        self.assertEqual(
            self.mod.kraken_units_per_balance_unit("STRCx", Decimal("1.0918128111453242")),
            Decimal(1))

    def test_market_units_come_from_the_config_not_the_multiplier(self):
        self.assertEqual(
            self.mod.kraken_units_per_balance_unit("TQQQx", Decimal("2.0116905979372675")),
            Decimal(1))

    def test_strcx_order_minimum_is_enforced_on_the_underlying(self):
        self.assertEqual(
            self.mod.kraken_order_minimum("STRCx", Decimal("0.05"),
                                          Decimal("1.0918128111453242")),
            Decimal("0.05459063574070758198421675539"))


class EconomicsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_sell_vwap_walks_the_bid_book(self):
        gross, vwap = self.mod.sell_vwap(
            [["99.28", "50"], ["99.26", "23"]], Decimal("60"))
        self.assertEqual(gross, Decimal("5956.60"))
        self.assertEqual(vwap, Decimal("99.27666666666666666666666667"))

    def test_sell_vwap_is_empty_when_depth_is_too_thin(self):
        self.assertEqual(self.mod.sell_vwap([["99.28", "1"]], Decimal("2")), (None, None))

    def test_economics_subtract_kraken_fee_buffer_and_gas(self):
        row = self.mod.quote_economics(
            size_usd=Decimal("100"),
            shares=Decimal("1.1"),
            kraken_bid=Decimal("100"),
            gas_usd=Decimal("0.5"),
        )
        self.assertEqual(row["gross_usdt"], Decimal("110.0"))
        self.assertEqual(row["kraken_fee_usdt"], Decimal("0.110"))
        self.assertEqual(row["net_usdt"], Decimal("9.29"))

    def test_pick_economics_requires_deposit_and_order_minimums(self):
        quotes = [
            {"usd": Decimal("5"), "shares": Decimal("0.04"), "share": Decimal("125")},
            {"usd": Decimal("20"), "shares": Decimal("0.16"), "share": Decimal("125")},
        ]
        picked = self.mod.pick_economics(
            quotes,
            kraken_bid=Decimal("130"),
            gas_usd=Decimal("0.2"),
            deposit_min=Decimal("0.05"),
            order_min=Decimal("0.05"),
        )
        self.assertEqual(picked["largest_profitable"]["usd"], Decimal("20"))
        self.assertEqual(picked["rows"][0]["order_ok"], False)


if __name__ == "__main__":
    unittest.main()
