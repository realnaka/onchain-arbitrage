"""赚钱优先（operator 2026-10-08）：盘口只定价，利润说话。

Two rules are locked here, both from the operator's own words (AGENTS.md
「第一原则：赚钱优先」):

* the orderbook (and the pool) is asked "what price does **this amount** get";
  no slippage cap and no book-share cap may veto a trade;
* a per-message cost (bridge fee, first-transfer rent) is amortised by size, so
  a row that loses at the base ticket can still pay at the ceiling -- and the
  ladder trades that rung.
"""
from __future__ import annotations

import unittest
from decimal import Decimal as D
from tempfile import TemporaryDirectory

from arb_monitor.xstocks_v2 import scan
from arb_monitor.xstocks_v2.feeds import kraken
from arb_monitor.xstocks_v2.policy import V2Policy


class BookFillTest(unittest.TestCase):
    """``book_fill`` answers a price question, never an economic one."""

    def test_a_thin_best_bid_is_priced_not_vetoed(self):
        """MCDx 14:27: 235.00 for 0.008511, then 230.52 -- the real 2026-10-08 book."""
        bids = [["235.00", "0.008511"], ["230.52", "0.534104"],
                ["230.51", "5.197912"]]
        fill = kraken.book_fill(bids, D("0.1062666"), side="sell")
        self.assertTrue(fill["ok"])
        self.assertEqual(fill["best"], D("235.00"))
        self.assertEqual(fill["levels_used"], 2)
        # 2.000 + 23.053 = 25.053 over 0.1062666 units -> 230.60
        self.assertLess(fill["vwap"], D("231"))
        self.assertGreater(fill["vwap"], D("230.5"))
        self.assertGreater(fill["slip_bps"], D("170"))
        self.assertEqual(fill["top_notional_usd"], D("2.000085"))

    def test_a_book_that_cannot_fill_says_so(self):
        fill = kraken.book_fill([["235.00", "0.008511"]], D("0.5"), side="sell")
        self.assertFalse(fill["ok"])
        self.assertEqual(D(fill["short"]), D("0.491489"))
        self.assertEqual(fill["levels_used"], 1)

    def test_an_unsorted_book_is_sorted_before_it_is_walked(self):
        bids = [["230.52", "0.5"], ["235.00", "1"]]
        fill = kraken.book_fill(bids, D("1"), side="sell")
        self.assertEqual(fill["best"], D("235.00"))
        self.assertEqual(fill["vwap"], D("235.00"))
        self.assertEqual(fill["slip_bps"], D("0"))

    def test_the_buy_side_walks_the_asks(self):
        asks = [["235.49", "0.029815"], ["235.50", "0.388545"]]
        fill = kraken.book_fill(asks, D("0.1"), side="buy")
        self.assertEqual(fill["best"], D("235.49"))
        self.assertGreater(fill["vwap"], D("235.49"))
        self.assertLess(fill["vwap"], D("235.50"))

    def test_an_empty_or_meaningless_book_is_not_a_price(self):
        for levels, units in (([], D("1")), (None, D("1")),
                              ([["1", "1"]], D("0")), ([["0", "5"]], D("1")),
                              ([["1", "0"]], D("1"))):
            with self.subTest(levels=levels, units=units):
                fill = kraken.book_fill(levels, units, side="sell")
                self.assertFalse(fill["ok"])
                self.assertIsNone(fill["vwap"])


class _FakeHttp:
    """Kraken public reads only: the repo's own feeds turn payloads into books."""

    def __init__(self, bids, asks=None, sol_usd="116.40"):
        self.bids = bids
        self.asks = asks or [["999", "1"]]
        self.sol_usd = sol_usd

    def get_json(self, url, **kwargs):        # noqa: ARG002
        if "Ticker" in url:
            if self.sol_usd is None:
                return 200, {"error": [], "result": {}}, {}
            return 200, {"error": [], "result": {"SOLUSD": {
                "b": [self.sol_usd, "1", "1"],
                "a": [self.sol_usd, "1", "1"]}}}, {}
        return 200, {"error": [], "result": {"MCDx/USD": {
            "bids": self.bids, "asks": self.asks}}}, {}


