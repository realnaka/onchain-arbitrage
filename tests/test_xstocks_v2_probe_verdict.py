"""One probe verdict for the board and the gate (A4).

Measured 2026-10-09: the board's "can run right now" list and the resident live
process answered the same question with two rules. The board looked only at the
plan's ``waiting_on`` and the spread; the gate also looked at the row's blockers.
So a row that carried ``below_net`` (the row's own numbers say the trade loses)
plus a runnable plan printed "能跑" on the board and got ``other_blockers`` from
the gate on the same snapshot.

The rule is one function now (``probe.verdict``), and this file pins the two
properties that matter:

* the board and ``live.probe_decision`` return the same answer for the same row;
* ``below_net`` is a veto in both -- a probe buys for real, and buying less of a
  losing trade does not make it pay.
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from arb_monitor.xstocks_v2 import live, probe, scan
from arb_monitor.xstocks_v2.policy import V2Policy


def _board():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "v2board_probe", root / "scripts" / "xstocks_v2_board.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(*, blockers, edge="25", required="4", plan=None) -> dict:
    return {"symbol": "METAx", "chain": "x-layer->solana",
            "family": "chain_via_bridge_to_kraken",
            "buy_price_usd": "378.7076410293779063054386184",
            "edge_t0_bps": edge, "required_edge_bps": required,
            "blockers": list(blockers), "probe_eligible": True,
            "probe": plan if plan is not None else runnable_plan()}


def runnable_plan() -> dict:
    return {"symbol": "METAx", "source_chain": "x-layer", "hub_chain": "solana",
            "deposit_min_units": "0.00181743", "bridge_fee_usd": "0.1038",
            "waiting_on": [], "runnable": True}


class BoardAndGateAgreeTest(unittest.TestCase):
    def setUp(self):
        self.policy = V2Policy.load()
        self.board = _board()

    def _both(self, item):
        return (self.board._probe_ready(item),
                bool(live.probe_decision(item, policy=self.policy)["probe"]))

    def test_a_clearable_row_is_ready_in_both(self):
        self.assertEqual(self._both(row(blockers=["bridge_unmeasured_symbol"])),
                         (True, True))

    def test_an_unpriced_fee_waits_in_both(self):
        plan = runnable_plan()
        plan.update({"bridge_fee_usd": None, "waiting_on": ["bridge_fee_usd"],
                     "runnable": False})
        ready = row(blockers=["unmeasured_route", "cost_unknown"], plan=plan)
        self.assertEqual(self._both(ready), (False, False))
        decision = live.probe_decision(ready, policy=self.policy)
        self.assertEqual(decision["reason"], "probe_inputs_unknown")

    def test_below_net_is_a_veto_in_both(self):
        """The 2026-10-09 mismatch: the board said 能跑, the gate refused."""
        losing = row(blockers=["bridge_unmeasured_symbol", "below_net"])
        self.assertEqual(self._both(losing), (False, False))
        decision = live.probe_decision(losing, policy=self.policy)
        self.assertEqual(decision["reason"], "other_blockers")
        self.assertEqual(decision["blocked_by"], ["below_net"])

    def test_a_dead_spread_is_not_ready_as_a_trade_probe(self):
        """A spread under the bar is not a trade-shaped probe (it is a sample)."""
        cheap = row(blockers=["bridge_unmeasured_symbol"], edge="3")
        self.assertEqual(self._both(cheap), (False, False))
        self.assertEqual(live.probe_decision(cheap, policy=self.policy)["reason"],
                         "below_threshold")
        self.assertTrue(live.probe_decision(
            cheap, policy=self.policy, require_spread=False)["probe"])

    def test_a_row_with_nothing_to_clear_is_not_a_probe(self):
        self.assertEqual(self._both(row(blockers=["phase_scan"])), (False, False))

    def test_an_unknown_threshold_fails_closed(self):
        item = row(blockers=["bridge_unmeasured_symbol"], required=None)
        decision = probe.verdict(item)          # no policy, no snapshot
        self.assertFalse(decision["probe"])
        self.assertEqual(decision["reason"], "no_threshold")

    def test_the_board_lists_only_what_the_gate_would_run(self):
        """The operator-visible list, not just the predicate."""
        good = row(blockers=["bridge_unmeasured_symbol"])
        losing = row(blockers=["bridge_unmeasured_symbol", "below_net"])
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / self.board.SCAN.name).write_text(json.dumps({
                "generated_ms": self.board.time.time() * 1000,
                "phase": "paper_kraken", "session": "closed",
                "threshold_bps": "4", "rows": [good, losing]}))
            data = self.board.collect(data_dir=Path(tmp), interval_s=180.0)
        symbols = [item["symbol"] for item in data["probe_candidates"]]
        self.assertEqual(len(data["probe_candidates"]), 1)
        # ``collect`` copies the rows it is given; the losing one must be gone
        # from the *trade* list entirely ...
        self.assertEqual(symbols, ["METAx"])
        self.assertEqual(len([item for item in data["probe_waiting"]
                              if item["symbol"] == "METAx"]), 0)
        # ... and it must show up in the sample list instead (operator
        # 2026-10-09: 缺样本不是拒绝理由). The two stages are separate lists on
        # purpose: one is the first leg of a trade, the other buys information.
        sample_symbols = [item["symbol"] for item in data["probe_samples"]]
        self.assertEqual(sample_symbols, ["METAx"])
        self.assertEqual(data["probe_samples"][0]["stage"], "sample")
        self.assertEqual(data["probe_candidates"][0]["stage"], "trade")


class OneBlockerRuleTest(unittest.TestCase):
    """``scan`` may not keep a second copy of the blocker sets."""

    def test_a_row_that_gains_below_net_stays_a_sample_not_a_trade(self):
        """Measured 2026-10-09: TSLAx/x-layer had the flag *and* below_net.

        Since the sample stage exists (operator 2026-10-09) the flag stays True
        -- the row is still worth one minimal crossing -- but it must never be
        offered as a trade-shaped probe.
        """
        item = row(blockers=["bridge_unmeasured_symbol"])
        item["probe_eligible"] = True
        item["net_usdt"] = None
        scan.apply_net_blocker(item, D("-1"), V2Policy.load())
        self.assertIn("below_net", item["blockers"])
        self.assertTrue(item["probe_eligible"])
        self.assertFalse(probe.ready(item))
        self.assertTrue(probe.sample_ready(item))
        self.assertEqual(probe.sample_verdict(item)["stage"], "sample")

    def test_the_round_two_reprice_also_refreshes_the_flag(self):
        """The other writer of ``blockers``: ``reprice_kraken_legs``."""
        from unittest import mock
        item = row(blockers=["bridge_unmeasured_symbol"], edge="11")
        item.update({"symbol": "PFEx", "chain": "solana",
                     "sell_venue": "kraken", "buy_venue": "PFEx-pool",
                     "buy_price_usd": "30", "sell_price_usd": "30",
                     "notional_usdt": "25", "probe_eligible": True,
                     "family": "chain_to_kraken"})
        depth = {"depth": {"PFEx": {"bids": [["30", "100"]]}}}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(scan.kraken, "fetch_depth",
                                  lambda http, symbols, **kw: depth), \
                mock.patch.object(scan.kraken, "native_usd",
                                  lambda http, wanted: {"SOL": D("110")}):
            scan.reprice_kraken_legs([item], http=object(),
                                     policy=V2Policy.load(), session="closed",
                                     counters={}, data_dir=Path(tmp))
        self.assertIn("below_net", item["blockers"])
        self.assertTrue(item["probe_eligible"])
        self.assertFalse(probe.ready(item))
        self.assertTrue(probe.sample_ready(item))

    CASES = (
        ["bridge_unmeasured_symbol"],
        ["unmeasured_route", "bridge_unmeasured_symbol"],
        ["unmeasured_route", "cost_unknown"],
        ["bridge_unmeasured_symbol", "below_net"],
        ["bridge_unmeasured_symbol", "below_threshold"],
        ["stage2_scan", "phase_scan", "chain_out_of_phase"],
        ["below_net"],
        [],
    )

    def test_scan_and_probe_agree_on_every_shape(self):
        for blockers in self.CASES:
            self.assertEqual(scan.probe_eligible(blockers),
                             (probe.eligible(blockers)
                              or probe.sample_eligible(blockers)), blockers)
            self.assertEqual(scan.probe_shaped(blockers),
                             (probe.plannable(blockers)
                              or probe.sample_plannable(blockers)), blockers)

    def test_scan_names_the_same_sets(self):
        self.assertEqual(scan.PROBE_CLEARABLE, probe.PROBE_CLEARABLE)
        self.assertEqual(scan.PROBE_PLANNABLE,
                         probe.PROBE_CLEARABLE | probe.TOLERATED)
        self.assertEqual(live.PROBE_CLEARABLE, probe.PROBE_CLEARABLE)
        self.assertEqual(live.PROBE_TOLERATED, probe.TOLERATED)


class SampleStageTest(unittest.TestCase):
    """operator 2026-10-09: 缺样本不是拒绝理由 —— 走一次最小额。

    The two-stage split exists because the old single rule deadlocked: 2,650
    rows a day carried a missing sample and 2,620 were refused by ``below_net``
    or ``below_threshold``, both of which are estimates a crossing replaces, so
    the route could never be sampled at all.
    """

    def setUp(self):
        self.policy = V2Policy.load()

    def test_a_sub_threshold_missing_sample_is_walkable(self):
        item = row(blockers=["bridge_unmeasured_symbol", "below_net",
                             "below_threshold"], edge="5", required="20")
        decision = probe.sample_verdict(item, policy=self.policy)
        self.assertTrue(decision["probe"])
        self.assertEqual(decision["stage"], "sample")
        self.assertEqual(decision["reason"], "sample_missing")
        self.assertEqual(decision["estimates"], ["below_net", "below_threshold"])
        # ... and it must never read as a trade-shaped probe.
        self.assertFalse(probe.ready(item, policy=self.policy))

    def test_a_hard_blocker_still_refuses(self):
        for blocker in ("no_address_book", "price_outlier", "hook_not_green",
                        "below_depth"):
            item = row(blockers=["bridge_unmeasured_symbol", blocker])
            decision = probe.sample_verdict(item, policy=self.policy)
            self.assertFalse(decision["probe"], blocker)
            self.assertEqual(decision["reason"], "other_blockers", blocker)
            self.assertEqual(decision["blocked_by"], [blocker], blocker)

    def test_an_unpriced_input_still_waits(self):
        plan = runnable_plan()
        plan.update({"bridge_fee_usd": None, "waiting_on": ["bridge_fee_usd"],
                     "runnable": False})
        item = row(blockers=["bridge_unmeasured_symbol", "cost_unknown"], plan=plan)
        decision = probe.sample_verdict(item, policy=self.policy)
        self.assertFalse(decision["probe"])
        self.assertEqual(decision["reason"], "probe_inputs_unknown")

    def test_without_a_plan_there_is_nothing_to_walk(self):
        item = row(blockers=["bridge_unmeasured_symbol"], plan={})
        self.assertEqual(probe.sample_verdict(item, policy=self.policy)["reason"],
                         "no_probe_plan")

    def test_nothing_missing_is_not_a_sample_probe(self):
        item = row(blockers=["phase_scan"])
        self.assertEqual(probe.sample_verdict(item, policy=self.policy)["reason"],
                         "no_sample_missing")


class ProbeBudgetTest(unittest.TestCase):
    """3 probes a day, 2 USDT each (operator 2026-10-09)."""

    NOW = 1_791_509_936_204      # 2026-10-09 (UTC)

    def _decide(self, state, size, day=3, cap="2"):
        return probe.budget_decide(state=state, now_ms=self.NOW, size_usdt=size,
                                   max_per_day=day, max_usdt_per_probe=cap)

    def test_a_small_probe_is_allowed_and_spends_nothing_yet(self):
        ok, why, state = self._decide(None, "1.8")
        self.assertTrue(ok)
        self.assertEqual(why, "ok")
        self.assertEqual(state["count"], 0)

    def test_one_probe_above_the_cap_is_refused(self):
        self.assertEqual(self._decide(None, "2.01")[1], "above_cap")

    def test_the_daily_cap_stops_the_fourth(self):
        state = {"day": probe.budget_day(self.NOW), "count": 3, "spent_usdt": "5.4"}
        self.assertEqual(self._decide(state, "1.8")[1], "daily_cap")

    def test_an_unset_cap_refuses_rather_than_meaning_unlimited(self):
        self.assertEqual(self._decide(None, "1", day=0)[1], "cap_not_set")
        self.assertEqual(self._decide(None, "1", cap="0")[1], "cap_not_set")
        self.assertEqual(self._decide(None, "1", cap=None)[1], "cap_not_set")

    def test_an_unknown_size_is_not_a_small_probe(self):
        self.assertEqual(self._decide(None, None)[1], "size_unknown")

    def test_the_counter_rolls_over_on_a_new_utc_day(self):
        state = {"day": "2026-10-08", "count": 3, "spent_usdt": "5.4"}
        ok, _why, fresh = self._decide(state, "1.8")
        self.assertTrue(ok)
        self.assertEqual(fresh["count"], 0)
        self.assertEqual(fresh["day"], "2026-10-09")

    def test_charging_counts_only_what_ran(self):
        state = probe.budget_charge(None, now_ms=self.NOW, size_usdt="1.8")
        state = probe.budget_charge(state, now_ms=self.NOW, size_usdt="1.8")
        self.assertEqual(state["count"], 2)
        self.assertEqual(state["spent_usdt"], "3.6")
        self.assertEqual(self._decide(state, "1.8")[1], "ok")
        state = probe.budget_charge(state, now_ms=self.NOW, size_usdt="1.8")
        self.assertEqual(self._decide(state, "1.8")[1], "daily_cap")

    def test_a_reservation_is_on_disk_before_the_probe_walks(self):
        """A81: a process killed mid-probe must not spend uncounted money.

        Measured 2026-10-09: an eth TSLAx sample really bought 1.12 USDC while
        the counter stayed at 4 because the charge happened after the cycle
        returned. Reserving first means the file is never behind the money.
        """
        state = probe.budget_reserve(None, now_ms=self.NOW, size_usdt="1.12")
        self.assertEqual(state["count"], 1)
        self.assertEqual(state["spent_usdt"], "1.12")
        # A reservation that never became a spend is given back exactly.
        state = probe.budget_release(state, size_usdt="1.12")
        self.assertEqual(state["count"], 0)
        self.assertEqual(state["spent_usdt"], "0")

    def test_a_release_cannot_make_the_day_negative(self):
        state = probe.budget_release({"day": probe.budget_day(self.NOW),
                                      "count": 0, "spent_usdt": "0"},
                                     size_usdt="2")
        self.assertEqual(state["count"], 0)
        self.assertEqual(state["spent_usdt"], "0")

    def test_a_reservation_taken_and_not_released_keeps_the_cap_honest(self):
        """The kill case: reserve, die, restart. The day is charged."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / probe.BUDGET_NAME
            probe.budget_save(path, probe.budget_reserve(
                probe.budget_load(path), now_ms=self.NOW, size_usdt="1.8"))
            # A restart reads the same file; nothing here knows the probe died.
            self.assertEqual(probe.budget_load(path)["count"], 1)

    def test_the_file_round_trips_and_a_broken_file_is_an_empty_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / probe.BUDGET_NAME
            self.assertEqual(probe.budget_load(path), {})
            probe.budget_save(path, {"day": "2026-10-09", "count": 2,
                                     "spent_usdt": "3.6"})
            self.assertEqual(probe.budget_load(path)["count"], 2)
            path.write_text("{not json")
            self.assertEqual(probe.budget_load(path), {})


