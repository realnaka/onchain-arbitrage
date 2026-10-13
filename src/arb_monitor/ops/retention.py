"""Bounded raw retention for the monitor database.

Operator's request, 2026-09-17: "这个库太大了，统计之后就不要保存这么多数据，存不下".

The monitor writes every observation it makes, and two of those kinds dwarf
everything else. Measured on 2026-09-17, ``data/hk_monitor.db`` was 4.36 GB over
4.9 days of running (~890 MB/day):

===============  =========  ==========  =========
kind             rows       total       share
===============  =========  ==========  =========
instrument_      1,581      1,555 MB    36%
discovery
run_summary      5,010      1,388 MB    32%
perp_book        96,263     316 MB       7%
spot_quote       78,773     209 MB       5%
basis_evaluation 197,971    108 MB       2%
===============  =========  ==========  =========

Nothing in the framework reads ``instrument_discovery`` back (it is evidence,
not input), and only the newest ``run_summary`` is rendered — the rest is
history that was being kept at full fidelity for no stated reason.

What this module does, in the order it matters:

1. **Rolls up before it deletes.** Every row that leaves the database
   contributes to ``event_daily_stats`` (rows, bytes, first/last timestamp, plus
   per-kind aggregates) and, for ``basis_evaluation``, to ``venue_daily_stats``
   (per day/venue/asset/instrument/phase: cycles, priced, opportunities, max and
   mean entry basis). The long-run venue comparison therefore survives the
   pruning of the rows it was computed from.
2. **Deletes in bounded batches, in one transaction per batch**, so the
   statistics and the deletion that produced them either both land or neither
   does. Overlapping runs cannot double count: a batch is rolled up and deleted
   together, and the rows are gone afterwards.
3. **Optionally rewrites the file** (``compact``) — SQLite keeps deleted pages
   on a freelist, so the file only shrinks after a ``VACUUM``. The rewrite
   enables ``auto_vacuum=INCREMENTAL`` on the new file, which lets later prunes
   hand pages back without another rewrite.

The thresholds live in ``config/hk_monitor.toml`` (``[retention]``); the
defaults here are only the fallback for a database that has no policy yet.

Operator, 2026-09-28: keep the last 48 hours of raw rows, and past that keep
only rows that are themselves a signal we emitted. A signal row is an ``alert``
or a ``basis_evaluation`` whose ``status`` is ``conditional_signal``. Quotes,
books, summaries and every other observation older than 48 hours are rolled up
and deleted. The intent ledger (``opportunities``) is not an event kind and is
not pruned here.

Every aggregate in the rollup tables is a statistic over recorded numbers, not a
trade amount: the project's Decimal rule governs order prices and quantities.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - the project runs 3.14
    import tomli as tomllib  # type: ignore

from arb_monitor.ops.venue_scoreboard import DEFAULT_MIN_GROSS_BPS

DEFAULT_CONFIG = "config/hk_monitor.toml"
DEFAULT_DB = "data/hk_monitor.db"
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
# The operator reads the board in Beijing time, so a "day" in the rollup means a
# Beijing day; a 16:00-to-16:00 UTC day would split every HK session.
DAY_EXPR = "substr(datetime(e.recv_ts_ms/1000,'unixepoch','+8 hours'),1,10)"


# Per-kind aggregates kept *after* the raw rows are gone. ``(SQL expression over
# the alias ``e``, merge mode)`` — the merge mode says how a second batch for the
# same day combines with the first: counters add, extremes win.
KIND_STATS: dict[str, dict[str, tuple[str, str]]] = {
    "run_summary": {
        "cycles_with_candidates": (
            "sum(case when cast(json_extract(e.payload_json,'$.candidate_count') "
            "as integer) > 0 then 1 else 0 end)", "sum"),
        "max_candidates": (
            "max(cast(json_extract(e.payload_json,'$.candidate_count') as integer))",
            "max"),
        "degraded_cycles": (
            "sum(case when e.status <> 'ok' then 1 else 0 end)", "sum"),
    },
    "basis_evaluation": {
        "priced_rows": (
            "sum(case when json_extract(e.payload_json,'$.entry_basis_bps') "
            "is not null then 1 else 0 end)", "sum"),
        "opportunity_rows": (
            "sum(case when cast(json_extract(e.payload_json,'$.entry_basis_bps') "
            f"as real) >= {DEFAULT_MIN_GROSS_BPS} then 1 else 0 end)", "sum"),
        "max_entry_basis_bps": (
            "max(cast(json_extract(e.payload_json,'$.entry_basis_bps') as real))",
            "max"),
    },
    "source_error": {
        "error_rows": ("sum(case when e.status = 'error' then 1 else 0 end)", "sum"),
    },
}

# Kinds that also feed the per-venue tally, so a long window stays answerable
# after the raw rows are pruned.
VENUE_STATS_KINDS = frozenset({"basis_evaluation"})


@dataclass(frozen=True)
class KindRule:
    """How long one kind of event is kept, and how many rows at most."""

    days: float | None = None
    keep_rows: int | None = None
    per_venue: bool = False


@dataclass
class RetentionPolicy:
    default_days: float = 7.0
    rules: dict[str, KindRule] = field(default_factory=dict)
    batch_rows: int = 2_000
    # When set, every kind shares this raw window and per-kind ``days`` /
    # ``keep_rows`` do not cut it shorter. Signal rows are the exception.
    raw_hours: float | None = None
    keep_signals: bool = False

    def rule(self, kind: str) -> KindRule:
        found = self.rules.get(kind)
        if found is not None:
            return found
        return KindRule(days=self.default_days)


def policy_from_document(document: dict) -> RetentionPolicy:
    """Read ``[retention]`` from an already-parsed config document."""
    section = document.get("retention") or {}
    rules: dict[str, KindRule] = {}
    for kind, spec in (section.get("kinds") or {}).items():
        if isinstance(spec, (int, float)):
            rules[kind] = KindRule(days=float(spec))
            continue
        if not isinstance(spec, dict):
            raise ValueError(f"[retention.kinds] {kind}: expected number or table")
        keep_rows = None if spec.get("keep_rows") is None else int(spec["keep_rows"])
        if keep_rows is not None and keep_rows < 1:
            raise ValueError(f"[retention.kinds] {kind}: keep_rows must be >= 1")
        rules[kind] = KindRule(
            days=None if spec.get("days") is None else float(spec["days"]),
            keep_rows=keep_rows,
            per_venue=bool(spec.get("per_venue", False)),
        )
    raw_hours = section.get("raw_hours")
    if raw_hours is not None:
        raw_hours = float(raw_hours)
        if raw_hours <= 0:
            raise ValueError("[retention] raw_hours must be > 0")
    return RetentionPolicy(
        default_days=float(section.get("default_days", 7)),
        rules=rules,
        batch_rows=int(section.get("batch_rows", 2_000)),
        raw_hours=raw_hours,
        keep_signals=bool(section.get("keep_signals", False)),
    )


def load_policy(config_path: str | Path = DEFAULT_CONFIG) -> RetentionPolicy:
    """Read ``[retention]``; a missing section keeps the built-in fallback.

    Shorthand is allowed because most kinds only need a number of days::

        [retention.kinds]
        perp_book = 5
        run_summary = { days = 2, keep_rows = 400 }

    ``raw_hours`` replaces those per-kind windows with one clock for every kind.
    """
    path = Path(config_path)
    if not path.exists():
        return RetentionPolicy()
    with path.open("rb") as handle:
        document = tomllib.load(handle)
    return policy_from_document(document)


def _signal_sql() -> str:
    """Rows that are a signal we emitted, not the tape around one.

    An ``alert`` is the announcement. A ``basis_evaluation`` is a signal only
    when the strategy set ``status`` to ``conditional_signal`` — a watch-only
    evaluation is an observation, and it ages out with the rest of the tape.
    """
    return (
        "kind = 'alert' OR ("
        "kind = 'basis_evaluation' AND "
        "json_extract(payload_json, '$.status') = 'conditional_signal')"
    )


def _cutoff_ms(policy: RetentionPolicy, rule: KindRule, now_ms: int) -> int | None:
    if policy.raw_hours is not None:
        return int(now_ms - float(policy.raw_hours) * HOUR_MS)
    if rule.days is None:
        return None
    return int(now_ms - float(rule.days) * DAY_MS)


def _age_where(policy: RetentionPolicy) -> str:
    clause = "kind = ? AND recv_ts_ms < ?"
    if policy.keep_signals:
        clause += f" AND NOT ({_signal_sql()})"
    return clause


def connect(db_path: str | Path) -> sqlite3.Connection:
    """One connection in explicit-transaction mode.

    Autocommit is on (``isolation_level = None``) so this module decides where
    the transaction boundaries are: the monitor writes to the same database
    while the prune runs, and a read that silently upgrades to a write fails
    with ``SQLITE_BUSY_SNAPSHOT`` the moment the reader's snapshot goes stale.
    Taking the write lock *first* (``BEGIN IMMEDIATE``) waits instead.
    """
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.isolation_level = None
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def _write_transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


ROLLUP_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS event_daily_stats (
      day TEXT NOT NULL,
      kind TEXT NOT NULL,
      rows INTEGER NOT NULL,
      bytes INTEGER NOT NULL,
      first_recv_ts_ms INTEGER,
      last_recv_ts_ms INTEGER,
      stats_json TEXT NOT NULL,
      PRIMARY KEY (day, kind)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS venue_daily_stats (
      day TEXT NOT NULL,
      venue TEXT NOT NULL,
      asset TEXT NOT NULL,
      instrument TEXT NOT NULL,
      phase TEXT NOT NULL,
      rows INTEGER NOT NULL,
      priced INTEGER NOT NULL,
      opportunities INTEGER NOT NULL,
      basis_sum REAL NOT NULL,
      max_entry_basis_bps REAL,
      PRIMARY KEY (day, venue, asset, instrument, phase)
    )
    """,
)


