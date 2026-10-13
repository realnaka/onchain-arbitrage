"""Rotation: a rail that never wins a quote slot still gets priced.

Measured 2026-10-08 00:06: Kraken offers 300 (asset, hub) withdrawal rails and
one round quoted 8 of them. 160 Solana rails produced no row at all, although
every online xStock has a Solana mint -- they lost the ranking to a larger edge
of the moment. Lessons C: "not sampled" is not "no opportunity".
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from arb_monitor.xstocks_v2 import rotation, scan
from arb_monitor.xstocks_v2.policy import V2Policy

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "ops" / "xstocks_v2_policy.toml"
NOW = 1_800_000_000_000
MINUTE = 60 * 1000
HOUR = 60 * MINUTE


class QueueTest(unittest.TestCase):
    def test_a_never_quoted_rail_leads_the_queue(self):
        queue = rotation.waiting({}, candidates=["SPYx", "SOXLx"], now_ms=NOW)
        self.assertEqual([item["symbol"] for item in queue], ["SOXLx", "SPYx"])
        self.assertTrue(all(item["never_quoted"] for item in queue))
        self.assertTrue(all(item["waited_s"] is None for item in queue))

    def test_the_longest_wait_comes_first(self):
        ledger = {"TSLAx": {"last_slot_ms": NOW - 2 * HOUR},
                  "SPYx": {"last_slot_ms": NOW - 10 * MINUTE}}
        queue = rotation.waiting(ledger, candidates=["SPYx", "TSLAx", "NVDAx"],
                                 now_ms=NOW)
        # Never quoted first, then the oldest slot, then the newest.
        self.assertEqual([item["symbol"] for item in queue],
                         ["NVDAx", "TSLAx", "SPYx"])
        self.assertEqual(queue[1]["waited_s"], 2 * 3600)

    def test_an_excluded_rail_is_not_in_the_queue(self):
        """The names the ranking already picked do not need a rotation slot."""
        queue = rotation.waiting({}, candidates=["SPYx", "SOXLx"],
                                 exclude={"SPYx"}, now_ms=NOW)
        self.assertEqual([item["symbol"] for item in queue], ["SOXLx"])

    def test_being_looked_at_moves_a_rail_to_the_back(self):
        queue = rotation.waiting({}, candidates=["SPYx", "SOXLx"], now_ms=NOW)
        picked = [queue[0]["symbol"]]
        ledger = rotation.mark({}, picked, now_ms=NOW)
        again = rotation.waiting(ledger, candidates=["SPYx", "SOXLx"],
                                 now_ms=NOW + MINUTE)
        self.assertEqual(picked, ["SOXLx"])
        self.assertEqual([item["symbol"] for item in again], ["SPYx", "SOXLx"])
        self.assertFalse(again[1]["never_quoted"])
        self.assertEqual(again[1]["slots"], 1)


class LedgerTest(unittest.TestCase):
    def test_the_ledger_survives_a_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rotation.json"
            rotation.save(path, {"SPYx": {"last_slot_ms": NOW, "slots": 2}},
                          now_ms=NOW)
            self.assertEqual(rotation.load(path),
                             {"SPYx": {"last_slot_ms": NOW, "slots": 2}})

    def test_an_unreadable_ledger_is_empty_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rotation.json"
            path.write_text("{not json")
            self.assertEqual(rotation.load(path), {})
            self.assertEqual(rotation.load(Path(tmp) / "missing.json"), {})
            path.write_text(json.dumps({"rails": "nope"}))
            self.assertEqual(rotation.load(path), {})

    def test_prune_drops_a_rail_that_is_no_longer_quotable(self):
        entries = {"SPYx": {"last_slot_ms": NOW}, "GONEx": {"last_slot_ms": NOW}}
        rotation.prune(entries, candidates=["SPYx"])
        self.assertEqual(sorted(entries), ["SPYx"])

    def test_an_empty_candidate_list_does_not_wipe_the_ledger(self):
        """No candidates means this round learned nothing, not "no rails"."""
        entries = {"SPYx": {"last_slot_ms": NOW}}
        rotation.prune(entries, candidates=[])
        self.assertEqual(sorted(entries), ["SPYx"])


class ReserveOrderTest(unittest.TestCase):
    """The reserve comes out of the budget, not on top of it."""

    def _wanted(self, names):
        return [{"symbol": name} for name in names]

    def test_the_tail_makes_room_and_the_top_keeps_its_place(self):
        wanted = self._wanted(["A", "B", "C", "D"])
        queue = rotation.waiting({}, candidates=["Z", "Y"], now_ms=NOW)
        got, picks = rotation.reserve_order(wanted, queue=queue, slots=2, budget=4)
        self.assertEqual([entry["symbol"] for entry in got], ["A", "B", "Y", "Z"])
        self.assertEqual([item["symbol"] for item in picks], ["Y", "Z"])
        self.assertEqual(len(got), 4)          # the same number of quotes

    def test_zero_slots_leaves_the_ranking_alone(self):
        wanted = self._wanted(["A"])
        queue = rotation.waiting({}, candidates=["Z"], now_ms=NOW)
        got, picks = rotation.reserve_order(wanted, queue=queue, slots=0, budget=1)
        self.assertEqual([entry["symbol"] for entry in got], ["A"])
        self.assertEqual(picks, [])

    def test_an_empty_queue_leaves_the_ranking_alone(self):
        wanted = self._wanted(["A", "B"])
        got, picks = rotation.reserve_order(wanted, queue=[], slots=3, budget=2)
        self.assertEqual([entry["symbol"] for entry in got], ["A", "B"])
        self.assertEqual(picks, [])

    def test_a_rail_is_never_quoted_twice(self):
        """A rail the ranking already picked costs the reserve nothing."""
        wanted = self._wanted(["A", "B", "C"])
        queue = rotation.waiting({}, candidates=["B", "Z"], now_ms=NOW)
        got, picks = rotation.reserve_order(wanted, queue=queue, slots=2, budget=3)
        symbols = [entry["symbol"] for entry in got]
        # Only ``Z`` is new, so only one slot of room is taken: ``B`` keeps its
        # ranked place and ``C`` is the one that makes room.
        self.assertEqual(symbols, ["A", "B", "Z"])
        self.assertEqual(len(set(symbols)), 3)
        self.assertEqual([item["symbol"] for item in picks], ["B", "Z"])

    def test_more_picks_than_budget_does_not_overflow(self):
        wanted = self._wanted(["A", "B"])
        queue = rotation.waiting({}, candidates=["Z", "Y", "X"], now_ms=NOW)
        got, _picks = rotation.reserve_order(wanted, queue=queue, slots=3, budget=2)
        self.assertEqual([entry["symbol"] for entry in got], ["X", "Y", "Z"])


class PolicySlotTest(unittest.TestCase):
    def _load_with(self, line: str) -> V2Policy:
        text = re.sub(r"(?m)^rotation_slots.*$\n?", "", POLICY.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.toml"
            path.write_text(text.replace("[watch]", "[watch]\n" + line, 1))
            return V2Policy.load(path)

    def test_the_reserve_comes_from_the_policy_file(self):
        self.assertEqual(self._load_with("rotation_slots = 3").rotation_slots, 3)

    def test_a_file_without_the_key_reserves_nothing(self):
        self.assertEqual(self._load_with("").rotation_slots, 0)

    def test_a_negative_reserve_is_not_a_credit(self):
        self.assertEqual(self._load_with("rotation_slots = -4").rotation_slots, 0)

    def test_the_shipped_policy_has_the_knob(self):
        self.assertIn("rotation_slots", POLICY.read_text())


class CandidateJoinTest(unittest.TestCase):
    """The queue is built from the ``(asset, rows)`` join, not from the table.

    Measured 2026-10-08: the first live round passed the whole ``{asset: rows}``
    table to ``has_withdraw_method``, which iterates its keys and finds no row
    dict -- so every symbol read as "no Solana rail" and the rotation silently
    reserved nothing while the ledger filled up.
    """

    METHODS = {"SPYx": [{"method": "SPYx", "network": "Solana"}]}

    def test_the_asset_rows_are_what_the_helper_wants(self):
        self.assertTrue(scan.has_withdraw_method("SPYx", "solana",
                                                 self.METHODS["SPYx"]))
        self.assertFalse(scan.has_withdraw_method("SPYx", "solana", self.METHODS))

    def test_the_shipped_table_yields_solana_rails(self):
        table = scan.load_withdraw_methods().get("withdraw_methods") or {}
        if not table:
            self.skipTest("no cached withdraw-methods table in this checkout")
        rails = [symbol for symbol in table
                 if scan.has_withdraw_method(symbol, "solana", table[symbol])]
        self.assertGreater(len(rails), 100)


if __name__ == "__main__":
    unittest.main()
