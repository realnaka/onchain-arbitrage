"""Hermetic tests for the TON chain -> Kraken cells.

The swap body has to be ston.fi v2 (opcode 0x6664de2a). A v1 body sent to a
v2 router does not fail the swap, it loses the jetton. These tests never
touch the network and never read ``config/ton.env``.
"""
from __future__ import annotations

import unittest

from arb_monitor.xstocks_v2 import legs_ton as ton


class TonRailTest(unittest.TestCase):
    def test_ton_method_matches_the_network_word(self):
        methods = [{"method": "SPYx - Solana"}, {"method": "SPYx - TON",
                                                 "minimum": "0.001"}]
        self.assertEqual(ton.ton_method(methods)["method"], "SPYx - TON")
        self.assertIsNone(ton.ton_method([{"method": "SPYx - Solana"}]))

    def test_deposit_memo_reads_the_comment_and_treats_a_missing_row_as_empty(self):
        self.assertEqual(ton.deposit_memo({"memo": "abc"}), "abc")
        self.assertEqual(ton.deposit_memo({"tag": ""}), "")
        self.assertEqual(ton.deposit_memo(None), "")

    def test_an_empty_comment_is_not_a_comment(self):
        with self.assertRaises(ValueError):
            ton.comment_cell("")


class TonSwapBodyTest(unittest.TestCase):
    def test_swap_opcode_is_v2_and_a_wallet_address_is_stable(self):
        wallet = ton.new_wallet()
        again = ton.wallet_from_mnemonic(wallet["words"])
        self.assertEqual(again["address"], wallet["address"])
        self.assertNotEqual(wallet["address"], wallet["address_bounceable"])
        body = ton.swap_body(
            ask_jetton_wallet=wallet["address_bounceable"],
            refund=wallet["address"], receiver=wallet["address"],
            min_ask_units=5, deadline=1_700_000_000)
        opcode = body.begin_parse().load_uint(32)
        self.assertEqual(opcode, ton.SWAP_OP)
        self.assertEqual(ton.SWAP_OP, 0x6664DE2A)

    def test_a_zero_swap_is_refused(self):
        wallet = ton.new_wallet()
        with self.assertRaises(ValueError):
            ton.swap_body(
                ask_jetton_wallet=wallet["address_bounceable"],
                refund=wallet["address"], receiver=wallet["address"],
                min_ask_units=0, deadline=1)

    def test_jetton_transfer_carries_the_kraken_comment(self):
        wallet = ton.new_wallet()
        body = ton.jetton_transfer_body(
            amount=10, destination=wallet["address_bounceable"],
            response=wallet["address"], forward_nanoton=10_000_000,
            comment="kraken-memo")
        parser = body.begin_parse()
        self.assertEqual(parser.load_uint(32), ton.JETTON_TRANSFER_OP)
        self.assertGreater(len(body.refs), 0)


if __name__ == "__main__":
    unittest.main()