def ensure_rollup_schema(conn: sqlite3.Connection) -> None:
    for statement in ROLLUP_SCHEMA:
        conn.execute(statement)
    conn.commit()


def _rollup_batch(conn: sqlite3.Connection, kind: str) -> tuple[int, int]:
    """Fold one batch into the rollup tables; returns ``(rows, bytes)``."""
    stats = KIND_STATS.get(kind) or {}
    pairs = ", ".join(f"'{name}', {expr}" for name, (expr, _) in stats.items())
    # No per-kind statistics means the counters are all there is to keep.
    stats_select = f"json_object({pairs})" if pairs else "'{}'"
    row = conn.execute(
        f"""SELECT count(*), coalesce(sum(length(e.payload_json)), 0)
            FROM monitor_events e
            WHERE e.id IN (SELECT id FROM _prune_batch)"""
    ).fetchone()
    rows, bytes_ = row
    if not rows:
        return 0, 0
    merge = []
    for name, (_, mode) in stats.items():
        old = f"coalesce(json_extract(event_daily_stats.stats_json,'$.{name}'), 0)"
        new = f"coalesce(json_extract(excluded.stats_json,'$.{name}'), 0)"
        # `sum(a,b)` is not the scalar two-argument form of the sum() aggregate;
        # adding explicitly keeps the merge readable for both modes.
        combined = f"({old} + {new})" if mode == "sum" else f"{mode}({old}, {new})"
        merge.append(f"'{name}', {combined}")
    update = [
        "rows = event_daily_stats.rows + excluded.rows",
        "bytes = event_daily_stats.bytes + excluded.bytes",
        "first_recv_ts_ms = min(event_daily_stats.first_recv_ts_ms,"
        " excluded.first_recv_ts_ms)",
        "last_recv_ts_ms = max(event_daily_stats.last_recv_ts_ms,"
        " excluded.last_recv_ts_ms)",
    ]
    if merge:
        update.append(f"stats_json = json_object({', '.join(merge)})")
    conn.execute(
        f"""INSERT INTO event_daily_stats
              (day, kind, rows, bytes, first_recv_ts_ms, last_recv_ts_ms, stats_json)
            SELECT {DAY_EXPR}, ?, count(*),
                   coalesce(sum(length(e.payload_json)), 0),
                   min(e.recv_ts_ms), max(e.recv_ts_ms), {stats_select}
            FROM monitor_events e
            WHERE e.id IN (SELECT id FROM _prune_batch)
            GROUP BY 1
            ON CONFLICT(day, kind) DO UPDATE SET {', '.join(update)}""",
        (kind,),
    )
    if kind in VENUE_STATS_KINDS:
        _rollup_venues(conn)
    return int(rows), int(bytes_)


