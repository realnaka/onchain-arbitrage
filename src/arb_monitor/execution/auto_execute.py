"""Unattended execution: a gated signal becomes an order, without a human.

The monitor used to stop at "here is an opportunity", and everything after that
was a person running ``full-chain --mode live``. This is the piece in between:

```text
candidate (already past the alert threshold)
  → re-quote at execution time      a signal is not a price
  → RiskGate                        allowlist, size, TTL, kill switch,
                                    open contracts, daily quota
  → maker-first executor            the same flow the CLI runs
  → ledger + Feishu result
```

Three properties are enforced in code rather than described in prose:

* **off unless explicitly on.** ``[execution] mode`` has to be exactly
  ``preauthorized_program`` *and* ``[execution] auto_orders`` has to be
  ``paper`` or ``live``. Two independent keys, because one typo should never be
  the distance between a quiet monitor and a signed order.
* **fail closed.** Any unknown state — an order that cannot be read, a spot leg
  whose receipt did not confirm — writes a halt file that stops every later
  automated execution until a human clears it. The file is checked before each
  attempt, so the next cycle does not "just try again".
* **one at a time.** An exclusive lock is held for the whole execution, so a
  second process (or a re-entrant cycle) cannot place a second order on the
  strength of the same signal.

The daily quota is a query over ``data/execution.db``, not a remembered number:
``daily_quote_usdt`` was documented as a limit long before it was one.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .ledger import ExecutionLedger
from .basis_exit import (BasisExitPolicy, BasisObservation,
                         decide as decide_basis_exit, parse_basis)
from .funding_exit import (FundingExitPolicy, decide as decide_funding_exit,
                           read_funding_state)
from .order_flows import (HALT_STATES, classify_flow, maker_first_close,
                          maker_first_open)
from .worker import ExecutionTask, ExecutionWorker
from .pending_scan import is_exposure_state, scan_pending_orders
from ..ops.venue_scoreboard import DATA_QUALITY_REJECTIONS

HK = timezone(timedelta(hours=8))

# ``[execution] mode`` values that mean "unattended execution is preauthorised".
AUTO_MODES = frozenset({"preauthorized_program"})
# ``[execution] auto_orders``: what an automated execution is allowed to do.
ORDER_MODES = frozenset({"off", "paper", "live"})
# Results that have to be looked at by a human before automation continues.
UNKNOWN_STATES = frozenset(HALT_STATES | {"maker_order_rejected",
                                          "orphan_pending_orders",
                                          "unresolved_exposure_groups"})
# Refusals that a human has to act on but that are *not* halts: nothing was
# sent, so automation may continue, yet the venue-side cause (a spent ERC-20
# allowance) stays broken until somebody tops it up. These go to the
# operational channel for the same reason a halt does.
OPERATIONAL_STATES = frozenset({"spot_preflight_blocked"})


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def _day_bounds(ts_ms: int) -> tuple[int, int]:
    """The Asia/Shanghai day around a timestamp, in milliseconds."""
    moment = datetime.fromtimestamp(ts_ms / 1000, HK)
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)


@dataclass
class AutoExecutor:
    """Turns one gated candidate into one executed (or refused) round trip."""

    policy: dict
    monitor_config: dict
    ledger_path: str = "data/execution.db"
    notifier: Any = None
    # The operational room. Halts and stranded legs go here, ordinary results
    # ("executed / refused") go to ``notifier``. Defaults to the same object so
    # a caller that configures one channel is never worse off than before.
    alert_notifier: Any = None
    stack_factory: Callable[..., Any] | None = None
    clock: Callable[[], int] = now_ms
    stop_file: str = "data/AUTO_HALT"
    lock_file: str = "data/auto_execute.lock"
    max_per_cycle: int = 1
    # Plumbing tests only. The monitor loop never sets this; the smoke runner
    # does, and says so in its own output, so that the chain can be exercised
    # when the market is not offering a signal.
    force_plumbing_test: bool = False
    # ``account_verifier() -> dict`` with a ``verified`` flag. The monitor wires
    # it to ``account_dashboard.build_snapshot``; without it nothing checks that
    # the account can be read at all before the machine starts signing.
    account_verifier: Callable[[], Any] | None = None
    # Notified states, so a candidate that keeps being refused for the same
    # reason does not turn the channel into a pulse.
    _notified: dict = field(default_factory=dict)
    # asset → when this process last executed it, so a signal that persists for
    # several cycles is acted on once, not once per cycle.
    _last_executed: dict = field(default_factory=dict)
    # Exit attempts are also rate limited.  A persistent <=3 bps observation
    # must not submit a fresh maker order every 60-second cycle while the prior
    # close is still waiting or being reconciled.
    _last_basis_exit_attempt: dict = field(default_factory=dict)
    # Who runs the execution body. "inline" is the historical shape: the
    # sampling cycle calls `maybe_execute`/`maybe_funding_exit`/
    # `maybe_basis_exit` itself and waits out any maker-first order. "worker"
    # hands the same calls to `ExecutionWorker` and returns immediately.
    dispatch_mode: str = "inline"
    worker: Any = None
    # How long a shutdown may wait for an in-flight execution task to finish
    # before it writes the task down and leaves. It never cancels anything.
    shutdown_grace_seconds: float = 20.0

    def __post_init__(self) -> None:
        configured = str((self.execution or {}).get("dispatch") or self.dispatch_mode)
        self.dispatch_mode = "worker" if configured == "worker" else "inline"
        self.shutdown_grace_seconds = max(0.0, float(
            (self.execution or {}).get("shutdown_grace_seconds",
                                       self.shutdown_grace_seconds)))

    def worker_enabled(self) -> bool:
        return self.dispatch_mode == "worker"

    def ensure_worker(self):
        """Create and start the execution worker, once. Never raises."""
        if not self.worker_enabled():
            return None
        if self.worker is None:
            self.worker = ExecutionWorker(
                self._run_task, max_pending=int(
                    (self.execution or {}).get("worker_max_pending", 2)),
                clock=self.clock, recorder=self._record_worker_task)
            self.worker.start()
        elif not self.worker.alive:
            # A dead thread is restarted, but the monitor is told either way:
            # `dispatch` refuses to open while the worker is unhealthy.
            self.worker.maybe_restart()
        return self.worker

    def _record_worker_task(self, task) -> None:
        """Audit one finished task — **without inventing a ledger group**.

        Only tasks that already belong to a group are written to the ledger. An
        entry nomination that was refused before it reached the venue has no
        group, and writing it under a synthetic one (the first version used
        ``worker-entry``) created a group that never existed: `ledger-triage`
        then listed it as an unregistered record for a human to judge, once per
        cycle. Observed live 2026-09-21 minutes after the switch to worker mode.

        Entry outcomes are not lost by this: every cycle's run summary carries
        `auto_execution.attempts` and `execution_worker.last_result`, which is
        where the board and the operator read them.
        """
        if not task.group_id:
            return
        ledger = ExecutionLedger(self.ledger_path)
        try:
            ledger.append(
                task.group_id, "execution_task",
                task.state,
                {"task": task.as_event(),
                 "result": (task.result or {}).get("state") if task.result else None,
                 "reason": ((task.result or {}).get("reason")
                            if task.result else task.error)},
                task.finished_ms or self.clock())
        except Exception:  # noqa: BLE001 - recording must not stop the worker
            pass
        finally:
            ledger.close()

    def _run_task(self, task) -> dict:
        """The worker's handler: the same calls, on the worker's thread."""
        now = self.clock()
        if task.kind == "entry":
            candidate = task.payload.get("candidate") or {}
            if not candidate:
                return {"state": "empty_nomination", "executed": False,
                        "halt": False, "reason": "task_carried_no_candidate"}
            return self.execute(candidate, now)
        if task.kind == "funding_exit":
            return self.maybe_funding_exit(now)
        if task.kind == "basis_exit":
            # The evaluations snapshot is the monitor's own basis table. Its
            # `evaluated_at_ms` is what `decide_basis_exit` measures the 15 s
            # quote age against, so a snapshot that is too old is refused (and
            # alerted) exactly as it would be inline. Re-deriving a second
            # pricing path here would add a new way to price a close, which is
            # the opposite of what this change is for.
            return self.maybe_basis_exit(task.payload.get("evaluations") or [], now)
        # Never guess at a task this version does not know how to run.
        return {"state": "unknown_task_kind", "executed": False, "halt": False,
                "reason": f"unsupported_task_kind:{task.kind}"}

    @property
    def cooldown_ms(self) -> int:
        """How long the same asset is left alone after an execution.

        A basis that clears the bar usually stays above it for a while — nothing
        about the market changes in sixty seconds. Without this, one signal
        becomes one trade per cycle: in live mode the open-contract limit would
        eventually stop it, but only after it had already acted repeatedly.
        """
        return int(self.execution.get("auto_cooldown_seconds", 900)) * 1000

    def last_execution_ms(self, asset: str, ts_ms: int) -> int:
        """Newest execution of this asset: memory first, then the ledger.

        Memory covers the running loop; the ledger covers a restart, so a
        monitor that comes back up inside a persistent signal does not
        immediately repeat a trade it already made.
        """
        last = int(self._last_executed.get(asset, 0))
        lookback = max(self.cooldown_ms, 3_600_000)
        try:
            ledger = ExecutionLedger(self.ledger_path)
        except Exception:  # noqa: BLE001 - memory is the weaker, still useful answer
            return last
        try:
            found = ledger.recent_execution_assets(int(ts_ms) - lookback)
        except Exception:  # noqa: BLE001
            return last
        finally:
            ledger.close()
        return max(last, found.get(asset, 0))

    # --- configuration --------------------------------------------------
    @property
    def execution(self) -> dict:
        return self.policy.get("execution") or {}

    @property
    def mode(self) -> str:
        return str(self.execution.get("mode") or "")

    @property
    def order_mode(self) -> str:
        return str(self.execution.get("auto_orders") or "off")

    @property
    def limits(self) -> dict:
        return self.policy.get("limits") or {}

    def enabled(self) -> tuple[bool, str]:
        """Two independent switches, and a kill switch that outranks both."""
        if Path(str(self.execution.get("kill_switch_file", "data/KILL_SWITCH"))).exists():
            return False, "kill_switch"
        if self.mode not in AUTO_MODES:
            return False, f"mode_not_preauthorized:{self.mode or 'unset'}"
        if self.order_mode not in ORDER_MODES or self.order_mode == "off":
            return False, f"auto_orders_off:{self.order_mode}"
        if self.order_mode == "live":
            blocked = self.live_preconditions()
            if blocked:
                return False, blocked
        return True, self.order_mode

    def live_preconditions(self) -> str | None:
        """The extra keys live needs beyond ``auto_orders = "live"``.

        Paper needs the two switches. Live additionally needs a human to have
        answered two operational questions in the config file, because neither
        has a safe default and both were open on 2026-09-16:

        * **who closes the position.** The automatic chain opens and does not
          close; if that is accepted, the operator names the arrangement
          (``auto_close_policy``) instead of the code assuming it.
        * **that the acceptance was read.** ``live_ack`` is a free-text name,
          deliberately not a boolean: it is a record of a decision, not a flag
          to flip twice.
        """
        if not str(self.execution.get("auto_close_policy") or "").strip():
            return "live_requires_auto_close_policy"
        if not str(self.execution.get("live_ack") or "").strip():
            return "live_requires_operator_acknowledgement"
        return None

    def halt_reason(self) -> str | None:
        path = Path(self.stop_file)
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8").strip() or "halted"
        except OSError:
            return "halted"

    # --- limits ---------------------------------------------------------
    @property
    def paper_budget_separate(self) -> bool:
        """Does a modelled fill consume the real cash limit?

        Reviewed 2026-09-16: paper fills were counted against
        ``daily_quote_usdt``, so the live board read
        ``daily_used_quote = 90.252212 / 100`` after four paper groups and no
        money had moved — the paper chain would then lock itself out of the
        budget it was meant to rehearse. The two are separate by default and
        the paper side is reported on its own.
        """
        raw = self.execution.get("paper_budget_separate", True)
        return str(raw).strip().lower() not in {"false", "0", "no", "off"}

    def daily_used(self, ts_ms: int | None = None) -> Decimal:
        """Notional actually filled today, straight from the ledger."""
        start, end = _day_bounds(ts_ms if ts_ms is not None else self.clock())
        ledger = ExecutionLedger(self.ledger_path)
        try:
            return ledger.executed_quote_between(
                start, end, include_paper=not self.paper_budget_separate)
        finally:
            ledger.close()

    def paper_used(self, ts_ms: int | None = None) -> Decimal:
        """Modelled spend today, kept out of the real cash limit."""
        start, end = _day_bounds(ts_ms if ts_ms is not None else self.clock())
        ledger = ExecutionLedger(self.ledger_path)
        try:
            return ledger.paper_quote_between(start, end)
        finally:
            ledger.close()

    def trigger(self, candidate: dict, ts_ms: int) -> tuple[bool, str]:
        """Is this candidate worth signing? The bar is the alert's own bar."""
        roundtrip = candidate.get("roundtrip") or {}
        if not roundtrip.get("complete", False):
            return False, "roundtrip_cost_incomplete"
        required = roundtrip.get("breakeven_gross_bps")
        gross = candidate.get("gross_entry_basis_bps")
        if required is None or gross is None:
            return False, "missing_basis_or_requirement"
        if Decimal(str(gross)) < Decimal(str(required)):
            return False, "below_breakeven"
        if not candidate.get("mapping_verified"):
            return False, "mapping_not_verified"
        if not candidate.get("cost_verified"):
            return False, "cost_not_verified"
        # The token reference is the `execution_required` tier: it carries the
        # identity and decimals of the token that would be bought. A row the
        # monitor kept from an earlier round is fine to *screen* with and fine to
        # show, but the machine may not sign against it. This is the same
        # outcome the old behaviour produced by dropping the asset from the
        # table entirely — with a reason attached instead of silence.
        if candidate.get("reference_stale"):
            return False, "token_reference_stale"
        return True, "ok"

    def resolve_instrument(self, candidate: dict) -> tuple[str | None, str]:
        """Which contract we may actually use for this candidate.

        A candidate names the venue that showed the *best* price, which is not
        necessarily a venue we have a reviewed adapter for — Gate and Binance
        are monitoring-only in this project. So the venue is resolved here, from
        the policy allowlist and the asset's own reviewed mappings, and the
        re-quote below then decides whether that instrument still clears the bar.
        """
        allow = self.policy.get("allowlist") or {}
        venues = {str(value) for value in allow.get("venues") or []}
        instruments = {str(value) for value in allow.get("instruments") or []}
        current = str(candidate.get("perp_instrument") or "")
        if current in instruments and str(candidate.get("perp_venue") or "") in venues:
            return current, "candidate_venue"
        asset = str(candidate.get("asset") or "")
        for token in self.monitor_config.get("tokens") or []:
            if token.get("asset") != asset:
                continue
            for key in (token.get("mappings") or {}):
                venue, _, instrument = str(key).partition(":")
                if instrument in instruments and venue in venues:
                    return instrument, "allowlisted_venue_for_asset"
        return None, "venue_not_allowlisted"

    # --- execution ------------------------------------------------------
    def maybe_execute(self, candidates: list[dict], ts_ms: int | None = None) -> dict:
        """Run at most ``max_per_cycle`` automated executions. Never raises."""
        now = ts_ms if ts_ms is not None else self.clock()
        record: dict = {"mode": self.mode, "order_mode": self.order_mode,
                        "attempts": [], "skipped": {}, "executed": 0}
        allowed, detail = self.enabled()
        record["enabled"] = allowed
        if not allowed:
            record["reason"] = detail
            return record
        halt = self.halt_reason()
        if halt:
            # A previous unknown state is a stop, not a warning.
            record["reason"] = f"halted:{halt}"
            return record
        nominations, skipped, reason, cooldown_left = self.entry_nominations(
            candidates, now)
        record["skipped"] = skipped
        if reason:
            record["reason"] = reason
        if cooldown_left is not None:
            record["cooldown_remaining_ms"] = cooldown_left
        executed = 0
        for candidate in nominations:
            outcome = self.execute(candidate, now)
            record["attempts"].append(outcome)
            if outcome.get("halt"):
                break
            if outcome.get("executed"):
                executed += 1
                self._last_executed[str(candidate.get("asset") or "")] = now
        record["executed"] = executed
        record["daily_used_quote"] = str(self.daily_used(now))
        record["paper_used_quote"] = str(self.paper_used(now))
        return record

    def entry_nominations(self, candidates: list[dict], now: int):
        """Which candidates deserve an attempt, on the already-agreed gates.

        Split out of `maybe_execute` so the worker path and the inline path
        decide with **one** piece of code. This half is pure: it reads no venue,
        writes nothing, and holds no lock — it only applies the authorisation
        keys, the halt file, the entry bar, the per-asset cooldown and
        `auto_max_per_cycle`. Anything that costs a network round trip belongs to
        `execute` (or, in worker mode, to the worker).
        """
        skipped: dict = {}
        reason: str | None = None
        cooldown_left: int | None = None
        allowed, detail = self.enabled()
        if not allowed:
            return [], skipped, detail, None
        halt = self.halt_reason()
        if halt:
            return [], skipped, f"halted:{halt}", None
        nominations: list[dict] = []
        for candidate in candidates:
            if len(nominations) >= max(1, int(self.max_per_cycle)):
                break
            ok, reason = self.trigger(candidate, now)
            if not ok:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            asset = str(candidate.get("asset") or "")
            since = now - self.last_execution_ms(asset, now)
            if since < self.cooldown_ms:
                # The same trade, one cycle later. Act on a signal once.
                skipped["auto_cooldown"] = skipped.get("auto_cooldown", 0) + 1
                cooldown_left = self.cooldown_ms - since
                continue
            nominations.append(candidate)
        return nominations, skipped, reason, cooldown_left

    def _exit_groups(self, record: dict) -> list[dict]:
        """Groups that may still hold exposure — a ledger read, nothing else."""
        ledger = ExecutionLedger(self.ledger_path)
        try:
            groups = ledger.open_exposure_groups()
        except Exception as exc:  # noqa: BLE001 - unreadable ledger, no action
            record["skipped"]["ledger_unreadable"] = (
                record["skipped"].get("ledger_unreadable", 0) + 1)
            record.setdefault("warnings", []).append(
                f"exit_ledger_unreadable:{type(exc).__name__}")
            return []
        finally:
            ledger.close()
        return self._actionable_groups(groups, record)

    def dispatch(self, candidates: list[dict], evaluations, ts_ms: int | None = None) -> dict:
        """Worker mode: nominate tasks and return without waiting for any of them.

        Everything decided here is cheap and local: the two authorisation keys,
        the halt file, the entry bar, the per-asset cooldown, and the ledger's
        own list of groups that may still hold exposure. Everything that touches
        a venue happens on the worker, through exactly the same functions the
        inline path calls — so the gates cannot drift between the two.

        Exits are nominated before entries, and the queue refuses to drop an exit
        to make room for an open. When the worker is unavailable this refuses to
        **open** anything and says so; closing is never blocked by it, because
        the same code is still reachable from the CLI path.
        """
        now = ts_ms if ts_ms is not None else self.clock()
        record: dict = {"mode": self.mode, "order_mode": self.order_mode,
                        "dispatch": "worker", "enabled": False, "attempts": [],
                        "skipped": {}, "executed": 0, "submitted": [],
                        "queue_depth": 0}
        worker = self.ensure_worker()
        if worker is None:
            record["reason"] = "dispatch_inline"
            return record
        record["worker_alive"] = worker.alive
        if not worker.alive:
            record["reason"] = "execution_worker_unavailable"
            record["entries_blocked"] = True
        else:
            allowed, detail = self.enabled()
            record["enabled"] = allowed
            if not allowed:
                record["reason"] = detail
            else:
                halt = self.halt_reason()
                if halt:
                    record["reason"] = f"halted:{halt}"
                else:
                    nominations, skipped, reason, cooldown_left = (
                        self.entry_nominations(candidates, now))
                    record["skipped"] = skipped
                    if reason:
                        record["reason"] = reason
                    if cooldown_left is not None:
                        record["cooldown_remaining_ms"] = cooldown_left
                    for candidate in nominations:
                        status = worker.submit(ExecutionTask(
                            kind="entry",
                            payload={"candidate": candidate},
                            asset=str(candidate.get("asset") or ""),
                            instrument=candidate.get("perp_instrument")))
                        record["submitted"].append(
                            {"kind": "entry",
                             "asset": str(candidate.get("asset") or ""),
                             "status": status})
        # Exits do not depend on the entry gate: they reduce risk that already
        # exists, so a halt file or an unavailable worker must not suppress them.
        groups = self._exit_groups(record)
        if groups:
            record["groups_with_exposure"] = [group["group_id"] for group in groups]
            for kind in ("funding_exit", "basis_exit"):
                status = worker.submit(ExecutionTask(
                    kind=kind,
                    payload={"evaluations": evaluations if kind == "basis_exit"
                             else []}))
                record["submitted"].append({"kind": kind, "status": status})
        snapshot = worker.snapshot()
        record["queue_depth"] = snapshot["queue_depth"]
        record["in_flight"] = snapshot["in_flight"]
        record["execution_worker"] = snapshot
        record["funding_exit"] = worker.last_by_kind("funding_exit")
        record["basis_exit"] = worker.last_by_kind("basis_exit")
        # The worker's newest finished entry, so `attempts` is not empty while
        # an order is actually being worked on. Observed live 2026-09-21: the
        # first worker-mode cycles showed `attempts: []` even though a task had
        # run and been refused, which made the board look like nothing happened.
        entry_result = worker.last_by_kind("entry")
        if entry_result:
            record["attempts"] = [entry_result.get("result") or {}]
            record["last_entry_task"] = entry_result.get("task")
        return record

    def shutdown(self) -> None:
        """Stop the worker without touching a single order.

        This waits for the in-flight task, it does **not** cancel it. Two
        separate things are easy to confuse here:

        * an order that has been sent is never cancelled by a shutdown. The
          maker-first timeout and its taker fallback belong to the strategy and
          stay exactly where they are — a shutdown that pulled orders would be a
          new way to leave a half-finished hedge;
        * what a shutdown *can* lose is the **supervision** of an order it is
          waiting on. If the process goes away while an order rests, the order
          stays live and unobserved. That is precisely the 2026-09-16 incident
          (an order assumed cancelled filled sixteen minutes later).

        So: wait first, and if the wait cannot finish, write the task down where
        a restart will find it. The reconciliation gate that runs before every
        open (`_pending_gate` / `_ledger_gate`) reads the venue's own pending
        orders, so an interrupted wait becomes a halt to reconcile, not a
        silent loss.
        """
        if self.worker is None:
            return
        if not self.worker.drain(self.shutdown_grace_seconds):
            task = self.worker.in_flight_task()
            if task is not None:
                self._record_interrupted_task(task)
        self.worker.stop()

    def _record_interrupted_task(self, task) -> None:
        """Durably note that the process stopped watching a live order.

        Written to the ledger rather than only to memory, because the whole
        point is that the process is going away. A human (or the next start's
        reconciliation) can then see which task was in flight.
        """
        ledger = ExecutionLedger(self.ledger_path)
        try:
            ledger.append(
                task.group_id,
                "execution_shutdown_interrupted", task.state,
                {"task": task.as_event(),
                 "note": ("monitor stopped while this task was in flight; no "
                          "cancel was sent, the order (if any) is still live and "
                          "must be reconciled before any new open")},
                self.clock())
        except Exception:  # noqa: BLE001 - shutdown must not fail on bookkeeping
            pass
        finally:
            ledger.close()

    def execute(self, candidate: dict, ts_ms: int) -> dict:
        """One candidate end to end: lock, re-quote, gate, execute, report."""
        outcome: dict = {"asset": candidate.get("asset"), "state": "not_started",
                         "executed": False, "halt": False}
        lock = self._acquire_lock()
        if lock is None:
            # Somebody else is mid-execution. That is a *skip*, not a halt: the
            # holder may be doing exactly the trade this cycle wanted to do, and
            # stopping automation underneath it would be the wrong reflex.
            outcome.update(state="lock_held", halt=False,
                           reason="another_execution_holds_the_lock")
            self._record_outcome(candidate, outcome, ts_ms)
            return outcome
        try:
            outcome = self._execute_locked(candidate, ts_ms)
        except Exception as exc:  # noqa: BLE001 - an unattended loop must not die
            outcome.update(state="execution_error", executed=False, halt=True,
                           reason=f"{type(exc).__name__}:{str(exc)[:160]}")
        finally:
            self._release_lock(lock)
        if outcome.get("halt"):
            self._halt(candidate, outcome)
        self._record_outcome(candidate, outcome, ts_ms)
        return outcome

    def _execute_locked(self, candidate: dict, ts_ms: int) -> dict:
        instrument, source = self.resolve_instrument(candidate)
        if instrument is None:
            return {"asset": candidate.get("asset"), "state": "venue_not_allowlisted",
                    "executed": False, "halt": False,
                    "reason": f'{candidate.get("perp_venue")}:'
                              f'{candidate.get("perp_instrument")}_has_no_reviewed_adapter'}
        stack = None
        entered_flow = False
        try:
            stack = self._stack(live=self.order_mode == "live")
            blocked = self._account_gate()
            if blocked is not None:
                return blocked
            gate = self._pending_gate(stack)
            if gate is not None:
                return gate
            gate = self._ledger_gate(stack)
            if gate is not None:
                return gate
            budget = Decimal(str(self.limits.get("per_trade_quote_usdt", "0")))
            balance = stack.quote_balance()
            if balance is None:
                return {"asset": candidate.get("asset"), "state": "no_quote_balance",
                        "executed": False, "halt": False,
                        "reason": "wallet_balance_unreadable"}
            plan = stack.chain.build_plan(
                asset=str(candidate.get("asset")),
                size_quote=budget,
                perp_instrument=instrument,
                balance_quote=balance,
                shares=None if candidate.get("shares") in (None, "")
                else Decimal(str(candidate.get("shares"))))
            # Re-quote done. The signal is only allowed to *nominate* the trade:
            # the order is priced from the book that exists now, and the entry
            # bar is re-checked against that fresh number.
            required = Decimal(str((candidate.get("roundtrip") or {})
                                   .get("breakeven_gross_bps")))
            if plan.gross_bps < required:
                return {"asset": plan.asset, "state": "requote_below_breakeven",
                        "executed": False, "halt": False,
                        "reason": f"fresh_gross_{plan.gross_bps}_required_{required}",
                        "gross_bps": str(plan.gross_bps)}
            ttl_ms = int(self.limits.get("quote_ttl_seconds", 300)) * 1000
            quote_age_ms = ts_ms - int(plan.quote_observed_at_s * 1000)
            if quote_age_ms > ttl_ms:
                return {"asset": plan.asset, "state": "requote_expired",
                        "executed": False, "halt": False,
                        "reason": f"quote_age_ms_{quote_age_ms}_ttl_{ttl_ms}"}
            spent = self.daily_used(ts_ms)
            daily_cap = Decimal(str(self.limits.get("daily_quote_usdt", "0")))
            if spent + plan.size_quote > daily_cap:
                return {"asset": plan.asset, "state": "daily_quota_reached",
                        "executed": False, "halt": False,
                        "reason": f"{spent}+{plan.size_quote}>{daily_cap}",
                        "daily_used_quote": str(spent)}
            if self.force_plumbing_test:
                plan.forced_plumbing_test = True
            # The same gate as live, in the same order: paper mode exists to
            # prove this path works, not to bypass it.
            #
            # The position snapshot is taken *once*, before `entered_flow` can
            # be set. It used to be read twice — once here and once inside the
            # `maker_first_open(...)` call below — and the second read happened
            # after `entered_flow = True`, so a transport blip while merely
            # *listing positions* was classified as "an order may be in
            # flight" and wrote `data/AUTO_HALT`. Measured twice, 2026-09-19
            # 17:46 and 2026-09-21 06:47 (`OKXReadError:okx_transport:URLError`,
            # asset SHEIN, no group, nothing submitted): the same read had just
            # succeeded one statement earlier. A read failure cannot be a
            # post-write unknown state when nothing has been written.
            open_contracts = stack.open_contracts()
            allowed, reasons = stack.chain.risk_check(
                plan, open_contracts=open_contracts, balance_quote=balance)
            if not allowed:
                return {"asset": plan.asset, "state": "rejected",
                        "executed": False, "halt": False,
                        "reason": ",".join(reasons)}
            if self.order_mode == "paper":
                return self._paper(stack, plan, candidate, ts_ms)
            entered_flow = True
            flow = maker_first_open(
                stack.chain, plan, stack.policy, stack.ledger,
                balance_quote=balance, open_contracts=open_contracts)
            payload = flow.payload
            state = flow.state
            residual = Decimal(str(payload.get("unhedged_tokens") or "0"))
            executed, halt = classify_flow("open", state, residual)
            return {"asset": plan.asset, "state": state,
                    "instrument": instrument, "instrument_source": source,
                    "executed": executed,
                    "halt": halt,
                    "reason": payload.get("reason") or "",
                    "group_id": plan.intent.signal_id if plan.intent else None,
                    "plan": plan.summary(), "flow": payload}
        except Exception as exc:  # noqa: BLE001 - the caller turns this into a state
            # Where the failure happened decides what it means. A quote read or a
            # sizing probe that fails has sent nothing, so it is a transient
            # miss and does not stop automation; a failure once the order flow
            # has started may have left a write in flight, and that is exactly
            # the unknown state the halt file exists for.
            #
            # Operator decision 2026-09-21: "只有真的拨了号，才算可能动手了".
            # Entering the flow is not a write; a submitted client order id or a
            # wallet broadcast is. (``getattr(stack.trade, ...)`` also used to
            # raise when the stack had no trade client, turning any earlier
            # exception into a halt whose reason was this line's AttributeError.)
            sent = bool(getattr(getattr(stack, "trade", None),
                                "submitted_client_ids", []))
            wallet_wrote = bool(getattr(getattr(stack, "wallet_leg", None),
                                       "writes_started", False))
            # Pull back whatever this run placed *before* stopping. A halt that
            # leaves a resting order alive is not a stop, it is a deferred fill:
            # measured 2026-09-21, the order placed at 06:45 filled at 06:50 —
            # three minutes after AUTO_HALT was written — and no ledger row
            # mentioned it, so `ledger-triage --check-account` answered
            # `may_hold_position: false` while the account was short two
            # contracts of SHEIN-USDT-SWAP.
            orders = self._cancel_submitted_orders(stack, instrument)
            return {"asset": candidate.get("asset"), "state": "execution_error",
                    "executed": False, "halt": bool(sent or wallet_wrote),
                    "instrument": instrument, "instrument_source": source,
                    "orders": orders,
                    "live_client_order_ids": [row["client_order_id"]
                                              for row in orders if row.get("live")],
                    # Recorded so a reconciliation knows what it is looking
                    # for: a failure before the first write proves nothing was
                    # sent (nothing to find), while a submitted client id may
                    # have reached the venue.
                    "writes_started": bool(sent or wallet_wrote),
                    "exposure_possible": bool(sent or wallet_wrote),
                    "reason": f"{type(exc).__name__}:{str(exc)[:160]}"}
        finally:
            if stack is not None:
                stack.close()

    def _cancel_submitted_orders(self, stack, instrument: str | None) -> list[dict]:
        """Pull back every order this run placed; say which ones may survive.

        One row per client order id: ``client_order_id``, ``live`` (may still be
        resting) and the venue's answer. A cancel that cannot be *confirmed*
        leaves ``live`` true — an order we cannot prove is gone is an order that
        can still fill, and the recovery path has to be able to find it by name.
        """
        trade = getattr(stack, "trade", None)
        ids = [str(cid) for cid in (getattr(trade, "submitted_client_ids", []) or [])]
        if not ids:
            return []
        if trade is None or not instrument:
            return [{"client_order_id": cid, "live": True,
                     "detail": "no_cancel_path"} for cid in ids]
        rows: list[dict] = []
        for cid in ids:
            live, detail = True, "not_attempted"
            for attempt in range(2):
                try:
                    answer = trade.cancel_by_client_id(instrument, cid)
                    live = False
                    detail = f"cancelled={answer.get('cancelled')}:{answer.get('sCode')}"
                    break
                except Exception as exc:  # noqa: BLE001 - unknown stays live
                    detail = f"{type(exc).__name__}:{str(exc)[:80]}"
            rows.append({"client_order_id": cid, "live": live, "detail": detail})
        return rows
    def _account_gate(self) -> dict | None:
        """Refuse to act on an account whose state cannot be read.

        The unattended path used to go from "there is a candidate" straight to
        "read the wallet balance", so an OKX or wallet outage showed up as a
        failed *step* rather than as a refusal to start. Measured 2026-09-16:
        ``account-status --json`` answered ``okx.status="error"`` and
        ``wallet.status="error"`` while the shell still exited 0.
        """
        if self.account_verifier is None:
            return None
        try:
            snapshot = self.account_verifier()
        except Exception as exc:  # noqa: BLE001 - unreadable is not verified
            return {"asset": None, "state": "account_not_verified",
                    "executed": False, "halt": False,
                    "reason": f"account_verifier_failed:{type(exc).__name__}"}
        if snapshot is None or not snapshot.get("verified"):
            detail = {}
            if isinstance(snapshot, dict):
                detail = {name: (snapshot.get(name) or {}).get("status")
                          for name in ("okx", "wallet", "gas", "ledger")}
            return {"asset": None, "state": "account_not_verified",
                    "executed": False, "halt": False,
                    "reason": f"account_state_unverified:{detail}"}
        return None

    def _pending_gate(self, stack) -> dict | None:
        """Refuse to start while a resting order has no live group.

        A read failure blocks this cycle (nothing is sent, nothing is halting
        the world); an actual orphan writes ``AUTO_HALT``, because an order we
        cannot attribute is the exact shape of the 2026-09-16 naked short.
        """
        ledger = ExecutionLedger(self.ledger_path)
        try:
            scan = scan_pending_orders(read=stack.read, ledger=ledger,
                                       now_ms=self.clock(),
                                       policy=self.policy)
        except Exception as exc:  # noqa: BLE001 - unknown blocks the cycle
            return {"asset": None, "state": "pending_scan_failed", "executed": False,
                    "halt": False,
                    "reason": f"pending_scan_error:{type(exc).__name__}"}
        finally:
            ledger.close()
        if scan.status != "ok":
            return {"asset": None, "state": "pending_orders_unreadable",
                    "executed": False, "halt": False,
                    "reason": scan.reasons[0] if scan.reasons else "unreadable",
                    "pending_scan": scan.summary()}
        if scan.orphans:
            return {"asset": None, "state": "orphan_pending_orders",
                    "executed": False, "halt": True,
                    "reason": ",".join(scan.reasons[:4]),
                    "pending_scan": scan.summary()}
        return None

    def _ledger_gate(self, stack) -> dict | None:
        """Refuse to open while an earlier group still holds something.

        This is the crash case: the process dies after the on-chain buy and
        before the hedge (or between any two writes), the ledger's last word is
        ``spot_filled``, and nothing follows it. A restart must not treat the
        absence of further events as an absence of exposure — it has to stop and
        ask, because the answer is only knowable from the venue.

        Preparation states (``precheck``, ``allowance_ready``) are listed as
        unresolved for the board but do not block: they cannot hold a position
        unless a later event exists, and blocking on them would stall the chain
        forever on the very first interrupted run.
        """
        ledger = ExecutionLedger(self.ledger_path)
        try:
            # A finished hedge is a matched position, not an unknown crash.
            # `hedged` used to sit in the exposure set, so one completed CRWV
            # open halted every later name (MRNA) with
            # groups_with_possible_exposure. Only an unfinished leg blocks.
            found = [{"group_id": group, "state": row.get("state")}
                     for group, row in ledger.recover().items()
                     if is_exposure_state(row.get("state"))
                     and row.get("state") != "hedged"]
        except Exception as exc:  # noqa: BLE001 - unknown blocks the cycle
            return {"asset": None, "state": "ledger_unreadable", "executed": False,
                    "halt": False,
                    "reason": f"ledger_read_failed:{type(exc).__name__}"}
        finally:
            ledger.close()
        if found:
            return {"asset": None, "state": "unresolved_exposure_groups",
                    "executed": False, "halt": True,
                    "reason": ("groups_with_possible_exposure:"
                               + ",".join(f"{row['group_id']}={row['state']}"
                                          for row in found[:4])),
                   "unresolved_groups": found}
        return None

    # --- funding-cost exit ----------------------------------------------
    @property
    def funding_policy(self) -> FundingExitPolicy:
        return FundingExitPolicy.from_policy(self.policy)

    # --- basis-target exit ----------------------------------------------
    @property
    def basis_policy(self) -> BasisExitPolicy:
        return BasisExitPolicy.from_policy(self.policy)

    def maybe_basis_exit(self, evaluations, ts_ms: int | None = None) -> dict:
        """The operator's exit rule: the basis came back to 3 bps → close.

        Operator, 2026-09-21: enter when the spot/perp spread reaches 20 bps,
        close when it comes back to 3 bps. The entry bar is
        ``[monitor] min_net_edge_bps``; this is the other half, and it is the
        number every entry plan already assumed (``assumed_exit_basis_bps``).

        It never invents a position: the size comes from the venue's own
        ``positions`` read and the identity from the ledger. A basis that cannot
        be read, or one carrying a data-quality flag, is an alert — not a close.
        """
        now = ts_ms if ts_ms is not None else self.clock()
        record: dict = {"checked": 0, "evaluations": [], "closed": [], "alerts": [],
                        "skipped": {}, "mode": self.order_mode}
        policy = self.basis_policy
        if not policy.enabled:
            record["reason"] = "disabled"
            return record
        ledger = ExecutionLedger(self.ledger_path)
        try:
            groups = ledger.open_exposure_groups()
        except Exception as exc:  # noqa: BLE001 - unreadable ledger, no action
            record["reason"] = f"ledger_unreadable:{type(exc).__name__}"
            ledger.close()
            return record
        ledger.close()
        if not groups:
            record["reason"] = "no_open_group"
            return record
        allowed, detail = self.enabled()
        if self.order_mode != "live":
            # Paper has no real position to protect; the rule stays inert.
            record["skipped"]["order_mode_not_live"] = 1
            record["reason"] = detail if not allowed else "order_mode_not_live"
            return record
        stack = None
        try:
            groups = self._actionable_groups(groups, record)
            if not groups:
                return record
            stack = self._stack(live=True)
            # Account verification is an entry gate.  An outage in the
            # dashboard/gas/ledger side of the verifier must not suppress an
            # already-authorised risk reduction; the close path performs its
            # own live position, pending-order and wallet checks.  Otherwise a
            # transient read outage would leave a basis-target position open.
            positions = list(stack.read.positions())
            for group in groups:
                instrument = group.get("instrument")
                if not instrument:
                    record["skipped"]["group_without_instrument"] = (
                        record["skipped"].get("group_without_instrument", 0) + 1)
                    continue
                rows = [row for row in positions
                        if str(row.get("instId") or "") == instrument
                        and Decimal(str(row.get("pos") or 0)) < 0]
                record["checked"] += 1
                if not rows:
                    # A completed perp leg can leave the on-chain leg behind.
                    # Let the close path inspect the wallet and decide whether
                    # a spot-only recovery is needed; returning here used to
                    # strand residual spot forever after a partial close.
                    record.setdefault("warnings", []).append(
                        f"{group['group_id']}:no_short_at_venue_check_spot_residual")
                    # This is risk reduction, not a basis decision.  Once the
                    # hedge is confirmed flat, any remaining token is naked
                    # spot exposure and must be reconciled/sold even when the
                    # current basis is above the normal 3 bps target or cannot
                    # be observed.  Waiting for basis convergence here was a
                    # real way to strand the residual indefinitely.
                    outcome = self._close_group(stack, group, {
                        "observation": {"instrument": instrument},
                        "reason": "no_short_at_venue_spot_recovery"}, now,
                        label="基差平仓")
                    record["closed"].append(outcome)
                    if outcome.get("state") not in {"closed", "perp_only_closed",
                                                    "closed_partial"}:
                        record["alerts"].append(group["group_id"])
                    continue
                observation = self._basis_observation(group, instrument,
                                                      evaluations, now)
                decision = decide_basis_exit(policy, observation, now)
                row = {"group_id": group["group_id"], "instrument": instrument,
                       **decision.as_event()}
                record["evaluations"].append(row)
                if decision.action == "above_target":
                    continue
                if decision.action in {"unknown", "stale", "no_observation"}:
                    self._alert_basis(group, row, "平仓基差不可用/不可信")
                    record["alerts"].append(row["group_id"])
                    continue
                last_attempt = int(self._last_basis_exit_attempt.get(group["group_id"], 0))
                cooldown_ms = max(0, int(policy.cooldown_seconds)) * 1000
                if last_attempt and now - last_attempt < cooldown_ms:
                    record["skipped"]["basis_exit_cooldown"] = (
                        record["skipped"].get("basis_exit_cooldown", 0) + 1)
                    continue
                self._last_basis_exit_attempt[group["group_id"]] = now
                outcome = self._close_group(stack, group, row, decision.reason, now,
                                            label="基差平仓")
                record["closed"].append(outcome)
                if outcome.get("state") not in {"closed", "perp_only_closed",
                                                "closed_partial"}:
                    record["alerts"].append(group["group_id"])
        except Exception as exc:  # noqa: BLE001 - an unattended loop must not die
            record["reason"] = f"basis_exit_error:{type(exc).__name__}:{str(exc)[:120]}"
        finally:
            if stack is not None:
                stack.close()
        return record

    def _basis_observation(self, group: dict, instrument: str, evaluations,
                           now_ms: int) -> BasisObservation:
        """The exit basis for this position, **on a venue we may trade**.

        The evaluation table lists every venue; the position lives on one. Taking
        the best basis in the table would price the exit on a venue with no
        execution adapter, so only allowlisted venues are considered.
        """
        venues = {str(v) for v in
                  (self.policy.get("allowlist") or {}).get("venues") or []}
        wanted = {f"{venue}:{instrument}" for venue in venues}
        row = None
        for candidate in evaluations or ():
            if str(candidate.get("instrument") or "") in wanted:
                row = candidate
                break
        if row is None:
            return BasisObservation(asset=str(group.get("asset") or ""),
                                    instrument=instrument,
                                    detail="no_evaluation_on_a_tradable_venue")
        flags = sorted(DATA_QUALITY_REJECTIONS.intersection(
            str(x) for x in (row.get("rejections") or ())))
        return BasisObservation(
            asset=str(row.get("asset") or group.get("asset") or ""),
            instrument=str(row.get("instrument") or instrument),
            exit_basis_bps=parse_basis(row.get("current_exit_basis_bps")),
            observed_at_ms=int(row.get("evaluated_at_ms") or 0) or None,
            trusted=not flags,
            detail=("data_quality:" + ",".join(flags)) if flags else "")

    def _alert_basis(self, group: dict, row: dict, headline: str) -> None:
        target = self.alert_notifier or self.notifier
        if target is None:
            return
        observation = row.get("observation") or {}
        text = "\n".join([
            f"【基差平仓·需要人工】{group.get('instrument')} · {headline}",
            f"账本组: {group['group_id']}（{group.get('state')}）",
            f"平仓基差: {observation.get('exit_basis_bps')} bps · "
            f"目标 {self.basis_policy.target_exit_basis_bps} bps",
            f"原因: {row.get('reason')}",
            "未下单：读不到或不可信的基差不能用来平仓。",
        ])
        try:
            target.send(text)
        except Exception:  # noqa: BLE001 - delivery must not break the loop
            pass

    def maybe_funding_exit(self, ts_ms: int | None = None) -> dict:
        """The operator's funding rule, evaluated once per cycle.

        Rule (2026-09-16): while we hold the short, if the funding rate is
        negative — we pay — and the funding we have actually paid reaches the
        threshold, then one minute before the next settlement the position is
        closed **whatever the basis is doing**. This is a cost rule, not an
        opportunity: it may close at a loss, on purpose.

        It never invents a position. The size comes from the venue's own
        ``positions`` read and the group identity from the ledger; if the two
        disagree, or the funding data cannot be read, the action is an alert for
        a human, not a close.
        """
        now = ts_ms if ts_ms is not None else self.clock()
        record: dict = {"checked": 0, "evaluations": [], "closed": [], "alerts": [],
                        "skipped": {}, "mode": self.order_mode}
        policy = self.funding_policy
        if not policy.enabled:
            record["reason"] = "disabled"
            return record
        ledger = ExecutionLedger(self.ledger_path)
        try:
            groups = ledger.open_exposure_groups()
        except Exception as exc:  # noqa: BLE001 - unreadable ledger, no action
            record["reason"] = f"ledger_unreadable:{type(exc).__name__}"
            ledger.close()
            return record
        ledger.close()
        if not groups:
            record["reason"] = "no_open_group"
            return record
        allowed, detail = self.enabled()
        if self.order_mode != "live":
            # Paper has no real position to protect; the rule stays inert and
            # says so rather than pretending to have closed something.
            record["skipped"]["order_mode_not_live"] = 1
            record["reason"] = detail if not allowed else "order_mode_not_live"
            return record
        stack = None
        try:
            groups = self._actionable_groups(groups, record)
            if not groups:
                return record
            stack = self._stack(live=True)
            # Same rule as basis exit: verification gates new risk, never the
            # funding stop that reduces an existing short.
            positions = list(stack.read.positions())
            for group in groups:
                instrument = group.get("instrument")
                rows = [row for row in positions
                        if str(row.get("instId") or "") == instrument
                        and Decimal(str(row.get("pos") or 0)) < 0]
                if not rows:
                    # A previous close may have flattened the perp while the
                    # spot leg still needs recovery.  Do not discard the
                    # group here; _close_group checks the wallet and can run a
                    # spot-only sale.
                    record.setdefault("warnings", []).append(
                        f"{group['group_id']}:no_short_at_venue_check_spot_residual")
                    short = Decimal(0)
                    notional = Decimal(0)
                else:
                    short = abs(sum((Decimal(str(row.get("pos") or 0)) for row in rows),
                                    Decimal(0)))
                    notional = sum((abs(Decimal(str(row.get("notionalUsd") or 0)))
                                    for row in rows), Decimal(0))
                record["checked"] += 1
                if not rows:
                    # Funding cannot be computed without a live short. The
                    # close path still gets a chance to reconcile a residual
                    # spot leg, then the group is terminal if both sides are
                    # confirmed flat.
                    outcome = self._close_group(stack, group, {
                        "observation": {"instrument": instrument},
                        "reason": "no_short_at_venue_spot_recovery"}, now,
                        label="资金费平仓")
                    record["closed"].append(outcome)
                    if outcome.get("state") not in {"closed", "perp_only_closed",
                                                    "closed_partial"}:
                        record["alerts"].append(group["group_id"])
                    continue
                observation = read_funding_state(
                    instrument=instrument,
                    position_start_ms=int(group.get("opened_at_ms") or 0),
                    now_ms=now,
                    public_get=stack.trade.public,
                    signed_get=stack.read.get,
                    position_notional_quote=notional)
                decision = decide_funding_exit(policy, observation, now)
                row = {"group_id": group["group_id"], "instrument": instrument,
                       "short_contracts": str(short), **decision.as_event()}
                record["evaluations"].append(row)
                if decision.action == "none" or decision.action == "disabled":
                    continue
                self._record_funding_decision(group, row, now)
                if decision.action == "unknown":
                    self._alert_funding(group, row, "无法读取资金费率数据")
                    record["alerts"].append(row["group_id"])
                    continue
                if decision.action == "armed":
                    if self._funding_alert_key(group, observation) not in self._notified:
                        self._notified[self._funding_alert_key(group, observation)] = now
                        self._alert_funding(group, row, "已到达资金费阈值，将在结算前"
                                                        f"{policy.lead_seconds} 秒平仓")
                        record["alerts"].append(row["group_id"])
                    continue
                outcome = self._close_group(stack, group, row, decision.reason, now,
                                            label="资金费平仓")
                record["closed"].append(outcome)
                if outcome.get("state") not in {"closed", "perp_only_closed",
                                                "closed_partial"}:
                    record["alerts"].append(group["group_id"])
        except Exception as exc:  # noqa: BLE001 - an unattended loop must not die
            record["reason"] = f"funding_exit_error:{type(exc).__name__}:{str(exc)[:120]}"
        finally:
            if stack is not None:
                stack.close()
        return record

    @staticmethod
    def _funding_alert_key(group: dict, observation) -> str:
        key = f"funding:{group['group_id']}:{observation.settlement_at_ms}"
        return key

    def _alert_funding(self, group: dict, row: dict, headline: str) -> None:
        observation = row.get("observation") or {}
        lines = [f"【无人值守平仓】{group.get('instrument')} · {headline}",
                 f"账本组: {group['group_id']}（{group.get('state')}）"]
        if "funding_rate" in observation:
            # Funding-specific lines only for the funding rule; the close path is
            # shared, and a basis exit has no settlement window to report.
            lines += [
                f"资金费率: {observation.get('funding_rate')}（负=空头支付） · "
                f"下次结算: {observation.get('settlement_at_ms')}",
                f"已支付资金费累计: {observation.get('paid_quote')} quote token "
                f"（门槛 {self.funding_policy.threshold_quote}）",
            ]
        if row.get("short_contracts"):
            lines.append(f"本组空头: {row.get('short_contracts')} 张 · 名义 "
                         f"{observation.get('position_notional_quote')}")
        if row.get("close_at_ms"):
            lines.append(f"计划平仓时间: {row.get('close_at_ms')}")
        lines += ["", f"规则/原因: {row.get('reason') or row.get('rule_reason') or headline}"]
        text = "\n".join(lines)
        target = self.alert_notifier or self.notifier
        if target is None:
            return
        try:
            target.send(text)
        except Exception:  # noqa: BLE001 - delivery never changes the decision
            pass

    def _record_funding_decision(self, group: dict, row: dict, ts_ms: int) -> None:
        ledger = ExecutionLedger(self.ledger_path)
        try:
            ledger.append(group["group_id"], "funding_exit_evaluated",
                          "funding_exit_evaluated", row, ts_ms)
        except Exception:  # noqa: BLE001 - the alert is the primary record
            pass
        finally:
            ledger.close()

    def _recover_missing_instruments(self, groups: list[dict], record: dict) -> None:
        """Fill a blank contract from the group's own asset, when that map is unique.

        Opens since 2026-09-24 write ``instrument`` on every ledger row. Groups
        opened before that (the three live CRWV hedges) recorded ``asset`` and
        the fills, and nothing else. Closing still requires one allowlisted
        contract: zero matches or two matches stay blank and are skipped.
        """
        allow = self.policy.get("allowlist") or {}
        venues = {str(value) for value in allow.get("venues") or []}
        instruments = {str(value) for value in allow.get("instruments") or []}
        ledger = ExecutionLedger(self.ledger_path)
        try:
            for group in groups:
                if group.get("instrument"):
                    continue
                asset = ledger.group_asset(group["group_id"])
                if not asset:
                    continue
                matches: set[str] = set()
                for token in self.monitor_config.get("tokens") or []:
                    if token.get("asset") != asset:
                        continue
                    for key in (token.get("mappings") or {}):
                        venue, _, instrument = str(key).partition(":")
                        if instrument in instruments and venue in venues:
                            matches.add(instrument)
                if len(matches) != 1:
                    continue
                instrument = next(iter(matches))
                group["instrument"] = instrument
                group["instrument_source"] = "unique_allowlisted_mapping"
                record.setdefault("instrument_recovered", []).append(
                    {"group_id": group["group_id"], "asset": asset,
                     "instrument": instrument})
        finally:
            ledger.close()

    def _actionable_groups(self, groups: list[dict], record: dict) -> list[dict]:
        """Drop groups that no close could even be addressed to.

        ``open_exposure_groups`` deliberately includes states such as
        ``perp_closed``/``closed_partial``, because a close can settle the
        perpetual leg before the spot sale and the residual spot still has to be
        found. But a group whose recorded ``instrument`` is unknown cannot be
        acted on at all: ``_close_group`` resolves the asset from the instrument
        and returns ``asset_unresolved`` without writing anything.

        Before this, such a group still made the exit rules build a live stack
        and read the venue's positions — two private reads, every cycle,
        forever, for a decision that was already made. Measured 2026-09-21:
        ``xiaomi-751a7aef0f`` (``perp_closed``, no instrument, 2026-09-16) had
        been doing exactly that.

        The skipping is recorded under the same key as before, so the board's
        picture does not change — only the reads it costs.

        A group that named its asset, and whose asset has exactly one
        allowlisted contract, is filled in first. That is the same mapping the
        entry path already uses. It is not a guess across venues.
        """
        self._recover_missing_instruments(groups, record)
        actionable = [group for group in groups if group.get("instrument")]
        missing = len(groups) - len(actionable)
        if missing:
            record["skipped"]["group_without_instrument"] = (
                record["skipped"].get("group_without_instrument", 0) + missing)
        return actionable

    def _close_group(self, stack, group: dict, row: dict, reason: str,
                     ts_ms: int, label: str = "平仓") -> dict:
        """Close one group through the reviewed close path.

        Shared by every unattended rule that decides to exit — the funding-cost
        stop and the basis-target exit. The rules differ in *why* they fire; the
        close itself must not, or two exits would drift into two behaviours.
        """
        outcome: dict = {"group_id": group["group_id"], "state": "not_started",
                        "executed": False, "halt": False, "reason": reason}
        lock = self._acquire_lock()
        if lock is None:
            outcome.update(state="lock_held", reason="another_execution_holds_the_lock")
            return outcome
        try:
            instrument = group["instrument"]
            asset = None
            for token in self.monitor_config.get("tokens") or []:
                for key in (token.get("mappings") or {}):
                    if str(key).endswith(":" + instrument) or key == instrument:
                        asset = token.get("asset")
                        break
                if asset:
                    break
            if asset is None:
                outcome.update(state="asset_unresolved",
                               reason=f"no_allowlisted_asset_for_{instrument}")
                return outcome
            positions = list(stack.read.positions())
            contracts = abs(sum((Decimal(str(p.get("pos") or 0)) for p in positions
                                 if str(p.get("instId") or "") == instrument),
                                Decimal(0)))
            plan = stack.chain.build_close_plan(
                asset=asset, perp_instrument=instrument,
                token_quantity=Decimal(0), contract_quantity=contracts)
            tokens = Decimal(0)
            try:
                tokens = Decimal(str(stack.wallet_leg.balance_of(
                    plan.token_address, stack.runner)))
            except Exception as exc:  # noqa: BLE001 - unknown balance, no sale
                outcome.update(state="spot_balance_unreadable",
                               reason=f"{type(exc).__name__}:{str(exc)[:120]}")
                return outcome
            try:
                pending = list(stack.read.pending_orders())
            except Exception as exc:  # noqa: BLE001 - unknown must not bypass the gate
                outcome.update(state="pending_orders_unreadable",
                               reason=f"{type(exc).__name__}:{str(exc)[:120]}")
                return outcome
            if contracts <= 0 and tokens <= 0:
                # The venue and wallet have both been read, and there are no
                # resting orders.  Keep this group from being rediscovered and
                # retried forever: record the three-way flat evidence as a
                # terminal reconciliation event.  This is not a guessed fill
                # and does not claim PnL; it only says the live account is flat.
                ledger = ExecutionLedger(self.ledger_path)
                try:
                    ledger.append(group["group_id"], "reconciled", "reconciled_flat",
                                  {"reason": "no_exposure_at_venue",
                                   "instrument": instrument,
                                   "contracts": str(contracts),
                                   "tokens": str(tokens),
                                   "pending_orders": 0,
                                   "source": "auto_close_live_reconciliation"}, ts_ms)
                finally:
                    ledger.close()
                outcome.update(state="reconciled", executed=False,
                               reconciled=True)
                return outcome
            ledger = ExecutionLedger(self.ledger_path)
            try:
                flow = maker_first_close(
                    stack.chain, plan, stack.policy, ledger,
                    contracts=contracts, tokens=tokens,
                    opened_group_id=group["group_id"],
                    positions=positions, pending_orders=pending,
                    balance_source=lambda address: stack.wallet_leg.balance_of(
                        address, stack.runner))
                outcome.update(state=flow.state, executed=flow.executed,
                               halt=flow.halt, halt_reason=flow.payload.get("reason"),
                               spot=(flow.payload.get("spot") or {}).get("tx_hash"),
                               contracts=str(contracts), tokens=str(tokens))
            finally:
                ledger.close()
        except Exception as exc:  # noqa: BLE001 - report, never raise upward
            # A close failure before the first write is a retryable refusal.
            # Once an order id or wallet broadcast exists, it is an unknown
            # write and must stop new risk until reconciled.
            trade = getattr(stack, "trade", None)
            sent = bool(getattr(trade, "submitted_client_ids", []))
            wallet_wrote = bool(getattr(getattr(stack, "wallet_leg", None),
                                       "writes_started", False))
            outcome.update(state="close_execution_error",
                           reason=f"{type(exc).__name__}:{str(exc)[:160]}",
                           halt=bool(sent or wallet_wrote),
                           writes_started=bool(sent or wallet_wrote),
                           exposure_possible=bool(sent or wallet_wrote))
        finally:
            self._release_lock(lock)
        if outcome.get("halt"):
            self._halt({"asset": row.get("observation", {}).get("instrument")},
                       {"state": outcome.get("state"), "reason": outcome.get("reason"),
                        "group_id": outcome.get("group_id"),
                        "writes_started": outcome.get("writes_started"),
                        "exposure_possible": outcome.get("exposure_possible")})
        elif outcome.get("state") not in {"closed", "perp_only_closed",
                                           "closed_partial"}:
            # An exit that could not be executed is exactly the kind of thing
            # that must not pass quietly: the gate refused it (a kill switch, an
            # unreadable balance), the venue rejected it, or the spot sale is
            # stranded. The position is still there, so say so.
            self._alert_funding(
                group, {"observation": row.get("observation") or {},
                        "reason": reason,
                        "close_at_ms": row.get("close_at_ms"),
                        "short_contracts": outcome.get("contracts")},
                f"{label}未完成（{outcome.get('state')}）— 需要人工处理")
        self._record_funding_decision(group, {"close": outcome, "rule": row}, ts_ms)
        return outcome

    def _paper(self, stack, plan, candidate: dict, ts_ms: int) -> dict:
        """Paper execution on real quotes: fills assumed, money not moved.

        The fill assumption is explicit — a paper run models *plumbing*, not the
        probability of getting filled at the touch. Everything else (the prices,
        the sizes, the cost model, the ledger, the notification) is the real
        thing, so a paper cycle fails for the same reasons a live one would.
        """
        signal_id = plan.intent.signal_id if plan.intent else candidate.get("asset", "")
        price = plan.perp_ask_usd or plan.perp_bid_usd
        event = {
            "signal_id": signal_id,
            "leg": "buy", "mode": "paper", "fill_assumption": "full_fill_at_touch",
            "perp": {"side": "sell", "price": str(price),
                     "size": str(plan.perp_contract_quantity)},
            "spot": {"side": "buy", "price": str(plan.spot_price_usd),
                     "account": str(plan.quote_symbol)},
            "quote_amount": str(plan.size_quote),
            "size_quote": str(plan.size_quote),
            "expected_token_quantity": str(plan.expected_token_quantity),
            "entry_cost_bps": str(plan.cost_bps),
            "plan": plan.summary(),
        }
        stack.ledger.append(signal_id, "paper_hedged", "paper_precheck",
                            {"signal_id": signal_id, "plan": plan.summary()}, ts_ms)
        stack.ledger.append(signal_id, "paper_hedged", "paper_filled", event, ts_ms)
        return {"asset": plan.asset, "state": "paper_hedged", "executed": True,
                "halt": False, "reason": "paper_fill_assumed", "group_id": signal_id,
                "plan": plan.summary(),
                "flow": {"state": "paper_hedged", "paper": event}}

    # --- plumbing -------------------------------------------------------
    def _stack(self, *, live: bool):
        if self.stack_factory is None:
            from .live_stack import build_live_stack
            self.stack_factory = build_live_stack
        return self.stack_factory(policy_path=self._policy_path(),
                                  ledger_path=self.ledger_path, live=live)

    def _policy_path(self) -> str:
        return str(self.execution.get("policy_path") or "config/automation_policy.toml")

    def _acquire_lock(self):
        """Exclusive, and released by the OS if this process dies."""
        path = Path(self.lock_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return None
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "at_ms": self.clock()}))
        handle.flush()
        return handle

    @staticmethod
    def _release_lock(handle) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _halt(self, candidate: dict, outcome: dict) -> None:
        """Stop all further automated execution until a human clears this."""
        payload = {"state": outcome.get("state"), "reason": outcome.get("reason"),
                   "asset": candidate.get("asset"),
                   "group_id": outcome.get("group_id"),
                   "writes_started": outcome.get("writes_started"),
                   "exposure_possible": outcome.get("exposure_possible"),
                   # Nested with `client_order_id` keys on purpose: the ledger's
                   # `client_order_ids()` walks any payload and attributes an
                   # order found later to the group that placed it.
                   "orders": outcome.get("orders") or [],
                   "live_client_order_ids": outcome.get("live_client_order_ids") or [],
                   "at_ms": self.clock()}
        try:
            path = Path(self.stop_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        target = self.alert_notifier or self.notifier
        if target is not None:
            try:
                target.send(format_halt(payload))
            except Exception:  # noqa: BLE001 - delivery must not break the halt
                pass

    def _record_outcome(self, candidate: dict, outcome: dict, ts_ms: int) -> None:
        """Ledger the halts, notify the outcomes (rate limited, never fatal)."""
        if outcome.get("halt"):
            self._record_halt(candidate, outcome, ts_ms)
        if self.notifier is None:
            return
        key = f"{candidate.get('asset')}:{outcome.get('state')}"
        if not (outcome.get("executed") or outcome.get("halt")):
            # A refusal is worth one message, not one per cycle: the same
            # candidate stays in the alert cooldown window for minutes.
            cooldown_ms = int((self.policy.get("notify") or {})
                              .get("cooldown_seconds", 900)) * 1000
            last = self._notified.get(key)
            if last is not None and self.clock() - last < cooldown_ms:
                return
        self._notified[key] = self.clock()
        # A halt or a stranded leg is an operational message: it goes to the
        # room a person is expected to act in, not the one they scan for ideas.
        # A blocked pre-flight qualifies: the machine is fine, the *wallet* is
        # not, and only a person can fix it.
        actionable = (outcome.get("halt")
                      or outcome.get("state") in OPERATIONAL_STATES)
        target = ((self.alert_notifier or self.notifier) if actionable
                  else self.notifier)
        if target is None:
            return
        try:
            target.send(format_auto_result(candidate, outcome, ts_ms))
        except Exception:  # noqa: BLE001 - notification is never fatal
            pass

    def _record_halt(self, candidate: dict, outcome: dict, ts_ms: int) -> None:
        try:
            ledger = ExecutionLedger(self.ledger_path)
        except Exception:  # noqa: BLE001 - the halt file is the primary record
            return
        try:
            group = str(outcome.get("group_id") or
                        f"auto-{str(candidate.get('asset', 'unknown')).lower()}")
            ledger.append(group, str(outcome.get("state") or "auto_halt"), "auto_halt",
                          {"state": outcome.get("state"), "reason": outcome.get("reason"),
                           "asset": candidate.get("asset"),
                           "writes_started": outcome.get("writes_started"),
                           "exposure_possible": outcome.get("exposure_possible"),
                           "orders": outcome.get("orders") or [],
                           "live_client_order_ids": outcome.get("live_client_order_ids") or [],
                           "requires": "manual_reconciliation_before_auto_resumes"},
                          ts_ms)
        except Exception:  # noqa: BLE001
            pass
        finally:
            ledger.close()


def format_auto_result(candidate: dict, outcome: dict, ts_ms: int) -> str:
    """Feishu text for an automated execution.

    Deliberately *not* the alert layout: an alert says "there is an
    opportunity", this says "here is what the machine did about it". Keeping
    the two apart is the difference between a channel an operator can scan and
    one where a filled trade looks like a suggestion.
    """
    state = outcome.get("state")
    paper = state == "paper_hedged"
    plan = outcome.get("plan") or {}
    lines = [
        f"【自动执行·{'纸面' if paper else '实盘'}】{candidate.get('asset')} "
        f"{'已完成' if outcome.get('executed') else '未执行'}：{state}",
        f"模式: {outcome.get('mode') or outcome.get('order_mode') or 'auto'}",
        f"标的: {candidate.get('asset')} · "
        f"永续 {candidate.get('perp_venue')}:{candidate.get('perp_instrument')}",
        f"触发毛基差: {candidate.get('gross_entry_basis_bps')} bps "
        f"(门槛 {(candidate.get('roundtrip') or {}).get('breakeven_gross_bps')} bps)",
    ]
    if plan:
        lines += [
            f"下单前重取价: 链上 {plan.get('spot_ask_usd')} USD/枚 · "
            f"永续 Bid {plan.get('perp_bid_usd')} · 毛基差 "
            f"{plan.get('gross_entry_basis_bps')} bps",
            f"数量: {plan.get('share_count')} 股 = {plan.get('perp_contract_quantity')} 张 · "
            f"预计花费 {plan.get('size_quote')} {plan.get('quote_token')}",
        ]
    flow = outcome.get("flow") or {}
    if flow.get("maker"):
        maker = flow["maker"]
        lines.append(f"永续挂单: 挂 {maker.get('price')} × {maker.get('requested')} 张 → "
                     f"成交 {maker.get('filled')} 张（{maker.get('state')}）")
    if flow.get("fallback"):
        lines.append(f"超时兜底: {json.dumps(flow['fallback'], ensure_ascii=False)}")
    if flow.get("spot"):
        spot = flow["spot"]
        lines.append(f"现货腿: {spot.get('leg')} tx={spot.get('tx_hash')} "
                     f"数量 {spot.get('token_quantity')} 价格 {spot.get('price_usd')}")
    if flow.get("paper"):
        paper_row = flow["paper"]
        lines.append(
            f"纸面成交（假设挂单在盘口成交）: 卖永续 "
            f"{paper_row['perp']['size']} 张 @ {paper_row['perp']['price']} · "
            f"买现货 {paper_row['expected_token_quantity']} 枚 @ {paper_row['spot']['price']}")
        lines.append(f"预计开仓成本 {paper_row['entry_cost_bps']} bps（未产生任何真实成交）")
    if outcome.get("reason"):
        lines.append(f"原因: {outcome.get('reason')}")
    if outcome.get("group_id"):
        lines.append(f"账本组: {outcome['group_id']}")
    if outcome.get("state") in OPERATIONAL_STATES:
        lines += [
            "",
            "⚠ 现货腿前置条件不满足，永续单**没有挂出**：无需对平，自动执行也没有停止。",
            "处理：补足该支付币的授权额度（或钱包余额）后，下一轮会自行重试。",
        ]
    if outcome.get("halt"):
        lines += [
            "",
            "⚠ 已停止自动执行，等待人工对账。",
            f"解除方式: 对账后删除 {os.environ.get('ARB_AUTO_HALT', 'data/AUTO_HALT')}",
        ]
    lines.append(f"采样时间(ms): {ts_ms}")
    return "\n".join(lines)


def format_halt(payload: dict) -> str:
    return "\n".join([
        f"【自动执行·已停止】{payload.get('asset')} · {payload.get('state')}",
        f"原因: {payload.get('reason')}",
        f"账本组: {payload.get('group_id')}",
        "自动执行已写入停止文件，不会在下一轮重试。",
        "处理：先按账本对账两腿（现货持仓 + 永续持仓/挂单），确认后删除 "
        "data/AUTO_HALT 再恢复自动执行。",
    ])
