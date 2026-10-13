"""The reference refresh must stop asking questions that are already answered.

Measured 2026-09-21 over 24 h: 8,002 `source_error` rows, of which 2,967 were
permanent 404s (a ticker that will never exist) and 3,966 were 429s from
GeckoTerminal. The refresh runs 46 curl calls every 300 s through the single
shared proxy, so a retry storm is not free: it competes with the sampling loop
for the same bandwidth.

These tests pin the three behaviours that fix it, and they are hermetic - the
curl runner is injected, no network is touched:

1. a permanent failure is remembered, not retried;
2. a rate-limited host goes on a cooldown that backs off;
3. a reused body is labelled as reused, never presented as a fresh sample.
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from arb_monitor.data.hk_reference import (
    _TRAILER, HTTPStatusError, PublicHTTP, PublicHTTPError, ReferenceAdapter,
)


class FakeCurl:
    """Counts invocations and replays a scripted sequence of outcomes."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[str] = []

    def __call__(self, args, timeout):
        self.calls.append(args[-1])
        index = min(len(self.calls) - 1, len(self.outcomes) - 1)
        outcome = self.outcomes[index]
        if isinstance(outcome, Exception):
            raise outcome
        code, stdout, stderr = outcome
        return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def body(payload: str = '{"data": []}') -> tuple[int, str, str]:
    """A successful response, in the shape `curl --write-out` really produces."""
    return status_response(200, payload)


def status_response(status: int, payload: str = "",
                    headers: dict | None = None) -> tuple[int, str, str]:
    """A response with an explicit HTTP status and headers.

    The real invocation carries these in a `--write-out` trailer, which is how
    a 429 is distinguishable from a 404 at all: through the local proxy both
    used to arrive as the same opaque curl exit code.
    """
    return 0, f"{payload}{_TRAILER}{status}\n{json.dumps(headers or {})}\n", ""


def curl_error(code: int, message: str) -> tuple[int, str, str]:
    return code, "", message


class Clock:
    def __init__(self, start: int = 1_000_000):
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms


class SilentJitter:
    """A `rand` that adds no jitter, for tests that assert exact cooldowns."""

    @staticmethod
    def __call__():
        return 0.5


