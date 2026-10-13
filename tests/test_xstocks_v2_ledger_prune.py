"""Ledger retention: drop old paper rows, never a real-money row.

Measured 2026-10-07: ``data-v2/xstocks_v2_cycles.jsonl`` had 1656 rows / 838 KB
and no retention policy, growing ~7k rows a day because one paper round writes
~60 lines. The rows that say where the coins are have ``mode == "live"``.
"""
from __future__ import annotations

import json
import importlib.util
import tempfile
import unittest
from pathlib import Path

from arb_monitor.xstocks_v2 import ledger

DAY_MS = 24 * 60 * 60 * 1000
NOW = 1_800_000_000_000


def _paper_runner():
    """The paper runner script, loaded the way it is run (it is a script)."""
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "v2paper", root / "scripts" / "xstocks_v2_paper.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _line(*, ts_ms, mode, state, symbol="SPYx", held_at="", reason=""):
    return json.dumps({"ts_ms": ts_ms, "mode": mode, "state": state,
                       "symbol": symbol, "family": "chain_to_kraken",
                       "chain": "solana", "held_at": held_at,
                       "reason": reason, "extra": {"step": "finish"}})


class EffectiveHeldAtTest(unittest.TestCase):
    """A ``probed`` row that predates ``final_held_at`` must not read as in flight.

    Measured 2026-10-07: the NVDAx probe row (21:36) carries the last step's
    worst case, ``held_at: in_bridge``, while the 0.0065 NVDAx had already landed
    in our Solana wallet -- so the board said a message was still crossing.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "cycles.jsonl"

    def _write(self, *lines):
        self.path.write_text("\n".join(lines) + "\n")

    def test_an_old_probed_row_reads_as_our_wallet(self):
        self._write(json.dumps({
            "ts_ms": NOW - 1000, "mode": "live", "state": "probed",
            "symbol": "NVDAx", "held_at": "in_bridge",
            "reason": "probe arrived; route sample written",
            "extra": {"step": "finish"}}))
        got = ledger.carried_inventory(self.path)
        self.assertEqual(got["NVDAx"]["held_at"], "dest_wallet")
        self.assertEqual(ledger.open_positions(self.path)[0]["held_at"],
                         "dest_wallet")

    def test_an_explicit_final_position_wins(self):
        self._write(json.dumps({
            "ts_ms": NOW, "mode": "live", "state": "probed", "symbol": "SPCXx",
            "held_at": "in_bridge", "final_held_at": "kraken"}))
        self.assertEqual(ledger.effective_held_at(
            ledger.carried_inventory(self.path)["SPCXx"]), "kraken")

    def test_a_stopped_row_keeps_its_recorded_worst_case(self):
        """For stopped/unknown the worst case *is* the honest answer."""
        entry = {"state": "stopped", "held_at": "in_bridge"}
        self.assertEqual(ledger.effective_held_at(entry), "in_bridge")
        # A row with no position recorded stays empty: nothing is invented.
        self.assertEqual(ledger.effective_held_at({"state": "unknown"}), "")

    def test_the_live_ledger_now_reports_the_probes_where_they_are(self):
        """The real file: NVDAx and SPCXx are in our wallet, not in a bridge."""
        positions = {row["symbol"]: row["held_at"]
                     for row in ledger.open_positions(ledger.DEFAULT_PATH)}
        for symbol in ("NVDAx", "SPCXx"):
            if symbol in positions:
                self.assertEqual(positions[symbol], "dest_wallet", symbol)


class PruneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "cycles.jsonl"

    def _write(self, lines):
        self.path.write_text("\n".join(lines) + "\n")

    def test_old_paper_rows_go_and_live_rows_stay(self):
        self._write([
            _line(ts_ms=NOW - 10 * DAY_MS, mode="live", state="sold"),
            _line(ts_ms=NOW - 10 * DAY_MS, mode="paper", state="refused"),
            _line(ts_ms=NOW - 1 * DAY_MS, mode="paper", state="refused"),
        ])
        result = ledger.prune(self.path, keep_paper_days=2, now_ms=NOW)
        self.assertEqual(result["dropped"], 1)
        self.assertEqual(result["kept"], 2)
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual([row["mode"] for row in rows], ["live", "paper"])
        self.assertEqual(rows[0]["state"], "sold")

    def test_a_held_live_row_survives_any_age(self):
        """The row that says where the coins are must never be dropped."""
        self._write([
            _line(ts_ms=NOW - 400 * DAY_MS, mode="live", state="stopped",
                  held_at="kraken", reason="deposit:credit_unreadable:NameError"),
            _line(ts_ms=NOW - 400 * DAY_MS, mode="paper", state="refused"),
        ])
        ledger.prune(self.path, keep_paper_days=2, now_ms=NOW)
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["held_at"], "kraken")

    def test_a_malformed_line_is_kept_and_counted(self):
        self._write([
            "{not json",
            _line(ts_ms=NOW - 9 * DAY_MS, mode="paper", state="refused"),
        ])
        result = ledger.prune(self.path, keep_paper_days=2, now_ms=NOW)
        self.assertEqual(result["malformed"], 1)
        self.assertEqual(result["dropped"], 1)
        self.assertIn("{not json", self.path.read_text())

    def test_a_dry_run_changes_nothing(self):
        lines = [_line(ts_ms=NOW - 9 * DAY_MS, mode="paper", state="refused")]
        self._write(lines)
        before = self.path.read_text()
        result = ledger.prune(self.path, keep_paper_days=2, now_ms=NOW,
                              dry_run=True)
        self.assertEqual(result["dropped"], 1)
        self.assertEqual(self.path.read_text(), before)

    def test_nothing_old_means_no_rewrite(self):
        self._write([_line(ts_ms=NOW - 1000, mode="paper", state="refused")])
        before = self.path.read_text()
        result = ledger.prune(self.path, keep_paper_days=2, now_ms=NOW)
        self.assertEqual(result["dropped"], 0)
        self.assertEqual(self.path.read_text(), before)

    def test_a_missing_file_is_not_an_error(self):
        result = ledger.prune(self.tmp / "nope.jsonl", now_ms=NOW)
        self.assertEqual(result["dropped"], 0)
        self.assertEqual(result["kept"], 0)

    def test_readers_still_see_the_positions_after_a_prune(self):
        """Pruning paper rows must not change what the gate believes."""
        self._write([
            _line(ts_ms=NOW - 9 * DAY_MS, mode="live", state="probed",
                  symbol="NVDAx", held_at="dest_wallet"),
            _line(ts_ms=NOW - 1 * DAY_MS, mode="paper", state="refused",
                  symbol="NVDAx"),
            _line(ts_ms=NOW - 9 * DAY_MS, mode="paper", state="refused",
                  symbol="SPCXx"),
        ])
        before = [row["symbol"] for row in ledger.open_positions(self.path)]
        ledger.prune(self.path, keep_paper_days=2, now_ms=NOW)
        after = [row["symbol"] for row in ledger.open_positions(self.path)]
        self.assertEqual(before, ["NVDAx"])
        self.assertEqual(after, before)

    def test_the_paper_loop_hook_runs_hourly_and_takes_a_str_data_dir(self):
        """The first version of this hook passed a ``str`` to ``/``.

        It failed as ``prune_error: TypeError: unsupported operand type(s) for /:
        'str' and 'str'`` in the live log (2026-10-07 23:0x), because the paper
        loop's ``data_dir`` is the raw ``--data-dir`` argument. The hook is
        exercised here with a string, exactly as the loop calls it.
        """
        paper_runner = _paper_runner()
        # The hook targets the ledger by its real name inside ``data_dir``.
        real = self.tmp / ledger.DEFAULT_PATH.name
        real.write_text(
            _line(ts_ms=NOW - 9 * DAY_MS, mode="paper", state="refused") + "\n"
            + _line(ts_ms=NOW - 1 * DAY_MS, mode="paper", state="refused") + "\n"
            + _line(ts_ms=NOW - 9 * DAY_MS, mode="live", state="sold") + "\n")
        # Round 2 of 20 is not the hourly one.
        self.assertIsNone(paper_runner.prune_every_hour(
            str(self.tmp), 2, 180.0, now_ms=NOW))
        result = paper_runner.prune_every_hour(str(self.tmp), 1, 180.0, now_ms=NOW)
        self.assertEqual(result["dropped"], 1)
        rows = [json.loads(line) for line in real.read_text().splitlines()]
        self.assertEqual([row["mode"] for row in rows], ["paper", "live"])


if __name__ == "__main__":
    unittest.main()
