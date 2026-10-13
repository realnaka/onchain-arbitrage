"""Hermetic tests for the Solana -> X Layer CCIP dialect.

The fixtures are real. Every hex string and public key below was decoded from a
mainnet transaction on 2026-10-07 (four samples: QQQx, TSLAx twice, HOODx).
Nothing here touches the network.

The invariants this file locks:

* the Solana source account table is **27** entries, never the EVM->Solana 11;
* the custody account belongs to **this mint** (swapping TSLAx and QQQx fails);
* the receiving address in the CCIP payload is **not all zero** -- that was the
  2026-09-26 X Layer -> Solana failure.
"""
from __future__ import annotations

import unittest

from arb_monitor.xstocks_v2 import ccip_solana as C

# QQQx sample, transaction 3aijbrPd3dPc3ULqtPSGBSP7pDNC2aRVXqvVjsZ5ZkMDKBGCU...
QQQ_MINT = "Xs8S1uUs1zvS2p7iwtsG3b6fkhpvmwz4GYU3gWAmWHZ"
QQQ_PRODUCT_ID = 3581283827036199749
QQQ_RECIPIENT = "bac617baae57134be31c2c0206ab30ef9e3f71cb"
QQQ_AMOUNT = 1197538502
QQQ_SIGNER = "bpfe9tJukfJWf4M8SPVR1kzby6NCfH45zA87vbYgtG5"
QQQ_SOURCE_ATA = "GwLEK7vSbr5H169472GtDwyj48Z7X7797jqxVZRDEnqd"
QQQ_CUSTODY = "Ah9P5Lxh9e1uN1x254jSo6bSGXHd2zzeRrkjXDEkJRrw"
QQQ_TOKEN_CONFIG = "DU8KX8Vt3v5zQcYAA8JryGbzrC7DbEsuNcm3t6FR2jin"
QQQ_INSTRUCTION_DATA = (
    "6cd886bff9ea2154"                       # sha256("global:ccip_send")[:8]
    + "96129f6342bddb29"                     # X Layer selector, u64 LE
    + "20000000" + "00" * 12                 # Vec<u8> 32: bridge, zero padded
    + "9ec0e4a4c411493773e01e2abf4d42395788846b"
    + "14000000"                             # Vec<u8> 20: recipient
    + "bac617baae57134be31c2c0206ab30ef9e3f71cb"
    + "451b588eaa46b331"                     # productId u64 LE
    + "c6fc604700000000")                    # amount u64 LE = 1197538502
QQQ_TOKEN_CONFIG_BYTES = (
    "5c49ff2b6b337565451b588eaa46b331"
    "07e81ab509e1e529946e7a63887b130617aae2dd85efed1a04da560c06237624"
    "9001e40363ee8f82156c2c7e258b84246a70b1f06fff90a430447351ffb63028"
    "08011027000000000000")
QQQ_PAYLOAD = (
    "00" * 12 + "bac617baae57134be31c2c0206ab30ef9e3f71cb"
    + "31b346aa8e581b45"                     # productId, big endian
    + "00" * 24
    + "a6311d3a35a25800")                    # shares, 8 -> 18 decimals

