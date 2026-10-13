"""What was in flight when a cycle died.

Measured 2026-10-09 10:43:31: a QQQx probe crashed inside ``usdg_balance`` -- a
**balance read**, before the first step of the plan -- and ``guard_family``
wrote ``state=unknown`` plus a HALT. The machine stopped for two hours and the
board listed QQQx as an open position, while X Layer and Solana both held 0
QQQx: nothing had been bought, the coins had never left.

Reading a number and moving money are not the same event, so they no longer
share one outcome. Three levels, from the outside in:

``none``  no step that can move money has started. A crash here is a refusal:
          the lock stays free, the next round may try again, nothing halts.
``step``  a step declared ``Step(broadcasts=True)`` is running. It may have
          sent something and died in its own verification, so the honest answer
          is "we do not know where the coins are".
``sent``  something really went out and its id is recorded. Same answer as
          ``step``, but now the evidence names the transaction.

Only ``step`` and ``sent`` stop the machine.

The snapshot is also the evidence an investigator reads: which family, which
symbol, which step, which send, how long the attempt had been running. It is
written to ``data-v2/xstocks_v2_incident.json`` on every change, and a failed
write never breaks a trade -- status is not a leg.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_DIR = ROOT / "data-v2"
NAME = "xstocks_v2_incident.json"

#: No money-moving step has started.
NONE = "none"
#: Inside a step that can move money.
STEP = "step"
#: Something left our hands.
SENT = "sent"

_ORDER = {NONE: 0, STEP: 1, SENT: 2}

_STATE: dict = {}


def path(data_dir: Path | str | None = None) -> Path:
    return Path(data_dir or _STATE.get("data_dir") or DEFAULT_DATA_DIR) / NAME


def snapshot() -> dict:
    """A copy of the current attempt, safe to put in a ledger row."""
    return json.loads(json.dumps(_STATE, ensure_ascii=False, default=str))


def risk() -> str:
    """``none`` / ``step`` / ``sent`` -- how far this attempt got."""
    return str(_STATE.get("risk") or NONE)


def state() -> str:
    """``running`` while the attempt is alive, then how it ended."""
    return str(_STATE.get("state") or "")


def _raise_to(level: str) -> None:
    if _ORDER.get(level, 0) > _ORDER.get(risk(), 0):
        _STATE["risk"] = level


def begin(*, family: str = "", symbol: str = "", note: str = "",
          data_dir: Path | str | None = None) -> dict:
    """Start a fresh attempt. Called once per family call, never per leg."""
    _STATE.clear()
    _STATE.update({
        "risk": NONE,
        "state": "running",
        "state_detail": "",
        "finished_ms": 0,
        "family": str(family),
        "symbol": str(symbol),
        "note": str(note),
        "pid": os.getpid(),
        "started_ms": int(time.time() * 1000),
        "updated_ms": int(time.time() * 1000),
        "steps": [],
        "sends": [],
    })
    if data_dir is not None:
        _STATE["data_dir"] = str(data_dir)
    _write()
    return snapshot()


def finish(step_state: str, *, detail: str = "",
           data_dir: Path | str | None = None) -> dict:
    """Close the attempt: ``finished`` / ``halted`` / ``refused``.

    This is what makes the file readable as an *event* rather than as a
    running commentary. Without it, "a step is in flight right now" and "a cycle
    died after sending something" look identical on disk -- and the watcher
    raised an alert for a probe that was still walking, two minutes into a
    normal bridge (measured 2026-10-09 12:28).
    """
    if not _STATE:
        return {}
    _STATE["state"] = str(step_state)
    _STATE["state_detail"] = str(detail)[:200]
    _STATE["finished_ms"] = int(time.time() * 1000)
    if data_dir is not None and not _STATE.get("data_dir"):
        _STATE["data_dir"] = str(data_dir)
    _touch()
    return snapshot()


def enter_step(name: str, *, data_dir: Path | str | None = None) -> dict:
    """A step that can move money is starting.

    Called by ``executor.Executor.run`` for every ``broadcasts=True`` step, so
    every family inherits it -- a family script cannot slip a crash past the
    classification by forgetting to call this.
    """
    if not _STATE:
        begin(data_dir=data_dir)
    _STATE.setdefault("steps", []).append(
        {"step": str(name), "at_ms": int(time.time() * 1000)})
    _raise_to(STEP)
    _touch()
    return snapshot()


def sent(kind: str, detail: str = "", *,
         data_dir: Path | str | None = None) -> dict:
    """Something left our hands: a transaction, an order, a transfer.

    Called from the egress helpers (``broadcast()``, ``_sell()``), so it records
    one entry per real send rather than one per attempt.
    """
    if not _STATE:
        begin(data_dir=data_dir)
    _STATE.setdefault("sends", []).append(
        {"kind": str(kind), "detail": str(detail)[:200],
         "at_ms": int(time.time() * 1000)})
    _raise_to(SENT)
    _touch()
    return snapshot()


def _touch() -> None:
    _STATE["updated_ms"] = int(time.time() * 1000)
    _write()


def _write() -> None:
    # Only an attempt that declared where its evidence belongs is written down.
    # ``guard_family`` always declares it; a unit test that merely drives the
    # executor does not, and must not leave a fake attempt in the live tree
    # (measured 2026-10-09: a test run put ``steps: ["x", "x"]`` into
    # ``data-v2/xstocks_v2_incident.json`` while a real probe was in flight).
    if not _STATE or not _STATE.get("data_dir"):
        return
    try:
        target = path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(_STATE, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read(data_dir: Path | str | None = None) -> dict:
    """What the last attempt left behind, or ``{}`` when there is none."""
    try:
        return json.loads(path(data_dir).read_text())
    except (OSError, ValueError):
        return {}
