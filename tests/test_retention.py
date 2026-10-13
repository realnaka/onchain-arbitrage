"""Raw-event retention: statistics survive, the rows (and the megabytes) do not.

Operator's request, 2026-09-17: "这个库太大了，统计之后就不要保存这么多数据，存不下".

The retention job edits live monitoring data, so each test pins a way it could
destroy something that mattered:

1. **The statistics must be written before the rows are gone.** A rollup that
   runs after the delete has nothing left to count.
2. **A cap must not eat the newest rows.** "Keep the newest N" is one off-by-one
   away from "keep the oldest N".
3. **A dry run must change nothing.** The operator reads a plan before letting it
   delete gigabytes.
4. **The rewrite must not lose rows or corrupt the file**, and it must refuse to
   run under a live writer.
"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sqlite3
import unittest

from arb_monitor.apps.hk_monitor import HKMonitor, load_config
from arb_monitor.ledger.store import EventStore
from arb_monitor.ops.retention import (KindRule, RetentionPolicy, apply, compact,
                                       connect, load_policy, plan,
                                       policy_from_document)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/hk_monitor.toml"
DAY = 86_400_000
NOW = 1_789_000_000_000          # 2026-09-11 13:06 UTC, fixed so plans are stable


def fill(db: Path) -> None:
    """Six daily summaries, hourly discovery per venue, two days of basis rows."""
    store = EventStore(db)
    try:
        for i in range(6):                      # i = 0 is the oldest
            store.record_market_event(
                f"run{i}", "run_summary",
                {"candidate_count": i, "evaluated_at_ms": NOW - (5 - i) * DAY},
                NOW - (5 - i) * DAY, source="arb_monitor")
        for venue in ("gate", "okx"):
            for i in range(15):
                store.record_market_event(
                    "run", "instrument_discovery", {"venue": venue, "n": i},
                    NOW - (14 - i) * 3_600_000, venue=venue)
        for i in range(40):
            store.record_market_event(
                "run", "basis_evaluation",
                {"instrument": f"okx:A{i:02d}", "phase": "continuous_am",
                 "entry_basis_bps": str(i - 10)},
                NOW - (1 - i // 20) * DAY, venue="okx", asset="XIAOMI")
    finally:
        store.close()


class RollupTests(unittest.TestCase):
    def setUp(self):
        self.dir = TemporaryDirectory()
        self.db = Path(self.dir.name) / "monitor.db"
        fill(self.db)
        self.conn = connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.dir.cleanup()

    def chosen(self, **rules) -> RetentionPolicy:
        return RetentionPolicy(default_days=7, batch_rows=4, rules=rules)

    def test_age_rule_drops_only_rows_older_than_the_window(self):
        report = apply(self.conn,
                       self.chosen(run_summary=KindRule(days=2)), now_ms=NOW)
        entry = next(r for r in report if r["kind"] == "run_summary")
        self.assertEqual(entry["deleted"]["rows"], 3)
        kept = self.conn.execute(
            "SELECT count(*) FROM monitor_events WHERE kind='run_summary'"
        ).fetchone()[0]
        self.assertEqual(kept, 3)
        newest = self.conn.execute(
            "SELECT max(recv_ts_ms) FROM monitor_events WHERE kind='run_summary'"
        ).fetchone()[0]
        self.assertEqual(newest, NOW)

    def test_keep_rows_cap_is_per_venue_and_keeps_the_newest(self):
        apply(self.conn, self.chosen(instrument_discovery=KindRule(
            days=None, keep_rows=5, per_venue=True)), now_ms=NOW)
        rows = self.conn.execute(
            "SELECT venue, count(*), max(recv_ts_ms), min(recv_ts_ms) "
            "FROM monitor_events WHERE kind='instrument_discovery' GROUP BY venue"
        ).fetchall()
        self.assertEqual(sorted(r[1] for r in rows), [5, 5])
        for _, _, newest, oldest in rows:
            self.assertEqual(newest, NOW)
            self.assertEqual(oldest, NOW - 4 * 3_600_000)

    def test_daily_statistics_are_written_before_the_rows_go(self):
        apply(self.conn, self.chosen(run_summary=KindRule(days=2)), now_ms=NOW)
        stats = self.conn.execute(
            "SELECT day, rows, stats_json FROM event_daily_stats "
            "WHERE kind='run_summary' ORDER BY day").fetchall()
        self.assertEqual(len(stats), 3)
        self.assertEqual(sum(row[1] for row in stats), 3)
        candidates = [json.loads(row[2])["max_candidates"] for row in stats]
        self.assertEqual(candidates, [0, 1, 2])       # the three oldest cycles

    def test_venue_tally_survives_the_basis_rows(self):
        apply(self.conn, self.chosen(basis_evaluation=KindRule(days=0.5)),
              now_ms=NOW)
        rows = self.conn.execute(
            "SELECT instrument, rows, priced, opportunities, max_entry_basis_bps "
            "FROM venue_daily_stats ORDER BY instrument").fetchall()
        self.assertEqual(len(rows), 20)               # the 20 pruned evaluations
        self.assertEqual({row[1] for row in rows}, {1})
        self.assertEqual({row[2] for row in rows}, {1})   # all priced
        self.assertEqual({row[3] for row in rows}, {0})   # none over 20 bps
        self.assertEqual(max(row[4] for row in rows), 9.0)
        left = self.conn.execute(
            "SELECT count(*) FROM monitor_events WHERE kind='basis_evaluation'"
        ).fetchone()[0]
        self.assertEqual(left, 20)

    def test_dry_run_reports_the_same_numbers_and_changes_nothing(self):
        chosen = self.chosen(
            run_summary=KindRule(days=2, keep_rows=2),
            instrument_discovery=KindRule(days=1, keep_rows=5, per_venue=True))
        before = self.conn.execute(
            "SELECT count(*) FROM monitor_events").fetchone()[0]
        planned = plan(self.conn, chosen, now_ms=NOW)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM monitor_events").fetchone()[0],
            before)
        applied = apply(self.conn, chosen, now_ms=NOW)
        for plan_entry, apply_entry in zip(planned, applied):
            self.assertEqual(plan_entry["kind"], apply_entry["kind"])
            self.assertEqual(
                plan_entry["by_age"]["rows"] + plan_entry["by_cap"]["rows"],
                apply_entry["deleted"]["rows"])

    def test_an_unchanged_batch_is_not_counted_twice(self):
        chosen = self.chosen(run_summary=KindRule(days=2))
        apply(self.conn, chosen, now_ms=NOW)
        first = self.conn.execute(
            "SELECT sum(rows) FROM event_daily_stats WHERE kind='run_summary'"
        ).fetchone()[0]
        apply(self.conn, chosen, now_ms=NOW)          # nothing left to prune
        again = self.conn.execute(
            "SELECT sum(rows) FROM event_daily_stats WHERE kind='run_summary'"
        ).fetchone()[0]
        self.assertEqual(first, again)


class SignalWindowTests(unittest.TestCase):
    """Older than 48 hours, only a row that is itself a signal survives."""

    def setUp(self):
        self.dir = TemporaryDirectory()
        self.db = Path(self.dir.name) / "monitor.db"
        self.store = EventStore(self.db)
        old = NOW - 3 * DAY
        recent = NOW - 3_600_000
        self.store.record_market_event(
            "old-run", "basis_evaluation",
            {"status": "watch_only", "instrument": "okx:MRNA", "phase": "closed",
             "entry_basis_bps": "30"},
            old, venue="okx", asset="MRNA")
        self.store.record_market_event(
            "old-run", "basis_evaluation",
            {"status": "conditional_signal", "instrument": "okx:MRNA",
             "phase": "closed", "entry_basis_bps": "40"},
            old, venue="okx", asset="MRNA")
        self.store.record_market_event(
            "old-run", "alert", {"asset": "MRNA", "text": "signal"},
            old, venue="okx", asset="MRNA")
        self.store.record_market_event(
            "old-run", "perp_book", {"bids": []}, old, venue="okx")
        self.store.record_market_event(
            "new-run", "perp_book", {"bids": [1]}, recent, venue="okx")
        self.store.record_market_event(
            "new-run", "run_summary", {"candidate_count": 0}, recent)
        self.conn = connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.store.close()
        self.dir.cleanup()

    def test_old_tape_is_deleted_and_the_signal_rows_stay(self):
        policy = RetentionPolicy(raw_hours=48, keep_signals=True, batch_rows=10,
                                 rules={"run_summary": KindRule(days=2, keep_rows=1)})
        report = apply(self.conn, policy, now_ms=NOW)
        by_kind = {entry["kind"]: entry for entry in report}
        self.assertEqual(by_kind["perp_book"]["deleted"]["rows"], 1)
        self.assertEqual(by_kind["basis_evaluation"]["deleted"]["rows"], 1)
        self.assertEqual(by_kind["basis_evaluation"]["signals_kept"]["rows"], 1)
        self.assertEqual(by_kind["alert"]["deleted"]["rows"], 0)
        self.assertEqual(by_kind["alert"]["signals_kept"]["rows"], 1)
        # The row cap on run_summary must not eat the row inside the window.
        self.assertEqual(by_kind["run_summary"]["deleted"]["rows"], 0)
        left = dict(self.conn.execute(
            "SELECT kind, count(*) FROM monitor_events GROUP BY kind"))
        self.assertEqual(left["basis_evaluation"], 1)
        status = self.conn.execute(
            "SELECT json_extract(payload_json, '$.status') FROM monitor_events "
            "WHERE kind = 'basis_evaluation'").fetchone()[0]
        self.assertEqual(status, "conditional_signal")
        self.assertEqual(left["alert"], 1)
        self.assertEqual(left["perp_book"], 1)
        self.assertEqual(left["run_summary"], 1)
        rolled = self.conn.execute(
            "SELECT rows FROM event_daily_stats WHERE kind = 'basis_evaluation'"
        ).fetchone()[0]
        self.assertEqual(rolled, 1)

    def test_without_the_signal_exception_the_old_signal_row_is_deleted(self):
        apply(self.conn, RetentionPolicy(raw_hours=48, keep_signals=False,
                                         batch_rows=10), now_ms=NOW)
        left = self.conn.execute(
            "SELECT count(*) FROM monitor_events WHERE kind = 'alert'"
        ).fetchone()[0]
        self.assertEqual(left, 0)


class LatestSummaryQueryTests(unittest.TestCase):
    def test_newest_row_of_one_kind_is_an_index_scan(self):
        with TemporaryDirectory() as directory:
            db = Path(directory) / "monitor.db"
            store = EventStore(db)
            try:
                store.record_market_event("run", "run_summary", {"a": 1}, NOW)
                query_plan = store.conn.execute(
                    "EXPLAIN QUERY PLAN SELECT payload_json FROM monitor_events "
                    "WHERE kind='run_summary' ORDER BY id DESC LIMIT 1").fetchall()
                self.assertEqual(len(query_plan), 1)
                detail = query_plan[0][-1]
                self.assertIn("monitor_event_kind_id", detail)
                self.assertNotIn("TEMP B-TREE", detail)
            finally:
                store.close()


class CompactTests(unittest.TestCase):
    def test_rewrite_shrinks_the_file_and_keeps_every_surviving_row(self):
        with TemporaryDirectory() as directory:
            db = Path(directory) / "monitor.db"
            store = EventStore(db)
            try:
                for i in range(200):             # ~2 MB of prunable payload
                    store.record_market_event(
                        "run", "instrument_discovery", {"blob": "x" * 10_000},
                        NOW - (200 - i) * 3_600_000, venue="gate")
                store.record_market_event("run", "run_summary",
                                          {"candidate_count": 1}, NOW)
            finally:
                store.close()
            conn = connect(db)
            apply(conn, RetentionPolicy(
                default_days=7, batch_rows=50,
                rules={"instrument_discovery": KindRule(days=None, keep_rows=5)}),
                now_ms=NOW)
            conn.close()
            with sqlite3.connect(db) as check:
                check.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            before = db.stat().st_size
            result = compact(db, log=lambda *_: None)
            self.assertLess(result["after_bytes"], before)
            self.assertIsNotNone(result["backup"])
            self.assertTrue(Path(result["backup"]).exists())
            with sqlite3.connect(db) as check:
                self.assertEqual(
                    check.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(
                    check.execute("PRAGMA auto_vacuum").fetchone()[0], 2)
                self.assertEqual(
                    check.execute("SELECT count(*) FROM monitor_events "
                                  "WHERE kind='instrument_discovery'").fetchone()[0],
                    5)

    def test_rewrite_refuses_to_run_under_a_live_writer(self):
        with TemporaryDirectory() as directory:
            db = Path(directory) / "monitor.db"
            store = EventStore(db)
            try:
                store.record_market_event("run", "run_summary", {"a": 1}, NOW)
            finally:
                store.close()
            writer = sqlite3.connect(db, timeout=1.0)
            writer.execute("BEGIN IMMEDIATE")
            try:
                with self.assertRaises(RuntimeError):
                    compact(db, log=lambda *_: None)
            finally:
                writer.execute("ROLLBACK")
                writer.close()

    def test_a_later_prune_hands_pages_back_without_another_rewrite(self):
        """The rebuild turns on incremental auto-vacuum, so the next prune shrinks."""
        with TemporaryDirectory() as directory:
            db = Path(directory) / "monitor.db"
            store = EventStore(db)
            try:
                store.record_market_event("run", "run_summary", {"a": 1}, NOW)
            finally:
                store.close()
            compact(db, log=lambda *_: None)
            store = EventStore(db)
            try:
                for i in range(200):
                    store.record_market_event(
                        "run", "instrument_discovery", {"blob": "x" * 10_000},
                        NOW - (200 - i) * 3_600_000, venue="gate")
            finally:
                store.close()
            with sqlite3.connect(db) as check:
                check.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            grown = db.stat().st_size
            conn = connect(db)
            try:
                apply(conn, RetentionPolicy(
                    default_days=7, batch_rows=50,
                    rules={"instrument_discovery": KindRule(days=None,
                                                            keep_rows=5)}),
                    now_ms=NOW)
            finally:
                conn.close()
            shrunk = db.stat().st_size
            self.assertLess(shrunk, grown / 2)


class PolicyTests(unittest.TestCase):
    def test_the_shipped_policy_keeps_48_hours_and_the_signal_rows(self):
        chosen = load_policy(CONFIG)
        self.assertEqual(chosen.raw_hours, 48)
        self.assertTrue(chosen.keep_signals)
        self.assertGreater(chosen.default_days, 0)
        # The hour window is the bound. A row cap must not be sitting in the
        # shipped file ready to delete inside those 48 hours.
        self.assertIsNone(chosen.rule("run_summary").keep_rows)
        self.assertIsNone(chosen.rule("instrument_discovery").keep_rows)

    def test_raw_hours_must_be_positive(self):
        with self.assertRaises(ValueError):
            policy_from_document({"retention": {"raw_hours": 0}})

    def test_shorthand_and_tables_are_both_accepted(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "retention.toml"
            path.write_text(
                "[retention]\n"
                "default_days = 3\n"
                "batch_rows = 100\n"
                "[retention.kinds]\n"
                "perp_book = 5\n"
                "run_summary = { days = 2, keep_rows = 400 }\n"
                "instrument_discovery = { days = 3, keep_rows = 12,"
                " per_venue = true }\n",
                encoding="utf-8")
            chosen = load_policy(path)
            self.assertEqual(chosen.default_days, 3)
            self.assertEqual(chosen.batch_rows, 100)
            self.assertEqual(chosen.rule("perp_book").days, 5)
            self.assertEqual(chosen.rule("run_summary").keep_rows, 400)
            self.assertTrue(chosen.rule("instrument_discovery").per_venue)
            self.assertEqual(chosen.rule("who_knows").days, 3)

    def test_a_zero_row_cap_is_rejected_rather_than_silently_ignored(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "retention.toml"
            path.write_text(
                '[retention.kinds]\nrun_summary = { keep_rows = 0 }\n',
                encoding="utf-8")
            with self.assertRaises(ValueError):
                load_policy(path)


class DiscoveryWriteTests(unittest.TestCase):
    """The 1 MB-per-visit dump is stored once, not on every metadata refresh."""

    def test_an_identical_payload_is_not_stored_twice(self):
        config = load_config(str(CONFIG))
        with TemporaryDirectory() as directory:
            monitor = HKMonitor(config, db=str(Path(directory) / "monitor.db"))
            try:
                payload = {"symbols": [{"symbol": "XIAOMI_USDT"}], "serverTime": 1}
                monitor._record_discovery("run", "gate", payload, NOW)
                monitor._record_discovery("run", "gate", payload, NOW + 1_000)
                changed = {"symbols": [{"symbol": "XIAOMI_USDT"},
                                       {"symbol": "MEITUAN_USDT"}],
                           "serverTime": 2}
                monitor._record_discovery("run", "gate", changed, NOW + 2_000)
                rows = monitor.store.conn.execute(
                    "SELECT count(*) FROM monitor_events "
                    "WHERE kind='instrument_discovery'").fetchone()[0]
                self.assertEqual(rows, 2)
            finally:
                monitor.close()


if __name__ == "__main__":
    unittest.main()
