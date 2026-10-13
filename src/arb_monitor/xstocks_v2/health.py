"""Is the runner actually still running?

Lesson 6.3: the v1 monitor once stopped for six hours and nobody noticed,
because a silent process and a quiet market look identical from the outside.
A resident runner therefore has to publish a heartbeat, and something has to
compare that heartbeat against wall time.
"""
from __future__ import annotations

from pathlib import Path

from arb_monitor.xstocks_v2.store import read_json

OK = "ok"
STALE = "stale"
NEVER = "never"


def heartbeat(state: dict | None, *, now_ms: int, max_age_ms: int) -> tuple[str, int | None]:
    """``(status, age_ms)`` for one runner's state file.

    Different writers name their timestamp differently (``updated_ms`` for the
    paper state, ``generated_ms`` for a scan snapshot), so all of the known
    names are accepted rather than forcing every writer to rename a field.
    """
    if not isinstance(state, dict):
        return NEVER, None
    stamp = None
    for key in ("updated_ms", "generated_ms", "measured_ms"):
        if state.get(key) is not None:
            stamp = state[key]
            break
    try:
        stamp = int(stamp)
    except (TypeError, ValueError):
        return NEVER, None
    age = max(0, now_ms - stamp)
    return (OK if age <= max_age_ms else STALE), age


def check(*, paths, now_ms: int, interval_s: float, tolerance: float = 3.0) -> dict:
    """Check one or more state files. Missing files are reported, not ignored."""
    budget = int(interval_s * 1000 * tolerance)
    out: dict[str, dict] = {}
    worst = OK
    for name, path in paths.items():
        status, age = heartbeat(read_json(path, None), now_ms=now_ms, max_age_ms=budget)
        out[name] = {"status": status, "age_ms": age, "budget_ms": budget}
        if status != OK and worst == OK:
            worst = status
    return {"status": worst, "budget_ms": budget, "runners": out}


def alert_text(report: dict) -> str:
    """A one-line reason, or an empty string when everything is healthy."""
    if report.get("status") == OK:
        return ""
    parts = []
    for name, item in (report.get("runners") or {}).items():
        if item.get("status") == NEVER:
            parts.append(f"{name} 没有心跳文件")
        elif item.get("status") == STALE:
            parts.append(f"{name} 心跳 {item.get('age_ms')} ms 超过 {item.get('budget_ms')} ms")
    return "；".join(parts)
