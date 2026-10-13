from __future__ import annotations

from decimal import Decimal
import unittest

from arb_monitor.execution.onchain_wallet import (AllowanceError, OnchainWalletLeg,
                                                 decode_approve_spender,
                                                 minimal_units, wallet_token_delta)

USDG = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"
WALLET = "0xabababababababababababababababababababab"
SPENDER = "0x8b773d83bc66be128c60e07e17c8901f7a64f000"


def approve_calldata(spender: str, amount: int) -> str:
    return "0x095ea7b3" + "0" * 24 + spender.lower()[2:] + f"{amount:064x}"


class DecodeTests(unittest.TestCase):
    def test_spender_is_decoded_from_the_calldata_itself(self):
        self.assertEqual(decode_approve_spender(approve_calldata(SPENDER, 4_400_000)),
                         SPENDER)

    def test_anything_that_is_not_an_approve_is_refused(self):
        with self.assertRaises(AllowanceError):
            decode_approve_spender("0xdeadbeef" + "0" * 130)
        with self.assertRaises(AllowanceError):
            decode_approve_spender("0x095ea7b3" + "0" * 10)

    def test_minimal_units_refuses_a_fractional_unit(self):
        self.assertEqual(minimal_units(Decimal("4.4"), 6), 4_400_000)
        with self.assertRaises(AllowanceError):
            minimal_units(Decimal("0.0000001"), 6)


class FakeLeg(OnchainWalletLeg):
    def __init__(self, spendable: int, spender: str = SPENDER):
        self.chain_id = "196"
        self.address = WALLET
        self.spendable = spendable
        self.spender = spender
        self.calls: list[tuple] = []
        self.receipt_waiter = self._receipt
        self.rpc = None
        self.after_receipt_spendable = None
        self.broadcast = True

    def _receipt(self, rpc, tx_hash, **kwargs):
        from arb_monitor.execution.onchain_wallet import ChainReceipt
        if self.after_receipt_spendable is not None:
            self.spendable = self.after_receipt_spendable
        return ChainReceipt(tx_hash, "filled", block_number=1, gas_used=1,
                            effective_gas_price_wei=0, l1_fee_wei=0)


def runner_for(leg: FakeLeg):
    def runner(argv):
        leg.calls.append(tuple(argv))
        if "check-approvals" in argv:
            return {"ok": True, "data": [{"spender": leg.spender, "tokens": [
                {"spendable": str(leg.spendable), "tokenContractAddress": USDG}]}]}
        if "approve" in argv:
            amount = int(argv[argv.index("--amount") + 1])
            # Same flat shape the CLI really returns.
            return {"ok": True, "data": [{
                "data": approve_calldata(leg.spender, amount),
                "dexContractAddress": leg.spender, "gasLimit": "70000"}]}
        if "tx-scan" in argv:
            return {"ok": True, "data": {"action": "", "warnings": [],
                                         "riskItemDetail": [],
                                         "simulator": {"revertReason": ""}}}
        if "simulate" in argv:
            return {"ok": True, "data": {"failReason": "", "risks": []}}
        if "contract-call" in argv:
            if not leg.broadcast:
                return {"ok": True, "data": {}}
            return {"ok": True, "data": {"orderId": "196000", "txHash": "0x" + "ab" * 32}}
        raise AssertionError(f"unexpected call: {argv}")
    return runner


class PreflightLeg(OnchainWalletLeg):
    """A leg whose allowance/balance reads can be set to anything, including
    ``unreadable`` — the two answers the entry pre-flight has to fail closed on.
    """

    def __init__(self, *, spendable: int | None = 11_000_000,
                 balance: Decimal | None = Decimal("11"),
                 allowance_fails: bool = False, balance_fails: bool = False):
        self.chain_id = "196"
        self.address = WALLET
        self.rpc = None
        self.receipt_waiter = None
        self.runner = None
        self.spendable = spendable
        self.balance = balance
        self.allowance_fails = allowance_fails
        self.balance_fails = balance_fails
        self.balance_reads = 0

    def allowance(self, runner, token_address):
        if self.allowance_fails:
            raise RuntimeError("okx_transport:URLError")
        return int(self.spendable), SPENDER

    def balance_of(self, token_address, runner):
        self.balance_reads += 1
        if self.balance_fails:
            raise RuntimeError("okx_transport:URLError")
        return self.balance