class PublicHTTPMemoryTests(unittest.TestCase):
    URL = "https://query1.finance.yahoo.com/v8/finance/chart/625.HK"

    def test_404_is_classified_and_not_retried(self):
        curl = FakeCurl(status_response(404, '{"error": "not found"}'))
        http = PublicHTTP(runner=curl, clock=Clock())
        with self.assertRaises(HTTPStatusError) as caught:
            http.get(self.URL)
        self.assertEqual(caught.exception.status, 404)
        self.assertTrue(caught.exception.permanent)
        for _ in range(9):
            with self.assertRaises(PublicHTTPError):
                http.get(self.URL)
        self.assertEqual(len(curl.calls), 1)
        self.assertEqual(http.stats["suppressed"], 9)

    def test_a_curl_without_a_trailer_falls_back_to_its_stderr_status(self):
        # Older curl builds, and anything else that reports only an exit code,
        # still get classified instead of becoming an anonymous transport error.
        curl = FakeCurl(curl_error(56, "curl: (56) The requested URL returned error: 404"))
        http = PublicHTTP(runner=curl, clock=Clock())
        with self.assertRaises(HTTPStatusError) as caught:
            http.get(self.URL)
        self.assertEqual(caught.exception.status, 404)
        self.assertTrue(caught.exception.permanent)

    def test_permanent_failure_is_retried_only_after_the_ttl(self):
        clock = Clock()
        curl = FakeCurl(status_response(404))
        http = PublicHTTP(runner=curl, clock=clock, permanent_ttl_ms=60_000)
        with self.assertRaises(PublicHTTPError):
            http.get(self.URL)
        clock.advance(61_000)
        with self.assertRaises(PublicHTTPError):
            http.get(self.URL)
        self.assertEqual(len(curl.calls), 2)

    def test_rate_limited_host_cools_down_and_backs_off(self):
        clock = Clock()
        curl = FakeCurl(status_response(429))
        http = PublicHTTP(runner=curl, clock=clock, cooldown_ms=1_000,
                          cooldown_max_ms=8_000, jitter=0.0)
        # A different path on the *same* host: the limit belongs to the host.
        other = "https://query1.finance.yahoo.com/v8/finance/chart/0700.HK"
        with self.assertRaises(PublicHTTPError):
            http.get(self.URL)
        with self.assertRaises(PublicHTTPError):
            http.get(other)
        self.assertEqual(len(curl.calls), 1)
        clock.advance(1_001)
        with self.assertRaises(PublicHTTPError):
            http.get(other)
        clock.advance(2_001)
        with self.assertRaises(PublicHTTPError):
            http.get(other)
        self.assertEqual(len(curl.calls), 3)

    def test_transport_failure_without_a_status_also_cools_the_host(self):
        curl = FakeCurl(curl_error(28, "curl: (28) SSL connection timeout"))
        http = PublicHTTP(runner=curl, clock=Clock())
        with self.assertRaises(PublicHTTPError) as caught:
            http.get(self.URL)
        self.assertIn("public_http_failed:28", str(caught.exception))
        with self.assertRaises(PublicHTTPError):
            http.get(self.URL)
        self.assertEqual(len(curl.calls), 1)

    def test_success_is_cached_only_when_asked_and_is_labelled(self):
        clock = Clock()
        curl = FakeCurl(body('{"ok": 1}'))
        http = PublicHTTP(runner=curl, clock=clock)
        first = http.get(self.URL, max_age_ms=900_000)
        self.assertFalse(first["cache_hit"])
        self.assertEqual(first["cache_age_ms"], 0)
        clock.advance(120_000)
        second = http.get(self.URL, max_age_ms=900_000)
        self.assertTrue(second["cache_hit"])
        self.assertEqual(second["cache_age_ms"], 120_000)
        self.assertEqual(second["raw"], first["raw"])
        self.assertEqual(len(curl.calls), 1)
        clock.advance(900_001)
        third = http.get(self.URL, max_age_ms=900_000)
        self.assertFalse(third["cache_hit"])
        self.assertEqual(len(curl.calls), 2)

    def test_callers_that_do_not_ask_for_reuse_always_refetch(self):
        curl = FakeCurl(body())
        http = PublicHTTP(runner=curl, clock=Clock())
        http.get(self.URL)
        http.get(self.URL)
        self.assertEqual(len(curl.calls), 2)
        self.assertFalse(http.get(self.URL)["cache_hit"])

    def test_a_successful_read_clears_the_cooldown(self):
        clock = Clock()
        curl = FakeCurl(status_response(429),
                        body('{"data": []}'))
        http = PublicHTTP(runner=curl, clock=clock, cooldown_ms=1_000, jitter=0.0)
        with self.assertRaises(PublicHTTPError):
            http.get(self.URL)
        clock.advance(1_001)
        self.assertEqual(http.get(self.URL)["raw"], {"data": []})
        clock.advance(1_500)
        self.assertFalse(http.get(self.URL)["cache_hit"])
        self.assertEqual(len(curl.calls), 3)

    def test_host_health_says_what_failed_and_when_to_try_again(self):
        # The panel needs "429, 92 s left", not "5 data-source errors": a count
        # of rows cannot tell a working host from one that is being backed off.
        clock = Clock()
        curl = FakeCurl(status_response(429))
        http = PublicHTTP(runner=curl, clock=clock, cooldown_ms=1_000,
                          cooldown_max_ms=8_000, jitter=0.0)
        with self.assertRaises(PublicHTTPError):
            http.get(self.URL)
        host = "query1.finance.yahoo.com"
        health = http.host_health()[host]
        self.assertEqual(health["state"], "backoff")
        self.assertEqual(health["consecutive_failures"], 1)
        self.assertEqual(health["last_status"], "error")
        self.assertIn("429", health["last_reason"])
        self.assertEqual(health["next_retry_ms"], clock.now + 1_000)
        self.assertEqual(health["retry_in_ms"], 1_000)
        self.assertIsNone(health["last_success_ms"])
        # A second failure deepens the state and pushes the retry out.
        clock.advance(1_001)
        with self.assertRaises(PublicHTTPError):
            http.get(self.URL)
        health = http.host_health()[host]
        self.assertEqual(health["state"], "open")
        self.assertEqual(health["consecutive_failures"], 2)
        self.assertEqual(health["retry_in_ms"], 2_000)
        # Past the retry time with a failure streak: the next call is the probe.
        clock.advance(2_001)
        self.assertEqual(http.host_health()[host]["state"], "recovering")

    def test_host_health_returns_to_healthy_after_a_success(self):
        clock = Clock()
        curl = FakeCurl(status_response(429),
                        body('{"data": []}'))
        http = PublicHTTP(runner=curl, clock=clock, cooldown_ms=1_000, jitter=0.0)
        with self.assertRaises(PublicHTTPError):
            http.get(self.URL)
        clock.advance(1_001)
        http.get(self.URL)
        health = http.host_health()["query1.finance.yahoo.com"]
        self.assertEqual(health["state"], "healthy")
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertEqual(health["last_status"], "ok")
        self.assertEqual(health["retry_in_ms"], 0)
        self.assertIsNone(health["next_retry_ms"])
        self.assertEqual(health["last_success_ms"], clock.now)

    def test_a_suppressed_read_is_not_counted_as_a_new_failure(self):
        # The cooldown doing its job must not deepen the backoff, or one 429
        # would escalate to the maximum wait on its own.
        clock = Clock()
        curl = FakeCurl(status_response(429))
        http = PublicHTTP(runner=curl, clock=clock, cooldown_ms=1_000,
                          cooldown_max_ms=8_000, jitter=0.0)
        with self.assertRaises(PublicHTTPError):
            http.get(self.URL)
        for _ in range(5):
            with self.assertRaises(PublicHTTPError):
                http.get(self.URL)
        health = http.host_health()["query1.finance.yahoo.com"]
        self.assertEqual(health["consecutive_failures"], 1)
        self.assertEqual(health["suppressed"], 5)
        self.assertEqual(len(curl.calls), 1)
        self.assertEqual(health["next_retry_ms"], clock.now + 1_000)

    def test_a_retry_after_header_sets_the_cooldown(self):
        # The host's own number beats our guess: it is the only value that is
        # about *this* host's situation rather than about our backoff policy.
        clock = Clock()
        curl = FakeCurl(status_response(429, headers={"Retry-After": "30"}),
                        body())
        http = PublicHTTP(runner=curl, clock=clock, cooldown_ms=1_000,
                          cooldown_max_ms=900_000, jitter=0.0)
        with self.assertRaises(PublicHTTPError):
            http.get(self.URL)
        health = http.host_health()["query1.finance.yahoo.com"]
        self.assertEqual(health["next_retry_ms"], clock.now + 30_000)
        # Inside the header's window nothing is asked, however long the default
        # cooldown would have been.
        clock.advance(29_000)
        with self.assertRaises(PublicHTTPError):
            http.get(self.URL)
        self.assertEqual(len(curl.calls), 1)
        clock.advance(1_001)
        self.assertEqual(http.get(self.URL)["raw"], {"data": []})
        self.assertEqual(len(curl.calls), 2)

    def test_an_unusable_retry_after_falls_back_to_the_backoff(self):
        # An HTTP-date is legal but would make the cooldown depend on a clock we
        # do not control, so it is ignored rather than misread.
        clock = Clock()
        curl = FakeCurl(status_response(
            429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}))
        http = PublicHTTP(runner=curl, clock=clock, cooldown_ms=1_000,
                          cooldown_max_ms=8_000, jitter=0.0)
        with self.assertRaises(PublicHTTPError):
            http.get(self.URL)
        self.assertEqual(http.host_health()["query1.finance.yahoo.com"]
                         ["next_retry_ms"], clock.now + 1_000)

    def test_jitter_keeps_the_wait_inside_its_band(self):
        # Jitter exists so a refresh round does not re-synchronise with the
        # next; it must never turn a one-second cooldown into anything unbounded.
        # With a +/-25% band the two extremes are the endpoints and nothing else.
        for value, expected in ((0.0, 750), (0.5, 1_000), (1.0, 1_250)):
            clock = Clock()
            curl = FakeCurl(status_response(429))
            http = PublicHTTP(runner=curl, clock=clock, cooldown_ms=1_000,
                              cooldown_max_ms=8_000, jitter=0.25,
                              rand=lambda v=value: v)
            with self.assertRaises(PublicHTTPError):
                http.get(self.URL)
            wait = http.host_health()["query1.finance.yahoo.com"]["retry_in_ms"]
            self.assertEqual(wait, expected)

    def test_only_one_probe_is_sent_once_a_host_is_recovering(self):
        # After the cooldown expires the whole fan-out would otherwise fire at
        # once — which is exactly the burst that earned the 429. One request
        # probes; the rest wait for its answer.
        import threading

        clock = Clock()
        started = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def runner(args, timeout):
            calls.append(args[-1])
            if len(calls) == 3:
                # This is the probe: hold it open so a second caller arrives
                # while it is still unanswered.
                started.set()
                release.wait(2.0)
            return SimpleNamespace(returncode=0,
                                   stdout=status_response(429)[1], stderr="")

        http = PublicHTTP(runner=runner, clock=clock, cooldown_ms=1_000,
                          cooldown_max_ms=8_000, jitter=0.0)
        for _ in range(2):        # reach the failure streak
            with self.assertRaises(PublicHTTPError):
                http.get(self.URL)
            clock.advance(2_000)
        self.assertEqual(len(calls), 2)
        # The next call is admitted as the single probe; a caller arriving while
        # it is unanswered is refused and named as a suppression, not as a read
        # failure.
        errors: list[str] = []

        def probe():
            try:
                http.get(self.URL)
            except PublicHTTPError as exc:
                errors.append(str(exc))

        worker = threading.Thread(target=probe)
        worker.start()
        self.assertTrue(started.wait(2.0), "the probe never started")
        with self.assertRaises(PublicHTTPError) as caught:
            http.get(self.URL)
        self.assertIn("public_http_suppressed:recovering:", str(caught.exception))
        release.set()
        worker.join()
        self.assertTrue(errors, "the probe was admitted and answered")
        self.assertNotIn("public_http_suppressed", errors[0],
                         "the probe reached the host instead of being suppressed")
        self.assertEqual(len(calls), 3, "only the probe touched the network")


