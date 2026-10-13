"""Hermetic checks for the xStocks route matrix."""
from __future__ import annotations

import importlib.util
import unittest
from decimal import Decimal
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "xstocks_route_matrix.py"
    spec = importlib.util.spec_from_file_location("xstocks_route_matrix", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()
        cls.deposits = {
            "STRCx": [
                {"method": "STRCx - Ethereum", "minimum": "0.01"},
                {"method": "STRCx", "minimum": "0.004"},
            ],
            "SPCXx": [{"method": "SPCXx - Solana", "minimum": "0.001"}],
        }

    def test_direct_deposit_matches_the_chain_label(self):
        got = self.mod.direct_deposit("STRCx", "eth", self.deposits)
        self.assertEqual(got["method"], "STRCx - Ethereum")

    def test_direct_deposit_does_not_match_another_chain(self):
        self.assertIsNone(self.mod.direct_deposit("STRCx", "bsc", self.deposits))

    def test_solana_method_needs_solana_chain(self):
        self.assertEqual(
            self.mod.direct_deposit("SPCXx", "solana", self.deposits)["method"],
            "SPCXx - Solana")

    def test_address_book_requires_the_same_asset_and_method(self):
        withdraw = {"method": "STRCx - Ethereum", "network": "Ethereum"}
        addresses = [
            {"key": "soltee1", "asset": "USDT", "method": "Tether USD (SPL)"},
            {"key": "xlayertee1", "asset": "USDG", "method": "USDG - Xlayer"},
        ]
        self.assertFalse(self.mod.address_ready("STRCx", "eth", withdraw, addresses))
        addresses.append({"key": "eth1", "asset": "STRCx", "method": "STRCx - Ethereum"})
        self.assertTrue(self.mod.address_ready("STRCx", "eth", withdraw, addresses))

    def test_funding_uses_usdc_ethereum_and_reports_fee(self):
        methods = {
            "USDC": [{
                "method": "USDC", "network": "Ethereum", "minimum": "1.2912",
                "fee": {"fee": "1.0760"},
            }],
        }
        got = self.mod.funding_for("eth", methods, [])
        self.assertEqual(got["asset"], "USDC")
        self.assertEqual(got["network"], "Ethereum")
        self.assertEqual(got["fee_units"], "1.0760")
        self.assertFalse(got["address_book_ready"])

    def test_x_layer_funding_can_use_the_existing_usdg_key(self):
        methods = {
            "USDG": [{
                "method": "USDG - Xlayer", "network": "Xlayer", "minimum": "2",
                "fee": {"fee": "0.5"},
            }],
        }
        addresses = [{"key": "xlayertee1", "asset": "USDG", "method": "USDG - Xlayer"}]
        got = self.mod.funding_for("x-layer", methods, addresses)
        self.assertEqual(got["asset"], "USDG")
        self.assertTrue(got["address_book_ready"])

    def test_network_map_accepts_both_bnb_spellings(self):
        self.assertEqual(self.mod.NETWORK_TO_CHAIN["BinanceSmartChain"], "bsc")
        self.assertEqual(self.mod.NETWORK_TO_CHAIN["BNB Smart Chain"], "bsc")


if __name__ == "__main__":
    unittest.main()
