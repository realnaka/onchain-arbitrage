import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from arb_monitor.execution.adapters import Balance, InstrumentSpec, OrderResult
from arb_monitor.execution.cex import ReadOnlyCEXAdapter
from arb_monitor.execution.intent import ExecutionIntent
from arb_monitor.execution.kill_switch import KillSwitch
from arb_monitor.execution.reconciliation import reconcile
from arb_monitor.execution.risk import ExecutionLimits, PreTradeRisk
from arb_monitor.execution.state_machine import ExecutionCoordinator, ExecutionState
from arb_monitor.execution.wallet import SafeXLayerWallet
from arb_monitor.execution.bridge import execution_intent_from_monitor
from arb_monitor.core.models import Intent, Leg, Side


class FakeCEX(ReadOnlyCEXAdapter):
    def place_order(self, request):
        return OrderResult("p1", "filled", request.quantity, Decimal("10"))


class FakeWallet(SafeXLayerWallet):
    def quote_exact_in(self, token_in, token_out, amount_in):
        return {"amount_out_minimum": Decimal("9"), "route": ["router"]}

    def swap_exact_in(self, quote, amount_out_minimum):
        return OrderResult("0xabc", "confirmed", Decimal("9"), Decimal("1"))


def intent(**kwargs):
    base = dict(
        signal_id="s1", asset="wXIAOx", chain_id="196",
        token_address="0x076cf393e701839fc7a5832d2c68aafa235682ae",
        quote_token_address="0xb6ceceab302e2e4948951ee7843fc24e92933061",
        spot_amount=Decimal("100"), expected_token_quantity=Decimal("9"),
        perp_contract_quantity=Decimal("9"), perp_venue="binance",
        perp_instrument="XIAOMIUSDT", net_edge_bps=Decimal("100"),
        expected_profit_quote=Decimal("1"), created_ts_ms=1000,
        expires_ts_ms=5000, max_slippage_bps=Decimal("10"),
        gas_budget_quote=Decimal("1"), execution_allowed=True,
    )
    base.update(kwargs)
    return ExecutionIntent(**base)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.spec = InstrumentSpec("XIAOMIUSDT", Decimal("1"), Decimal("1"), Decimal("1"), Decimal("0.001"), "USDT")
        self.cex = FakeCEX("binance", {"XIAOMIUSDT": self.spec}, [Balance("USDT", Decimal("1000"), Decimal("1000"))], True)
        self.wallet = FakeWallet(allow_signing=True)

    def test_disabled_intent_is_rejected(self):
        c = ExecutionCoordinator(self.cex, self.wallet, PreTradeRisk())
        r = c.execute_open(intent(execution_allowed=False), 1100)
        self.assertEqual(r.state, ExecutionState.REJECTED)
        self.assertIn("intent_execution_disabled", r.reason)

    def test_expired_and_kill_switch(self):
        switch = KillSwitch()
        switch.trip()
        risk = PreTradeRisk(ExecutionLimits(), switch)
        d = risk.check(intent(), 1100)
        self.assertFalse(d.allowed)
        self.assertIn("kill_switch", d.reasons)

    def test_safe_default_adapters_do_not_trade(self):
        c = ReadOnlyCEXAdapter("binance", {"X": self.spec}, [], False)
        with self.assertRaises(PermissionError):
            c.place_order(None)
        with self.assertRaises(PermissionError):
            SafeXLayerWallet().swap_exact_in({}, Decimal("1"))

    def test_successful_paper_coordinator(self):
        c = ExecutionCoordinator(self.cex, self.wallet, PreTradeRisk())
        r = c.execute_open(intent(), 1100)
        self.assertEqual(r.state, ExecutionState.OPEN)
        self.assertEqual(r.perp.filled_quantity, Decimal("9"))

    def test_reconciliation(self):
        self.assertTrue(reconcile(Decimal("9"), Decimal("9"), Decimal("9"), Decimal("9")).balanced)
        self.assertFalse(reconcile(Decimal("9"), Decimal("8"), Decimal("9"), Decimal("9")).balanced)

    def test_kill_switch_file(self):
        with tempfile.TemporaryDirectory() as d:
            s = KillSwitch(Path(d) / "kill")
            s.trip()
            self.assertTrue(s.is_tripped())
            s.reset()
            self.assertFalse(s.is_tripped())

    def test_monitor_intent_bridge_preserves_safety_gate(self):
        i = Intent(
            "hk", "wXIAOx",
            (Leg("onchain@196", "0xtoken", Side.BUY, Decimal("10"), Decimal("9"), Decimal("90")),
             Leg("binance", "XIAOMIUSDT", Side.SELL, Decimal("10"), Decimal("9"), Decimal("90"))),
            Decimal("30"), Decimal("1"), 5000, 1000,
            {"execution_allowed": False, "quote_token_address": "0xquote"},
        )
        e = execution_intent_from_monitor(i)
        self.assertFalse(e.execution_allowed)
        self.assertEqual(e.chain_id, "196")

    def test_monitor_signal_triggers_paper_execution_only_when_allowed(self):
        monitor_intent = Intent(
            "hk", "wXIAOx",
            (Leg("onchain@196", "0xtoken", Side.BUY, Decimal("10"), Decimal("9"), Decimal("90")),
             Leg("binance", "XIAOMIUSDT", Side.SELL, Decimal("10"), Decimal("9"), Decimal("90"))),
            Decimal("30"), Decimal("1"), 5000, 1000,
            {"execution_allowed": True, "quote_token_address": "0xquote",
             "signal_id": "paper-signal-1"},
        )
        execution_intent = execution_intent_from_monitor(monitor_intent)
        coordinator = ExecutionCoordinator(self.cex, self.wallet, PreTradeRisk())
        result = coordinator.execute_open(execution_intent, 1100)
        self.assertEqual(result.state, ExecutionState.OPEN)
        self.assertEqual(result.perp.order_id, "p1")
        self.assertEqual(result.spot.order_id, "0xabc")


if __name__ == "__main__":
    unittest.main()
