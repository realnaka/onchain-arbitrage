"""The funding-cost stop: close the group before it pays for another interval.

Operator's rule (2026-09-16): while holding, if the funding rate is negative
(our short pays) and the funding actually paid reaches 1 USD, then one minute
before the next settlement the position is closed **regardless of the basis**.

These tests pin four things the rule must never get wrong:

* "we pay" is decided by the sign of the rate, not by a guess about the venue;
* the cumulative paid amount comes from the account's bill lines;
* the deadline is the settlement that is about to happen, minus the lead time;
* unreadable funding data produces an alert, never a silent 'no cost' and never
  a close.
"""
from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from arb_monitor.execution.auto_execute import AutoExecutor
from arb_monitor.execution.funding_exit import (FundingExitPolicy,
                                                decide, read_funding_state)
from arb_monitor.execution.ledger import ExecutionLedger

from tests.test_auto_execute import FakePlan, Recorder, policy

SETTLEMENT = 1_789_520_000_000
NOW = SETTLEMENT - 30_000            # 30 s before settlement: inside the window


class FundingRuleTests(unittest.TestCase):
    policy = FundingExitPolicy(threshold_quote=Decimal("1"), lead_seconds=60)

    def observation(self, *, rate="-0.0001", paid="0", settlement=SETTLEMENT,
                    readable=True, error=None, notional="10"):
        return read_state_stub(rate=rate, paid=paid, settlement=settlement,
                               readable=readable, error=error, notional=notional)

    def test_a_positive_rate_costs_the_short_nothing(self):
        # Positive funding pays the short: there is no cost to stop.
        decision = decide(self.policy, self.observation(rate="0.0001", paid="5"), NOW)
        self.assertEqual(decision.action, "none")
        self.assertIn("rate_not_costing_us", decision.reason)

    def test_below_the_threshold_nothing_happens(self):
        decision = decide(self.policy, self.observation(paid="0.99"), NOW)
        self.assertEqual(decision.action, "none")
        self.assertIn("below_1", decision.reason)

    def test_at_the_threshold_inside_the_window_it_closes_now(self):
        decision = decide(self.policy, self.observation(paid="1"), NOW)
        self.assertEqual(decision.action, "close_now")
        self.assertEqual(decision.close_at_ms, SETTLEMENT - 60_000)
        self.assertTrue(decision.alerts)

    def test_before_the_window_it_is_armed_with_the_deadline(self):
        earlier = SETTLEMENT - 10 * 60_000
        decision = decide(self.policy, self.observation(paid="1.2"), earlier)
        self.assertEqual(decision.action, "armed")
        self.assertEqual(decision.close_at_ms, SETTLEMENT - 60_000)

    def test_a_missed_window_closes_at_the_earliest_opportunity(self):
        # The sampler runs on a 60 s clock, so the window can be missed. Waiting
        # for the *next* interval would pay another settlement for nothing.
        after = SETTLEMENT + 5_000
        decision = decide(self.policy, self.observation(paid="1.5"), after)
        self.assertEqual(decision.action, "close_now")
        self.assertIn("missed", decision.reason)

    def test_unreadable_data_is_an_alert_not_a_close(self):
        decision = decide(self.policy, self.observation(paid="9", readable=False,
                                                        error="URLError"), NOW)
        self.assertEqual(decision.action, "unknown")
        self.assertTrue(decision.alerts)

    def test_a_missing_settlement_time_is_unknown(self):
        decision = decide(self.policy, self.observation(paid="9", settlement=None),
                          NOW)
        self.assertEqual(decision.action, "unknown")

    def test_the_projected_next_payment_is_reported_not_acted_on(self):
        obs = self.observation(paid="0.9", notional="1000")
        # 1000 × 0.0001 = 0.1, so holding another interval would cross 1 USD —
        # information for the operator, not a trigger the code invented.
        self.assertEqual(obs.projected_next_payment_quote(), Decimal("0.1"))
        self.assertEqual(decide(self.policy, obs, NOW).action, "none")

    def test_the_rule_can_be_disabled_by_config(self):
        off = FundingExitPolicy.from_policy({"funding_exit": {"enabled": False}})
        self.assertFalse(off.enabled)
        self.assertEqual(decide(off, self.observation(paid="5"), NOW).action,
                         "disabled")

    def test_the_policy_reads_the_operators_numbers(self):
        parsed = FundingExitPolicy.from_policy({"funding_exit": {
            "enabled": True, "threshold_quote": "2.5", "lead_seconds": 90}})
        self.assertEqual(parsed.threshold_quote, Decimal("2.5"))
        self.assertEqual(parsed.lead_seconds, 90)


