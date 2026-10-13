"""Hermetic tests for the v2 live gate, amount floor and cycle plan.

Nothing here touches a wallet, a venue or a real halt file.
"""
from __future__ import annotations

import importlib.util
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

from arb_monitor.xstocks_v2 import live
from arb_monitor.xstocks_v2.policy import V2Policy
from arb_monitor.xstocks_v2.spot_venue import BUY, REAL, Quote

#: The route ledger is runtime state: it is written by a real bridge arrival and
#: it lives under the gitignored ``data-v2/``. A fresh clone and a CI runner do
#: not have it, so the one test below that reads it says so instead of failing.
LIVE_ROUTE_SAMPLES = Path(__file__).resolve().parents[1] / "data-v2" \
    / "xstocks_v2_route_samples.jsonl"


def _live_runner():
    """The resident runner script, loaded the way the runner itself loads legs."""
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "v2liverunner", root / "scripts" / "xstocks_v2_live.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LiveGateTest(unittest.TestCase):
    def setUp(self):
        self.policy = V2Policy.load()

    def _gate(self, policy=None, **kwargs):
        kwargs.setdefault("halt", False)
        kwargs.setdefault("lock", False)
        kwargs.setdefault("kill", False)
        kwargs.setdefault("auto_halt", False)
        kwargs.setdefault("v1_busy", False)
        return live.live_gate(policy or self.policy, **kwargs)

    def test_every_one_of_the_three_keys_is_required(self):
        base = replace(self.policy, phase="paper_kraken", mode="live",
                       auto_orders="live", live_ack="operator-2026-10-07")
        self.assertEqual(self._gate(base), (True, "ok"))
        for broken in (replace(base, phase="scan"),
                       replace(base, mode="off"),
                       replace(base, auto_orders="paper"),
                       replace(base, live_ack="   ")):
            self.assertFalse(self._gate(broken)[0])

    def test_stop_files_and_a_busy_v1_each_block(self):
        base = replace(self.policy, phase="paper_kraken", mode="live",
                       auto_orders="live", live_ack="ack")
        for kwargs, expected in (
            ({"kill": True}, "kill"),
            ({"auto_halt": True}, "auto_halt"),
            ({"halt": True}, "halt"),
            ({"lock": True}, "lock"),
            ({"v1_busy": True}, "v1_live_busy"),
        ):
            allowed, reason = self._gate(base, **kwargs)
            self.assertFalse(allowed)
            self.assertEqual(reason, expected)

    def test_only_the_implemented_family_and_chains_are_executable(self):
        base = replace(self.policy, phase="paper_kraken", mode="live",
                       auto_orders="live", live_ack="ack")
        self.assertFalse(self._gate(base, family="dex_to_dex")[0])
        self.assertFalse(self._gate(base, chain="bsc")[0])
        self.assertTrue(self._gate(base, chain="eth")[0])


class AmountFloorTest(unittest.TestCase):
    def test_the_floor_is_the_worst_leg_not_the_first(self):
        """Every leg has its own minimum; the transfer has to clear all of them."""
        floor, reason = live.amount_floor(bridge_min=D("0.0001"),
                                          deposit_min=D("0.0164"),
                                          sell_min=D("0.05459063574"))
        self.assertEqual(floor, D("0.05459063574"))
        self.assertIn("sell_min", reason)

    def test_a_leg_that_does_not_apply_is_not_a_constraint(self):
        floor, reason = live.amount_floor(bridge_min=None, deposit_min=D("0.0164"),
                                          sell_min=D("0.0001"))
        self.assertEqual(floor, D("0.0164"))
        self.assertEqual(reason, "deposit_min")

    def test_no_known_minimum_is_refused_not_assumed_small(self):
        floor, reason = live.amount_floor(bridge_min=None, deposit_min=None,
                                          sell_min=None)
        self.assertIsNone(floor)
        self.assertEqual(reason, "no_minimum_known")

    def test_unknown_minimums_are_named_per_route(self):
        missing = live.unknown_minimums(bridge_min=None, deposit_min=D("0.01"),
                                        sell_min=D("0.01"), requires_bridge=True)
        self.assertEqual(missing, ["bridge_min"])
        missing = live.unknown_minimums(bridge_min=D("0.01"), deposit_min=None,
                                        sell_min=D("0.01"))
        self.assertEqual(missing, ["deposit_min"])
        self.assertEqual(live.unknown_minimums(
            bridge_min=None, deposit_min=D("1"), sell_min=D("1")), [])

    def test_the_marrow_case_matches_the_measured_numbers(self):
        """MRNAx: bridge min 0.0001, Kraken deposit min 0.0164."""
        floor, _reason = live.amount_floor(bridge_min=D("0.0001"),
                                           deposit_min=D("0.0164"),
                                           sell_min=D("0.0001"))
        self.assertEqual(floor, D("0.0164"))
        # 0.0001 alone must never be forwarded to Kraken.
        self.assertLess(D("0.0001"), floor)


