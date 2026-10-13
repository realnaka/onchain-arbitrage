"""Stage-0 instrumentation: the numbers a slow cycle has to be explained with.

Three things were impossible to answer from the recorded data before this:

* *where* a long cycle's wall clock went — the summary only carried
  ``evaluated_at_ms``, so the 1 322 s gap measured on 2026-09-21 could not be
  attributed to a phase;
* whether a slow quote grid was one asset or all of them — only the batch total
  was recorded;
* which failures were *events* and which were *state* — ``self.reference_errors``
  is repopulated on every refresh and re-reported on every cycle in between, so
  the event table's error counts were mostly the same handful of failures
  written over and over.

These tests pin the instrumentation, not any trading rule: none of it feeds a
candidate, a gate or an order.
"""
from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from arb_monitor.apps.hk_monitor import HKMonitor, load_config
from arb_monitor.core.hk import Contract, SpotQuote
from arb_monitor.core.models import Level, OrderBook

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/hk_monitor.toml"
NOW = 1_789_000_000_000


def contract(**kwargs):
    base = Contract(
        "gate", "XIAOMI_USDT", "XIAOMI", "hk_stock_candidate", "usd_linear",
        D("0.1"), D("1"), D("1"), D("5"), D(".001"), True, "public:test", NOW, {},
    )
    if not kwargs:
        return base
    fields = dict(base.__dict__)
    fields.update(kwargs)
    return Contract(**fields)


def spot(side="buy", status="ok", error="", error_code=""):
    return SpotQuote(
        asset="XIAOMI", side=side, chain_id="196",
        token_address="0x" + "1" * 40, quote_address="0x" + "2" * 40,
        quote_currency="USDC", requested_amount=D("1"), token_quantity=D("1"),
        quote_amount=D("3.42"), status=status, error_code=error_code, error=error,
        request_ts_ms=NOW, recv_ts_ms=NOW, expires_ts_ms=NOW + 10_000,
        source="fixture", raw={},
    )


def book():
    return OrderBook(
        "gate", "XIAOMI_USDT",
        (Level(D("3.40"), D("5000")),), (Level(D("3.41"), D("5000")),),
        NOW, NOW, 1, "fixture",
    )


class FakeHTTP:
    """The read-only host-health view a real ``PublicHTTP`` exposes."""

    def __init__(self, health=None):
        self.health = health or {}

    def host_health(self, now=None):
        return self.health


class FakeReferences:
    """A ``ReferenceAdapter`` stand-in that can be told to fail one kind.

    ``fail`` maps a reference kind to the message it raises, which is the shape
    ``refresh_references`` records: ``{"asset", "kind", "error"}``.
    """

    def __init__(self, fail=None, health=None, on_call=None):
        self.http = FakeHTTP(health)
        self.fail = dict(fail or {})
        self.attempts = []
        # Hook for tests that need to hold a round open (the reference worker
        # must let the cycle walk on while a round is still unanswered).
        self.on_call = on_call or (lambda kind, asset: None)

    def _answer(self, kind, asset):
        self.attempts.append((kind, asset))
        self.on_call(kind, asset)
        if kind in self.fail:
            raise RuntimeError(self.fail[kind])
        return {"source": "fixture", "raw": {}, "recv_ts_ms": NOW,
                "asset": asset, "price_usd": D("3.42")}

    def token(self, token):
        return self._answer("token_reference", token["asset"])

    def pool_spot(self, token):
        return self._answer("pool_reference", token["asset"])

    def stock_daily(self, token):
        return self._answer("stock_daily_reference", token["asset"])

    def hkd_daily(self):
        return self._answer("fx_daily_reference", "HKD")


class CountingQuotes:
    """A quote adapter that fails a configurable number of times, then answers."""

    def __init__(self, failures=0,
                 message="Network unavailable — check your connection"):
        self.failures = int(failures)
        self.message = message
        self.calls = 0

    def _next(self, side):
        self.calls += 1
        if self.calls <= self.failures:
            return spot(side=side, status="api_error", error=self.message)
        return spot(side=side)

    def quote(self, token, quote_token, side, amount):
        return self._next(side)

    def quote_exact_out(self, token, quote_token, shares):
        return self._next("buy")


