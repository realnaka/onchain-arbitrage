import unittest
from decimal import Decimal

from arb_monitor.core.models import Intent, Leg, Side
from arb_monitor.execution.adapters import Balance, InstrumentSpec
from arb_monitor.execution.bridge import execution_intent_from_monitor
from arb_monitor.execution.ledger import ExecutionLedger
from arb_monitor.execution.paper import PaperCEXAdapter, PaperWalletAdapter
from arb_monitor.execution.risk import ExecutionLimits, PreTradeRisk
from arb_monitor.execution.state_machine import ExecutionCoordinator, ExecutionState


class FullChainTests(unittest.TestCase):
    def test_signal_to_risk_execution_ledger_and_close(self):
        signal = Intent(
            "hk", "XIAOMI",
            (Leg("onchain@196", "0x076cf393e701839fc7a5832d2c68aafa235682ae",
                 Side.BUY, Decimal("1"), Decimal("1"), Decimal("1")),
             Leg("okx", "XIAOMI-USDT-SWAP", Side.SELL,
                 Decimal("1"), Decimal("1"), Decimal("1"))),
            Decimal("100"), Decimal("1"), 99999, 0,
            {"execution_allowed": True, "quote_token_address": "0xquote",
             "signal_id": "full-chain-1"},
        )
        intent = execution_intent_from_monitor(signal)
        spec = InstrumentSpec("XIAOMI-USDT-SWAP", Decimal("1"), Decimal("1"),
                              Decimal("1"), Decimal("0.001"), "USDT")
        cex = PaperCEXAdapter("okx", {spec.symbol: spec},
                              [Balance("USDT", Decimal("100"), Decimal("100"))])
        ledger = ExecutionLedger()
        coordinator = ExecutionCoordinator(
            cex, PaperWalletAdapter(),
            PreTradeRisk(ExecutionLimits(max_signal_age_ms=99999,
                                         max_single_notional=Decimal("100"))),
            ledger=ledger,
        )
        opened = coordinator.execute_open(intent, 1)
        closed = coordinator.execute_close(intent, 2)
        self.assertEqual(opened.state, ExecutionState.OPEN)
        self.assertEqual(closed.state, ExecutionState.CLOSED)
        events = ledger.events("full-chain-1")
        self.assertTrue(any(e["event_type"] == "risk_decision" for e in events))
        self.assertEqual(ledger.last("full-chain-1")["state"], "closed")


if __name__ == "__main__":
    unittest.main()
