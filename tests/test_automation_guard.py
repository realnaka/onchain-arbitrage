import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
import time

from arb_monitor.execution.automation_guard import AutomationPolicy


class AutomationGuardTests(unittest.TestCase):
    def test_spot_confirmation_is_hard_gate(self):
        with tempfile.TemporaryDirectory() as d:
            p = AutomationPolicy(kill_switch_file=str(Path(d) / "kill"))
            ok, reason = p.allows(asset="XIAOMI", chain_id="196",
                                  spot_token="x", quote_token="q", venue="okx",
                                  instrument="XIAOMI-USDT-SWAP", wallet="w",
                                  quote_amount=Decimal("4"),
                                  quote_observed_at=time.time(),
                                  spot_filled=Decimal("0"), open_contracts=Decimal("0"))
            self.assertFalse(ok)
            self.assertEqual(reason, "spot_not_confirmed")

    def test_expired_quote_and_kill_switch_block(self):
        with tempfile.TemporaryDirectory() as d:
            kill = Path(d) / "kill"
            p = AutomationPolicy(kill_switch_file=str(kill), quote_ttl_seconds=10)
            kill.touch()
            self.assertEqual(p.allows(asset="X", chain_id="1", spot_token="s",
                quote_token="q", venue="okx", instrument="i", wallet="w",
                quote_amount=Decimal("1"), quote_observed_at=time.time(),
                spot_filled=Decimal("1"), open_contracts=Decimal("0"))[1], "kill_switch")


if __name__ == "__main__":
    unittest.main()
