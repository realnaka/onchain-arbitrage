"""Dead pools: measured once, skipped for a while, never forgotten.

Measured 2026-10-07: every round re-quoted pools the previous round had already
rejected for depth (29 USD MSTRx/BSC, 3.77 USD FluxBeam, 811 USD Manifest), and
the round was taking 84 s with `http_429 x6`.
"""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from arb_monitor.xstocks_v2 import dead_pools, scan

HOUR_MS = 60 * 60 * 1000
NOW = 1_800_000_000_000


def _scan_module():
    return scan


class EntryTest(unittest.TestCase):
    def test_a_pool_key_prefers_the_pool_address(self):
        self.assertEqual(
            dead_pools.key(venue="uniswap-v3-x-layer", pool_id="0xAB", chain="x-layer",
                           symbol="SPYx"),
            "uniswap-v3-x-layer:0xab")
        # Jupiter rows have no pool id: the (chain, symbol) pair is the identity.
        self.assertEqual(
            dead_pools.key(venue="jupiter", chain="solana", symbol="SPYx"),
            "jupiter:solana:SPYx")

    def test_marking_twice_keeps_the_first_timestamp(self):
        entries: dict = {}
        dead_pools.mark(entries, "k", now_ms=NOW, reason="below_depth", depth_usd="3.77")
        dead_pools.mark(entries, "k", now_ms=NOW + HOUR_MS, reason="below_depth")
        entry = entries["k"]
        self.assertEqual(entry["since_ms"], NOW)
        self.assertEqual(entry["hits"], 2)
        self.assertEqual(entry["last_seen_ms"], NOW + HOUR_MS)
        self.assertEqual(entry["depth_usd"], "3.77")   # the measurement survives

    def test_an_entry_expires(self):
        entries = {"k": {"since_ms": NOW, "reason": "below_depth"}}
        self.assertIsNotNone(dead_pools.is_dead(entries, "k", now_ms=NOW + HOUR_MS))
        self.assertIsNone(dead_pools.is_dead(
            entries, "k", now_ms=NOW + 7 * HOUR_MS))   # ttl is 6 h

    def test_sweep_drops_only_expired_entries(self):
        entries = {"old": {"since_ms": NOW - 7 * HOUR_MS},
                   "fresh": {"since_ms": NOW - HOUR_MS},
                   "broken": {"reason": "no timestamp"}}
        dropped = dead_pools.sweep(entries, now_ms=NOW)
        self.assertEqual(sorted(dropped), ["broken", "old"])
        self.assertEqual(sorted(entries), ["fresh"])

    def test_an_unknown_pool_is_not_dead(self):
        self.assertIsNone(dead_pools.is_dead({}, "nope", now_ms=NOW))

    def test_round_trip_through_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dead.json"
            entries: dict = {}
            dead_pools.mark(entries, "jupiter:solana:MSTRx", now_ms=NOW,
                            reason="below_depth", depth_usd="29")
            dead_pools.save(path, entries, now_ms=NOW)
            self.assertEqual(dead_pools.load(path), entries)
            # A corrupt file is an empty list, never an exception in a scan round.
            path.write_text("{not json")
            self.assertEqual(dead_pools.load(path), {})


class ScanWiringTest(unittest.TestCase):
    """The scan marks what it rejected and skips what is already marked."""

    def test_the_skip_key_matches_the_mark_key_for_a_solana_row(self):
        row = {"symbol": "MSTRx", "chain": "solana", "buy_venue": "jupiter",
               "pool_id": "", "source_chain": "solana"}
        marked = dead_pools.key(
            venue=row["buy_venue"], pool_id=row["pool_id"],
            chain=row["source_chain"], symbol=row["symbol"])
        skipped = dead_pools.key(venue="jupiter", chain="solana", symbol="MSTRx")
        self.assertEqual(marked, skipped)

    def test_the_skip_key_matches_for_a_two_hop_row(self):
        """``solana->x-layer`` marks the Solana mint, not the arrow string."""
        row = {"symbol": "SPYx", "chain": "solana->x-layer",
               "buy_venue": "jupiter", "pool_id": "",
               "source_chain": "solana"}
        marked = dead_pools.key(venue=row["buy_venue"], pool_id="",
                                chain=row["source_chain"], symbol=row["symbol"])
        self.assertEqual(marked, "jupiter:solana:SPYx")

    def test_an_evm_row_is_keyed_by_pool_address(self):
        row = {"symbol": "SPYx", "chain": "x-layer",
               "buy_venue": "uniswap-v3-x-layer",
               "pool_id": "0x07c40850F1B1F1E1D0F3F0B2E8B0A1C2D3E4F5A6"}
        marked = dead_pools.key(venue=row["buy_venue"], pool_id=row["pool_id"],
                                chain=row["chain"], symbol=row["symbol"])
        self.assertEqual(
            marked, "uniswap-v3-x-layer:0x07c40850f1b1f1e1d0f3f0b2e8b0a1c2d3e4f5a6")

    def test_the_scan_module_exposes_the_file_it_uses(self):
        self.assertEqual(_scan_module().DEAD_POOLS.name,
                         "xstocks_v2_dead_pools.json")


