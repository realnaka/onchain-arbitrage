#!/usr/bin/env python
"""Watch the on-disk state and send Codex after anything that looks wrong.

Operator 2026-10-09: the periodic patrol should be replaced by an event. This is
that event loop, and it is deliberately the dumbest possible one:

    read the files the runners already write -> if something is wrong, start a
    read-only investigation -> tell the operator once when the answer is in.

It is a *separate process* on purpose. The runners stop themselves and write a
HALT; if the watcher lived inside the runner, the alert it is meant to catch --
a runner that died, hung or never woke up -- would take the watcher with it. And
it never writes a switch, never trades and never restarts anything: those are
the operator's, and a program that both detects a problem and starts the thing
that had the problem is how a loop of restarts hides a real failure.

    scripts/xstocks_v2_watch.py once [--dry-run]     # one pass, prints JSON
    scripts/xstocks_v2_watch.py run  [--interval 30] # resident
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.xstocks_v2 import alerts as alerts_core        # noqa: E402
from arb_monitor.xstocks_v2 import investigate as inv           # noqa: E402
from arb_monitor.xstocks_v2 import notify as v2notify           # noqa: E402

DATA = ROOT / "data-v2"
ALERT_STATE = "alert_state.json"


def one_pass(*, data_dir: Path | str = DATA, root: Path | str = ROOT,
             dry_run: bool = False, notify: bool = True,
             now_ms: int | None = None, settings: dict | None = None) -> dict:
    """One full look. Returns the record that also lands in ``alert_state.json``."""
    data_dir = Path(data_dir)
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    settings = settings or inv.settings()
    state = inv.state_load(data_dir)

    finished = inv.reap(state=state, now_ms=now_ms, settings=settings,
                        data_dir=data_dir)
    if notify and not dry_run:
        for record in finished:
            v2notify.sender("alert")(inv.format_finished(record))

    found = alerts_core.collect(data_dir=data_dir, root=root, now_ms=now_ms)
    launched: list[dict] = []
    skipped: list[dict] = []
    for alert in found:
        may, reason = inv.decide(alert=alert, state=state, now_ms=now_ms,
                                 settings=settings)
        if not may:
            skipped.append({"kind": alert.get("kind"),
                            "symbol": alert.get("symbol") or "",
                            "reason": reason})
            continue
        if dry_run:
            launched.append({"id": "dry-run", "kind": alert.get("kind"),
                             "symbol": alert.get("symbol") or "",
                             "dry_run": True})
            continue
        record = inv.launch(alert=alert, state=state, now_ms=now_ms,
                            settings=settings, data_dir=data_dir, repo_root=root)
        launched.append(record)
        if notify and record.get("ok"):
            v2notify.sender("alert")(inv.format_launched(record))

    report = {
        "updated_ms": now_ms,
        "pid": os.getpid(),
        "dry_run": bool(dry_run),
        "counts": alerts_core.summary(found),
        "alerts": found,
        "launched": launched,
        "skipped": skipped,
        "finished": [{"id": item.get("id"), "outcome": item.get("outcome"),
                      "summary": item.get("summary")} for item in finished],
        "running": state.get("running") or [],
        "launched_today": int(state.get("launched_today") or 0),
        "max_per_day": int(settings.get("max_per_day") or 0),
    }
    if not dry_run:
        inv.state_save(state, data_dir)
        try:
            (data_dir / inv.DIR_NAME).mkdir(parents=True, exist_ok=True)
            (data_dir / inv.DIR_NAME / ALERT_STATE).write_text(
                json.dumps(report, ensure_ascii=False, indent=1) + "\n")
        except OSError:
            pass
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd")
    once = sub.add_parser("once", help="one pass, then exit")
    once.add_argument("--dry-run", action="store_true",
                      help="decide and report, start nothing")
    once.add_argument("--quiet", action="store_true")
    run = sub.add_parser("run", help="resident")
    run.add_argument("--interval", type=float, default=30.0)

    args = parser.parse_args(argv)
    if args.cmd == "run":
        pid_file = DATA / "xstocks-v2-watch.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()))
        while True:
            try:
                one_pass()
            except Exception as exc:  # noqa: BLE001 - the watcher outlives errors
                print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}),
                      flush=True)
            time.sleep(max(5.0, float(args.interval)))
    report = one_pass(dry_run=bool(getattr(args, "dry_run", False)))
    if not getattr(args, "quiet", False):
        print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