class RepriceKrakenLegTest(unittest.TestCase):
    """Round 1 prices the Kraken leg on the book, for our own ticket."""

    def setUp(self):
        self.policy = V2Policy.load()

    def _row(self, *, sell="235.00", buy="230.3051097915863956311378926",
             notional="25"):
        return {
            "symbol": "MCDx", "family": "chain_to_kraken", "chain": "solana",
            "buy_venue": "jupiter", "sell_venue": "kraken",
            "buy_price_usd": buy, "sell_price_usd": sell,
            "edge_t0_bps": "203.8552341570807864782360000",
            "notional_usdt": notional, "blockers": [], "executable": True,
            "reason": "", "kraken_price_basis": "best_bid",
        }

    def test_the_two_dollar_best_bid_becomes_the_real_fill_price(self):
        row = self._row()
        counters: dict = {}
        with TemporaryDirectory() as tmp:
            done = scan.reprice_kraken_legs(
                [row], http=_FakeHttp([["235.00", "0.008511"],
                                       ["230.52", "0.534104"],
                                       ["230.51", "5.197912"]]),
                policy=self.policy, session="premarket", counters=counters,
                data_dir=tmp)
        self.assertEqual(done, 1)
        self.assertEqual(row["kraken_price_basis"], "book_vwap")
        self.assertEqual(row["kraken_best_price"], "235.00")
        self.assertLess(D(row["sell_price_usd"]), D("231"))
        self.assertLess(D(row["edge_t0_bps"]), D("50"))     # +203 -> ~+20 bps
        # The spread is still above the 4 bps bar, and the row is refused on the
        # **net** the executor would really pay: transfer + taker + buffer.
        self.assertNotIn("below_threshold", row["blockers"])
        self.assertIn("below_net", row["blockers"])
        self.assertLess(D(row["net_usdt"]), D("0"))
        self.assertFalse(row["executable"])
        self.assertGreater(D(row["book_slip_bps"]), D("170"))
        self.assertEqual(counters["kraken_depth_calls"], 1)

    def test_a_book_too_thin_to_fill_is_not_a_price_opinion(self):
        row = self._row(notional="200")
        with TemporaryDirectory() as tmp:
            done = scan.reprice_kraken_legs(
                [row], http=_FakeHttp([["235.00", "0.008511"]]),
                policy=self.policy, session="premarket", counters={},
                data_dir=tmp)
        self.assertEqual(done, 1)
        self.assertIn("kraken_depth_short", row["blockers"])
        self.assertFalse(row["executable"])

    def test_a_row_far_below_the_bar_is_not_worth_a_depth_call(self):
        row = self._row(sell="200.00")
        row["edge_t0_bps"] = "-2000"
        counters: dict = {}
        self.assertEqual(scan.reprice_kraken_legs(
            [row], http=_FakeHttp([["235.00", "1"]]), policy=self.policy,
            session="premarket", counters=counters,
            data_dir="/nonexistent"), 0)
        self.assertEqual(counters, {})
        self.assertEqual(row["kraken_price_basis"], "best_bid")


class FixedCostAmortisesTest(unittest.TestCase):
    """A per-message cost must not make a 200 USDT opportunity look like a loss."""

    def setUp(self):
        self.policy = V2Policy.load()

    def _row(self):
        return {"symbol": "NVDAx", "family": "chain_via_bridge_to_kraken",
                "buy_price_usd": "100.9", "sell_price_usd": "101",
                "edge_t0_bps": "100", "notional_usdt": "25",
                "blockers": [], "executable": True, "reason": ""}

    def test_the_base_ticket_net_is_kept_as_evidence(self):
        row = self._row()
        scan.apply_net_blocker(row, D("0.099"), self.policy,
                               fixed_cost_usd=D("0.101"),
                               family=row["family"])
        self.assertEqual(row["net_usdt"], "0.099")
        self.assertNotIn("below_net", row["blockers"])
        self.assertEqual(row["net_note"], "fixed_cost_amortises_with_size")
        self.assertGreater(D(row["net_at_ceiling_usdt"]),
                           self.policy.min_net_profit_usdt)
        self.assertEqual(row["net_ceiling_size_usdt"], "200")

    def test_a_row_that_loses_at_every_size_is_still_blocked(self):
        row = self._row()
        row["edge_t0_bps"] = "5"          # 5 bps cannot pay a per-message fee
        scan.apply_net_blocker(row, D("-0.4"), self.policy,
                               fixed_cost_usd=D("0.101"),
                               family=row["family"])
        self.assertIn("below_net", row["blockers"])
        self.assertFalse(row["executable"])

    def test_a_row_with_no_fixed_cost_is_judged_where_it_was_asked(self):
        row = self._row()
        scan.apply_net_blocker(row, D("-0.02"), self.policy,
                               family=row["family"])
        self.assertIn("below_net", row["blockers"])


if __name__ == "__main__":
    unittest.main()
