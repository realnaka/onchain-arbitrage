"""The quote grid has to run concurrently, and still fail closed.

Each probe is a CLI round trip. Issued one after another the grid took about
70 seconds, during which the order books fetched alongside it aged out of the
pairing budget, so every evaluation was rejected as stale. These tests pin the
concurrency, the ordering, and the circuit breaker that stops a rejecting
channel from being hammered once per asset.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import threading
import time
import unittest

from arb_monitor.apps.hk_monitor import HKMonitor, load_config
from arb_monitor.core.hk import SpotQuote

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/hk_monitor.toml"


def quote(asset, side, amount, status="ok", error_code=""):
    return SpotQuote(
        asset=asset, side=side, chain_id="196",
        token_address=f"0xtoken{asset.lower()}", quote_address="0xquote",
        quote_currency="USDC", requested_amount=Decimal(str(amount)),
        request_ts_ms=1, recv_ts_ms=1, expires_ts_ms=2,
        source="test", raw={}, token_quantity=Decimal(str(amount)) if side == "buy" else Decimal("1"),
        quote_amount=Decimal(str(amount)), status=status, error_code=error_code,
    )


class FakeQuotes:
    def __init__(self, delay=0.0, auth_after=None, rate_limit_times=0):
        self.delay = delay
        self.auth_after = auth_after
        self.rate_limit_times = rate_limit_times
        self.calls = []
        self.lock = threading.Lock()
        # Observed overlap, which is what "the probes run concurrently" really
        # means. Measured here instead of inferred from wall-clock time: a busy
        # machine stretches the clock without changing the concurrency, and that
        # is exactly how this test used to fail for no reason.
        self.in_flight = 0
        self.max_in_flight = 0

    def quote(self, token, quote_token, side, amount):
        with self.lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        with self.lock:
            self.calls.append((token["asset"], side, str(amount)))
            count = len(self.calls)
        try:
            return self._answer(token, side, amount, count)
        finally:
            with self.lock:
                self.in_flight -= 1

    def _answer(self, token, side, amount, count):
        if self.delay:
            time.sleep(self.delay)
        if count <= self.rate_limit_times:
            broken = quote(token["asset"], side, amount, status="api_error")
            return SpotQuote(**{**broken.__dict__, "error": "Rate limited — retry with backoff"})
        if self.auth_after is not None and count > self.auth_after:
            return quote(token["asset"], side, amount, status="auth_error",
                         error_code="50114")
        return quote(token["asset"], side, amount)

    def quote_exact_out(self, token, quote_token, shares):
        """Buy side: deliver exactly this many shares, bounded spend."""
        with self.lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        with self.lock:
            self.calls.append((token["asset"], "buy-exact", str(shares)))
            count = len(self.calls)
        try:
            if self.delay:
                time.sleep(self.delay)
            if count <= self.rate_limit_times:
                broken = quote(token["asset"], "buy", shares, status="api_error")
                return SpotQuote(**{**broken.__dict__,
                                    "error": "Rate limited — retry with backoff"})
            if self.auth_after is not None and count > self.auth_after:
                return quote(token["asset"], "buy", shares, status="auth_error",
                             error_code="50114")
            record = quote(token["asset"], "buy", shares)
            # exact-out fixes the share count; the cash side is what it costs.
            return SpotQuote(**{**record.__dict__, "token_quantity": Decimal(str(shares)),
                                "quote_amount": Decimal(str(shares)) * Decimal("3.42")})
        finally:
            with self.lock:
                self.in_flight -= 1


def monitor(quotes, sizes=None, workers=8, backoff_ms=None):
    config = load_config(str(CONFIG))
    if sizes is not None:
        config["monitor"]["probe_shares"] = sizes
    config["monitor"]["quote_workers"] = workers
    if backoff_ms is not None:
        config["monitor"]["quote_backoff_ms"] = backoff_ms
    instance = HKMonitor(config, proxy=None, db=":memory:")
    instance.quotes = quotes
    instance.tokens = [t for t in instance.tokens if t["asset"] in ("XIAOMI", "TENCENT")]
    return instance


class QuoteBatchTests(unittest.TestCase):
    def test_probes_run_concurrently_instead_of_one_at_a_time(self):
        quotes = FakeQuotes(delay=0.2)
        mon = monitor(quotes, sizes=["1", "5"], workers=8)
        try:
            started = time.monotonic()
            pairs, records = mon._quote_batch(True)
            elapsed = time.monotonic() - started
        finally:
            mon.close()
        # 2 assets x 2 share counts x (buy+sell) = 8 calls at 0.2s each: sequential
        # would be 1.6s, concurrent should be well under half of that.
        self.assertEqual(len(records), 8)
        # Observed overlap is the real assertion: at least two probes were in
        # flight at the same moment. A wall-clock bound stays as a sanity check
        # with room for a loaded machine (sequential would be 1.6s).
        self.assertGreaterEqual(quotes.max_in_flight, 2,
                                "probes were serialised, not overlapped")
        self.assertLess(elapsed, 1.5, f"batch did not overlap ({elapsed:.2f}s)")

    def test_every_asset_and_size_pair_gets_a_buy_and_a_sell(self):
        quotes = FakeQuotes()
        mon = monitor(quotes, sizes=["1", "5"], workers=4)
        try:
            pairs, records = mon._quote_batch(True)
        finally:
            mon.close()
        self.assertEqual(sorted(pairs), [("TENCENT", "1"), ("TENCENT", "5"),
                                         ("XIAOMI", "1"), ("XIAOMI", "5")])
        for asset, size in pairs:
            buy, sell = pairs[(asset, size)]
            self.assertEqual(buy.side, "buy")
            self.assertEqual(sell.side, "sell")
            # The buy leg is exact: the share count is what the hedge matches.
            self.assertEqual(buy.token_quantity, Decimal(size))
        # Records follow the canonical order, not completion order.
        self.assertEqual([(r.asset, r.side) for r in records],
                         [("XIAOMI", "buy"), ("XIAOMI", "sell"),
                          ("XIAOMI", "buy"), ("XIAOMI", "sell"),
                          ("TENCENT", "buy"), ("TENCENT", "sell"),
                          ("TENCENT", "buy"), ("TENCENT", "sell")])

    def test_an_auth_error_stops_the_rest_of_the_grid(self):
        quotes = FakeQuotes(auth_after=2)
        mon = monitor(quotes, sizes=["1", "5", "25"], workers=1)
        try:
            pairs, records = mon._quote_batch(True)
        finally:
            mon.close()
        # One worker keeps the ordering deterministic: after the first two
        # rejected calls nothing else is attempted.
        self.assertLessEqual(len(quotes.calls), 3)
        self.assertTrue(any(r.status == "auth_error" for r in records))

    def test_disabled_quotes_produce_no_calls(self):
        quotes = FakeQuotes()
        mon = monitor(quotes, sizes=["1"], workers=4)
        try:
            pairs, records = mon._quote_batch(False)
        finally:
            mon.close()
        self.assertEqual(quotes.calls, [])
        self.assertEqual(records, [])
        self.assertEqual(pairs, {("XIAOMI", "1"): (None, None),
                                 ("TENCENT", "1"): (None, None)})

    def test_batch_duration_is_recorded_for_the_summary(self):
        quotes = FakeQuotes(delay=0.05)
        mon = monitor(quotes, sizes=["1"], workers=2)
        try:
            mon._quote_batch(True)
            recorded = mon.last_quote_batch_ms
        finally:
            mon.close()
        self.assertGreater(recorded, 0)

    def test_a_rate_limited_probe_is_retried_instead_of_lost(self):
        quotes = FakeQuotes(rate_limit_times=2)
        mon = monitor(quotes, sizes=["1"], workers=1)
        try:
            pairs, records = mon._quote_batch(True)
            limited = mon.quote_rate_limited
        finally:
            mon.close()
        self.assertEqual(limited, 2)
        # The two rejected attempts were retried and still produced quotes.
        self.assertEqual(len(records), 4)
        self.assertTrue(all(record.status == "ok" for record in records))

    def test_retries_are_bounded(self):
        quotes = FakeQuotes(rate_limit_times=99)
        mon = monitor(quotes, sizes=["1"], workers=1, backoff_ms=1)
        try:
            pairs, records = mon._quote_batch(True)
        finally:
            mon.close()
        # 2 assets x (1 buy + 1 sell), each attempted at most 1 + quote_retries.
        retries = int(load_config(str(CONFIG))["monitor"].get("quote_retries", 2))
        self.assertLessEqual(len(quotes.calls), 2 * 2 * (retries + 1))
        self.assertTrue(any(record.status == "api_error" for record in records))

    def test_transient_network_errors_are_retried_but_auth_errors_are_not(self):
        class NetworkThenOk(FakeQuotes):
            def quote(self, token, quote_token, side, amount):
                with self.lock:
                    self.calls.append((token["asset"], side, str(amount)))
                    count = len(self.calls)
                if count == 1:
                    broken = quote(token["asset"], side, amount, status="api_error")
                    return SpotQuote(**{**broken.__dict__,
                                        "error": "Network unavailable — check your connection"})
                return quote(token["asset"], side, amount)

        quotes = NetworkThenOk()
        mon = monitor(quotes, sizes=["1"], workers=1, backoff_ms=1)
        try:
            _pairs, records = mon._quote_batch(True)
        finally:
            mon.close()
        self.assertTrue(all(record.status == "ok" for record in records))

        auth = FakeQuotes(auth_after=0)
        mon = monitor(auth, sizes=["1"], workers=1, backoff_ms=1)
        try:
            _pairs, records = mon._quote_batch(True)
        finally:
            mon.close()
        # A rejected channel is not retried once per probe.
        self.assertEqual(len(auth.calls), 1)
        self.assertTrue(all(record.status == "auth_error" for record in records))


class ConfigurationTests(unittest.TestCase):
    def test_probes_are_whole_share_counts(self):
        config = load_config(str(CONFIG))
        shares = config["monitor"]["probe_shares"]
        # Legacy list only. The live probe is each contract's minSz, not this.
        self.assertTrue(config["monitor"]["probe_from_contract_minimum"])
        for value in shares:
            self.assertEqual(Decimal(value), Decimal(value).to_integral_value())
        self.assertEqual(shares[0], "1")
        self.assertLessEqual(Decimal(shares[-1]) * Decimal("4"), Decimal("100"))

    def test_concurrency_is_configured(self):
        config = load_config(str(CONFIG))
        workers = int(config["monitor"]["quote_workers"])
        # More than one, so the grid overlaps; not so many that the venue's
        # rate limiter answers instead of a price.
        self.assertGreater(workers, 1)
        self.assertLessEqual(workers, 12)


if __name__ == "__main__":
    unittest.main()
