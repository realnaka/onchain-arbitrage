"""Hermetic tests for the TON chain -> Kraken cells and wallet identity.

No network and no mnemonic is written to disk. The assertions are the opcode
and the fact that one mnemonic is one address.
"""
from __future__ import annotations

import unittest

from arb_monitor.xstocks_v2 import legs_ton


class TonCellTest(unittest.TestCase):
    def setUp(self):
        self.wallet = legs_ton.new_wallet()
        self.addr = self.wallet["address_bounceable"]

    def test_the_swap_body_is_the_v2_opcode(self):
        cell = legs_ton.swap_body(
            ask_jetton_wallet=self.addr, refund=self.wallet["address"],
            receiver=self.wallet["address"], min_ask_units=123, deadline=99)
        self.assertEqual(cell.begin_parse().load_uint(32), legs_ton.SWAP_OP)
        with self.assertRaises(ValueError):
            legs_ton.swap_body(
                ask_jetton_wallet=self.addr, refund=self.wallet["address"],
                receiver=self.wallet["address"], min_ask_units=0, deadline=1)

    def test_a_deposit_with_a_memo_carries_the_comment_opcode(self):
        cell = legs_ton.jetton_transfer_body(
            amount=10, destination=self.addr, response=self.wallet["address"],
            forward_nanoton=legs_ton.FORWARD_NOTIFY_NANOTON, comment="8841")
        parser = cell.begin_parse()
        self.assertEqual(parser.load_uint(32), legs_ton.JETTON_TRANSFER_OP)
        bare = legs_ton.jetton_transfer_body(
            amount=10, destination=self.addr, response=self.wallet["address"],
            forward_nanoton=0)
        self.assertGreater(len(cell.refs), len(bare.refs))
        with self.assertRaises(ValueError):
            legs_ton.jetton_transfer_body(
                amount=0, destination=self.addr, response=self.wallet["address"],
                forward_nanoton=0)

    def test_one_mnemonic_is_one_address(self):
        again = legs_ton.wallet_from_mnemonic(self.wallet["words"])
        self.assertEqual(again["address"], self.wallet["address"])
        self.assertTrue(self.wallet["address"].startswith("UQ"))

    def test_the_ton_rail_is_found_by_the_network_word(self):
        found = legs_ton.ton_method([
            {"method": "SPYx", "network": "Solana"},
            {"method": "SPYx - TON", "minimum": "0.00160914", "fee": "0"},
        ])
        self.assertEqual(found["method"], "SPYx - TON")
        self.assertIsNone(legs_ton.ton_method([{"method": "SPYx - Ethereum"}]))
        self.assertEqual(legs_ton.deposit_memo({"address": "EQ", "memo": "9"}), "9")
        self.assertEqual(legs_ton.deposit_memo({"address": "EQ"}), "")


class SignedBocTest(unittest.TestCase):
    def test_seqno_zero_produces_a_boc(self):
        wallet = legs_ton.new_wallet()
        body = legs_ton.swap_body(
            ask_jetton_wallet=wallet["address_bounceable"],
            refund=wallet["address"], receiver=wallet["address"],
            min_ask_units=5, deadline=1)
        boc = legs_ton.external_boc(
            private_key=wallet["private_key"], seqno=0,
            wallet_id=wallet["wallet_id"], wallet_address=wallet["address"],
            destination=wallet["address_bounceable"], value=300_000_000,
            body=body, state_init=wallet["state_init"])
        self.assertTrue(boc.startswith(b"\xb5\xee\x9c\x72"))


if __name__ == "__main__":
    unittest.main()
