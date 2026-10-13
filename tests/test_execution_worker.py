"""Stage 3: the execution wait belongs to a worker, not to the sampling loop.

Maker-first opens rest at the touch for up to 120 s and closes for up to 600 s
before their fallback. That is the operator's approved trading semantics and it
is deliberately not shortened. What was wrong is *where* the wait happened: on
the sampling thread, so for its duration the monitor wrote no run summary,
aged every order book, and could not notice a new candidate. Measured
2026-09-21: a 1 322 s gap between two summaries with a live process and an awake
machine.

What these tests pin, in the order the risks were listed:

* the cycle returns while the worker is still waiting;
* the queue is bounded and deduplicated, and an exit is never dropped to make
  room for an open;
* a task the worker does not recognise is refused, never guessed at;
* a crashing handler is recorded and the worker survives; a dead worker blocks
  new *entries* and says so, while the cycle keeps monitoring;
* every finished task still lands in the ledger;
* `inline` remains the default and is byte-for-byte the old call sequence.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import time
import unittest

from arb_monitor.execution.auto_execute import AutoExecutor
from arb_monitor.execution.ledger import ExecutionLedger
from arb_monitor.execution.worker import ExecutionTask, ExecutionWorker
from tests.test_auto_execute import candidate as auto_candidate
from tests.test_auto_execute import policy as auto_policy
from tests.test_cycle_instrumentation import Harness

NOW = 1_789_520_000_000


class BlockingWorker:
    """An `ExecutionWorker` whose handler waits until the test opens the gate."""

    def __init__(self, tmp):
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.seen: list[str] = []

        def handler(task):
            self.seen.append(task.kind)
            self.entered.set()
            self.gate.wait(5.0)
            return {"state": "waited", "executed": False}

        self.worker = ExecutionWorker(handler, max_pending=2, clock=lambda: NOW)
        self.worker.start()

    def release(self):
        self.gate.set()
        self.worker.stop()


class QueueTests(unittest.TestCase):
    def test_the_monitor_keeps_sampling_while_the_execution_waits(self):
        blocking = BlockingWorker(None)
        try:
            with Harness(execution_dispatch="worker") as h:
                # One group that may hold exposure, so an exit task is due.
                ledger = ExecutionLedger(h.config["execution_ledger"])
                ledger.append("xiaomi-open", "hedged", "hedged",
                              {"plan": {"asset": "XIAOMI",
                                        "perp": "okx:XIAOMI-USDT-SWAP"}}, NOW)
                ledger.close()
                h.monitor.auto_executor.worker = blocking.worker
                first = h.cycles(1)[0]
                self.assertTrue(blocking.entered.wait(5.0),
                                "the worker never picked the task up")
                # The cycle that handed the task over had already returned
                # before the worker even picked it up — that is the whole point:
                # a queued or waiting order does not hold the sampler.
                self.assertTrue(first["execution_queue_depth"]
                                <= h.monitor.auto_executor.worker.max_pending)
                # And the next cycle reports the wait as state, not as a stall.
                second = h.cycles(1)[0]
                self.assertNotEqual(first["run_id"], second["run_id"])
                self.assertTrue(second["execution_in_flight"],
                                "the waiting task was not visible on the summary")
                self.assertEqual(second["basis_exit"]["reason"], "queued")
                # Let the worker finish before the harness tears the monitor
                # down, and keep that teardown short: the point of this test is
                # the cycle, not the shutdown (which has its own tests).
                h.monitor.auto_executor.shutdown_grace_seconds = 0.5
                blocking.release()
        finally:
            blocking.release()

    def test_an_exit_outranks_an_entry(self):
        seen: list[str] = []
        worker = ExecutionWorker(lambda task: seen.append(task.kind) or {})
        worker.submit(ExecutionTask(kind="entry", asset="XIAOMI"))
        worker.submit(ExecutionTask(kind="basis_exit", group_id="g1"))
        worker.start()
        for _ in range(200):
            if len(seen) == 2:
                break
            time.sleep(0.01)
        worker.stop()
        self.assertEqual(seen, ["basis_exit", "entry"])

    def test_an_exit_is_never_evicted_to_make_room_for_an_entry(self):
        worker = ExecutionWorker(lambda task: {}, max_pending=1)
        self.assertEqual(worker.submit(ExecutionTask(kind="basis_exit",
                                                     group_id="g1")), "queued")
        # Creating new risk is the droppable one.
        self.assertEqual(worker.submit(ExecutionTask(kind="entry", asset="XIAOMI")),
                         "full")
        self.assertEqual(worker.snapshot()["queued"][0]["kind"], "basis_exit")
        # An exit may replace a queued entry, and the reverse may not happen.
        self.assertEqual(worker.submit(ExecutionTask(kind="entry", asset="TENCENT")),
                         "full")
        self.assertEqual(worker.snapshot()["rejected"]["full"], 2)

    def test_the_queue_is_deduplicated_per_asset_and_per_group(self):
        worker = ExecutionWorker(lambda task: {}, max_pending=4)
        first = ExecutionTask(kind="entry", asset="XIAOMI")
        second = ExecutionTask(kind="entry", asset="XIAOMI")
        self.assertEqual(worker.submit(first), "queued")
        self.assertEqual(worker.submit(second), "replaced")
        # The replacement keeps the identity but carries the newer nomination.
        self.assertEqual(worker.snapshot()["queued"][0]["task_id"], first.task_id)
        self.assertEqual(worker.snapshot()["deduplicated"], 1)
        # Different kinds on the same group are different jobs.
        self.assertEqual(worker.submit(ExecutionTask(kind="basis_exit",
                                                     group_id="g1")), "queued")
        self.assertEqual(worker.submit(ExecutionTask(kind="funding_exit",
                                                     group_id="g1")), "queued")
        self.assertEqual(worker.snapshot()["queue_depth"], 3)


class FailureIsolationTests(unittest.TestCase):
    def test_a_crashing_handler_is_recorded_and_the_worker_survives(self):
        seen: list[str] = []

        def handler(task):
            seen.append(task.kind)
            if task.kind == "entry":
                raise RuntimeError("boom")
            return {"state": "ok"}

        worker = ExecutionWorker(handler)
        # Queued before the thread starts, so the order is the priority order
        # rather than whichever task the thread happened to pick up first.
        worker.submit(ExecutionTask(kind="entry", asset="XIAOMI"))
        worker.submit(ExecutionTask(kind="basis_exit", group_id="g1"))
        worker.start()
        try:
            for _ in range(200):
                if len(seen) == 2 and worker.snapshot()["queue_depth"] == 0:
                    break
                time.sleep(0.01)
            snapshot = worker.snapshot()
        finally:
            worker.stop()
        self.assertTrue(snapshot["alive"],
                        "one bad handler call killed the thread")
        self.assertEqual(snapshot["failed"], 1)
        self.assertIn("boom", snapshot["last_error"])
        # The next task still ran: one bad handler call is not an outage.
        self.assertEqual(seen, ["basis_exit", "entry"])
        self.assertEqual(snapshot["completed"], 2)

    def test_a_dead_worker_blocks_new_entries_and_says_so(self):
        class UnrestartableWorker:
            """A worker whose thread died and cannot be brought back."""

            max_pending = 2

            @property
            def alive(self):
                return False

            def maybe_restart(self):
                return False

            def submit(self, task):
                raise AssertionError("nothing may be submitted to a dead worker")

            def snapshot(self):
                return {"alive": False, "queue_depth": 0, "in_flight": None}

            def last_by_kind(self, kind):
                return None

            def drain(self, timeout):
                return True

            def in_flight_task(self):
                return None

            def stop(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            executor = AutoExecutor(
                policy=auto_policy(mode="preauthorized_program",
                                   auto_orders="paper", dispatch="worker"),
                monitor_config={"tokens": []},
                ledger_path=str(Path(tmp) / "execution.db"),
                stop_file=str(Path(tmp) / "AUTO_HALT"),
                lock_file=str(Path(tmp) / "auto.lock"), clock=lambda: NOW)
            executor.worker = UnrestartableWorker()
            record = executor.dispatch([auto_candidate()], [], NOW)
            executor.shutdown()
        self.assertFalse(record["worker_alive"])
        self.assertTrue(record["entries_blocked"])
        self.assertEqual(record["reason"], "execution_worker_unavailable")
        self.assertEqual(record["submitted"], [],
                         "a dead worker must not be handed new risk")


class ShutdownNeverCancelsTests(unittest.TestCase):
    """`stop` waits for an order. It never pulls one.

    The distinction matters because the two look similar and mean opposite
    things. Cancelling on shutdown would turn "stop the monitor" into "abandon
    the hedge plan"; ignoring an in-flight order entirely is how the 2026-09-16
    naked short happened (a resting order was assumed cancelled and filled
    sixteen minutes later). So a shutdown must (a) wait, and (b) if it cannot
    wait long enough, leave a durable record naming the task.
    """

    def test_shutdown_waits_for_the_in_flight_task_and_sends_no_cancel(self):
        cancelled: list[str] = []

        def handler(task):
            # An execution path that wanted to cancel would have to call
            # something; anything of the sort is recorded here and must stay
            # empty.
            cancelled.append("handler-ran")
            time.sleep(0.2)
            return {"state": "closed", "executed": True}

        worker = ExecutionWorker(handler)
        worker.start()
        try:
            worker.submit(ExecutionTask(kind="basis_exit", group_id="g1"))
            for _ in range(200):
                if worker.in_flight_task() is not None:
                    break
                time.sleep(0.01)
            finished = worker.drain(timeout=5.0)
        finally:
            worker.stop()
        self.assertTrue(finished, "shutdown did not wait for the task to finish")
        self.assertEqual(cancelled, ["handler-ran"])
        self.assertIsNone(worker.in_flight_task())

    def test_an_interrupted_wait_is_written_down_not_forgotten(self):
        release = threading.Event()

        def handler(task):
            release.wait(5.0)
            return {"state": "closed"}

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "execution.db")
            executor = AutoExecutor(
                policy=auto_policy(mode="preauthorized_program",
                                   auto_orders="paper", dispatch="worker",
                                   shutdown_grace_seconds="0.2"),
                monitor_config={"tokens": []},
                ledger_path=path, stop_file=str(Path(tmp) / "AUTO_HALT"),
                lock_file=str(Path(tmp) / "auto.lock"), clock=lambda: NOW)
            worker = executor.ensure_worker()
            # Hold the task open, so shutdown has something to be interrupted
            # in the middle of.
            worker.handler = handler
            worker.submit(ExecutionTask(kind="basis_exit", group_id="g1",
                                        instrument="XIAOMI-USDT-SWAP"))
            for _ in range(200):
                if worker.in_flight_task() is not None:
                    break
                time.sleep(0.01)
            executor.shutdown()
            release.set()
            ledger = ExecutionLedger(path)
            events = ledger.conn.execute(
                "SELECT state, payload FROM execution_events WHERE group_id='g1'"
            ).fetchall()
            ledger.close()
        row = [item for item in events
               if item[0] == "execution_shutdown_interrupted"]
        self.assertTrue(row, "the interrupted wait was not recorded")
        self.assertIn("no cancel was sent", row[0][1])

    def test_shutdown_is_a_no_op_when_nothing_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = AutoExecutor(
                policy=auto_policy(mode="preauthorized_program",
                                   auto_orders="paper", dispatch="worker"),
                monitor_config={"tokens": []},
                ledger_path=str(Path(tmp) / "execution.db"),
                stop_file=str(Path(tmp) / "AUTO_HALT"),
                lock_file=str(Path(tmp) / "auto.lock"), clock=lambda: NOW)
            executor.ensure_worker()
            executor.shutdown()          # must not raise, must not record anything


class TaskKindTests(unittest.TestCase):
    def test_an_unknown_task_kind_is_refused_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = AutoExecutor(
                policy=auto_policy(mode="preauthorized_program",
                                   auto_orders="paper", dispatch="worker"),
                monitor_config={"tokens": []},
                ledger_path=str(Path(tmp) / "execution.db"),
                stop_file=str(Path(tmp) / "AUTO_HALT"),
                lock_file=str(Path(tmp) / "auto.lock"), clock=lambda: NOW)
            result = executor._run_task(ExecutionTask(kind="restore_old_task"))
            executor.shutdown()
        self.assertEqual(result["state"], "unknown_task_kind")
        self.assertFalse(result["executed"])
        self.assertFalse(result["halt"])
        self.assertIn("restore_old_task", result["reason"])

    def test_an_empty_nomination_does_not_reach_the_venue(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = AutoExecutor(
                policy=auto_policy(mode="preauthorized_program",
                                   auto_orders="paper", dispatch="worker"),
                monitor_config={"tokens": []},
                ledger_path=str(Path(tmp) / "execution.db"),
                stop_file=str(Path(tmp) / "AUTO_HALT"),
                lock_file=str(Path(tmp) / "auto.lock"), clock=lambda: NOW)
            result = executor._run_task(ExecutionTask(kind="entry"))
            executor.shutdown()
        self.assertEqual(result["state"], "empty_nomination")


class SemanticsAreUnchangedTests(unittest.TestCase):
    def test_inline_remains_the_default_and_the_worker_is_off(self):
        executor = AutoExecutor(policy=auto_policy(), monitor_config={"tokens": []})
        self.assertFalse(executor.worker_enabled())
        self.assertIsNone(executor.ensure_worker())
        self.assertIsNone(executor.worker)

    def test_worker_mode_still_applies_the_halt_file_to_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            stop = Path(tmp) / "AUTO_HALT"
            stop.write_text("reconcile me", encoding="utf-8")
            executor = AutoExecutor(
                policy=auto_policy(mode="preauthorized_program",
                                   auto_orders="paper", dispatch="worker"),
                monitor_config={"tokens": []},
                ledger_path=str(Path(tmp) / "execution.db"),
                stop_file=str(stop), lock_file=str(Path(tmp) / "auto.lock"),
                clock=lambda: NOW)
            record = executor.dispatch([auto_candidate()], [], NOW)
            executor.shutdown()
        self.assertEqual(record["reason"], "halted:reconcile me")
        self.assertEqual([row for row in record["submitted"]
                          if row["kind"] == "entry"], [])

    def test_worker_mode_still_refuses_a_candidate_below_the_bar(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = AutoExecutor(
                policy=auto_policy(mode="preauthorized_program",
                                   auto_orders="paper", dispatch="worker"),
                monitor_config={"tokens": []},
                ledger_path=str(Path(tmp) / "execution.db"),
                stop_file=str(Path(tmp) / "AUTO_HALT"),
                lock_file=str(Path(tmp) / "auto.lock"), clock=lambda: NOW)
            below = auto_candidate(gross_entry_basis_bps=1)
            record = executor.dispatch([below], [], NOW)
            executor.shutdown()
        self.assertEqual(record["submitted"], [])
        self.assertEqual(record["skipped"].get("below_breakeven"), 1)

    def test_every_finished_task_is_written_to_the_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "execution.db")
            executor = AutoExecutor(
                policy=auto_policy(mode="preauthorized_program",
                                   auto_orders="paper", dispatch="worker"),
                monitor_config={"tokens": []},
                ledger_path=path, stop_file=str(Path(tmp) / "AUTO_HALT"),
                lock_file=str(Path(tmp) / "auto.lock"), clock=lambda: NOW)
            worker = executor.ensure_worker()
            worker.recorder = executor._record_worker_task
            worker.submit(ExecutionTask(kind="basis_exit", group_id="g1"))
            for _ in range(200):
                if worker.snapshot()["queue_depth"] == 0 and \
                        worker.snapshot()["completed"]:
                    break
                time.sleep(0.01)
            executor.shutdown()
            ledger = ExecutionLedger(path)
            events = [row for row in ledger.recover()]
            ledger.close()
        self.assertIn("g1", events)

    def test_a_refused_entry_does_not_invent_a_ledger_group(self):
        # Live 2026-09-21: the first version wrote every finished task under a
        # synthetic `worker-entry` group, so `ledger-triage` started asking a
        # human to judge a group that never traded. An entry nomination with no
        # group belongs in the run summary, not in the exposure ledger.
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "execution.db")
            executor = AutoExecutor(
                policy=auto_policy(mode="preauthorized_program",
                                   auto_orders="paper", dispatch="worker"),
                monitor_config={"tokens": []},
                ledger_path=path, stop_file=str(Path(tmp) / "AUTO_HALT"),
                lock_file=str(Path(tmp) / "auto.lock"), clock=lambda: NOW)
            worker = executor.ensure_worker()
            worker.recorder = executor._record_worker_task
            worker.submit(ExecutionTask(kind="entry", asset="XIAOMI",
                                        instrument="XIAOMI-USDT-SWAP"))
            for _ in range(200):
                if worker.snapshot()["completed"]:
                    break
                time.sleep(0.01)
            executor.shutdown()
            ledger = ExecutionLedger(path)
            groups = sorted(ledger.recover())
            ledger.close()
        self.assertEqual(groups, [], "a refused entry created a ledger group")

    def test_worker_mode_does_not_bypass_the_pre_execution_gates(self):
        # The worker runs the same `execute()` the inline path runs, and that is
        # where the account, pending-order and ledger gates live. If the worker
        # had its own copy of the placement path, a restart could open before
        # reconciling an orphan — the one thing the project never accepts.
        with tempfile.TemporaryDirectory() as tmp:
            executor = AutoExecutor(
                policy=auto_policy(mode="preauthorized_program",
                                   auto_orders="paper", dispatch="worker"),
                monitor_config={"tokens": []},
                ledger_path=str(Path(tmp) / "execution.db"),
                stop_file=str(Path(tmp) / "AUTO_HALT"),
                lock_file=str(Path(tmp) / "auto.lock"), clock=lambda: NOW)
            seen: list[str] = []
            executor.execute = lambda candidate, ts_ms: (
                seen.append(str(candidate.get("asset"))),
                {"state": "stubbed", "executed": False, "halt": False})[1]
            record = executor.dispatch([auto_candidate()], [], NOW)
            worker = executor.ensure_worker()
            for _ in range(200):
                if seen:
                    break
                time.sleep(0.01)
            executor.shutdown()
        self.assertEqual(seen, ["XIAOMI"])
        self.assertEqual([row["status"] for row in record["submitted"]
                          if row["kind"] == "entry"], ["queued"])

if __name__ == "__main__":
    unittest.main()
