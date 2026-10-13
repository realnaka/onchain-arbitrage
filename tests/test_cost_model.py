from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import unittest

from arb_monitor.apps.hk_monitor import costs_from_config, load_config
from arb_monitor.strategy.xstocks_screen import entry_cost_bps

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/hk_monitor.toml"


class CostModelTests(unittest.TestCase):
    """The cost model must stay tied to measured fills, not to guesses."""

    def setUp(self) -> None:
        self.config = load_config(str(CONFIG))

    def test_perp_fee_is_the_measured_taker_rate_not_the_old_guess(self):
        costs = costs_from_config(self.config)
        # Six live fills were all taker at 4.50000 bps; the model keeps 5 bps.
        self.assertEqual(costs.perp_fee_bps, Decimal("5"))
        self.assertEqual(self.config["costs"]["perp_fee_bps_measured"], "4.5")
        self.assertLessEqual(costs.perp_fee_bps, Decimal("5"))

    def test_on_chain_leg_is_not_charged_twice(self):
        costs = costs_from_config(self.config)
        # The pool fee is already inside the on-chain ask the model uses.
        self.assertEqual(costs.spot_fee_bps, Decimal("0"))
        breakdown = entry_cost_bps(self.config["costs"], None, Decimal("100"))
        self.assertEqual(breakdown["total_bps"], Decimal("15"))

    def test_gas_budget_is_explicit_zero_with_receipt_evidence(self):
        costs = costs_from_config(self.config)
        # Written explicitly rather than left unknown, so the pairing gate can be
        # judged; gas_watch re-reads real receipts and warns if sponsorship ends.
        self.assertEqual(costs.roundtrip_gas_budget_usdt, Decimal("0"))
        self.assertEqual(self.config["costs"]["gas_measured_usdt"], "0")

    def test_costs_are_verified_against_measured_fills_with_an_expiry(self):
        costs = costs_from_config(self.config)
        # Flipped 2026-09-16 after three 3-share round trips: every perpetual
        # fill was taker at 4.5 bps and both on-chain sides were compared
        # against the pool price on the side actually traded.
        self.assertTrue(costs.verified)
        self.assertGreater(costs.valid_until_ms, 1_789_500_000_000)
        evidence = self.config["costs"]["evidence"]
        self.assertIn("4.5 bps", evidence)
        self.assertIn("gas", evidence)

    def test_the_exit_assumption_is_configured_not_assumed(self):
        scenario = self.config.get("scenario") or {}
        # The strategy refuses to invent a future basis; the operator supplies it.
        self.assertEqual(scenario.get("target_exit_basis_bps"), "3")
        self.assertIn("converge", scenario.get("exit_policy", ""))

    def test_gas_sponsorship_is_watched_on_a_clock(self):
        watch = self.config.get("gas_watch") or {}
        self.assertTrue(watch.get("enabled"))
        self.assertGreaterEqual(int(watch.get("check_seconds")), 60)


if __name__ == "__main__":
    unittest.main()
