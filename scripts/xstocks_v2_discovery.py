#!/usr/bin/env python3
"""Report xStock listings that our on-disk universe does not watch yet.

    scripts/xstocks_v2_discovery.py check            # one look, human text
    scripts/xstocks_v2_discovery.py check --json     # the whole report
    scripts/xstocks_v2_discovery.py check --notify   # also send to Feishu

Read-only: two upstream calls (Kraken tokenized pairs, issuer catalog). It never
writes a universe, never restarts anything and never signs. Exit codes:

    0  every source answered (whether or not new symbols were found)
    2  at least one source could not be read (``errors`` in the report)

Exit 2 is deliberately distinct from "no new symbols": a source we cannot read
must never be reported as "nothing new".
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.xstocks_v2 import discovery          # noqa: E402
from arb_monitor.xstocks_v2 import notify as v2notify  # noqa: E402
from arb_monitor.xstocks_v2.feeds.http import PROXY, Http  # noqa: E402
from arb_monitor.xstocks_v2.store import read_json, write_json  # noqa: E402

#: A targeted census is seconds; the nightly sweep is an hour or more because
#: GeckoTerminal paces one request per 2.1 s per address.
NIGHTLY_CENSUS_TIMEOUT_S = 7200


def fingerprint(report: dict) -> tuple:
    """What makes this run worth telling a person about.

    The job runs every few minutes; "nothing changed" must be silent. A new
    symbol, a source that newly broke, or a step that just ran are all changes;
    the same result twice in a row is not.
    """
    steps = ((report.get("ingest") or {}).get("steps") or [])
    return (
        tuple(sorted(report.get("new_symbols") or [])),
        tuple(sorted((report.get("errors") or {}).items())),
        tuple((step.get("name"), step.get("returncode")) for step in steps),
    )


def _run_step(name: str, cmd: list[str], runner, *, timeout: float = 900) -> dict:
    """Run one ingest command and keep its tail as evidence."""
    try:
        done = runner(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
        tail = (done.stdout or "").strip().splitlines()[-3:]
        return {"name": name, "cmd": cmd, "returncode": done.returncode, "tail": tail}
    except Exception as exc:  # noqa: BLE001 -- a failed step must reach the report
        return {"name": name, "cmd": cmd, "returncode": None,
                "tail": [f"{type(exc).__name__}: {exc}"]}


def run_ingest(report: dict, *, proxy: str = PROXY, runner=subprocess.run) -> dict:
    """Turn a detection into a wider universe.

    Three steps, each skipped when its own kind of gap is absent:

    * a symbol the issuer knows and we do not -> refresh the catalog (13 pages
      plus three calls; the Solana rail comes straight from this book);
    * that same symbol then gets a **targeted** chain census, so any-to-any
      edges (``dex_to_dex``, ``chain_to_chain``) can be built for it without
      waiting for the nightly sweep. The census walks the whole issuer catalog,
      so a name with no Kraken pair is still measured;
    * a symbol Kraken lists and our units table does not -> validate just that
      symbol and merge it (one private call, not 177), so the next scan round
      can price it.

    A targeted census is a handful of calls. The full ~1400-call sweep stays in
    the quiet-hours run.
    """
    engine = sys.executable
    steps: list[dict] = []
    if report.get("new_at_issuer"):
        steps.append(_run_step("catalog_refresh", [
            engine, str(ROOT / "scripts" / "xstocks_book.py"),
            "--proxy", proxy, "refresh"], runner))
        steps.append(_run_step("chain_census", [
            engine, str(ROOT / "scripts" / "xstocks_chain_census.py"),
            "--symbols", ",".join(report["new_at_issuer"])], runner))
    if report.get("new_on_kraken"):
        steps.append(_run_step("kraken_units", [
            engine, str(ROOT / "scripts" / "kraken_xstocks_units.py"),
            "--symbols", ",".join(report["new_on_kraken"])], runner))
    return {"steps": steps, "ran": bool(steps)}


def nightly_due(*, now: float, at: str, last_day: str | None) -> bool:
    """True once a day, from ``at`` (local ``HH:MM``) onwards.

    A missed minute must not skip the night: the test is "the clock is past
    ``at`` and this day has not run yet".
    """
    import datetime

    moment = datetime.datetime.fromtimestamp(now)
    return moment.strftime("%H:%M") >= at and moment.strftime("%Y-%m-%d") != last_day


def run_nightly(*, proxy: str = PROXY, runner=subprocess.run) -> dict:
    """The quiet-hours full sync: catalog, units, and the chain census.

    The census is the ~1400-call job that must not compete with the live
    sampler, which is why it lives here and not in the 15-minute check.
    """
    engine = sys.executable
    steps = [
        _run_step("catalog_refresh", [
            engine, str(ROOT / "scripts" / "xstocks_book.py"),
            "--proxy", proxy, "refresh"], runner),
        _run_step("kraken_units_full", [
            engine, str(ROOT / "scripts" / "kraken_xstocks_units.py")], runner),
        # The full sweep is the slow one: measured 2026-10-09, mantle alone sat
        # on GeckoTerminal for over 25 minutes (350 addresses, 2.1 s apart) and
        # the whole run did not finish in an hour. A 15-minute cap would kill it
        # every night, so this step gets its own budget.
        _run_step("chain_census", [
            engine, str(ROOT / "scripts" / "xstocks_chain_census.py")], runner,
            timeout=NIGHTLY_CENSUS_TIMEOUT_S),
    ]
    return {"steps": steps, "ran": True}


def run_loop(*, interval: float, nightly: str | None, proxy: str = PROXY,
             runner=subprocess.run, sleep=time.sleep, clock=time.time,
             http_factory=None, notify=None) -> None:
    """Resident: a cheap check every ``interval`` s, the full sync once a night."""
    http_factory = http_factory or (lambda: Http(proxy=proxy))
    last_day: str | None = None
    import datetime

    while True:
        try:
            previous = read_json(discovery.DEFAULT_REPORT, default={}) or {}
            report = discovery.check(http_factory())
            if report.get("new_symbols"):
                report["ingest"] = run_ingest(report, proxy=proxy, runner=runner)
            discovery.update_pending_rail(
                report, previous,
                priced_symbols=discovery.census_pool_symbols())
            message = discovery.format_message(report)
            report["message"] = message
            write_json(discovery.DEFAULT_REPORT, report)
            if notify is not None and fingerprint(report) != fingerprint(previous) \
                    and fingerprint(report) != ((), (), ()):
                notify(message)
        except Exception as exc:  # noqa: BLE001 -- the loop must survive one round
            print(f"discovery round failed: {type(exc).__name__}: {exc}", flush=True)
        if nightly and nightly_due(now=clock(), at=nightly, last_day=last_day):
            last_day = datetime.datetime.fromtimestamp(clock()).strftime("%Y-%m-%d")
            try:
                result = run_nightly(proxy=proxy, runner=runner)
                if notify is not None:
                    failed = [s["name"] for s in result["steps"] if s.get("returncode") != 0]
                    notify("xStocks 夜间全量同步完成"
                           + (f"；失败：{'、'.join(failed)}" if failed else "（全部成功）"))
            except Exception as exc:  # noqa: BLE001
                print(f"nightly failed: {type(exc).__name__}: {exc}", flush=True)
        sleep(interval)


def main(argv: list[str] | None = None) -> int:
    # The flags live on both the top-level parser and the ``check`` subparser, so
    # ``check --json`` and ``--json check`` both work.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--proxy", default=PROXY)
    common.add_argument("--units", default=str(discovery.DEFAULT_UNITS))
    common.add_argument("--book", default=str(discovery.DEFAULT_BOOK))
    common.add_argument("--report", default=str(discovery.DEFAULT_REPORT))
    common.add_argument("--json", action="store_true", help="print the full report")
    common.add_argument("--notify", action="store_true",
                        help="send the message on the signal channel")
    common.add_argument("--ingest", "--apply", dest="ingest", action="store_true",
                        help="bring the new symbols into the universe instead of "
                             "only reporting them")
    common.add_argument("--seed-pending", default=None,
                        help="comma separated: mark these as waiting for a Kraken "
                             "pair (used when a refresh happened outside this job)")
    common.add_argument("--dry-run", action="store_true",
                        help="print only; do not write the report, do not notify")
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("check", help="one look (default)", parents=[common])
    run_cmd = sub.add_parser("run", help="resident: check on a timer, full sync nightly",
                             parents=[common])
    run_cmd.add_argument("--interval", type=float, default=900)
    run_cmd.add_argument("--nightly", default="02:00",
                         help="local HH:MM for the full sync (empty disables)")
    args = parser.parse_args(argv)

    if args.cmd == "run":
        run_loop(interval=args.interval, nightly=args.nightly or None, proxy=args.proxy,
                 notify=v2notify.sender("signal"))
        return 0
    previous = read_json(args.report, default={}) or {}
    report = discovery.check(Http(proxy=args.proxy), units_path=args.units,
                             book_path=args.book)
    if args.ingest and report.get("new_symbols"):
        report["ingest"] = run_ingest(report, proxy=args.proxy)
        after = discovery.check(Http(proxy=args.proxy), units_path=args.units,
                                book_path=args.book)
        report["after_ingest"] = {
            "new_symbols": after.get("new_symbols") or [],
            "errors": after.get("errors") or {},
        }
    seed = {item.strip() for item in (args.seed_pending or "").split(",") if item.strip()}
    if seed:
        report["new_at_issuer"] = sorted(set(report.get("new_at_issuer") or []) | seed)
    discovery.update_pending_rail(report, previous,
                                  priced_symbols=discovery.census_pool_symbols())
    message = discovery.format_message(report)
    if report.get("ingest", {}).get("ran"):
        steps = report["ingest"]["steps"]
        failed = [step["name"] for step in steps if step.get("returncode") != 0]
        left = report["after_ingest"]["new_symbols"]
        message += ("\n已自动入库：" + "、".join(step["name"] for step in steps)
                    + (f"（失败：{'、'.join(failed)}）" if failed else "")
                    + (f"\n入库后仍在册外：{'、'.join(left)}" if left else
                       "\n入库后全部在册。"))
    if not args.dry_run:
        report["message"] = message
        write_json(args.report, report)
    changed = fingerprint(report) != fingerprint(previous)
    if args.notify and not args.dry_run and changed and fingerprint(report) != ((), (), ()):
        v2notify.sender("signal")(message)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(message)
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