class Harness:
    """One offline monitor: fixtures in, ``cycle()`` out, nothing on the wire."""

    def __init__(self, references=None, quotes=None, assets=("XIAOMI", "TENCENT"),
                 health=None, venues=("gate", "okx"),
                 contract_assets=("XIAOMI",), execution_dispatch=None):
        self.config = load_config(str(CONFIG))
        self.config["monitor"]["venues"] = list(venues)
        # Hermetic: no X Layer RPC, no background pool/trade scans. These tests
        # are about what a cycle records, not about the chain leg.
        self.config["onchain"]["enabled"] = False
        # Keep the retry backoff out of the test's wall clock.
        self.config["monitor"]["quote_backoff_ms"] = 1
        self.references = (references if references is not None
                           else FakeReferences(health=health))
        self.quotes = quotes if quotes is not None else CountingQuotes()
        self.assets = set(assets)
        self.contract_assets = tuple(contract_assets)
        if execution_dispatch:
            self.config.setdefault("execution_policy", {}).setdefault(
                "execution", {})["dispatch"] = execution_dispatch

        def client(venue, proxy):
            mock = MagicMock()
            mock.instruments.return_value = (
                [contract(venue=venue, asset=asset,
                          # The execution allowlist names OKX contracts the way
                          # the venue does ("XIAOMI-USDT-SWAP"); Gate's naming is
                          # different, which is exactly why the priority rule
                          # reads the allowlist instead of guessing from a ticker.
                          instrument_id=(f"{asset}-USDT-SWAP" if venue == "okx"
                                         else f"{asset}_USDT"))
                 for asset in self.contract_assets], {"raw": True}, NOW)
            mock.book.return_value = book()
            mock.funding.return_value = {
                "rate": D("0"), "next_funding_ts_ms": NOW + 50_000,
                "venue_ts_ms": NOW, "recv_ts_ms": NOW, "source": "fixture",
            }
            return mock

        self._client = client

    def __enter__(self):
        self._tmp = TemporaryDirectory()
        self._patches = [
            patch("arb_monitor.apps.hk_monitor.CexPublicAdapter", side_effect=self._client),
            patch("arb_monitor.apps.hk_monitor.ReferenceAdapter",
                  return_value=self.references),
            patch("arb_monitor.apps.hk_monitor.OnchainOSQuoteAdapter",
                  return_value=self.quotes),
            patch("arb_monitor.apps.hk_monitor.fetch_issuer_assets", return_value=[]),
            patch("arb_monitor.apps.hk_monitor.now_ms", return_value=NOW),
        ]
        for item in self._patches:
            item.start()
        # Hermetic: the executor reads an execution ledger, and it must not be
        # the live one. Points it at this test's own file.
        self.config["execution_ledger"] = str(Path(self._tmp.name) / "execution.db")
        self.monitor = HKMonitor(self.config,
                                 db=str(Path(self._tmp.name) / "monitor.db"))
        if self.assets:
            self.monitor.tokens = [t for t in self.monitor.tokens
                                   if t["asset"] in self.assets]
        return self

    def __exit__(self, *exc):
        try:
            self.monitor.close()
        finally:
            for item in reversed(self._patches):
                item.stop()
            self._tmp.cleanup()
        return False

    def cycles(self, count):
        return [self.monitor.cycle() for _ in range(count)]

    def source_errors(self):
        return self.monitor.store.events("source_error")


