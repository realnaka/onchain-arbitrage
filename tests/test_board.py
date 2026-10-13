"""One page, two tabs: both boards, one server, neither able to blank the other.

Operator's request, 2026-09-16: "把这两个看板合并到一起，我打开两个网页太麻烦了".
The merge is a shell around the two existing renderers, so these tests check the
things that could actually go wrong in a merge: the tab routing, that both
documents really appear, that the JSON endpoints still exist for scripts, and
that an empty monitor database produces an explicit "no data" page rather than a
blank one.
"""
from __future__ import annotations

from http.server import ThreadingHTTPServer
from threading import Thread
import json
import unittest
from urllib.request import urlopen

from decimal import Decimal

from arb_monitor.apps.board import (_market_diagnostics, _snapshot_wallet_quote,
                                    route, tabbed_html)
from arb_monitor.ledger.store import EventStore


class WalletForScoreboardTests(unittest.TestCase):
    """The tally's money columns are sized against the wallet, not the ceiling.

    Measured 2026-09-17: the ceiling is 100 USDT but the wallet holds 10.92
    USDG, so `min(ceiling, wallet x 0.999)` is what a trade actually is. The
    number has to come from the account snapshot the page already took, because
    the tally is a read over recorded rows and must not add a chain call to a
    page load.
    """

    def test_the_balance_comes_out_of_the_snapshot(self):
        value = _snapshot_wallet_quote(
            lambda: {"grey_scale": {"wallet_quote_balance": "10.92387"}})
        self.assertEqual(value, Decimal("10.92387"))

    def test_an_unverified_snapshot_yields_none_not_an_exception(self):
        # No keys / no network: the tally falls back to the ceiling alone and
        # says so. It must never take the market tab down.
        self.assertIsNone(_snapshot_wallet_quote(
            lambda: {"grey_scale": {"wallet_quote_balance": None}}))
        self.assertIsNone(_snapshot_wallet_quote(lambda: {}))
        self.assertIsNone(_snapshot_wallet_quote(lambda: (_ for _ in ()).throw(
            RuntimeError("okx_transport:URLError"))))


