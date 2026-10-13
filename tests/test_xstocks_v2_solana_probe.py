"""The Solana probe must report the balance delta, not the delta minus the spend.

Measured 2026-10-08: two probes (KLACx 2 USDC at 13:10, PFEx 2 USDC at 22:21)
wrote ``net_usdt = -4`` while the wallets had barely moved. Both used the same
wrong formula: ``net = (after - before) - spend``, which charges the buy twice.
"""
from __future__ import annotations

import importlib.util
import unittest
from decimal import Decimal as D
from pathlib import Path


def _probe():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "v2solprobe", root / "scripts" / "xstocks_v2_solana_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RoundTripNumbersTest(unittest.TestCase):
    """No network, no wallet: the arithmetic that lands in the ledger."""

    @classmethod
    def setUpClass(cls):
        cls.mod = _probe()

    def test_a_perfect_round_trip_is_zero_not_minus_the_spend(self):
        numbers = self.mod.round_trip_numbers(before_raw=8_481_916,
                                              after_raw=8_481_916,
                                              spend_raw=2_000_000)
        self.assertEqual(numbers["spend_usdc"], "2")
        self.assertEqual(numbers["received_usdc"], "2")      # 卖腿把本金还回来了
        self.assertEqual(D(numbers["net_usdc"]), D("0"))     # 旧公式在这里记 −2
        self.assertEqual(D(numbers["round_trip_bps"]), D("0"))

    def test_a_fee_only_round_trip_is_the_fee(self):
        # 2 USDC 往返花掉 0.006 USDC（0.3%）——这是 2026-10-08 实测的量级。
        numbers = self.mod.round_trip_numbers(before_raw=8_481_916,
                                              after_raw=8_475_511,
                                              spend_raw=2_000_000)
        self.assertEqual(D(numbers["net_usdc"]), D("-0.006405"))
        self.assertEqual(D(numbers["received_usdc"]), D("1.993595"))
        self.assertEqual(D(numbers["round_trip_bps"]), D("32.025"))

    def test_the_measured_two_units_that_became_minus_four(self):
        """The exact shape of the wrong row: the balance fell by ~2, the sell brought 0."""
        numbers = self.mod.round_trip_numbers(before_raw=8_481_916,
                                              after_raw=6_481_969,
                                              spend_raw=2_000_000)
        self.assertEqual(D(numbers["net_usdc"]), D("-1.999947"))   # 不是 −3.999947
        self.assertEqual(D(numbers["received_usdc"]), D("0.000053"))

    def test_a_profitable_round_trip_is_positive(self):
        numbers = self.mod.round_trip_numbers(before_raw=8_481_916,
                                              after_raw=8_521_916,
                                              spend_raw=2_000_000)
        self.assertEqual(D(numbers["net_usdc"]), D("0.04"))
        # +0.04 on a 2 USDC spend = −200 bps（负号 = 赚）。
        self.assertEqual(D(numbers["round_trip_bps"]), D("-200.00"))

    def test_a_zero_spend_has_no_ratio(self):
        numbers = self.mod.round_trip_numbers(before_raw=1, after_raw=1, spend_raw=0)
        self.assertIsNone(numbers["round_trip_bps"])
        self.assertEqual(D(numbers["net_usdc"]), D("0"))

    def test_the_old_double_charge_is_gone(self):
        """A regression guard: net must not be ``delta - spend`` (the old formula)."""
        numbers = self.mod.round_trip_numbers(before_raw=1_000_000,
                                              after_raw=1_000_000,
                                              spend_raw=2_000_000)
        spend = D(numbers["spend_usdc"])
        old_formula = D("0") - spend          # delta - spend，旧口径在这里记 −2
        self.assertNotEqual(D(numbers["net_usdc"]), old_formula)
        self.assertEqual(D(numbers["net_usdc"]), D("0"))


if __name__ == "__main__":
    unittest.main()