class VerdictDisciplineTest(unittest.TestCase):
    """A wrong entry must cost one round, not a TTL (measured 2026-10-08).

    TSLAx/x-layer and SPCXx/x-layer were marked dead in one round; the next hour
    measured a clean 10.5 / 11.2 bps round trip on the same pools. Six hours of
    not quoting them is six hours of not seeing a real spread.
    """

    def test_one_rejection_is_not_enough_to_skip(self):
        entries: dict = {}
        dead_pools.mark(entries, "k", now_ms=NOW, reason="below_depth")
        self.assertIsNone(dead_pools.is_dead(entries, "k", now_ms=NOW,
                                            confirm_hits=2))
        dead_pools.mark(entries, "k", now_ms=NOW + 1, reason="below_depth")
        self.assertIsNotNone(dead_pools.is_dead(entries, "k", now_ms=NOW + 1,
                                               confirm_hits=2))

    def test_an_entry_from_before_the_counter_counts_as_one_observation(self):
        entries = {"k": {"since_ms": NOW, "reason": "below_depth"}}
        self.assertIsNotNone(dead_pools.is_dead(entries, "k", now_ms=NOW))
        self.assertIsNone(dead_pools.is_dead(entries, "k", now_ms=NOW,
                                            confirm_hits=2))

    def test_the_pool_nobody_looked_at_for_longest_is_rechecked_first(self):
        entries = {}
        dead_pools.mark(entries, "fresh", now_ms=NOW, reason="below_depth")
        dead_pools.mark(entries, "stale", now_ms=NOW, reason="below_depth")
        entries["fresh"]["last_seen_ms"] = NOW + HOUR_MS
        self.assertEqual(dead_pools.recheck_keys(entries, now_ms=NOW + HOUR_MS,
                                                 limit=1), ["stale"])
        self.assertEqual(dead_pools.recheck_keys(entries, now_ms=NOW + HOUR_MS,
                                                 limit=0), [])

    def test_rechecking_a_pool_moves_it_to_the_back_of_the_rotation(self):
        entries = {}
        for key in ("a", "b"):
            dead_pools.mark(entries, key, now_ms=NOW, reason="below_depth")
        first = dead_pools.recheck_keys(entries, now_ms=NOW, limit=1)[0]
        dead_pools.mark(entries, first, now_ms=NOW + 1, reason="below_depth")
        second = dead_pools.recheck_keys(entries, now_ms=NOW + 1, limit=1)[0]
        self.assertNotEqual(first, second)

    def test_an_expired_entry_is_not_rechecked(self):
        entries = {"k": {"since_ms": NOW - 7 * HOUR_MS, "hits": 3}}
        self.assertEqual(dead_pools.recheck_keys(entries, now_ms=NOW, limit=4), [])

    def test_clear_drops_a_verdict_that_did_not_hold_up(self):
        entries: dict = {}
        dead_pools.mark(entries, "k", now_ms=NOW, reason="below_depth")
        self.assertEqual(dead_pools.clear(entries, "k")["reason"], "below_depth")
        self.assertEqual(entries, {})
        self.assertIsNone(dead_pools.clear(entries, "k"))

    def test_a_retired_verdict_stops_suppressing_a_pool(self):
        """``same_pool_round_trip`` was retired 2026-10-08 (operator).

        It had put 9 EVM pools and 24 Solana mints out of monitoring, and no
        later round can refresh or contradict it, so it must not survive a load.
        """
        entries = {
            "retired": {"since_ms": NOW, "hits": 5,
                        "detail": "same_pool_round_trip"},
            "still_real": {"since_ms": NOW, "hits": 2, "detail": "pool_too_thin"},
        }
        removed = dead_pools.drop_retired(entries)
        self.assertEqual(sorted(removed), ["retired"])
        self.assertEqual(sorted(entries), ["still_real"])

    def test_the_evidence_is_stored_with_the_verdict(self):
        entries: dict = {}
        dead_pools.mark(entries, "k", now_ms=NOW, reason="below_depth",
                        detail="same_pool_round_trip", depth_usd="355158.78",
                        notional_usdt="200", round_trip_bps="9430.17")
        entry = entries["k"]
        self.assertEqual(entry["detail"], "same_pool_round_trip")
        self.assertEqual(entry["depth_usd"], "355158.78")
        self.assertEqual(entry["notional_usdt"], "200")
        self.assertEqual(entry["round_trip_bps"], "9430.17")
        # A later round that only knows the reason must not erase the numbers.
        dead_pools.mark(entries, "k", now_ms=NOW + 1, reason="below_depth")
        self.assertEqual(entries["k"]["round_trip_bps"], "9430.17")


