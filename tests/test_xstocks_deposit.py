"""Hermetic tests for the Kraken deposit module. No network, no signing."""
from __future__ import annotations

import json
import struct
import unittest
from decimal import Decimal

from arb_monitor.execution import xstocks_deposit as dep

MINT = "XsfCC9VL4DamVGNgdJpfLXB3sBVa158Gbx8sh7NzmTk"
METHODS = [{"method": "VIDAx - Solana", "minimum": "0.378", "fee": "0"}]
ADDR = [{"address": "48SEVUduDW84t9SLxzGdxXb55qnx74wgLA3eGAYtKPyX"}]


class EncodingTests(unittest.TestCase):
    def test_raw_amount_never_rounds_up(self):
        self.assertEqual(dep.to_raw(Decimal("6.44780479"), 8), 644780479)
        self.assertEqual(dep.to_raw(Decimal("0.000000019"), 8), 1)
        self.assertEqual(dep.to_raw(Decimal("0.000000001"), 8), 0)

    def test_transfer_checked_layout(self):
        data = dep.transfer_checked_data(Decimal("6.44780479"), 8)
        self.assertEqual(data[0], dep.TRANSFER_CHECKED)
        self.assertEqual(struct.unpack("<Q", data[1:9])[0], 644780479)
        self.assertEqual(data[9], 8)
        self.assertEqual(len(data), 10)

    def test_zero_amount_is_rejected(self):
        with self.assertRaises(ValueError):
            dep.transfer_checked_data(Decimal("0.000000001"), 8)

    def test_create_ata_idempotent_opcode(self):
        self.assertEqual(dep.create_ata_idempotent_data(), bytes([1]))


class MethodTests(unittest.TestCase):
    def test_named_solana_method(self):
        self.assertIsNotNone(dep.find_solana_method(METHODS, "VIDAx"))

    def test_unnamed_method_counts_as_solana(self):
        rows = [{"method": "SPYx", "minimum": "0.0017"}]
        self.assertIsNotNone(dep.find_solana_method(rows, "SPYx"))

    def test_ethereum_only_is_not_solana(self):
        rows = [{"method": "XOMx - Ethereum", "minimum": "1"}]
        self.assertIsNone(dep.find_solana_method(rows, "XOMx"))


class PlanTests(unittest.TestCase):
    def plan(self, tokens, methods=METHODS, addresses=ADDR):
        return dep.plan_deposit(symbol="VIDAx", mint=MINT, tokens=Decimal(tokens),
                                methods=methods, addresses=addresses)

    def test_happy_path(self):
        p = self.plan("6.44780479")
        self.assertTrue(p.ok, p.reason)
        self.assertEqual(p.raw_amount, 644780479)
        self.assertEqual(p.destination, ADDR[0]["address"])
        self.assertEqual(p.token_program, dep.TOKEN_2022_PROGRAM)
        self.assertIn("48SEVU", p.reason)

    def test_below_minimum_is_refused(self):
        p = self.plan("0.2")
        self.assertFalse(p.ok)
        self.assertIn("永久不到账", p.reason)

    def test_missing_address_is_refused(self):
        p = self.plan("6.44", addresses=[])
        self.assertFalse(p.ok)
        self.assertIn("不要凭记忆填", p.reason)

    def test_no_solana_method_is_refused(self):
        p = self.plan("6.44", methods=[{"method": "VIDAx - Ethereum", "minimum": "1"}])
        self.assertFalse(p.ok)
        self.assertIn("没有 Solana", p.reason)

    def test_wallet_payload_carries_everything_needed(self):
        p = self.plan("6.44780479")
        payload = json.loads(dep.wallet_payload(p))
        self.assertEqual(payload["action"], "spl_transfer")
        self.assertEqual(payload["to"], ADDR[0]["address"])
        self.assertEqual(payload["decimals"], 8)
        self.assertEqual(payload["token_program"], dep.TOKEN_2022_PROGRAM)
        self.assertTrue(payload["transfer_checked_data"].startswith("0c"))

    def test_wallet_payload_refuses_a_bad_plan(self):
        p = self.plan("0.2")
        with self.assertRaises(ValueError):
            dep.wallet_payload(p)


if __name__ == "__main__":
    unittest.main()