# the exact 27 accounts of that transaction, in order
QQQ_ACCOUNTS = (
    "3VLdCUjPeBoyFbXQoQK1BpCd79jsCzdeJSnoifM2qN3B",
    QQQ_SIGNER,
    "A8JPtC8mYrZKvainsrr3cGhTcoBeUV2vd8SXun55in3u",
    "11111111111111111111111111111111",
    QQQ_SOURCE_ATA,
    QQQ_CUSTODY,
    QQQ_MINT,
    "Ccip842gzYHhvdDkSyi2YVCoAWPbYJoApMFzSxQroE9C",
    "3Yrg9E4ySAeRezgQY99NNarAmFLtixapga9MZb6y2dt3",
    "5iP5hCRwPjF7wMjTCaTfRQmcNXrvc1Tu2BKKFJ8NvJSd",
    "DEYRxGEPPCvaVJJpmgcK8QSq1yNUAKwHvuA1V9UnVdAT",
    "FeeQPGkKDeRV1MgoYfMH6L8o3KeuYjwUZrgn4LRKfjHi",
    "Jexa4tyW5xjDuucnNpTc3cz3Jsq1qY8fp4P4Dd4NQAb",
    "8muJ841RbE71zUWXpCiyR9UPYsku3sbymWS45jDiRu5m",
    "7e8EXjofujRmNxSMwo8CT8yxs1xxTexx55BN62xwWrKN",
    "8qWXPpijw2y5kYhJhUvPwyju8baEDdV5jESKrTBdpYy6",
    "RmnXLft1mSEwDgMKu2okYuHkiazxntFFcZFrrcXxYg7",
    "CpbeEvmTR4UE8CgDDL5b1nqjSz7JCD4wNJhxPLZRkSL1",
    "GbQFCDTPbhwPjeUvhu7hXEM3Wm2S6t3FCxoCmQtKxetw",
    "2AjuzTy6z2webxEUu7eZ1DkAyLagZaqH2dgzhbBYjJiG",
    "9TKiJBGMu1c6aqxaKuzV6G1otYaYM3PLZf5VoUHhjKY2",
    "So11111111111111111111111111111111111111112",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "11111111111111111111111111111111",
    "5RxSBZJZ1Qai79MPrGTp6qiqp4Bw99F2nfB2ZPjsB5P5",
    QQQ_TOKEN_CONFIG,
)

# TSLAx, from the second and fourth samples (same custody account both times)
TSLA_MINT = "XsDoVfqeBukxuZHWhdvWHBhgEHjGNst4MLodqsJHzoB"
TSLA_PRODUCT_ID = 3800906088853912450
TSLA_CUSTODY = "AKPw7dYAMrdAYa1wQicgTYo4cdFtzdLfG5LRmynwj9w9"
TSLA_TOKEN_CONFIG = "AfSC4eXCocKMLP7DqapMVm7p1m7eHWLc26VupGLn3pdH"
TSLA_SIGNER = "5iM3cFWcASthU7UMySh6JEdNpTYLxcDYsjMgpCiSCVQQ"
TSLA_SOURCE_ATA = "8T7H2HMszeyLYm5iDoYM9nY4CbhMHiBtxx6yYEBdNKbn"

# our own wallet, 2026-10-07
OUR_WALLET = "AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev"


class Base58Test(unittest.TestCase):
    def test_round_trip(self):
        for text in (OUR_WALLET, C.SOLANA_PROGRAM, QQQ_TOKEN_CONFIG):
            self.assertEqual(C.to_b58(C.pubkey(text)), text)

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            C.pubkey("0OIl")                     # not in the base58 alphabet
        with self.assertRaises(ValueError):
            C.pubkey(C.SOLANA_PROGRAM + "1")     # 33 bytes


class PdaTest(unittest.TestCase):
    def test_associated_token_accounts(self):
        """Derivation must reproduce the signer-side token accounts we read.

        The custody account is deliberately absent: it is *not* an associated
        token account of the pool authority (checked 2026-10-07), which is why
        it is read out of the token config instead.
        """
        vectors = (
            (QQQ_SIGNER, QQQ_MINT, QQQ_SOURCE_ATA),
            (TSLA_SIGNER, TSLA_MINT, TSLA_SOURCE_ATA),
            (OUR_WALLET, "XsViHjyRbiSBskjizb1gszGTmZqtAH8TuqwJ7sB5AcP",
             "67asuidxpJYv1FxGmekDPPyixCU7zxtQTNu4QjxLy2xs"),
        )
        for owner, mint, expected in vectors:
            self.assertEqual(C.to_b58(C.associated_token_address(owner, mint)), expected)

    def test_custody_is_not_the_plain_associated_account(self):
        """A derivation would have produced a plausible, wrong address."""
        derived = C.to_b58(C.associated_token_address(C.POOL_AUTHORITY, QQQ_MINT))
        self.assertNotEqual(derived, QQQ_CUSTODY)
        self.assertEqual(derived, "FMQUsCaaq7vZ9BPipEyRqQcoLBp7q4giiV8PMsBETYBX")

    def test_derived_addresses_are_off_curve(self):
        address, bump = C.find_program_address([b"seed"], C.SOLANA_PROGRAM)
        self.assertEqual(len(address), 32)
        self.assertFalse(C.is_on_curve(address))
        self.assertLessEqual(bump, 255)

    def test_our_wallet_is_not_the_sample_signer(self):
        mine = C.to_b58(C.associated_token_address(OUR_WALLET, QQQ_MINT))
        self.assertNotEqual(mine, QQQ_SOURCE_ATA)
        self.assertEqual(mine, "DKCCSWLcPCoRrE5SWYgybXjffFWGC2sxDBYxwCDNyvRa")