class RoundUpdateTest(unittest.TestCase):
    """What one round does to the list: mark the rejected, revive the rechecked."""

    KEY = "uniswap-v3-x-layer:0xthin"

    def _dead_row(self, pool_key=KEY, *, detail="same_pool_round_trip"):
        return {
            "symbol": "TSLAx", "family": "kraken_to_chain",
            "quoted_pools": [pool_key],
            "dead_pools": [{"key": pool_key, "venue": "uniswap-v3-x-layer",
                            "pool_id": "0xTHIN", "chain": "x-layer",
                            "symbol": "TSLAx", "reason": "below_depth",
                            "detail": detail, "depth_usd": "355158.78",
                            "notional_usdt": "200",
                            "round_trip_bps": "9430.17"}],
        }

    def _clean_row(self, pool_key=KEY):
        return {"symbol": "TSLAx", "family": "kraken_to_chain",
                "quoted_pools": [pool_key], "dead_pools": []}

    def test_a_rejected_pool_is_recorded_under_its_own_key(self):
        entries: dict = {}
        got = scan.update_dead_pools(entries, [self._dead_row()], now_ms=NOW)
        self.assertEqual(got, {"marked": 1, "revived": 0})
        entry = entries[self.KEY]
        self.assertEqual(entry["detail"], "same_pool_round_trip")
        self.assertEqual(entry["depth_usd"], "355158.78")
        self.assertEqual(entry["round_trip_bps"], "9430.17")
        self.assertEqual(entry["notional_usdt"], "200")

    def test_a_rechecked_pool_that_measures_clean_is_dropped(self):
        entries: dict = {}
        dead_pools.mark(entries, self.KEY, now_ms=NOW, reason="below_depth")
        got = scan.update_dead_pools(entries, [self._clean_row()], now_ms=NOW + 1,
                                     recheck={self.KEY})
        self.assertEqual(got["revived"], 1)
        self.assertEqual(entries, {})

    def test_a_rechecked_pool_with_no_rows_is_left_alone(self):
        """Not seeing a pool is not the same as seeing it healthy."""
        entries: dict = {}
        dead_pools.mark(entries, self.KEY, now_ms=NOW, reason="below_depth")
        got = scan.update_dead_pools(entries, [], now_ms=NOW + 1,
                                     recheck={self.KEY})
        self.assertEqual(got["revived"], 0)
        self.assertIn(self.KEY, entries)

    def test_a_clean_row_for_a_pool_outside_the_rotation_does_not_revive_it(self):
        entries: dict = {}
        dead_pools.mark(entries, self.KEY, now_ms=NOW, reason="below_depth")
        scan.update_dead_pools(entries, [self._clean_row()], now_ms=NOW + 1)
        self.assertIn(self.KEY, entries)

    def test_a_pool_rejected_again_is_marked_and_not_revived(self):
        entries: dict = {}
        dead_pools.mark(entries, self.KEY, now_ms=NOW, reason="below_depth")
        got = scan.update_dead_pools(entries, [self._dead_row()], now_ms=NOW + 1,
                                     recheck={self.KEY})
        self.assertEqual(got["revived"], 0)
        self.assertEqual(entries[self.KEY]["hits"], 2)
        self.assertIsNotNone(dead_pools.is_dead(entries, self.KEY,
                                               now_ms=NOW + 1, confirm_hits=2))

    def test_one_round_counts_once_no_matter_how_many_rows_mention_it(self):
        """A pool appears in up to six rows a round; ``hits`` counts rounds.

        Measured 2026-10-08: counting rows made a pool look "confirmed" inside
        the same round that first rejected it.
        """
        entries: dict = {}
        rows = [self._dead_row(), self._dead_row(detail="pool_too_thin"),
                self._clean_row()]
        got = scan.update_dead_pools(entries, rows, now_ms=NOW)
        self.assertEqual(got["marked"], 1)
        self.assertEqual(entries[self.KEY]["hits"], 1)
        self.assertIsNone(dead_pools.is_dead(entries, self.KEY, now_ms=NOW,
                                            confirm_hits=2))


if __name__ == "__main__":
    unittest.main()