class HostConcurrencyTests(unittest.TestCase):
    """The cap is per host, and it blocks rather than refusing."""

    def test_no_more_than_the_cap_is_in_flight_per_host(self):
        import threading
        import time
        from arb_monitor.data.hk_reference import PublicHTTP

        peak = {"now": 0, "max": 0}
        guard = threading.Lock()

        def runner(args, timeout):
            with guard:
                peak["now"] += 1
                peak["max"] = max(peak["max"], peak["now"])
            time.sleep(0.02)
            with guard:
                peak["now"] -= 1
            return SimpleNamespace(returncode=0, stdout=body()[1], stderr="")

        http = PublicHTTP(runner=runner, max_per_host=2)
        threads = [threading.Thread(
            target=lambda index=i: http.get(f"https://example.test/{index}"))
            for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(peak["max"], 2)

    def test_a_different_host_gets_its_own_cap(self):
        import threading
        from arb_monitor.data.hk_reference import PublicHTTP

        seen = []
        guard = threading.Lock()
        release = threading.Event()

        def runner(args, timeout):
            with guard:
                seen.append(args[-1])
            release.wait(1.0)
            return SimpleNamespace(returncode=0, stdout=body()[1], stderr="")

        http = PublicHTTP(runner=runner, max_per_host=1)
        first = threading.Thread(
            target=lambda: http.get("https://one.test/a"))
        second = threading.Thread(
            target=lambda: http.get("https://two.test/b"))
        first.start()
        second.start()
        for _ in range(100):
            with guard:
                if len(seen) == 2:
                    break
            threading.Event().wait(0.01)
        release.set()
        first.join()
        second.join()
        self.assertEqual(sorted(seen), ["https://one.test/a", "https://two.test/b"])


class ReferenceAdapterTTLTests(unittest.TestCase):
    def test_hk_codes_are_quoted_with_four_digits(self):
        """SHEIN *is* listed in Hong Kong; ``625.HK`` is the wrong spelling.

        Measured 2026-09-21 through the local proxy: ``625.HK`` → 404 and
        ``0625.HK`` → 200 ("SHEIN-W", HKG); ``388.HK`` → 404 and ``0388.HK`` →
        200. Those two unpadded codes were all 2,967 of that day's 404s, and
        they are also why SHEIN had never once had a daily reference.
        """
        curl = FakeCurl(body('{"chart": {"result": []}}'))
        adapter = ReferenceAdapter(PublicHTTP(runner=curl, clock=Clock()))
        adapter.stock_daily({"asset": "SHEIN", "hk_code": "625", "market": "HK"})
        adapter.stock_daily({"asset": "HKEXCx", "hk_code": "388", "market": "HK"})
        adapter.stock_daily({"asset": "TENCENT", "hk_code": "0700", "market": "HK"})
        adapter.stock_daily({"asset": "AAPL", "stock_symbol": "AAPL",
                             "listing_country": "US"})
        self.assertTrue(curl.calls[0].endswith("/chart/0625.HK?range=1mo&interval=1d"))
        self.assertTrue(curl.calls[1].endswith("/chart/0388.HK?range=1mo&interval=1d"))
        self.assertTrue(curl.calls[2].endswith("/chart/0700.HK?range=1mo&interval=1d"))
        self.assertTrue(curl.calls[3].endswith("/chart/AAPL?range=1mo&interval=1d"))

    def test_daily_candles_are_reused_between_refreshes(self):
        clock = Clock()
        curl = FakeCurl(body('{"chart": {"result": []}}'))
        http = PublicHTTP(runner=curl, clock=clock)
        adapter = ReferenceAdapter(http, daily_ttl_ms=900_000)
        token = {"asset": "SHEIN", "hk_code": "625", "listing_country": "US"}
        first = adapter.stock_daily(token)
        second = adapter.stock_daily(token)
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(len(curl.calls), 1)
        self.assertFalse(adapter.hkd_daily()["cache_hit"])
        self.assertTrue(adapter.hkd_daily()["cache_hit"])
        self.assertEqual(len(curl.calls), 2)


if __name__ == "__main__":
    unittest.main()
