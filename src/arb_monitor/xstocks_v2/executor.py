"""Cycle state machine for a multi-leg path. Pure transitions, injected legs.

The executor does not know how to buy, bridge or sell. It knows the *rules*
that must hold around a leg that moves money:

* a broadcast needs the gate to pass first, every time, not once per cycle;
* an outcome of ``unknown`` (a broadcast whose result nobody can see) stops
  everything: the lock is kept and a HALT file is written;
* a trade that stops with the coins in a known wallet stops only that symbol.
  The other symbols keep going. A signer that refused before any hash existed
  (a zero amount, not enough gas) stops nothing;
* a leg that returns success is not success until ``verify`` re-reads the
  world (2026-10-07: ``swap execute`` returned ``ok:true`` with a txHash for a
  transaction the wallet backend never broadcast);
* a refusal or a reverted atomic leg leaves no inventory, so it may release the
  lock and the next round may try again;
* ``retry_once`` exists for the races we have measured (an approve that raced
  its swap, a delegated account over its in-flight limit), and for nothing else.

States match the v2 spec section 8.1 and the v1 ledger:
``refused`` / ``reverted`` / ``sold`` / ``stopped`` / ``unknown``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Callable

from arb_monitor.xstocks_v2 import guards, incident, ledger

OK = "ok"
REFUSED = ledger.REFUSED
REVERTED = ledger.REVERTED
RETRY = "retry"
STOPPED = ledger.STOPPED
UNKNOWN = ledger.UNKNOWN
SOLD = ledger.SOLD

# Outcomes a runner may return. ``stopped`` is "we put the coins somewhere on
# purpose and will not continue"; ``unknown`` is "we do not know where they are".
RUNNER_RESULTS = frozenset({OK, REFUSED, REVERTED, RETRY, STOPPED, UNKNOWN})


@dataclass
class Outcome:
    status: str
    detail: str = ""
    held_at: str = ""
    observed: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.status not in RUNNER_RESULTS:
            raise ValueError(f"unknown outcome status {self.status!r}")


@dataclass
class Step:
    """One leg.

    ``held_at`` is where the coins are if the process dies *after* this step
    stops: ``wallet`` / ``in_bridge`` / ``dest_wallet`` / ``kraken`` / ``unknown``.
    ``broadcasts`` says whether real money moves; those steps need the gate.
    """

    name: str
    run: Callable[[], Outcome]
    held_at: str = "unknown"
    verify: Callable[[Outcome], Outcome] | None = None
    broadcasts: bool = False
    retry_once: bool = False
    retry_reason: str = ""
    #: True when a successful run of this step leaves us holding the token.
    #: A later refusal then means ``stopped`` (we own something), not ``refused``
    #: (flat). Measured 2026-10-07: the buy filled and the withdrawal was
    #: rejected for a settlement lag; calling that "refused" hid real inventory.
    inventory: bool = False


class Executor:
    def __init__(self, *, symbol: str, chain: str, family: str, mode: str,
                 size_usdt, tokens, steps: list[Step],
                 gate: Callable[[], tuple[bool, str]],
                 ledger_path: Path | str = ledger.DEFAULT_PATH,
                 halt_path: Path | str | None = None,
                 symbol_halt_dir: Path | str | None = None,
                 notify: Callable[[str], object] | None = None,
                 progress: Callable[..., object] | None = None,
                 reconcile: Callable[[], tuple] | None = None,
                 realized: Callable[[], dict | None] | None = None,
                 max_bps: Decimal = Decimal("15"),
                 final_state: str = SOLD,
                 final_reason: str = "both legs done",
                 final_held_at: str = ""):
        self.symbol = symbol
        self.chain = chain
        self.family = family
        self.mode = mode
        self.size_usdt = size_usdt
        self.tokens = tokens
        self.steps = list(steps)
        self.gate = gate
        self.ledger_path = Path(ledger_path)
        self.halt_path = Path(halt_path) if halt_path else None
        # A symbol file stops that symbol only. The global HALT file is reserved
        # for a broadcast we cannot see (operator 2026-10-10: one stopped trade
        # must not stop every other chain).
        self.symbol_halt_dir = Path(symbol_halt_dir) if symbol_halt_dir else None
        # A HALT that nobody hears about is the dangerous state: the lock stays
        # and the next round silently refuses. v2 had no notification path until
        # 2026-10-07; ``notify`` is called on every hold state.
        self.notify = notify
        # ``progress`` is called with each step's name (and, for a broadcast
        # step, its outcome). The lock uses it to write "which step is in flight"
        # next to the pid, so a 25-minute bridge does not look like a hang.
        self.progress = progress
        # ``reconcile`` is the prediction-vs-realized gate (operator 2026-10-07):
        # it returns ``(predicted_usd, realized_usd, notional_usd)`` for a closed
        # cycle. A mismatch does not keep the lock -- it warns, because the coins
        # already moved -- but it is the only place that compares what we said
        # would happen with what happened.
        self.reconcile = reconcile
        # ``realized`` answers "what did this cycle actually earn?" from the
        # venue's own numbers (cash in, cash out, the fees that are not inside
        # either). It runs once, on a finished cycle, and its parts land in the
        # ledger row's ``costs.realized`` -- a missing part keeps the net
        # ``None`` instead of turning it into a zero (2026-10-07: three of the
        # day's four ``sold`` rows carried no net at all).
        self.realized = realized
        self.max_bps = Decimal(max_bps)
        # A path that ends holding a token on purpose is not ``sold``. A probe
        # (buy the minimum, cross, stop) proves the route and leaves the coins
        # in our own wallet; calling that "sold" would put a fiction in the
        # ledger, and the next trade has to know the inventory is already there.
        self.final_state = final_state
        self.final_reason = final_reason
        # Where the coins are once the *last* step has verified, which is not
        # the same as ``last_inventory``: a bridge step's ``held_at`` is its
        # worst case while in flight ("in_bridge"), and a verified arrival has
        # left that state behind (measured 2026-10-07: the NVDAx probe wrote
        # ``held_at: in_bridge`` for tokens already sitting in the wallet).
        self.final_held_at = final_held_at
        self.rows: list[dict] = []
        self.state = None
        self.held_at = ""
        self.reason = ""
        self.keep_lock = False
        self.retried: list[str] = []
        self.reconcile_result: dict | None = None

    # -- bookkeeping ------------------------------------------------------
    def _record(self, *, state: str, held_at: str = "", reason: str = "",
                step: str = "", costs: dict | None = None,
                extra: dict | None = None) -> None:
        entry = ledger.row(
            symbol=self.symbol, chain=self.chain, family=self.family,
            state=state, mode=self.mode, size_usdt=self.size_usdt,
            tokens=self.tokens, held_at=held_at, reason=reason,
            costs=costs, extra=dict(extra or {}, step=step),
            # The row has to carry where the coins ended up, or
            # ``ledger.effective_held_at`` answers with the legacy Solana
            # default (``dest_wallet``) for a probe whose slice is on Kraken.
            final_held_at=(self.final_held_at if state == self.final_state
                           else ""))
        ledger.record(entry, self.ledger_path)
        self.rows.append(entry)

    def _stop_symbol(self, reason: str) -> None:
        """Remember this symbol. Do not write the global HALT file."""
        if self.symbol_halt_dir is None or not self.symbol:
            return
        self.symbol_halt_dir.mkdir(parents=True, exist_ok=True)
        (self.symbol_halt_dir / self.symbol).write_text(
            f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {reason} "
            f"held_at={self.held_at}\n")

    def _halt(self, reason: str, held_at: str) -> None:
        if self.halt_path is not None:
            self.halt_path.parent.mkdir(parents=True, exist_ok=True)
            self.halt_path.write_text(
                f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {self.symbol} "
                f"{self.family} {reason} held_at={held_at}\n")

    def _notify(self, *, state: str, held_at: str, reason: str) -> None:
        if self.notify is None:
            return
        try:
            self.notify(state=state, held_at=held_at, reason=reason)
        except Exception:  # noqa: BLE001
            pass          # losing a message must never break the cycle

    def _progress(self, **fields) -> None:
        """Report progress. A status writer never breaks a trade."""
        if self.progress is None:
            return
        try:
            self.progress(**fields)
        except Exception:  # noqa: BLE001
            pass

    def _run_reconcile(self) -> dict:
        """Predicted vs realized for the closed cycle. Warn, never halt.

        The coins have already moved, so a mismatch is not a reason to keep the
        lock: it is a reason to tell a human that the model and the world differ.
        An exception in the prediction itself is reported as a failed reconcile
        rather than a crashed cycle.
        """
        try:
            answer = self.reconcile()
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"[:200]
            self._notify(state="reconcile_error", held_at="", reason=detail)
            return {"ok": False, "error": detail}
        if answer is None:
            # No prediction was available (no quote, no reference price). An
            # unmeasured cycle is written down as unmeasured rather than
            # compared against a number that was never earned.
            return {"measured": False}
        try:
            predicted, realized, notional = answer
        except (TypeError, ValueError) as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"[:200]
            self._notify(state="reconcile_error", held_at="", reason=detail)
            return {"ok": False, "error": detail}
        predicted = Decimal(str(predicted))
        realized = Decimal(str(realized))
        notional = Decimal(str(notional))
        ok = guards.reconciliation_ok(predicted=predicted, realized=realized,
                                      notional=notional, max_bps=self.max_bps)
        record = {
            "predicted_usd": str(predicted), "realized_usd": str(realized),
            "notional_usd": str(notional), "max_bps": str(self.max_bps),
            "ok": ok,
        }
        if not ok:
            self._notify(
                state="reconcile_mismatch", held_at="",
                reason=f"predicted {predicted} realized {realized} "
                       f"notional {notional} > {self.max_bps} bps")
        return record

    def _finish(self, state: str, reason: str, held_at: str = "",
                *, keep_lock: bool, extra: dict | None = None) -> dict:
        self.state = state
        self.reason = reason
        self.held_at = (held_at if state in ledger.HOLD_STATES | ledger.INVENTORY_STATES
                        else "")
        self.keep_lock = keep_lock
        if state == UNKNOWN:
            self._halt(reason, self.held_at)
            self._notify(state=state, held_at=self.held_at, reason=reason)
        elif state == STOPPED:
            self._stop_symbol(reason)
            self._notify(state=state, held_at=self.held_at, reason=reason)
        payload = {"retried": self.retried}
        if extra:
            payload.update(extra)
        costs = None
        if self.realized is not None and state == SOLD:
            try:
                answer = self.realized()
            except Exception as exc:  # noqa: BLE001
                answer = {"net_usdt": None, "measured": False,
                          "missing": [f"realized_error:{type(exc).__name__}"],
                          "parts": {}}
            if answer:
                costs = {"realized": answer}
                payload["realized"] = answer
        self._record(state=state, held_at=self.held_at, reason=reason,
                     step="finish", extra=payload, costs=costs)
        return self.result()

    def result(self) -> dict:
        return {
            "state": self.state,
            "held_at": self.held_at,
            "reason": self.reason,
            "keep_lock": self.keep_lock,
            "halt": self.state == UNKNOWN,
            "broadcast": any(step.broadcasts for step in self.steps),
            "symbol": self.symbol,
            "family": self.family,
            "reconcile": self.reconcile_result,
        }

    # -- the machine ------------------------------------------------------
    def _run_step(self, step: Step) -> Outcome:
        """Run one leg, verify it, and allow exactly one retry.

        The retry may be requested by the runner *or* by ``verify``. That
        matters: the 2026-10-07 wallet backend returned ``ok:true`` with a
        txHash for a transaction it never broadcast, and only ``verify`` (via
        ``wallet history``) could see the ``ERROR``. A retry decided there has
        to be honoured too, otherwise a recoverable failure becomes a HALT.
        """
        attempts = 0
        while True:
            attempts += 1
            try:
                outcome = step.run()
            except Exception as exc:  # noqa: BLE001
                outcome = classify_exception(exc, broadcast=step.broadcasts)
            if outcome.status == OK and step.verify is not None:
                try:
                    verified = step.verify(outcome)
                except Exception as exc:  # noqa: BLE001
                    outcome = classify_exception(exc, broadcast=step.broadcasts)
                else:
                    # Keep what the runner saw *and* what verify saw. Overwriting
                    # one with the other lost the tx hash of every verified leg
                    # (measured 2026-10-07: the sold cycle's bridge and sell rows
                    # carried no hash, so the closure had to be re-derived from
                    # the chain by hand).
                    merged = dict(outcome.observed or {})
                    merged.update(verified.observed or {})
                    verified.observed = merged
                    outcome = verified
            if outcome.status != RETRY or not step.retry_once or attempts >= 2:
                return outcome
            self.retried.append(f"{step.name}:{outcome.detail}")
            self._record(state=REFUSED, reason=f"retry:{outcome.detail}",
                         step=step.name,
                         extra={"attempt": attempts, **_observed_extra(outcome)})

    def run(self) -> dict:
        last_inventory = ""
        for step in self.steps:
            self._progress(step=step.name, symbol=self.symbol, family=self.family)
            if step.broadcasts:
                allowed, why = self.gate()
                if not allowed:
                    self._record(state=REFUSED, reason=f"gate:{why}", step=step.name)
                    return self._finish(REFUSED, f"gate:{why}", keep_lock=False)
                # From here on a crash may be "we sent something and do not
                # know what became of it". Recorded *before* the leg runs, so
                # an exception inside the leg cannot claim to be harmless
                # (2026-10-09: the outer guard called a balance read a position).
                incident.enter_step(step.name)

            # A crash inside a leg that broadcasts is an unknown, never a silent
            # traceback: the 2026-10-07 run crashed in the sell leg after the
            # bridge had been broadcast, and the exception released the lock
            # with real coins in flight.
            outcome = self._run_step(step)

            if outcome.status == OK:
                if step.inventory:
                    last_inventory = step.held_at or last_inventory
                self._record(state=OK, held_at="", reason="",
                             step=step.name, extra={"observed": outcome.observed})
                continue
            if outcome.status == REFUSED:
                if last_inventory:
                    return self._finish(STOPPED, f"{step.name}:{outcome.detail}",
                                        held_at=last_inventory, keep_lock=False,
                                        extra=_observed_extra(outcome))
                return self._finish(REFUSED, f"{step.name}:{outcome.detail}",
                                    keep_lock=False, extra=_observed_extra(outcome))
            if outcome.status == REVERTED:
                if last_inventory:
                    return self._finish(STOPPED, f"{step.name}:{outcome.detail}",
                                        held_at=last_inventory, keep_lock=False,
                                        extra=_observed_extra(outcome))
                return self._finish(REVERTED, f"{step.name}:{outcome.detail}",
                                    keep_lock=False,
                                    extra=_observed_extra(outcome))
            if outcome.status == STOPPED:
                return self._finish(STOPPED, f"{step.name}:{outcome.detail}",
                                    held_at=outcome.held_at or step.held_at,
                                    keep_lock=False, extra=_observed_extra(outcome))
            if pre_sign_refusal(outcome):
                # The step was labelled as a broadcast, but the signer refused
                # before a hash existed. That is not an in-flight transaction.
                if last_inventory:
                    return self._finish(STOPPED, f"{step.name}:{outcome.detail}",
                                        held_at=last_inventory, keep_lock=False,
                                        extra=_observed_extra(outcome))
                return self._finish(REFUSED, f"{step.name}:{outcome.detail}",
                                    keep_lock=False, extra=_observed_extra(outcome))
            # A retry we did not take again, or an explicit unknown. A broadcast
            # may have left the machine. This is the only case that stops
            # every other symbol.
            return self._finish(UNKNOWN, f"{step.name}:{outcome.detail or outcome.status}",
                                held_at=outcome.held_at or step.held_at or "unknown",
                                keep_lock=True, extra=_observed_extra(outcome))
        extra: dict = {}
        if self.reconcile is not None:
            self.reconcile_result = self._run_reconcile()
            extra["reconcile"] = self.reconcile_result
        return self._finish(self.final_state, self.final_reason,
                            held_at=(self.final_held_at or last_inventory
                                     if self.final_state in ledger.INVENTORY_STATES
                                     else ""),
                            keep_lock=False, extra=extra)


#: Wallet answers that mean "refused before signing". Measured 2026-10-09
#: 19:12: ``Wallet API error (code=10004): This address doesn’t have enough
#: native tokens.`` came back with ``ok: false`` and no txHash, and the nonce
#: did not move.
WALLET_REFUSAL_MARKERS = ("code=10004", "doesn’t have enough native",
                          "doesn't have enough native")
#: Answers that mean the signer never produced a hash. 2026-10-10 09:07: a
#: zero-amount buy was retried, then recorded as ``unknown``, and the global
#: HALT file stopped every family.
PRE_SIGN_MARKERS = WALLET_REFUSAL_MARKERS + (
    "must be greater than zero",
    "below_min_notional",
)


def pre_sign_refusal(outcome: Outcome) -> bool:
    """True when the signer refused and no transaction hash came back."""
    if outcome.status not in (RETRY, UNKNOWN):
        return False
    text = outcome.detail or ""
    if not any(marker in text for marker in PRE_SIGN_MARKERS):
        return False
    for value in (outcome.observed or {}).values():
        if isinstance(value, str) and value.startswith("0x") and len(value) >= 20:
            return False
    return True


def _observed_extra(outcome: Outcome) -> dict:
    """What the leg saw, on the row that ends the cycle.

    Measured 2026-10-10 14:17: the Ethereum probe's buy step came back ``ok``
    with a txHash, the verifier waited a minute, the token balance had not
    moved, and ``wallet history`` had never heard of the hash. The leg ended
    ``unknown`` and stopped every chain -- and the hash went *nowhere*: no
    ``ok`` row is written for a leg whose verify failed, the finish row carried
    only ``step=finish``, and the incident file's ``sends`` list was empty
    because this family's buy leg never called ``incident.sent``. Reconciling
    it (nothing was bought, nothing was in flight) took a hand-written chain
    query against the wallet nonce. The answer stays ``unknown``; the evidence
    now travels with it, so the next reconciliation starts from a hash.
    """
    return {"observed": dict(outcome.observed)} if outcome.observed else {}


def broadcast_failure(error: object, *, nonce_before: int | None,
                      nonce_after: int | None, held_at: str = "wallet",
                      label: str = "broadcast") -> Outcome:
    """Outcome for a broadcast call that raised instead of returning a hash.

    ``REFUSED`` needs both: the wallet's own refusal text, and a pending nonce
    that did not move. Anything else stays ``UNKNOWN`` -- a hash the chain may
    still see is not a refusal (lessons: onchainos sometimes returns a hash
    that never lands, and sometimes lands without returning one).
    """
    text = str(error or "")
    refused = any(marker in text for marker in WALLET_REFUSAL_MARKERS)
    if refused and nonce_before is not None and nonce_after == nonce_before:
        return Outcome(REFUSED, f"wallet_refused:{label}:{text[:160]}")
    return Outcome(UNKNOWN, f"{label}:{text[:160]}", held_at=held_at)


def classify_exception(exc: BaseException, *, broadcast: bool = False) -> Outcome:
    """Map a raised error to the safe outcome.

    A refused pre-flight is not an unknown. Anything raised *after* something
    was broadcast is unknown until a human re-reads the account.
    """
    text = f"{type(exc).__name__}: {exc}"
    return Outcome(UNKNOWN if broadcast else REFUSED, text[:300])


def parse_wallet_status(payload: dict, tx_hash: str = "") -> str:
    """``txStatus`` for one hash out of a ``wallet history`` answer.

    ``ok:true`` with a hash is not proof of a broadcast: the wallet backend can
    answer ``ERROR`` for a hash it never sent (2026-10-07, twice). ``ERROR`` is
    the only status that licenses a retry, because nothing left the wallet.
    """
    for row in (payload or {}).get("data") or []:
        if not isinstance(row, dict):
            continue
        if not tx_hash or row.get("txHash") == tx_hash:
            return str(row.get("txStatus") or "")
    return ""