class TimingTests(unittest.TestCase):
    def test_a_cycle_reports_its_own_work_and_each_phase(self):
        with Harness() as h:
            summary = h.cycles(1)[0]
        self.assertEqual(summary["cycle_started_ms"], NOW)
        self.assertIsInstance(summary["cycle_work_ms"], int)
        self.assertGreaterEqual(summary["cycle_work_ms"], 0)
        # The spans that can hold a cycle: the two synchronous refresh calls, the
        # market batch, the read-and-evaluate work, and the executor calls.
        for name in ("discover_ms", "reference_ms", "onchain_ms", "token_scan_ms",
                     "gas_watch_ms", "market_batch_ms", "postprocess_ms",
                     "execution_ms"):
            self.assertIn(name, summary["phase_ms"], name)
            self.assertIsInstance(summary["phase_ms"][name], int)

    def test_the_gap_between_cycles_is_reported_not_inferred(self):
        with Harness() as h:
            first, second = h.cycles(2)
        # The first cycle has no predecessor to measure against.
        self.assertIsNone(first["cycle_gap_ms"])
        # `now_ms` is frozen in this harness, so the distance is exactly zero —
        # which is the point: the field is the wall clock, not a guess.
        self.assertEqual(second["cycle_gap_ms"], 0)


class QuoteMetricTests(unittest.TestCase):
    def test_first_and_last_success_are_separate_numbers(self):
        with Harness() as h:
            summary = h.cycles(1)[0]
        self.assertEqual(summary["quote_probe_jobs"], 1)
        self.assertEqual(summary["quote_probe_done"], 1)
        self.assertEqual(summary["quote_deadline_exceeded_count"], 0)
        self.assertIsNotNone(summary["quote_first_success_ms"])
        self.assertIsNotNone(summary["quote_last_success_ms"])
        self.assertLessEqual(summary["quote_first_success_ms"],
                             summary["quote_last_success_ms"])
        assets = summary["quote_assets"]
        self.assertEqual([row["asset"] for row in assets], ["XIAOMI"])
        self.assertEqual(assets[0]["final_status"], "ok")
        self.assertEqual(assets[0]["retry_count"], 0)
        self.assertIsNotNone(assets[0]["buy_ms"])
        self.assertIsNotNone(assets[0]["sell_ms"])
        # The batch total is unchanged: it still measures the arrival span.
        self.assertEqual(summary["quote_batch_ms"],
                         summary["quote_batch_elapsed_ms"])

    def test_coverage_is_stated_against_the_grid_that_was_issued(self):
        # Both assets are monitored, but only one has a perpetual to pair
        # against. The old denominator counted both, so a perfect grid read
        # "2/4"; coverage now means the grid that was actually sent.
        with Harness() as h:
            summary = h.cycles(1)[0]
        self.assertEqual(len(h.monitor.tokens), 2)
        self.assertEqual(summary["quote_quotable_assets"], ["XIAOMI"])
        self.assertEqual(summary["quote_probe_expected"], 2)
        self.assertEqual(summary["quote_probe_coverage"], "2/2")
        self.assertEqual(summary["quote_probe_ok"], 2)
        # The old number is still published, so a series does not change meaning.
        self.assertEqual(summary["quote_probe_coverage_all_tokens"], "2/4")

    def test_an_unreachable_proxy_is_not_counted_as_a_rate_limit(self):
        quotes = CountingQuotes(
            failures=1, message="Network unavailable — check your connection")
        with Harness(quotes=quotes) as h:
            summary = h.cycles(1)[0]
        self.assertEqual(summary["quote_api_error_retries"], 1)
        self.assertEqual(summary["quote_rate_limited"], 0)
        self.assertEqual(summary["quote_assets"][0]["retry_count"], 1)

    def test_a_real_rate_limit_is_counted_as_one(self):
        quotes = CountingQuotes(failures=1,
                                message="Rate limited — retry with backoff")
        with Harness(quotes=quotes) as h:
            summary = h.cycles(1)[0]
        self.assertEqual(summary["quote_api_error_retries"], 1)
        self.assertEqual(summary["quote_rate_limited"], 1)