def read_state_stub(*, rate, paid, settlement, readable=True, error=None,
                    notional="10"):
    from arb_monitor.execution.funding_exit import FundingObservation
    obs = FundingObservation(instrument="XIAOMI-USDT-SWAP", observed_at_ms=NOW,
                             readable=readable, error=error,
                             position_notional_quote=Decimal(str(notional)))
    if rate not in (None, ""):
        obs.funding_rate = Decimal(str(rate))
    obs.settlement_at_ms = settlement
    obs.paid_quote = Decimal(str(paid))
    return obs


class FundingReadTests(unittest.TestCase):
    """The two venue reads and how their numbers are interpreted."""

    def test_paid_funding_is_summed_from_the_bill_lines(self):
        def public(path, query):
            return [{"instId": "XIAOMI-USDT-SWAP", "fundingRate": "-0.0002",
                     "nextFundingRate": "-0.0001", "fundingTime": str(SETTLEMENT)}]

        def signed(path, query):
            # Negative pnl = paid; a positive line is money we received and must
            # not be subtracted from the cost.
            return [{"ts": "1", "pnl": "-0.6", "type": "8"},
                    {"ts": "2", "pnl": "-0.5", "type": "8"},
                    {"ts": "3", "pnl": "0.2", "type": "8"}]

        obs = read_funding_state(instrument="XIAOMI-USDT-SWAP",
                                 position_start_ms=1, now_ms=NOW,
                                 public_get=public, signed_get=signed,
                                 position_notional_quote=Decimal("10"))
        self.assertTrue(obs.readable)
        self.assertEqual(obs.paid_quote, Decimal("1.1"))
        self.assertEqual(obs.received_quote, Decimal("0.2"))
        self.assertEqual(obs.settlement_at_ms, SETTLEMENT)
        self.assertTrue(obs.pays_us)

    def test_the_rate_read_uses_funding_time_not_the_next_one(self):
        seen = {}

        def public(path, query):
            return [{"fundingRate": "-0.001", "fundingTime": "1000",
                     "nextFundingTime": "2000"}]

        obs = read_funding_state(instrument="X", position_start_ms=1, now_ms=NOW,
                                 public_get=public,
                                 signed_get=lambda p, q: [], )
        self.assertEqual(obs.settlement_at_ms, 1000)

    def test_a_venue_that_will_not_answer_is_unreadable(self):
        def broken(path, query):
            raise RuntimeError("okx_transport:URLError")

        obs = read_funding_state(instrument="X", position_start_ms=1, now_ms=NOW,
                                 public_get=broken, signed_get=lambda p, q: [])
        self.assertFalse(obs.readable)
        self.assertIn("URLError", obs.error)
        self.assertEqual(obs.paid_quote, Decimal("0"))   # unknown, not "none paid"

    def test_a_failed_bill_read_makes_the_whole_observation_unreadable(self):
        def public(path, query):
            return [{"fundingRate": "-0.001", "fundingTime": str(SETTLEMENT)}]

        def broken(path, query):
            raise RuntimeError("okx_transport:URLError")

        obs = read_funding_state(instrument="X", position_start_ms=1, now_ms=NOW,
                                 public_get=public, signed_get=broken)
        self.assertFalse(obs.readable)
        self.assertIn("funding_bills", obs.error)


class FakeFundingRead:
    """Positions + funding reads for the executor test."""

    def __init__(self, *, rate="-0.0002", paid="-1.1", settle_at=SETTLEMENT):
        self.rate, self.paid, self.settle_at = rate, paid, settle_at
        self.pending = []

    def positions(self, inst_type="SWAP"):
        return [{"instId": "XIAOMI-USDT-SWAP", "pos": "-3",
                 "notionalUsd": "10.0"}]

    def pending_orders(self, inst_type="SWAP"):
        return list(self.pending)

    def get(self, path, query=""):
        if path.endswith("/account/bills"):
            return [{"ts": "1", "pnl": self.paid, "type": "8"}]
        return []