class ProbeOrTradeTest(unittest.TestCase):
    def test_a_route_with_no_arrival_sample_is_probed(self):
        self.assertEqual(live.route_stage(measured=False), live.PROBE)
        self.assertEqual(live.route_stage(measured=True), live.TRADE)

    def test_a_probe_is_only_held_to_the_bridge_and_pool_minimums(self):
        amount, _reason = live.probe_amount(bridge_min=D("0.0001"),
                                            wallet_balance=D("1"))
        self.assertEqual(amount, D("0.0001"))

    def test_a_probe_is_not_the_deposit_minimum_because_it_never_goes_to_kraken(self):
        amount, _ = live.probe_amount(bridge_min=D("0.0001"), pool_min=D("0.002"))
        self.assertEqual(amount, D("0.002"))

    def test_a_probe_we_cannot_afford_is_refused(self):
        amount, reason = live.probe_amount(bridge_min=D("0.5"),
                                           wallet_balance=D("0.1"))
        self.assertIsNone(amount)
        self.assertEqual(reason, "below_floor")

    def test_a_probe_without_a_bridge_minimum_is_refused(self):
        amount, reason = live.probe_amount(bridge_min=None, pool_min=D("0.1"))
        self.assertEqual(amount, D("0.1"))
        self.assertEqual(reason, "pool_min")


class SizeTest(unittest.TestCase):
    def test_size_is_the_minimum_of_notional_balance_and_depth(self):
        size, why = live.size_for(notional=D("25"), balance_quote=D("40"),
                                  depth=D("100"), floor=D("0.05"), price=D("100"))
        self.assertEqual(why, "full")
        # 25 USDT / 100 = 0.25 tokens
        self.assertAlmostEqual(float(size), 0.25, places=9)

    def test_the_wallet_can_be_the_binding_constraint(self):
        size, _why = live.size_for(notional=D("25"), balance_quote=D("10"),
                                   depth=D("100"), floor=D("0.05"), price=D("100"))
        self.assertAlmostEqual(float(size), 0.0999, places=9)

    def test_a_size_below_the_floor_is_refused(self):
        size, why = live.size_for(notional=D("25"), balance_quote=D("0.2"),
                                  depth=D("100"), floor=D("0.05"), price=D("100"))
        self.assertIsNone(size)
        self.assertEqual(why, "below_floor")

    def test_a_probe_is_capped_at_the_floor(self):
        size, why = live.size_for(notional=D("25"), balance_quote=D("40"),
                                  depth=D("100"), floor=D("0.05459063574"),
                                  price=D("100"), probe=True)
        self.assertEqual(size, D("0.05459063574"))
        self.assertEqual(why, "probe")

    def test_an_unknown_balance_or_depth_is_not_a_number(self):
        self.assertEqual(live.size_for(notional=D("25"), balance_quote=None,
                                       depth=D("100"), floor=D("0.05"),
                                       price=D("100"))[1], "balance_unknown")
        self.assertEqual(live.size_for(notional=D("25"), balance_quote=D("10"),
                                       depth=None, floor=D("0.05"),
                                       price=D("100"))[1], "depth_unknown")
        self.assertEqual(live.size_for(notional=D("25"), balance_quote=D("10"),
                                       depth=D("1"), floor=D("0.05"),
                                       price=None)[1], "no_price")


