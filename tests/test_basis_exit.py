"""Basis-target exit: enter at 20 bps, close when the spread comes back to 3.

Operator's rule, 2026-09-21. The rule itself is small; what these tests guard is
that it can only ever fire on an observation we would actually be willing to
trade against:

* a basis from a venue we cannot execute on is not an observation (closing a
  gate price against an okx position would price a trade that cannot happen);
* a flagged or stale evaluation alerts a human instead of closing;
* nothing at all is not a signal to close.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from arb_monitor.execution.basis_exit import (BasisExitPolicy, BasisObservation,
                                              decide)
from arb_monitor.execution.ledger import ExecutionLedger
from arb_monitor.execution.order_flows import FlowResult
from tests.test_auto_execute import Recorder
from tests.test_funding_exit import NOW, FakeFundingStack, FundingExitExecutorTests


class BasisRuleTests(unittest.TestCase):
    policy = BasisExitPolicy(enabled=True, target_exit_basis_bps=Decimal("3"),
                             max_quote_age_ms=15_000)

    def observation(self, basis="2.5", *, age_ms=0, trusted=True, detail=""):
        return BasisObservation(
            asset="XIAOMI", instrument="okx:XIAOMI-USDT-SWAP",
            exit_basis_bps=Decimal(basis) if basis is not None else None,
            observed_at_ms=NOW - age_ms, trusted=trusted, detail=detail)

    def test_the_operators_target_closes(self):
        for basis in ("3", "0", "-1", "2.5"):
            with self.subTest(basis=basis):
                self.assertEqual(
                    decide(self.policy, self.observation(basis), NOW).action,
                    "close_now")

    def test_a_basis_above_the_target_waits(self):
        self.assertEqual(decide(self.policy, self.observation("3.0001"), NOW).action,
                         "above_target")
        self.assertEqual(decide(self.policy, self.observation("40"), NOW).action,
                         "above_target")

    def test_a_flagged_evaluation_is_unknown_not_a_close(self):
        decision = decide(
            self.policy,
            self.observation("1", trusted=False,
                             detail="data_quality:non_simultaneous_quotes"),
            NOW)
        self.assertEqual(decision.action, "unknown")
        self.assertIn("non_simultaneous_quotes", decision.reason)

    def test_a_stale_evaluation_is_not_a_close(self):
        self.assertEqual(
            decide(self.policy, self.observation("1", age_ms=60_000), NOW).action,
            "stale")

    def test_no_observation_is_not_a_close(self):
        self.assertEqual(decide(self.policy, None, NOW).action, "no_observation")
        self.assertEqual(decide(self.policy, self.observation(None), NOW).action,
                         "unknown")

    def test_the_rule_can_be_disabled_by_config(self):
        off = BasisExitPolicy.from_policy({"auto_close": {"enabled": False}})
        self.assertFalse(off.enabled)
        self.assertEqual(decide(off, self.observation("1"), NOW).action, "disabled")

    def test_the_policy_reads_the_operators_numbers(self):
        loaded = BasisExitPolicy.from_policy(
            {"auto_close": {"enabled": True, "target_exit_basis_bps": "3"}})
        self.assertTrue(loaded.enabled)
        self.assertEqual(loaded.target_exit_basis_bps, Decimal("3"))


class BasisExitExecutorTests(FundingExitExecutorTests):
    """The executor half: same harness as the funding rule, same close path."""

    def test_a_group_without_an_instrument_never_costs_a_venue_read(self):
        # `open_exposure_groups` keeps `perp_closed` in its default set on
        # purpose: a close can settle the perpetual leg before the spot sale, and
        # the residual spot still has to be found. But a group that recorded no
        # instrument cannot be closed at all — `_close_group` returns
        # `asset_unresolved` without writing anything. Measured 2026-09-21: one
        # such group from 2026-09-16 made both exit rules build a live stack and
        # read the venue's positions twice per cycle, forever, for nothing.
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "execution.db")
            ledger = ExecutionLedger(path)
            ledger.append("xiaomi-legacy", "perp_closed", "perp_closed", {},
                          NOW - 1_000)
            ledger.close()
            stack = FakeFundingStack()
            reads: list = []
            stack.read.positions = lambda *a, **k: (reads.append(1), [])[1]
            record = self.executor(tmp, stack, None).maybe_basis_exit(
                [self.evaluation("2.5")], NOW)
        self.assertEqual(record["skipped"].get("group_without_instrument"), 1)
        self.assertEqual(record["closed"], [])
        self.assertEqual(reads, [],
                         "the venue was read for a group no close could address")

    def test_a_hedged_group_with_only_an_asset_uses_the_one_allowlisted_contract(self):
        # The three live CRWV hedges recorded asset and fills, not the contract.
        # One reviewed OKX mapping is enough to name the close. Two mappings,
        # or no asset at all, still skip the group and do not read the venue.
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "execution.db")
            ledger = ExecutionLedger(path)
            ledger.append("crwv-open", "hedged", "legs",
                          {"asset": "CRWV", "filled_contracts": "0.01"}, NOW)
            ledger.close()
            stack = FakeFundingStack()
            reads: list = []
            stack.read.positions = lambda *a, **k: (
                reads.append(1),
                [{"instId": "CRWV-USDT-SWAP", "pos": "-0.01"}])[1]
            executor = self.executor(tmp, stack, None)
            executor.policy["allowlist"]["instruments"] = [
                "XIAOMI-USDT-SWAP", "CRWV-USDT-SWAP"]
            executor.monitor_config["tokens"].append(
                {"asset": "CRWV",
                 "mappings": {"okx:CRWV-USDT-SWAP": {"verified": True}}})
            record = executor.maybe_basis_exit(
                [self.evaluation("12", instrument="okx:CRWV-USDT-SWAP")], NOW)
        self.assertEqual(record["skipped"].get("group_without_instrument"), None)
        self.assertEqual(record["instrument_recovered"],
                         [{"group_id": "crwv-open", "asset": "CRWV",
                           "instrument": "CRWV-USDT-SWAP"}])
        self.assertEqual(record["evaluations"][0]["action"], "above_target")
        self.assertEqual(reads, [1])

    def test_two_contracts_for_one_asset_are_not_a_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "execution.db")
            ledger = ExecutionLedger(path)
            ledger.append("crwv-open", "hedged", "legs", {"asset": "CRWV"}, NOW)
            ledger.close()
            stack = FakeFundingStack()
            reads: list = []
            stack.read.positions = lambda *a, **k: (reads.append(1), [])[1]
            executor = self.executor(tmp, stack, None)
            executor.policy["allowlist"]["instruments"] = [
                "CRWV-USDT-SWAP", "CRWV-USDC-SWAP"]
            executor.monitor_config["tokens"].append(
                {"asset": "CRWV", "mappings": {
                    "okx:CRWV-USDT-SWAP": {},
                    "okx:CRWV-USDC-SWAP": {}}})
            record = executor.maybe_basis_exit(
                [self.evaluation("1", instrument="okx:CRWV-USDT-SWAP")], NOW)
        self.assertEqual(record["skipped"].get("group_without_instrument"), 1)
        self.assertEqual(record["closed"], [])
        self.assertEqual(reads, [])

    def executor(self, tmp, stack, notifier, **execution):
        executor = super().executor(tmp, stack, notifier, **execution)
        executor.policy = {**executor.policy,
                           "auto_close": {"enabled": True,
                                          "target_exit_basis_bps": "3"}}
        return executor

    def evaluation(self, basis="2.5", *, instrument="okx:XIAOMI-USDT-SWAP",
                   rejections=(), age_ms=0):
        return {"asset": "XIAOMI", "instrument": instrument,
                "current_exit_basis_bps": basis,
                "evaluated_at_ms": NOW - age_ms,
                "rejections": list(rejections)}

    def test_the_target_closes_the_group_through_the_reviewed_close_path(self):
        import arb_monitor.execution.auto_execute as module

        with tempfile.TemporaryDirectory() as tmp:
            self.ledger_with_short(tmp)
            stack = FakeFundingStack()
            calls: dict = {}
            original = module.maker_first_close

            def fake_close(chain, plan, pol, ledger, **kwargs):
                calls.update(kwargs, plan_asset=plan.asset)
                return FlowResult("closed", {"state": "closed", "executed": True,
                                             "halt": False, "reason": "closed"})

            module.maker_first_close = fake_close
            try:
                record = self.executor(tmp, stack, None).maybe_basis_exit(
                    [self.evaluation("2.5")], NOW)
            finally:
                module.maker_first_close = original
            self.assertEqual(len(record["closed"]), 1)
            self.assertEqual(record["closed"][0]["state"], "closed")
            self.assertEqual(record["closed"][0]["reason"],
                             "exit_basis_2.5_at_or_below_target_3")
            self.assertEqual(calls["contracts"], Decimal("3"))
            self.assertEqual(calls["opened_group_id"], "xiaomi-open")

    def test_a_basis_above_the_target_does_not_close(self):
        import arb_monitor.execution.auto_execute as module

        with tempfile.TemporaryDirectory() as tmp:
            self.ledger_with_short(tmp)
            original = module.maker_first_close
            try:
                module.maker_first_close = lambda *a, **k: self.fail(
                    "a basis above the target must not close")
                record = self.executor(tmp, FakeFundingStack(), None).maybe_basis_exit(
                    [self.evaluation("12")], NOW)
            finally:
                module.maker_first_close = original
            self.assertEqual(record["evaluations"][0]["action"], "above_target")
            self.assertEqual(record["closed"], [])

    def test_a_basis_from_a_venue_we_cannot_trade_is_not_an_observation(self):
        import arb_monitor.execution.auto_execute as module

        with tempfile.TemporaryDirectory() as tmp:
            self.ledger_with_short(tmp)
            notifier = Recorder()
            original = module.maker_first_close
            try:
                module.maker_first_close = lambda *a, **k: self.fail(
                    "a gate basis cannot close an okx position")
                record = self.executor(
                    tmp, FakeFundingStack(), notifier).maybe_basis_exit(
                        [self.evaluation("1", instrument="gate:XIAOMI_USDT")], NOW)
            finally:
                module.maker_first_close = original
            # Not "close_now" and not silent: the reason names what was missing,
            # so the operator reading the alert knows it is a data problem.
            self.assertEqual(record["evaluations"][0]["action"], "unknown")
            self.assertEqual(record["evaluations"][0]["reason"],
                             "no_evaluation_on_a_tradable_venue")
            self.assertEqual(record["alerts"], ["xiaomi-open"])
            self.assertTrue(any("基差平仓" in m for m in notifier.messages))

    def test_a_flagged_basis_alerts_instead_of_closing(self):
        import arb_monitor.execution.auto_execute as module

        with tempfile.TemporaryDirectory() as tmp:
            self.ledger_with_short(tmp)
            original = module.maker_first_close
            try:
                module.maker_first_close = lambda *a, **k: self.fail(
                    "a flagged evaluation must not close")
                record = self.executor(tmp, FakeFundingStack(), None).maybe_basis_exit(
                    [self.evaluation("1", rejections=["non_simultaneous_quotes"])],
                    NOW)
            finally:
                module.maker_first_close = original
            self.assertEqual(record["evaluations"][0]["action"], "unknown")
            self.assertIn("non_simultaneous_quotes",
                          record["evaluations"][0]["reason"])
            self.assertEqual(record["closed"], [])

    def test_the_rule_is_inert_in_paper_mode(self):
        import arb_monitor.execution.auto_execute as module

        with tempfile.TemporaryDirectory() as tmp:
            self.ledger_with_short(tmp)
            executor = self.executor(tmp, FakeFundingStack(), None,
                                     auto_orders="paper")
            original = module.maker_first_close
            try:
                module.maker_first_close = lambda *a, **k: self.fail(
                    "paper must not close a real position")
                record = executor.maybe_basis_exit([self.evaluation("1")], NOW)
            finally:
                module.maker_first_close = original
            self.assertEqual(record["reason"], "order_mode_not_live")
            self.assertEqual(record["closed"], [])

    def test_no_open_group_means_nothing_to_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self.executor(tmp, FakeFundingStack(), None).maybe_basis_exit(
                [self.evaluation("1")], NOW)
            self.assertEqual(record["reason"], "no_open_group")


if __name__ == "__main__":
    unittest.main()