class FakeTrade:
    def __init__(self, rate="-0.0002", settle_at=SETTLEMENT):
        self.rate, self.settle_at = rate, settle_at

    def public(self, path, query=""):
        return [{"instId": "XIAOMI-USDT-SWAP", "fundingRate": self.rate,
                 "fundingTime": str(self.settle_at)}]


class FakeChain:
    def __init__(self):
        self.closed: list[dict] = []

    def build_close_plan(self, **kwargs):
        plan = FakePlan(quote_at_s=NOW / 1000)
        plan.asset = kwargs.get("asset", "XIAOMI")
        plan.perp_instrument = kwargs.get("perp_instrument", "XIAOMI-USDT-SWAP")
        plan.token_address = "0xtoken"
        plan.token_symbol = "wXIAOx"
        plan.token_decimals = 18
        plan.quote_token_address = "0xusdg"
        plan.quote_decimals = 6
        plan.expected_token_quantity = Decimal("3")
        plan.perp_contract_quantity = Decimal("3")
        plan.intent = None
        plan.summary = lambda: {"asset": plan.asset,
                                "perp": "okx:XIAOMI-USDT-SWAP"}
        return plan

    def risk_check_close(self, plan, *, contracts, tokens, positions=None,
                         pending_orders=None):
        return True, []


class FakeWalletLeg:
    def balance_of(self, address, runner):
        return Decimal("3")


class FakeFundingStack:
    def __init__(self, *, rate="-0.0002", paid="-1.1", settle_at=SETTLEMENT):
        self.read = FakeFundingRead(rate=rate, paid=paid, settle_at=settle_at)
        self.trade = FakeTrade(rate=rate, settle_at=settle_at)
        self.chain = FakeChain()
        self.wallet_leg = FakeWalletLeg()
        self.runner = None
        self.policy = policy(mode="preauthorized_program", auto_orders="live")
        self.policy["funding_exit"] = {"enabled": True, "threshold_quote": "1",
                                       "lead_seconds": 60}

    def close(self):
        pass