class CyclePlanTest(unittest.TestCase):
    def test_a_probe_plan_is_not_a_one_message_trade(self):
        plan = live.cycle_plan(symbol="MRNAx", chain="solana",
                               family="chain_to_kraken", tokens=D("0.001"),
                               buy_price=D("190"), expected_sell_price=D("191"),
                               floor=D("0.0001"), floor_reason="bridge_min",
                               stage=live.PROBE, net_usdt=D("-0.02"),
                               deposit_method="MRNAx - Solana")
        self.assertFalse(plan["one_message"])
        self.assertFalse(plan["broadcast"])

    def test_a_trade_plan_is_one_message_and_still_not_broadcast_by_this_module(self):
        plan = live.cycle_plan(symbol="MRNAx", chain="solana",
                               family="chain_to_kraken", tokens=D("0.06"),
                               buy_price=D("190"), expected_sell_price=D("191"),
                               floor=D("0.0164"), floor_reason="deposit_min",
                               stage=live.TRADE, net_usdt=D("0.15"),
                               deposit_method="MRNAx - Solana")
        self.assertTrue(plan["one_message"])
        self.assertFalse(plan["broadcast"])


class ResidentFamilySelectorTest(unittest.TestCase):
    """Which rows the resident runner is allowed to pick up.

    The selector is the last gate before real money, and it was previously
    untested (found 2026-10-07: the bridge families looked wired in but
    ``bridge_candidates`` returned 0 on every real snapshot).
    """

    def setUp(self):
        self.runner = _live_runner()

    def _snapshot(self, *rows, threshold_bps="4"):
        return {"rows": list(rows), "threshold_bps": threshold_bps}

    def _row(self, **overrides):
        """A row that would be picked if -- and only if -- the net clears."""
        row = {"family": "chain_to_chain", "symbol": "SPYx",
               "source_chain": "x-layer", "dest_chain": "solana",
               "edge_t0_bps": "35", "edge_after_requote_bps": "35",
               "notional_usdt": "200", "gross_usdt": "0.7", "net_usdt": "0.5",
               "bridge_fee_usd": "0.1",
               "blockers": []}
        row.update(overrides)
        return row

    def test_a_reverse_row_with_only_informational_marks_is_a_candidate(self):
        rows = self.runner.solana_candidates(self._snapshot(
            self._row(blockers=["phase_scan", "chain_out_of_phase"])))
        self.assertEqual([row["symbol"] for row in rows], ["SPYx"])

    def test_a_real_blocker_keeps_the_reverse_family_inert(self):
        """``cost_unknown`` is a refusal, not a rounding error. Until an edge's
        cost is measured the row must never reach the executor."""
        for blocker in ("cost_unknown", "unmeasured_route", "below_depth",
                        "stale_or_skew", "hook_not_green"):
            rows = self.runner.solana_candidates(self._snapshot(
                self._row(blockers=[blocker])))
            self.assertEqual(rows, [], blocker)

    def test_the_wrong_direction_is_not_a_reverse_candidate(self):
        """Solana -> X Layer is the forward family, not this one."""
        rows = self.runner.solana_candidates(self._snapshot(
            self._row(source_chain="solana", dest_chain="x-layer")))
        self.assertEqual(rows, [])

    def test_the_kraken_hub_family_keeps_its_own_selector(self):
        row = self._row(family="kraken_via_bridge_to_chain", symbol="MRNAx",
                        chain="solana->x-layer",
                        edge_t0_bps="35", edge_after_requote_bps="35")
        self.assertEqual(self.runner.bridge_candidates(self._snapshot(row)),
                         [row])
        self.assertEqual(self.runner.solana_candidates(self._snapshot(row)), [])

    def test_the_highest_edge_goes_first_and_the_limit_is_kept(self):
        rows = [self._row(symbol=symbol, edge_t0_bps=edge,
                          edge_after_requote_bps=edge)
                for symbol, edge in (("AAPLx", "35"), ("SPYx", "60"), ("TSLAx", "48"))]
        picked = self.runner.solana_candidates(self._snapshot(*rows), limit=2)
        self.assertEqual([row["symbol"] for row in picked], ["SPYx", "TSLAx"])

    def test_a_row_with_no_net_is_not_a_candidate(self):
        """A row the scan could not price is a refusal, however clean it looks."""
        rows = self.runner.solana_candidates(
            self._snapshot(self._row(net_usdt=None, bridge_fee_usd=None)))
        self.assertEqual(rows, [])

    def test_a_row_below_the_profit_floor_is_not_a_candidate(self):
        """The bridge fee is real money now, so the net has to clear the bar."""
        # 11 bps over 200 USDT is 0.22 gross, minus a 0.10 fee and 0.40 of
        # slippage+taker: below the 0.10 floor.
        rows = self.runner.solana_candidates(self._snapshot(
            self._row(edge_t0_bps="11", edge_after_requote_bps="11")))
        self.assertEqual(rows, [])

    def test_a_row_below_the_session_edge_threshold_is_not_a_candidate(self):
        rows = self.runner.solana_candidates(self._snapshot(
            self._row(edge_t0_bps="35", edge_after_requote_bps="35"), threshold_bps="40"))
        self.assertEqual(rows, [])

    def test_the_snapshot_threshold_is_what_the_gate_reads(self):
        row = self._row(edge_t0_bps="35", edge_after_requote_bps="35", net_usdt="0.5")
        self.assertEqual(len(self.runner.solana_candidates(
            self._snapshot(row, threshold_bps="4"))), 1)
        self.assertEqual(self.runner.solana_candidates(
            self._snapshot(row, threshold_bps="40")), [])

    def test_the_net_is_judged_at_the_size_the_wallet_can_afford(self):
        """A fixed ~0.10 USD bridge fee on an 8 USD ticket is 125 bps.

        The same row clears the bar at 200 USDT and loses money at the size this
        wallet can actually trade, so the gate has to take the balance.

        The proportional term is the slippage buffer alone (10 bps): this row is
        ``chain_to_chain``, which never trades on Kraken, so its taker fee is not
        a cost of this path. Charging it here (until 2026-10-07) put the gate's
        net 10 bps below the scan's net for the same row and moved this floor
        from ~38 USDT to ~48 USDT.
        """
        row = self._snapshot(self._row(edge_t0_bps="62.19",
                                       edge_after_requote_bps="62.19",
                                       bridge_fee_usd="0.1003056807744",
                                       notional_usdt="200"))["rows"][0]
        snapshot = self._snapshot(row)
        # 62.19 bps, minus the 0.1003 fixed fee and 10 bps of slippage:
        # ~38.4 USDT is where the 0.10 profit floor is met.
        for available in ("200", "50", "40"):
            self.assertTrue(self.runner.bridge_net_ok(
                row, snapshot=snapshot, available_usdt=D(available)), available)
        for available in ("8.481916", "30", "20"):
            self.assertFalse(self.runner.bridge_net_ok(
                row, snapshot=snapshot, available_usdt=D(available)), available)

    def test_a_row_without_a_measured_fee_cannot_fire(self):
        row = self._snapshot(self._row(bridge_fee_usd=None))["rows"][0]
        self.assertFalse(self.runner.bridge_net_ok(row, snapshot=self._snapshot(row)))

    def test_a_balance_we_cannot_read_is_a_refusal(self):
        row = self._snapshot(self._row(bridge_fee_usd="0.1"))["rows"][0]
        snapshot = self._snapshot(row)
        for available in ("0", None):
            if available is None:
                continue          # None means "not supplied": policy notional
            self.assertFalse(self.runner.bridge_net_ok(
                row, snapshot=snapshot, available_usdt=D(available)))


