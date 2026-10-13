from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any


def _json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


class ExecutionLedger:
    """Durable, append-only execution event ledger used by paper and live adapters."""

    def __init__(self, path: str | Path = ":memory:", *,
                 busy_timeout_ms: int = 5000, wal: bool = True):
        # Reviewed 2026-09-16: the monitor, the unattended executor, the
        # dashboard and a human running `ledger-reconcile` all write this file.
        # With the default rollback journal and no busy timeout, a concurrent
        # write is a `database is locked` exception at the worst possible
        # moment — in the middle of an execution. WAL plus a busy timeout turns
        # that into a short wait. Statements are committed one at a time and no
        # transaction is held across a network call.
        self.conn = sqlite3.connect(str(path), timeout=busy_timeout_ms / 1000)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        if wal and str(path) != ":memory:":
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.DatabaseError:
                # A filesystem without WAL support (some network mounts) still
                # gets the busy timeout, which is the part that prevents the
                # mid-execution failure.
                pass
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS execution_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL,
            signal_id TEXT, state TEXT NOT NULL, event_type TEXT NOT NULL,
            payload TEXT NOT NULL, created_ts_ms INTEGER NOT NULL)"""
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_execution_group ON execution_events(group_id,id)"
        )
        self.conn.commit()

    def ping(self, timeout_ms: int = 2000) -> bool:
        """Can this process write right now? Used by the recovery演练 code."""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute("ROLLBACK")
                return True
            except sqlite3.OperationalError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.05)

    def append(self, group_id: str, state: str, event_type: str,
               payload: dict[str, Any], created_ts_ms: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO execution_events(group_id,signal_id,state,event_type,payload,created_ts_ms)"
            " VALUES(?,?,?,?,?,?)",
            (group_id, payload.get("signal_id"), state, event_type, _json(payload), created_ts_ms),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def events(self, group_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM execution_events WHERE group_id=? ORDER BY id", (group_id,)
        ).fetchall()
        return [dict(r, payload=json.loads(r["payload"])) for r in rows]

    def last(self, group_id: str) -> dict[str, Any] | None:
        rows = self.events(group_id)
        return rows[-1] if rows else None

    def recover(self) -> dict[str, dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        for row in self.conn.execute(
            "SELECT * FROM execution_events WHERE id IN "
            "(SELECT MAX(id) FROM execution_events GROUP BY group_id)"
        ):
            groups[row["group_id"]] = dict(row, payload=json.loads(row["payload"]))
        return groups

    def close(self) -> None:
        self.conn.close()

    def record_settled_group(self, group_id: str, signal_id: str, fills: list[dict[str, Any]],
                             realized_pnl: Decimal, created_ts_ms: int) -> int:
        """Import a reconciled external execution without pretending it was paper-filled."""
        return self.append(group_id, "closed", "external_settlement", {
            "signal_id": signal_id, "fills": fills,
            "realized_pnl": str(realized_pnl), "source": "okx_live_smoke",
        }, created_ts_ms)

    def close_paper_position(self, group_id: str, *, exit_basis_bps: Decimal,
                             created_ts_ms: int, note: str = "",
                             quote_observed_at_s: float | None = None
                             ) -> dict[str, Any]:
        """Close a modelled position and record what the model assumed.

        The arithmetic is stated rather than implied, and every input is stored
        with the result: entry basis and cost come from the opening snapshot,
        the exit basis is the number the caller observed, and the PnL is
        ``size × (entry_basis − exit_basis) / 10000`` gross of the round-trip
        cost that the entry snapshot already declares. Nothing here claims a
        real fill happened.
        """
        positions = {row["group_id"]: row for row in self.paper_positions()}
        row = positions.get(group_id)
        if row is None:
            raise KeyError(f"no_open_paper_position:{group_id}")
        if row["state"] != "open":
            raise ValueError(f"paper_position_already_closed:{group_id}")
        size = Decimal(str(row["size_quote"] or 0))
        entry_basis = (Decimal(str(row["entries"]))
                       if row["entries"] not in (None, "") else None)
        gross = (size * (entry_basis - Decimal(str(exit_basis_bps)))
                 / Decimal(10000) if entry_basis is not None else None)
        cost_bps = (Decimal(str(row["entry_cost_bps"]))
                    if row["entry_cost_bps"] not in (None, "") else None)
        # Entry cost is charged once per leg, so a round trip carries it twice.
        cost = (size * cost_bps * 2 / Decimal(10000)
                if cost_bps is not None else None)
        payload = {
            "group_id": group_id, "mode": "paper_close",
            "fill_assumption": "modelled_exit_at_observed_basis",
            "entry_basis_bps": (None if entry_basis is None else str(entry_basis)),
            "exit_basis_bps": str(Decimal(str(exit_basis_bps))),
            "size_quote": str(size),
            "entry_cost_bps": None if cost_bps is None else str(cost_bps),
            "modelled_gross_pnl_quote": None if gross is None else str(gross),
            "modelled_cost_quote": None if cost is None else str(cost),
            "modelled_net_pnl_quote": (None if gross is None or cost is None
                                       else str(gross - cost)),
            "quote_observed_at_s": quote_observed_at_s,
            "note": note,
        }
        self.append(group_id, "paper_closed", "paper_closed", payload, created_ts_ms)
        return payload

    def executed_quote_between(self, start_ts_ms: int, end_ts_ms: int, *,
                               include_paper: bool = True) -> Decimal:
        """Quote-token actually committed inside a window, from the fills.

        The daily quota needs a number that cannot drift from what happened, so
        it is a query over the same events the executions wrote — buy side only,
        so a sale cannot inflate the spend — rather than a remembered total.

        ``include_paper`` decides whether modelled fills count against the real
        cash limit. They used to be mixed in unconditionally, which is how the
        live board came to read 90.252212 of a 100 USDT day (2026-09-16) with
        no money moved at all. The real limit now counts real fills only, and
        the paper budget is reported separately by ``paper_quote_between``.
        """
        kinds = ("spot_filled",) if not include_paper else ("spot_filled", "paper_filled")
        marks = ",".join("?" for _ in kinds)
        total = Decimal(0)
        for row in self.conn.execute(
                "SELECT event_type, payload FROM execution_events"
                " WHERE created_ts_ms >= ? AND created_ts_ms < ?"
                f" AND event_type IN ({marks})",
                (int(start_ts_ms), int(end_ts_ms), *kinds)):
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if str(payload.get("leg") or "buy") != "buy":
                continue
            amount = payload.get("quote_amount")
            if amount in (None, ""):
                amount = payload.get("size_quote")
            if amount in (None, ""):
                continue
            total += abs(Decimal(str(amount)))
        return total

    def paper_quote_between(self, start_ts_ms: int, end_ts_ms: int) -> Decimal:
        """The modelled spend inside a window, kept apart from real cash."""
        total = Decimal(0)
        for row in self.conn.execute(
                "SELECT payload FROM execution_events"
                " WHERE created_ts_ms >= ? AND created_ts_ms < ?"
                " AND event_type = 'paper_filled'",
                (int(start_ts_ms), int(end_ts_ms))):
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            amount = payload.get("quote_amount") or payload.get("size_quote")
            if amount in (None, ""):
                continue
            total += abs(Decimal(str(amount)))
        return total

    def client_order_ids(self) -> dict[str, str]:
        """``clOrdId -> group_id`` for every order this project recorded.

        This is what lets a resting order be attributed to the group that
        placed it. Without it, an order found in `orders-pending` is anonymous,
        and an anonymous order is exactly what formed the 2026-09-16 naked
        short: nothing could say which run it belonged to or whether that run
        was still alive.
        """
        found: dict[str, str] = {}
        for row in self.conn.execute("SELECT group_id, payload FROM execution_events ORDER BY id"):
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            for value in _walk_client_order_ids(payload):
                found[value] = row["group_id"]
        return found

    def group_states(self) -> dict[str, str]:
        """group_id → its newest state, for orphan attribution."""
        return {group: (row.get("state") or "") for group, row in self.recover().items()}

    # --- open groups -----------------------------------------------------
    def group_instrument(self, group_id: str) -> str | None:
        """The perpetual instrument a group traded, from its own plan snapshot.

        Read from the ``plan`` the group recorded at precheck, not from the
        command line: a funding rule that closed the wrong instrument would be
        worse than not closing at all.
        """
        for row in self.conn.execute(
                "SELECT payload FROM execution_events WHERE group_id=? ORDER BY id",
                (group_id,)):
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            plan = payload.get("plan") or {}
            perp = plan.get("perp") or payload.get("perp")
            if isinstance(perp, str) and ":" in perp:
                return perp.split(":", 1)[1]
            if isinstance(perp, str) and perp:
                return perp
            instrument = plan.get("perp_instrument") or payload.get("instrument")
            if instrument:
                return str(instrument)
        return None

    def group_asset(self, group_id: str) -> str | None:
        """The asset named on this group's own events, or None if it conflicts.

        Used only when ``group_instrument`` is empty. Two different asset names
        in one group is not a position we can address.
        """
        found: set[str] = set()
        for row in self.conn.execute(
                "SELECT payload FROM execution_events WHERE group_id=? ORDER BY id",
                (group_id,)):
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            plan = payload.get("plan") or {}
            asset = payload.get("asset") or plan.get("asset")
            if isinstance(asset, str) and asset:
                found.add(asset)
        if len(found) == 1:
            return next(iter(found))
        return None

    def open_exposure_groups(self, *, states: set[str] | frozenset[str] | None = None
                             ) -> list[dict[str, Any]]:
        """Groups whose latest state may hold a position, newest first.

        The funding rule needs "what do we own and since when", and the ledger is
        the only place that knows when a group started. The venue is asked for
        the size; this only supplies identity and time.
        """
        # Do not reduce this to the two happy-path states.  A crash or a
        # partial/unknown close is precisely when the latest state is named
        # ``maker_partial``/``close_unknown``/``manual_intervention_required``
        # and the venue may still hold risk.  Exit protection must discover
        # those groups too; the venue position read below the caller remains
        # the authority for size.
        wanted = states or frozenset({
            "hedged", "spot_filled", "perp_submitted", "maker_partial",
            "maker_unknown", "close_perp_underfilled", "unhedged_spot_only",
            "spot_short_of_hedge", "spot_over_hedge", "naked_short_unconfirmed",
            "perp_unknown", "close_unknown", "manual_intervention_required",
            "spot_not_confirmed", "unwound_after_spot_failure",
            # A close can settle the perp leg before the spot sale. Until a
            # later closed-by/reconciliation event proves the spot leg is gone,
            # these states remain candidates for residual exposure.
            "perp_closed", "closed_partial",
        })
        rows: list[dict[str, Any]] = []
        opened_at: dict[str, int] = {}
        for row in self.conn.execute(
                "SELECT group_id, MIN(created_ts_ms) AS first_ms FROM execution_events"
                " GROUP BY group_id"):
            opened_at[row["group_id"]] = int(row["first_ms"])
        for group_id, row in self.recover().items():
            if row.get("state") not in wanted:
                continue
            rows.append({
                "group_id": group_id,
                "state": row.get("state"),
                "opened_at_ms": opened_at.get(group_id, int(row.get("created_ts_ms") or 0)),
                "last_event_ms": int(row.get("created_ts_ms") or 0),
                "instrument": self.group_instrument(group_id),
            })
        rows.sort(key=lambda item: item["opened_at_ms"], reverse=True)
        return rows

    # --- paper positions -------------------------------------------------
    def paper_positions(self) -> list[dict[str, Any]]:
        """Modelled groups that were opened and not yet closed.

        Paper used to be a pair of events and nothing else: it consumed budget,
        and there was no way to ask "what am I supposedly holding, and at what
        basis". A paper run that cannot be closed cannot be judged, so the
        opening fill is a position here, and ``paper_close`` ends it.
        """
        opened: dict[str, dict[str, Any]] = {}
        for row in self.conn.execute(
                "SELECT * FROM execution_events WHERE event_type IN"
                " ('paper_filled','paper_closed','paper_close_reconciled')"
                " ORDER BY id"):
            payload = json.loads(row["payload"])
            group = row["group_id"]
            if row["event_type"] == "paper_filled":
                opened[group] = {
                    "group_id": group, "opened_ms": row["created_ts_ms"],
                    "state": "open", "entry": payload,
                    "size_quote": payload.get("size_quote")
                                  or payload.get("quote_amount"),
                    "entries": (payload.get("plan") or {}).get(
                        "gross_entry_basis_bps")
                              or payload.get("entry_basis_bps"),
                    "entry_cost_bps": payload.get("entry_cost_bps"),
                }
            elif group in opened:
                opened[group].update(state="closed", closed_ms=row["created_ts_ms"],
                                     exit=payload)
        return list(opened.values())

    def paper_stats(self) -> dict[str, Any]:
        """What the paper ledger can and cannot answer today.

        Deliberately reports the sample size next to every number, and lists the
        metrics it has *no* data for, because the paper chain assumes a fill at
        the touch: it measures the cost and quota plumbing, not the probability
        of being filled.
        """
        positions = self.paper_positions()
        closed = [row for row in positions if row["state"] == "closed"]
        open_count = len(positions) - len(closed)
        return {
            "groups": len(positions),
            "open_positions": open_count,
            "closed_positions": len(closed),
            "modelled_quote": str(sum((Decimal(str(row["size_quote"] or 0))
                                       for row in positions), Decimal(0))),
            "entry_basis_bps": [row["entries"] for row in positions],
            "unavailable": [
                "maker_fill_probability", "maker_wait_time",
                "partial_fill_distribution", "taker_fallback_ratio",
                "realized_slippage_at_size", "funding_cashflow",
            ],
            "unavailable_reason": (
                "paper 假设挂单在盘口全额成交（fill_assumption=full_fill_at_touch），"
                "没有模拟排队、部分成交和转 taker 的条件概率；"
                "这些指标需要先实现纸面成交模拟器，或改用真实人工成交样本"),
        }

    def recent_execution_assets(self, start_ts_ms: int) -> dict[str, int]:
        """asset → newest execution timestamp inside a window.

        Used by the unattended executor so that a restart in the middle of a
        persistent signal does not immediately re-execute a trade this process
        already did: the cooldown lives in memory for the running loop and in
        the ledger across restarts.
        """
        found: dict[str, int] = {}
        for row in self.conn.execute(
                "SELECT payload, created_ts_ms FROM execution_events"
                " WHERE created_ts_ms >= ?"
                " AND event_type IN ('paper_filled','spot_filled')"
                " ORDER BY id", (int(start_ts_ms),)):
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            asset = payload.get("asset") or (payload.get("plan") or {}).get("asset")
            if not asset:
                continue
            found[str(asset)] = max(found.get(str(asset), 0), int(row["created_ts_ms"]))
        return found

    def swap_tx_hashes(self) -> list[str]:
        """Every on-chain transaction this project actually sent.

        Used by the gas watcher to separate "our trade path stopped being
        sponsored" from "some unrelated transaction paid a fee".
        """
        found: list[str] = []
        for row in self.conn.execute("SELECT payload FROM execution_events ORDER BY id"):
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            for value in _walk_tx_hashes(payload):
                if value not in found:
                    found.append(value)
        return found


def _walk_tx_hashes(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in {"tx_hash", "spot_tx", "approval_tx", "swap_tx"} and isinstance(value, str):
                if value.startswith("0x") and len(value) == 66:
                    yield value
            else:
                yield from _walk_tx_hashes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_tx_hashes(item)


def _walk_client_order_ids(node: Any):
    """Every clOrdId-shaped string in an event payload, at any depth."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in {"client_order_id", "clOrdId", "clOrdID"} and isinstance(value, str):
                if value:
                    yield value
            else:
                yield from _walk_client_order_ids(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_client_order_ids(item)
