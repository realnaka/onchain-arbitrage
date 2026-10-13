import tempfile
import unittest
from decimal import Decimal

from arb_monitor.execution.adapters import InstrumentSpec, Balance
from arb_monitor.execution.intent import ExecutionIntent
from arb_monitor.execution.ledger import ExecutionLedger
from arb_monitor.execution.paper import PaperCEXAdapter, PaperWalletAdapter
from arb_monitor.execution.quote import ReadOnlySwapQuoteAdapter
from arb_monitor.execution.replay import PaperReplay, ReplayEvent
from arb_monitor.execution.fixtures import BINANCE_ORDER, OKX_ORDER
from arb_monitor.execution.parsers import (parse_binance_instrument, parse_okx_instrument,
                                           parse_binance_order, parse_okx_order)
from arb_monitor.execution.okx import OKXDemoClient, okx_signature
from arb_monitor.execution.network import NetworkFault, NetworkState, RemoteOrderState
from arb_monitor.execution.config import OKXConfig, load_env_file
from arb_monitor.execution.risk import ExecutionLimits, PreTradeRisk
from arb_monitor.execution.state_machine import ExecutionCoordinator, ExecutionState


def intent(**kw):
    base = dict(signal_id="g1", asset="wKUAIx", chain_id="196",
        token_address="0xt", quote_token_address="0qus", spot_amount=Decimal("1"),
        expected_token_quantity=Decimal("1"), perp_contract_quantity=Decimal("1"),
        perp_venue="okx", perp_instrument="KUAI-USDT-SWAP", net_edge_bps=Decimal("50"),
        expected_profit_quote=Decimal("1"), created_ts_ms=0, expires_ts_ms=999999,
        max_slippage_bps=Decimal("50"), gas_budget_quote=Decimal("1"),
        execution_allowed=True)
    base.update(kw)
    return ExecutionIntent(**base)


