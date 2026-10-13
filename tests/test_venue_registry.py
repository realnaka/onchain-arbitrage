import json
import tempfile
import unittest

from arb_monitor.core.venue_registry import (
    AssetVenueMapping, ChainAsset, VenueInstrument, discover_okx_instruments,
    merge_registry, read_registry, write_registry,
)


class VenueRegistryTests(unittest.TestCase):
    def test_okx_rows_are_normalized_without_name_guessing(self):
        rows = discover_okx_instruments([{
            "instType": "SWAP", "instId": "XIAOMI-USDT-SWAP", "state": "live",
            "instFamily": "XIAOMI-USDT", "settleCcy": "USDT", "ctVal": "1",
            "minSz": "1", "lotSz": "1",
        }])
        self.assertEqual(rows[0].base_underlying, "XIAOMI")
        self.assertEqual(rows[0].instrument_id, "XIAOMI-USDT-SWAP")

    def test_mapping_is_not_executable_until_explicitly_verified(self):
        chain = ChainAsset("KUAISHOU", "wKUAIx", "196", "0xabc", 18)
        instrument = VenueInstrument("okx", "KUAI-USDT-SWAP", "SWAP", "live",
                                     "KUAI", "USDT", "1", "1", "1", "USDT")
        mapping = AssetVenueMapping("KUAISHOU", chain, instrument)
        self.assertFalse(mapping.executable())

    def test_registry_round_trip(self):
        chain = ChainAsset("XIAOMI", "wXIAOx", "196", "0xabc", 18)
        instrument = VenueInstrument("okx", "XIAOMI-USDT-SWAP", "SWAP", "live",
                                     "XIAOMI", "USDT", "1", "1", "1", "USDT")
        mapping = AssetVenueMapping("XIAOMI", chain, instrument, True,
                                    ("api_identity_match",), {"source": "test"})
        with tempfile.NamedTemporaryFile() as f:
            write_registry(f.name, [mapping])
            restored = read_registry(f.name)
        self.assertTrue(restored[0].verified)
        self.assertEqual(restored[0].instrument.instrument_id, "XIAOMI-USDT-SWAP")

    def test_refresh_merge_preserves_manual_verification(self):
        chain = ChainAsset("XIAOMI", "wXIAOx", "196", "0xabc", 18)
        old_inst = VenueInstrument("okx", "XIAOMI-USDT-SWAP", "SWAP", "live",
                                   "XIAOMI", "USDT", "1", "1", "1", "USDT",
                                   "old", "old")
        new_inst = VenueInstrument("okx", "XIAOMI-USDT-SWAP", "SWAP", "live",
                                   "XIAOMI", "USDT", "1", "1", "1", "USDT",
                                   "new", "new")
        old = AssetVenueMapping("XIAOMI", chain, old_inst, True, ("manual",), {"x": 1})
        new = AssetVenueMapping("XIAOMI", chain, new_inst, False)
        merged = merge_registry([old], [new])
        self.assertTrue(merged[0].verified)
        self.assertEqual(merged[0].verification_basis, ("manual",))
        self.assertEqual(merged[0].instrument.source, "new")


if __name__ == "__main__":
    unittest.main()