def _rollup_venues(conn: sqlite3.Connection) -> None:
    """Per day/venue/asset/instrument/phase tally over the batch being deleted."""
    conn.execute(
        f"""
        INSERT INTO venue_daily_stats
          (day, venue, asset, instrument, phase, rows, priced, opportunities,
           basis_sum, max_entry_basis_bps)
        SELECT {DAY_EXPR}, e.venue, e.asset,
               coalesce(json_extract(e.payload_json,'$.instrument'), ''),
               coalesce(json_extract(e.payload_json,'$.phase'), ''),
               count(*),
               sum(case when json_extract(e.payload_json,'$.entry_basis_bps')
                        is not null then 1 else 0 end),
               sum(case when cast(json_extract(e.payload_json,'$.entry_basis_bps')
                                 as real) >= {DEFAULT_MIN_GROSS_BPS} then 1 else 0 end),
               coalesce(sum(cast(json_extract(e.payload_json,'$.entry_basis_bps')
                                 as real)), 0),
               max(cast(json_extract(e.payload_json,'$.entry_basis_bps') as real))
        FROM monitor_events e
        WHERE e.id IN (SELECT id FROM _prune_batch)
        GROUP BY 1, 2, 3, 4, 5
        ON CONFLICT(day, venue, asset, instrument, phase) DO UPDATE SET
          rows = venue_daily_stats.rows + excluded.rows,
          priced = venue_daily_stats.priced + excluded.priced,
          opportunities = venue_daily_stats.opportunities + excluded.opportunities,
          basis_sum = venue_daily_stats.basis_sum + excluded.basis_sum,
          max_entry_basis_bps = max(venue_daily_stats.max_entry_basis_bps,
                                    excluded.max_entry_basis_bps)
        """
    )


