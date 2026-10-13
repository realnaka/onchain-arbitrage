"""The execution worker: the monitor nominates, this thread acts.

Why this exists. Maker-first execution waits by design — an open rests at the
touch for up to ``maker_timeout_seconds`` (120 s) and a close for up to
``maker_close_timeout_seconds`` (600 s) before its fallback. That is the
operator's approved trading semantics and it is not to be shortened. But those
waits were happening on the **sampling thread**, so for their duration the
monitor wrote no run summary, aged every order book, and could not notice a new
candidate. Measured 2026-09-21: a 1 322 s (22-minute) gap between two summaries
with a live process and an awake machine, and no way to attribute it because the
summary recorded only its own end time.

What this does *not* change: the same functions run, with the same gates, the
same risk checks, the same freshness limits and the same unknown-write rules.
The only things that change are which thread runs them and when the monitor asks.

Design rules, each one tied to a risk it removes:

* **one worker, serial** — the execution lock is the authority, and a second
  concurrent actor is not made safe by adding a thread;
* **bounded queue** — the monitor cannot pile work up behind a slow order;
* **exits outrank entries**, including at the queue boundary: when the queue is
  full it is the lowest-priority *entry* that is dropped, never an exit;
* **deduplicated by key** — one pending task per asset (entry) or per group and
  reason (exit); a newer nomination replaces a queued one instead of adding a
  second;
* **a crash is a state, not an outage** — an exception is recorded on the task
  and the thread keeps going; if the thread itself dies the monitor is told
  (`alive` is False) and automation stops opening until it is healthy again.

Nothing here decides *whether* a trade is allowed. That stays in
``auto_execute.AutoExecutor``; this module only carries the request.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
import uuid

from arb_monitor.data.hk_cex import now_ms

#: Order of business. Lower runs first. A close is not "the same size of job" as
#: an open: one reduces risk that already exists, the other creates it.
PRIORITY = {
    "spot_recovery": 0,   # a stranded leg: the only exit that cannot wait
    "funding_exit": 1,    # a cost stop the operator defined as unconditional
    "basis_exit": 2,      # the operator's 3 bps target
    "entry": 3,
}


@dataclass
class ExecutionTask:
    """One unit of work for the worker.

    ``payload`` carries the *nomination*, never a price to execute at: an entry
    is re-quoted from the live book before anything is signed, and a close
    re-checks the operator's rule at execution time.
    """

    kind: str
    payload: dict = field(default_factory=dict)
    asset: str = ""
    instrument: str | None = None
    group_id: str | None = None
    enqueued_ms: int = 0
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    state: str = "queued"
    started_ms: int | None = None
    finished_ms: int | None = None
    result: dict | None = None
    error: str | None = None

    @property
    def priority(self) -> int:
        return PRIORITY.get(self.kind, 9)

    @property
    def dedupe_key(self) -> tuple:
        """What "the same task" means.

        An open is per asset — two entries for one asset cannot both be right.
        An exit is per group *and reason*: a stranded spot leg and a basis target
        on the same group are different jobs, and neither may hide the other.
        """
        if self.kind == "entry":
            return (self.kind, self.asset)
        return (self.kind, self.group_id or self.asset)

    def as_event(self) -> dict:
        return {"task_id": self.task_id, "kind": self.kind, "asset": self.asset,
                "instrument": self.instrument, "group_id": self.group_id,
                "state": self.state, "priority": self.priority,
                "enqueued_ms": self.enqueued_ms, "started_ms": self.started_ms,
                "finished_ms": self.finished_ms, "error": self.error,
                "elapsed_ms": (None if self.started_ms is None else
                               int((self.finished_ms or now_ms()) - self.started_ms))}


class ExecutionWorker:
    """A single serial worker over a bounded, priority-ordered queue."""

    def __init__(self, handler, *, max_pending: int = 2, clock=now_ms,
                 name: str = "execution-worker", recorder=None):
        self.handler = handler
        # Called with each finished task so the monitor can write the execution
        # event; a failure there must not stop the worker.
        self.recorder = recorder
        self.max_pending = max(1, int(max_pending))
        self.clock = clock
        self.name = name
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._queue: list[ExecutionTask] = []
        self._inflight: ExecutionTask | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._heartbeat_ms = 0
        self._last_result: dict | None = None
        self._last_by_kind: dict[str, dict] = {}
        self._last_error: str | None = None
        self._restarts = 0
        self._completed = 0
        self._failed = 0
        self._rejected: dict[str, int] = {}
        self._deduped = 0

    # --- lifecycle --------------------------------------------------------
    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name=self.name,
                                            daemon=True)
            self._thread.start()
        return True

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        with self._wake:
            self._wake.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def drain(self, timeout: float) -> bool:
        """Wait for the queue and the in-flight task to empty. Returns success.

        Used at shutdown, and *only* to wait. It sends nothing, cancels nothing
        and touches no order: an order that has been placed stays placed, which
        is the whole point of the rule this project learned the hard way on
        2026-09-16 (a resting order that was assumed cancelled filled sixteen
        minutes later and left a naked short).
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._wake:
            while (self._queue or self._inflight is not None) and \
                    time.monotonic() < deadline:
                self._wake.wait(min(0.5, max(0.0, deadline - time.monotonic())))
            return not self._queue and self._inflight is None

    def in_flight_task(self) -> ExecutionTask | None:
        with self._lock:
            return self._inflight

    def maybe_restart(self) -> bool:
        """Bring the thread back if it died. The monitor calls this per cycle."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
        self._restarts += 1
        self.start()
        return True

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # --- queue ------------------------------------------------------------
    def submit(self, task: ExecutionTask) -> str:
        """``queued`` / ``replaced`` / ``duplicate`` / ``full`` / ``stopped``."""
        task.enqueued_ms = self.clock()
        with self._wake:
            if self._stop.is_set():
                return "stopped"
            if self._inflight is not None and self._inflight.dedupe_key == task.dedupe_key:
                self._deduped += 1
                return "duplicate"
            for index, queued in enumerate(self._queue):
                if queued.dedupe_key != task.dedupe_key:
                    continue
                # The newer nomination carries fresher inputs; the queued one has
                # not started, so replacing it loses no in-flight work.
                task.task_id = queued.task_id
                self._queue[index] = task
                self._deduped += 1
                self._sort_locked()
                self._wake.notify_all()
                return "replaced"
            if len(self._queue) >= self.max_pending:
                # Exits outrank entries here too. Creating new risk is the
                # droppable one.
                victim = max(self._queue, key=lambda item: (item.priority,
                                                            item.enqueued_ms))
                if victim.priority <= task.priority:
                    self._rejected["full"] = self._rejected.get("full", 0) + 1
                    return "full"
                self._queue.remove(victim)
                self._rejected["evicted_entry"] = (
                    self._rejected.get("evicted_entry", 0) + 1)
            self._queue.append(task)
            self._sort_locked()
            self._wake.notify_all()
            return "queued"

    def _sort_locked(self) -> None:
        self._queue.sort(key=lambda item: (item.priority, item.enqueued_ms))

    # --- the thread -------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._wake:
                while not self._queue and not self._stop.is_set():
                    self._wake.wait(1.0)
                if self._stop.is_set():
                    return
                task = self._queue.pop(0)
                task.state = "started"
                task.started_ms = self.clock()
                self._inflight = task
                self._heartbeat_ms = self.clock()
            try:
                result = self.handler(task)
                task.state = "completed"
                task.result = result if isinstance(result, dict) else {"value": result}
            except (KeyboardInterrupt, SystemExit):
                with self._wake:
                    self._inflight = None
                    task.state = "interrupted"
                raise
            except BaseException as exc:  # noqa: BLE001 - the thread must survive
                # An exception is recorded on the task, never allowed to take
                # the worker (and therefore the automation) down with it. The
                # handler itself is responsible for writing AUTO_HALT when a
                # write may have happened — that rule is unchanged and lives in
                # `auto_execute`.
                task.state = "error"
                task.error = f"{type(exc).__name__}:{str(exc)[:200]}"
                with self._wake:
                    self._last_error = task.error
            with self._wake:
                task.finished_ms = self.clock()
                self._inflight = None
                self._heartbeat_ms = self.clock()
                self._completed += 1
                if task.state == "error":
                    self._failed += 1
                elif task.result is not None:
                    self._last_result = {"task": task.as_event(),
                                         "result": task.result}
                if task.result is not None:
                    self._last_by_kind[task.kind] = {
                        "task": task.as_event(), "result": task.result}
            self._record(task)

    def _record(self, task: ExecutionTask) -> None:
        """Hook for the caller (the monitor writes the execution event)."""
        if self.recorder is None:
            return
        try:
            self.recorder(task)
        except Exception:  # noqa: BLE001 - recording must not stop the worker
            pass

    # --- reading ----------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            inflight = self._inflight
            now = self.clock()
            return {
                "alive": self.alive,
                "in_flight": inflight.as_event() if inflight is not None else None,
                "queue_depth": len(self._queue),
                "queued": [item.as_event() for item in self._queue],
                "last_heartbeat_ms": self._heartbeat_ms or None,
                "heartbeat_age_ms": (max(0, now - self._heartbeat_ms)
                                     if self._heartbeat_ms else None),
                "completed": self._completed,
                "failed": self._failed,
                "restarts": self._restarts,
                "deduplicated": self._deduped,
                "rejected": dict(self._rejected),
                "last_result": self._last_result,
                "last_error": self._last_error,
            }

    def last_by_kind(self, kind: str) -> dict | None:
        """The newest finished task of this kind, or None."""
        with self._lock:
            return self._last_by_kind.get(kind)
