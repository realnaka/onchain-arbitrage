"""C: the pre-flight gas top-up decides; it never spends.

Measured 2026-10-09: X Layer holds 0.0006056 OKB and one CCIP message costs
0.00080244199238106 OKB, so every x-layer family is refused ``gas_low`` before
the buy leg and nothing but a human refills it. The fix is to swap the shortfall
(plus a reserve) out of USDG on the same chain.

Two properties matter more than the happy path:

* **off by default, and a blank cap is a refusal.** An unattended size ceiling is
  the operator's number (``cap_not_set``), not "unlimited".
* **an unreadable number is not a small number.** An unknown balance, fee or
  price refuses -- a top-up sized off a zero would buy nothing and still write a
  ledger row.
"""
from __future__ import annotations

import unittest
from decimal import Decimal as D

from arb_monitor.xstocks_v2 import gas_topup as gt

FEE = "0.00080244199238106"
PRICE = D("124.4731")


def policy(**kwargs) -> gt.TopupPolicy:
    base = {"enabled": True, "max_usdt_per_topup": D("1"),
            "max_topups_per_day": 2}
    base.update(kwargs)
    return gt.TopupPolicy(**base)


class DecideTest(unittest.TestCase):
    def _decide(self, **kwargs):
        args = {"policy": policy(), "chain": "x-layer",
                "native_balance": D("0.0006056"), "fee_per_message": D(FEE),
                "stable_balance": D("50"), "native_usd": PRICE}
        args.update(kwargs)
        return gt.decide(**args)

    def test_the_shipped_default_is_off(self):
        decision = self._decide(policy=gt.TopupPolicy())
        self.assertFalse(decision["topup"])
        self.assertEqual(decision["reason"], "disabled")

    def test_a_blank_cap_is_not_unlimited(self):
        decision = self._decide(policy=gt.TopupPolicy(
            enabled=True, max_usdt_per_topup=None, max_topups_per_day=2))
        self.assertFalse(decision["topup"])
        self.assertEqual(decision["reason"], "cap_not_set")
        decision = self._decide(policy=gt.TopupPolicy(
            enabled=True, max_usdt_per_topup=D("1"), max_topups_per_day=None))
        self.assertEqual(decision["reason"], "cap_not_set")

    def test_the_measured_shortfall_becomes_the_size(self):
        decision = self._decide()
        self.assertTrue(decision["topup"])
        self.assertEqual(decision["reason"], "shortfall_within_cap")
        # Two messages of reserve minus what we hold.
        expected = D("2") * D(FEE) - D("0.0006056")
        self.assertAlmostEqual(float(decision["size_usdt"]),
                               float(expected * PRICE), places=6)
        self.assertEqual(decision["gas"], "OKB")
        self.assertEqual(decision["stable"], "USDG")

    def test_a_funded_wallet_is_left_alone(self):
        decision = self._decide(native_balance=D("0.002"))
        self.assertFalse(decision["topup"])
        self.assertEqual(decision["reason"], "no_shortfall")

    def test_a_size_over_the_cap_is_refused_not_clamped(self):
        """Clamping would leave the leg short and the trade unpayable."""
        decision = self._decide(policy=policy(max_usdt_per_topup=D("0.05")))
        self.assertFalse(decision["topup"])
        self.assertEqual(decision["reason"], "over_cap")

    def test_the_daily_ceiling_stops_the_third_topup(self):
        self.assertTrue(self._decide(tops_today=1)["topup"])
        decision = self._decide(tops_today=2)
        self.assertFalse(decision["topup"])
        self.assertEqual(decision["reason"], "daily_cap")

    def test_a_short_stable_balance_is_a_refusal(self):
        decision = self._decide(stable_balance=D("0.001"))
        self.assertFalse(decision["topup"])
        self.assertEqual(decision["reason"], "stable_short")

    def test_unknowns_refuse(self):
        self.assertEqual(self._decide(native_balance=None)["reason"],
                         "balance_unknown")
        self.assertEqual(self._decide(fee_per_message=None)["reason"],
                         "fee_unknown")
        self.assertEqual(self._decide(native_usd=None)["reason"],
                         "gas_price_unknown")

    def test_a_chain_without_a_route_is_refused(self):
        self.assertEqual(self._decide(chain="ton")["reason"], "no_route:ton")

    def test_the_size_never_exceeds_the_cap_or_the_shortfall(self):
        """The invariant a caller may rely on when it builds the swap."""
        for balance in ("0.0", "0.0001", "0.0006056", "0.0008"):
            decision = self._decide(native_balance=D(balance))
            if not decision["topup"]:
                continue
            size = D(decision["size_usdt"])
            shortfall = D("2") * D(FEE) - D(balance)
            self.assertLessEqual(size, D("1"))
            self.assertLessEqual(size, shortfall * PRICE)


class LedgerRowTest(unittest.TestCase):
    def test_the_row_carries_what_was_swapped_and_where(self):
        row = gt.ledger_row(chain="x-layer", size_usdt=D("0.1"),
                            got_native=D("0.00083"), native_usd=PRICE,
                            tx="0xabc", symbol="SPYx")
        self.assertEqual(row["event"], "gas_topup")
        self.assertEqual(row["stable"], "USDG")
        self.assertEqual(row["gas"], "OKB")
        self.assertEqual(row["tx"], "0xabc")


if __name__ == "__main__":
    unittest.main()