def _drain(
    conn: sqlite3.Connection,
    policy: RetentionPolicy,
    kind: str,
    where_sql: str,
    params: list,
) -> tuple[int, int]:
    """Roll up and delete everything matching ``where_sql``, one batch at a time.

    The id lookup stays outside the write transaction. On the live 13 GB file
    that lookup (and the pre-pass that used to measure every payload) held
    ``BEGIN IMMEDIATE`` long enough that the sampler's insert hit
    ``database is locked`` and the process exited. Measured 2026-09-28: four
    restarts in twenty minutes, and the file never shrank because each pass
    died with the process.
    """
    deleted_rows = deleted_bytes = 0
    while True:
        ids = [row[0] for row in conn.execute(
            f"SELECT id FROM monitor_events WHERE {where_sql} LIMIT ?",
            (*params, policy.batch_rows),
        )]
        if not ids:
            return deleted_rows, deleted_bytes
        # The statistics for a batch and the deletion of that batch commit
        # together: a crash can never leave a row counted twice, or gone and
        # uncounted. The lock covers only this already-chosen batch.
        with _write_transaction(conn):
            conn.execute("DELETE FROM _prune_batch")
            conn.executemany(
                "INSERT INTO _prune_batch(id) VALUES (?)",
                [(row_id,) for row_id in ids],
            )
            rows, bytes_ = _rollup_batch(conn, kind)
            conn.execute(
                "DELETE FROM monitor_events WHERE id IN (SELECT id FROM _prune_batch)")
        deleted_rows += rows
        deleted_bytes += bytes_
        # Let a sampler insert land between batches. Without this gap the
        # prune re-takes BEGIN IMMEDIATE before the waiting writer wakes.
        time.sleep(0.01)
    return deleted_rows, deleted_bytes


