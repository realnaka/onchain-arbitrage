"""Hermetic tests for the EVM -> Solana CCIP dialect.

The golden vector is the real X Layer transaction
``0xde280184e269247406a42023efe50edcd7ed20ff14de5efd332c9e76d46adb73``
(2026-09-26, MRNAx). Its ``send(...)`` call sits inside an ERC-4337
``handleOps``; decoding the inner call reproduced all 11 accounts, bitmap 772,
the Solana selector, the receiver, the MRNAx token and the amount -- and this
file locks that shape without touching the network.
"""
from __future__ import annotations

import unittest

from arb_monitor.xstocks_v2 import ccip_evm as E
from arb_monitor.xstocks_v2 import ccip_solana as S

OWNER = "AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev"
MINT = E.MRNAX_SAMPLE["mint"]
CONFIG = E.MRNAX_SAMPLE["token_config"]
CUSTODY = E.MRNAX_SAMPLE["custody"]
ATA = E.MRNAX_SAMPLE["ata"]
TOKEN = "0xefd30445a4ec1f4b3e0a6f4d9bdbd215f805047f"
AMOUNT = 539900002628231336


class SelectorTest(unittest.TestCase):
    def test_selector_is_the_measured_one(self):
        # This exact 4-byte prefix appears inside the real transaction input.
        self.assertEqual(E.SEND_SELECTOR.hex(), "5d2a9df2")
        self.assertEqual(E.SEND_SIGNATURE.count(","), 4)


class ExtraArgsTest(unittest.TestCase):
    def test_round_trip_carries_the_bitmap_and_all_accounts(self):
        accounts = E.MRNAX_SAMPLE["accounts"]
        blob = E.encode_extra_args(accounts)
        self.assertEqual(len(blob), 32 * (3 + 11))
        bitmap, decoded = E.decode_extra_args(blob)
        self.assertEqual(bitmap, 772)
        self.assertEqual(tuple(S.to_b58(a) for a in decoded), accounts)

    def test_bitmap_772_marks_accounts_2_8_and_9(self):
        self.assertEqual([i for i in range(11) if 772 >> i & 1], [2, 8, 9])

    def test_wrong_account_count_is_refused(self):
        with self.assertRaises(ValueError):
            E.encode_extra_args(E.MRNAX_SAMPLE["accounts"][:10])

    def test_decode_rejects_a_wrong_offset(self):
        blob = bytearray(E.encode_extra_args(E.MRNAX_SAMPLE["accounts"]))
        blob[63] = 0x41
        with self.assertRaises(ValueError):
            E.decode_extra_args(bytes(blob))


