import unittest
from decimal import Decimal

from arb_monitor.execution.intent import ExecutionIntent
from arb_monitor.execution.kill_switch import KillSwitch
from arb_monitor.execution.network import (
    NetworkFault, NetworkState, RemoteOrderState, RequestTracker, Resolution,
)
from arb_monitor.execution.recovery import (
    OrderRecovery, RecoveryState, recover_cancel, recover_cancel_loop,
)
from arb_monitor.execution.reconciliation import reconcile_balances
from arb_monitor.execution.ledger import ExecutionLedger
from arb_monitor.execution.risk import ExecutionLimits, PreTradeRisk
from arb_monitor.execution.state_machine import ExecutionCoordinator, ExecutionState
from arb_monitor.execution.adapters import Balance, InstrumentSpec
from arb_monitor.execution.paper import PaperCEXAdapter, PaperWalletAdapter


def make_intent(**updates):
    values = dict(
        signal_id="network-1", asset="wKUAIx", chain_id="196",
        token_address="0xt", quote_token_address="0xq", spot_amount=Decimal("10"),
        expected_token_quantity=Decimal("10"), perp_contract_quantity=Decimal("10"),
        perp_venue="okx", perp_instrument="KUAI-USDT-SWAP",
        net_edge_bps=Decimal("50"), expected_profit_quote=Decimal("1"),
        created_ts_ms=0, expires_ts_ms=99999, max_slippage_bps=Decimal("5"),
        gas_budget_quote=Decimal("1"), execution_allowed=True,
    )
    values.update(updates)
    return ExecutionIntent(**values)


