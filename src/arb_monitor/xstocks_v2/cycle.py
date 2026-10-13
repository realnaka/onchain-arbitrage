"""One in-flight cycle. Unknown and leftover inventory keep the lock."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

REFUSED = "refused"
REVERTED = "reverted"
SOLD = "sold"
STOPPED = "stopped"
UNKNOWN = "unknown"
PAPER = "paper"

_HOLD_STATES = frozenset({STOPPED, UNKNOWN})


class CycleBusy(RuntimeError):
    pass


class CycleLock:
    """Exclusive lock. ``hold=True`` leaves the file in place and writes HALT.

    The lock file keeps its old format -- the bare pid, which scripts and the
    gate already read -- and a *sidecar* (``xstocks_v2_cycle.json`` next to it)
    carries what a human actually needs while a cycle runs: which symbol, which
    step, since when. Measured 2026-10-07: a 25-minute bridge looked exactly like
    a stuck process, and the question "who deleted the lock" had no answer,
    because the lock had no history at all.
    """

    def __init__(self, path: Path, halt_path: Path, *, label: str = "",
                 symbol: str = "", family: str = ""):
        self.path = path
        self.halt_path = halt_path
        self.sidecar = Path(str(path) + ".json")
        self.label = label
        self.symbol = symbol
        self.family = family
        self.step = "starting"
        self.started_ms = 0
        self.hold = False

    def __enter__(self) -> "CycleLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise CycleBusy(str(self.path)) from exc
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        self.started_ms = int(time.time() * 1000)
        self.update(step="starting")
        return self

    def update(self, *, step: str | None = None, note: str = "",
               symbol: str | None = None, family: str | None = None) -> None:
        """Refresh the sidecar. Never fails a cycle: status is not a leg.

        Anything that cannot answer a *read* may not break a *trade*, so an
        error here is swallowed -- but a fresh file is what lets the board say
        "the bridge is crossing, 12 minutes in" instead of nothing.
        """
        if step:
            self.step = step
        if symbol:
            self.symbol = symbol
        if family:
            self.family = family
        payload = {
            "pid": os.getpid(),
            "label": self.label,
            "symbol": self.symbol,
            "family": self.family,
            "step": self.step,
            "note": note,
            "started_ms": self.started_ms or int(time.time() * 1000),
            "updated_ms": int(time.time() * 1000),
        }
        try:
            self.sidecar.write_text(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.hold:
            self.halt_path.write_text("halt\n")
            # The sidecar stays on purpose: it says where the coins are when a
            # cycle stopped holding the lock.
            return False
        self.path.unlink(missing_ok=True)
        self.sidecar.unlink(missing_ok=True)
        return False


def _alive(pid: int) -> bool:
    """Is this pid running? ``os.kill(0)`` is the only portable check here."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists, owned by someone else
    return True


def status(lock: Path | str, *, now_ms: int | None = None) -> dict:
    """What is in flight, in words a human can read.

    ``state`` is one of ``free`` (no lock), ``running`` (lock held by a live
    process, with its sidecar when there is one) or ``stale`` (lock left behind
    by a process that is gone -- the state a person has to clear, and the one the
    old format could not tell apart from a bridge in flight).
    """
    lock = Path(lock)
    now_ms = now_ms or int(time.time() * 1000)
    if not lock.exists():
        return {"state": "free", "lock": str(lock)}
    try:
        pid = int(lock.read_text().strip().split()[0])
    except (OSError, ValueError, IndexError):
        pid = 0
    out: dict = {"state": "running" if _alive(pid) else "stale", "pid": pid,
                 "lock": str(lock), "age_s": None, "step": "", "note": "",
                 "symbol": "", "family": "", "label": "", "started_ms": None}
    sidecar = Path(str(lock) + ".json")
    try:
        payload = json.loads(sidecar.read_text())
        if isinstance(payload, dict):
            out.update({k: payload.get(k) for k in
                        ("step", "note", "symbol", "family", "label",
                         "started_ms") if payload.get(k) is not None})
    except (OSError, ValueError):
        pass
    if out["started_ms"]:
        out["age_s"] = round((now_ms - out["started_ms"]) / 1000, 1)
    return out


def open_gate(*, halt: Path, lock: Path, v1_busy: bool,
              kill: bool = False, auto_halt: bool = False) -> tuple[bool, str]:
    if kill:
        return False, "kill"
    if auto_halt:
        return False, "auto_halt"
    if halt.exists():
        return False, "halt"
    if lock.exists():
        # A lock is another cycle in flight, not a human-required stop. Reading
        # "halt" for both made the board say a person was needed when a bridge
        # was simply mid-crossing (measured 2026-10-07: a live SPYx run held the
        # lock and the paper runner reported ``halt`` for ~25 minutes).
        return False, "lock"
    if v1_busy:
        return False, "v1_live_busy"
    return True, "ok"


def send_again(*, broadcast_unknown: bool, approve_race: bool, attempts: int) -> bool:
    """Unknown broadcasts are never retried. An approve race is retried once."""
    if broadcast_unknown:
        return False
    return approve_race and attempts == 1


def finish_state(state: str, lock: CycleLock) -> None:
    if state in _HOLD_STATES:
        lock.hold = True


def symbol_halt(directory: Path, symbol: str, status: str) -> None:
    """A bad credit ratio stops one symbol. It does not write the global HALT."""
    if status != "symbol_halt":
        return
    directory.mkdir(parents=True, exist_ok=True)
    (directory / symbol).write_text("credit_mismatch\n")


def symbol_halted(directory: Path, symbol: str) -> bool:
    return (directory / symbol).exists()


# ---------------------------------------------------------------------------
# v1 coexistence
# ---------------------------------------------------------------------------
# v1 owns the Kraken key and both chain wallets, and Kraken nonces must be
# strictly increasing. Two processes signing at once manufacture exactly the
# unknown state this file exists to prevent, so every broadcast checks v1 first.
V1_LOCK_NAMES = ("xstocks_cycle.lock", "xstocks_eth_cycle.lock")
V1_PROCESS_PATTERNS = ("xstocks_auto_live.py", "xstocks_eth_auto_live.py")


def v1_busy(v1_root: Path | str, *, patterns=V1_PROCESS_PATTERNS,
            lock_names=V1_LOCK_NAMES, check_processes: bool = True,
            runner=subprocess.run) -> tuple[bool, str]:
    """Is v1 holding a lock or running an auto chain?

    Returns ``(busy, reason)``. A missing ``pgrep`` is reported as busy rather
    than assumed idle: the v1 runner once reported "not running" from inside a
    sandbox that could not see processes at all.
    """
    root = Path(v1_root)
    for name in lock_names:
        if (root / "data" / name).exists():
            return True, f"v1 lock {name}"
    if not check_processes:
        return False, ""
    for pattern in patterns:
        try:
            result = runner(["pgrep", "-f", pattern], capture_output=True, text=True)
        except (OSError, FileNotFoundError):
            return True, "pgrep unavailable"
        if result.returncode == 0 and (result.stdout or "").strip():
            return True, f"v1 process {pattern}"
    return False, ""


def halt_paths(data_dir: Path | str) -> dict[str, Path]:
    """v2's own locks and stop file. Same semantics as v1, separate directory."""
    directory = Path(data_dir)
    return {
        "lock": directory / "xstocks_v2_cycle.lock",
        "halt": directory / "xstocks_v2_HALT",
        "kill": directory / "KILL_SWITCH",
        "auto_halt": directory / "AUTO_HALT",
        "symbols": directory / "symbol_halts",
    }
