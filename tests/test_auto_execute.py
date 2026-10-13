"""Unattended execution: two keys to turn it on, several ways to stop it.

These tests are about the *decisions*, not about trading: when the machine is
allowed to sign at all, what it refuses, what it does when the fresh quote
disagrees with the signal, and what happens after an unknown state. A chain
that can place an order without a human has to be able to prove it also knows
when not to.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from arb_monitor.execution.auto_execute import (
    AutoExecutor, format_auto_result, format_halt,
)
from arb_monitor.execution.ledger import ExecutionLedger
from arb_monitor.execution.okx_account import OKXReadError


def policy(**execution) -> dict:
    # ``auto_close_policy`` and ``live_ack`` are required before ``live`` is
    # accepted at all (added 2026-09-16: the automatic chain opens and does not
    # close, so the arrangement has to be named rather than assumed).
    base = {"mode": "paper_or_explicit_live", "auto_orders": "off",
            "auto_close_policy": "manual_sla", "live_ack": "test-suite"}
    base.update(execution)
    return {
        "execution": base,
        "allowlist": {"venues": ["okx"], "assets": ["XIAOMI"],
                      "instruments": ["XIAOMI-USDT-SWAP"],
                      "chains": ["196"],
                      "spot_tokens": ["0xtoken"], "quote_tokens": ["0xusdg"],
                      "wallets": ["0xw"]},
        "limits": {"per_trade_quote_usdt": "100", "daily_quote_usdt": "100",
                   "quote_ttl_seconds": 300},
        "notify": {"cooldown_seconds": 900},
    }


def candidate(**overrides) -> dict:
    row = {
        "asset": "XIAOMI", "perp_venue": "okx",
        "perp_instrument": "XIAOMI-USDT-SWAP",
        "gross_entry_basis_bps": Decimal("45"),
        "roundtrip": {"complete": True, "breakeven_gross_bps": Decimal("38"),
                      "total_bps": Decimal("13")},
        "mapping_verified": True, "cost_verified": True,
    }
    row.update(overrides)
    return row


class FakeIntent:
    signal_id = "xiaomi-testgroup"
    created_ts_ms = 1_789_520_000_000


class FakePlan:
    asset = "XIAOMI"
    perp_instrument = "XIAOMI-USDT-SWAP"
    perp_contract_quantity = Decimal("3")
    perp_contract_multiplier = Decimal("1")
    perp_lot_size = Decimal("1")
    expected_token_quantity = Decimal("3")
    size_quote = Decimal("10.05")
    perp_bid_usd = Decimal("3.34")
    perp_ask_usd = Decimal("3.343")
    spot_price_usd = Decimal("3.3432")
    quote_symbol = "USDG"
    token_symbol = "wXIAOx"
    gross_bps = Decimal("45")
    cost_bps = Decimal("15")
    intent = FakeIntent()

    def __init__(self, gross: Decimal | None = None, quote_at_s: float = 0.0):
        if gross is not None:
            self.gross_bps = gross
        self.quote_observed_at_s = quote_at_s

    def summary(self) -> dict:
        return {"asset": self.asset, "share_count": str(self.expected_token_quantity),
                "perp_contract_quantity": str(self.perp_contract_quantity),
                "spot_ask_usd": str(self.spot_price_usd),
                "perp_bid_usd": str(self.perp_bid_usd),
                "gross_entry_basis_bps": str(self.gross_bps),
                "size_quote": str(self.size_quote), "quote_token": self.quote_symbol}


class FakeChain:
    def __init__(self, plan: FakePlan, allowed=True):
        self.plan = plan
        self.allowed = allowed
        self.built: list[dict] = []

    def build_plan(self, **kwargs):
        self.built.append(kwargs)
        return self.plan

    def risk_check(self, plan, *, open_contracts, balance_quote=None):
        return (self.allowed, [] if self.allowed else ["net_basis_below_threshold"])


class FakeStack:
    def __init__(self, plan: FakePlan, ledger: ExecutionLedger, *,
                 balance=Decimal("10.94"), allowed=True, pending=None):
        self.chain = FakeChain(plan, allowed=allowed)
        self.ledger = ledger
        self.policy = policy(mode="preauthorized_program", auto_orders="live")
        self._balance = balance
        # The venue's resting orders. The unattended executor asks for these
        # before it does anything else, so a fake that cannot answer is not a
        # fake of a venue.
        self.read = FakeRead(pending if pending is not None else [])
        self.closed = False

    def quote_balance(self):
        return self._balance

    def open_contracts(self):
        return Decimal("0")

    def close(self):
        self.closed = True


class FakeRead:
    """`orders-pending`, which the orphan gate reads every cycle."""

    def __init__(self, pending):
        self.pending = pending

    def pending_orders(self, inst_type="SWAP"):
        return list(self.pending)


class Recorder:
    def __init__(self):
        self.messages: list[str] = []

    def send(self, text):
        self.messages.append(text)
        return {"ok": True, "channel": "test"}


class AutoExecutorTests(unittest.TestCase):
    def make(self, tmp: str, *, stack=None, notifier=None, alert_notifier=None,
             **execution) -> AutoExecutor:
        ledger_path = str(Path(tmp) / "execution.db")
        executor = AutoExecutor(
            policy=policy(**execution), monitor_config={},
            ledger_path=ledger_path, notifier=notifier,
            alert_notifier=alert_notifier,
            stack_factory=(lambda **kwargs: stack) if stack is not None else None,
            stop_file=str(Path(tmp) / "AUTO_HALT"),
            lock_file=str(Path(tmp) / "auto.lock"),
            clock=lambda: 1_789_520_000_000)
        return executor

    # --- the two keys ---------------------------------------------------
    def test_the_default_policy_cannot_execute_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = self.make(tmp)
            allowed, detail = executor.enabled()
            self.assertFalse(allowed)
            self.assertIn("mode_not_preauthorized", detail)
            record = executor.maybe_execute([candidate()], 1_789_520_000_000)
            self.assertEqual(record["executed"], 0)
            self.assertFalse(record["enabled"])

    def test_either_key_alone_is_not_enough(self):
        with tempfile.TemporaryDirectory() as tmp:
            only_mode = self.make(tmp, mode="preauthorized_program")
            self.assertEqual(only_mode.enabled()[1], "auto_orders_off:off")
            only_orders = self.make(tmp, mode="paper_or_explicit_live",
                                    auto_orders="live")
            self.assertIn("mode_not_preauthorized", only_orders.enabled()[1])
            both = self.make(tmp, mode="preauthorized_program", auto_orders="paper")
            self.assertEqual(both.enabled(), (True, "paper"))

    def test_the_kill_switch_outranks_the_authorisation(self):
        with tempfile.TemporaryDirectory() as tmp:
            kill = Path(tmp) / "KILL_SWITCH"
            kill.touch()
            executor = self.make(tmp, mode="preauthorized_program", auto_orders="live",
                                 kill_switch_file=str(kill))
            self.assertEqual(executor.enabled(), (False, "kill_switch"))

    def test_a_halt_file_stops_every_later_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            halt = Path(tmp) / "AUTO_HALT"
            halt.write_text('{"state": "maker_unknown"}')
            executor = self.make(tmp, mode="preauthorized_program", auto_orders="live")
            record = executor.maybe_execute([candidate()], 1_789_520_000_000)
            self.assertEqual(record["executed"], 0)
            self.assertIn("halted", record["reason"])

    # --- the trigger ----------------------------------------------------
    def test_the_entry_bar_is_the_alerts_own_bar(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = self.make(tmp, mode="preauthorized_program", auto_orders="live")
            self.assertFalse(executor.trigger(candidate(
                gross_entry_basis_bps=Decimal("37.9")), 0)[0])
            self.assertEqual(executor.trigger(candidate(
                gross_entry_basis_bps=Decimal("37.9")), 0)[1], "below_breakeven")
            self.assertTrue(executor.trigger(candidate(), 0)[0])

    def test_an_incomplete_cost_or_an_unverified_mapping_blocks_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = self.make(tmp, mode="preauthorized_program", auto_orders="live")
            self.assertEqual(executor.trigger(candidate(
                roundtrip={"complete": False}), 0)[1], "roundtrip_cost_incomplete")
            self.assertEqual(executor.trigger(candidate(
                mapping_verified=False), 0)[1], "mapping_not_verified")
            self.assertEqual(executor.trigger(candidate(
                cost_verified=False), 0)[1], "cost_not_verified")

    # --- venue resolution -----------------------------------------------
    def test_a_candidate_from_a_venue_with_no_adapter_is_routed_to_a_reviewed_one(self):
        # Measured 2026-09-16: the monitor's best perpetual price was on Gate
        # (`gate:XIAOMI_USDT`), which has no reviewed execution adapter. The
        # trade has to happen on the allowlisted instrument for the same asset,
        # and the re-quote then decides whether it still clears the bar.
        with tempfile.TemporaryDirectory() as tmp:
            executor = self.make(tmp, mode="preauthorized_program", auto_orders="live")
            executor.monitor_config = {"tokens": [{
                "asset": "XIAOMI",
                "mappings": {"okx:XIAOMI-USDT-SWAP": {"verified": True}}}]}
            venue_candidate = candidate(perp_venue="gate",
                                        perp_instrument="XIAOMI_USDT")
            instrument, source = executor.resolve_instrument(venue_candidate)
            self.assertEqual(instrument, "XIAOMI-USDT-SWAP")
            self.assertEqual(source, "allowlisted_venue_for_asset")
            # And an asset the policy does not list stays refused.
            self.assertIsNone(executor.resolve_instrument(
                candidate(asset="UNLISTED", perp_instrument="UNLISTED_USDT"))[0])

    # --- the daily quota ------------------------------------------------
    def test_the_daily_quota_is_read_from_the_ledger_not_remembered(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = self.make(tmp, mode="preauthorized_program", auto_orders="live")
            day = 1_789_520_000_000
            ledger = ExecutionLedger(executor.ledger_path)
            try:
                ledger.append("g1", "hedged", "spot_filled",
                              {"leg": "buy", "quote_amount": "10.05",
                               "token_quantity": "3"}, day - 1000)
                ledger.append("g2", "hedged", "spot_filled",
                              {"leg": "buy", "quote_amount": "10.05"}, day - 500)
                # A sale is not a spend, and yesterday is not today.
                ledger.append("g3", "closed", "spot_filled",
                              {"leg": "sell", "quote_amount": "10.00"}, day - 500)
                ledger.append("g4", "hedged", "spot_filled",
                              {"leg": "buy", "quote_amount": "99"}, day - 86_400_000)
            finally:
                ledger.close()
            self.assertEqual(executor.daily_used(day), Decimal("20.10"))

    def test_an_execution_that_would_pass_the_daily_cap_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = FakePlan(quote_at_s=1_789_519_999)
            stack = FakeStack(plan, ExecutionLedger(str(Path(tmp) / "execution.db")))
            executor = self.make(tmp, stack=stack, mode="preauthorized_program",
                                 auto_orders="live")
            ledger = ExecutionLedger(executor.ledger_path)
            try:
                # A *finished* group: the spend counts, the group does not look
                # like unresolved exposure (the ledger gate added 2026-09-16
                # stops on any group whose last state may still hold something).
                ledger.append("old", "closed", "spot_filled",
                              {"leg": "buy", "quote_amount": "95"}, 1_789_519_000_000)
            finally:
                ledger.close()
            record = executor.maybe_execute([candidate()], 1_789_520_000_000)
            attempt = record["attempts"][0]
            self.assertEqual(attempt["state"], "daily_quota_reached")
            self.assertFalse(attempt["executed"])

    # --- re-quote -------------------------------------------------------
    def test_a_signal_that_no_longer_clears_the_bar_is_not_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = FakePlan(gross=Decimal("20"), quote_at_s=1_789_519_999)
            stack = FakeStack(plan, ExecutionLedger(str(Path(tmp) / "execution.db")))
            executor = self.make(tmp, stack=stack, mode="preauthorized_program",
                                 auto_orders="live")
            record = executor.maybe_execute([candidate()], 1_789_520_000_000)
            self.assertEqual(record["attempts"][0]["state"], "requote_below_breakeven")
            self.assertFalse(stack.chain.risk_check(FakePlan(), open_contracts=Decimal(0))[1])

    def test_a_stale_execution_quote_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = FakePlan(quote_at_s=1_789_519_000)   # 1000s old at execution
            stack = FakeStack(plan, ExecutionLedger(str(Path(tmp) / "execution.db")))
            executor = self.make(tmp, stack=stack, mode="preauthorized_program",
                                 auto_orders="live")
            record = executor.maybe_execute([candidate()], 1_789_520_000_000)
            self.assertEqual(record["attempts"][0]["state"], "requote_expired")

    def test_a_persistent_signal_is_executed_once_not_once_per_cycle(self):
        # Measured 2026-09-16: the paper chain fired on the same SHEIN signal at
        # 12:00:25 and again at 12:00:42. One signal is one trade, not one per
        # cycle — the open-contract limit alone would only stop it after it had
        # already acted repeatedly.
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ExecutionLedger(str(Path(tmp) / "execution.db"))
            stack = FakeStack(FakePlan(quote_at_s=1_789_519_999), ledger)
            executor = self.make(tmp, stack=stack, mode="preauthorized_program",
                                 auto_orders="paper")
            first = executor.maybe_execute([candidate()], 1_789_520_000_000)
            self.assertEqual(first["executed"], 1)
            second = executor.maybe_execute([candidate()], 1_789_520_060_000)
            self.assertEqual(second["executed"], 0)
            self.assertEqual(second["skipped"].get("auto_cooldown"), 1)
            self.assertGreater(second["cooldown_remaining_ms"], 0)

    def test_the_cooldown_survives_a_restart(self):
        # A new process must not repeat what the previous one already did: the
        # ledger, not the process, is the memory.
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ExecutionLedger(str(Path(tmp) / "execution.db"))
            stack = FakeStack(FakePlan(quote_at_s=1_789_519_999), ledger)
            executor = self.make(tmp, stack=stack, mode="preauthorized_program",
                                 auto_orders="paper")
            executor.maybe_execute([candidate()], 1_789_520_000_000)
            restarted = self.make(tmp, stack=stack, mode="preauthorized_program",
                                  auto_orders="paper")
            record = restarted.maybe_execute([candidate()], 1_789_520_060_000)
            self.assertEqual(record["executed"], 0)
            self.assertEqual(record["skipped"].get("auto_cooldown"), 1)

    # --- paper ----------------------------------------------------------
    def test_paper_execution_records_a_round_trip_and_spends_no_money(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ExecutionLedger(str(Path(tmp) / "execution.db"))
            stack = FakeStack(FakePlan(quote_at_s=1_789_519_999), ledger)
            notifier = Recorder()
            executor = self.make(tmp, stack=stack, notifier=notifier,
                                 mode="preauthorized_program", auto_orders="paper")
            record = executor.maybe_execute([candidate()], 1_789_520_000_000)
            attempt = record["attempts"][0]
            self.assertEqual(attempt["state"], "paper_hedged")
            self.assertTrue(attempt["executed"])
            events = [row["event_type"] for row in ledger.events("xiaomi-testgroup")]
            self.assertEqual(events, ["paper_precheck", "paper_filled"])
            self.assertIn("自动执行·纸面", notifier.messages[0])
            self.assertIn("未产生任何真实成交", notifier.messages[0])
            # The modelled spend is tracked, and it does NOT consume the real
            # cash limit: measured 2026-09-16, four paper groups had put the
            # live board at 90.252212/100 with no money moved, which would have
            # locked the paper chain out of the budget it rehearses.
            self.assertEqual(executor.paper_used(1_789_520_000_000), Decimal("10.05"))
            self.assertEqual(executor.daily_used(1_789_520_000_000), Decimal("0"))

    # --- locks and halts ------------------------------------------------
    def test_entering_the_flow_without_a_write_is_not_a_halt(self):
        """Operator decision 2026-09-21: only a real write can be "in flight".

        The flow is entered before anything is signed, so a failure there — the
        risk gate re-read inside it — must be a skip for this cycle. It used to
        halt on `entered_flow` alone.
        """
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ExecutionLedger(str(Path(tmp) / "execution.db"))
            stack = FakeStack(FakePlan(quote_at_s=1_789_519_999), ledger)
            calls = {"n": 0}

            def second_call_fails(plan, *, open_contracts, balance_quote=None):
                calls["n"] += 1
                if calls["n"] >= 2:      # the re-check inside the flow
                    raise OKXReadError("okx_transport:URLError")
                return (True, [])

            stack.chain.risk_check = second_call_fails
            executor = self.make(tmp, stack=stack,
                                 mode="preauthorized_program", auto_orders="live")
            record = executor.maybe_execute([candidate()], 1_789_520_000_000)
            attempt = record["attempts"][0]
            self.assertEqual(attempt["state"], "execution_error")
            self.assertFalse(attempt["halt"])
            self.assertFalse(attempt["writes_started"])
            self.assertFalse(attempt["exposure_possible"])
            self.assertFalse(Path(executor.stop_file).exists())
            ledger.close()

    def test_a_halt_pulls_back_the_orders_it_placed_and_names_them(self):
        """A halt that leaves a resting order alive is a deferred fill.

        Measured 2026-09-21: the order placed at 06:45 filled at 06:50 — three
        minutes after AUTO_HALT was written — and no ledger row mentioned it, so
        `ledger-triage --check-account` answered `may_hold_position: false`
        while the account was short two contracts. So the halt (a) cancels by
        client id even when reads are down, and (b) records the ids so the order
        can be attributed to its group afterwards.
        """
        class FakeTrade:
            def __init__(self, cancel_ok=True):
                self.submitted_client_ids = ["arbmTESTORDER00001"]
                self.cancel_ok = cancel_ok
                self.cancelled: list[str] = []

            def cancel_by_client_id(self, inst_id, client_order_id):
                self.cancelled.append(client_order_id)
                if not self.cancel_ok:
                    raise OKXReadError("okx_transport:URLError")
                return {"cancelled": True, "sCode": "0"}

        for cancel_ok, expected_live in ((True, []), (False, ["arbmTESTORDER00001"])):
            with self.subTest(cancel_ok=cancel_ok):
                with tempfile.TemporaryDirectory() as tmp:
                    ledger = ExecutionLedger(str(Path(tmp) / "execution.db"))
                    stack = FakeStack(FakePlan(quote_at_s=1_789_519_999), ledger)
                    stack.trade = FakeTrade(cancel_ok=cancel_ok)

                    def unreadable(plan, *, open_contracts, balance_quote=None):
                        raise OKXReadError("okx_transport:URLError")

                    stack.chain.risk_check = unreadable
                    executor = self.make(tmp, stack=stack,
                                         mode="preauthorized_program",
                                         auto_orders="live")
                    record = executor.maybe_execute([candidate()], 1_789_520_000_000)
                    attempt = record["attempts"][0]
                    self.assertTrue(attempt["halt"])          # a write was sent
                    self.assertTrue(attempt["exposure_possible"])
                    # One confirmable cancel, or two bounded attempts before it
                    # is reported as possibly still live.
                    self.assertEqual(stack.trade.cancelled,
                                     ["arbmTESTORDER00001"] * (1 if cancel_ok else 2))
                    self.assertEqual(attempt["live_client_order_ids"], expected_live)
                    written = json.loads(Path(executor.stop_file).read_text())
                    self.assertEqual(written["orders"][0]["client_order_id"],
                                     "arbmTESTORDER00001")
                    # The reconciliation reads this: the order is attributable
                    # to the group that placed it.
                    self.assertEqual(
                        ledger.client_order_ids()["arbmTESTORDER00001"], "auto-xiaomi")
                    ledger.close()

    def test_a_position_read_failure_before_any_write_is_not_a_halt(self):
        """Listing positions is a read; losing it cannot mean "a write is in flight".

        Measured 2026-09-21 06:47 and 2026-09-19 17:46: `OKXReadError:
        okx_transport:URLError` on asset SHEIN wrote `data/AUTO_HALT` while the
        ledger recorded no group and no order. The chain then refused to open
        anything until an operator reconciled it — twice, about a day each.
        """
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ExecutionLedger(str(Path(tmp) / "execution.db"))
            stack = FakeStack(FakePlan(quote_at_s=1_789_519_999), ledger)

            def unreadable():
                raise OKXReadError("okx_transport:URLError")

            stack.open_contracts = unreadable
            executor = self.make(tmp, stack=stack,
                                 mode="preauthorized_program", auto_orders="live")
            record = executor.maybe_execute([candidate()], 1_789_520_000_000)
            attempt = record["attempts"][0]
            self.assertEqual(attempt["state"], "execution_error")
            self.assertFalse(attempt["executed"])
            self.assertFalse(attempt["halt"])
            self.assertFalse(Path(executor.stop_file).exists())
            ledger.close()

    def test_a_held_lock_stops_a_second_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ExecutionLedger(str(Path(tmp) / "execution.db"))
            stack = FakeStack(FakePlan(quote_at_s=1_789_519_999), ledger)
            notifier = Recorder()
            executor = self.make(tmp, stack=stack, notifier=notifier,
                                 mode="preauthorized_program", auto_orders="live")
            holder = executor._acquire_lock()
            self.assertIsNotNone(holder)
            try:
                record = executor.maybe_execute([candidate()], 1_789_520_000_000)
            finally:
                executor._release_lock(holder)
            attempt = record["attempts"][0]
            self.assertEqual(attempt["state"], "lock_held")
            self.assertFalse(attempt["executed"])
            # A skip, not a halt: the holder may be placing this very trade.
            self.assertFalse(attempt["halt"])
            self.assertFalse(Path(executor.stop_file).exists())

    def test_an_unknown_result_halts_the_chain_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ExecutionLedger(str(Path(tmp) / "execution.db"))
            stack = FakeStack(FakePlan(quote_at_s=1_789_519_999), ledger)
            notifier = Recorder()
            executor = self.make(tmp, stack=stack, notifier=notifier,
                                 mode="preauthorized_program", auto_orders="live")
            original = executor.order_mode
            try:
                import arb_monitor.execution.auto_execute as module

                class FakeFlow:
                    state = "maker_unknown"
                    payload = {"state": state, "reason": "order_state_unreadable"}

                module.maker_first_open = lambda *a, **k: FakeFlow()
                executor.order_mode_override = original  # keep the linter honest
                record = executor.maybe_execute([candidate()], 1_789_520_000_000)
            finally:
                import arb_monitor.execution.auto_execute as module
                module.maker_first_open = MAKER_FIRST_OPEN
            attempt = record["attempts"][0]
            self.assertEqual(attempt["state"], "maker_unknown")
            self.assertTrue(attempt["halt"])
            halt_file = Path(executor.stop_file)
            self.assertTrue(halt_file.exists())
            self.assertIn("maker_unknown", halt_file.read_text())
            self.assertIn("【自动执行·已停止】", notifier.messages[0])
            self.assertTrue(any("halted" in m or "【自动执行·实盘】" in m
                                for m in notifier.messages[1:]))
            # The next cycle refuses to do anything at all.
            again = executor.maybe_execute([candidate()], 1_789_520_000_001)
            self.assertEqual(again["executed"], 0)
            self.assertIn("halted", again["reason"])
            # And the halt is in the ledger, where a reconciliation can find it.
            events = [row["event_type"] for row in ledger.events("xiaomi-testgroup")]
            self.assertIn("auto_halt", events)

    def test_the_rendered_messages_are_distinguishable_from_an_alert(self):
        text = format_auto_result(candidate(), {"state": "rejected",
                                                "executed": False,
                                                "reason": "net_basis_below_threshold",
                                                "plan": FakePlan().summary()},
                                  1_789_520_000_000)
        self.assertIn("【自动执行·实盘】", text)
        self.assertIn("未执行", text)
        self.assertNotIn("【套利监控】", text)
        self.assertIn("已停止自动执行", format_halt({"state": "maker_unknown",
                                                 "reason": "x",
                                                 "asset": "XIAOMI"}) + "已停止自动执行")

    def test_a_blocked_preflight_alerts_a_human_without_stopping_the_chain(self):
        """2026-09-18: the allowance was spent, and the machine found out late.

        The refusal itself is a skip (nothing was sent, the next cycle tries
        again), but the *cause* only a person can fix, so it has to reach the
        operational room rather than the channel people scan for ideas — and it
        must not write ``AUTO_HALT``, which would need reconciling for a trade
        that never existed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ExecutionLedger(str(Path(tmp) / "execution.db"))
            stack = FakeStack(FakePlan(quote_at_s=1_789_519_999), ledger)
            signals, alerts = Recorder(), Recorder()
            executor = self.make(tmp, stack=stack, notifier=signals,
                                 alert_notifier=alerts,
                                 mode="preauthorized_program", auto_orders="live")
            try:
                import arb_monitor.execution.auto_execute as module

                class FakeFlow:
                    state = "spot_preflight_blocked"
                    payload = {"state": state, "halt": False, "executed": False,
                               "reason": "spot_preflight_allowance_short:1006008<10339067",
                               "unhedged_tokens": "0"}

                module.maker_first_open = lambda *a, **k: FakeFlow()
                record = executor.maybe_execute([candidate()], 1_789_520_000_000)
            finally:
                import arb_monitor.execution.auto_execute as module
                module.maker_first_open = MAKER_FIRST_OPEN
            attempt = record["attempts"][0]
            self.assertEqual(attempt["state"], "spot_preflight_blocked")
            self.assertFalse(attempt["halt"])
            self.assertFalse(Path(executor.stop_file).exists())
            self.assertEqual(signals.messages, [])          # not the signal room
            self.assertEqual(len(alerts.messages), 1)       # the operational one
            self.assertIn("没有挂出", alerts.messages[0])
            self.assertIn("allowance_short", alerts.messages[0])


import arb_monitor.execution.auto_execute as _module  # noqa: E402
MAKER_FIRST_OPEN = _module.maker_first_open


if __name__ == "__main__":
    unittest.main()