class ArgsTest(unittest.TestCase):
    def test_decode_the_recorded_instruction(self):
        args = C.decode_args(bytes.fromhex(QQQ_INSTRUCTION_DATA))
        self.assertEqual(args.dest_chain_selector, C.XLAYER_CHAIN_SELECTOR)
        self.assertEqual(args.receiver, C.canonical_xlayer_receiver())
        self.assertEqual(C.bytes32_to_evm(args.receiver), C.XSTOCKS_BRIDGE_EVM)
        self.assertEqual(args.recipient.hex(), QQQ_RECIPIENT)
        self.assertEqual(args.product_id, QQQ_PRODUCT_ID)
        self.assertEqual(args.amount, QQQ_AMOUNT)
        self.assertEqual(len(QQQ_INSTRUCTION_DATA) // 2, 92)

    def test_encode_round_trips_byte_for_byte(self):
        args = C.decode_args(bytes.fromhex(QQQ_INSTRUCTION_DATA))
        self.assertEqual(args.encode().hex(), QQQ_INSTRUCTION_DATA)
        self.assertEqual(
            C.instruction_data(recipient=QQQ_RECIPIENT, product_id=QQQ_PRODUCT_ID,
                               amount=QQQ_AMOUNT).hex(),
            QQQ_INSTRUCTION_DATA)

    def test_decode_rejects_wrong_shapes(self):
        good = bytes.fromhex(QQQ_INSTRUCTION_DATA)
        with self.assertRaises(ValueError):
            C.decode_args(b"\x00" * 8 + good[8:])
        with self.assertRaises(ValueError):
            C.decode_args(good + b"\x00")
        with self.assertRaises(ValueError):
            C.decode_args(good[:-1])                    # truncated
        long_receiver = bytearray(good)
        long_receiver[16:20] = (33).to_bytes(4, "little")   # receiver claims 33 bytes
        with self.assertRaises(ValueError):
            C.decode_args(bytes(long_receiver))
        with self.assertRaises(ValueError):
            C.decode_args(bytes.fromhex(QQQ_INSTRUCTION_DATA)[:91])

    def test_validate_flags_the_two_fatal_zeros(self):
        good = C.decode_args(bytes.fromhex(QQQ_INSTRUCTION_DATA))
        self.assertEqual(C.validate_args(good, product_id=QQQ_PRODUCT_ID,
                                         amount=QQQ_AMOUNT), [])
        zero_receiver = C.CcipSendArgs(C.XLAYER_CHAIN_SELECTOR, b"\x00" * 32,
                                       good.recipient, QQQ_PRODUCT_ID, QQQ_AMOUNT)
        self.assertIn("receiver_all_zero", C.validate_args(
            zero_receiver, product_id=QQQ_PRODUCT_ID, amount=QQQ_AMOUNT))
        zero_recipient = C.CcipSendArgs(C.XLAYER_CHAIN_SELECTOR, good.receiver,
                                        b"\x00" * 20, QQQ_PRODUCT_ID, QQQ_AMOUNT)
        self.assertIn("recipient_all_zero", C.validate_args(
            zero_recipient, product_id=QQQ_PRODUCT_ID, amount=QQQ_AMOUNT))

    def test_validate_flags_a_wrong_amount(self):
        good = C.decode_args(bytes.fromhex(QQQ_INSTRUCTION_DATA))
        problems = C.validate_args(good, product_id=QQQ_PRODUCT_ID,
                                   amount=QQQ_AMOUNT + 1)
        self.assertIn(f"amount:{QQQ_AMOUNT}", problems)

    def test_every_other_selector_is_rejected(self):
        """The other xStocks lanes must not be reachable by accident."""
        for selector in (C.SOLANA_CHAIN_SELECTOR, 5009297550715157269,
                         11344663589394136015, 4949039107694359620):
            problems = C.validate_args(
                C.CcipSendArgs(selector, C.canonical_xlayer_receiver(),
                               bytes.fromhex(QQQ_RECIPIENT), QQQ_PRODUCT_ID,
                               QQQ_AMOUNT),
                product_id=QQQ_PRODUCT_ID, amount=QQQ_AMOUNT)
            self.assertIn(f"dest_selector:{selector}", problems)


class PayloadTest(unittest.TestCase):
    def test_payload_matches_the_recorded_ccip_message(self):
        payload = C.message_payload(bytes.fromhex(QQQ_RECIPIENT), QQQ_PRODUCT_ID,
                                    QQQ_AMOUNT)
        self.assertEqual(payload.hex(), QQQ_PAYLOAD)
        self.assertEqual(len(payload), 72)
        receiver, product_id, shares = C.parse_message_payload(payload)
        self.assertEqual(C.bytes32_to_evm(receiver), "0x" + QQQ_RECIPIENT)
        self.assertEqual(product_id, QQQ_PRODUCT_ID)
        self.assertEqual(shares, QQQ_AMOUNT * 10 ** 10)

    def test_payload_receiver_is_never_zero_for_a_real_recipient(self):
        payload = C.message_payload(bytes.fromhex(QQQ_RECIPIENT), QQQ_PRODUCT_ID,
                                    QQQ_AMOUNT)
        self.assertNotEqual(payload[:32], b"\x00" * 32)

    def test_payload_length_is_enforced(self):
        with self.assertRaises(ValueError):
            C.parse_message_payload(b"\x00" * 71)


class AccountTest(unittest.TestCase):
    def test_built_table_equals_the_recorded_transaction(self):
        accounts = C.build_accounts(signer=QQQ_SIGNER, mint=QQQ_MINT,
                                    token_config=QQQ_TOKEN_CONFIG,
                                    custody=QQQ_CUSTODY)
        self.assertEqual(len(accounts), 27)
        self.assertEqual(tuple(row.pubkey for row in accounts), QQQ_ACCOUNTS)
        self.assertEqual(len(accounts), C.ACCOUNT_COUNT)
        self.assertEqual(C.validate_accounts(accounts, signer=QQQ_SIGNER,
                                             mint=QQQ_MINT,
                                             token_config=QQQ_TOKEN_CONFIG,
                                             custody=QQQ_CUSTODY), [])

    def test_the_svm_destination_table_is_eleven_not_twenty_seven(self):
        """Solana sources must never reuse the EVM->Solana 11 account shape."""
        self.assertNotEqual(C.ACCOUNT_COUNT, 11)
        accounts = C.build_accounts(signer=QQQ_SIGNER, mint=QQQ_MINT,
                                    token_config=QQQ_TOKEN_CONFIG, custody=QQQ_CUSTODY)
        self.assertEqual(len(accounts), 27)

    def test_custody_is_read_from_the_token_config(self):
        raw = bytes.fromhex(QQQ_TOKEN_CONFIG_BYTES)
        self.assertEqual(len(raw), C.TOKEN_CONFIG_LEN)
        self.assertEqual(C.to_b58(C.token_config_custody(
            raw, mint=QQQ_MINT, product_id=QQQ_PRODUCT_ID)), QQQ_CUSTODY)
        self.assertEqual(C.token_config_product_id(raw), QQQ_PRODUCT_ID)

    def test_token_config_from_another_mint_is_rejected(self):
        raw = bytes.fromhex(QQQ_TOKEN_CONFIG_BYTES)
        with self.assertRaises(ValueError):
            C.token_config_custody(raw, mint=TSLA_MINT)
        with self.assertRaises(ValueError):
            C.token_config_custody(raw, product_id=TSLA_PRODUCT_ID)
        with self.assertRaises(ValueError):
            C.token_config_custody(raw[:-1])
        with self.assertRaises(ValueError):
            C.token_config_custody(b"\x00" * 8 + raw[8:])

    def test_mixing_two_tokens_fails_validation(self):
        """The whole point: a TSLAx mint with QQQx's custody account is a bug."""
        accounts = C.build_accounts(signer=QQQ_SIGNER, mint=TSLA_MINT,
                                    token_config=TSLA_TOKEN_CONFIG,
                                    custody=QQQ_CUSTODY)
        problems = C.validate_accounts(accounts, signer=QQQ_SIGNER, mint=TSLA_MINT,
                                       token_config=TSLA_TOKEN_CONFIG,
                                       custody=TSLA_CUSTODY)
        self.assertIn("custody_slot", problems)
        self.assertEqual(accounts[6].pubkey, TSLA_MINT)

    def test_a_short_table_is_a_count_failure(self):
        accounts = C.build_accounts(signer=QQQ_SIGNER, mint=QQQ_MINT,
                                    token_config=QQQ_TOKEN_CONFIG, custody=QQQ_CUSTODY)
        problems = C.validate_accounts(accounts[:26], signer=QQQ_SIGNER, mint=QQQ_MINT,
                                       token_config=QQQ_TOKEN_CONFIG, custody=QQQ_CUSTODY)
        self.assertEqual(problems, ["account_count:26"])

    def test_writability_flags_match_the_measured_transaction(self):
        accounts = C.build_accounts(signer=QQQ_SIGNER, mint=QQQ_MINT,
                                    token_config=QQQ_TOKEN_CONFIG, custody=QQQ_CUSTODY)
        writable = {index for index, row in enumerate(accounts) if row.is_writable}
        signers = {index for index, row in enumerate(accounts) if row.is_signer}
        self.assertEqual(writable, {1, 2, 4, 5, 9, 10, 19, 20})
        self.assertEqual(signers, {1})

    def test_build_refuses_a_custody_account_that_is_the_signer(self):
        with self.assertRaises(ValueError):
            C.build_accounts(signer=QQQ_SIGNER, mint=QQQ_MINT,
                             token_config=QQQ_TOKEN_CONFIG, custody=QQQ_SIGNER)

    def test_build_requires_the_custody_account_when_there_is_no_config_body(self):
        with self.assertRaises(ValueError):
            C.build_accounts(signer=QQQ_SIGNER, mint=QQQ_MINT,
                             token_config=QQQ_TOKEN_CONFIG)


class EvmAddressTest(unittest.TestCase):
    def test_only_a_real_address_passes(self):
        self.assertEqual(C.bytes32_to_evm(C.evm_to_bytes32(C.XSTOCKS_BRIDGE_EVM)),
                         C.XSTOCKS_BRIDGE_EVM)
        for bad in ("0x", "0xdead", "0x" + "z" * 40, C.XSTOCKS_BRIDGE_EVM + "00"):
            with self.assertRaises(ValueError):
                C.evm_to_bytes32(bad)


class TransactionTest(unittest.TestCase):
    def _transaction(self):
        accounts = C.build_accounts(signer=OUR_WALLET, mint=QQQ_MINT,
                                    token_config=QQQ_TOKEN_CONFIG, custody=QQQ_CUSTODY)
        return C.build_legacy_transaction(
            payer=OUR_WALLET, accounts=accounts, program_id=C.SOLANA_PROGRAM,
            data=C.instruction_data(recipient=QQQ_RECIPIENT,
                                    product_id=QQQ_PRODUCT_ID, amount=QQQ_AMOUNT),
            recent_blockhash=b"\x11" * 32)

    def _ccip_instruction(self, parsed):
        return [item for item in parsed["instructions"]
                if item["program_id"] == C.SOLANA_PROGRAM][0]

    def test_round_trip(self):
        parsed = C.parse_legacy_transaction(self._transaction())
        self.assertEqual(parsed["signers"], 1)
        self.assertEqual(parsed["keys"][0], OUR_WALLET)
        self.assertEqual(self._ccip_instruction(parsed)["data"].hex(), QQQ_INSTRUCTION_DATA)

    def test_the_caller_order_is_preserved_in_the_instruction(self):
        """Account order inside the instruction is the program's, not key order."""
        parsed = C.parse_legacy_transaction(self._transaction())
        ccip = self._ccip_instruction(parsed)
        self.assertEqual(len(ccip["accounts"]), 27)
        self.assertEqual(ccip["accounts"][6], QQQ_MINT)
        self.assertEqual(ccip["accounts"][26], QQQ_TOKEN_CONFIG)

    def test_compute_budget_comes_first(self):
        """200 000 CU is not enough; the measured transactions raise it."""
        parsed = C.parse_legacy_transaction(self._transaction())
        first = parsed["instructions"][0]
        self.assertEqual(first["program_id"], C.COMPUTE_BUDGET_PROGRAM)
        self.assertEqual(len(first["accounts"]), 0)
        self.assertEqual(first["data"], b"\x02" + (400_000).to_bytes(4, "little"))
        self.assertEqual(C.DEFAULT_COMPUTE_UNIT_LIMIT, 400_000)

    def test_compute_budget_can_be_turned_off(self):
        accounts = C.build_accounts(signer=OUR_WALLET, mint=QQQ_MINT,
                                    token_config=QQQ_TOKEN_CONFIG, custody=QQQ_CUSTODY)
        raw = C.build_legacy_transaction(
            payer=OUR_WALLET, accounts=accounts, program_id=C.SOLANA_PROGRAM,
            data=b"\x00", recent_blockhash=b"\x11" * 32, compute_unit_limit=None)
        parsed = C.parse_legacy_transaction(raw)
        self.assertEqual(len(parsed["instructions"]), 1)
        self.assertNotIn(C.COMPUTE_BUDGET_PROGRAM, parsed["keys"])

    def test_a_payer_that_is_not_a_signer_is_refused(self):
        accounts = C.build_accounts(signer=QQQ_SIGNER, mint=QQQ_MINT,
                                    token_config=QQQ_TOKEN_CONFIG, custody=QQQ_CUSTODY)
        with self.assertRaises(ValueError):
            C.build_legacy_transaction(payer=OUR_WALLET, accounts=accounts,
                                       program_id=C.SOLANA_PROGRAM,
                                       data=b"\x00", recent_blockhash=b"\x11" * 32)

    def test_shortvec(self):
        self.assertEqual(C.shortvec(0), b"\x00")
        self.assertEqual(C.shortvec(127), b"\x7f")
        self.assertEqual(C.shortvec(128), b"\x80\x01")
        self.assertEqual(C.shortvec(300), b"\xac\x02")


class DestSlotsTest(unittest.TestCase):
    #: Slots 9, 10, 13, 25 of a successful Solana -> Ethereum send, read
    #: 2026-10-09 (``4do2aKPnCjZBVumqds9q…``).
    ETH_SAMPLE = {9: "CowGG7G1FsfedN4jx66Gw7co1xEsiKQfE1f8BXSnSK9w",
                  10: "5Di6up51tyv9xXuVurRgQpRWytntikVEua3YaxjHFH86",
                  13: "JCY1GFP2ayVmEsQxeRQXGZvk3nR9TovFbXF8rzcfgjHA",
                  25: "6VU9T42q3bGUTmoZ8LA5rxZ2Tdom86uLekj5c2N4w8mp"}

    def test_x_layer_derivation_is_the_measured_table(self):
        table = {row[0]: row[2] for row in C.LANE_ACCOUNTS}
        for index, key in C.dest_slots("x-layer").items():
            self.assertEqual(key, table[index])

    def test_eth_slots_match_the_mainnet_send(self):
        self.assertEqual(C.dest_slots("eth"), self.ETH_SAMPLE)

    def test_an_eth_table_is_not_an_x_layer_table(self):
        accounts = C.build_accounts(signer=OUR_WALLET, mint=TSLA_MINT,
                                    token_config=TSLA_TOKEN_CONFIG,
                                    custody=TSLA_CUSTODY, dest_chain="eth")
        for index, key in self.ETH_SAMPLE.items():
            self.assertEqual(accounts[index].pubkey, key)
        self.assertEqual(C.validate_accounts(accounts, signer=OUR_WALLET, mint=TSLA_MINT,
                                             token_config=TSLA_TOKEN_CONFIG,
                                             custody=TSLA_CUSTODY, dest_chain="eth"), [])
        wrong = C.validate_accounts(accounts, signer=OUR_WALLET, mint=TSLA_MINT,
                                    token_config=TSLA_TOKEN_CONFIG,
                                    custody=TSLA_CUSTODY, dest_chain="x-layer")
        self.assertIn("dest_slot:25", wrong)

    def test_a_chain_without_a_lane_raises(self):
        with self.assertRaises(ValueError):
            C.dest_slots("monad")
        with self.assertRaises(ValueError):
            C.build_accounts(signer=OUR_WALLET, mint=TSLA_MINT,
                             token_config=TSLA_TOKEN_CONFIG, custody=TSLA_CUSTODY,
                             dest_chain="mantle")


if __name__ == "__main__":
    unittest.main()