def _size_of(conn: sqlite3.Connection, where_sql: str, params: list) -> tuple[int, int]:
    row = conn.execute(
        f"SELECT count(*), coalesce(sum(length(payload_json)), 0) "
        f"FROM monitor_events WHERE {where_sql}",
        params,
    ).fetchone()
    return int(row[0]), int(row[1])


def plan(conn: sqlite3.Connection, policy: RetentionPolicy, *,
         now_ms: int | None = None) -> list[dict]:
    """What ``apply`` would remove, without touching anything."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    kinds = [r[0] for r in conn.execute(
        "SELECT DISTINCT kind FROM monitor_events ORDER BY kind")]
    planned = []
    for kind in kinds:
        rule = policy.rule(kind)
        entry = {"kind": kind, "days": rule.days, "hours": policy.raw_hours,
                 "keep_rows": None if policy.raw_hours is not None else rule.keep_rows,
                 "per_venue": rule.per_venue,
                 "by_age": {"rows": 0, "bytes": 0},
                 "by_cap": {"rows": 0, "bytes": 0},
                 "signals_kept": {"rows": 0, "bytes": 0}}
        cutoff = _cutoff_ms(policy, rule, now_ms)
        if cutoff is not None:
            rows, bytes_ = _size_of(conn, _age_where(policy), [kind, cutoff])
            entry["cutoff_ms"] = cutoff
            entry["by_age"] = {"rows": rows, "bytes": bytes_}
            if policy.keep_signals:
                kept_rows, kept_bytes = _size_of(
                    conn, f"kind = ? AND recv_ts_ms < ? AND ({_signal_sql()})",
                    [kind, cutoff])
                entry["signals_kept"] = {"rows": kept_rows, "bytes": kept_bytes}
        # A raw-hour window is the bound. A row cap must not delete inside it.
        if rule.keep_rows is not None and policy.raw_hours is None:
            entry["by_cap"] = _cap_plan(conn, kind, rule, entry.get("cutoff_ms"))
        entry["kept"] = _kept_estimate(conn, kind, entry)
        planned.append(entry)
    return planned


def _cap_plan(conn: sqlite3.Connection, kind: str, rule: KindRule,
              cutoff_ms: int | None = None) -> dict:
    """Rows that fall outside the newest-``keep_rows`` window (per venue if asked).

    ``cutoff_ms`` excludes the rows the age rule already claimed, so the plan is
    not double counted; ``apply`` runs the age rule first and then re-derives the
    threshold from the rows that survived it.
    """
    venues = [""]
    if rule.per_venue:
        venues = [r[0] for r in conn.execute(
            "SELECT DISTINCT venue FROM monitor_events WHERE kind = ?", (kind,))]
    rows = bytes_ = 0
    for venue in venues:
        threshold = _cap_threshold(conn, kind, int(rule.keep_rows), venue or None)
        if threshold is None:
            continue
        where = "kind = ? AND recv_ts_ms < ?"
        params: list = [kind, threshold]
        if cutoff_ms is not None:
            where += " AND recv_ts_ms >= ?"
            params.append(cutoff_ms)
        if venue:
            where += " AND venue = ?"
            params.append(venue)
        part = _size_of(conn, where, params)
        rows += part[0]
        bytes_ += part[1]
    return {"rows": rows, "bytes": bytes_}


def _cap_threshold(conn: sqlite3.Connection, kind: str, keep_rows: int,
                   venue: str | None) -> int | None:
    """``recv_ts_ms`` of the ``keep_rows``-th newest row, or None when nothing to trim.

    Everything strictly older than that timestamp goes. Time is the right
    dimension here (not ``id``) because the retention window is expressed in
    days: a row replayed late would otherwise be judged by insertion order.
    """
    where = "kind = ?"
    params: list = [kind]
    if venue is not None:
        where += " AND venue = ?"
        params.append(venue)
    row = conn.execute(
        f"SELECT recv_ts_ms FROM monitor_events WHERE {where} "
        f"ORDER BY recv_ts_ms DESC LIMIT 1 OFFSET ?",
        (*params, keep_rows - 1),
    ).fetchone()
    return int(row[0]) if row else None


def _kept_estimate(conn: sqlite3.Connection, kind: str, entry: dict) -> dict:
    rows, bytes_ = _size_of(conn, "kind = ?", [kind])
    return {"rows": rows - entry["by_age"]["rows"] - entry["by_cap"]["rows"],
            "bytes": bytes_ - entry["by_age"]["bytes"] - entry["by_cap"]["bytes"]}


def _schedule(conn: sqlite3.Connection, policy: RetentionPolicy,
              now_ms: int) -> list[dict]:
    """What to delete, without reading payloads.

    ``plan`` counts bytes first. On the live database that count is a
    multi-minute read, and a read that long pins the WAL so the sampler cannot
    checkpoint. The delete itself does not need those counts.
    """
    kinds = [row[0] for row in conn.execute(
        "SELECT DISTINCT kind FROM monitor_events ORDER BY kind")]
    scheduled = []
    for kind in kinds:
        rule = policy.rule(kind)
        entry = {"kind": kind, "days": rule.days, "hours": policy.raw_hours,
                 "keep_rows": None if policy.raw_hours is not None else rule.keep_rows,
                 "per_venue": rule.per_venue,
                 "by_age": {"rows": 0, "bytes": 0},
                 "by_cap": {"rows": 0, "bytes": 0},
                 "signals_kept": {"rows": 0, "bytes": 0}}
        cutoff = _cutoff_ms(policy, rule, now_ms)
        if cutoff is not None:
            entry["cutoff_ms"] = cutoff
        scheduled.append(entry)
    return scheduled


def apply(conn: sqlite3.Connection, policy: RetentionPolicy, *,
          now_ms: int | None = None, progress=None) -> list[dict]:
    """Roll up and delete everything the policy says is past its keep-by date."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    ensure_rollup_schema(conn)
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _prune_batch(id INTEGER PRIMARY KEY)")
    report = _schedule(conn, policy, now_ms)
    for entry in report:
        kind = entry["kind"]
        rule = policy.rule(kind)
        removed_rows = removed_bytes = 0
        if entry.get("cutoff_ms") is not None:
            rows, bytes_ = _drain(conn, policy, kind,
                                  _age_where(policy),
                                  [kind, entry["cutoff_ms"]])
            removed_rows += rows
            removed_bytes += bytes_
        if rule.keep_rows is not None and policy.raw_hours is None:
            venues = [None]
            if rule.per_venue:
                venues = [r[0] for r in conn.execute(
                    "SELECT DISTINCT venue FROM monitor_events WHERE kind = ?", (kind,))]
            for venue in venues:
                threshold = _cap_threshold(conn, kind, int(rule.keep_rows), venue)
                if threshold is None:
                    continue
                where = "kind = ? AND recv_ts_ms < ?"
                params: list = [kind, threshold]
                if venue is not None:
                    where += " AND venue = ?"
                    params.append(venue)
                rows, bytes_ = _drain(conn, policy, kind, where, params)
                removed_rows += rows
                removed_bytes += bytes_
        entry["deleted"] = {"rows": removed_rows, "bytes": removed_bytes}
        if policy.keep_signals and entry.get("cutoff_ms") is not None:
            # After the delete, the only rows still inside the window are the
            # signal rows, so this count does not re-read the tape just removed.
            kept = conn.execute(
                f"SELECT count(*) FROM monitor_events WHERE kind = ? "
                f"AND recv_ts_ms < ? AND ({_signal_sql()})",
                (kind, entry["cutoff_ms"]),
            ).fetchone()[0]
            entry["signals_kept"] = {"rows": int(kept), "bytes": 0}
        if progress is not None:
            progress(entry)
    _hand_pages_back(conn)
    return report


