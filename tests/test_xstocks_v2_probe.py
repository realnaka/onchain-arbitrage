"""Hermetic tests for the spread-triggered probe.

Operator, 2026-10-07: the board blamed "这个币在这条桥上没有到账样本" for rows
that had no spread at all, and asked for the system to sample on demand instead
of pre-sampling every coin on every bridge.

Two things have to hold for that to be safe, and both are pinned here:

1. ``bridge_unmeasured_symbol`` may only mean "this *coin* has no arrival
   sample", and it may not be the headline reason for a row whose spread is
   already dead.
2. A row is only probe-eligible when the spread clears the bar **and** the
   sample is the only thing missing; every minimum the crossing needs has to be
   known, because an unknown minimum is not a zero.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from arb_monitor.xstocks_v2 import executor as ex
from arb_monitor.xstocks_v2 import ledger, live, probe, scan
from arb_monitor.xstocks_v2.policy import V2Policy
from arb_monitor.xstocks_v2.spot_venue import BUY, REAL, SELL, Quote


def _board():
    """The read-only board, loaded the way it is run (it is a script)."""
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "v2board", root / "scripts" / "xstocks_v2_board.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _roundtrip():
    """The X Layer -> Solana script, loaded the way the runner loads it."""
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "v2solround", root / "scripts" / "xstocks_v2_solana_roundtrip.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DustPlanIsARefusalTest(unittest.TestCase):
    """A plan too small to express must refuse, not halt the chain.

    Measured 2026-10-10 09:07: the X Layer wallet was down to 0.000001 USDG, the
    probe sized itself at 9.99e-7, ``int(spend * 10**6)`` was 0, and the wallet's
    "--amount must be greater than zero" came back from inside the buy step.
    That step is broadcast-capable, so the runner wrote ``unknown`` and a HALT
    and kept the lock -- for a plan that could never have sent anything.
    """

    def setUp(self):
        self.module = _roundtrip()

    def test_the_measured_dust_spend_is_a_named_refusal(self):
        reason = self.module.dust_reason(D("0.000000999"))
        self.assertTrue(reason.startswith("below_min_notional:"), reason)
        self.assertIn("0.000001", reason)

    def test_a_real_ticket_is_not_refused(self):
        self.assertEqual(self.module.dust_reason(D("1.2")), "")
        self.assertEqual(self.module.dust_reason(D("0.000001")), "")

    def test_an_unreadable_spend_is_not_a_free_action(self):
        self.assertEqual(self.module.dust_reason(None), "spend_unreadable")
        self.assertEqual(self.module.dust_reason("abc"), "spend_unreadable")

    def test_the_plan_checks_dust_before_any_step_exists(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts" / "xstocks_v2_solana_roundtrip.py").read_text()
        self.assertIn('return {"error": dust, "symbol": args.symbol}', source)
        # And the buy step refuses it again, so a dust spend cannot reach the
        # wallet through any other caller either.
        buy = source.split("def buy():", 1)[1].split("def verify_buy", 1)[0]
        self.assertIn("dust_reason(ctx[\"spend\"])", buy)
        self.assertIn("ex.Outcome(ex.REFUSED, dust)", buy)

    def test_a_plan_refusal_is_written_down_not_only_logged(self):
        """The board reads the ledger, so a log-only refusal is a silent one.

        Measured 2026-10-10 09:32, on the first live cycle after the dust fix:
        the refusal was printed and the live snapshot showed an empty reason.
        """
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts" / "xstocks_v2_solana_roundtrip.py").read_text()
        branch = source.split('if ctx.get("error"):', 1)[1].split("fee_wei =", 1)[0]
        self.assertIn("ledger.record(", branch)
        self.assertIn('mode="live"', branch)
        self.assertIn('state=ledger.REFUSED', branch)
        # A read-only ``plan``/``simulate`` run must not leave a live row.
        self.assertIn('if args.mode == "once":', branch)


def _live_runner():
    """The resident runner script, loaded the way the runner itself loads legs."""
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "v2liverunner", root / "scripts" / "xstocks_v2_live.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RouteLedgerTest(unittest.TestCase):
    def test_a_sample_records_the_symbol_not_only_the_chain_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "samples.jsonl"
            scan.record_route_sample(scan.route_sample(
                symbol="SPYx", source_chain="x-layer", dest_chain="solana",
                arrived=True, amount_raw=947893, dest_balance_after="0.0095",
                source="test"), path)
            self.assertEqual(scan.load_measured_bridges(path),
                             frozenset({("x-layer", "solana")}))
            self.assertEqual(scan.load_measured_symbols(path),
                             frozenset({("x-layer", "solana", "SPYx")}))

    def test_an_unarrived_row_opens_nothing(self):
        """A source-chain simulation is not an arrival (lessons 4.1)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "samples.jsonl"
            scan.record_route_sample(scan.route_sample(
                symbol="SPYx", source_chain="x-layer", dest_chain="solana",
                arrived=False, source="test"), path)
            self.assertEqual(scan.load_measured_bridges(path), frozenset())
            self.assertEqual(scan.load_measured_symbols(path), frozenset())

    def test_chain_spellings_are_normalised(self):
        """``xlayer`` and ``x-layer`` are one chain, or the sample never matches."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "samples.jsonl"
            scan.record_route_sample(scan.route_sample(
                symbol="NVDAx", source_chain="XLayer", dest_chain="Solana",
                arrived=True), path)
            self.assertIn(("x-layer", "solana", "NVDAx"),
                          scan.load_measured_symbols(path))


class HeadlineReasonTest(unittest.TestCase):
    def test_economics_outrank_plumbing(self):
        self.assertEqual(
            scan.headline_reason(["bridge_unmeasured_symbol", "below_threshold"]),
            "below_threshold")
        self.assertEqual(
            scan.headline_reason(["unmeasured_route", "cost_unknown", "below_depth"]),
            "below_depth")

    def test_plumbing_is_still_the_reason_when_nothing_else_is_wrong(self):
        self.assertEqual(
            scan.headline_reason(["bridge_unmeasured_symbol"]),
            "bridge_unmeasured_symbol")
        self.assertEqual(scan.headline_reason([]), "")

    def test_an_unknown_name_does_not_get_dropped(self):
        self.assertEqual(scan.headline_reason(["something_new"]), "something_new")


class ProbeEligibilityTest(unittest.TestCase):
    def test_only_a_missing_sample_counts(self):
        self.assertTrue(scan.probe_eligible(["unmeasured_route"]))
        self.assertTrue(scan.probe_eligible(
            ["phase_scan", "bridge_unmeasured_symbol"]))

    def test_the_two_estimates_are_sample_eligible(self):
        """below_threshold / below_net are estimates a crossing replaces.

        operator 2026-10-09: 缺样本不是拒绝理由. The row may be walked once at
        the minimum -- but it must never read as a trade-shaped probe.
        """
        for extra in ("below_threshold", "below_net"):
            with self.subTest(extra=extra):
                self.assertTrue(scan.probe_eligible(
                    ["bridge_unmeasured_symbol", extra]))
                self.assertFalse(probe.eligible(
                    ["bridge_unmeasured_symbol", extra]))

    def test_any_other_blocker_disqualifies(self):
        for extra in ("below_depth", "stale_or_skew", "cost_unknown",
                      "no_address_book", "price_outlier", "hook_not_green"):
            with self.subTest(extra=extra):
                self.assertFalse(scan.probe_eligible(
                    ["bridge_unmeasured_symbol", extra]))

    def test_a_clean_row_is_not_a_probe(self):
        self.assertFalse(scan.probe_eligible([]))
        self.assertFalse(scan.probe_eligible(["phase_scan"]))


class ProbePlanTest(unittest.TestCase):
    def test_the_plan_names_the_credit_minimum_and_the_bridge_fee(self):
        plan = scan.probe_plan(
            symbol="NVDAx", source_chain="x-layer", hub_chain="solana",
            blockers=["bridge_unmeasured_symbol"],
            deposits={"NVDAx": [{"method": "NVDAx - Ethereum",
                                 "minimum": "0.00621054"},
                                {"method": "NVDAx", "minimum": "0.00641088"}]},
            withdraw_methods={"NVDAx": [{"method": "NVDAx", "network": "Solana"}]},
            bridge_fees={("x-layer", "solana"): D("0.101")},
            required_bps=D("4"))
        self.assertEqual(D(plan["deposit_min_units"]), D("0.00641088"))
        self.assertEqual(D(plan["bridge_fee_usd"]), D("0.101"))
        self.assertEqual(plan["waiting_on"], [])
        self.assertTrue(plan["runnable"])

    def test_an_unknown_input_is_a_refusal_not_a_zero(self):
        plan = scan.probe_plan(
            symbol="NVDAx", source_chain="x-layer", hub_chain="solana",
            blockers=["unmeasured_route"], deposits={},
            withdraw_methods={}, bridge_fees={}, required_bps=D("4"))
        self.assertIn("deposit_min_units", plan["waiting_on"])
        self.assertIn("bridge_fee_usd", plan["waiting_on"])
        self.assertFalse(plan["runnable"])
        self.assertIsNone(plan["deposit_min_units"])
        self.assertIsNone(plan["bridge_fee_usd"])


class ScanProbeRowTest(unittest.TestCase):
    """The scan itself has to produce the trigger, not only the board."""

    def setUp(self):
        self.policy = V2Policy.load()

    def _quote(self, side, price, *, chain="x-layer", venue="uniswap-v3-x-layer",
               pool_id="xl"):
        return Quote(venue=venue, chain=chain, symbol="NVDAx", side=side,
                     kind=REAL, price_usd=D(price), recv_ms=1000, pool_id=pool_id)

    def _entries(self):
        return [
            {"symbol": "NVDAx", "chain": "x-layer", "pool_id": "xl", "errors": [],
             "depth_usd": D("500000"),
             "buy": self._quote(BUY, "100"),
             "sell": self._quote(SELL, "100.5")},
            # The hub has to be in the catalogue for the hop to exist at all:
            # the row is "pool chain -> hub -> Kraken", and Kraken credits
            # NVDAx on Solana only.
            {"symbol": "NVDAx", "chain": "solana", "pool_id": "sol", "errors": [],
             "depth_usd": D("120000"),
             "buy": self._quote(BUY, "101.2", chain="solana", venue="jupiter",
                                pool_id="sol"),
             "sell": self._quote(SELL, "101.0", chain="solana", venue="jupiter",
                                 pool_id="sol")},
        ]

    def _rows(self, *, measured_bridges=frozenset(), measured_symbols=frozenset(),
              bid="101", session="closed", bridge_fees=None):
        return scan.build_candidates(
            symbol="NVDAx", chain_quotes=self._entries(),
            kraken_row={"bid": D(bid), "ask": D("101.5"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"),
            deposits={"NVDAx": [{"method": "NVDAx", "minimum": "0.00181743"}]},
            v4_tiers={}, now_ms=1000, phase="paper_kraken",
            withdraw_methods={"NVDAx": [{"method": "NVDAx", "network": "Solana"}]},
            measured_bridges=measured_bridges, measured_symbols=measured_symbols,
            bridge_fees=({("x-layer", "solana"): D("0.101")}
                         if bridge_fees is None else bridge_fees),
            session=session)

    def _hop(self, rows):
        return [row for row in rows
                if row["family"] == "chain_via_bridge_to_kraken"][0]

    def test_a_clearing_spread_with_no_symbol_sample_is_probe_eligible(self):
        row = self._hop(self._rows(
            measured_bridges=frozenset({("x-layer", "solana")})))
        self.assertIn("bridge_unmeasured_symbol", row["blockers"])
        self.assertTrue(row["probe_eligible"])
        self.assertTrue(row["probe"]["runnable"])
        self.assertFalse(row["executable"])

    def test_the_same_row_with_its_own_sample_is_no_longer_blocked(self):
        row = self._hop(self._rows(
            measured_bridges=frozenset({("x-layer", "solana")}),
            measured_symbols=frozenset({("x-layer", "solana", "NVDAx")})))
        self.assertNotIn("bridge_unmeasured_symbol", row["blockers"])
        self.assertNotIn("unmeasured_route", row["blockers"])
        self.assertFalse(row.get("probe_eligible", False))

    def test_a_dead_spread_is_a_sample_not_a_trade_probe(self):
        """The operator's NVDAx row: a losing spread printed as "no sample"."""
        row = self._hop(self._rows(
            measured_bridges=frozenset({("x-layer", "solana")}), bid="99"))
        self.assertIn("below_threshold", row["blockers"])
        self.assertEqual(row["reason"], "below_threshold")
        # 2026-10-09: the missing sample is still worth one minimal crossing,
        # but only on the sample stage -- never as a trade-shaped probe.
        self.assertTrue(row.get("probe_eligible", False))
        self.assertFalse(probe.ready(row))
        self.assertTrue(probe.sample_ready(row))

    def test_an_unpriced_bridge_keeps_the_probe_waiting(self):
        rows = self._rows(measured_bridges=frozenset({("x-layer", "solana")}),
                          bridge_fees={})
        plan = self._hop(rows)["probe"]
        self.assertEqual(plan["waiting_on"], ["bridge_fee_usd"])
        self.assertFalse(plan["runnable"])


