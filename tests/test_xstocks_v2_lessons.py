"""Lesson regressions and cases the first 32 tests did not lock."""
from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from arb_monitor.xstocks_v2 import cycle, guards, scoreboard
from arb_monitor.xstocks_v2.costs import path_cost


class KnownBugTests(unittest.TestCase):
    def test_first_sol_transfer_is_not_the_base_fee(self):
        first = guards.sol_transfer_fee(first_for_mint=True)
        self.assertEqual(first, Decimal("0.00313"))
        self.assertNotEqual(first, Decimal("0.000015"))
        self.assertEqual(guards.sol_transfer_fee(first_for_mint=False), Decimal("0.000015"))

    def test_per_token_price_is_not_per_share(self):
        per_share = guards.per_share_price(Decimal("152.31"), Decimal("1.0259"))
        self.assertLess(per_share, Decimal("152.31"))
        self.assertEqual(per_share, Decimal("152.31") / Decimal("1.0259"))

    def test_missing_balance_is_not_zero(self):
        self.assertFalse(guards.balance_is_known(None))
        self.assertTrue(guards.balance_is_known(Decimal("0")))

    def test_dust_and_undersized_deposit_do_not_count(self):
        minimum = Decimal("0.05459063574")
        self.assertFalse(guards.dust_counts_as_credit(Decimal("0.000001"), minimum))
        self.assertFalse(guards.may_deposit_to_kraken(Decimal("0.0001"), Decimal("0.0164")))
        self.assertTrue(guards.may_deposit_to_kraken(Decimal("0.02"), Decimal("0.0164")))

    def test_source_simulation_is_not_delivery(self):
        self.assertFalse(guards.arrival_confirmed(
            source_simulated=True, dest_balance_increased=False))
        self.assertTrue(guards.arrival_confirmed(
            source_simulated=False, dest_balance_increased=True))

    def test_unknown_duration_is_not_a_few_minutes(self):
        self.assertFalse(guards.route_duration_known(None))
        self.assertTrue(guards.route_duration_known(25 * 60))

    def test_wrapper_redeems_on_the_same_chain_only(self):
        self.assertTrue(guards.needs_same_chain_redeem("wrapper"))
        self.assertFalse(guards.needs_same_chain_redeem("native"))

    def test_phase1_never_rests_a_maker_sell(self):
        self.assertTrue(guards.phase1_sell_is_taker("XOPx"))
        self.assertTrue(guards.phase1_sell_is_taker("STRCx"))

    def test_sol_is_not_sent_to_an_ata(self):
        self.assertTrue(guards.sol_may_fund("wallet"))
        self.assertFalse(guards.sol_may_fund("ata"))

    def test_one_bad_row_does_not_drop_the_venue(self):
        kept = guards.drop_bad_row([
            {"symbol": "A", "bad": False},
            {"symbol": "SOXS", "bad": True},
            {"symbol": "B", "bad": False},
        ])
        self.assertEqual([row["symbol"] for row in kept], ["A", "B"])

    def test_budget_field_is_not_quote_age(self):
        self.assertFalse(guards.quote_age_field("quote_max_age_vs_budget_ms"))
        self.assertTrue(guards.quote_age_field("recv_age_ms"))

    def test_fixed_fee_stays_in_the_path_cost(self):
        net_inputs = path_cost([
            Decimal("0.38"), Decimal("0.10"), Decimal("0.88"),
        ])
        self.assertEqual(net_inputs, Decimal("1.36"))
        self.assertIsNone(path_cost([Decimal("0.38"), None]))

    def test_sandwich_exposure_is_slippage_times_notional(self):
        self.assertEqual(
            guards.sandwich_exposure(Decimal("50"), Decimal("857")),
            Decimal("857") * Decimal("50") / Decimal("10000"))

    def test_reverted_atomic_trade_does_not_halt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path, halt_path = root / "cycle.lock", root / "HALT"
            with cycle.CycleLock(lock_path, halt_path) as lock:
                cycle.finish_state(cycle.REVERTED, lock)
            self.assertFalse(lock_path.exists())
            self.assertFalse(halt_path.exists())

    def test_stale_rows_are_not_opportunities(self):
        rows = [{"priced": False, "hit": False, "reason": ""} for _ in range(85)]
        rows += [{"priced": True, "hit": False, "reason": "stale_or_skew"} for _ in range(5)]
        rows += [{"priced": True, "hit": True, "reason": ""} for _ in range(2)]
        rows += [{"priced": True, "hit": False, "reason": ""} for _ in range(8)]
        got = scoreboard.tally(rows)
        self.assertEqual(got["untrusted"], 5)
        self.assertEqual(got["priced"], 10)
        self.assertEqual(got["hits"], 2)
        self.assertEqual(
            scoreboard.opportunity_rate(priced_rounds=got["priced"], opportunity_rounds=got["hits"]),
            0.2)

    def test_sign_mismatch_fails_even_if_the_notional_gap_looks_small(self):
        self.assertFalse(guards.reconciliation_ok(
            predicted=Decimal("0.15"), realized=Decimal("-0.15"), notional=Decimal("25")))
        self.assertTrue(guards.reconciliation_ok(
            predicted=Decimal("0.20"), realized=Decimal("0.18"), notional=Decimal("25")))


if __name__ == "__main__":
    unittest.main()