def _hand_pages_back(conn: sqlite3.Connection) -> None:
    """Return freed pages to the filesystem without a full rewrite.

    Only possible after the file has been rebuilt with
    ``auto_vacuum=INCREMENTAL`` (``compact`` does that once); on any other
    database this is a no-op and the space waits for the next ``--vacuum``.

    Measured on this SQLite (3.51, Homebrew): one ``incremental_vacuum`` call
    returns **one** page — the file is truncated at commit — so the pragma is
    applied in a loop until the freelist stops shrinking. 5,000 pages took 0.15 s
    in WAL mode / 0.97 s in rollback mode, which is why a bound is enough. The
    ``wal_checkpoint(TRUNCATE)`` afterwards is what makes the smaller file
    visible: inside a WAL, the truncation only reaches the database file when a
    checkpoint copies it back.
    """
    if conn.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
        return
    # One call returns one page. A multi-gigabyte prune can free a few million
    # pages; 5,000 pages measured at ~0.15 s, so a few million is on the order
    # of a minute and belongs at the end of the background job, not in the
    # sampling loop. The cap is only a runaway guard.
    for _ in range(4_000_000):
        before = conn.execute("PRAGMA freelist_count").fetchone()[0]
        if not before:
            break
        conn.execute("PRAGMA incremental_vacuum")
        if conn.execute("PRAGMA freelist_count").fetchone()[0] >= before:
            break
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        # A reader (the board) can hold the checkpoint back; the next one gets it.
        pass


