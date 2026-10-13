"""The reference refresh runs off the sampling thread — and still fails closed.

Reference data is evidence: a token identity, a public pool price, a daily
candle. Until 2026-09-21 it was fetched inline inside the sampling cycle on a
20 s budget, and a slow request pushed every later sample out by minutes. From
this change on, only the first round after a start is inline (an empty token
table would make the first cycle screen nothing); later rounds run on their own
thread and the cycle adopts whatever the last completed round produced.

Two invariants have to survive that move:

* a round that could not re-read an asset no longer *deletes* it — the last good
  answer is kept and labelled with its own age;
* a kept (`stale`) token reference is a display/screening fact only. The token
  identity is the `execution_required` tier, so `execution_blockers` names it and
  `AutoExecutor.trigger` refuses to sign it. That is the same outcome as the old
  "the asset is simply absent", with a reason attached.
"""
from __future__ import annotations

import threading
import unittest

from arb_monitor.apps.hk_monitor import execution_blockers
from arb_monitor.execution.auto_execute import AutoExecutor

from tests.test_auto_execute import candidate as auto_candidate
from tests.test_auto_execute import policy as auto_policy
from tests.test_cycle_instrumentation import FakeReferences, Harness


class ReferenceWorkerTests(unittest.TestCase):
    def test_the_second_round_runs_off_the_cycle_thread(self):
        # A gate the test controls: the round cannot finish until it is opened,
        # so "the cycle returned anyway" is an invariant, not a stopwatch.
        entered = threading.Event()
        release = threading.Event()
        armed = {"on": False}

        def on_call(kind, asset):
            if armed["on"]:
                entered.set()
                release.wait(5.0)

        refs = FakeReferences(on_call=on_call)
        try:
            with Harness(references=refs) as h:
                # The first round is the startup one and runs inline.
                h.cycles(1)
                self.assertFalse(h.monitor._reference_worker_state()["in_flight"])
                armed["on"] = True
                h.monitor.last_reference = 0     # force the next round to be due
                summary = h.cycles(1)[0]
                self.assertTrue(entered.wait(5.0), "the round never started")
                self.assertTrue(summary["reference_worker"]["in_flight"],
                                "the cycle waited for the round instead of "
                                "handing it to the worker")
                self.assertTrue(summary["reference_worker"]["primed"])
                release.set()
        finally:
            release.set()

    def test_the_worker_adopts_the_round_on_a_later_cycle(self):
        refs = FakeReferences()
        with Harness(references=refs) as h:
            h.cycles(1)
            rounds_after_prime = h.monitor.reference_rounds
            h.monitor.last_reference = 0
            h.cycles(1)                     # starts the round
            self.assertIsNotNone(h.monitor._reference_future)
            h.monitor._reference_future.result(timeout=5)
            summary = h.cycles(1)[0]        # adopts it
        self.assertEqual(summary["reference_worker"]["rounds"],
                         rounds_after_prime + 1)
        self.assertFalse(summary["reference_worker"]["in_flight"])
        self.assertEqual(summary["reference_worker"]["kept_from_previous_round"], 0)

    def test_a_failed_round_keeps_the_last_good_answer_and_dates_it(self):
        refs = FakeReferences()
        with Harness(references=refs) as h:
            first = h.cycles(1)[0]
            fresh_rows = {row["asset"]: row for row in first["token_references"]}
            self.assertFalse(fresh_rows["XIAOMI"]["reference_stale"])
            self.assertEqual(fresh_rows["XIAOMI"]["reference_age_ms"], 0)
            # The next round cannot re-read anything.
            refs.fail["token_reference"] = "PublicHTTPError:429"
            h.monitor.last_reference = 0
            h.cycles(1)
            h.monitor._reference_future.result(timeout=5)
            summary = h.cycles(1)[0]
            rows = {row["asset"]: row for row in summary["token_references"]}
        # The asset is still screened — an un-refreshed reference is not a
        # missing asset — and the row says how old the confirmation is.
        self.assertIn("XIAOMI", rows)
        self.assertTrue(rows["XIAOMI"]["reference_stale"])
        self.assertIn("token_reference",
                      {error["kind"] for error in summary["source_errors"]})
        self.assertEqual(summary["reference_worker"]["kept_from_previous_round"], 2)

    def test_a_first_round_failure_does_not_invent_an_asset(self):
        # With nothing to keep, a failed round leaves the table empty rather
        # than filling it with a guess.
        refs = FakeReferences(fail={"token_reference": "PublicHTTPError:429"})
        with Harness(references=refs) as h:
            summary = h.cycles(1)[0]
        self.assertEqual(summary["token_references"], [])
        self.assertEqual(summary["candidate_count"], 0)


class StaleReferenceIsNotExecutableTests(unittest.TestCase):
    def test_the_blocker_names_the_stale_token_reference(self):
        reasons = execution_blockers(
            {"asset": "XIAOMI", "perp_venue": "okx",
             "perp_instrument": "XIAOMI-USDT-SWAP", "mapping_verified": True,
             "cost_verified": True, "reference_stale": True,
             "reference_age_ms": 640_000},
            {"allowlist": auto_policy()["allowlist"]})
        self.assertTrue(any("代币引用已过期" in reason for reason in reasons), reasons)

    def test_a_fresh_reference_raises_no_such_blocker(self):
        reasons = execution_blockers(
            {"asset": "XIAOMI", "perp_venue": "okx",
             "perp_instrument": "XIAOMI-USDT-SWAP", "mapping_verified": True,
             "cost_verified": True, "reference_stale": False},
            {"allowlist": auto_policy()["allowlist"]})
        self.assertEqual(reasons, [])

    def test_the_unattended_executor_refuses_to_sign_a_stale_reference(self):
        executor = AutoExecutor(policy=dict(auto_policy()),
                                monitor_config={"tokens": []})
        ok, reason = executor.trigger(auto_candidate(reference_stale=True),
                                      ts_ms=1_789_520_000_000)
        self.assertFalse(ok)
        self.assertEqual(reason, "token_reference_stale")
        # The same candidate without the flag still clears the entry bar, so the
        # new check is exactly the new fact and not a broader refusal.
        ok, reason = executor.trigger(auto_candidate(),
                                      ts_ms=1_789_520_000_000)
        self.assertTrue(ok, reason)


if __name__ == "__main__":
    unittest.main()