class AvailableBalanceTest(unittest.TestCase):
    """The buy-side money a family can spend has to come from a real read."""

    def setUp(self):
        self.runner = _live_runner()

    class _Http:
        def __init__(self, answers, *, only_mint=None):
            self.answers = answers
            self.only_mint = only_mint
            self.mints = []

        def post_json(self, url, payload, **kwargs):
            second = (payload.get("params") or [None, {}])[1]
            mint = second.get("mint") if isinstance(second, dict) else None
            self.mints.append(mint)
            if self.only_mint and mint != self.only_mint:
                return 200, {"result": {"value": []}}
            return 200, self.answers.get(url, {})

    def test_solana_usdc_is_read_in_six_decimals(self):
        # The real answer is wrapped: {"result": {"value": [...]}}.
        http = self._Http(
            {"https://api.mainnet-beta.solana.com": {"result": {"value": [
                {"account": {"data": {"parsed": {"info": {
                    "tokenAmount": {"amount": "8481916"}}}}}}]}}},
            only_mint=live.SOLANA_QUOTES["USDC"])
        got = self.runner.available_quote_usdt(
            self.runner.SOLANA_BRIDGE_FAMILY, http=http)
        self.assertEqual(got, D("8.481916"))

    def test_usdt_is_money_for_the_same_family(self):
        """Both Solana stables are spendable; reading one hid 44.5405 USDT."""
        http = self._Http(
            {"https://api.mainnet-beta.solana.com": {"result": {"value": [
                {"account": {"data": {"parsed": {"info": {
                    "tokenAmount": {"amount": "44540500"}}}}}}]}}},
            only_mint=live.SOLANA_QUOTES["USDT"])
        got = self.runner.available_quote_usdt(
            self.runner.SOLANA_BRIDGE_FAMILY, http=http)
        self.assertEqual(got, D("44.5405"))
        self.assertEqual(http.mints,
                         [live.SOLANA_QUOTES["USDC"],
                          live.SOLANA_QUOTES["USDT"]])

    def test_an_unwrapped_answer_is_not_mistaken_for_an_empty_wallet(self):
        """Reading ``value`` off the envelope returns nothing and looks funded-0."""
        http = self._Http({"https://api.mainnet-beta.solana.com": {"value": [
            {"account": {"data": {"parsed": {"info": {
                "tokenAmount": {"amount": "8481916"}}}}}}]}})
        got = self.runner.available_quote_usdt(
            self.runner.SOLANA_BRIDGE_FAMILY, http=http)
        self.assertEqual(got, D("0"))

    def test_x_layer_usdg_is_read_in_six_decimals(self):
        http = self._Http({"https://rpc.xlayer.tech": {
            "result": hex(55_315_343)}})
        got = self.runner.available_quote_usdt(
            self.runner.SOLANA_FAMILY, http=http)
        self.assertEqual(got, D("55.315343"))

    def test_an_unreadable_wallet_is_zero_not_the_policy_notional(self):
        class Boom:
            def post_json(self, *a, **k):
                raise OSError("no proxy")

        for family in (self.runner.SOLANA_BRIDGE_FAMILY, self.runner.SOLANA_FAMILY):
            self.assertEqual(
                self.runner.available_quote_usdt(family, http=Boom()), D("0"))

    def test_a_zero_balance_refuses_every_bridge_family(self):
        row = {"family": "chain_to_chain", "edge_t0_bps": "200",
               "edge_after_requote_bps": "200", "notional_usdt": "200",
               "bridge_fee_usd": "0.1"}
        snapshot = {"threshold_bps": "4"}
        self.assertTrue(self.runner.bridge_net_ok(row, snapshot=snapshot,
                                                  available_usdt=D("200")))
        self.assertFalse(self.runner.bridge_net_ok(row, snapshot=snapshot,
                                                   available_usdt=D("0")))

    @unittest.skipUnless(LIVE_ROUTE_SAMPLES.exists(),
                         "needs this machine's data-v2 route ledger")
    def test_the_measured_reverse_bridge_is_registered(self):
        """The x-layer -> solana arrival of 2026-10-07 opened this edge. A row
        without an arrival sample must stay out of the candidate set, so the
        measurement and the selector have to agree."""
        from arb_monitor.xstocks_v2 import scan
        self.assertIn(("x-layer", "solana"), scan.load_measured_bridges())