class FundingExitExecutorTests(unittest.TestCase):
    def executor(self, tmp, stack, notifier, **execution):
        base = policy(mode="preauthorized_program",
                      **{"auto_orders": "live", **execution})
        base["funding_exit"] = {"enabled": True, "threshold_quote": "1",
                                "lead_seconds": 60}
        return AutoExecutor(
            policy=base,
            monitor_config={"tokens": [{"asset": "XIAOMI", "mappings":
                                        {"okx:XIAOMI-USDT-SWAP": {"verified": True}}}]},
            ledger_path=str(Path(tmp) / "execution.db"), notifier=notifier,
            alert_notifier=notifier,
            stack_factory=lambda **kwargs: stack,
            stop_file=str(Path(tmp) / "AUTO_HALT"),
            lock_file=str(Path(tmp) / "auto.lock"), clock=lambda: NOW)

    def ledger_with_short(self, tmp, *, state="hedged"):
        path = str(Path(tmp) / "execution.db")
        ledger = ExecutionLedger(path)
        ledger.append("xiaomi-open", "precheck", "precheck",
                      {"plan": {"asset": "XIAOMI", "perp": "okx:XIAOMI-USDT-SWAP"}},
                      NOW - 3_600_000)
        ledger.append("xiaomi-open", state, state,
                      {"signal_id": "xiaomi-open"}, NOW - 3_500_000)
        ledger.close()
        return path

    def test_the_rule_closes_the_group_through_the_reviewed_close_path(self):
        import arb_monitor.execution.auto_execute as module
        from arb_monitor.execution.order_flows import FlowResult

        calls = {}

        def fake_close(chain, plan, pol, ledger, **kwargs):
            calls.update(kwargs)
            calls["plan_asset"] = plan.asset
            ledger.append("xiaomi-close", "closed", "spot_sold",
                          {"leg": "sell", "quote_amount": "10"}, NOW)
            return FlowResult("closed", {"state": "closed", "executed": True,
                                         "halt": False, "spot": {"tx_hash": "0xabc"},
                                         "reason": ""})

        with tempfile.TemporaryDirectory() as tmp:
            self.ledger_with_short(tmp)
            notifier = Recorder()
            stack = FakeFundingStack()
            executor = self.executor(tmp, stack, notifier)
            original = module.maker_first_close
            try:
                module.maker_first_close = fake_close
                record = executor.maybe_funding_exit(NOW)
            finally:
                module.maker_first_close = original
            self.assertEqual(len(record["closed"]), 1)
            self.assertEqual(record["closed"][0]["state"], "closed")
            # The close was addressed to the real venue size and the real wallet
            # balance, and it told the opening group who closed it.
            self.assertEqual(calls["contracts"], Decimal("3"))
            self.assertEqual(calls["tokens"], Decimal("3"))
            self.assertEqual(calls["opened_group_id"], "xiaomi-open")
            self.assertEqual(calls["plan_asset"], "XIAOMI")

    def test_an_armed_decision_alerts_once_and_closes_nothing(self):
        import arb_monitor.execution.auto_execute as module

        with tempfile.TemporaryDirectory() as tmp:
            self.ledger_with_short(tmp)
            notifier = Recorder()
            # One hour before settlement: armed, not yet due.
            stack = FakeFundingStack(settle_at=NOW + 3_600_000)
            executor = self.executor(tmp, stack, notifier)
            original = module.maker_first_close
            try:
                module.maker_first_close = lambda *a, **k: self.fail(
                    "an armed rule must not close early")
                first = executor.maybe_funding_exit(NOW)
                second = executor.maybe_funding_exit(NOW + 60_000)
            finally:
                module.maker_first_close = original
            self.assertEqual(first["evaluations"][0]["action"], "armed")
            self.assertEqual(len(first["alerts"]), 1)
            # Alerted once per settlement, not once per cycle.
            self.assertEqual(second["alerts"], [])
            self.assertEqual(len([m for m in notifier.messages if "资金费" in m]), 1)

    def test_unreadable_funding_alerts_instead_of_closing(self):
        import arb_monitor.execution.auto_execute as module

        with tempfile.TemporaryDirectory() as tmp:
            self.ledger_with_short(tmp)
            notifier = Recorder()
            stack = FakeFundingStack()
            stack.read.get = lambda path, query="": (_ for _ in ()).throw(
                RuntimeError("okx_transport:URLError"))
            executor = self.executor(tmp, stack, notifier)
            original = module.maker_first_close
            try:
                module.maker_first_close = lambda *a, **k: self.fail(
                    "unknown funding data must never trigger a close")
                record = executor.maybe_funding_exit(NOW)
            finally:
                module.maker_first_close = original
            self.assertEqual(record["evaluations"][0]["action"], "unknown")
            self.assertEqual(record["alerts"], ["xiaomi-open"])
            self.assertEqual(record["closed"], [])

    def test_the_rule_is_inert_in_paper_mode(self):
        import arb_monitor.execution.auto_execute as module

        with tempfile.TemporaryDirectory() as tmp:
            self.ledger_with_short(tmp)
            stack = FakeFundingStack()
            executor = self.executor(tmp, stack, Recorder(), auto_orders="paper")
            original = module.maker_first_close
            try:
                module.maker_first_close = lambda *a, **k: self.fail(
                    "paper must not close a real position")
                record = executor.maybe_funding_exit(NOW)
            finally:
                module.maker_first_close = original
            self.assertEqual(record["reason"], "order_mode_not_live")
            self.assertEqual(record["closed"], [])

    def test_a_refused_funding_close_pages_a_human(self):
        # The close gate can legitimately refuse (a kill switch, an unreadable
        # balance). The position is still paying funding, so a refusal must not
        # pass quietly.
        import arb_monitor.execution.auto_execute as module
        from arb_monitor.execution.order_flows import FlowResult

        with tempfile.TemporaryDirectory() as tmp:
            self.ledger_with_short(tmp)
            notifier = Recorder()
            stack = FakeFundingStack()
            executor = self.executor(tmp, stack, notifier)
            original = module.maker_first_close
            try:
                module.maker_first_close = lambda *a, **k: FlowResult(
                    "rejected", {"state": "rejected", "executed": False,
                                 "halt": False, "reason": "kill_switch"})
                record = executor.maybe_funding_exit(NOW)
            finally:
                module.maker_first_close = original
            self.assertEqual(record["closed"][0]["state"], "rejected")
            self.assertEqual(len(record["alerts"]), 1)
            self.assertTrue(any("资金费平仓未完成" in m for m in notifier.messages))

    def test_no_open_group_means_nothing_to_protect(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = self.executor(tmp, FakeFundingStack(), Recorder())
            record = executor.maybe_funding_exit(NOW)
            self.assertEqual(record["reason"], "no_open_group")


if __name__ == "__main__":
    unittest.main()