class ProbeDecisionTest(unittest.TestCase):
    def setUp(self):
        self.policy = V2Policy.load()

    def _row(self, *, edge, blockers, plan=None, required="4"):
        return {"symbol": "NVDAx", "chain": "x-layer->solana",
                "family": "chain_via_bridge_to_kraken", "edge_t0_bps": str(edge),
                "required_edge_bps": required, "blockers": list(blockers),
                "probe_eligible": True, "probe": plan or {}}

    def _runnable(self):
        return {"symbol": "NVDAx", "source_chain": "x-layer", "hub_chain": "solana",
                "deposit_min_units": "0.00181743", "bridge_fee_usd": "0.101",
                "waiting_on": [], "runnable": True}

    def test_a_clearing_spread_with_a_runnable_plan_probes(self):
        decision = live.probe_decision(
            self._row(edge="25", blockers=["bridge_unmeasured_symbol"],
                      plan=self._runnable()), policy=self.policy)
        self.assertTrue(decision["probe"])
        self.assertEqual(decision["stage"], live.PROBE)

    def test_a_negative_spread_never_probes(self):
        decision = live.probe_decision(
            self._row(edge="-20.04", blockers=["bridge_unmeasured_symbol"],
                      plan=self._runnable()), policy=self.policy)
        self.assertFalse(decision["probe"])
        self.assertEqual(decision["reason"], "below_threshold")

    def test_a_second_problem_is_not_a_probe(self):
        decision = live.probe_decision(
            self._row(edge="25", blockers=["bridge_unmeasured_symbol", "below_depth"],
                      plan=self._runnable()), policy=self.policy)
        self.assertFalse(decision["probe"])
        self.assertEqual(decision["reason"], "other_blockers")
        self.assertEqual(decision["blocked_by"], ["below_depth"])

    def test_a_missing_fee_is_a_missing_input_not_a_different_problem(self):
        """The probe is *how* the fee gets measured, so it is reported as an input."""
        plan = self._runnable()
        plan["bridge_fee_usd"] = None
        plan["waiting_on"] = ["bridge_fee_usd"]
        plan["runnable"] = False
        decision = live.probe_decision(
            self._row(edge="25", blockers=["unmeasured_route", "cost_unknown"],
                      plan=plan), policy=self.policy)
        self.assertFalse(decision["probe"])
        self.assertEqual(decision["reason"], "probe_inputs_unknown")
        self.assertEqual(decision["waiting_on"], ["bridge_fee_usd"])

    def test_an_incomplete_plan_waits(self):
        plan = self._runnable()
        plan["waiting_on"] = ["bridge_fee_usd"]
        plan["runnable"] = False
        decision = live.probe_decision(
            self._row(edge="25", blockers=["unmeasured_route"], plan=plan),
            policy=self.policy)
        self.assertFalse(decision["probe"])
        self.assertEqual(decision["reason"], "probe_inputs_unknown")

    def test_candidates_are_ranked_and_capped(self):
        snapshot = {"threshold_bps": "4", "rows": [
            self._row(edge="30", blockers=["unmeasured_route"], plan=self._runnable()),
            self._row(edge="9", blockers=["unmeasured_route"], plan=self._runnable()),
            self._row(edge="50", blockers=["below_threshold"], plan=self._runnable()),
        ]}
        picked = live.probe_candidates(snapshot, policy=self.policy)
        self.assertEqual([item["spread_bps"] for item in picked], ["30", "9"])