class SolanaQuoteChoiceTest(unittest.TestCase):
    """Round 2 sizes the trade: base ticket first, then step up (2026-10-08)."""

    def _plan(self, *, clears_above=None, usdc=D("500"), usdt=D("0"),
              conversion_bps=None):
        """Run ``plan_one`` with everything network-shaped stubbed out.

        ``clears_above`` is the size where the trade stops paying: the ladder has
        to keep the largest size that still clears, and stop there.

        The plan now charges the same cost lines the executor pays, so this
        supplies the two inputs it cannot invent: the gas token's USD price and
        the mint's credit multiplier (1 here -- PFEx's own 1.0537 is exercised
        by ``CreditUnitTest``). The spread is 400 bps so the ladder is decided
        by size, not by the fixed transfer.
        """
        import time as _time
        module = _live_runner()
        if conversion_bps is not None:
            # The funding decision quotes the swap; a test must not touch the
            # network to price it.
            self.addCleanup(setattr, module, "conversion_cost_bps",
                            module.conversion_cost_bps)
            module.conversion_cost_bps = lambda **kwargs: D(conversion_bps)
        now = int(_time.time() * 1000)
        policy = V2Policy.load()
        row = {"symbol": "PFEx", "chain": "solana", "family": "chain_to_kraken",
               "notional_usdt": "25", "buy_price_usd": "100", "edge_t0_bps": "500",
               "sell_price_usd": "104", "blockers": [], "pool_id": "sol"}

        def fake_requote(**kwargs):
            size = kwargs.get("notional") or D("25")
            # Above the threshold the route delivers 5% fewer tokens: the same
            # 104 bid then no longer clears the bar, which is what "深度不够"
            # looks like to the second quote.
            delivered = size / D("100")
            if clears_above is not None and size >= clears_above:
                delivered = delivered * D("0.95")
            quote = Quote(venue="jupiter", chain="solana", symbol="PFEx", side=BUY,
                          kind=REAL, price_usd=D("100"), recv_ms=now)
            return {"buy": quote, "tokens": delivered, "errors": []}

        # These are attributes of **shared modules**, not locals: without a
        # restore every later test inherits this stub (found 2026-10-08, when the
        # profit-first tests picked up this fake depth and saw an empty book).
        self.addCleanup(setattr, module.scan, "requote_buy",
                        module.scan.requote_buy)
        self.addCleanup(setattr, module.kraken_feed, "fetch_depth",
                        module.kraken_feed.fetch_depth)
        self.addCleanup(setattr, module, "_seen", module._seen)
        module.scan.requote_buy = fake_requote
        # A mint nobody has deposited yet pays the first-transfer rent; the test
        # must not depend on the operator's real transfer ledger either way.
        module._seen = lambda: {}
        module.kraken_feed.fetch_depth = lambda *a, **k: {
            "depth": {"PFEx": {"bids": [["104", "5", "1"]],
                               "asks": [["105", "5", "1"]]}}}

        class Client:
            def deposit_methods(self, symbol):
                return [{"method": "PFEx", "minimum": "0.001"}]

        return module.plan_one(row, policy=policy, http=None, client=Client(),
                               units={"PFEx": {"effective_min_units": "0.0000001"}},
                               sol_usd=D("115.12"),
                               wallets={"USDC": usdc, "USDT": usdt})

    def test_the_ladder_keeps_the_largest_size_that_still_clears(self):
        plan = self._plan()
        self.assertEqual(plan["decision"], "paper")
        self.assertEqual(plan["chosen_usdt"], "200")
        self.assertEqual(len(plan["ladder"]), 4)

    def test_the_ladder_stops_at_the_first_size_that_loses(self):
        plan = self._plan(clears_above=D("100"))
        self.assertEqual(plan["chosen_usdt"], "50")
        self.assertEqual(plan["decision"], "paper")
        self.assertEqual(len(plan["ladder"]), 3)     # 25, 50, then 100 stops it

    def test_a_base_that_does_not_clear_does_not_step_up(self):
        plan = self._plan(clears_above=D("25"))
        # ``false_t0``: the scan's t0 spread looked fine, the fresh quote does
        # not. Either way the base failed, so the ladder must stop at one step.
        self.assertEqual(plan["decision"], "false_t0")
        self.assertEqual(len(plan["ladder"]), 1)

    def test_only_usdt_in_the_wallet_funds_the_ticket_by_converting(self):
        """Operator 2026-10-08: 机会发现不看钱包，执行时缺什么就换什么.

        The wallet holds 8.4755 USDC and 44.54 USDT (measured). The plan must
        keep the opportunity and add conversions instead of refusing on "we
        hold the wrong dollar": 25 swaps the whole ticket, 50 only needs the
        gap (8.4755 + 44.54 = 53.02), and 100 is beyond the wallet's total.
        """
        plan = self._plan(usdc=D("8.475511"), usdt=D("44.5405"),
                          conversion_bps="2")
        self.assertEqual(plan["decision"], "paper")
        # The wallet caps the size: 50 is the largest rung it can fund, and
        # that is a funding fact, not a price verdict.
        self.assertEqual(plan["chosen_usdt"], "50")
        self.assertEqual(plan["funding"], "top_up_usdc_from_usdt")
        # The gap plus the swap's own slack (10 bps of the ticket): a top-up
        # that lands on exactly 50.00 would leave 49.99 USDC and be refused by
        # the venue's own balance check a second later.
        gap = D("50") * D("1.001") - D("8.475511")
        self.assertEqual(D(plan["convert_amount_usdt"]), gap)
        self.assertEqual(plan["conversion_bps"], "2")
        self.assertEqual(D(plan["conversion_usdt"]), gap * D("2") / D("10000"))
        self.assertEqual(plan["ladder"][0], "25:paper:")
        self.assertIn("funding_short", plan["ladder"][-1])

    def test_no_stablecoin_covers_the_ticket_is_a_funding_answer(self):
        plan = self._plan(usdc=D("1"), usdt=D("2"))
        self.assertEqual(plan["decision"], "refused")
        self.assertEqual(plan["decision_reason"], "funding_short")
        self.assertEqual(plan["funding"], "insufficient")

    def test_the_ladder_starts_at_the_base_and_doubles_to_the_ceiling(self):
        self.assertEqual(live.ladder_sizes(base=D("25"), ceiling=D("200")),
                         [D("25"), D("50"), D("100"), D("200")])

    def test_a_ceiling_below_the_base_still_yields_one_step(self):
        self.assertEqual(live.ladder_sizes(base=D("200"), ceiling=D("200")),
                         [D("200")])

    def test_a_zero_base_has_no_ladder(self):
        self.assertEqual(live.ladder_sizes(base=D("0"), ceiling=D("200")), [])
    """The Solana buy leg may spend USDC or USDT (operator 2026-10-07).

    Measured that day: the wallet held 8.481916 USDC and 44.5405 USDT, and the
    executor read only USDC -- so a funded route was reported as unfunded.
    """

    def test_auto_takes_the_larger_balance(self):
        self.assertEqual(live.quote_asset_choice(usdc="8.481916", usdt="44.5405"),
                         "USDT")
        self.assertEqual(live.quote_asset_choice(usdc="10", usdt="1"), "USDC")

    def test_a_requested_asset_needs_a_balance(self):
        self.assertEqual(live.quote_asset_choice(usdc="1", usdt="0",
                                                 requested="USDT"), "")
        self.assertEqual(live.quote_asset_choice(usdc="1", usdt="0",
                                                 requested="USDC"), "USDC")

    def test_two_empty_wallets_are_not_a_choice(self):
        for usdc, usdt in (("0", "0"), (None, None), ("", "")):
            self.assertEqual(live.quote_asset_choice(usdc=usdc, usdt=usdt), "")

    def test_an_unknown_request_is_refused_not_guessed(self):
        self.assertEqual(live.quote_asset_choice(usdc="5", usdt="5",
                                                 requested="DAI"), "")

    def test_both_mints_are_6_decimal_stables(self):
        self.assertEqual(set(live.SOLANA_QUOTES), {"USDC", "USDT"})
        self.assertEqual(live.SOLANA_QUOTES["USDT"],
                         "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB")


