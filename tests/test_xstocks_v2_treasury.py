"""The treasury names a refill. It does not sign, and a short venue is not a halt."""
from __future__ import annotations

import unittest
from decimal import Decimal

from arb_monitor.xstocks_v2.treasury import plan


def _bal(rows):
    return {key: Decimal(str(value)) for key, value in rows.items()}


class TreasuryPlanTest(unittest.TestCase):
    def test_xlayer_dust_withdraws_the_kraken_minimum(self):
        moves = plan(_bal({
            ("x-layer", "USDG"): "0.000001",
            ("kraken", "USDG"): "3.02613",
            ("kraken", "ZUSD"): "5",
        }))
        ready = [row for row in moves if row["status"] == "ready"]
        self.assertEqual(ready[0]["rail"], "kraken_withdraw")
        self.assertEqual(ready[0]["key"], "xlayertee1")
        self.assertEqual(ready[0]["amount"], "2.50")
        self.assertEqual(ready[0]["arrives"], "1.50")

    def test_a_full_xlayer_wallet_is_not_refilled(self):
        moves = plan(_bal({
            ("x-layer", "USDG"): "1.5",
            ("kraken", "USDG"): "3",
            ("kraken", "ZUSD"): "5",
        }))
        self.assertEqual(moves, [])

    def test_kraken_without_the_minimum_is_named_not_sent(self):
        moves = plan(_bal({
            ("x-layer", "USDG"): "0",
            ("kraken", "USDG"): "0.5",
            ("kraken", "ZUSD"): "5",
        }))
        self.assertEqual(moves[0]["status"], "short_source")
        self.assertNotIn("ready", [row["status"] for row in moves])

    def test_an_unread_balance_is_not_treated_as_zero(self):
        moves = plan(_bal({("kraken", "USDG"): "10", ("kraken", "ZUSD"): "5"}))
        self.assertEqual(moves, [])

    def test_zusd_short_and_usdc_above_the_pair_minimum_sells(self):
        moves = plan(_bal({
            ("x-layer", "USDG"): "2",
            ("kraken", "USDG"): "3",
            ("kraken", "ZUSD"): "0.17",
            ("kraken", "USDC"): "5.1",
            ("eth", "USDC"): "99",
        }))
        self.assertEqual(moves[0]["rail"], "kraken_sell_usdc")
        self.assertEqual(moves[0]["amount"], "5")

    def test_zusd_short_and_usdc_below_five_deposits_the_gap(self):
        moves = plan(_bal({
            ("x-layer", "USDG"): "2",
            ("kraken", "USDG"): "3",
            ("kraken", "ZUSD"): "0.17",
            ("kraken", "USDC"): "2.10",
            ("eth", "USDC"): "99.29",
        }))
        self.assertEqual(moves[0]["rail"], "eth_usdc_deposit")
        self.assertEqual(moves[0]["status"], "ready")
        self.assertEqual(Decimal(moves[0]["amount"]), Decimal("2.90"))

    def test_the_ethereum_reserve_is_not_spent(self):
        moves = plan(_bal({
            ("x-layer", "USDG"): "2",
            ("kraken", "USDG"): "3",
            ("kraken", "ZUSD"): "0.17",
            ("kraken", "USDC"): "0",
            ("eth", "USDC"): "20",
        }))
        self.assertEqual(moves[0]["status"], "short_source")


if __name__ == "__main__":
    unittest.main()