class ExecutionComponentsTests(unittest.TestCase):
    def setUp(self):
        spec = InstrumentSpec("KUAI-USDT-SWAP", Decimal("1"), Decimal("1"), Decimal("1"), Decimal("0.1"), "USDT")
        self.cex = PaperCEXAdapter("okx", {spec.symbol: spec}, [Balance("USDT", Decimal("100"), Decimal("100"))])
        self.wallet = PaperWalletAdapter()
        self.risk = PreTradeRisk(ExecutionLimits(max_signal_age_ms=999999, max_single_notional=Decimal("100")))

    def test_ledger_survives_reopen_and_recovers_last_state(self):
        with tempfile.NamedTemporaryFile() as f:
            ledger = ExecutionLedger(f.name)
            ledger.append("g1", "open", "opened", {"signal_id": "s"}, 1)
            ledger.close()
            ledger = ExecutionLedger(f.name)
            self.assertEqual(ledger.recover()["g1"]["state"], "open")

    def test_paper_open_close_and_ledger(self):
        ledger = ExecutionLedger()
        c = ExecutionCoordinator(self.cex, self.wallet, self.risk, ledger=ledger)
        i = intent()
        self.assertEqual(c.execute_open(i, 1).state, ExecutionState.OPEN)
        self.assertEqual(c.execute_close(i, 2).state, ExecutionState.CLOSED)
        self.assertEqual(ledger.last("g1")["state"], "closed")

    def test_partial_fill_is_detectable(self):
        self.cex.fill_ratio = Decimal("0.5")
        c = ExecutionCoordinator(self.cex, self.wallet, self.risk)
        self.assertEqual(c.execute_open(intent(), 1).state, ExecutionState.MANUAL_INTERVENTION_REQUIRED)
        self.assertEqual(self.cex.orders["paper-1"].status, "partially_filled")

    def test_readonly_quote_normalizes_decimal_and_does_not_execute(self):
        calls = []
        adapter = ReadOnlySwapQuoteAdapter(lambda **kw: (calls.append(kw) or {
            "action": "ok", "amount_out": "2.5", "amount_out_minimum": "2.4",
            "route": ["USDG", "wKUAIx"], "gas": 162000, "price_impact_bps": "23"
        }))
        q = adapter.quote("u", "t", Decimal("1"))
        self.assertEqual(q.amount_out, Decimal("2.5"))
        self.assertEqual(q.gas, 162000)
        self.assertEqual(len(calls), 1)

    def test_replay_is_deterministic_and_time_ordered(self):
        seen = []
        replay = PaperReplay([ReplayEvent(2, {"v": 2}), ReplayEvent(1, {"v": 1})],
                             lambda e: seen.append(e.payload["v"]) or e.ts_ms)
        self.assertEqual(replay.run(), [1, 2])
        self.assertEqual(seen, [1, 2])

    def test_api_fixtures_preserve_partial_fill_semantics(self):
        self.assertEqual(BINANCE_ORDER["status"], "PARTIALLY_FILLED")
        self.assertEqual(OKX_ORDER["data"][0]["state"], "partially-filled")

    def test_exchange_parsers_normalize_metadata_and_orders(self):
        from arb_monitor.execution.fixtures import BINANCE_EXCHANGE_INFO, OKX_INSTRUMENT
        b = parse_binance_instrument(BINANCE_EXCHANGE_INFO["symbols"][0])
        o = parse_okx_instrument(OKX_INSTRUMENT["data"][0])
        self.assertEqual((b.quantity_step, o.settle_currency), (Decimal("1"), "USDT"))
        self.assertEqual(parse_binance_order(BINANCE_ORDER).status, "partially_filled")
        self.assertEqual(parse_okx_order(OKX_ORDER).filled_quantity, Decimal("4"))

    def test_okx_signature_and_demo_header(self):
        captured = {}
        def transport(method, url, headers, payload):
            captured.update(method=method, url=url, headers=headers, payload=payload)
            return {"code": "0", "data": [{"ordId": "9", "sCode": "0"}]}
        client = OKXDemoClient("key", "secret", "pass", transport=transport)
        result = client.place_order(__import__("arb_monitor.execution.adapters", fromlist=["OrderRequest"]).OrderRequest(
            "XIAOMI-USDT-SWAP", "sell", Decimal("1"), "ioc", price=Decimal("10"), client_order_id="g1"))
        self.assertEqual(result.order_id, "9")
        self.assertEqual(result.filled_quantity, Decimal("0"))
        self.assertEqual(captured["headers"]["x-simulated-trading"], "1")
        self.assertTrue(captured["headers"]["OK-ACCESS-SIGN"])

    def test_okx_query_cancel_fills_and_config(self):
        calls = []
        def transport(method, url, headers, payload):
            calls.append((method, url, payload))
            if "/trade/order" in url and method == "GET":
                return {"code": "0", "data": [dict(OKX_ORDER["data"][0], ordId="9", accFillSz="1", avgPx="10")]}
            if "/trade/fills" in url:
                return {"code": "0", "data": [dict(OKX_ORDER["data"][0], ordId="9", accFillSz="1", avgPx="10")]}
            return {"code": "0", "data": [{"ordId": "9", "sCode": "0"}]}
        client = OKXDemoClient("k", "s", "p", transport=transport)
        self.assertEqual(client.get_order("X", "9").filled_quantity, Decimal("1"))
        self.assertEqual(len(client.get_fills("X", "9")), 1)
        client.cancel_order("X", "9")
        self.assertEqual(len(calls), 3)
        cfg = OKXConfig.from_env({"OKX_API_KEY": "k", "OKX_SECRET_KEY": "s", "OKX_PASSPHRASE": "p"})
        cfg.validate()

    def test_config_fails_closed_for_live_or_missing(self):
        with self.assertRaises(ValueError):
            OKXConfig.from_env({}).validate()
        with self.assertRaises(ValueError):
            OKXConfig("k", "s", "p", simulated=False).validate()

    def test_okx_client_order_id_and_fill_metadata(self):
        with self.assertRaises(ValueError):
            OKXDemoClient("k", "s", "p", transport=lambda *x: {}).place_order(
                __import__("arb_monitor.execution.adapters", fromlist=["OrderRequest"]).OrderRequest(
                    "X", "sell", Decimal("1"), "ioc", price=Decimal("1"), client_order_id="bad-id"))
        from arb_monitor.execution.parsers import parse_okx_order
        result = parse_okx_order({"data": [{"ordId": "1", "state": "filled",
            "accFillSz": "1", "avgPx": "3.4", "fee": "-0.0015", "feeCcy": "USDT",
            "execType": "T", "tradeId": "7", "fillPx": "3.4", "fillSz": "1"}]})
        self.assertEqual(result.fee, Decimal("0.0015"))
        self.assertEqual(result.metadata["execution_type"], "T")

    def test_okx_ambiguous_submit_is_not_retried_and_can_resolve_by_client_id(self):
        calls = []
        def transport(method, url, headers, payload):
            calls.append((method, url, payload))
            if method == "POST":
                raise TimeoutError("simulated timeout after send")
            return {"code": "0", "data": [{
                "ordId": "77", "clOrdId": "cid1", "state": "filled",
                "accFillSz": "1", "avgPx": "10", "fee": "0", "feeCcy": "USDT"
            }]}
        client = OKXDemoClient("k", "s", "p", transport=transport)
        with self.assertRaises(NetworkFault) as caught:
            client.place_order(__import__(
                "arb_monitor.execution.adapters", fromlist=["OrderRequest"]
            ).OrderRequest("X", "sell", Decimal("1"), "ioc",
                           price=Decimal("10"), client_order_id="cid1"))
        self.assertEqual(caught.exception.state, NetworkState.REQUEST_TIMEOUT)
        self.assertTrue(caught.exception.may_have_reached_remote)
        resolved = client.resolve_client_order("X", "cid1")
        self.assertEqual(resolved.state, RemoteOrderState.FILLED)
        self.assertEqual(resolved.value.filled_quantity, Decimal("1"))
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
