"""Hermetic tests for the sell-size ladder.

The ladder exists because one quote at the full size is a point, not a curve.
These tests pin the three ways a point-quote lies: a size the route cannot
price, a full size that is much worse than a quarter size, and a ladder whose
larger size looks better (two quotes that do not describe one book).
"""
from __future__ import annotations

import unittest
from decimal import Decimal as D

from arb_monitor.xstocks_v2 import ladder


class LadderSizesTest(unittest.TestCase):
    def test_the_default_ladder_is_quarter_steps_up_to_the_whole_size(self):
        self.assertEqual(ladder.ladder_sizes(1_000_000),
                         [250_000, 500_000, 750_000, 1_000_000])

    def test_the_largest_point_is_always_the_full_size(self):
        """A rounded 0.99 total must still end at the total, not at 99%."""
        sizes = ladder.ladder_sizes(1_000_003)
        self.assertEqual(sizes[-1], 1_000_003)
        self.assertEqual(sorted(set(sizes)), sizes)

    def test_a_tiny_balance_gets_a_shorter_ladder_not_a_zero_point(self):
        sizes = ladder.ladder_sizes(3)
        self.assertEqual(sizes, [1, 2, 3])
        self.assertNotIn(0, sizes)

    def test_a_zero_or_negative_size_has_no_ladder(self):
        self.assertEqual(ladder.ladder_sizes(0), [])
        self.assertEqual(ladder.ladder_sizes(-5), [])


class LadderGateTest(unittest.TestCase):
    def _points(self, head, tail, *, sizes=(100, 400)):
        return [(sizes[0], D(head)), (sizes[1], D(tail))]

    def test_a_flat_book_passes(self):
        ok, reason = ladder.ladder_ok(self._points("1.0000", "1.0000"),
                                      max_bps=D("10"))
        self.assertTrue(ok, reason)

    def test_five_bps_of_degradation_passes_a_ten_bps_allowance(self):
        ok, _ = ladder.ladder_ok(self._points("1.0000", "0.9995"), max_bps=D("10"))
        self.assertTrue(ok)

    def test_fifteen_bps_of_degradation_is_refused(self):
        ok, reason = ladder.ladder_ok(self._points("1.0000", "0.9985"),
                                      max_bps=D("10"))
        self.assertFalse(ok)
        self.assertTrue(reason.startswith(ladder.DEGRADATION), reason)
        self.assertIn("15.00", reason)

    def test_a_single_point_is_not_a_ladder(self):
        ok, reason = ladder.ladder_ok([(400, D("1.0"))], max_bps=D("10"))
        self.assertFalse(ok)
        self.assertEqual(reason, ladder.INCOMPLETE)

    def test_out_of_order_points_are_refused(self):
        ok, reason = ladder.ladder_ok([(400, D("1.0")), (100, D("1.0"))],
                                      max_bps=D("10"))
        self.assertFalse(ok)
        self.assertEqual(reason, ladder.ORDER)

    def test_a_bigger_size_getting_a_better_price_is_not_a_fill(self):
        """Two quotes that disagree by more than tolerance describe two books."""
        ok, reason = ladder.ladder_ok(self._points("1.0000", "1.0100"),
                                      max_bps=D("10"))
        self.assertFalse(ok)
        self.assertTrue(reason.startswith(ladder.NON_MONOTONIC), reason)

    def test_a_non_positive_price_is_refused(self):
        ok, reason = ladder.ladder_ok([(100, D("1.0")), (400, D("0"))],
                                      max_bps=D("10"))
        self.assertFalse(ok)
        self.assertIn("non_positive_price", reason)

    def test_confirm_returns_the_full_size_ratio(self):
        ok, reason, worst = ladder.confirm(self._points("1.0000", "0.9995"),
                                           max_bps=D("10"))
        self.assertTrue(ok, reason)
        self.assertEqual(worst, D("0.9995"))

    def test_confirm_returns_no_ratio_when_the_ladder_fails(self):
        ok, _, worst = ladder.confirm(self._points("1.0000", "0.9900"),
                                      max_bps=D("10"))
        self.assertFalse(ok)
        self.assertIsNone(worst)

    def test_average_price_is_out_over_in(self):
        self.assertEqual(ladder.average_price(400, 399), D("0.9975"))
        with self.assertRaises(ValueError):
            ladder.average_price(0, 1)


class LadderShapeTest(unittest.TestCase):
    #: Four quotes of SPYx -> USDC on Solana read 2026-10-07 at 19:0x, at
    #: 25/50/75/100% of a 1 033 496-raw position. Amounts are raw in and raw out.
    MEASURED_SOLANA_LADDER = {
        258_374: 2_019_701,
        516_748: 4_039_394,
        775_122: 6_059_079,
        1_033_496: 8_078_756,
    }

    def test_the_real_spy_x_ladder_shape_passes(self):
        """The Solana exit is flat over this range: 0.06 bps across four sizes.

        That is the point of the ladder -- it turns "the route exists at the full
        size" into "the route exists at every size and the last quarter is not
        filled much worse".
        """
        sizes = ladder.ladder_sizes(1_033_496)
        self.assertEqual(sizes, sorted(self.MEASURED_SOLANA_LADDER))
        points = [(size, ladder.average_price(size, self.MEASURED_SOLANA_LADDER[size]))
                  for size in sizes]
        ok, reason = ladder.ladder_ok(points, max_bps=D("10"))
        self.assertTrue(ok, reason)
        self.assertLess(float(reason.split(":")[1].rstrip("bps")), 0.1)

    def test_the_same_ladder_fails_a_tighter_than_measured_allowance(self):
        """The allowance is a parameter, so the gate is not a constant."""
        sizes = ladder.ladder_sizes(1_033_496)
        points = [(size, ladder.average_price(size, self.MEASURED_SOLANA_LADDER[size]))
                  for size in sizes]
        ok, reason = ladder.ladder_ok(points, max_bps=D("0.01"))
        self.assertFalse(ok)
        self.assertTrue(reason.startswith(ladder.DEGRADATION), reason)


if __name__ == "__main__":
    unittest.main()
