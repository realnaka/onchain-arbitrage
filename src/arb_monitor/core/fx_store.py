"""Durable FX snapshots used by deterministic trading calculations.

The execution path reads the latest acceptable snapshot; it never calls an LLM
or a remote service while calculating an opportunity.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class FxSnapshot:
    pair: str
    rate: Decimal
    observed_at_ms: int
    source: str


class FxStore:
    def __init__(self, path: str | Path = "data/fx.sqlite"):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS fx_snapshots (
              pair TEXT NOT NULL, rate TEXT NOT NULL, observed_at_ms INTEGER NOT NULL,
              source TEXT NOT NULL
            )"""
        )
        self.conn.commit()

    def put(self, snapshot: FxSnapshot) -> None:
        if snapshot.rate <= 0:
            raise ValueError("FX rate must be positive")
        self.conn.execute(
            "INSERT INTO fx_snapshots(pair,rate,observed_at_ms,source) VALUES(?,?,?,?)",
            (snapshot.pair, str(snapshot.rate), snapshot.observed_at_ms, snapshot.source),
        )
        self.conn.commit()

    def latest(self, pair: str = "USD/HKD") -> FxSnapshot | None:
        row = self.conn.execute(
            "SELECT pair,rate,observed_at_ms,source FROM fx_snapshots "
            "WHERE pair=? ORDER BY observed_at_ms DESC LIMIT 1", (pair,)
        ).fetchone()
        if row is None:
            return None
        return FxSnapshot(row[0], Decimal(row[1]), int(row[2]), row[3])

    def close(self) -> None:
        self.conn.close()
