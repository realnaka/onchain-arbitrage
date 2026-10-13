import unittest
from decimal import Decimal

from arb_monitor.execution.swap_transaction import SwapPhase, SwapTransaction


class SwapTransactionTests(unittest.TestCase):
    def test_approval_is_not_completion(self):
        tx = SwapTransaction("g1", Decimal("4"), Decimal("4.4"))
        tx.approval_confirmed("approve", Decimal("4"))
        self.assertEqual(tx.phase, SwapPhase.APPROVED)
        tx.swap_broadcast("swap")
        tx.swap_result(False)
        self.assertEqual(tx.phase, SwapPhase.RECOVERY_REQUIRED)

    def test_unbounded_or_insufficient_approval_rejected(self):
        tx = SwapTransaction("g2", Decimal("4"), Decimal("4.4"))
        with self.assertRaises(ValueError):
            tx.approval_confirmed("approve", Decimal("999999"))


if __name__ == "__main__":
    unittest.main()