class CalldataTest(unittest.TestCase):
    def _accounts(self):
        return E.build_accounts(source_chain="x-layer", mint=MINT,
                                token_config=CONFIG, custody=CUSTODY, owner=OWNER,
                                destination_ata=ATA)

    def test_the_golden_vector_round_trips(self):
        accounts = self._accounts()
        self.assertEqual(tuple(accounts), E.MRNAX_SAMPLE["accounts"])
        data, call = E.build_call(source_chain="x-layer", token=TOKEN, amount=AMOUNT,
                                  receiver=OWNER, accounts=accounts)
        self.assertEqual(call.dest_selector, E.SOLANA_CHAIN_SELECTOR)
        self.assertEqual(call.token, TOKEN)
        self.assertEqual(call.amount, AMOUNT)
        self.assertEqual(call.bitmap, 772)
        self.assertEqual(S.to_b58(call.receiver), OWNER)
        self.assertEqual((len(data) - 2) // 2, 644)
        self.assertEqual(E.validate_call(call, source_chain="x-layer", mint=MINT,
                                         custody=CUSTODY, owner=OWNER,
                                         amount=AMOUNT), [])

    def test_changing_the_source_chain_changes_slot_three(self):
        """Lesson 4.1: the lane account encodes the source selector."""
        base = E.build_accounts(source_chain="x-layer", mint=MINT, token_config=CONFIG,
                                custody=CUSTODY, owner=OWNER, destination_ata=ATA)
        eth = E.build_accounts(source_chain="eth", mint=MINT, token_config=CONFIG,
                               custody=CUSTODY, owner=OWNER, destination_ata=ATA)
        self.assertNotEqual(base[3], eth[3])
        self.assertEqual(eth[3], E.SOURCE_LANE_ACCOUNT["eth"])
        problems = E.validate_accounts(eth, source_chain="x-layer", mint=MINT,
                                       custody=CUSTODY, owner=OWNER,
                                       destination_ata=ATA)
        self.assertIn("lane_slot:x-layer", problems)

    def test_slot_zero_follows_the_source_chain(self):
        """Measured 2026-10-09: an eth-sourced execution carries 65KwH3…, not
        X Layer's Eqebtzd3…. The X Layer slot 0 on an eth send is refused."""
        eth = E.build_accounts(source_chain="eth", mint=MINT, token_config=CONFIG,
                               custody=CUSTODY, owner=OWNER, destination_ata=ATA)
        self.assertEqual(eth[0], E.SOURCE_SLOT0["eth"])
        self.assertNotEqual(eth[0], E.MRNAX_SAMPLE["accounts"][0])
        self.assertEqual(E.validate_accounts(eth, source_chain="eth", mint=MINT,
                                             custody=CUSTODY, owner=OWNER,
                                             destination_ata=ATA), [])
        borrowed = (E.MRNAX_SAMPLE["accounts"][0],) + tuple(eth[1:])
        self.assertIn("slot0_not_source_chain:eth",
                      E.validate_accounts(borrowed, source_chain="eth", mint=MINT,
                                          custody=CUSTODY, owner=OWNER,
                                          destination_ata=ATA))

    def test_a_source_without_a_measured_slot_zero_raises(self):
        with self.assertRaises(ValueError):
            E.build_accounts(source_chain="optimism", mint=MINT, token_config=CONFIG,
                             custody=CUSTODY, owner=OWNER, destination_ata=ATA)

    def test_the_mrna_table_for_another_mint_is_refused(self):
        """The exact shape of the 2026-09-26 loss: someone else's accounts."""
        other = "Xs8S1uUs1zvS2p7iwtsG3b6fkhpvmwz4GYU3gWAmWHZ"
        problems = E.validate_accounts(E.MRNAX_SAMPLE["accounts"],
                                       source_chain="x-layer", mint=other,
                                       custody=CUSTODY, owner=OWNER)
        self.assertIn("mint_slot", problems)
        self.assertIn("mrna_sample_leftover:4", problems)
        self.assertIn("mrna_sample_leftover:8", problems)
        self.assertIn("mrna_sample_leftover:9", problems)

    def test_a_zero_receiver_is_refused_before_encoding(self):
        with self.assertRaises(ValueError):
            E.build_calldata(dest_selector=E.SOLANA_CHAIN_SELECTOR,
                             receiver="11111111111111111111111111111111",
                             token=TOKEN, amount=1, extra_args=b"\x00" * 448)

    def test_bitmap_other_than_772_is_flagged(self):
        data, call = E.build_call(source_chain="x-layer", token=TOKEN, amount=1,
                                  receiver=OWNER, accounts=self._accounts(), bitmap=1)
        problems = E.validate_call(call, source_chain="x-layer", mint=MINT,
                                   custody=CUSTODY, owner=OWNER, amount=1)
        self.assertIn("bitmap:1", problems)

    def test_wrong_destination_selector_is_flagged(self):
        data, call = E.build_call(source_chain="x-layer", token=TOKEN, amount=1,
                                  receiver=OWNER, accounts=self._accounts(),
                                  dest_selector=E.SOURCE_SELECTORS["eth"])
        problems = E.validate_call(call, source_chain="x-layer", mint=MINT,
                                   custody=CUSTODY, owner=OWNER, amount=1)
        self.assertIn(f"dest_selector:{E.SOURCE_SELECTORS['eth']}", problems)


class FeeTest(unittest.TestCase):
    def test_value_zero_is_refused(self):
        self.assertEqual(E.fee_guard(None), (False, "fee_unknown"))
        self.assertEqual(E.fee_guard(0), (False, "fee_zero"))
        self.assertEqual(E.fee_guard(791242725185203), (True, "ok"))


if __name__ == "__main__":
    unittest.main()
