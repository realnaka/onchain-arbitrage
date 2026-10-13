from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from decimal import Decimal

from arb_monitor.core.models import Intent
from arb_monitor.core.hk import dumps


class EventStore:
    def __init__(self, path: str | Path = "data/arb_monitor.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        # Autocommit. A deferred transaction grabs a shared lock and then waits
        # to upgrade it; the retention job's BEGIN IMMEDIATE waits for that
        # shared lock. The two wait on each other and the sampler exits.
        # Measured 2026-09-28 23:37: the last event froze while one batch of 20
        # rows took ~75 s.
        self.conn.isolation_level = None
        self._write_lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        # The retention job takes a write lock for one batch at a time. Wait
        # for it instead of failing the sample that happened to land then.
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS opportunities (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              strategy TEXT NOT NULL,
              symbol TEXT NOT NULL,
              created_ts_ms INTEGER NOT NULL,
              expires_ts_ms INTEGER NOT NULL,
              net_edge_bps TEXT NOT NULL,
              expected_profit_quote TEXT NOT NULL,
              payload_json TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS monitor_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL, kind TEXT NOT NULL, asset TEXT NOT NULL,
                venue TEXT NOT NULL, source TEXT NOT NULL,
                venue_ts_ms INTEGER, recv_ts_ms INTEGER NOT NULL, sequence INTEGER,
                status TEXT NOT NULL, payload_json TEXT NOT NULL
            )"""
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS monitor_event_kind_time ON monitor_events(kind, recv_ts_ms)"
        )
        # The boards ask for the newest row of one kind (``ORDER BY id DESC
        # LIMIT 1``) and the retention job asks for "the rows of one kind older
        # than the N-th newest" — with only ``(kind, recv_ts_ms)`` both of those
        # need a temp B-tree over every row of that kind. Measured 2026-09-17 on
        # the 4.4 GB production database: 5.5 s for one indexed-looking row.
        # This index makes both an index scan.
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS monitor_event_kind_id ON monitor_events(kind, id)"
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS manual_executions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              opportunity_id INTEGER,
              status TEXT NOT NULL,
              note TEXT,
              created_ts_ms INTEGER NOT NULL
            )
            """
        )

    def record_intent(self, intent: Intent) -> int:
        with self._write_lock:
            cur = self.conn.execute(
                """
                INSERT INTO opportunities
                (strategy, symbol, created_ts_ms, expires_ts_ms, net_edge_bps,
                 expected_profit_quote, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent.strategy,
                    intent.symbol,
                    intent.created_ts_ms,
                    intent.created_ts_ms + intent.ttl_ms,
                    str(intent.net_edge_bps),
                    str(intent.expected_profit_quote),
                    dumps(_intent_payload(intent)),
                ),
            )
        return int(cur.lastrowid)

    def record_manual_execution(
        self, opportunity_id: int, status: str, note: str, created_ts_ms: int
    ) -> None:
        with self._write_lock:
            self.conn.execute(
                """
                INSERT INTO manual_executions
                (opportunity_id, status, note, created_ts_ms)
                VALUES (?, ?, ?, ?)
                """,
                (opportunity_id, status, note, created_ts_ms),
            )

    def close(self) -> None:
        with self._write_lock:
            self.conn.close()

    def record_market_event(
        self, run_id: str, kind: str, payload: Any, recv_ts_ms: int,
        *, asset: str = "", venue: str = "", source: str = "", status: str = "ok",
        venue_ts_ms: int | None = None, sequence: int | None = None,
    ) -> int:
        cur = self._write_retrying(
            """INSERT INTO monitor_events
               (run_id,kind,asset,venue,source,venue_ts_ms,recv_ts_ms,sequence,status,payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (run_id, kind, asset, venue, source, venue_ts_ms, recv_ts_ms, sequence, status,
             dumps(payload)),
        )
        return int(cur.lastrowid)

    def _write_retrying(self, sql: str, params: tuple):
        """Commit one write, waiting out a retention batch that holds the lock.

        ``busy_timeout`` already waits inside one attempt. A batch of fat rows
        can still outlast it. Raising here exits the sampler (the supervisor
        then starts a *second* prune). Retry instead.
        """
        delay = 0.25
        for attempt in range(6):
            try:
                with self._write_lock:
                    return self.conn.execute(sql, params)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 5:
                    raise
                try:
                    self.conn.rollback()
                except sqlite3.Error:
                    pass
            time.sleep(delay)
            delay = min(delay * 2, 4.0)
        raise sqlite3.OperationalError("database is locked")

    def latest_summary(self) -> dict | None:
        with self._write_lock:
            row = self.conn.execute(
                "SELECT payload_json FROM monitor_events WHERE kind='run_summary' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return json.loads(row[0], parse_float=Decimal) if row else None

    def recent_summaries(self, limit: int = 31) -> list[dict]:
        """Return recent run summaries with their receive timestamps.

        This is intentionally a small, indexed read for the read-only board;
        the monitor loop and the trading decisions never depend on it.
        """
        limit = max(1, min(int(limit), 200))
        with self._write_lock:
            rows = self.conn.execute(
                "SELECT recv_ts_ms, payload_json FROM monitor_events "
                "WHERE kind='run_summary' ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for recv_ts_ms, payload_json in rows:
            item = json.loads(payload_json, parse_float=Decimal)
            item["_recv_ts_ms"] = recv_ts_ms
            result.append(item)
        return result

    def latest_event_meta(self, kind: str) -> dict | None:
        """Return the newest event's receive/venue times for diagnostics."""
        with self._write_lock:
            row = self.conn.execute(
                "SELECT recv_ts_ms, venue_ts_ms, status FROM monitor_events "
                "WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,)
            ).fetchone()
        if not row:
            return None
        return {"recv_ts_ms": row[0], "venue_ts_ms": row[1], "status": row[2]}

    def events(self, kind: str, since_ms: int = 0) -> list[dict]:
        with self._write_lock:
            rows = self.conn.execute(
                """SELECT payload_json FROM monitor_events
                   WHERE kind=? AND recv_ts_ms>=? ORDER BY id""", (kind, since_ms),
            ).fetchall()
        return [json.loads(row[0], parse_float=Decimal) for row in rows]

    def latest_event(self, kind: str) -> dict | None:
        """The most recent row of one kind, or ``None`` if there is none.

        Used for state that is measured on a slower clock than the cycle and
        has to survive a restart (the issuer discovery funnel): reading the
        whole history with ``events`` would grow with retention, and the kind
        is written once per scan, not once per cycle.
        """
        with self._write_lock:
            row = self.conn.execute(
                """SELECT payload_json FROM monitor_events
                   WHERE kind=? ORDER BY id DESC LIMIT 1""", (kind,),
            ).fetchone()
        return json.loads(row[0], parse_float=Decimal) if row else None


def _intent_payload(intent: Intent) -> dict:
    return {
        "strategy": intent.strategy,
        "symbol": intent.symbol,
        "net_edge_bps": str(intent.net_edge_bps),
        "expected_profit_quote": str(intent.expected_profit_quote),
        "ttl_ms": intent.ttl_ms,
        "created_ts_ms": intent.created_ts_ms,
        "metadata": intent.metadata,
        "legs": [
            {
                "venue": leg.venue,
                "symbol": leg.symbol,
                "side": leg.side.value,
                "price": str(leg.price),
                "quantity": str(leg.quantity),
                "notional": str(leg.notional),
            }
            for leg in intent.legs
        ],
    }
