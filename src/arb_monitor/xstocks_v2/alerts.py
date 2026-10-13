"""What counts as an alert, read from files rather than from a running process.

The point of this module is that the *same* list can be produced by the resident
watcher, by a manual ``once`` run and by a test. It never imports a venue, never
calls a CLI and never writes anything: it reads the state the runners already
leave on disk and turns it into a small, stable list of facts.

That matters because the alternative -- asking the trader loop "are you in
trouble?" -- only works while the trader loop is alive, and the interesting
failure is the one where it is not.

Two rules, both learned the hard way:

* **A designed hold is not an alert.** Sample inventory booked as ``probed`` is
  supposed to sit in our wallet; only ``stopped``/``unknown`` mean a human (or a
  machine) has to look (2026-10-07, ``ledger.open_positions``).
* **Not being able to read is a fact about us.** A wallet read that fails is
  reported as ``wallet_unreadable`` and never as an empty wallet (A42), so the
  count of those is an alert in its own right -- a proxy that drops reads will
  silently starve every decision downstream.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from arb_monitor.xstocks_v2 import cycle, incident, ledger

# Kinds. The first four stop the machine; the rest are "look at this".
KILL_SWITCH = "kill_switch"
AUTO_HALT = "auto_halt"
HALT = "halt"
STALE_LOCK = "stale_lock"
HELD = "held_position"
INCIDENT = "incident"
WALLET_UNREADABLE = "wallet_unreadable"
RUNNER_STALE = "runner_stale"

SEVERITY = {
    KILL_SWITCH: "stop",
    AUTO_HALT: "stop",
    HALT: "stop",
    STALE_LOCK: "stop",
    HELD: "hold",
    INCIDENT: "hold",
    WALLET_UNREADABLE: "warn",
    RUNNER_STALE: "warn",
}

#: How many failed wallet reads inside the window make a noise.
WALLET_UNREADABLE_MIN = 3
WALLET_WINDOW_S = 3600.0
#: A runner whose state file has not moved for this long is stuck.
RUNNER_STALE_S = 900.0
#: An attempt with no ``state`` that has been silent this long counts as dead.
#: A bridge step can legitimately take ~26 minutes without touching the file.
INCIDENT_ABANDONED_S = 3600.0


def fingerprint(alert: dict) -> str:
    """A stable id for "this same problem", so one alert investigates once.

    Deliberately coarse: the symbol and the kind, not the message. The same
    crash with a different stack pointer is the same problem, and a fingerprint
    that changed on every retry would burn the daily budget in a minute.
    """
    parts = [str(alert.get("kind") or ""), str(alert.get("symbol") or ""),
             str(alert.get("family") or "")]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def collect(*, data_dir: Path | str, root: Path | str | None = None,
            now_ms: int | None = None, ledger_limit: int = 600) -> list[dict]:
    """Every alert the on-disk state currently supports. Sorted, deduped."""
    data_dir = Path(data_dir)
    root = Path(root) if root is not None else data_dir.parent
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    out: list[dict] = []

    # Paths come from the cycle module, never from a string written here: two
    # spellings of "the halt file" is how a watcher ends up watching nothing.
    paths_of = cycle.halt_paths(data_dir)
    for kind, path, note in (
            (KILL_SWITCH, paths_of["kill"], "v2 停一切新单"),
            (AUTO_HALT, paths_of["auto_halt"], "v2 只挡开仓"),
            (KILL_SWITCH, root / "data" / "KILL_SWITCH", "v1 停一切新单"),
            (AUTO_HALT, root / "data" / "AUTO_HALT", "v1 只挡开仓"),
            (HALT, paths_of["halt"], "v2 已停开仓")):
        if path.exists():
            out.append({"kind": kind, "severity": SEVERITY[kind],
                        "detail": f"{note}：{path.name}",
                        "path": str(path),
                        "since_ms": _mtime_ms(path)})

    lock_state = cycle.status(paths_of["lock"])
    if lock_state.get("state") == "stale":
        out.append({"kind": STALE_LOCK, "severity": SEVERITY[STALE_LOCK],
                    "symbol": str(lock_state.get("symbol") or ""),
                    "family": str(lock_state.get("family") or ""),
                    "detail": (f"锁还在但 pid {lock_state.get('pid')} 已退出，"
                               f"停在步骤 {lock_state.get('step')}"),
                    "path": str(paths_of["lock"]),
                    "since_ms": lock_state.get("started_ms")})

    paths = ledger.recent(data_dir / ledger.DEFAULT_PATH.name, limit=ledger_limit)
    for entry in ledger.open_positions(data_dir / ledger.DEFAULT_PATH.name):
        # Only hold states: ``probed`` inventory is held on purpose.
        if str(entry.get("state")) not in ledger.HOLD_STATES:
            continue
        out.append({"kind": HELD, "severity": SEVERITY[HELD],
                    "symbol": str(entry.get("symbol") or ""),
                    "family": str(entry.get("family") or ""),
                    "detail": (f"{entry.get('symbol')} {entry.get('state')} @ "
                               f"{ledger.effective_held_at(entry)}："
                               f"{str(entry.get('reason') or '')[:160]}"),
                    "since_ms": int(entry.get("ts_ms") or 0)})

    unreadable = [row for row in paths
                  if str(row.get("reason") or "").startswith(WALLET_UNREADABLE)
                  and int(row.get("ts_ms") or 0) >= now_ms - WALLET_WINDOW_S * 1000]
    if len(unreadable) >= WALLET_UNREADABLE_MIN:
        out.append({"kind": WALLET_UNREADABLE, "severity": SEVERITY[WALLET_UNREADABLE],
                    "detail": (f"最近 {int(WALLET_WINDOW_S // 60)} 分钟有 "
                               f"{len(unreadable)} 行钱包读失败"
                               "（读不到不是空钱包）"),
                    "count": len(unreadable),
                    "since_ms": int(unreadable[0].get("ts_ms") or 0)})

    for name, label in (("xstocks_v2_live.json", "live"),
                        ("xstocks_v2_paper.json", "paper")):
        path = data_dir / name
        try:
            age_ms = now_ms - int(json.loads(path.read_text()).get("updated_ms") or 0)
        except (OSError, ValueError, TypeError):
            continue
        if age_ms > RUNNER_STALE_S * 1000:
            out.append({"kind": RUNNER_STALE, "severity": SEVERITY[RUNNER_STALE],
                        "symbol": label,
                        "detail": f"{label} 状态停在 {age_ms / 1000:.0f} s 前",
                        "path": str(path), "age_s": round(age_ms / 1000, 1)})

    evidence = incident.read(data_dir)
    # Only a *closed* attempt counts. The same file is written while a cycle is
    # walking -- entering a step, sending a transaction -- and "money is moving
    # right now" is not an alert; "a cycle died with money in flight" is
    # (2026-10-09 12:28: the first version alerted on a healthy probe two
    # minutes into a normal bridge).
    if _incident_halted(evidence, now_ms):
        out.append({"kind": INCIDENT, "severity": SEVERITY[INCIDENT],
                    "symbol": str(evidence.get("symbol") or ""),
                    "family": str(evidence.get("family") or ""),
                    "detail": (f"上一次尝试崩在动钱之后（{_incident_why(evidence, now_ms)}）："
                               f"risk={evidence.get('risk')} "
                               f"steps={[s.get('step') for s in evidence.get('steps') or []]} "
                               f"sends={[s.get('kind') for s in evidence.get('sends') or []]}"),
                    "path": str(incident.path(data_dir)),
                    "since_ms": int(evidence.get("finished_ms")
                                    or evidence.get("updated_ms") or 0)})

    for alert in out:
        alert.setdefault("fingerprint", fingerprint(alert))
    out.sort(key=lambda item: ({"stop": 0, "hold": 1, "warn": 2}
                               .get(item["severity"], 3), item["kind"]))
    return out


def summary(alerts: list[dict]) -> dict:
    """Counts for a board line: how loud is it right now."""
    counts: dict[str, int] = {}
    for alert in alerts:
        counts[alert["kind"]] = counts.get(alert["kind"], 0) + 1
    return {"total": len(alerts), "by_kind": counts,
            "stops": sum(1 for a in alerts if a["severity"] == "stop")}


def _incident_halted(evidence: dict, now_ms: int) -> bool:
    """Did the last attempt die with money in flight?

    ``state == "halted"`` is the explicit answer. The fallback exists because
    that field is new (2026-10-09): an evidence file written by an older process
    has no ``state`` at all, and a check that only looks for the new key would
    stay silent for exactly the crash it was written to catch. So a file whose
    writer is gone -- or that has not moved for an hour -- also counts. The
    window is wide on purpose: a bridge step can take ~26 minutes without
    touching the file, and a false alarm here halts nothing that is not already
    stopped.
    """
    if not evidence:
        return False
    state = str(evidence.get("state") or "")
    if state:
        return state == "halted"
    if _alive(int(evidence.get("pid") or 0)):
        return (now_ms - int(evidence.get("updated_ms") or 0)
                > INCIDENT_ABANDONED_S * 1000)
    return True


def _incident_why(evidence: dict, now_ms: int) -> str:
    if str(evidence.get("state") or "") == "halted":
        return "已写 HALT"
    if not _alive(int(evidence.get("pid") or 0)):
        return f"写它的进程 {evidence.get('pid')} 已退出"
    return "证据文件已超过 1 小时没动"


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _mtime_ms(path: Path) -> int:
    try:
        return int(path.stat().st_mtime * 1000)
    except OSError:
        return 0