class BoardProbePanelTest(unittest.TestCase):
    """The panel the operator reads. It has to show the trigger and the gaps."""

    def _render(self, rows):
        board = _board()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / board.SCAN.name).write_text(json.dumps({
                "generated_ms": board.time.time() * 1000, "phase": "paper_kraken",
                "session": "closed", "threshold_bps": "4", "rows": rows}))
            data = board.collect(data_dir=Path(tmp), interval_s=180.0)
            return data, board.render(data)

    def _probe_row(self, *, symbol="NVDAx", edge="25", runnable=True):
        return {
            "symbol": symbol, "family": "chain_via_bridge_to_kraken",
            "chain": "x-layer->solana", "buy_venue": "uniswap-v3-x-layer",
            "sell_venue": "kraken", "buy_price_usd": "100",
            "sell_price_usd": "100.25", "edge_t0_bps": edge,
            "required_edge_bps": "4", "notional_usdt": "200",
            "gross_usdt": "5", "net_usdt": "3", "executable": False,
            "blockers": ["bridge_unmeasured_symbol"],
            "reason": "bridge_unmeasured_symbol", "probe_eligible": True,
            "probe": {"symbol": symbol, "source_chain": "x-layer",
                      "hub_chain": "solana", "deposit_min_units": "0.00181743",
                      "bridge_fee_usd": "0.101" if runnable else None,
                      "required_edge_bps": "4",
                      "waiting_on": [] if runnable else ["bridge_fee_usd"],
                      "runnable": runnable},
        }

    def test_a_ready_probe_is_shown_as_runnable(self):
        data, page = self._render([self._probe_row()])
        self.assertEqual(len(data["probe_candidates"]), 1)
        self.assertIn("自动小样本", page)
        self.assertIn("NVDAx", page)
        self.assertIn("0.00181743", page)
        self.assertIn(">能<", page)

    def test_a_probe_missing_its_fee_says_what_it_waits_for(self):
        data, page = self._render([self._probe_row(runnable=False)])
        self.assertEqual(data["probe_candidates"], [])
        self.assertEqual(len(data["probe_waiting"]), 1)
        self.assertIn("bridge_fee_usd", page)

    def test_a_losing_row_is_not_a_probe_candidate(self):
        row = self._probe_row(edge="-20.04")
        row["blockers"] = ["below_threshold", "bridge_unmeasured_symbol"]
        row["reason"] = "below_threshold"
        row["probe_eligible"] = False
        data, page = self._render([row])
        self.assertEqual(data["probe_candidates"], [])
        self.assertIn("价差没过门槛", page)


