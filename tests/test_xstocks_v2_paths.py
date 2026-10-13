"""Hermetic tests for the any-to-any path catalogue and the cycle state machine."""
from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from arb_monitor.xstocks_v2 import executor as ex
from arb_monitor.xstocks_v2 import paths as P
from arb_monitor.xstocks_v2 import routes as R


class BridgeEdgeTest(unittest.TestCase):
    def test_the_catalogue_inherits_the_missing_ccip_edges(self):
        legs = P.bridge_legs(symbol="SPYx")
        pairs = {(leg.source[1], leg.dest[1]) for leg in legs}
        self.assertIn(("solana", "x-layer"), pairs)
        self.assertIn(("x-layer", "solana"), pairs)
        # measured missing 2026-10-07, and it must stay missing here
        self.assertNotIn(("solana", "mantle"), pairs)
        self.assertNotIn(("x-layer", "hyperevm"), pairs)
        self.assertNotIn(("hyperevm", "x-layer"), pairs)
        # TON is not on this bridge at all
        self.assertFalse([pair for pair in pairs if "ton" in pair])

    def test_every_bridge_leg_carries_its_dialect(self):
        for leg in P.bridge_legs(symbol="SPYx"):
            self.assertTrue(leg.note.startswith("ccip:"))
            self.assertEqual(leg.note, f"ccip:{R.dialect(leg.source[1], leg.dest[1])}")

    def test_duration_unknown_is_not_fast(self):
        legs = P.bridge_legs(symbol="SPYx", bridge_duration_s=None)
        graph = P.PathGraph(legs)
        found = graph.enumerate(P.node("solana", "solana"), P.node("x-layer", "x-layer"),
                                "SPYx")
        ranked = graph.rank(found, notional_usd=D(100), tokens=D(1))
        self.assertTrue(ranked)
        self.assertFalse(ranked[0].executable)
        self.assertIn("duration_unknown:bridge", ranked[0].blockers)

    def test_unmeasured_route_blocks_but_does_not_hide(self):
        graph = P.PathGraph(P.bridge_legs(symbol="SPYx"))
        found = graph.enumerate(P.node("solana", "solana"), P.node("x-layer", "x-layer"),
                                "SPYx")
        ranked = graph.rank(found, notional_usd=D(100), tokens=D(1))
        self.assertIn("unmeasured_route", ranked[0].blockers)
        self.assertFalse(ranked[0].executable)
        measured = P.bridge_legs(symbol="SPYx", measured=frozenset({("solana", "x-layer")}))
        graph = P.PathGraph(measured)
        ranked = graph.rank(
            graph.enumerate(P.node("solana", "solana"), P.node("x-layer", "x-layer"), "SPYx"),
            notional_usd=D(100), tokens=D(1))
        self.assertNotIn("unmeasured_route", ranked[0].blockers)
        self.assertTrue(ranked[0].executable)


class FormTest(unittest.TestCase):
    def test_only_evm_chains_have_a_wrapper(self):
        self.assertEqual(P.wrap_legs(symbol="SPYx", chain="solana"), [])
        self.assertEqual(P.wrap_legs(symbol="SPYx", chain="ton"), [])
        kinds = {leg.kind for leg in P.wrap_legs(symbol="SPYx", chain="x-layer")}
        self.assertEqual(kinds, {P.WRAP, P.REDEEM})

    def test_the_deep_x_layer_pool_is_the_wrapper_and_needs_a_wrap(self):
        """Measured 2026-10-07: the bridge hands over native, the pool quotes wrapper."""
        graph = P.full_catalog(
            symbol="SPYx", measured=frozenset({("solana", "x-layer")}),
            native_chains={"solana"},
            deposit_rail={"chain": "solana", "method": "SPYx", "minimum": "0.00056027"},
            withdraw_rail={"chain": "solana", "key": "solanspyx1",
                           "fee_usd": "0.364", "minimum": "0.00056027"})
        graph.add(P.Leg(kind=P.SELL, source=P.node("x-layer", "x-layer", P.WRAPPER),
                        dest=P.cash_node("x-layer", "x-layer"), symbol="SPYx",
                        fee_bps=D("5"), duration_s=D("3"), measured=True))
        ranked = graph.rank(
            graph.enumerate(P.cash_node("kraken"), P.cash_node("x-layer", "x-layer"), "SPYx"),
            notional_usd=D("7.80"), tokens=D("0.0095"))
        live = [row for row in ranked if not row.blockers]
        self.assertTrue(live)
        self.assertEqual(live[0].kinds[:2], (P.BUY, P.WITHDRAW))
        self.assertIn(P.WRAP, live[0].kinds)
        self.assertIn(P.BRIDGE, live[0].kinds)
        self.assertEqual(live[0].kinds[-1], P.SELL)