class LiveProbeStageTest(unittest.TestCase):
    """``live.probe_candidates``: trade stage first, sample stage as fallback."""

    NOW = 1_791_509_936_204

    def setUp(self):
        self.policy = V2Policy.load()

    def test_the_trade_stage_wins_when_it_exists(self):
        snapshot = {"threshold_bps": "4", "rows": [
            row(blockers=["bridge_unmeasured_symbol"], edge="25"),
            row(blockers=["bridge_unmeasured_symbol", "below_net"], edge="5")]}
        picked = live.probe_candidates(snapshot, policy=self.policy, now_ms=self.NOW)
        self.assertEqual([item["stage"] for item in picked], ["trade"])
        self.assertTrue(picked[0]["budget_ok"])

    def test_the_sample_stage_runs_when_no_trade_stage_row_exists(self):
        snapshot = {"threshold_bps": "4", "rows": [
            row(blockers=["bridge_unmeasured_symbol", "below_net",
                          "below_threshold"], edge="5")]}
        picked = live.probe_candidates(snapshot, policy=self.policy, now_ms=self.NOW)
        self.assertEqual([item["stage"] for item in picked], ["sample"])
        self.assertTrue(picked[0]["budget_ok"])
        # 0.00181743 credit units at 378.7076 USDT = 0.6883 USDT, inside the cap.
        self.assertTrue(picked[0]["size_usdt"].startswith("0.688"))

    def test_a_spent_budget_keeps_the_candidate_and_marks_it_blocked(self):
        snapshot = {"threshold_bps": "4", "rows": [
            row(blockers=["bridge_unmeasured_symbol", "below_net"], edge="5")]}
        # The cap is a policy value the operator sets; a test must follow it
        # rather than pin today's number (it was 3, then 10).
        cap = self.policy.probe_max_per_day
        spent = {"day": probe.budget_day(self.NOW), "count": cap,
                 "spent_usdt": "5.4"}
        self.assertGreater(cap, 0)
        picked = live.probe_candidates(snapshot, policy=self.policy,
                                       budget=spent, now_ms=self.NOW)
        self.assertEqual(len(picked), 1)          # still visible ...
        self.assertFalse(picked[0]["budget_ok"])  # ... but the gate says no
        self.assertEqual(picked[0]["budget_reason"], "daily_cap")

    def test_a_probe_over_the_per_probe_cap_is_blocked_not_silently_shrunk(self):
        big = row(blockers=["bridge_unmeasured_symbol", "below_net"], edge="5")
        big["buy_price_usd"] = "5000"       # 0.0018 × 5000 = 9 USDT
        snapshot = {"threshold_bps": "4", "rows": [big]}
        picked = live.probe_candidates(snapshot, policy=self.policy, now_ms=self.NOW)
        self.assertEqual(picked[0]["budget_reason"], "above_cap")


