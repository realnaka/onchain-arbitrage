"""A sell must be decoded from the token that left the wallet.

Measured 2026-09-16: the first 3-share close reported the spot leg as
"1.0232333E-11 shares at 1E+12 USD" because the receipt decoder was watching
the *payment* token for both directions. The trade itself was fine; the record
of it was worthless, which is worse than useless for a cost-calibration sample.
"""
from __future__ import annotations

from decimal import Decimal
import unittest

from arb_monitor.execution.onchain_wallet import (ChainReceipt, OnchainWalletLeg,
                                                 OnchainFillError)

USDG = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"
WX = "0x076cf393e701839fc7a5832d2c68aafa235682ae"
WALLET = "0xabababababababababababababababababababab"


class FakeQuote:
    def __init__(self, amount_in, amount_out):
        self.amount_in = Decimal(str(amount_in))
        self.amount_out = Decimal(str(amount_out))
        self.amount_out_minimum = self.amount_out * Decimal("0.99")
        self.router = "0xrouter"
        self.price_impact_pct = Decimal("-0.01")
        self.trade_fee_usd = Decimal("0.0003")
        self.gas_limit = 400000


class FakeWallet:
    def __init__(self, quote, tx_hash="0x" + "ab" * 32):
        self.quote = quote
        self.tx_hash = tx_hash

    def quote_exact_in(self, token_in, token_out, amount_in):
        return self.quote

    def build_swap(self, quote):
        class Plan:
            pass
        plan = Plan()
        plan.quote = quote
        plan.tx_to = "0xrouter"
        plan.calldata = "0xabcd"
        plan.gas_limit = 400000
        plan.value = 0
        plan.raw = {}
        return plan

    def simulate(self, plan):
        return {"intention": "Swap"}

    def execute(self, plan, confirmed=False):
        class Result:
            pass
        result = Result()
        result.tx_hash = self.tx_hash
        result.order_id = "196000"
        result.raw = {}
        return result


def leg(quote, receipt) -> OnchainWalletLeg:
    instance = OnchainWalletLeg(wallet=FakeWallet(quote), address=WALLET)
    instance.rpc = None
    instance.receipt_waiter = lambda *a, **k: receipt
    return instance


class SellDecodingTests(unittest.TestCase):
    def test_a_sell_reads_the_tokens_that_left_not_the_cash_that_arrived(self):
        tokens_out = int(Decimal("3.000751023551683119") * Decimal(10 ** 18))
        cash_in = int(Decimal("10.232333") * Decimal(10 ** 6))
        receipt = ChainReceipt("0x" + "ab" * 32, "filled",
                               token_delta_raw=-tokens_out, quote_delta_raw=cash_in)
        instance = leg(FakeQuote(Decimal("3.000751023551683119"), Decimal("10.28")), receipt)
        outcome = instance.swap_exact_in(
            token_in=WX, token_out=USDG, amount_in=Decimal("3.000751023551683119"),
            token_decimals=18, quote_token=USDG, quote_decimals=6, leg="sell")
        self.assertEqual(outcome.token_quantity, Decimal("3.000751023551683119"))
        self.assertEqual(outcome.quote_amount, Decimal("10.232333"))
        self.assertEqual(outcome.price_usd,
                         Decimal("10.232333") / Decimal("3.000751023551683119"))

    def test_a_buy_still_reads_the_tokens_that_arrived(self):
        token_in = int(Decimal("10.244010") * Decimal(10 ** 6))
        token_out = int(Decimal("3.000751023551683119") * Decimal(10 ** 18))
        receipt = ChainReceipt("0x" + "ab" * 32, "filled",
                               token_delta_raw=token_out, quote_delta_raw=-token_in)
        instance = leg(FakeQuote(Decimal("10.244010"), Decimal("3.0007")), receipt)
        outcome = instance.swap_exact_in(
            token_in=USDG, token_out=WX, amount_in=Decimal("10.244010"),
            token_decimals=18, quote_token=USDG, quote_decimals=6, leg="buy")
        self.assertEqual(outcome.token_quantity, Decimal("3.000751023551683119"))
        self.assertEqual(outcome.quote_amount, Decimal("10.244010"))

    def test_a_sell_that_moved_no_tokens_is_an_error(self):
        receipt = ChainReceipt("0x" + "ab" * 32, "filled",
                               token_delta_raw=0, quote_delta_raw=0)
        instance = leg(FakeQuote(Decimal("1"), Decimal("3.4")), receipt)
        with self.assertRaisesRegex(OnchainFillError, "moved_no_tokens"):
            instance.swap_exact_in(
                token_in=WX, token_out=USDG, amount_in=Decimal("1"),
                token_decimals=18, quote_token=USDG, quote_decimals=6, leg="sell")


if __name__ == "__main__":
    unittest.main()