class TransferRentTest(unittest.TestCase):
    """The rent is paid once per mint, and both runners must agree on when."""

    def test_only_the_first_transfer_pays_the_rent(self):
        import json as _json
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / live.TRANSFER_SEEN_NAME
            self.assertTrue(live.first_transfer_for("JPMx", path))
            path.write_text(_json.dumps({"JPMx": 1}))
            self.assertFalse(live.first_transfer_for("JPMx", path))
            # An unreadable ledger is "never deposited", i.e. the safe answer
            # (it over-charges the fee rather than approving a trade with none).
            self.assertTrue(live.first_transfer_for("JPMx", Path(tmp) / "missing"))


class RequoteRetryTest(unittest.TestCase):
    """A 429 is "ask again", not "this pool is dead" (2026-10-08).

    Measured 13:4x: the executor's re-quote of a row that had just cleared
    round 2 came back 429 ("[API Gateway] Too many requests") and the cycle
    aborted at ``requote_failed``. The very next call to the same URL answered
    200 with ``outAmount 7550159`` -- nothing was wrong with the pool. The
    cause is structural: the scan, the paper runner and the live runner each
    pace their own limiter but share one 1 rps free key.
    """

    class Jup:
        def __init__(self, replies):
            self.replies = list(replies)
            self.calls = 0

        def quote_status(self, input_mint, output_mint, raw_amount, slippage_bps):
            self.calls += 1
            index = min(self.calls - 1, len(self.replies) - 1)
            return self.replies[index]

    class OldJup:
        """The v1-shaped client: a dict or ``{}`` and no status."""

        def __init__(self, replies):
            self.replies = list(replies)
            self.calls = 0

        def quote(self, input_mint, output_mint, raw_amount, slippage_bps):
            self.calls += 1
            index = min(self.calls - 1, len(self.replies) - 1)
            return self.replies[index]

    def _requote(self, jup, **kwargs):
        module = _live_runner()
        return module.requote_buy_leg(jup, input_mint="usdc", mint="jpmx",
                                      raw_amount=25_000_000, slippage_bps=10,
                                      sleep=lambda seconds: None, **kwargs)

    def test_a_rate_limited_quote_is_asked_again_and_then_clears(self):
        jup = self.Jup([(429, None), (200, {"outAmount": "7550159"})])
        quote, why = self._requote(jup)
        self.assertEqual(quote, {"outAmount": "7550159"})
        self.assertEqual(why, "")
        self.assertEqual(jup.calls, 2)

    def test_a_request_that_never_arrived_is_asked_again(self):
        jup = self.Jup([(0, None), (200, {"outAmount": "1"})])
        quote, why = self._requote(jup)
        self.assertEqual(quote, {"outAmount": "1"})
        self.assertEqual(why, "")

    def test_a_dead_pool_is_not_retried(self):
        """400 is the pool answering. Retrying it burns the key we are short of."""
        jup = self.Jup([(400, {"error": "NO_ROUTES_FOUND"})])
        quote, why = self._requote(jup)
        self.assertIsNone(quote)
        self.assertEqual(why, "http_400")
        self.assertEqual(jup.calls, 1)

    def test_giving_up_still_says_why(self):
        jup = self.Jup([(429, None)])
        quote, why = self._requote(jup, tries=2)
        self.assertIsNone(quote)
        self.assertEqual(why, "http_429")
        self.assertEqual(jup.calls, 2)

    def test_an_old_client_shape_still_works(self):
        """``legs()`` may hand over a v1-shaped client; it must not raise."""
        jup = self.OldJup([{"outAmount": "7"}])
        quote, why = self._requote(jup)
        self.assertEqual(quote, {"outAmount": "7"})
        self.assertEqual(why, "")