# --------------------------------------------------------------------------
# File rewrite: deleting rows frees pages into a freelist, so the file only
# shrinks after a VACUUM. The rewrite also flips the new file to incremental
# auto-vacuum, so the *next* prune can hand pages back without a rewrite.
# --------------------------------------------------------------------------


def _in_use(path: Path, timeout: float = 2.0) -> bool:
    """True when another connection still holds the database open for writing."""
    try:
        probe = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=timeout)
    except sqlite3.Error:
        return False
    try:
        probe.execute("BEGIN EXCLUSIVE")
        probe.execute("ROLLBACK")
        return False
    except sqlite3.OperationalError:
        return True
    finally:
        probe.close()


def compact(path: str | Path = DEFAULT_DB, *, keep_backup: bool = True,
            verify: bool = True, force: bool = False, log=print) -> dict:
    """Rewrite ``path`` so freed pages leave the disk.

    Refuses to run while another process has the database open (the monitor and
    the board both do) unless ``force`` is set: swapping the file under a live
    writer is how a database gets corrupted.
    """
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(source)
    if _in_use(source) and not force:
        raise RuntimeError(
            f"{source} 仍被其他进程占用（监控或看板）。先 "
            f"scripts/hk-monitor-ctl.sh stop / scripts/board-ctl.sh stop，或加 --force。")
    before = source.stat().st_size
    temp = source.with_name(source.name + ".compact-tmp")
    if temp.exists():
        temp.unlink()
    log(f"  重写中：{before / 1048576:.1f} MB → {temp.name}")
    conn = connect(source)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM INTO ?", (str(temp),))
    finally:
        conn.close()
    with sqlite3.connect(str(temp)) as rebuilt:
        rebuilt.execute("PRAGMA auto_vacuum=INCREMENTAL")
        rebuilt.execute("VACUUM")
    if verify:
        with sqlite3.connect(str(temp)) as check:
            verdict = check.execute("PRAGMA quick_check").fetchone()[0]
        if verdict != "ok":
            temp.unlink(missing_ok=True)
            raise RuntimeError(f"重写后的数据库自检失败：{verdict}")
    after = temp.stat().st_size
    # Re-check immediately before the swap. `scripts/run_hk_monitor.sh` restarts
    # the monitor 10 s after it exits, so a stop that loses that race leaves a
    # live writer holding the *old* inode: the rewrite would then move the file
    # it is still writing to out from under it (observed 2026-09-17 15:18).
    if _in_use(source) and not force:
        temp.unlink(missing_ok=True)
        raise RuntimeError(
            f"{source} 在重写期间被重新打开（监控可能被 supervisor 拉起）。"
            f"先确认没有 arb_monitor 进程，再重跑 --vacuum。")
    backup = None
    if keep_backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = source.with_name(f"{source.name}.pre-prune-{stamp}")
        os.replace(source, backup)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(source) + suffix)
            if sidecar.exists():
                shutil.move(str(sidecar), str(backup) + suffix)
    os.replace(temp, source)
    log(f"  完成：{after / 1048576:.1f} MB"
        + (f"（旧库备份 {backup}，核对后可删）" if backup else ""))
    return {"before_bytes": before, "after_bytes": after,
            "backup": str(backup) if backup else None}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def add_arguments(sub) -> None:
    parser = sub.add_parser(
        "db-retention",
        help="roll old monitor events up into daily statistics, then delete them")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--db", default=None,
                        help=f"monitor database (default: [monitor] db, else {DEFAULT_DB})")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be deleted; change nothing")
    parser.add_argument("--vacuum", action="store_true",
                        help="after pruning, rewrite the file so the space is actually freed")
    parser.add_argument("--keep-backup", action="store_true", default=True,
                        help="keep the pre-rewrite file next to the database (default)")
    parser.add_argument("--no-backup", dest="keep_backup", action="store_false")
    parser.add_argument("--json", action="store_true")