class ProbedInventoryTest(unittest.TestCase):
    """A probe ends holding the token on purpose, and the next trade carries it."""

    def _row(self, state, symbol="NVDAx"):
        return ledger.row(symbol=symbol, chain="x-layer",
                          family="xlayer_via_bridge_to_solana", state=state,
                          mode="live", size_usdt=D("1.54"),
                          tokens=D("0.0065"), held_at="dest_wallet")

    def test_probed_is_not_a_hold_state(self):
        """Nothing went wrong, so it must not write a HALT or keep the lock."""
        self.assertNotIn(ledger.PROBED, ledger.HOLD_STATES)
        self.assertIn(ledger.PROBED, ledger.INVENTORY_STATES)
        self.assertEqual(self._row(ledger.PROBED)["held_at"], "dest_wallet")

    def test_the_next_cycle_sees_the_probed_slice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cycles.jsonl"
            ledger.record(self._row(ledger.PROBED), path)
            self.assertEqual([row["symbol"] for row in ledger.open_positions(path)],
                             ["NVDAx"])
            self.assertIn("NVDAx", ledger.carried_inventory(path))
            ledger.record(self._row(ledger.SOLD), path)
            self.assertEqual(ledger.open_positions(path), [])
            self.assertEqual(ledger.carried_inventory(path), {})


