"""A94: decide "can this wallet sign?" before signing, not by the wallet's error.

Measured 2026-10-09 on Ethereum: the wallet refused a swap with ``10004`` and
``balance 246927887542942, tx cost 539589813939648`` -- 0.000247 ETH against
0.00054 ETH -- while ``eth_gasPrice`` read 0.10 gwei. That refusal was recorded
as ``unknown`` and halted the live runner for two hours.

Two properties this file pins:

* the reserve follows the wallet (its own fee cap), not just the network price,
  so the check does not pass a wallet the signer is about to refuse;
* an unreadable balance or gas price is a refusal with its own name, never a
  quiet "fine" and never an ``unknown`` that stops the chain.
"""
from __future__ import annotations

import unittest
from decimal import Decimal as D

from arb_monitor.xstocks_v2 import native_reserve

#: The wallet's own numbers from the 2026-10-09 refusal.
THIN_BALANCE = D("0.000246927887542942")
NETWORK_PRICE_WEI = 100_000_000          # 0.10 gwei, as measured


class NativeReserveTest(unittest.TestCase):

    def test_the_measured_refusal_is_refused_here_too(self):
        short = native_reserve.shortfall(balance=THIN_BALANCE, gas_limit=300_000,
                                         gas_price_wei=NETWORK_PRICE_WEI)
        self.assertTrue(short.startswith("gas_short:"), short)
        # The number is the wallet's: 300k gas at the measured 1.8 gwei floor.
        self.assertIn("0.00054", short)

    def test_a_funded_wallet_passes(self):
        self.assertEqual(
            native_reserve.shortfall(balance=D("0.001"), gas_limit=300_000,
                                     gas_price_wei=NETWORK_PRICE_WEI), "")

    def test_the_floor_is_what_the_wallet_uses_not_the_network_price(self):
        """0.10 gwei x 300k would be 0.00003 ETH -- and would have passed."""
        naive = D("0.000246927887542942")
        self.assertGreater(naive, D(300_000) * D(NETWORK_PRICE_WEI) / D(10 ** 18))
        self.assertEqual(native_reserve.fee_cap_wei(NETWORK_PRICE_WEI),
                         native_reserve.FEE_CAP_FLOOR_WEI)

    def test_a_congested_chain_raises_the_reserve_above_the_floor(self):
        cheap = native_reserve.need_native(gas_limit=300_000,
                                           gas_price_wei=NETWORK_PRICE_WEI)
        dear = native_reserve.need_native(gas_limit=300_000,
                                          gas_price_wei=100_000_000_000)
        self.assertLess(cheap, dear)
        self.assertEqual(dear, D("0.06"))

    def test_a_missing_gas_limit_uses_the_measured_fallback(self):
        self.assertEqual(
            native_reserve.need_native(gas_limit=None,
                                       gas_price_wei=NETWORK_PRICE_WEI),
            D(native_reserve.FALLBACK_GAS_UNITS) * D(native_reserve.FEE_CAP_FLOOR_WEI)
            / D(10 ** 18))

    def test_an_unreadable_balance_is_a_named_refusal(self):
        self.assertEqual(native_reserve.shortfall(balance=None, gas_limit=1,
                                                  gas_price_wei=1),
                         "native_balance_unreadable")
        self.assertEqual(native_reserve.shortfall(balance="not a number",
                                                  gas_limit=1, gas_price_wei=1),
                         "native_balance_unreadable")

    def test_an_unreadable_gas_price_is_a_refusal_not_a_free_action(self):
        self.assertEqual(native_reserve.shortfall(balance=D("1"), gas_limit=1,
                                                  gas_price_wei=None),
                         "gas_price_unreadable")
        self.assertEqual(native_reserve.shortfall(balance=D("1"), gas_limit=1,
                                                  gas_price_wei="abc"),
                         "gas_price_unreadable")

    def test_a_zero_gas_price_is_unreadable_not_free(self):
        self.assertEqual(native_reserve.shortfall(balance=D("1"), gas_limit=1,
                                                  gas_price_wei=0),
                         "gas_price_unreadable")

    def test_a_string_balance_from_json_still_works(self):
        self.assertTrue(native_reserve.shortfall(balance="0.0001", gas_limit=300_000,
                                                 gas_price_wei="100000000")
                        .startswith("gas_short:"))


if __name__ == "__main__":
    unittest.main()
