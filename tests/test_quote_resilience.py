"""Stage 2: a slow or absent quote channel costs one asset, not the whole grid.

Measured 2026-09-21, the number this file exists for: `quote_batch_ms` was over
the 15 s freshness budget on **55% of the most recent 60 cycles**, with p95 at
26.6 s. The mechanism was not a slow venue. Every probe retried an `api_error`
on its own with exponential backoff, and the dominant `api_error` was the CLI
answering "Network unavailable" — a channel that is down, rediscovered
separately by each of 16 probes at roughly 4 s each.

So the rules pinned here are:

* a channel that is known to be down stops being asked, and the probes that are
  not sent are recorded as **not sampled** — never as a price, never as the
  previous cycle's quote;
* one asset over its own budget loses its own round, not everybody's;
* the assets the machine may actually trade are quoted first;
* concurrency may only move inside [floor, configured `quote_workers`], and only
  one step per cooldown — the operator's instruction was not to raise it.
"""
from __future__ import annotations

import threading
import unittest

from tests.test_cycle_instrumentation import Harness, spot


class DeadChannel:
    """Every probe answers the way an unreachable proxy does.

    ``dead`` can be cleared to model the channel coming back; the call counter
    is then the record of how much of the grid was spent finding that out.
    """

    def __init__(self, message="Network unavailable — check your connection",
                 dead=True):
        self.message = message
        self.dead = dead
        self.calls = 0
        self.lock = threading.Lock()

    def _next(self, side):
        with self.lock:
            self.calls += 1
            dead = self.dead
        if dead:
            return spot(side=side, status="api_error", error=self.message)
        return spot(side=side)

    def quote(self, token, quote_token, side, amount):
        return self._next(side)

    def quote_exact_out(self, token, quote_token, shares):
        return self._next("buy")


class OneAssetIsStuck:
    """One asset blocks past its budget; the others answer immediately."""

    def __init__(self, stuck_asset="XIAOMI", hold_seconds=3.0):
        self.stuck_asset = stuck_asset
        self.hold_seconds = hold_seconds
        self.calls = []
        self.lock = threading.Lock()
        self.stuck_entered = threading.Event()
        self.release = threading.Event()

    def _answer(self, token, side):
        asset = token["asset"]
        with self.lock:
            self.calls.append((asset, side))
        if asset == self.stuck_asset:
            self.stuck_entered.set()
            self.release.wait(self.hold_seconds)
        return spot(side=side)

    def quote(self, token, quote_token, side, amount):
        return self._answer(token, side)

    def quote_exact_out(self, token, quote_token, shares):
        return self._answer(token, "buy-exact")


class DeadChannelTests(unittest.TestCase):
    ASSETS = ("XIAOMI", "TENCENT", "POPMART", "MEITUAN", "KUAISHOU", "SHEIN")

    def test_a_dead_channel_stops_the_retry_storm(self):
        quotes = DeadChannel()
        # 6 assets x 1 size x 2 sides, each with 1 + quote_retries attempts, is
        # what the grid used to cost before the channel was consulted at all.
        attempts_each = 1 + int(Harness().config["monitor"]["quote_retries"])
        naive_cost = len(self.ASSETS) * 3 * attempts_each
        with Harness(quotes=quotes, assets=self.ASSETS,
                     contract_assets=self.ASSETS) as h:
            summary = h.cycles(1)[0]
            workers = h.monitor.quote_workers_ceiling
        self.assertLess(quotes.calls, naive_cost,
                        "the grid paid every probe's full retry budget against a "
                        "channel that was already known to be down")
        self.assertLessEqual(quotes.calls, 2 * workers,
                             "the cost must be bounded by the grid width, not by "
                             "the number of assets in it")
        self.assertIn(summary["quote_channel"]["state"], {"open", "recovering"})
        self.assertGreater(summary["quote_channel"]["refused"], 0)
        # Nothing is invented for the probes that were not sent.
        unquoted = [row for row in summary["quote_assets"]
                    if row["final_status"] == "not_sampled"]
        self.assertTrue(unquoted, "a refused probe must be recorded as not sampled")
        for row in unquoted:
            self.assertIsNone(row["sell_ms"])
        self.assertEqual(summary["quote_probe_ok"], 0)

    def test_the_channel_recovers_and_the_grid_comes_back(self):
        quotes = DeadChannel()
        with Harness(quotes=quotes, assets=self.ASSETS,
                     contract_assets=self.ASSETS) as h:
            # One worker keeps the count deterministic: three transport
            # failures open the channel, and everything after that is refused
            # rather than paid for.
            h.monitor.quote_workers = 1
            h.cycles(1)
            self.assertIn(h.monitor.quote_channel.snapshot()["state"],
                          {"open", "recovering"})
            refused_before = h.monitor.quote_channel.refused
            self.assertGreater(refused_before, 0)
            # The channel comes back. Past the cooldown one probe is admitted;
            # its success clears the streak and the round completes normally.
            quotes.dead = False
            h.monitor.quote_channel._open_until = 0
            h.monitor.quote_workers = h.monitor.quote_workers_ceiling
            summary = h.cycles(1)[0]
        self.assertEqual(summary["quote_probe_ok"], 2 * len(self.ASSETS))
        self.assertEqual(summary["quote_channel"]["state"], "healthy")
        self.assertEqual(summary["quote_channel"]["consecutive_failures"], 0)