class RankingTest(unittest.TestCase):
    def _graph(self, *, slow_cheap: D, fast_expensive: D):
        """Two routes from kraken cash to solana cash: cheap/slow vs dear/fast."""
        graph = P.PathGraph()
        kraken_cash = P.cash_node("kraken")
        kraken = P.kraken_node()
        graph.add(P.Leg(P.BUY, kraken_cash, kraken, "SPYx", fee_bps=D("10"),
                        duration_s=D("5"), measured=True))
        graph.add(P.Leg(P.WITHDRAW, kraken, P.node("solana", "solana"), "SPYx",
                        fixed_usd=slow_cheap, duration_s=D("600"), measured=True))
        graph.add(P.Leg(P.WITHDRAW, kraken, P.node("x-layer", "x-layer"), "SPYx",
                        fixed_usd=fast_expensive, duration_s=D("30"), measured=True))
        graph.add(P.Leg(P.BRIDGE, P.node("x-layer", "x-layer"), P.node("solana", "solana"),
                        "SPYx", fixed_usd=D("0"), duration_s=D("120"), measured=True))
        for chain in ("solana", "x-layer"):
            graph.add(P.Leg(P.SELL, P.node(chain, chain), P.cash_node(chain, chain),
                            "SPYx", fee_bps=D("5"), duration_s=D("3"), measured=True))
        return graph

    def test_cheapest_wins_even_when_it_is_slower(self):
        graph = self._graph(slow_cheap=D("0.10"), fast_expensive=D("0.50"))
        ranked = graph.rank(
            graph.enumerate(P.cash_node("kraken"), P.cash_node("solana", "solana"), "SPYx"),
            notional_usd=D(200), tokens=D("0.25"))
        self.assertEqual(ranked[0].legs[1].dest[1], "solana")
        self.assertLess(ranked[0].cost_bps, ranked[1].cost_bps)

    def test_on_equal_cost_the_faster_one_wins(self):
        graph = self._graph(slow_cheap=D("0.30"), fast_expensive=D("0.30"))
        ranked = graph.rank(
            graph.enumerate(P.cash_node("kraken"), P.cash_node("solana", "solana"), "SPYx"),
            notional_usd=D(200), tokens=D("0.25"))
        self.assertEqual(ranked[0].legs[1].dest[1], "x-layer")
        self.assertLess(ranked[0].duration_s, ranked[1].duration_s)

    def test_fixed_fees_hurt_more_on_a_small_ticket(self):
        graph = self._graph(slow_cheap=D("0.10"), fast_expensive=D("0.50"))
        small = graph.rank(
            graph.enumerate(P.cash_node("kraken"), P.cash_node("solana", "solana"), "SPYx"),
            notional_usd=D(8), tokens=D("0.01"))[0]
        big = graph.rank(
            graph.enumerate(P.cash_node("kraken"), P.cash_node("solana", "solana"), "SPYx"),
            notional_usd=D(200), tokens=D("0.25"))[0]
        self.assertGreater(small.cost_bps, big.cost_bps * 5)

    def test_depth_cap_blocks_an_oversized_order(self):
        graph = P.PathGraph()
        trail = (
            P.Leg(P.BUY, P.cash_node("x-layer", "x-layer"),
                  P.node("x-layer", "x-layer"), "SPYx", measured=True,
                  duration_s=D("3"), max_amount=D("1000")),
            P.Leg(P.SELL, P.node("x-layer", "x-layer"),
                  P.cash_node("x-layer", "x-layer"), "SPYx", measured=True,
                  duration_s=D("3"), max_amount=D("1000")),
        )
        ranked = graph.rank(
            [trail],
            notional_usd=D("200000"), tokens=D("2000"))
        self.assertTrue(ranked)
        self.assertIn("above_depth:sell", ranked[0].blockers)

    def test_kraken_is_not_a_cross_chain_hub_by_default(self):
        graph = P.full_catalog(symbol="SPYx", measured=frozenset({("solana", "x-layer")}))
        graph.add(P.Leg(P.DEPOSIT, P.node("solana", "solana"), P.kraken_node(), "SPYx",
                        duration_s=D("60"), measured=True))
        graph.add(P.Leg(P.WITHDRAW, P.kraken_node(), P.node("x-layer", "x-layer"), "SPYx",
                        duration_s=D("120"), measured=True))
        off = graph.enumerate(P.cash_node("solana", "solana"),
                              P.cash_node("x-layer", "x-layer"), "SPYx")
        on = graph.enumerate(P.cash_node("solana", "solana"),
                             P.cash_node("x-layer", "x-layer"), "SPYx",
                             allow_cex_hub=True)
        self.assertFalse([trail for trail in off if P.DEPOSIT in [leg.kind for leg in trail]])
        self.assertTrue([trail for trail in on if P.DEPOSIT in [leg.kind for leg in trail]])


class ExecutorTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.ledger = self.dir / "cycles.jsonl"
        self.halt = self.dir / "HALT"

    def _executor(self, steps, gate=lambda: (True, "ok")):
        return ex.Executor(symbol="SPYx", chain="x-layer", family="xlayer_roundtrip",
                           mode="live", size_usdt=D("7.80"), tokens=D("0.0095"),
                           steps=steps, gate=gate, ledger_path=self.ledger,
                           halt_path=self.halt)

    def test_all_steps_ok_is_sold_and_releases_the_lock(self):
        calls = []
        steps = [ex.Step("buy", lambda: (calls.append("buy"), ex.Outcome(ex.OK))[1],
                         held_at="kraken", broadcasts=True)]
        result = self._executor(steps).run()
        self.assertEqual(result["state"], ex.SOLD)
        self.assertFalse(result["keep_lock"])
        self.assertFalse(self.halt.exists())
        self.assertTrue(self.ledger.exists())

    def test_refusal_and_revert_move_no_inventory(self):
        for status, expected in ((ex.REFUSED, ex.REFUSED), (ex.REVERTED, ex.REVERTED)):
            steps = [ex.Step("buy", lambda s=status: ex.Outcome(s, "no fill"),
                             held_at="kraken", broadcasts=True)]
            result = self._executor(steps).run()
            self.assertEqual(result["state"], expected)
            self.assertFalse(result["keep_lock"])
            self.assertFalse(self.halt.exists())

    def test_unknown_keeps_the_lock_and_writes_halt(self):
        steps = [ex.Step("bridge", lambda: ex.Outcome(ex.UNKNOWN, "no receipt"),
                         held_at="in_bridge", broadcasts=True)]
        result = self._executor(steps).run()
        self.assertEqual(result["state"], ex.UNKNOWN)
        self.assertEqual(result["held_at"], "in_bridge")
        self.assertTrue(result["keep_lock"])
        self.assertTrue(self.halt.exists())

    def test_a_step_with_inventory_stops_without_being_unknown(self):
        steps = [ex.Step("sell", lambda: ex.Outcome(ex.STOPPED, "wrap ok, swap reverted"),
                         held_at="dest_wallet", broadcasts=True)]
        result = self._executor(steps).run()
        self.assertEqual(result["state"], ex.STOPPED)
        self.assertEqual(result["held_at"], "dest_wallet")
        # Coins are in a known wallet. That symbol stops; the runner does not.
        self.assertFalse(result["keep_lock"])
        self.assertFalse(result["halt"])
        self.assertFalse(self.halt.exists())

    def test_verify_can_downgrade_a_success(self):
        """The 2026-10-07 lesson: ok:true with a txHash is not a broadcast."""
        steps = [ex.Step("sell", lambda: ex.Outcome(ex.OK, "", observed={"tx": "0x1"}),
                         held_at="dest_wallet", broadcasts=True,
                         verify=lambda outcome: ex.Outcome(ex.UNKNOWN, "wallet says ERROR"))]
        result = self._executor(steps).run()
        self.assertEqual(result["state"], ex.UNKNOWN)
        self.assertTrue(self.halt.exists())

    def test_a_retry_decided_by_verify_runs_the_step_again(self):
        """The wallet said ok:true; only verify can see it never broadcast."""
        state = {"runs": 0, "verifies": 0}

        def run():
            state["runs"] += 1
            return ex.Outcome(ex.OK, "", observed={"tx": f"0x{state['runs']}"})

        def verify(outcome):
            state["verifies"] += 1
            if state["verifies"] == 1:
                return ex.Outcome(ex.RETRY, "history says ERROR")
            return ex.Outcome(ex.OK, "", observed=outcome.observed)

        steps = [ex.Step("sell", run, held_at="dest_wallet", broadcasts=True,
                         retry_once=True, retry_reason="not broadcast", verify=verify)]
        machine = self._executor(steps)
        result = machine.run()
        self.assertEqual(result["state"], ex.SOLD)
        self.assertEqual(state["runs"], 2)
        self.assertEqual(machine.retried, ["sell:history says ERROR"])

    def test_a_retry_after_verify_is_capped_at_two_attempts(self):
        state = {"runs": 0}

        def run():
            state["runs"] += 1
            return ex.Outcome(ex.OK)

        steps = [ex.Step("sell", run, held_at="dest_wallet", broadcasts=True,
                         retry_once=True,
                         verify=lambda outcome: ex.Outcome(ex.RETRY, "still ERROR"))]
        result = self._executor(steps).run()
        self.assertEqual(state["runs"], 2)
        self.assertEqual(result["state"], ex.UNKNOWN)
        self.assertTrue(self.halt.exists())

    def test_retry_once_then_success(self):
        state = {"n": 0}

        def run():
            state["n"] += 1
            if state["n"] == 1:
                return ex.Outcome(ex.RETRY, "in-flight limit")
            return ex.Outcome(ex.OK)

        steps = [ex.Step("sell", run, held_at="dest_wallet", broadcasts=True,
                         retry_once=True, retry_reason="in-flight limit")]
        machine = self._executor(steps)
        result = machine.run()
        self.assertEqual(result["state"], ex.SOLD)
        self.assertEqual(state["n"], 2)
        self.assertEqual(machine.retried, ["sell:in-flight limit"])

    def test_a_second_retry_is_unknown(self):
        steps = [ex.Step("sell", lambda: ex.Outcome(ex.RETRY, "still in flight"),
                         held_at="dest_wallet", broadcasts=True, retry_once=True)]
        result = self._executor(steps).run()
        self.assertEqual(result["state"], ex.UNKNOWN)
        self.assertTrue(self.halt.exists())

    def test_a_broadcasting_step_is_refused_when_the_gate_is_shut(self):
        called = []
        steps = [ex.Step("sell", lambda: (called.append(1), ex.Outcome(ex.OK))[1],
                         held_at="dest_wallet", broadcasts=True)]
        result = self._executor(steps, gate=lambda: (False, "v1_live_busy")).run()
        self.assertEqual(result["state"], ex.REFUSED)
        self.assertEqual(result["reason"], "gate:v1_live_busy")
        self.assertEqual(called, [])

    def test_the_ledger_has_one_row_per_transition(self):
        steps = [ex.Step("a", lambda: ex.Outcome(ex.OK), broadcasts=False),
                 ex.Step("b", lambda: ex.Outcome(ex.OK), broadcasts=False)]
        self._executor(steps).run()
        rows = self.ledger.read_text().strip().splitlines()
        self.assertEqual(len(rows), 3)      # a, b, finish

    def test_classify_exception_after_a_broadcast_is_unknown(self):
        self.assertEqual(ex.classify_exception(RuntimeError("boom")).status, ex.REFUSED)
        self.assertEqual(
            ex.classify_exception(RuntimeError("boom"), broadcast=True).status, ex.UNKNOWN)

    def test_wallet_history_status_parsing(self):
        payload = {"ok": True, "data": [
            {"txHash": "0xaa", "txStatus": "SUCCESS"},
            {"txHash": "0xbb", "txStatus": "ERROR"},
        ]}
        self.assertEqual(ex.parse_wallet_status(payload, "0xbb"), "ERROR")
        self.assertEqual(ex.parse_wallet_status(payload, "0xcc"), "")
        self.assertEqual(ex.parse_wallet_status({"data": [{"txHash": "0xdd",
                                                           "txStatus": "PENDING"}]}), "PENDING")
        self.assertEqual(ex.parse_wallet_status({}, "0xaa"), "")