def _db_from_config(config_path: str) -> str:
    try:
        with open(config_path, "rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return DEFAULT_DB
    return str((document.get("monitor") or {}).get("db") or DEFAULT_DB)


def _human(report: list[dict], *, dry_run: bool) -> str:
    lines = []
    head = "计划删除（未改动）" if dry_run else "已删除"
    lines.append(f"原始事件留存 · {head}")
    total_rows = total_bytes = 0
    for entry in report:
        if dry_run:
            rows = entry["by_age"]["rows"] + entry["by_cap"]["rows"]
            bytes_ = entry["by_age"]["bytes"] + entry["by_cap"]["bytes"]
        else:
            rows = entry["deleted"]["rows"]
            bytes_ = entry["deleted"]["bytes"]
        total_rows += rows
        total_bytes += bytes_
        kept_signals = (entry.get("signals_kept") or {}).get("rows") or 0
        if not rows and not kept_signals:
            continue
        if entry.get("hours") is not None:
            keep = f"{entry['hours']:g} 小时"
        elif entry["days"] is None:
            keep = "永久"
        else:
            keep = f"{entry['days']:g} 天"
        cap = f"，上限 {entry['keep_rows']} 行" if entry["keep_rows"] else ""
        per_venue = "／场馆" if entry["per_venue"] else ""
        spared = f"；信号留下 {kept_signals} 行" if kept_signals else ""
        lines.append(f"  {entry['kind']:22s} 保留 {keep}{cap}{per_venue}"
                     f"  删 {rows:7d} 行 / {bytes_ / 1048576:8.1f} MB{spared}")
    lines.append(f"  合计：{total_rows} 行 / {total_bytes / 1048576:.1f} MB"
                 + ("（incremental_vacuum 会把空页还给磁盘；文件没缩小再跑 --vacuum）"
                    if not dry_run else ""))
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    config_path = args.config
    db_path = args.db or _db_from_config(config_path)
    policy = load_policy(config_path)
    conn = connect(db_path)
    try:
        if args.dry_run:
            report = plan(conn, policy)
            if args.json:
                print(json.dumps({"db": db_path, "dry_run": True,
                                  "plan": report}, ensure_ascii=False, indent=2))
            else:
                print(_human(report, dry_run=True))
            return 0
        report = apply(conn, policy)
        size = Path(db_path).stat().st_size
    finally:
        conn.close()
    result = {"db": db_path, "dry_run": False, "plan": report,
              "file_bytes": size}
    if args.vacuum:
        result["compact"] = compact(db_path, keep_backup=args.keep_backup)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(_human(report, dry_run=False))
        print(f"  数据库文件：{size / 1048576:.1f} MB")
    return 0
