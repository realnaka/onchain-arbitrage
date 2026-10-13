"""An unverifiable account must not look like a good one.

Measured 2026-09-16: `account-status --json` printed `okx.status="error"`
(`okx_transport:URLError`) and `wallet.status="error"` and still exited 0, so a
cron, a health check or the unattended executor could not tell "the account is
readable and flat" from "nothing could be read". These tests pin the verdict,
the exit code and the executor's refusal to act on it.
"""
from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

import arb_monitor.apps.account_dashboard as dashboard
import arb_monitor.apps.cli as cli
from arb_monitor.execution.auto_execute import AutoExecutor
from arb_monitor.execution.ledger import ExecutionLedger

from tests.test_auto_execute import FakePlan, FakeStack, Recorder, candidate, policy


def snapshot(**sources) -> dict:
    row = {
        "okx": {"status": "ok", "fetched_at_ms": 1},
        "wallet": {"status": "ok", "fetched_at_ms": 1},
        "gas": {"status": "sponsored", "fetched_at_ms": 1},
        "ledger": {"status": "ok", "fetched_at_ms": 1},
    }
    for name, value in sources.items():
        row[name] = value
    return row


class VerificationTests(unittest.TestCase):
    def test_a_healthy_snapshot_is_verified(self):
        verdict = dashboard.verification(snapshot())
        self.assertTrue(verdict["verified"])
        self.assertEqual(verdict["unverified_sources"], [])

    def test_every_required_source_can_withhold_verification(self):
        for name, status in (("okx", "error"), ("wallet", "error"),
                             ("gas", "unknown"), ("ledger", "empty")):
            with self.subTest(source=name, status=status):
                verdict = dashboard.verification(snapshot(**{name: {"status": status}}))
                self.assertFalse(verdict["verified"])
                self.assertIn(name, verdict["unverified_sources"])

    def test_a_missing_source_is_not_verified(self):
        verdict = dashboard.verification({"okx": {"status": "ok"}})
        self.assertFalse(verdict["verified"])
        self.assertIn("wallet", verdict["unverified_sources"])

    def test_the_cli_exits_non_zero_when_unverified(self):
        original = dashboard.build_snapshot
        try:
            dashboard.build_snapshot = lambda **kwargs: snapshot(
                okx={"status": "error", "error": "okx_transport:URLError"})
            self.assertEqual(cli.main(["account-status", "--json"]), 1)
            dashboard.build_snapshot = lambda **kwargs: snapshot()
            self.assertEqual(cli.main(["account-status", "--json"]), 0)
        finally:
            dashboard.build_snapshot = original

    def test_the_json_always_carries_the_verdict(self):
        original = dashboard.build_snapshot
        try:
            dashboard.build_snapshot = lambda **kwargs: snapshot()
            import contextlib
            import io
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                cli.main(["account-status", "--json"])
            payload = json.loads(buffer.getvalue())
            self.assertIn("verified", payload)
            self.assertIn("source_checks", payload)
        finally:
            dashboard.build_snapshot = original


class ExecutorAccountGateTests(unittest.TestCase):
    NOW = 1_789_520_000_000

    def executor(self, tmp: str, verifier):
        ledger_path = str(Path(tmp) / "execution.db")
        ledger = ExecutionLedger(ledger_path)
        stack = FakeStack(FakePlan(quote_at_s=self.NOW / 1000 - 1), ledger)
        return AutoExecutor(
            policy=policy(mode="preauthorized_program", auto_orders="paper"),
            monitor_config={}, ledger_path=ledger_path, notifier=Recorder(),
            stack_factory=lambda **kwargs: stack,
            account_verifier=verifier,
            stop_file=str(Path(tmp) / "AUTO_HALT"),
            lock_file=str(Path(tmp) / "auto.lock"), clock=lambda: self.NOW)

    def test_an_unverified_account_blocks_the_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = self.executor(tmp, lambda: {"verified": False,
                                                   "okx": {"status": "error"}})
            record = executor.maybe_execute([candidate()], self.NOW)
            attempt = record["attempts"][0]
            self.assertEqual(attempt["state"], "account_not_verified")
            self.assertFalse(attempt["executed"])
            # Nothing was sent, so nothing halts the world either.
            self.assertFalse(attempt["halt"])
            self.assertFalse(Path(executor.stop_file).exists())

    def test_an_errored_verifier_is_unverified_too(self):
        def boom():
            raise RuntimeError("okx_transport:URLError")

        with tempfile.TemporaryDirectory() as tmp:
            executor = self.executor(tmp, boom)
            record = executor.maybe_execute([candidate()], self.NOW)
            self.assertEqual(record["attempts"][0]["state"], "account_not_verified")

    def test_a_verified_account_still_runs_paper(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = self.executor(tmp, lambda: {"verified": True})
            record = executor.maybe_execute([candidate()], self.NOW)
            self.assertEqual(record["attempts"][0]["state"], "paper_hedged")


class HaltDurabilityTests(unittest.TestCase):
    """If the halt file cannot be written, the stop must survive anyway."""

    NOW = 1_789_520_000_000

    def test_a_halt_that_cannot_be_written_is_still_in_the_ledger(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = str(Path(tmp) / "execution.db")
            ledger = ExecutionLedger(ledger_path)
            # A group that reached "spot_filled" and then stopped: the halt this
            # test wants to record.
            ledger.append("xiaomi-crashed", "spot_filled", "spot_filled",
                          {"leg": "buy", "quote_amount": "10.05"}, self.NOW - 5_000)
            stack = FakeStack(FakePlan(quote_at_s=self.NOW / 1000 - 1), ledger)
            halt_path = Path(tmp) / "AUTO_HALT"
            executor = AutoExecutor(
                policy=policy(mode="preauthorized_program", auto_orders="paper"),
                monitor_config={}, ledger_path=ledger_path, notifier=Recorder(),
                stack_factory=lambda **kwargs: stack,
                stop_file=str(halt_path), lock_file=str(Path(tmp) / "auto.lock"),
                clock=lambda: self.NOW)
            # The filesystem refuses the halt file. The previous code swallowed
            # this OSError, which would have let the next cycle try again.
            with mock.patch.object(Path, "write_text",
                                   side_effect=OSError("read-only filesystem")):
                record = executor.maybe_execute([candidate()], self.NOW)
            self.assertEqual(record["attempts"][0]["state"], "unresolved_exposure_groups")
            self.assertTrue(record["attempts"][0]["halt"])
            self.assertFalse(halt_path.exists())
            events = [row["event_type"]
                      for row in ledger.events("auto-xiaomi")]
            self.assertIn("auto_halt", events)
            # And the next cycle still refuses: the stop does not depend on the
            # file existing, because the unresolved group is still there.
            again = executor.maybe_execute([candidate()], self.NOW + 60_000)
            self.assertEqual(again["attempts"][0]["state"],
                             "unresolved_exposure_groups")
            ledger.close()


if __name__ == "__main__":
    unittest.main()