class SpotPreflightTests(unittest.TestCase):
    """The spot leg's static conditions, read *before* the perpetual order.

    2026-09-18 (SHEIN): the maker leg filled 2 contracts, the swap reverted on
    ERC-20 allowance — 1.006008 USDG spendable against 10.339067 needed — and
    the unwind cost 0.0264 USDT. Neither number can change between the read and
    the fill, so both belong in front of the order.
    """

    def preflight(self, leg, amount="10.339067", balance_quote=None):
        return leg.spot_preflight(token_in=USDG, amount=Decimal(amount),
                                  decimals=6, balance_quote=balance_quote,
                                  runner=object())

    def test_a_spent_allowance_blocks_before_the_order(self):
        result = self.preflight(PreflightLeg(spendable=1_006_008))
        self.assertFalse(result["ok"])
        self.assertTrue(result["reason"].startswith("allowance_short"))
        self.assertEqual(result["spendable_minimal_units"], "1006008")
        self.assertEqual(result["required_minimal_units"], "10339067")

    def test_an_allowance_that_just_covers_the_spend_is_enough(self):
        result = self.preflight(PreflightLeg(spendable=10_339_067))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "")

    def test_a_balance_short_of_the_spend_blocks_too(self):
        result = self.preflight(PreflightLeg(balance=Decimal("9.5")))
        self.assertFalse(result["ok"])
        self.assertTrue(result["reason"].startswith("quote_balance_short"))

    def test_the_callers_balance_is_trusted_instead_of_a_second_read(self):
        leg = PreflightLeg(balance=Decimal("0.01"))
        result = self.preflight(leg, balance_quote=Decimal("10.9"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(leg.balance_reads, 0)

    def test_an_unreadable_allowance_is_not_evidence_of_a_good_one(self):
        result = self.preflight(PreflightLeg(allowance_fails=True))
        self.assertFalse(result["ok"])
        self.assertTrue(result["reason"].startswith("allowance_unreadable"))

    def test_an_unreadable_balance_is_not_evidence_of_a_good_one(self):
        result = self.preflight(PreflightLeg(balance_fails=True))
        self.assertFalse(result["ok"])
        self.assertTrue(result["reason"].startswith("balance_unreadable"))

    def test_a_leg_with_no_runner_cannot_answer_and_blocks(self):
        leg = PreflightLeg()
        result = leg.spot_preflight(token_in=USDG, amount=Decimal("1"),
                                    decimals=6)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "preflight_no_runner")


class EnsureAllowanceTests(unittest.TestCase):
    def run_leg(self, leg: FakeLeg, amount="4", cap="110"):
        return leg.ensure_allowance(runner_for(leg), token_address=USDG,
                                    token_symbol="USDG", decimals=6,
                                    amount=Decimal(amount), cap=Decimal(cap),
                                    sleeper=lambda _s: None)

    def test_an_existing_allowance_is_reused_without_a_new_transaction(self):
        leg = FakeLeg(spendable=9_000_000)
        outcome = self.run_leg(leg)
        self.assertEqual(outcome.action, "reused")
        self.assertFalse([c for c in leg.calls if "tx-scan" in c])

    def test_a_missing_allowance_is_approved_bounded_and_read_back(self):
        leg = FakeLeg(spendable=0)
        leg.after_receipt_spendable = 4_400_000
        outcome = self.run_leg(leg)
        self.assertEqual(outcome.action, "approved")
        # 4 USDG + 10% headroom, never unlimited, never above the policy cap.
        self.assertEqual(outcome.approved_amount, 4_400_000)
        self.assertEqual(outcome.spendable_after, 4_400_000)
        order = [c[1] for c in leg.calls if len(c) > 1 and c[0] == "swap"]
        self.assertEqual(order[0], "check-approvals")
        self.assertIn("tx-scan", [c for call in leg.calls for c in call])

    def test_the_bounded_cap_wins_over_a_large_request(self):
        leg = FakeLeg(spendable=0)
        leg.after_receipt_spendable = 4_400_000
        outcome = self.run_leg(leg, amount="4", cap="4.4")
        self.assertEqual(outcome.approved_amount, 4_400_000)

    def test_an_allowance_left_above_the_cap_is_refused(self):
        leg = FakeLeg(spendable=0)
        leg.after_receipt_spendable = 900_000_000
        with self.assertRaises(AllowanceError):
            self.run_leg(leg)

    def test_a_changed_spender_is_refused_before_anything_is_signed(self):
        leg = FakeLeg(spendable=0, spender=SPENDER)

        def runner(argv):
            leg.calls.append(tuple(argv))
            if "check-approvals" in argv:
                return {"ok": True, "data": [{"spender": SPENDER, "tokens": [
                    {"spendable": "0", "tokenContractAddress": USDG}]}]}
            if "approve" in argv:
                # The API answers with a different spender than it will use.
                return {"ok": True, "data": [{
                    "data": approve_calldata("0x" + "11" * 20, 4_400_000),
                    "dexContractAddress": "0x" + "11" * 20}]}
            raise AssertionError("nothing may be scanned or signed here")

    def test_a_payload_that_contradicts_its_own_calldata_is_refused(self):
        leg = FakeLeg(spendable=0)

        def runner(argv):
            leg.calls.append(tuple(argv))
            if "check-approvals" in argv:
                return {"ok": True, "data": [{"spender": SPENDER, "tokens": [
                    {"spendable": "0", "tokenContractAddress": USDG}]}]}
            if "approve" in argv:
                return {"ok": True, "data": [{
                    "data": approve_calldata(SPENDER, 4_400_000),
                    "dexContractAddress": "0x" + "33" * 20}]}
            raise AssertionError("nothing may be scanned or signed here")

        with self.assertRaises(AllowanceError):
            leg.ensure_allowance(runner, token_address=USDG, token_symbol="USDG",
                                 decimals=6, amount=Decimal("4"),
                                 cap=Decimal("4.4"), sleeper=lambda _s: None)

        with self.assertRaises(AllowanceError):
            leg.ensure_allowance(runner, token_address=USDG, token_symbol="USDG",
                                 decimals=6, amount=Decimal("4"),
                                 cap=Decimal("4.4"), sleeper=lambda _s: None)

    def test_an_unknown_broadcast_result_is_an_error_not_a_success(self):
        leg = FakeLeg(spendable=0)
        leg.broadcast = False
        with self.assertRaises(AllowanceError):
            self.run_leg(leg)


class WalletDeltaTests(unittest.TestCase):
    def test_net_token_change_is_decoded_from_transfer_logs(self):
        transfer = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        receipt = {"logs": [
            {"address": USDG, "blockNumber": "0x1", "topics": [
                transfer, "0x" + "0" * 24 + "11" * 20, "0x" + "0" * 24 + WALLET[2:]],
             "data": hex(2_000_000)},
        ]}
        self.assertEqual(wallet_token_delta(receipt, WALLET, USDG), 2_000_000)
        self.assertEqual(wallet_token_delta(receipt, "0x" + "22" * 20, USDG), 0)


if __name__ == "__main__":
    unittest.main()