class PerAssetDeadlineTests(unittest.TestCase):
    ASSETS = ("XIAOMI", "TENCENT")

    def test_one_stuck_asset_does_not_void_the_others(self):
        quotes = OneAssetIsStuck(stuck_asset="XIAOMI", hold_seconds=3.0)
        try:
            with Harness(quotes=quotes, assets=self.ASSETS,
                         contract_assets=self.ASSETS) as h:
                # A budget the stuck asset cannot meet but the others can.
                h.monitor.quote_asset_deadline_ms = 300
                summary = h.cycles(1)[0]
        finally:
            quotes.release.set()
        by_asset = {row["asset"]: row for row in summary["quote_assets"]}
        self.assertIn("TENCENT", by_asset)
        self.assertEqual(by_asset["TENCENT"]["final_status"], "ok")
        self.assertIsNotNone(by_asset["TENCENT"]["sell_ms"])
        # The stuck asset is recorded as its own miss, with no substituted value.
        self.assertTrue(by_asset["XIAOMI"]["deadline_exceeded"])
        self.assertIsNone(by_asset["XIAOMI"]["sell_ms"])
        self.assertEqual(summary["quote_asset_deadlines_missed"], 1)

    def test_a_missed_asset_deadline_is_not_a_freshness_relaxation(self):
        # The 15 s pairing budget is untouched: this only bounds how long one
        # asset may hold a worker, and a quote without a sell side is no quote.
        with Harness(contract_assets=("XIAOMI",)) as h:
            summary = h.cycles(1)[0]
        self.assertEqual(summary["max_age_ms"], 15000)
        self.assertEqual(summary["quote_max_age_vs_budget_ms"][1], 15000)


class QuotePriorityTests(unittest.TestCase):
    def test_the_tradeable_assets_are_quoted_first(self):
        # XIAOMI is on the execution allowlist (okx:XIAOMI-USDT-SWAP); the other
        # monitored assets are not. A bounded grid must spend its first wave on
        # what can actually become an order.
        assets = ("XIAOMI", "TENCENT", "POPMART")
        with Harness(assets=assets, contract_assets=assets) as h:
            summary = h.cycles(1)[0]
        order = [row["asset"] for row in summary["quote_priority_order"]]
        priorities = {row["asset"]: row["priority"]
                      for row in summary["quote_priority_order"]}
        self.assertEqual(order[0], "XIAOMI")
        self.assertEqual(priorities["XIAOMI"], 0)
        self.assertEqual(priorities["TENCENT"], 1)

    def test_an_asset_with_no_perp_leg_is_the_lowest_tier(self):
        assets = ("XIAOMI", "MIXUx")
        # Only XIAOMI has a contract, so MIXUx has nothing to pair against.
        with Harness(assets=assets, contract_assets=("XIAOMI",)) as h:
            summary = h.cycles(1)[0]
            self.assertEqual(h.monitor._quote_priority({"asset": "MIXUx"}), 2)
        priorities = {row["asset"]: row["priority"]
                      for row in summary["quote_priority_order"]}
        self.assertEqual(priorities["XIAOMI"], 0)
        # "Display only" means it is not in the grid at all — not that its row is
        # filled in from somewhere else.
        self.assertEqual(summary["quote_quotable_assets"], ["XIAOMI"])