class SourceErrorDedupeTests(unittest.TestCase):
    """A persistent failure is state. It is logged when it appears or changes."""

    def test_a_repeated_reference_failure_is_logged_once_not_once_per_cycle(self):
        refs = FakeReferences(fail={"token_reference": "PublicHTTPError:boom"})
        with Harness(references=refs) as h:
            summaries = h.cycles(3)
            rows = h.source_errors()
        failures = [row for row in rows if row.get("kind") == "token_reference"]
        # Two assets, one row each — not two rows per cycle for three cycles.
        self.assertEqual(len(failures), 2)
        self.assertEqual({row["asset"] for row in failures}, {"XIAOMI", "TENCENT"})
        # The dedupe is about the event table only: every cycle's summary still
        # carries the failure, because that is what the boards read.
        for summary in summaries:
            kinds = {row["kind"] for row in summary["source_errors"]}
            self.assertIn("token_reference", kinds)

    def test_a_failure_that_changes_is_logged_again(self):
        refs = FakeReferences(fail={"token_reference": "PublicHTTPError:429"})
        with Harness(references=refs) as h:
            h.cycles(1)
            after_first = len([r for r in h.source_errors()
                               if r.get("kind") == "token_reference"])
            # Force the next refresh: the clock is frozen in this harness.
            h.monitor.last_reference = 0
            refs.fail["token_reference"] = "PublicHTTPError:timeout"
            h.cycles(1)
            # From the second round on the refresh runs on the reference worker,
            # so the change is adopted on a later cycle.
            if h.monitor._reference_future is not None:
                h.monitor._reference_future.result(timeout=5)
            h.cycles(1)
            rows = [r for r in h.source_errors()
                    if r.get("kind") == "token_reference"]
        self.assertEqual(after_first, 2)
        self.assertEqual(len(rows), 4, "a changed failure is a new event")
        self.assertEqual({row["error"] for row in rows},
                         {"RuntimeError:PublicHTTPError:429",
                          "RuntimeError:PublicHTTPError:timeout"})

    def test_this_cycles_own_read_failure_is_recorded_every_time(self):
        # Unlike a cached reference failure, a perpetual-book read really is
        # attempted — and really fails — once per cycle, so every attempt is an
        # event. Suppressing these would erase "how often could we not price
        # this venue", which is the one number the scoreboard needs.
        with Harness(venues=("gate", "okx")) as h:
            for client in h.monitor.clients.values():
                client.book.side_effect = RuntimeError("book down")
            summaries = h.cycles(2)
            rows = [r for r in h.source_errors() if r.get("kind") == "depth"]
        # One contract per venue, two cycles, two venues.
        self.assertEqual(len(rows), 4)
        for summary in summaries:
            self.assertTrue(summary["source_errors"])


class ReferenceHostHealthTests(unittest.TestCase):
    def test_host_health_is_published_on_the_summary(self):
        health = {"api.geckoterminal.com": {
            "state": "backoff", "consecutive_failures": 1,
            "last_success_ms": NOW - 60_000, "last_failure_ms": NOW,
            "next_retry_ms": NOW + 92_000, "retry_in_ms": 92_000,
            "last_status": "error", "last_reason": "public_http_status:429",
            "suppressed": 3}}
        with Harness(health=health) as h:
            summary = h.cycles(1)[0]
        self.assertEqual(summary["reference_hosts"], health)
        self.assertEqual(summary["reference_tiers"]["execution_required"],
                         ["token_reference"])
        self.assertEqual(summary["reference_tiers"]["display_only"],
                         ["stock_daily_reference", "fx_daily_reference"])

    def test_a_stub_adapter_cannot_break_the_summary(self):
        # `references` is a stand-in in many tests; the diagnostics must degrade
        # to "no view", never to an exception in the middle of a cycle.
        stub = MagicMock()
        stub.http.host_health.side_effect = RuntimeError("no view")
        with Harness(references=stub) as h:
            summary = h.cycles(1)[0]
        self.assertEqual(summary["reference_hosts"], {})


if __name__ == "__main__":
    unittest.main()