class VerifiedLedgerTest(unittest.TestCase):
    """The ledger has to keep both halves of a verified leg."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.ledger = self.dir / "cycles.jsonl"
        self.halt = self.dir / "HALT"

    def _executor(self, steps, **kwargs):
        return ex.Executor(symbol="SPYx", chain="x-layer", family="xlayer_roundtrip",
                           mode="live", size_usdt=D("8"), tokens=D("0.01"),
                           steps=steps, gate=lambda: (True, "ok"),
                           ledger_path=self.ledger, halt_path=self.halt, **kwargs)

    def test_verify_does_not_lose_the_runner_tx_hash(self):
        """Measured 2026-10-07: verify returned only balances, and the ledger's
        ``ok`` rows for the bridge and the sell kept no transaction hash. The
        closure then had to be re-derived from the chain by hand."""
        steps = [ex.Step(
            "sell",
            lambda: ex.Outcome(ex.OK, "", observed={"tx": "0xabc", "raw": 1033496}),
            held_at="dest_wallet", broadcasts=True,
            verify=lambda outcome: ex.Outcome(ex.OK, "",
                                              observed={"usdc_after": "8481916"}))]
        self._executor(steps).run()
        rows = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        ok_rows = [row for row in rows if row["state"] == ex.OK]
        self.assertEqual(ok_rows[0]["extra"]["observed"],
                         {"tx": "0xabc", "raw": 1033496, "usdc_after": "8481916"})

    def test_a_reconcile_within_fifteen_bps_is_silent(self):
        alerts = []
        steps = [ex.Step("sell", lambda: ex.Outcome(ex.OK), held_at="dest_wallet")]
        machine = self._executor(
            steps, reconcile=lambda: (D("8.1000"), D("8.0900"), D("8.1000")),
            notify=lambda **kw: alerts.append(kw))
        result = machine.run()
        self.assertEqual(result["state"], ex.SOLD)
        self.assertTrue(result["reconcile"]["ok"])
        self.assertEqual(alerts, [])
        self.assertFalse(machine.keep_lock)

    def test_a_reconcile_mismatch_alerts_but_does_not_hold(self):
        """The coins have already moved, so the gate warns; it must not lock."""
        alerts = []
        steps = [ex.Step("sell", lambda: ex.Outcome(ex.OK), held_at="dest_wallet")]
        machine = self._executor(
            steps, reconcile=lambda: (D("8.10"), D("7.00"), D("8.10")),
            notify=lambda **kw: alerts.append(kw))
        result = machine.run()
        self.assertEqual(result["state"], ex.SOLD)
        self.assertFalse(result["reconcile"]["ok"])
        self.assertFalse(result["keep_lock"])
        self.assertFalse(self.halt.exists())
        self.assertEqual([call["state"] for call in alerts], ["reconcile_mismatch"])

    def test_a_sign_flip_fails_the_reconcile(self):
        alerts = []
        steps = [ex.Step("sell", lambda: ex.Outcome(ex.OK), held_at="dest_wallet")]
        machine = self._executor(
            steps, reconcile=lambda: (D("0.10"), D("-0.10"), D("0.10")),
            notify=lambda **kw: alerts.append(kw))
        result = machine.run()
        self.assertFalse(result["reconcile"]["ok"])
        self.assertEqual(len(alerts), 1)

    def test_a_broken_prediction_is_reported_not_raised(self):
        alerts = []

        def boom():
            raise RuntimeError("no quote")

        steps = [ex.Step("sell", lambda: ex.Outcome(ex.OK), held_at="dest_wallet")]
        machine = self._executor(steps, reconcile=boom,
                                 notify=lambda **kw: alerts.append(kw))
        result = machine.run()
        self.assertEqual(result["state"], ex.SOLD)
        self.assertFalse(result["reconcile"]["ok"])
        self.assertIn("no quote", result["reconcile"]["error"])
        self.assertEqual([call["state"] for call in alerts], ["reconcile_error"])

    def test_nothing_is_reconciled_when_no_prediction_is_supplied(self):
        steps = [ex.Step("sell", lambda: ex.Outcome(ex.OK), held_at="dest_wallet")]
        result = self._executor(steps).run()
        self.assertIsNone(result["reconcile"])

    def test_no_prediction_available_is_written_down_as_unmeasured(self):
        """A missing quote is not a mismatch: record it, do not cry wolf."""
        alerts = []
        steps = [ex.Step("sell", lambda: ex.Outcome(ex.OK), held_at="dest_wallet")]
        machine = self._executor(steps, reconcile=lambda: None,
                                 notify=lambda **kw: alerts.append(kw))
        result = machine.run()
        self.assertEqual(result["state"], ex.SOLD)
        self.assertEqual(result["reconcile"], {"measured": False})
        self.assertEqual(alerts, [])


class ReconcileBoundaryTest(unittest.TestCase):
    def test_the_default_is_fifteen_bps_of_notional(self):
        from arb_monitor.xstocks_v2 import guards as G
        self.assertTrue(G.reconciliation_ok(predicted=D("8.10"), realized=D("8.088"),
                                            notional=D("8.10")))
        self.assertFalse(G.reconciliation_ok(predicted=D("8.10"), realized=D("8.087"),
                                             notional=D("8.10")))
        # The denominator is the notional, never the small net. 1 cent against a
        # 9-cent net is 1111 bps of the net but only 12.3 bps of the notional.
        self.assertTrue(G.reconciliation_ok(predicted=D("0.10"), realized=D("0.09"),
                                            notional=D("8.10")))


if __name__ == "__main__":
    unittest.main()