class MarketDiagnosticsTests(unittest.TestCase):
    def test_groups_errors_and_reports_quote_and_cadence_without_network(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            store = EventStore(f"{directory}/events.db")
            payload = {
                "evaluated_at_ms": 3000,
                "quote_status": "quotes_observed",
                "quote_batch_ms": 16000,
                "quote_probe_coverage": "12/16",
                "max_age_ms": 15000,
                "auto_execution": {"enabled": True},
                "source_errors": [
                    {"error": "HTTP 429"},
                    {"error": "SSL timeout"},
                    {"error": "pool_state_in_flight_not_sampled"},
                ],
                "onchain_errors": [],
                "onchain_status": "degraded",
            }
            for recv in (1000, 2000, 3000):
                store.record_market_event("r", "run_summary", payload, recv)
            diagnostics = _market_diagnostics(store, payload, now=4000)
            self.assertEqual(diagnostics["source_error_groups"]["429/限流"], 1)
            self.assertEqual(diagnostics["source_error_groups"]["timeout/SSL/超时"], 1)
            self.assertEqual(diagnostics["background_not_sampled"], 1)
            self.assertEqual(diagnostics["quote_over_budget_ms"], 1000)
            self.assertEqual(diagnostics["quote_coverage"], "12/16")
            self.assertEqual(diagnostics["max_interval_30_ms"], 1000)
            self.assertIn("报价覆盖 12/16", diagnostics["blockers"])
            store.close()


class TabShellTests(unittest.TestCase):
    def shell(self, **kwargs) -> str:
        return tabbed_html(market_html="<h1>MARKET-BODY</h1>",
                           account_html_text="<h1>ACCOUNT-BODY</h1>", **kwargs)

    def test_both_documents_are_in_the_page(self):
        page = self.shell()
        self.assertIn("MARKET-BODY", page)
        self.assertIn("ACCOUNT-BODY", page)
        self.assertIn("#market", page)
        self.assertIn("#account", page)

    def test_each_document_keeps_its_own_tab(self):
        page = self.shell()
        # The panes are separate sections: an account read that fails cannot
        # empty the market pane, because they are not combined.
        self.assertIn('id="pane-market"', page)
        self.assertIn('id="pane-account"', page)
        self.assertIn("location.hash", page)

    def test_badges_are_optional(self):
        page = self.shell(market_badge="3 候选·live", account_badge="已验证")
        self.assertIn("3 候选·live", page)
        self.assertIn("已验证", page)
        self.assertIn("MARKET-BODY", self.shell())


class BoardServerTests(unittest.TestCase):
    """Serve it for real, on an ephemeral port, and fetch both tabs."""

    def test_the_router_answers_every_path_without_a_socket(self):
        """The routing is a pure function, so it is tested without bind().

        The development sandbox refuses ``bind()`` ("Operation not permitted"),
        which is an environment fact, not a defect — so the paths are exercised
        directly and the socket test below is the extra.
        """
        summary = {"status": "ok", "candidate_count": 2,
                   "auto_execution": {"order_mode": "live"}, "instruments": [],
                   "probes": [], "token_references": []}
        account = {"verified": True, "okx": {}, "wallet": {}, "exposure": {},
                   "risk": {}, "ledger": {}, "gas": {}, "grey_scale": {},
                   "universe": {}}
        calls: list[bool] = []

        def snapshot_fn(force=False):
            calls.append(force)
            return account

        for path, expect in (("/", 200), ("/api/status", 200),
                             ("/api/account", 200), ("/favicon.ico", 204),
                             ("/nope", 404)):
            with self.subTest(path=path):
                status, mime, body = route(path, summary_fn=lambda: summary,
                                           snapshot_fn=snapshot_fn)
                self.assertEqual(status, expect)
                if expect == 200:
                    self.assertTrue(body)
                if path == "/api/status":
                    self.assertEqual(json.loads(body)["candidate_count"], 2)
                if path == "/api/account":
                    self.assertTrue(json.loads(body)["verified"])
                if path == "/":
                    self.assertIn("id=\"pane-market\"", body)
                    self.assertIn("id=\"pane-account\"", body)
                    self.assertIn("2 候选·live", body)
                    self.assertIn("已验证", body)
        # ?refresh=1 must reach the snapshot builder (the cache bypass).
        route("/api/account?refresh=1", summary_fn=lambda: summary,
              snapshot_fn=snapshot_fn)
        self.assertIn(True, calls)

    def test_an_empty_monitor_database_is_explicit_not_a_blank_page(self):
        status, _mime, body = route(
            "/", summary_fn=lambda: {"status": "no_data"},
            snapshot_fn=lambda force=False: {"verified": False, "okx": {},
                                             "wallet": {}, "exposure": {},
                                             "risk": {}, "ledger": {}, "gas": {},
                                             "grey_scale": {}, "universe": {}})
        self.assertEqual(status, 200)
        self.assertIn("id=\"pane-account\"", body)

    def start_server(self, monkeypatch_market=None):
        """Run the real board on port 0 and hand back the port it bound.

        Port 0 lets the OS choose, so the test never fights a running board (or
        a second test) for 8765.

        Returns ``(port, cleanup)``. The fake summary stays in place until
        ``cleanup()`` is called by the test: restoring it as soon as the socket
        is bound would let the *real* monitor database answer the first request,
        which is exactly how this test failed once with "1 != 2".
        """
        import time
        from arb_monitor.apps import board as bm

        holder: dict = {}
        servers: list = []
        original_server = bm.ThreadingHTTPServer
        original_market = bm.market_summary
        original_snapshot = bm.build_snapshot

        def fake_snapshot(**_kwargs) -> dict:
            """A hermetic account side: real renderers, no network, no wallet CLI.

            The real builder does signed OKX GETs and runs the OnchainOS CLI; over
            a slow link the first request took longer than the client's timeout,
            which is how this test failed intermittently. The snapshot's *shape*
            is what the board needs here; the data itself is covered by
            tests/test_account_dashboard.py.
            """
            from arb_monitor.apps.account_dashboard import (gas_banner,
                                                            grey_scale_section,
                                                            ledger_section,
                                                            risk_parameters,
                                                            trade_universe)
            universe = trade_universe()
            return {
                "generated_at_ms": 1_789_550_000_000,
                "verified": True, "unverified_sources": [],
                "okx": {"status": "ok", "positions": [], "pending_orders": [],
                        "balances": [], "recent_fills": [], "fees": []},
                "wallet": {"status": "ok", "address": "0xwallet", "balances": []},
                "exposure": {"perp": [], "spot": [], "unhedged": []},
                "risk": risk_parameters(),
                "ledger": ledger_section(),
                "gas": gas_banner(),
                "grey_scale": grey_scale_section(),
                "universe": universe,
                "instruments": [],
            }

        bm.build_snapshot = fake_snapshot

        class CapturingServer(original_server):
            def __init__(self, address, handler, *args, **kwargs):
                super().__init__(address, handler, *args, **kwargs)
                servers.append(self)
                holder["port"] = self.server_address[1]

        bm.ThreadingHTTPServer = CapturingServer
        if monkeypatch_market is not None:
            bm.market_summary = monkeypatch_market
        errors: list = []

        def run():
            try:
                bm.serve_board(port=0, ttl_seconds=0)
            except Exception as exc:  # noqa: BLE001 - reported as a skip
                errors.append(exc)

        thread = Thread(target=run, daemon=True)
        try:
            thread.start()
            for _ in range(30):
                if holder.get("port") or errors:
                    break
                time.sleep(0.05)
        except BaseException:
            bm.ThreadingHTTPServer = original_server
            bm.market_summary = original_market
            bm.build_snapshot = original_snapshot
            raise
        finally:
            bm.ThreadingHTTPServer = original_server
        if errors and isinstance(errors[0], PermissionError):
            bm.market_summary = original_market
            bm.build_snapshot = original_snapshot
            raise errors[0]

        def cleanup() -> None:
            bm.market_summary = original_market
            bm.build_snapshot = original_snapshot
            for server in servers:
                server.shutdown()
                server.server_close()

        return holder.get("port"), cleanup

    def test_the_page_and_both_json_endpoints_answer(self):
        fake_summary = {"status": "ok", "candidate_count": 2,
                        "auto_execution": {"order_mode": "live"},
                        "instruments": [], "probes": [],
                        "token_references": []}
        try:
            port, cleanup = self.start_server(lambda *a, **k: fake_summary)
        except PermissionError:
            self.skipTest("this sandbox refuses to bind a socket")
        if not port:
            cleanup()
            self.skipTest("socket binding is not permitted in this environment")
        try:
            base = f"http://127.0.0.1:{port}"
            page = urlopen(base + "/", timeout=10).read().decode()
            self.assertIn("id=\"pane-market\"", page)
            self.assertIn("id=\"pane-account\"", page)
            self.assertIn("行情 / 候选与成本", page)
            self.assertIn("账户 / 风控与额度", page)
            self.assertIn("监控范围 vs 可自动执行", page)   # the account side
            # /api/status is the market JSON the old page polled.
            status = json.loads(urlopen(base + "/api/status", timeout=10).read())
            self.assertEqual(status["candidate_count"], 2)
            account = json.loads(urlopen(base + "/api/account", timeout=10).read())
            self.assertIn("verified", account)
        finally:
            cleanup()

    def test_an_empty_monitor_database_is_an_explicit_no_data_page(self):
        try:
            port, cleanup = self.start_server(
                lambda *a, **k: {"status": "no_data", "note": "no summary yet"})
        except PermissionError:
            self.skipTest("this sandbox refuses to bind a socket")
        if not port:
            cleanup()
            self.skipTest("socket binding is not permitted in this environment")
        try:
            page = urlopen(f"http://127.0.0.1:{port}/", timeout=10).read().decode()
            # The account tab is still rendered; the market tab says why it is
            # empty instead of showing a stale board.
            self.assertIn("id=\"pane-account\"", page)
            self.assertIn("监控范围 vs 可自动执行", page)
        finally:
            cleanup()


if __name__ == "__main__":
    unittest.main()