class ExecutorProbeStateTest(unittest.TestCase):
    """A probe must not be written down as ``sold``."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.halt = self.dir / "HALT"

    def _machine(self, **kwargs):
        # ``held_at`` on the bridge step is the in-flight worst case, exactly as
        # the real script declares it.
        steps = [ex.Step("bridge", lambda: ex.Outcome(ex.OK), held_at="in_bridge",
                         broadcasts=True, inventory=True)]
        return ex.Executor(symbol="NVDAx", chain="x-layer",
                           family="xlayer_via_bridge_to_solana", mode="live",
                           size_usdt=D("1.54"), tokens=D("0.0065"), steps=steps,
                           gate=lambda: (True, "ok"),
                           ledger_path=self.dir / "cycles.jsonl",
                           halt_path=self.halt, **kwargs)

    def test_a_probe_finishes_as_probed_with_the_coins_where_they_are(self):
        result = self._machine(final_state=ledger.PROBED,
                               final_reason="probe arrived",
                               final_held_at="dest_wallet").run()
        self.assertEqual(result["state"], ledger.PROBED)
        self.assertEqual(result["held_at"], "dest_wallet")
        self.assertFalse(result["keep_lock"])
        self.assertFalse(self.halt.exists())
        # Without the explicit final position the worst case would leak through.
        leaked = self._machine(final_state=ledger.PROBED).run()
        self.assertEqual(leaked["held_at"], "in_bridge")

    def test_a_verified_arrival_is_not_reported_as_still_in_the_bridge(self):
        """The bridge step's ``held_at`` is its in-flight worst case.

        Measured 2026-10-07: the first NVDAx probe wrote ``held_at: in_bridge``
        for tokens already sitting in our Solana wallet, because the finish used
        the last inventory step's worst case instead of where the coins ended.
        """
        result = self._machine(final_state=ledger.PROBED,
                               final_held_at="dest_wallet").run()
        self.assertEqual(result["held_at"], "dest_wallet")

    def test_a_trade_still_finishes_as_sold(self):
        result = self._machine().run()
        self.assertEqual(result["state"], ex.SOLD)
        self.assertEqual(result["held_at"], "")


class ViaBridgeSelectorTest(unittest.TestCase):
    """The fifth family's selector: the last gate before real money."""

    def setUp(self):
        self.runner = _live_runner()

    def _row(self, **overrides):
        row = {"family": "chain_via_bridge_to_kraken", "symbol": "NVDAx",
               "chain": "x-layer->solana", "edge_t0_bps": "35",
               "edge_after_requote_bps": "35", "notional_usdt": "200",
               "gross_usdt": "0.7", "net_usdt": "0.2", "bridge_fee_usd": "0.1",
               "blockers": []}
        row.update(overrides)
        return row

    def _snapshot(self, *rows):
        return {"rows": list(rows), "threshold_bps": "4"}

    def test_a_row_with_a_sample_and_a_clearing_net_is_picked(self):
        picked = self.runner.via_bridge_candidates(self._snapshot(
            self._row(blockers=["phase_scan"])))
        self.assertEqual([row["symbol"] for row in picked], ["NVDAx"])

    def test_a_missing_arrival_sample_keeps_it_out(self):
        """That row belongs to the probe queue, not to the trade queue."""
        picked = self.runner.via_bridge_candidates(self._snapshot(
            self._row(blockers=["bridge_unmeasured_symbol"])))
        self.assertEqual(picked, [])

    def test_other_families_are_not_this_selector(self):
        picked = self.runner.via_bridge_candidates(self._snapshot(
            self._row(family="chain_to_kraken")))
        self.assertEqual(picked, [])

    def test_a_row_that_cannot_pay_is_not_picked(self):
        """The selector recomputes the net; a fee that eats the spread blocks.

        The stored ``net_usdt`` is not what decides -- it was computed at the
        policy notional, and the wallet is often the real constraint. ``5`` USD
        of bridge fee on a 35 bps spread is a loss at any size we can pay.
        """
        picked = self.runner.via_bridge_candidates(self._snapshot(
            self._row(bridge_fee_usd="5")))
        self.assertEqual(picked, [])


