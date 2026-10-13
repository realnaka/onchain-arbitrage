"""Paper groups are positions, and the sample says what it does not know.

Reviewed 2026-09-16: paper wrote two events and consumed the daily quota, so
there was no paper position to close, no way to reconstruct what the run
assumed, and the live budget read 90.252212/100 with no money moved. These
tests pin the position model, the closing arithmetic and the honesty of the
statistics.
"""
from __future__ import annotations

from decimal import Decimal
import unittest

from arb_monitor.apps.paper_cli import run as paper_run
from arb_monitor.execution.ledger import ExecutionLedger


ENTRY = {
    "signal_id": "shein-1", "leg": "buy", "mode": "paper",
    "fill_assumption": "full_fill_at_touch",
    "size_quote": "10.00", "quote_amount": "10.00",
    "entry_cost_bps": "15",
    "plan": {"asset": "SHEIN", "gross_entry_basis_bps": "40"},
}


class PaperPositionTests(unittest.TestCase):
    def ledger(self):
        ledger = ExecutionLedger()
        ledger.append("shein-1", "paper_hedged", "paper_filled", ENTRY, 1_789_520_000_000)
        return ledger

    def test_an_open_paper_group_is_a_position(self):
        ledger = self.ledger()
        positions = ledger.paper_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["state"], "open")
        self.assertEqual(positions[0]["size_quote"], "10.00")
        self.assertEqual(positions[0]["entries"], "40")

    def test_closing_it_records_the_arithmetic_not_just_the_result(self):
        ledger = self.ledger()
        payload = ledger.close_paper_position(
            "shein-1", exit_basis_bps=Decimal("3"),
            created_ts_ms=1_789_520_600_000, note="basis converged")
        # 10 quote × (40 − 3) bps = 0.037 gross; 15 bps per leg × 2 = 0.03 cost.
        self.assertEqual(payload["modelled_gross_pnl_quote"], "0.037")
        self.assertEqual(Decimal(payload["modelled_cost_quote"]), Decimal("0.030"))
        self.assertEqual(payload["modelled_net_pnl_quote"], "0.007")
        self.assertEqual(payload["fill_assumption"],
                         "modelled_exit_at_observed_basis")
        positions = ledger.paper_positions()
        self.assertEqual(positions[0]["state"], "closed")

    def test_closing_twice_is_refused(self):
        ledger = self.ledger()
        ledger.close_paper_position("shein-1", exit_basis_bps=Decimal("3"),
                                    created_ts_ms=1_789_520_600_000)
        with self.assertRaises(ValueError):
            ledger.close_paper_position("shein-1", exit_basis_bps=Decimal("3"),
                                        created_ts_ms=1_789_520_700_000)

    def test_closing_something_that_was_never_opened_is_refused(self):
        with self.assertRaises(KeyError):
            self.ledger().close_paper_position("nope", exit_basis_bps=Decimal("3"),
                                               created_ts_ms=1)

    def test_the_stats_name_the_metrics_the_sample_cannot_answer(self):
        stats = self.ledger().paper_stats()
        self.assertEqual(stats["open_positions"], 1)
        self.assertIn("maker_fill_probability", stats["unavailable"])
        self.assertIn("partial_fill_distribution", stats["unavailable"])
        self.assertIn("fill_assumption", stats["unavailable_reason"])

    def test_the_cli_refuses_a_close_without_an_observed_exit(self):
        import argparse
        args = argparse.Namespace(group="shein-1", exit_basis_bps=None, note="",
                                  stats=False, json=False, ledger=":memory:",
                                  list=False)
        self.assertEqual(paper_run(args), 2)


if __name__ == "__main__":
    unittest.main()
