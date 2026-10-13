"""A venue answer is not a dead route.

Measured 2026-09-16: under a burst of quote probes the venue answered
"Rate limited — retry with backoff" and the CLI exited 1. The runner treated a
non-zero exit as "this route failed", tried the SOCKS route and then direct,
both of which failed for real network reasons, and finally reported
`all_routes_failed` — hiding the rate limit that the caller needs to back off
from. The two cases must be told apart.
"""
from __future__ import annotations

import unittest

from arb_monitor.execution.network_policy import NetworkRoute
from arb_monitor.execution.wallet_cli import OnchainOSRunner, WalletCliError, is_route_failure


class RouteFailureHeuristicTests(unittest.TestCase):
    def test_transport_problems_are_route_problems(self):
        for text in ("Network unavailable — check your connection and try again",
                     "Failed to fetch token decimals for 0xabc: Network unavailable",
                     "dial tcp: i/o timeout"):
            self.assertTrue(is_route_failure(text), text)

    def test_a_venue_answer_is_not_a_route_problem(self):
        for text in ("Rate limited — retry with backoff",
                     "API error (code=82112): the value difference ...",
                     "insufficient balance"):
            self.assertFalse(is_route_failure(text), text)


class RunnerTests(unittest.TestCase):
    def route(self, name="r1"):
        return (NetworkRoute(name, None, timeout_seconds=1),)

    def runner(self, results):
        calls = {"n": 0}

        def run(command, capture_output=True, text=True, timeout=30, env=None):
            calls["n"] += 1
            code, stdout = results[min(calls["n"] - 1, len(results) - 1)]
            class Done:
                returncode = code
                stderr = ""
            Done.stdout = stdout
            return Done()

        return OnchainOSRunner(binary="/bin/true", routes=self.route(), run=run,
                               environ={}), calls

    def test_a_rate_limit_is_returned_instead_of_falling_through_routes(self):
        runner, calls = self.runner([
            (1, '{"ok":false,"error":"Rate limited — retry with backoff"}')])
        result = runner(("swap", "quote", "--from", "0xa"))
        self.assertFalse(result["ok"])
        self.assertIn("Rate limited", result["error"])
        self.assertEqual(calls["n"], 1)

    def test_a_real_network_failure_still_falls_through(self):
        runner, calls = self.runner([
            (1, '{"ok":false,"error":"Network unavailable — check your connection"}'),
            (0, '{"ok":true,"data":[]}')])
        runner.routes = (NetworkRoute("r1", None, 1), NetworkRoute("r2", None, 1))
        result = runner(("swap", "quote", "--from", "0xa"))
        self.assertTrue(result["ok"])
        self.assertEqual(calls["n"], 2)

    def test_every_route_failing_is_still_an_error(self):
        runner, _calls = self.runner([
            (1, '{"ok":false,"error":"Network unavailable"}')])
        with self.assertRaises(WalletCliError):
            runner(("swap", "quote", "--from", "0xa"))


if __name__ == "__main__":
    unittest.main()
