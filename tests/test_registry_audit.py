import unittest

from arb_monitor.core.registry_audit import audit_mapping
from arb_monitor.core.venue_registry import AssetVenueMapping, ChainAsset, VenueInstrument


class RegistryAuditTests(unittest.TestCase):
    def make(self, **kwargs):
        chain = ChainAsset("XIAOMI", "wXIAOx", "196",
                           "0x" + "a" * 40, 18, "1810.HK")
        instrument = VenueInstrument(
            "okx", "XIAOMI-USDT-SWAP", "SWAP", "live", "XIAOMI",
            "USDT", "1", "1", "1", "USDT", "okx-api", "2026-09-15",
        )
        values = dict(asset="XIAOMI", chain=chain, instrument=instrument,
                      verified=False, verification_basis=("api_observed",))
        values.update(kwargs)
        return AssetVenueMapping(**values)

    def test_unverified_mapping_is_not_executable(self):
        result = audit_mapping(self.make())
        self.assertTrue(result.api_present)
        self.assertTrue(result.chain_identity_complete)
        self.assertTrue(result.underlying_name_match)
        self.assertFalse(result.verified)
        self.assertIn("mapping_not_verified", result.reasons)

    def test_mismatched_underlying_is_rejected(self):
        result = audit_mapping(self.make(
            instrument=VenueInstrument(
                "okx", "KUAI-USDT-SWAP", "SWAP", "live", "KUAI",
                "USDT", "1", "1", "1", "USDT", "okx-api", "2026-09-15",
            )
        ))
        self.assertFalse(result.underlying_name_match)
        self.assertIn("underlying_not_explicitly_matched", result.reasons)

    def test_verified_mapping_requires_no_remaining_reasons(self):
        result = audit_mapping(self.make(
            verified=True,
            verification_basis=("api_identity", "issuer_identity", "manual_review"),
        ))
        self.assertTrue(result.verified)
        self.assertEqual(result.reasons, ())


if __name__ == "__main__":
    unittest.main()
