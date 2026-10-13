"""Depth census: a measured empty pool is not a name we keep reading."""
from __future__ import annotations

from decimal import Decimal as D
import unittest

from arb_monitor.data.depth_census import (
    census_targets, excluded_assets, split_measurements, tokens_for_pool_read,
    watchlist_is_fresh,
)


class BoardFilterTests(unittest.TestCase):
    def test_names_under_the_depth_floor_are_omitted_from_the_board(self):
        from arb_monitor.apps.hk_monitor import summary_for_board

        summary = {
            "depth_watchlist": {"active": True, "assets": ["AAPL"]},
            "token_references": [{"asset": "AAPL"}, {"asset": "ZZZ"}],
            "instruments": [{"asset": "AAPL"}, {"asset": "ZZZ"}],
            "price_screen": {"rows": [{"asset": "AAPL"}, {"asset": "ZZZ"}]},
        }
        shown = summary_for_board(summary)
        self.assertEqual([row["asset"] for row in shown["token_references"]], ["AAPL"])
        self.assertEqual([row["asset"] for row in shown["instruments"]], ["AAPL"])
        self.assertEqual([row["asset"] for row in shown["price_screen"]["rows"]], ["AAPL"])
        raw = {"token_references": [{"asset": "ZZZ"}]}
        self.assertIs(summary_for_board(raw), raw)


class SplitTests(unittest.TestCase):
    def test_a_failed_read_is_not_recorded_as_no_depth(self):
        groups = split_measurements([
            {"asset": "AAPL", "pool": "0xabc", "depth_usd": "5000"},
            {"asset": "ZZZ", "pool": None, "depth_usd": None, "pools_seen": 0},
            {"asset": "QQQ", "pool": "0xdef", "depth_usd": "10"},
            {"asset": "ERR", "pool": None, "depth_usd": None, "error": "timeout"},
            {"asset": "APLD", "pool": None, "depth_usd": None,
             "error": "pool_not_initialized"},
        ], D("1000"))
        self.assertEqual([row["asset"] for row in groups["with_depth"]], ["AAPL"])
        self.assertEqual([row["asset"] for row in groups["no_depth"]],
                         ["ZZZ", "QQQ", "APLD"])
        self.assertEqual([row["asset"] for row in groups["unchecked"]], ["ERR"])

    def test_a_fresh_list_excludes_only_confirmed_empty_pools(self):
        document = {"scanned_at_ms": 1_000, "no_depth": [{"asset": "ZZZ"}],
                    "with_depth": [{"asset": "AAPL"}]}
        self.assertTrue(watchlist_is_fresh(document, 2_000, 10_000))
        self.assertEqual(excluded_assets(document, 2_000, 10_000), {"ZZZ"})
        self.assertIsNone(excluded_assets(document, 20_000, 10_000))
        tokens = [{"asset": "AAPL"}, {"asset": "ZZZ"}, {"asset": "XIAOMI"}]
        kept = tokens_for_pool_read(tokens, {"ZZZ"}, {"XIAOMI"})
        self.assertEqual([token["asset"] for token in kept], ["AAPL", "XIAOMI"])
        # No census: do not drop anyone.
        self.assertEqual(tokens_for_pool_read(tokens, None, set()), tokens)

    def test_configured_okx_names_are_measured_from_their_own_pools(self):
        issuer = [{"hk_code": "AAPL", "symbol": "AAPLx", "name": "Apple",
                   "listing_country": "US", "wrapper_address": "0xwrap",
                   "native_address": "0xnat"}]
        perps = {"AAPL": {}, "XIAOMI": {}}
        configured = [
            {"asset": "XIAOMI", "symbol": "wXIAOx", "address": "0xhk",
             "pools": [{"address": "0xpool"}]},
            {"asset": "AAPL", "symbol": "AAPLx", "address": "0xus",
             "pools": [{"address": "0xuspool"}]},
            {"asset": "NOT_ON_OKX", "pools": []},
        ]
        targets = {row["asset"]: row for row in census_targets(issuer, perps, configured)}
        self.assertEqual(set(targets), {"AAPL", "XIAOMI"})
        self.assertEqual(targets["AAPL"]["known_pools"][0]["address"], "0xuspool")
        self.assertEqual(targets["XIAOMI"]["known_pools"][0]["address"], "0xpool")


if __name__ == "__main__":
    unittest.main()
