"""The P0 guards from the 2026-09-16 review, tested through the real flows.

Each test names the failure it exists to prevent, because the point of this
file is not coverage — it is that the five ways the review found a half-finished
arbitrage could be reported as a finished one can no longer be reached:

1. fewer spot tokens than the contracts need, reported as a success;
2. an order whose terminal state is unreadable, treated as filled because
   fill rows exist;
3. a spot balance read that failed, followed by a broadcast anyway;
4. an exit that walks past the gates an entry has to pass;
5. a resting order that outlives its group, with nobody watching it.

A sixth file of tests (`test_auto_execute.py`) covers the switches; this one
covers the arithmetic and the state machines underneath them.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import json
import tempfile
import unittest

from arb_monitor.execution.auto_execute import AutoExecutor
from arb_monitor.execution.ledger import ExecutionLedger
from arb_monitor.execution.maker_first import MakerFirstCloser
from arb_monitor.execution.order_flows import (classify_flow, maker_first_close,
                                               maker_first_open)
from arb_monitor.execution.pending_scan import scan_pending_orders
from arb_monitor.execution.quantity import QuantitySpec

from tests.test_full_chain_live import FakeOKX, FakeWalletLeg, runner
from tests.test_maker_close import FakePlan as ClosePlan
from tests.test_maker_close import FakeOKX as CloseOKX
from tests.test_maker_close import FakeSpot as CloseSpot
from tests.test_maker_close import close as run_close
from tests.test_maker_first import FakeOrder
from tests.test_maker_first import FakeOKX as MakerOKX
from tests.test_maker_first import FakePlan, FakeSpot

SIGNAL_ID = "xiaomi-testgroup"


class FakeIntent:
    signal_id = SIGNAL_ID
    created_ts_ms = 1_789_520_000_000


class FlowPlan(FakePlan):
    """The plan shape ``maker_first_open`` actually consumes."""

    asset = "XIAOMI"
    intent = FakeIntent()
    perp_min_size = Decimal("1")

    def summary(self) -> dict:
        return {"asset": self.asset, "share_count": str(self.expected_token_quantity),
                "perp_contract_quantity": str(self.perp_contract_quantity),
                "perp": "okx:XIAOMI-USDT-SWAP"}


class FlowClosePlan(ClosePlan):
    asset = "XIAOMI"
    intent = None
    perp_min_size = Decimal("1")
    perp_venue = "okx"
    # The test runner's clock is frozen at 1e6 ms, so this is "observed now".
    quote_observed_at_s = 1000.0

    def summary(self) -> dict:
        return {"asset": self.asset, "perp": "okx:XIAOMI-USDT-SWAP"}


def maker_venue(fills=None):
    """A maker venue that also answers the submit-time ticker read."""
    okx = MakerOKX(fill_plan=fills if fills is not None else ["3"])
    okx.ticker = lambda inst_id: {"instId": inst_id, "askPx": "3.367",
                                  "bidPx": "3.366"}
    okx.set_leverage = lambda inst_id, lever: None
    return okx


# --- 1. spot short of hedge -------------------------------------------------


class FakeChain:
    """The two objects `maker_first_open` actually touches."""

    def __init__(self, okx, wallet, allowed=True, reasons=None):
        self.okx = okx
        self.wallet = wallet
        self._allowed = allowed
        self._reasons = reasons or []

    def risk_check(self, plan, *, open_contracts, balance_quote=None):
        return self._allowed, list(self._reasons)

    def risk_check_close(self, plan, *, contracts, tokens, positions=None,
                         pending_orders=None):
        return self._allowed, list(self._reasons)


class ShortDeliveryTests(unittest.TestCase):
    NOW = 1_789_520_000_000

    def test_a_fill_that_delivers_fewer_tokens_than_the_hedge_is_not_a_success(self):
        # 3 contracts sold, 2 shares delivered: one share of the short is naked.
        # Before the fix this returned state="spot_short_of_hedge" and the
        # unattended executor counted it as executed, halting nothing.
        okx = maker_venue()
        spot = FakeSpot(deliver=Decimal("2"))
        ledger = ExecutionLedger()
        chain = FakeChain(okx, spot)
        flow = maker_first_open(chain, FlowPlan(), {"limits": {"max_unhedged_bps": "5"},
                                                    "orders": {}}, ledger,
                                balance_quote=Decimal("100"),
                                open_contracts=Decimal(0))
        self.assertEqual(flow.state, "spot_short_of_hedge")
        self.assertFalse(flow.executed)
        self.assertTrue(flow.halt)
        self.assertEqual(flow.payload["legs"]["filled_contracts"], "3")
        self.assertEqual(flow.payload["legs"]["required_tokens"], "3")
        self.assertEqual(flow.payload["legs"]["received_tokens"], "2")
        self.assertEqual(flow.payload["unhedged_tokens"], "-1")
        self.assertEqual(flow.payload["naked_short_tokens"], "1")
        events = [row["event_type"] for row in ledger.events(SIGNAL_ID)]
        self.assertIn("legs", events)
        self.assertIn("spot_short_of_hedge", events)
        # The ledger carries the measured two legs, not a planned quantity.
        legs = [row for row in ledger.events(SIGNAL_ID)
                if row["event_type"] == "legs"][0]["payload"]
        self.assertEqual(legs["residual_tokens"], "-1")
        self.assertEqual(legs["residual_tolerance_bps"], "5")
        self.assertEqual(legs["instrument"], "XIAOMI-USDT-SWAP")
        self.assertEqual(
            ledger.group_instrument(SIGNAL_ID), "XIAOMI-USDT-SWAP")

    def test_the_auto_executor_refuses_to_call_it_executed(self):
        # The unattended entry itself: `executed` must be false and `halt` true
        # for any state carrying a residual.
        self.assertEqual(classify_flow("open", "spot_short_of_hedge", Decimal("-1")),
                         (False, True))
        self.assertEqual(classify_flow("open", "spot_short_of_hedge", Decimal("0")),
                         (False, True))
        self.assertEqual(classify_flow("open", "hedged", Decimal("0")), (True, False))
        # A state nobody listed is not a success either.
        self.assertEqual(classify_flow("open", "brand_new_state", Decimal("0")),
                         (False, True))

    def test_the_unattended_entry_halts_on_a_short_hedge_flow(self):
        # The regression the review named: the flow reports
        # spot_short_of_hedge, and the unattended entry must record
        # executed=false, halt=true and write the stop file.
        import arb_monitor.execution.auto_execute as module
        from arb_monitor.execution.order_flows import FlowResult
        from tests.test_auto_execute import (FakePlan as AutoPlan, FakeStack,
                                             candidate, policy)

        flow = FlowResult("spot_short_of_hedge", {
            "state": "spot_short_of_hedge", "reason": "spot_below_hedge",
            "unhedged_tokens": "-1", "naked_short_tokens": "1",
            "executed": False, "halt": True,
            "plan": {"asset": "XIAOMI", "share_count": "3"},
            "legs": {"filled_contracts": "3", "received_tokens": "2",
                     "residual_tokens": "-1"}})
        original = module.maker_first_open
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "execution.db")
            ledger = ExecutionLedger(path)
            stack = FakeStack(AutoPlan(quote_at_s=self.NOW / 1000 - 1), ledger)
            executor = AutoExecutor(
                policy=policy(mode="preauthorized_program", auto_orders="live"),
                monitor_config={}, ledger_path=path,
                stack_factory=lambda **kwargs: stack,
                stop_file=str(Path(tmp) / "AUTO_HALT"),
                lock_file=str(Path(tmp) / "auto.lock"), clock=lambda: self.NOW)
            try:
                module.maker_first_open = lambda *a, **k: flow
                record = executor.maybe_execute([candidate()], self.NOW)
            finally:
                module.maker_first_open = original
            attempt = record["attempts"][0]
            self.assertEqual(attempt["state"], "spot_short_of_hedge")
            self.assertFalse(attempt["executed"])
            self.assertTrue(attempt["halt"])
            self.assertEqual(record["executed"], 0)
            halt = json.loads(Path(executor.stop_file).read_text())
            self.assertEqual(halt["state"], "spot_short_of_hedge")
            self.assertEqual([row["event_type"]
                              for row in ledger.events("xiaomi-testgroup")],
                             ["auto_halt"])
            ledger.close()

    def test_dust_inside_the_tolerance_is_hedged_and_recorded(self):
        okx = maker_venue()
        spot = FakeSpot(deliver=Decimal("3.0007"))     # 2.3 bps over 3 shares
        ledger = ExecutionLedger()
        flow = maker_first_open(FakeChain(okx, spot), FlowPlan(),
                                {"limits": {"max_unhedged_bps": "5"}, "orders": {}},
                                ledger, balance_quote=Decimal("100"),
                                open_contracts=Decimal(0))
        self.assertEqual(flow.state, "hedged")
        self.assertTrue(flow.executed)
        self.assertFalse(flow.halt)
        self.assertEqual(flow.payload["legs"]["residual_tokens"], "0.0007")
        events = [row["event_type"] for row in ledger.events(SIGNAL_ID)]
        self.assertIn("spot_dust_unhedged", events)

    def test_long_dust_under_half_a_dollar_stays_hedged_past_the_bps_cap(self):
        okx = maker_venue()
        spot = FakeSpot(deliver=Decimal("3.01"))  # ~33 bps over 3 shares
        flow = maker_first_open(
            FakeChain(okx, spot), FlowPlan(),
            {"limits": {"max_unhedged_bps": "5", "max_unhedged_quote_usdt": "0.5"},
             "orders": {}},
            ExecutionLedger(), balance_quote=Decimal("100"),
            open_contracts=Decimal(0))
        self.assertEqual(flow.state, "hedged")
        self.assertTrue(flow.executed)
        self.assertFalse(flow.halt)

    def test_dust_beyond_the_tolerance_stops_the_chain(self):
        okx = maker_venue()
        spot = FakeSpot(deliver=Decimal("4"))          # 3333 bps over 3 shares
        flow = maker_first_open(FakeChain(okx, spot), FlowPlan(),
                                {"limits": {"max_unhedged_bps": "5"}, "orders": {}},
                                ExecutionLedger(), balance_quote=Decimal("100"),
                                open_contracts=Decimal(0))
        self.assertEqual(flow.state, "spot_over_hedge")
        self.assertFalse(flow.executed)
        self.assertTrue(flow.halt)

    def test_an_unwind_that_did_not_complete_is_not_a_safe_state(self):
        class NoUnwind(MakerOKX):
            """The maker fill is real; the buy-back shows no fills at all."""

            def place_ioc(self, **kwargs):
                self.ioc.append(kwargs)
                self.current_fill = Decimal(0)
                return FakeOrder("unwind-1")

        okx = NoUnwind(fill_plan=["3"])
        okx.ticker = lambda inst_id: {"askPx": "3.367", "bidPx": "3.366"}
        okx.set_leverage = lambda inst_id, lever: None
        spot = FakeSpot(fail=True)
        flow = maker_first_open(FakeChain(okx, spot), FlowPlan(),
                                {"limits": {"max_unhedged_bps": "5"}, "orders": {}},
                                ExecutionLedger(), balance_quote=Decimal("100"),
                                open_contracts=Decimal(0))
        self.assertEqual(flow.state, "naked_short_unconfirmed")
        self.assertFalse(flow.executed)
        self.assertTrue(flow.halt)
        self.assertIn("unwind_partial", flow.payload["reason"])


# --- 2. fills present, status unknown --------------------------------------


class UnknownOrderStateTests(unittest.TestCase):
    def test_a_fill_row_with_an_unreadable_order_is_not_a_hedge(self):
        # The venue will not say what the order is; it does show one fill row.
        # The review's failure mode: "fills exist, therefore filled".
        ticking = {"now": 1_000_000}

        def clock():
            ticking["now"] += 5_000
            return ticking["now"]

        wallet = FakeWalletLeg()
        okx = FakeOKX(order_state="",
                      fills=[{"fillSz": "1", "fillPx": "3.366",
                              "fee": "-0.00151470"}])
        ledger = ExecutionLedger()
        chain = runner(wallet, okx, ledger)
        chain.clock = clock
        plan = chain.build_plan(asset="XIAOMI", size_quote=Decimal("4"),
                                perp_instrument="XIAOMI-USDT-SWAP", forced=True)
        result = chain.execute_open(plan, live=True)
        self.assertEqual(result.state, "perp_unknown")
        self.assertNotEqual(result.state, "hedged")
        unknown = [row for row in ledger.events(plan.intent.signal_id)
                   if row["event_type"] == "perp_unknown"]
        self.assertTrue(unknown)
        payload = unknown[0]["payload"]
        self.assertEqual(payload["known_filled"], "1")
        self.assertEqual(payload["unknown_remaining"], "0")
        self.assertTrue(payload["fills_present"])
        # The orchestration layer repeats the state with the residual it knows
        # (the venue cannot tell us whether the order is still live).
        final = [row for row in ledger.events(plan.intent.signal_id)
                 if row["event_type"] == "perp_unknown"][-1]["payload"]
        self.assertIn("unhedged_tokens", final)

    def test_a_close_with_an_unreadable_buyback_never_sells_the_spot(self):
        ticking = {"now": 1_000_000}

        def clock():
            ticking["now"] += 5_000
            return ticking["now"]

        wallet = FakeWalletLeg()
        okx = FakeOKX(order_state="",
                      fills=[{"fillSz": "1", "fillPx": "3.366",
                              "fee": "-0.00151470"}])
        ledger = ExecutionLedger()
        chain = runner(wallet, okx, ledger)
        chain.clock = clock
        plan = chain.build_plan(asset="XIAOMI", size_quote=Decimal("4"),
                                perp_instrument="XIAOMI-USDT-SWAP", forced=True)
        result = chain.execute_close(plan, live=True, token_quantity=Decimal("1"),
                                     contract_quantity=Decimal("1"))
        self.assertEqual(result.state, "perp_unknown")
        self.assertEqual(wallet.swaps, [])     # the spot leg was not sent


# --- 3. unknown on-chain writes are never replayed --------------------------


class UnknownSwapTests(unittest.TestCase):
    def test_a_broadcast_whose_response_was_lost_is_not_replayed(self):
        kind = type("LostBroadcast", (RuntimeError,), {
            "may_have_reached_remote": True})
        okx, spot = CloseOKX(fill_plan=["3"]), CloseSpot()
        spot.fail = True
        spot.failure = kind("network left before the response arrived")

        def boom(**_kwargs):
            spot.calls.append(_kwargs)
            raise spot.failure

        spot.swap_exact_in = boom
        result = run_close(okx, spot)
        self.assertEqual(result.state, "close_unknown")
        self.assertEqual(len(spot.calls), 1)   # one broadcast, never a retry
        self.assertIn("unknown_after_broadcast", result.reason)

    def test_an_unreadable_balance_stops_before_the_next_broadcast(self):
        okx, spot = CloseOKX(fill_plan=["3"]), CloseSpot()
        spot.fail = True

        def no_balance(_address):
            raise RuntimeError("wallet_rpc_unavailable")

        result = run_close(okx, spot, token_balance=no_balance)
        self.assertEqual(result.state, "close_unknown")
        self.assertEqual(spot.calls, [])       # nothing was sent with a blind size
        self.assertIn("spot_balance_unreadable", result.reason)

    def test_a_reverted_receipt_may_be_retried_because_it_moved_nothing(self):
        okx, spot = CloseOKX(fill_plan=["3"]), CloseSpot()
        attempts = {"n": 0}

        def reverted(**_kwargs):
            attempts["n"] += 1
            spot.calls.append(_kwargs)
            if attempts["n"] == 1:
                raise RuntimeError("onchain_leg_not_filled:reverted")
            return CloseSpot.swap_exact_in(spot, **_kwargs)

        spot.swap_exact_in = reverted
        result = run_close(okx, spot)
        self.assertEqual(result.state, "closed")
        self.assertEqual(attempts["n"], 2)


# --- 4. the exit gate is the entry gate -------------------------------------


class CloseGateTests(unittest.TestCase):
    def close_plan(self, chain):
        return chain.build_close_plan(asset="XIAOMI", perp_instrument="XIAOMI-USDT-SWAP",
                                      token_quantity=Decimal("1"),
                                      contract_quantity=Decimal("1"))

    def test_a_kill_switch_stops_a_close_even_when_forced(self):
        with tempfile.TemporaryDirectory() as tmp:
            kill = Path(tmp) / "KILL_SWITCH"
            kill.write_text("TRIPPED")
            chain = runner(policy_overrides={"execution.kill_switch_file": str(kill)})
            plan = self.close_plan(chain)
            plan.forced_plumbing_test = True     # --forced
            allowed, reasons = chain.risk_check_close(
                plan, contracts=Decimal("1"), tokens=Decimal("1"))
            self.assertFalse(allowed)
            self.assertIn("kill_switch", reasons)
            # And the entry gate agrees, in the same words.
            allowed, reasons = chain.risk_check(plan, open_contracts=Decimal(0))
            self.assertFalse(allowed)
            self.assertIn("kill_switch", reasons)

    def test_an_unresolved_halt_stops_the_entry_and_not_the_exit(self):
        """A halt means "open nothing new", not "you may not reduce".

        Changed 2026-09-18 (operator): ``AUTO_HALT`` used to sit in the gate
        both directions share, so the emergency close — and the funding-cost
        stop — were refused by the very state that says something may already
        be stranded. The exit gate has to stay usable while the entry is shut;
        the kill switch remains the switch that stops everything.
        """
        with tempfile.TemporaryDirectory() as tmp:
            halt = Path(tmp) / "AUTO_HALT"
            halt.write_text('{"state": "maker_unknown"}')
            chain = runner(policy_overrides={"execution.auto_halt_file": str(halt)})
            plan = self.close_plan(chain)
            allowed, reasons = chain.risk_check(plan, open_contracts=Decimal(0))
            self.assertFalse(allowed)
            self.assertIn("auto_halt_active", reasons)
            allowed, reasons = chain.risk_check_close(
                plan, contracts=Decimal("1"), tokens=Decimal("1"))
            self.assertTrue(allowed, reasons)
            self.assertNotIn("auto_halt_active", reasons)

    def test_a_close_without_a_short_is_refused(self):
        chain = runner()
        allowed, reasons = chain.risk_check_close(
            self.close_plan(chain), contracts=Decimal("1"), tokens=Decimal("1"),
            positions=[])
        self.assertFalse(allowed)
        self.assertIn("no_short_position_to_close", reasons)

    def test_a_close_larger_than_the_position_is_refused(self):
        chain = runner()
        allowed, reasons = chain.risk_check_close(
            self.close_plan(chain), contracts=Decimal("3"), tokens=Decimal("3"),
            positions=[{"instId": "XIAOMI-USDT-SWAP", "pos": "-1"}])
        self.assertFalse(allowed)
        self.assertTrue(any(r.startswith("close_exceeds_position") for r in reasons))

    def test_a_resting_order_on_the_instrument_blocks_a_close(self):
        chain = runner()
        allowed, reasons = chain.risk_check_close(
            self.close_plan(chain), contracts=Decimal("1"), tokens=Decimal("1"),
            positions=[{"instId": "XIAOMI-USDT-SWAP", "pos": "-1"}],
            pending_orders=[{"instId": "XIAOMI-USDT-SWAP", "clOrdId": "arbm1"}])
        self.assertFalse(allowed)
        self.assertTrue(any(r.startswith("pending_orders_on_instrument")
                            for r in reasons))

    def test_a_close_through_the_flow_function_is_gated_too(self):
        # The CLI is not the only caller, so the gate lives in the flow.
        with tempfile.TemporaryDirectory() as tmp:
            kill = Path(tmp) / "KILL_SWITCH"
            kill.write_text("TRIPPED")
            chain = runner(policy_overrides={"execution.kill_switch_file": str(kill)})
            okx, spot = MakerOKX(fill_plan=["3"]), CloseSpot()
            flow = maker_first_close(chain, FlowClosePlan(), {"orders": {}},
                                     ExecutionLedger(), contracts=Decimal("1"),
                                     tokens=Decimal("1"))
            self.assertEqual(flow.state, "rejected")
            self.assertIn("kill_switch", flow.payload["reason"])
            self.assertEqual(okx.placed, [])
            self.assertEqual(spot.calls, [])


# --- 5. orphan resting orders ----------------------------------------------


class FakeRead:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error

    def pending_orders(self, inst_type="SWAP"):
        if self.error:
            raise self.error
        return list(self.rows)


def resting_order(*, clord, side="sell", filled="0", age_ms=1000,
                  inst="XIAOMI-USDT-SWAP"):
    return {"ordId": "ord-1", "clOrdId": clord, "instId": inst, "side": side,
            "sz": "3", "accFillSz": filled, "state": "live",
            "cTime": str(1_789_520_000_000 - age_ms)}


class OrphanOrderTests(unittest.TestCase):
    NOW = 1_789_520_000_000

    def ledger_with_group(self, state: str):
        ledger = ExecutionLedger()
        ledger.append("xiaomi-abc", state, "maker_order",
                      {"client_order_id": "arbm-deadbeef", "asset": "XIAOMI"},
                      self.NOW - 60_000)
        return ledger

    def test_an_order_from_a_finished_group_is_an_orphan(self):
        # Exactly 2026-09-16 11:08: the group ended as maker_unfilled, a
        # post-only sell stayed on the book and filled sixteen minutes later.
        ledger = self.ledger_with_group("maker_unfilled")
        scan = scan_pending_orders(read=FakeRead([resting_order(clord="arbm-deadbeef")]),
                                   ledger=ledger, now_ms=self.NOW, policy={"orders": {}})
        self.assertTrue(scan.blocked)
        self.assertEqual(scan.orphans[0].verdict, "group_already_terminal")
        self.assertEqual(scan.orphans[0].group_id, "xiaomi-abc")

    def test_an_order_nobody_placed_is_an_orphan(self):
        scan = scan_pending_orders(read=FakeRead([resting_order(clord="arbm-unknown")]),
                                   ledger=ExecutionLedger(), now_ms=self.NOW,
                                   policy={"orders": {}})
        self.assertTrue(scan.blocked)
        self.assertEqual(scan.orphans[0].verdict, "no_group_in_ledger")

    def test_an_order_that_cannot_be_read_blocks_the_cycle(self):
        scan = scan_pending_orders(read=FakeRead(error=RuntimeError("okx_transport")),
                                   ledger=ExecutionLedger(), now_ms=self.NOW,
                                   policy={"orders": {}})
        self.assertEqual(scan.status, "unreadable")
        self.assertTrue(scan.blocked)

    def test_an_order_resting_too_long_is_an_orphan(self):
        ledger = self.ledger_with_group("hedged")
        scan = scan_pending_orders(
            read=FakeRead([resting_order(clord="arbm-deadbeef", age_ms=900_000)]),
            ledger=ledger, now_ms=self.NOW, policy={"orders": {}}, ttl_seconds=120)
        self.assertTrue(scan.blocked)
        self.assertEqual(scan.orphans[0].verdict, "age_beyond_ttl")

    def test_an_order_on_the_wrong_side_is_an_orphan(self):
        ledger = self.ledger_with_group("hedged")     # an opening group
        scan = scan_pending_orders(
            read=FakeRead([resting_order(clord="arbm-deadbeef", side="buy",
                                         age_ms=1_000)]),
            ledger=ledger, now_ms=self.NOW, policy={"orders": {}})
        self.assertTrue(scan.blocked)
        self.assertEqual(scan.orphans[0].verdict, "direction_mismatch")

    def test_an_order_inside_its_window_is_attributed_not_flagged(self):
        ledger = self.ledger_with_group("hedged")
        scan = scan_pending_orders(
            read=FakeRead([resting_order(clord="arbm-deadbeef", age_ms=1_000)]),
            ledger=ledger, now_ms=self.NOW, policy={"orders": {}})
        self.assertFalse(scan.blocked)
        self.assertEqual(scan.orders[0].verdict, "attributed")

    def test_the_unattended_chain_halts_and_writes_the_file(self):
        from tests.test_auto_execute import FakePlan as AutoPlan
        from tests.test_auto_execute import FakeStack, Recorder, candidate, policy

        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = str(Path(tmp) / "execution.db")
            ledger = ExecutionLedger(ledger_path)
            ledger.append("xiaomi-old", "maker_unfilled", "maker_order",
                          {"client_order_id": "arbm-deadbeef"}, self.NOW - 60_000)
            stack = FakeStack(AutoPlan(quote_at_s=self.NOW / 1000 - 1), ledger,
                              pending=[resting_order(clord="arbm-deadbeef")])
            executor = AutoExecutor(
                policy=policy(mode="preauthorized_program", auto_orders="paper"),
                monitor_config={}, ledger_path=ledger_path, notifier=Recorder(),
                stack_factory=lambda **kwargs: stack,
                stop_file=str(Path(tmp) / "AUTO_HALT"),
                lock_file=str(Path(tmp) / "auto.lock"), clock=lambda: self.NOW)
            record = executor.maybe_execute([candidate()], self.NOW)
            attempt = record["attempts"][0]
            self.assertEqual(attempt["state"], "orphan_pending_orders")
            self.assertTrue(attempt["halt"])
            self.assertFalse(attempt["executed"])
            self.assertTrue(Path(executor.stop_file).exists())
            halt = json.loads(Path(executor.stop_file).read_text())
            self.assertEqual(halt["state"], "orphan_pending_orders")
            ledger.close()


# --- 6. crash between the legs, and the ledger gates -------------------------


class RestartRecoveryTests(unittest.TestCase):
    NOW = 1_789_520_000_000

    def executor(self, ledger_path: str, tmp: str, **execution):
        from tests.test_auto_execute import FakePlan as AutoPlan
        from tests.test_auto_execute import FakeStack, policy

        ledger = ExecutionLedger(ledger_path)
        stack = FakeStack(AutoPlan(quote_at_s=self.NOW / 1000 - 1), ledger)
        return AutoExecutor(
            policy=policy(mode="preauthorized_program", auto_orders="paper",
                          **execution),
            monitor_config={}, ledger_path=ledger_path,
            stack_factory=lambda **kwargs: stack,
            stop_file=str(Path(tmp) / "AUTO_HALT"),
            lock_file=str(Path(tmp) / "auto.lock"), clock=lambda: self.NOW)

    def test_a_group_that_died_between_the_legs_blocks_a_new_open(self):
        from tests.test_auto_execute import candidate

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "execution.db")
            ledger = ExecutionLedger(path)
            # The crash shape: the on-chain buy landed, the hedge never did.
            ledger.append("xiaomi-crashed", "spot_filled", "spot_filled",
                          {"leg": "buy", "quote_amount": "10.05"}, self.NOW - 5_000)
            executor = self.executor(path, tmp)
            record = executor.maybe_execute([candidate()], self.NOW)
            attempt = record["attempts"][0]
            self.assertEqual(attempt["state"], "unresolved_exposure_groups")
            self.assertTrue(attempt["halt"])

    def test_a_finished_hedge_does_not_block_a_different_name(self):
        from tests.test_auto_execute import candidate

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "execution.db")
            ledger = ExecutionLedger(path)
            ledger.append("crwv-done", "hedged", "hedged",
                          {"asset": "CRWV", "filled_contracts": "0.01"}, self.NOW - 5_000)
            executor = self.executor(path, tmp)
            record = executor.maybe_execute([candidate(asset="MRNA")], self.NOW)
            attempt = record["attempts"][0]
            self.assertNotEqual(attempt["state"], "unresolved_exposure_groups")
            self.assertFalse(attempt.get("halt"))
            self.assertFalse(Path(executor.stop_file).exists())
            ledger.close()

    def test_a_preparation_state_does_not_block_forever(self):
        from tests.test_auto_execute import candidate

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "execution.db")
            ledger = ExecutionLedger(path)
            ledger.append("xiaomi-prep", "precheck", "precheck", {}, self.NOW - 5_000)
            ledger.append("xiaomi-allow", "allowance_ready", "allowance_ready", {},
                          self.NOW - 4_000)
            executor = self.executor(path, tmp)
            record = executor.maybe_execute([candidate()], self.NOW)
            self.assertEqual(record["attempts"][0]["state"], "paper_hedged")
            ledger.close()


class LedgerConcurrencyTests(unittest.TestCase):
    def test_two_writers_do_not_collide_and_the_journal_is_wal(self):
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "execution.db")
            first = ExecutionLedger(path)
            self.assertEqual(first.conn.execute("pragma journal_mode").fetchone()[0],
                             "wal")
            self.assertGreater(first.conn.execute("pragma busy_timeout").fetchone()[0], 0)
            errors: list[Exception] = []

            def write(tag):
                # Each thread gets its own connection: that is the real
                # concurrency (monitor writer + dashboard reader + a human
                # running ledger-reconcile), not one connection used twice.
                ledger = ExecutionLedger(path)
                try:
                    for index in range(25):
                        ledger.append(f"g-{tag}", "hedged", "spot_filled",
                                      {"leg": "buy", "quote_amount": "1",
                                       "n": index}, 1_789_520_000_000 + index)
                except Exception as exc:  # noqa: BLE001 - the failure is the test
                    errors.append(exc)
                finally:
                    ledger.close()

            threads = [threading.Thread(target=write, args=("a",)),
                       threading.Thread(target=write, args=("b",))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(first.events("g-a")), 25)
            self.assertEqual(len(first.events("g-b")), 25)
            self.assertTrue(first.ping())
            first.close()

    def test_paper_spend_is_kept_apart_from_real_cash(self):
        ledger = ExecutionLedger()
        day = 1_789_520_000_000
        ledger.append("p1", "paper_hedged", "paper_filled",
                      {"leg": "buy", "quote_amount": "10.05"}, day - 1000)
        ledger.append("r1", "closed", "spot_filled",
                      {"leg": "buy", "quote_amount": "5"}, day - 500)
        self.assertEqual(ledger.executed_quote_between(day - 10_000, day + 10_000),
                         Decimal("15.05"))
        self.assertEqual(ledger.executed_quote_between(day - 10_000, day + 10_000,
                                                       include_paper=False),
                         Decimal("5"))
        self.assertEqual(ledger.paper_quote_between(day - 10_000, day + 10_000),
                         Decimal("10.05"))


class QuantitySpecTests(unittest.TestCase):
    def test_the_token_step_is_the_share_step_not_the_contract_step(self):
        class Plan:
            perp_contract_multiplier = Decimal("2")
            perp_lot_size = Decimal("1")
            perp_min_size = Decimal("1")

        spec = QuantitySpec.from_plan(Plan())
        # One contract is two shares, so three contracts need six.
        self.assertEqual(spec.tokens_for_contracts(Decimal("3")), Decimal("6"))
        self.assertEqual(spec.contracts_for_tokens(Decimal("7")), Decimal("3"))
        self.assertEqual(spec.residual_tokens(Decimal("3"), Decimal("6")), Decimal("0"))
        self.assertEqual(spec.residual_tokens(Decimal("3"), Decimal("5")), Decimal("-1"))
        self.assertEqual(spec.residual_bps(Decimal("3"), Decimal("5")),
                         Decimal("-10000") / Decimal("6"))

    def test_a_missing_step_is_refused_rather_than_assumed(self):
        from arb_monitor.execution.quantity import QuantityError

        class Plan:
            perp_contract_multiplier = Decimal("0")
            perp_lot_size = Decimal("1")

        with self.assertRaises(QuantityError):
            QuantitySpec.from_plan(Plan())


if __name__ == "__main__":
    unittest.main()
