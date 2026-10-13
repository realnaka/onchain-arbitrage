from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from arb_monitor.apps.hk_monitor import load_config
from arb_monitor.data.xstocks_registry import (
    discovered_token_entry, hk_shaped_code, is_hk_listing, listing_market,
    match_cex_asset, new_tokens, render_discovered_toml,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/hk_monitor.toml"


def asset(symbol="BYDCOx", name="BYD xStock", code="1211",
          native="0x" + "a" * 40, wrapper="0x" + "b" * 40, country="HK"):
    return {"symbol": symbol, "name": name, "hk_code": code,
            "listing_country": country,
            "market": country or "UNKNOWN", "native_address": native,
            "wrapper_address": wrapper}


class ListingMarketTests(unittest.TestCase):
    """The venue comes from the issuer, and a missing one is never guessed."""

    def test_issuer_country_decides_the_market(self):
        self.assertEqual(listing_market(asset()), "HK")
        self.assertEqual(listing_market(asset("AAPLx", "Apple xStock", "AAPL",
                                             country="US")), "US")
        self.assertTrue(is_hk_listing(asset()))
        self.assertFalse(is_hk_listing(asset("SKHYx", "SK hynix xStock", "SKHY",
                                             country="US")))

    def test_unlisted_country_is_unknown_not_a_guess(self):
        # Xerox-style rows carry a numeric-free ticker and a null country; the
        # ISIN prefix is the issuer's domicile, so it cannot be used here.
        row = asset("XRXx", "Xerox xStock", "XRX", country=None)
        self.assertEqual(listing_market(row), "UNKNOWN")
        self.assertFalse(is_hk_listing(row))
        self.assertFalse(hk_shaped_code(row))

    def test_hk_codes_are_numeric_and_us_tickers_are_not(self):
        self.assertTrue(hk_shaped_code(asset(code="1810")))
        self.assertTrue(hk_shaped_code(asset(code="4")))
        self.assertTrue(hk_shaped_code(asset(code="625")))
        self.assertFalse(hk_shaped_code(asset("AAPLx", "Apple xStock", "AAPL",
                                              country="US")))

    def test_discovered_entry_carries_the_market(self):
        entry = discovered_token_entry(
            asset("SKHYx", "SK hynix xStock", "SKHY", country="US"),
            "wrapper", "0x" + "b" * 40,
            {"address": "0x" + "e" * 40, "quote_symbol": "USDG",
             "quote_address": "0x" + "f" * 40, "fee": 500},
            D("232182"), None, 1_789_436_402_678)
        self.assertEqual(entry["market"], "US")
        self.assertIn('market = "US"', render_discovered_toml([entry]))


class DiscoveryDiffTests(unittest.TestCase):
    def test_only_never_seen_addresses_are_new_listings(self):
        known = {"0x" + "b" * 40}
        self.assertEqual(new_tokens([asset()], known), [])
        self.assertEqual(len(new_tokens([asset(native="0x" + "c" * 40,
                                               wrapper="0x" + "d" * 40)], known)), 1)

    def test_cex_match_ignores_spacing_and_the_xstock_suffix(self):
        self.assertEqual(match_cex_asset(asset(), {"BYD", "XIAOMI"}), "BYD")
        self.assertEqual(
            match_cex_asset(asset("POPMTx", "Pop Mart International xStock", "9992"),
                            {"POPMART", "XIAOMI"}), "POPMART")
        self.assertIsNone(
            match_cex_asset(asset("HRZRBx", "Horizon Robotics xStock", "9660"),
                            {"HSBC", "KUAISHOU"}))
        self.assertIsNone(match_cex_asset(asset("AIx", "C3.ai xStock", "AI"),
                                          {"HSBC", "MIDEA"}))


class DiscoveredConfigTests(unittest.TestCase):
    def test_discovered_entry_carries_pool_provenance(self):
        entry = discovered_token_entry(
            asset(), "wrapper", "0x" + "b" * 40,
            {"address": "0x" + "e" * 40, "quote_symbol": "USDG",
             "quote_address": "0x" + "f" * 40, "fee": 500},
            D("71215.83"), "BYD", 1_700_000_000_000)
        self.assertTrue(entry["auto_discovered"])
        self.assertEqual(entry["asset"], "BYD")
        self.assertEqual(entry["symbol"], "wBYDCOx")
        self.assertEqual(entry["pools"][0]["fee"], 500)
        self.assertEqual(entry["depth_at_discovery_usd"], "71215.83")

    def test_rendered_file_is_loadable_and_merged_into_the_monitor(self):
        entry = discovered_token_entry(
            asset(symbol="TESTx", name="Test Corp xStock", code="9999",
                  native="0x" + "1" * 40, wrapper="0x" + "2" * 40),
            "wrapper", "0x" + "2" * 40,
            {"address": "0x" + "3" * 40, "quote_symbol": "USDC",
             "quote_address": "0x" + "4" * 40, "fee": 500},
            D("5000"), None, 1_700_000_000_000)
        with TemporaryDirectory() as directory:
            discovered = Path(directory) / "discovered_tokens.toml"
            discovered.write_text(render_discovered_toml([entry]))
            loadable = CONFIG.read_text().replace(
                'discovered_config = "config/discovered_tokens.toml"',
                f'discovered_config = "{discovered}"', 1)
            local = Path(directory) / "hk_monitor.toml"
            local.write_text(loadable)
            loaded = load_config(str(local))
            self.assertIn("TESTx", loaded["monitor"]["assets"])
            merged = [t for t in loaded["tokens"] if t.get("auto_discovered")]
            self.assertEqual([t["asset"] for t in merged], ["TESTx"])
            self.assertEqual(merged[0]["pools"][0]["quote_symbol"], "USDC")

    def test_an_address_already_in_the_config_is_not_duplicated(self):
        existing = [t for t in load_config(str(CONFIG))["tokens"]
                    if t["asset"] == "SHEIN"][0]
        entry = discovered_token_entry(
            asset(symbol="SHEINx", name="SHEIN xStock", code="625",
                  native="0x" + "5" * 40, wrapper=existing["address"]),
            "wrapper", existing["address"], existing["pools"][0], D("90000"),
            "SHEIN", 1_700_000_000_000)
        with TemporaryDirectory() as directory:
            discovered = Path(directory) / "discovered_tokens.toml"
            discovered.write_text(render_discovered_toml([entry]))
            loadable = CONFIG.read_text().replace(
                'discovered_config = "config/discovered_tokens.toml"',
                f'discovered_config = "{discovered}"', 1)
            local = Path(directory) / "hk_monitor.toml"
            local.write_text(loadable)
            loaded = load_config(str(local))
            addresses = [t["address"].lower() for t in loaded["tokens"]]
            self.assertEqual(len(addresses), len(set(addresses)))


if __name__ == "__main__":
    unittest.main()