class ExitArgvTest(unittest.TestCase):
    """``--probe-only`` and ``--exit kraken`` have to reach the script."""

    def setUp(self):
        self.module = _roundtrip()
        self.seen: list[str] = []

        def fake_main():
            self.seen = list(sys.argv)
            return 0

        self.module.main = fake_main

    def test_a_probe_asks_for_the_arrival_only_and_an_exact_size(self):
        code = self.module.cycle_once(symbol="NVDAx", tokens="0.0065",
                                      price="237.54", probe_only=True,
                                      confirm_live=True)
        self.assertEqual(code, 0)
        self.assertIn("--probe-only", self.seen)
        self.assertIn("--tokens", self.seen)
        self.assertIn("0.0065", self.seen)
        self.assertNotIn("--exit", self.seen)

    def test_a_kraken_trade_asks_for_the_kraken_exit(self):
        self.module.cycle_once(symbol="NVDAx", price="237.54", ref_price="237.03",
                               exit_kind=self.module.KRAKEN_EXIT, confirm_live=True)
        self.assertIn("--exit", self.seen)
        self.assertIn("kraken", self.seen)
        self.assertIn("--ref-price", self.seen)
        self.assertIn("237.03", self.seen)
        self.assertNotIn("--probe-only", self.seen)


if __name__ == "__main__":
    unittest.main()
