import unittest
from arb_monitor.execution.mev import MEVPolicy, SafeBroadcaster


class MEVTests(unittest.TestCase):
    def test_protection_is_enabled_at_broadcast_boundary(self):
        p = MEVPolicy()
        payload = SafeBroadcaster(p).prepare("0xsigned", "1", "0xabc")
        self.assertTrue(payload["extraData"]["enableMevProtection"])

    def test_xlayer_fails_closed_until_support_is_verified(self):
        with self.assertRaises(ValueError):
            SafeBroadcaster().prepare("0xsigned", "196", "0xabc")

    def test_public_fallback_is_not_implicit(self):
        b = SafeBroadcaster(MEVPolicy(enabled=True), allow_public_fallback=False)
        with self.assertRaises(NotImplementedError):
            b.broadcast("0xsigned", "1", "0xabc")


if __name__ == "__main__":
    unittest.main()
