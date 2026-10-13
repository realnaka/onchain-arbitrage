import tempfile
import unittest
from decimal import Decimal

from arb_monitor.core.fx_store import FxSnapshot, FxStore


class FxStoreTests(unittest.TestCase):
    def test_latest_snapshot_is_durable_and_decimal(self):
        with tempfile.TemporaryDirectory() as d:
            s = FxStore(f"{d}/fx.sqlite")
            s.put(FxSnapshot("USD/HKD", Decimal("7.8000"), 100, "operational_snapshot"))
            s.put(FxSnapshot("USD/HKD", Decimal("7.8010"), 200, "operational_snapshot"))
            latest = s.latest()
            self.assertEqual(latest.rate, Decimal("7.8010"))
            self.assertEqual(latest.source, "operational_snapshot")
            s.close()


if __name__ == "__main__":
    unittest.main()
