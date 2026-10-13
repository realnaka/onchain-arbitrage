"""Hermetic tests for the Kraken exit gate. No network."""
from __future__ import annotations

import json
import unittest
from decimal import Decimal

from arb_monitor.venues import kraken_exit_gate as gate


class AssetPairTests(unittest.TestCase):
    def test_reads_wsname_and_prefers_online(self):
        payload = {
            "MRNAXUSD": {"wsname": "MRNAx/USD", "status": "post_only",
                         "ordermin": "0.07", "costmin": "0.5"},
            "MRNAXUSD.alt": {"wsname": "MRNAx/USD", "status": "online",
                             "ordermin": "0.07", "costmin": "0.5"},
        }
        pairs = gate.parse_asset_pairs(payload)
        self.assertEqual(pairs["MRNAx"]["status"], "online")
        self.assertEqual(pairs["MRNAx"]["ordermin"], "0.07")

    def test_skips_rows_without_wsname(self):
        self.assertEqual(gate.parse_asset_pairs({"X": {"status": "online"}}), {})


class DepositTests(unittest.TestCase):
    def test_named_solana_method(self):
        methods = [{"method": "MRNAx - Solana", "minimum": "0.0164"}]
        self.assertEqual(gate.solana_deposit(methods, "MRNAx")["minimum"], "0.0164")

    def test_unnamed_method_same_as_symbol_counts_as_solana(self):
        methods = [{"method": "SPYx", "minimum": "0.0017"}]
        self.assertIsNotNone(gate.solana_deposit(methods, "SPYx"))

    def test_ethereum_only_is_not_solana(self):
        methods = [{"method": "XOMx - Ethereum", "minimum": "1"}]
        self.assertIsNone(gate.solana_deposit(methods, "XOMx"))

    def test_solana_source_ok(self):
        ok, reason = gate.reachable(
            "MRNAx", source_network="Solana",
            deposits={"MRNAx": [{"method": "MRNAx - Solana", "minimum": "0.0164"}]})
        self.assertTrue(ok)
        self.assertIn("Solana", reason)

    def test_solana_source_without_deposit_is_refused(self):
        ok, reason = gate.reachable(
            "XOMx", source_network="Solana",
            deposits={"XOMx": [{"method": "XOMx - Ethereum", "minimum": "1"}]})
        self.assertFalse(ok)
        self.assertIn("不收", reason)

    def test_uncached_symbol_is_refused_not_assumed(self):
        ok, reason = gate.reachable("ZZZx", source_network="Solana", deposits={})
        self.assertFalse(ok)
        self.assertIn("未核对", reason)

    def test_xlayer_needs_a_bridge(self):
        deps = {"CRWVx": [{"method": "CRWVx - Solana", "minimum": "0.06"}]}
        ok, _ = gate.reachable("CRWVx", source_network="XLayer", deposits=deps,
                               bridge_symbols={"CRWVx"})
        self.assertTrue(ok)
        ok, reason = gate.reachable("CRWVx", source_network="XLayer", deposits=deps,
                                    bridge_symbols=set())
        self.assertFalse(ok)
        self.assertIn("桥", reason)

    def test_unknown_chain_refused(self):
        ok, _ = gate.reachable(
            "CRWVx", source_network="Monad",
            deposits={"CRWVx": [{"method": "CRWVx - Solana", "minimum": "0.06"}]})
        self.assertFalse(ok)

    def test_bridge_symbols_from_products_json(self):
        products = json.dumps([{"symbol": "MRSHx",
                                "deployments": [{"network": "Solana", "address": "Xs1"},
                                                {"network": "XLayer", "address": "0x1"}]},
                               {"symbol": "NOBRIDGEx", "deployments": [{"network": "XLayer"}]}])
        self.assertEqual(gate.parse_bridge_symbols([products]), {"MRSHx"})


class BookExitTests(unittest.TestCase):
    def test_thin_order_fills_at_best_bid(self):
        bids = [[Decimal("190.55"), Decimal("0.62")], [Decimal("190.00"), Decimal("60")]]
        plan = gate.book_exit(bids, "0.54")
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["vwap"], "190.5500")
        self.assertEqual(plan["slippage_bps"], "0.0")

    def test_order_bigger_than_the_book_is_refused(self):
        bids = [[Decimal("100"), Decimal("0.01")]]
        plan = gate.book_exit(bids, "5")
        self.assertFalse(plan["ok"])
        self.assertIn("吃不下", plan["reason"])

    def test_share_of_in_range_book_is_capped(self):
        # 1 token of a 2 token book inside 50 bps -> 50% of the range
        bids = [[Decimal("100"), Decimal("1")], [Decimal("99.9"), Decimal("1")]]
        plan = gate.book_exit(bids, "1")
        self.assertFalse(plan["ok"])
        self.assertIn("盘口", plan["reason"])

    def test_empty_book(self):
        plan = gate.book_exit([], "0.5")
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["reason"], "盘口为空")

    def test_selling_through_levels_reports_slippage(self):
        bids = [[Decimal("100"), Decimal("1")], [Decimal("99"), Decimal("10")]]
        plan = gate.book_exit(bids, "2", max_book_share=Decimal("5"))
        self.assertEqual(plan["vwap"], "99.5000")
        self.assertEqual(plan["slippage_bps"], "50.0")


if __name__ == "__main__":
    unittest.main()