class CostsAgeTest(unittest.TestCase):
    """A2: a stale gas-token price is not a gas line."""

    def setUp(self):
        self.policy = V2Policy.load()

    def test_a_fresh_price_prices_one_action(self):
        from arb_monitor.xstocks_v2 import costs
        value, note = costs.gas_cost_usd(
            self.policy, chain="x-layer", gas_price_wei=D("20000001"),
            native_usd=D("129.4153"), native_as_of_ms=1_000_000,
            now_ms=1_000_000, max_native_age_ms=3 * 3600 * 1000)
        self.assertIsNotNone(value)
        self.assertEqual(note, "estimated")

    def test_a_stale_price_is_native_price_stale_not_a_confident_zero(self):
        from arb_monitor.xstocks_v2 import costs
        value, note = costs.gas_cost_usd(
            self.policy, chain="x-layer", gas_price_wei=D("20000001"),
            native_usd=D("129.4153"), native_as_of_ms=1_000_000,
            now_ms=1_000_000 + 20 * 3600 * 1000,
            max_native_age_ms=3 * 3600 * 1000)
        self.assertIsNone(value)
        self.assertEqual(note, "native_price_stale")

    def test_without_an_age_the_old_behaviour_is_kept(self):
        from arb_monitor.xstocks_v2 import costs
        value, note = costs.gas_cost_usd(
            self.policy, chain="x-layer", gas_price_wei=D("20000001"),
            native_usd=D("129.4153"))
        self.assertIsNotNone(value)
        self.assertEqual(note, "estimated")


if __name__ == "__main__":
    unittest.main()