class NetworkRecoveryTests(unittest.TestCase):
    def test_timeout_that_may_have_reached_remote_is_not_retried(self):
        tracker = RequestTracker()
        record = tracker.start("okx", "/trade/order", request_id="req-1")
        tracker.outcome(record.request_id, NetworkState.REQUEST_TIMEOUT,
                        may_have_reached_remote=True)
        self.assertFalse(tracker.retry_allowed("req-1"))

    def test_cancel_unknown_resolves_by_client_order_id(self):
        r = OrderRecovery("cid-1")
        r.cancel_failed()
        state = recover_cancel(
            r, lambda cid: Resolution(RemoteOrderState.FILLED, source="backup-read")
        )
        self.assertEqual(state, RecoveryState.FILLED_UNCONFIRMED)
        self.assertFalse(r.allows_new_orders)

    def test_cancel_unknown_escalates_after_bounded_attempts(self):
        r = OrderRecovery("cid-2", max_attempts=2)
        r.cancel_failed()
        recover_cancel(r, lambda cid: Resolution(RemoteOrderState.UNKNOWN))
        state = recover_cancel(r, lambda cid: Resolution(RemoteOrderState.UNKNOWN))
        self.assertEqual(state, RecoveryState.MANUAL_INTERVENTION_REQUIRED)

    def test_exposure_limit_rejects_new_order(self):
        limits = ExecutionLimits(max_asset_exposure=Decimal("10"))
        decision = PreTradeRisk(limits).check(
            make_intent(), 1, asset_exposure=Decimal("9")
        )
        self.assertFalse(decision.allowed)
        self.assertIn("asset_exposure_limit", decision.reasons)

    def test_reconciliation_gate_blocks_after_restart(self):
        spec = InstrumentSpec("KUAI-USDT-SWAP", Decimal("1"), Decimal("1"),
                              Decimal("1"), Decimal("0.1"), "USDT")
        cex = PaperCEXAdapter("okx", {spec.symbol: spec},
                              [Balance("USDT", Decimal("100"), Decimal("100"))])
        coordinator = ExecutionCoordinator(cex, PaperWalletAdapter(),
                                           PreTradeRisk(ExecutionLimits(
                                               max_signal_age_ms=99999)))
        coordinator.require_reconciliation()
        result = coordinator.execute_open(make_intent(), 1)
        self.assertEqual(result.state, ExecutionState.RECONCILIATION_REQUIRED)
        coordinator.reconcile_before_trading()
        self.assertEqual(coordinator.execute_open(make_intent(), 1).state,
                         ExecutionState.OPEN)

    def test_kill_switch_is_fail_closed_for_new_orders(self):
        switch = KillSwitch()
        switch.trip()
        with self.assertRaises(RuntimeError):
            switch.assert_allows_new_orders()

    def test_bounded_cancel_loop_escalates_without_resubmitting(self):
        recovery = OrderRecovery("cid-loop", max_attempts=2)
        recovery.cancel_failed()
        calls = []
        state = recover_cancel_loop(
            recovery,
            lambda cid: (calls.append(cid) or Resolution(RemoteOrderState.UNKNOWN)),
        )
        self.assertEqual(state, RecoveryState.MANUAL_INTERVENTION_REQUIRED)
        self.assertEqual(calls, ["cid-loop", "cid-loop"])

    def test_balance_reconciliation_detects_demo_account_drift(self):
        ok = reconcile_balances({"USDT": Decimal("100"), "USDG": Decimal("1")},
                                {"USDT": Decimal("100.0000001"), "USDG": Decimal("1")})
        bad = reconcile_balances({"USDT": Decimal("100")},
                                 {"USDT": Decimal("99.9")})
        self.assertTrue(ok.balanced)
        self.assertFalse(bad.balanced)

    def test_restart_recovery_blocks_unresolved_group(self):
        ledger = ExecutionLedger()
        ledger.append("g-restart", "open_unconfirmed", "network_fault",
                      {"signal_id": "g-restart"}, 1)
        spec = InstrumentSpec("KUAI-USDT-SWAP", Decimal("1"), Decimal("1"),
                              Decimal("1"), Decimal("0.1"), "USDT")
        coordinator = ExecutionCoordinator(
            PaperCEXAdapter("okx", {spec.symbol: spec}),
            PaperWalletAdapter(),
            PreTradeRisk(ExecutionLimits(max_signal_age_ms=99999)),
            ledger=ledger,
        )
        groups = coordinator.recover_from_ledger()
        self.assertIn("g-restart", groups)
        self.assertTrue(coordinator.recovery_required)

    def test_paper_timeout_after_first_leg_never_blindly_retries(self):
        spec = InstrumentSpec("KUAI-USDT-SWAP", Decimal("1"), Decimal("1"),
                              Decimal("1"), Decimal("0.1"), "USDT")
        cex = PaperCEXAdapter("okx", {spec.symbol: spec},
                              [Balance("USDT", Decimal("100"), Decimal("100"))],
                              response_lost_orders=True)
        coordinator = ExecutionCoordinator(
            cex, PaperWalletAdapter(),
            PreTradeRisk(ExecutionLimits(max_signal_age_ms=99999)))
        result = coordinator.execute_open(make_intent(), 1)
        self.assertEqual(result.state, ExecutionState.OPEN_UNCONFIRMED)
        self.assertEqual(len(cex.orders), 1)
        self.assertEqual(next(iter(cex.orders.values())).status, "filled")

    def test_pending_chain_is_not_recorded_as_open(self):
        spec = InstrumentSpec("KUAI-USDT-SWAP", Decimal("1"), Decimal("1"),
                              Decimal("1"), Decimal("0.1"), "USDT")
        cex = PaperCEXAdapter("okx", {spec.symbol: spec},
                              [Balance("USDT", Decimal("100"), Decimal("100"))])
        coordinator = ExecutionCoordinator(
            cex, PaperWalletAdapter(pending_swaps=True),
            PreTradeRisk(ExecutionLimits(max_signal_age_ms=99999)))
        result = coordinator.execute_open(make_intent(), 1)
        self.assertEqual(result.state, ExecutionState.PENDING_CHAIN)

    def test_paper_cancel_unknown_is_a_network_fault(self):
        spec = InstrumentSpec("KUAI-USDT-SWAP", Decimal("1"), Decimal("1"),
                              Decimal("1"), Decimal("0.1"), "USDT")
        cex = PaperCEXAdapter("okx", {spec.symbol: spec},
                              [Balance("USDT", Decimal("100"), Decimal("100"))],
                              cancel_unknown=True)
        order = cex.place_order(__import__(
            "arb_monitor.execution.adapters", fromlist=["OrderRequest"]
        ).OrderRequest("KUAI-USDT-SWAP", "sell", Decimal("1"), "ioc"))
        with self.assertRaises(NetworkFault):
            cex.cancel_order("KUAI-USDT-SWAP", order.order_id)


if __name__ == "__main__":
    unittest.main()