class OneSlotPerPairTest(unittest.TestCase):
    """A bridge slot is one script with one source chain, not a whole family.

    The pickers filtered on the family alone, so a row from another chain pair
    would be handed to a script that signs somewhere else. Nothing fired that
    way yet (those rows carry blockers), which is exactly why this is a check
    and not a hope -- operator 2026-10-10: 「runner 没有槽是什么意思」.
    """

    def setUp(self):
        self.runner = _live_runner()

    def _row(self, family, chain, symbol="SPYx"):
        return {"symbol": symbol, "family": family, "chain": chain,
                "blockers": [], "edge_t0_bps": "200", "notional_usdt": "25",
                "net_usdt": "1", "bridge_fee_usd": "0.5"}

    def test_a_row_from_another_pair_never_reaches_the_slot(self):
        from unittest import mock
        with mock.patch.object(self.runner, "bridge_net_ok",
                               lambda row, **kw: True):
            same = self.runner.bridge_candidates(
                {"rows": [self._row(self.runner.BRIDGE_FAMILY, "solana->x-layer")]})
            other = self.runner.bridge_candidates(
                {"rows": [self._row(self.runner.BRIDGE_FAMILY, "eth->bsc")]})
        self.assertEqual(len(same), 1)
        self.assertEqual(other, [])

    def test_the_via_bridge_slot_is_x_layer_only(self):
        from unittest import mock
        with mock.patch.object(self.runner, "bridge_net_ok",
                               lambda row, **kw: True):
            same = self.runner.via_bridge_candidates(
                {"rows": [self._row(self.runner.VIA_BRIDGE_TO_KRAKEN,
                                    "x-layer->solana")]})
            other = self.runner.via_bridge_candidates(
                {"rows": [self._row(self.runner.VIA_BRIDGE_TO_KRAKEN,
                                    "eth->solana")]})
        self.assertEqual(len(same), 1)
        self.assertEqual(other, [])

    def test_the_slot_map_describes_the_scripts_that_exist(self):
        self.assertEqual(self.runner.SLOT_PAIR[self.runner.BRIDGE_FAMILY],
                         "solana->x-layer")
        self.assertEqual(self.runner.SLOT_PAIR[self.runner.VIA_BRIDGE_TO_KRAKEN],
                         "x-layer->solana")


if __name__ == "__main__":
    unittest.main()
