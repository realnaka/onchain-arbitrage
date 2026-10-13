"""A transient signed-GET failure must not read as an unverifiable account.

Measured 2026-09-16: three consecutive `account-status` runs hit one
`okx_transport:URLError`. A single-shot read turns that into `verified=false`,
and the unattended executor refuses to act on an unverified account — so one
network blip becomes one skipped cycle. Reads are retryable under the project's
network policy; this pins that they actually are, and that a persistent failure
still surfaces as unknown rather than as an empty portfolio.
"""
from __future__ import annotations

import unittest
import urllib.error

import arb_monitor.execution.okx_account as okx_account
from arb_monitor.apps.account_dashboard import _read_with_retry, okx_section
from arb_monitor.execution.okx_account import OKXReadError, OKXReadOnlyClient


def client(transport, retries=0) -> OKXReadOnlyClient:
    return OKXReadOnlyClient(api_key="k", secret_key="s", passphrase="p",
                             transport=transport, read_retries=retries,
                             retry_backoff_seconds=0)


class ClientReadRetryTests(unittest.TestCase):
    """`[network] read_retries` has to reach the client the executor uses.

    The unattended executor reads positions every cycle (``open_contracts``).
    Measured twice — 2026-09-19 17:46 and 2026-09-21 06:47 — one
    ``okx_transport:URLError`` on that read stopped every automated entry until
    an operator reconciled it. The policy already allows bounded read retries
    and forbids write retries; this is the client honouring the first half.
    """

    def test_a_transient_transport_failure_is_retried(self):
        calls = {"n": 0}

        def flaky(url, headers):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("connection reset")
            return {"code": "0", "data": [{"pos": "3"}]}

        self.assertEqual(client(flaky, retries=2).positions(), [{"pos": "3"}])
        self.assertEqual(calls["n"], 3)

    def test_without_retries_one_blip_is_still_unknown(self):
        def dead(url, headers):
            raise OSError("connection reset")

        with self.assertRaises(OKXReadError) as caught:
            client(dead).positions()
        self.assertIn("okx_transport:OSError", str(caught.exception))

    def test_a_persistent_transport_failure_raises_after_the_bound(self):
        calls = {"n": 0}

        def dead(url, headers):
            calls["n"] += 1
            raise OSError("connection reset")

        with self.assertRaises(OKXReadError):
            client(dead, retries=2).positions()
        self.assertEqual(calls["n"], 3)          # bounded, not endless

    def test_an_http_status_is_an_answer_and_is_not_retried(self):
        calls = {"n": 0}

        def gone(url, headers):
            calls["n"] += 1
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)

        with self.assertRaises(OKXReadError) as caught:
            client(gone, retries=2).positions()
        self.assertEqual(calls["n"], 1)
        self.assertIn("okx_http_404", str(caught.exception))

    def test_a_read_falls_through_the_configured_routes(self):
        """`[network] routes` lists three ways out; the read should try them.

        Measured 2026-09-21: the shared proxy answered an OKX read with a
        URLError while the SOCKS and direct routes were configured and never
        used. A signed GET may change route (a write may not), and the attempt
        budget stays `read_retries + 1`.
        """
        seen: list[str | None] = []
        original = okx_account._http_get

        def fake(url, headers, timeout, proxy):
            seen.append(proxy)
            if proxy is not None:
                raise OSError("proxy down")
            return {"code": "0", "data": [{"pos": "1"}]}

        okx_account._http_get = fake
        try:
            routed = OKXReadOnlyClient(
                api_key="k", secret_key="s", passphrase="p",
                routes=("http://127.0.0.1:4780", "socks5://127.0.0.1:4781", None),
                read_retries=2, retry_backoff_seconds=0)
            self.assertEqual(routed.positions(), [{"pos": "1"}])
        finally:
            okx_account._http_get = original
        self.assertEqual(seen, ["http://127.0.0.1:4780",
                                "socks5://127.0.0.1:4781", None])


class RetryTests(unittest.TestCase):
    def test_a_transient_failure_is_retried(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("okx_transport:URLError")
            return [{"ccy": "USDT", "eq": "10"}]

        rows = _read_with_retry(flaky, attempts=3, backoff_seconds=0)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(rows, [{"ccy": "USDT", "eq": "10"}])

    def test_a_persistent_failure_still_raises(self):
        calls = {"n": 0}

        def dead():
            calls["n"] += 1
            raise RuntimeError("okx_transport:URLError")

        with self.assertRaises(RuntimeError):
            _read_with_retry(dead, attempts=3, backoff_seconds=0)
        self.assertEqual(calls["n"], 3)         # bounded: exactly three attempts

    def test_the_first_success_is_not_retried(self):
        calls = {"n": 0}

        def fine():
            calls["n"] += 1
            return []

        self.assertEqual(_read_with_retry(fine, attempts=3, backoff_seconds=0), [])
        self.assertEqual(calls["n"], 1)


class SectionTests(unittest.TestCase):
    class Client:
        simulated = False
        base_url = "https://www.okx.com"

        def __init__(self, fail_first=0):
            self.fail_first = fail_first
            self.calls = {"balances": 0, "positions": 0, "pending_orders": 0}

        def _maybe_fail(self, name):
            self.calls[name] += 1
            if self.calls[name] <= self.fail_first:
                raise OKXReadError("okx_transport:URLError")

        def balances(self, ccy=None):
            self._maybe_fail("balances")
            return [{"ccy": "USDT", "eq": "9.97", "availBal": "9.97"}]

        def positions(self, inst_type="SWAP"):
            self._maybe_fail("positions")
            return []

        def pending_orders(self, inst_type="SWAP"):
            self._maybe_fail("pending_orders")
            return []

        def fills(self, limit=20, **kwargs):
            return []

        def trade_fee(self, **kwargs):
            return []

    def test_one_blip_does_not_make_the_account_look_unreadable(self):
        section = okx_section(self.Client(fail_first=1), [])
        self.assertEqual(section["status"], "ok")
        self.assertEqual(section["pending_orders"], [])
        self.assertEqual(section["positions"], [])

    def test_a_dead_venue_is_reported_as_error_not_as_empty(self):
        class Dead(self.Client):
            def _maybe_fail(self, name):
                self.calls[name] += 1
                raise OKXReadError("okx_transport:URLError")

        section = okx_section(Dead(), [])
        self.assertEqual(section["status"], "error")
        self.assertIn("URLError", section["error"])
        # Crucially: no empty portfolio is invented.
        self.assertNotIn("pending_orders", section)


if __name__ == "__main__":
    unittest.main()
