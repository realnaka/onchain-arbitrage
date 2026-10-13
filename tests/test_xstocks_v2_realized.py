"""A finished cycle has to state what it cost, and the sum has to reconcile.

Measured 2026-10-07: four ``sold`` rows, three with ``net_usdt = null``, while
the three wallets had moved by about -5.40 USDT. Nothing was lost (the patrol
checked every row), but the ledger could not prove that by itself.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from arb_monitor.xstocks_v2 import executor as ex, ledger, realized


class ArithmeticTest(unittest.TestCase):
    def test_a_complete_cycle_gets_a_net(self):
        answer = realized.cycle_realized(
            cash_in_usd=D("40.10"), cash_out_usd=D("40.00"),
            fees_usd={"withdraw_fee_usd": D("0.36"), "bridge_fee_usd": D("0.10")},
            gas_usd=D("0.0012"))
        self.assertTrue(answer["measured"])
        self.assertEqual(D(answer["net_usdt"]),
                         D("40.10") - D("40.00") - D("0.36") - D("0.10") - D("0.0012"))
        self.assertEqual(answer["missing"], [])
        self.assertEqual(D(answer["fees_total_usd"]), D("0.4612"))

    def test_a_missing_part_keeps_the_net_unknown_but_names_it(self):
        """Unknown is not zero: one unreadable leg refuses the *net*, not the row."""
        answer = realized.cycle_realized(
            cash_in_usd=None, cash_out_usd=D("40"),
            fees_usd={"withdraw_fee_usd": D("0.36"), "bridge_fee_usd": None},
            gas_usd=D("0.0012"))
        self.assertFalse(answer["measured"])
        self.assertIsNone(answer["net_usdt"])
        self.assertEqual(answer["missing"], ["bridge_fee_usd", "cash_in_usd"])
        # The parts that *were* read are still written down.
        self.assertEqual(answer["parts"]["cash_out_usd"], "40")
        self.assertEqual(answer["parts"]["withdraw_fee_usd"], "0.36")

    def test_a_zero_gas_line_is_a_measurement_not_a_gap(self):
        answer = realized.cycle_realized(
            cash_in_usd=D("1"), cash_out_usd=D("1"),
            fees_usd={}, gas_usd=D("0"))
        self.assertTrue(answer["measured"])
        self.assertEqual(D(answer["net_usdt"]), D("0"))


class ExecutorTest(unittest.TestCase):
    """The finished row carries the parts, and never invents a net."""

    def _machine(self, tmp, realized_fn):
        steps = [ex.Step("sell", lambda: ex.Outcome(ex.OK), broadcasts=True)]
        return ex.Executor(symbol="SPYx", chain="x-layer", family="f",
                           mode="live", size_usdt=D("40"), tokens=D("0.05"),
                           steps=steps, gate=lambda: (True, "ok"),
                           ledger_path=Path(tmp) / "cycles.jsonl",
                           realized=realized_fn)

    def test_the_sold_row_records_the_realized_parts(self):
        tmp = tempfile.mkdtemp()
        answer = {"net_usdt": "-0.46", "measured": True, "missing": [],
                  "parts": {"cash_in_usd": "40.1", "cash_out_usd": "40.5",
                            "withdraw_fee_usd": "0.36"}}
        machine = self._machine(tmp, lambda: answer)
        result = machine.run()
        self.assertEqual(result["state"], ledger.SOLD)
        row = machine.rows[-1]
        self.assertEqual(row["costs"]["realized"]["net_usdt"], "-0.46")
        self.assertEqual(row["extra"]["realized"]["parts"]["cash_out_usd"], "40.5")

    def test_a_realized_writer_that_raises_does_not_break_the_cycle(self):
        def boom():
            raise OSError("kraken balance unreadable")

        machine = self._machine(tempfile.mkdtemp(), boom)
        result = machine.run()
        self.assertEqual(result["state"], ledger.SOLD)
        answer = machine.rows[-1]["costs"]["realized"]
        self.assertFalse(answer["measured"])
        self.assertEqual(answer["missing"], ["realized_error:OSError"])

    def test_a_refused_cycle_records_nothing_about_earnings(self):
        tmp = tempfile.mkdtemp()
        steps = [ex.Step("sell", lambda: ex.Outcome(ex.REFUSED, "no route"),
                         broadcasts=True)]
        machine = ex.Executor(symbol="SPYx", chain="x-layer", family="f",
                              mode="live", size_usdt=D("40"), tokens=D("0"),
                              steps=steps, gate=lambda: (True, "ok"),
                              ledger_path=Path(tmp) / "cycles.jsonl",
                              realized=lambda: {"net_usdt": "999", "measured": True})
        result = machine.run()
        self.assertEqual(result["state"], ledger.REFUSED)
        self.assertNotIn("realized", machine.rows[-1]["costs"])


class FamilyRealizedTest(unittest.TestCase):
    """Both live families answer "what did it earn?" from their own legs."""

    @staticmethod
    def _module(name: str):
        import importlib.util
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            name, root / "scripts" / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_family_6_charges_the_bridge_and_the_gas(self):
        module = self._module("xstocks_v2_xlayer_roundtrip")
        # The bridge fee lookup needs a *fresh* gas price, and the live gas file
        # goes stale on purpose. Reading it here made this test pass in the
        # morning and fail at night (2026-10-08) -- a unit test must not depend
        # on the operator's data directory, so the measured fee is supplied.
        self.addCleanup(setattr, module, "bridge_fee_usd", module.bridge_fee_usd)
        module.bridge_fee_usd = lambda edge: D("0.101")
        # The gas line reads the live scan snapshot (``data/xstocks_v2_scan.json``),
        # which a fresh clone and a CI runner do not have. Supply it for the same
        # reason the fee above is supplied: this test is about the arithmetic.
        self.addCleanup(setattr, module, "gas_line_usd", module.gas_line_usd)
        module.gas_line_usd = lambda: (D("0.02"), "test")
        ctx = {"price": D("775.99"), "withdraw_fee": D("0.00046539")}
        state = {"kraken_usd_before": D("40.54"), "kraken_usd_after": D("0"),
                 "before_usdg": D("50.0"), "usdg_after": D("89.5")}
        answer = module.realized_xlayer(ctx, state)
        self.assertTrue(answer["measured"], answer)
        parts = answer["parts"]
        self.assertEqual(D(parts["cash_out_usd"]), D("40.54"))
        self.assertEqual(D(parts["cash_in_usd"]), D("39.5"))
        # The bridge fee and the gas line are both in there, and the withdraw fee
        # is priced in units x price rather than assumed.
        self.assertGreater(D(parts["bridge_fee_usd"]), D("0.09"))
        self.assertGreater(D(parts["gas_usd"]), D("0"))
        self.assertAlmostEqual(float(D(parts["withdraw_fee_usd"])),
                               float(D("0.00046539") * D("775.99")), places=6)

    def test_family_2_pays_no_bridge_and_no_evm_gas(self):
        module = self._module("xstocks_v2_kraken_to_chain")
        ctx = {"price": D("775.73"), "withdraw_fee": D("0.00045901")}
        state = {"kraken_usd_before": D("40.54"), "kraken_usd_after": D("1.0"),
                 "before_usdc": D("8.48"), "after_usdc": D("47.9")}
        answer = module.realized_family2(ctx, state)
        self.assertTrue(answer["measured"], answer)
        self.assertEqual(D(answer["parts"]["gas_usd"]), D("0"))
        self.assertNotIn("bridge_fee_usd", answer["parts"])
        # Same cash legs as family 6, minus the message: the whole point of the
        # direct family.
        expected = (D("47.9") - D("8.48")) - (D("40.54") - D("1.0")) \
            - D("0.00045901") * D("775.73")
        self.assertEqual(D(answer["net_usdt"]).quantize(D("0.0001")),
                         expected.quantize(D("0.0001")))

    def test_a_leg_that_was_not_read_keeps_the_net_unknown(self):
        module = self._module("xstocks_v2_kraken_to_chain")
        ctx = {"price": D("775.73"), "withdraw_fee": D("0.00045901")}
        state = {"kraken_usd_before": D("40.54"), "kraken_usd_after": D("1.0")}
        answer = module.realized_family2(ctx, state)   # no USDC readings
        self.assertFalse(answer["measured"])
        self.assertIn("cash_in_usd", answer["missing"])


class TotalsTest(unittest.TestCase):
    def _rows(self):
        return [
            {"mode": "live", "state": "sold", "symbol": "SPYx", "ts_ms": 1,
             "costs": {"realized": {"measured": True, "net_usdt": "-1.50",
                                    "missing": []}}},
            {"mode": "live", "state": "sold", "symbol": "SPYx", "ts_ms": 2,
             "costs": {"realized": {"measured": True, "net_usdt": "0.25",
                                    "missing": []}}},
            {"mode": "live", "state": "sold", "symbol": "SPCXx", "ts_ms": 3,
             "costs": {"realized": {"measured": False, "net_usdt": None,
                                    "missing": ["bridge_fee_usd"]}}},
            {"mode": "live", "state": "sold", "symbol": "NVDAx", "ts_ms": 4,
             "costs": {}},                      # written before A15 existed
            {"mode": "paper", "state": "sold", "symbol": "SPYx", "ts_ms": 5,
             "costs": {"realized": {"measured": True, "net_usdt": "999"}}},
            {"mode": "live", "state": "refused", "symbol": "SPYx", "ts_ms": 6},
        ]

    def test_the_sum_and_the_gaps_are_both_reported(self):
        got = realized.totals(self._rows())
        self.assertEqual(got["measured_rows"], 2)
        self.assertEqual(got["unmeasured_rows"], 2)
        self.assertEqual(D(got["net_usdt"]), D("-1.25"))
        self.assertEqual(got["by_symbol"], {"SPYx": "-1.25"})
        missing = {(row["symbol"], tuple(row["missing"]))
                   for row in got["unmeasured"]}
        self.assertIn(("SPCXx", ("bridge_fee_usd",)), missing)
        self.assertIn(("NVDAx", ("not_recorded",)), missing)

    def test_paper_rows_are_not_money(self):
        got = realized.totals(self._rows())
        self.assertNotIn("999", got["net_usdt"])

    def test_the_reconciliation_the_patrol_asked_for(self):
        """Sum of realized nets must be compared with the wallets' own change.

        The test states the rule the patrol used: a gap is a finding, not a
        rounding error, and a row that could not be priced is a *reason* for a
        gap -- so both numbers travel together.
        """
        rows = self._rows()
        totals = realized.totals(rows)
        balances_before = D("40.54") + D("44.54") + D("50.86")
        balances_after = D("0.00") + D("52.12") + D("62.95")
        wallet_change = balances_after - balances_before
        ledger_net = D(totals["net_usdt"])
        gap = wallet_change - ledger_net
        # 115.07 - 135.94 = -20.87 of wallet change, against -1.25 the ledger can
        # prove: the gap is -19.62, and the two rows it cannot price are listed
        # right next to it instead of being averaged away.
        self.assertTrue(totals["unmeasured_rows"] > 0)
        self.assertEqual(wallet_change.quantize(D("0.01")), D("-20.87"))
        self.assertEqual(gap.quantize(D("0.01")), D("-19.62"))
        # and the definition of "realized" lives in one place
        self.assertEqual(realized.realized_of(rows[0]), D("-1.50"))
        self.assertIsNone(realized.realized_of(rows[3]))


if __name__ == "__main__":
    unittest.main()
