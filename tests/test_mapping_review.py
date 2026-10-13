import unittest

from arb_monitor.core.mapping_review import eligibility_reason, review_mapping
from arb_monitor.core.venue_registry import AssetVenueMapping, ChainAsset, VenueInstrument


class MappingReviewTests(unittest.TestCase):
    def setUp(self):
        self.mapping = AssetVenueMapping(
            "XIAOMI",
            ChainAsset("XIAOMI", "wXIAOx", "196", "0x" + "a" * 40, 18, "1810.HK"),
            VenueInstrument("okx", "XIAOMI-USDT-SWAP", "SWAP", "live", "XIAOMI",
                            "USDT", "1", "1", "1", "USDT", "api", "2026-09-15"),
        )

    def evidence(self):
        return {
            "issuer_asset_source": "issuer-api:asset-1810",
            "cex_instrument_source": "okx-api:instrument-XIAOMI-USDT-SWAP",
            "underlying_match_source": "issuer-symbol-and-okx-instFamily",
            "contract_spec_source": "okx-api:ctVal/minSz/lotSz",
            "observed_at": "2026-09-15T00:00:00Z",
        }

    def test_approval_requires_all_evidence(self):
        with self.assertRaises(ValueError):
            review_mapping(self.mapping, {}, approve=True, reviewer="tester")

    def test_approval_is_eligible_only_after_evidence(self):
        reviewed = review_mapping(self.mapping, self.evidence(),
                                  approve=True, reviewer="tester",
                                  reviewed_at="2026-09-15T01:00:00Z")
        self.assertTrue(reviewed.verified)
        self.assertEqual(eligibility_reason(reviewed), "eligible")

    def test_rejection_remains_ineligible(self):
        reviewed = review_mapping(self.mapping, {}, approve=False, reviewer="tester")
        self.assertFalse(reviewed.verified)
        self.assertEqual(eligibility_reason(reviewed), "mapping_not_verified")


if __name__ == "__main__":
    unittest.main()
