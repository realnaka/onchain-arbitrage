"""In-flight visibility: the lock says *what* is running, not just "someone".

Measured 2026-10-07: a real 25-minute bridge and a dead process looked exactly
the same from outside (a lock file with a pid in it), the board could only say
"lock", and the question "who deleted the lock an hour ago" had no answer.
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from arb_monitor.xstocks_v2 import cycle, executor as ex, ledger

NOW = 1_800_000_000_000


class LockSidecarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.lock = self.tmp / "xstocks_v2_cycle.lock"
        self.halt = self.tmp / "HALT"

    def test_the_lock_keeps_its_old_format_and_writes_a_sidecar(self):
        with cycle.CycleLock(self.lock, self.halt, label="SPYx family5",
                             symbol="SPYx", family="chain_via_bridge_to_kraken"):
            # Scripts read the bare pid; that has to keep working.
            self.assertEqual(int(self.lock.read_text().strip()), os.getpid())
            payload = json.loads((self.tmp / "xstocks_v2_cycle.lock.json").read_text())
            self.assertEqual(payload["pid"], os.getpid())
            self.assertEqual(payload["symbol"], "SPYx")
            self.assertEqual(payload["step"], "starting")
        self.assertFalse(self.lock.exists())
        self.assertFalse((self.tmp / "xstocks_v2_cycle.lock.json").exists())

    def test_update_reports_the_step_that_is_running(self):
        with cycle.CycleLock(self.lock, self.halt, symbol="SPYx") as held:
            held.update(step="bridge", note="waiting for Solana")
            payload = json.loads((self.tmp / "xstocks_v2_cycle.lock.json").read_text())
            self.assertEqual(payload["step"], "bridge")
            self.assertEqual(payload["note"], "waiting for Solana")

    def test_a_held_lock_keeps_the_sidecar_so_a_human_can_read_it(self):
        with cycle.CycleLock(self.lock, self.halt, symbol="SPYx") as held:
            held.update(step="deposit")
            held.hold = True
        self.assertTrue(self.halt.exists())
        self.assertTrue(self.lock.exists())
        self.assertTrue((self.tmp / "xstocks_v2_cycle.lock.json").exists())

    def test_a_second_lock_is_still_refused(self):
        with cycle.CycleLock(self.lock, self.halt):
            with self.assertRaises(cycle.CycleBusy):
                with cycle.CycleLock(self.lock, self.halt):
                    pass


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.lock = self.tmp / "xstocks_v2_cycle.lock"

    def test_no_lock_is_free(self):
        self.assertEqual(cycle.status(self.lock, now_ms=NOW)["state"], "free")

    def test_a_live_pid_is_running_with_its_step(self):
        self.lock.write_text(str(os.getpid()))
        (Path(str(self.lock) + ".json")).write_text(json.dumps(
            {"pid": os.getpid(), "symbol": "SPYx", "family": "f", "step": "bridge",
             "started_ms": NOW - 12 * 60 * 1000, "label": "SPYx f"}))
        got = cycle.status(self.lock, now_ms=NOW)
        self.assertEqual(got["state"], "running")
        self.assertEqual(got["step"], "bridge")
        self.assertEqual(got["age_s"], 720.0)

    def test_a_dead_pid_is_stale(self):
        self.lock.write_text("999999")            # no such process here
        self.assertEqual(cycle.status(self.lock, now_ms=NOW)["state"], "stale")

    def test_an_old_lock_without_a_sidecar_still_answers(self):
        """Every lock written before today has only the pid."""
        self.lock.write_text(str(os.getpid()))
        got = cycle.status(self.lock, now_ms=NOW)
        self.assertEqual(got["state"], "running")
        self.assertEqual(got["step"], "")
        self.assertIsNone(got["age_s"])

    def test_a_corrupt_pid_is_not_a_crash(self):
        self.lock.write_text("{not a pid")
        self.assertEqual(cycle.status(self.lock, now_ms=NOW)["state"], "stale")


class ProgressHookTest(unittest.TestCase):
    """The executor tells the lock which leg is running."""

    def test_every_step_is_reported_in_order(self):
        seen: list[str] = []
        steps = [ex.Step("buy", lambda: ex.Outcome(ex.OK), broadcasts=True,
                         inventory=True, held_at="wallet"),
                 ex.Step("bridge", lambda: ex.Outcome(ex.OK), broadcasts=True,
                         inventory=True, held_at="in_bridge")]
        machine = ex.Executor(symbol="SPYx", chain="x-layer", family="f",
                              mode="live", size_usdt=D("1"), tokens=D("0.1"),
                              steps=steps, gate=lambda: (True, "ok"),
                              ledger_path=Path(tempfile.mkdtemp()) / "c.jsonl",
                              progress=lambda **kw: seen.append(kw["step"]))
        machine.run()
        self.assertEqual(seen, ["buy", "bridge"])

    def test_a_progress_writer_that_raises_does_not_stop_the_cycle(self):
        def boom(**kwargs):
            raise OSError("the sidecar is on a read-only disk")

        steps = [ex.Step("sell", lambda: ex.Outcome(ex.OK), broadcasts=True)]
        machine = ex.Executor(symbol="SPYx", chain="x-layer", family="f",
                              mode="live", size_usdt=D("1"), tokens=D("0.1"),
                              steps=steps, gate=lambda: (True, "ok"),
                              ledger_path=Path(tempfile.mkdtemp()) / "c.jsonl",
                              progress=boom)
        self.assertEqual(machine.run()["state"], ledger.SOLD)


if __name__ == "__main__":
    unittest.main()