class AdaptiveConcurrencyTests(unittest.TestCase):
    def test_pressure_lowers_the_grid_and_calm_raises_it_back(self):
        with Harness() as h:
            ceiling = h.monitor.quote_workers_ceiling
            floor = h.monitor.quote_workers_floor
            self.assertEqual(h.monitor.quote_workers, ceiling)
            pressured = {"quote_batch_elapsed_ms": 26_000,
                         "quote_deadline_exceeded_count": 2,
                         "quote_assets": [{"asset": "XIAOMI", "buy_ms": 12_000,
                                           "sell_ms": 12_000, "retry_count": 3,
                                           "deadline_exceeded": True}],
                         "quote_probe_jobs": 1}
            decisions = []
            for cycle in range(1, 14):
                h.monitor.cycle_index = cycle
                h.monitor.quote_metrics = pressured
                h.monitor.quote_rate_limited = 0
                decisions.append(h.monitor._adapt_quote_workers())
            self.assertEqual(h.monitor.quote_workers, floor,
                             "a persistently over-budget grid must back off")
            # One bad round is not enough to move the grid — the window has to
            # agree — but a persistently bad one is.
            self.assertEqual(decisions[0]["decision"], "hold")
            self.assertEqual(decisions[1]["decision"], "lower")
            self.assertGreaterEqual(h.monitor.quote_workers, floor)
            calm = {"quote_batch_elapsed_ms": 2_000,
                    "quote_deadline_exceeded_count": 0,
                    "quote_assets": [{"asset": "XIAOMI", "buy_ms": 500,
                                      "sell_ms": 500, "retry_count": 0,
                                      "deadline_exceeded": False}],
                    "quote_probe_jobs": 1}
            for cycle in range(20, 40):
                h.monitor.cycle_index = cycle
                h.monitor.quote_metrics = calm
                h.monitor._adapt_quote_workers()
            self.assertEqual(h.monitor.quote_workers, ceiling,
                             "a comfortable window may return to the configured width")
            self.assertLessEqual(h.monitor.quote_workers, ceiling)

    def test_it_moves_at_most_one_step_per_cooldown(self):
        with Harness() as h:
            h.monitor.quote_workers = h.monitor.quote_workers_ceiling
            pressured = {
                "quote_batch_elapsed_ms": 30_000, "quote_deadline_exceeded_count": 4,
                "quote_assets": [{"asset": "XIAOMI", "buy_ms": 15_000,
                                  "sell_ms": 15_000, "retry_count": 4,
                                  "deadline_exceeded": True}],
                "quote_probe_jobs": 1}
            h.monitor.quote_metrics = pressured
            # A window that already agrees, so the first call is a real decision.
            h.monitor._quote_round_history = [
                {"p50_ms": 30_000, "p95_ms": 30_000, "elapsed_ms": 30_000,
                 "deadline_missed": 4, "asset_deadline_missed": 1,
                 "rate_limited": 0, "retries": 4, "requested": 1}] * 3
            h.monitor.cycle_index = 10
            h.monitor._adapt_quote_workers()
            after_one = h.monitor.quote_workers
            # The very next cycle is inside the cooldown: no further move.
            h.monitor.cycle_index = 11
            h.monitor._adapt_quote_workers()
            self.assertEqual(h.monitor.quote_workers, after_one)
            self.assertEqual(after_one, h.monitor.quote_workers_ceiling - 1)

    def test_the_decision_is_recorded_for_the_board(self):
        with Harness() as h:
            summary = h.cycles(1)[0]
        block = summary["quote_concurrency"]
        self.assertEqual(block["ceiling"], h.monitor.quote_workers_ceiling)
        self.assertIn(block["decision"], {"hold", "lower", "raise"})
        self.assertEqual(block["quote_workers"], h.monitor.quote_workers)


class ReferenceDefersToTheQuoteGridTests(unittest.TestCase):
    def test_a_round_is_deferred_while_the_grid_is_over_budget(self):
        # One proxy, two consumers. References are evidence; a stale quote is not
        # usable at all. So while the grid is over its per-asset budget the
        # reference round waits — but only for a bounded number of cycles.
        with Harness() as h:
            h.cycles(1)                       # primes the reference table
            h.monitor.last_reference = 0
            # The pressure signal is `last_quote_batch_ms`, so the decision is
            # driven directly: a cycle would recompute it from its own grid and
            # the deferral would look intermittent rather than bounded.
            h.monitor.last_quote_batch_ms = 30_000
            h.monitor._reference_deferred = 0
            limit = int(h.monitor.settings.get("reference_defer_max_cycles", 5))
            for expected in range(1, limit + 1):
                h.monitor._reference_round("run-deferred")
                self.assertIsNone(h.monitor._reference_future,
                                  "a slow grid must not be joined by a refresh")
                self.assertEqual(h.monitor._reference_deferred, expected)
            # The bound: after `reference_defer_max_cycles` the round runs even
            # though the grid is still slow, so evidence cannot starve.
            h.monitor._reference_round("run-deferred")
            self.assertIsNotNone(h.monitor._reference_future,
                                 "the deferred reference round never ran")
            self.assertLessEqual(h.monitor._reference_deferred, limit)
            h.monitor._reference_future.result(timeout=5)


if __name__ == "__main__":
    unittest.main()
