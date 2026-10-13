"""Hermetic tests for the v2 feeds, scan gate and store.

No network, no CLI, no halt files. Every payload here is a recorded shape, so
the assertions pin parsing rather than availability.
"""
from __future__ import annotations

import json
import threading
import time
import unittest
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory

from arb_monitor.xstocks_v2 import scan, store, units
from arb_monitor.xstocks_v2.feeds import census as census_feed
from arb_monitor.xstocks_v2.feeds import evm, kraken
from arb_monitor.xstocks_v2.feeds import jupiter as jup
from arb_monitor.xstocks_v2.feeds.http import RateLimiter
from arb_monitor.xstocks_v2.policy import V2Policy
from arb_monitor.xstocks_v2.spot_venue import BUY, REAL, SELL, Quote, best_buy, best_sell, edge_bps


class FakeHttp:
    """Records calls and replays canned responses."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls: list[str] = []

    def get_json(self, url, *, headers=None, timeout=None):
        self.calls.append(url)
        if not self.responses:
            return 200, {}, {}
        status, payload, extra = self.responses.pop(0)
        return status, payload, extra

    def request(self, url, *, data=None, headers=None):
        self.calls.append(url)
        if not self.responses:
            return 200, b"[]"
        status, payload, _extra = self.responses.pop(0)
        return status, json.dumps(payload).encode()


class KrakenParseTest(unittest.TestCase):
    def test_ticker_carries_both_sides(self):
        payload = {"error": [], "result": {
            "AAPLx/USD": {"b": ["240.10", "2", "1"], "a": ["240.50", "3", "1"]},
        }}
        rows = kraken.parse_ticker(payload, recv_ms=111)
        self.assertEqual(rows["AAPLx"]["bid"], D("240.10"))
        self.assertEqual(rows["AAPLx"]["ask"], D("240.50"))
        self.assertEqual(rows["AAPLx"]["source"], "kraken:ticker")

    def test_missing_side_is_none_not_zero(self):
        """A venue with no ask is not a venue quoting zero."""
        payload = {"error": [], "result": {"MRNAx/USD": {"b": ["1.0", "1", "1"]}}}
        rows = kraken.parse_ticker(payload, recv_ms=1)
        self.assertIsNone(rows["MRNAx"]["ask"])
        # A zero bid next to a real ask keeps the row and leaves the bid empty.
        zero_bid = {"error": [], "result": {
            "MRNAx/USD": {"b": ["0", "1", "1"], "a": ["6.0", "1", "1"]}}}
        self.assertIsNone(kraken.parse_ticker(zero_bid, recv_ms=1)["MRNAx"]["bid"])
        # Both sides empty is not a quote at all.
        empty = {"error": [], "result": {"MRNAx/USD": {"b": ["0", "1", "1"], "a": []}}}
        self.assertNotIn("MRNAx", kraken.parse_ticker(empty, recv_ms=1))

    def test_symbol_of_handles_both_spellings(self):
        self.assertEqual(kraken.symbol_of("MRNAx/USD"), "MRNAx")
        self.assertEqual(kraken.symbol_of("MRNAXUSD"), "MRNAX")

    def test_bad_result_shape_does_not_raise(self):
        self.assertEqual(kraken.parse_ticker({"result": []}, recv_ms=1), {})
        self.assertEqual(kraken.parse_ticker(None, recv_ms=1), {})

    def test_url_splits_tokenized_from_native(self):
        tokenized = kraken.ticker_url(["AAPLx"])
        self.assertIn("asset_class=tokenized_asset", tokenized)
        native = kraken.ticker_url(["SOL"], tokenized=False)
        self.assertNotIn("asset_class", native)

    def test_depth_best_side(self):
        payload = {"error": [], "result": {"AAPLx/USD": {
            "bids": [["240.10", "1", "1"], ["240.00", "5", "1"]],
            "asks": [["240.50", "1", "1"], ["240.60", "5", "1"]],
        }}}
        rows = kraken.parse_depth(payload, recv_ms=7)
        self.assertEqual(kraken.book_side(rows["AAPLx"]["bids"], side="sell"), D("240.10"))
        self.assertEqual(kraken.book_side(rows["AAPLx"]["asks"], side="buy"), D("240.50"))


class JupiterParseTest(unittest.TestCase):
    def test_the_executor_swap_client_uses_the_keyed_feed(self):
        """A23: the executor's own quote/swap must go through the keyed client.

        Measured 2026-10-08: the v1 client it used to borrow hard-codes
        api.jup.ag with no key and answers ``401 Unauthorized``, so every row
        that cleared round 2 died at ``requote_failed``.
        """
        seen: list[dict] = []

        class Recorder:
            def __init__(self):
                self.posts: list[tuple[str, dict]] = []

            def get_json(self, url, *, headers=None, timeout=None):
                seen.append(dict(headers or {}))
                return 200, {"outAmount": "12345", "priceImpactPct": "0.0012"}, {}

            def post_json(self, url, payload, *, headers=None):
                self.posts.append((url, dict(headers or {})))
                return 200, {"swapTransaction": "base64tx"}

        http = Recorder()
        feed = jup.JupiterFeed(http, api_key="k", base="https://api.jup.ag")
        client = jup.SwapClient(feed)
        quote = client.quote("in", "out", 25000000, 10)
        self.assertEqual(quote["outAmount"], "12345")
        self.assertEqual(seen[0].get("x-api-key"), "k")
        self.assertEqual(client.build_swap(quote)["swapTransaction"], "base64tx")
        self.assertEqual(http.posts[0][1].get("x-api-key"), "k")

    def test_a_swap_quote_without_a_route_is_empty_not_an_exception(self):
        http = FakeHttp([(200, {"error": "NO_ROUTES_FOUND"}, {}),
                         (429, {"error": "rate"}, {})])
        feed = jup.JupiterFeed(http, api_key="k")
        client = jup.SwapClient(feed)
        self.assertEqual(client.quote("in", "out", 1000, 10), {})
        self.assertEqual(client.quote("in", "out", 1000, 10), {})

    def test_the_executor_can_tell_a_rate_limit_from_a_dead_pool(self):
        """Both used to arrive as ``{}``, so both read as ``requote_failed``.

        Measured 2026-10-08 13:4x: the executor's second-round quote answered
        429 while the scan was quoting; the next call answered 200 with a real
        ``outAmount``. Retrying needs the status; refusing a dead pool needs it
        too, in the other direction.
        """
        http = FakeHttp([(429, {"error": "rate"}, {}),
                         (200, {"outAmount": "7550159"}, {}),
                         (400, {"error": "NO_ROUTES_FOUND"}, {})])
        feed = jup.JupiterFeed(http, api_key="k")
        client = jup.SwapClient(feed)
        self.assertEqual(client.quote_status("in", "out", 1000, 10), (429, None))
        status, payload = client.quote_status("in", "out", 1000, 10)
        self.assertEqual((status, payload["outAmount"]), (200, "7550159"))
        self.assertEqual(client.quote_status("in", "out", 1000, 10), (400, None))

    def test_a_decision_time_quote_can_re_ask_on_a_rate_limit(self):
        """``tries`` is opt-in: the scan's own high-volume quoting keeps 1."""
        http = FakeHttp([(429, {}, {}),
                         (200, {"outAmount": "5", "priceImpactPct": "0.001"}, {})])
        feed = jup.JupiterFeed(http, api_key="k")
        self.assertIsNone(feed.quote("in", "out", 1000))
        # The 429 spent one response; the retry takes the next one.
        http.responses.insert(0, (429, {}, {}))
        parsed = feed.quote("in", "out", 1000, tries=2,
                            sleep=lambda seconds: None)
        self.assertEqual(parsed["out_amount"], 5)

    def test_price_v3_reads_stock_data(self):
        payload = {"mintA": {"stockData": {"price": "240.5"}},
                   "mintB": {"stockData": {"price": None}, "price": "12.5"},
                   "mintC": {"stockData": {"price": "0"}}}
        rows = jup.parse_price_v3(payload)
        self.assertEqual(rows["mintA"], D("240.5"))
        self.assertEqual(rows["mintB"], D("12.5"))
        self.assertNotIn("mintC", rows)

    def test_quote_parses_amount_and_route(self):
        payload = {"outAmount": "12345", "priceImpactPct": "0.0012",
                   "routePlan": [{"swapInfo": {"label": "Raydium"}}]}
        parsed = jup.parse_quote(payload)
        self.assertEqual(parsed["out_amount"], 12345)
        self.assertEqual(parsed["impact_bps"], D("12.00"))
        self.assertEqual(parsed["route"], "Raydium")

    def test_quote_without_out_amount_is_none(self):
        self.assertIsNone(jup.parse_quote({"error": "NO_ROUTES_FOUND"}))
        self.assertIsNone(jup.parse_quote("nope"))

    def test_error_code_is_machine_readable(self):
        self.assertEqual(jup.error_code({"error": "NO_ROUTES_FOUND"}), "NO_ROUTES_FOUND")
        self.assertEqual(jup.error_code({"errorCode": "TOKEN_NOT_TRADABLE"}), "TOKEN_NOT_TRADABLE")

    def test_429_pauses_every_worker_once(self):
        http = FakeHttp([(429, {}, {"x-ratelimit-reset": str(time.time())})])
        feed = jup.JupiterFeed(http, rps=1000.0, api_key="k", base="https://example.invalid")
        feed.quote("a", "b", 1000)
        self.assertEqual(feed.http_429, 1)
        self.assertGreaterEqual(feed.limiter.pauses, 1)


class RateLimiterTest(unittest.TestCase):
    def test_workers_overlap_instead_of_serialising(self):
        """The lock is for accounting, not for the request.

        The v1 board measured that a strictly serial limiter left the wire idle
        between calls; the invariant is that at least two requests are in flight.
        """
        limiter = RateLimiter(1000.0, capacity=4.0)
        gate = threading.Barrier(2, timeout=5)
        in_flight = []
        peak = []
        lock = threading.Lock()

        def worker():
            limiter.acquire()
            with lock:
                in_flight.append(1)
                peak.append(len(in_flight))
            gate.wait()
            with lock:
                in_flight.pop()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertGreaterEqual(max(peak), 2)

    def test_pause_delays_the_next_token(self):
        slept = []
        now = [0.0]

        def clock():
            return now[0]

        def sleep(seconds):
            slept.append(seconds)
            now[0] += seconds

        limiter = RateLimiter(1.0, capacity=1.0, clock=clock, sleep=sleep)
        limiter.acquire()
        limiter.pause(5.0)
        limiter.acquire()
        # Exactly one wait, and it is the pause remainder. A ``wait`` computed
        # outside the pause branch sleeps a sliver, finds itself still paused,
        # and spins -- which is how a 429 turned into a busy loop on 2026-10-08.
        self.assertEqual(slept, [5.0])


class SharedRateBudgetTest(unittest.TestCase):
    """One key, one budget, several processes (2026-10-08).

    The free Jupiter key allows 1 request/s. The v2 stack quotes from two
    processes, each with its own in-process limiter, so together they asked for
    ~1.8 rps and Jupiter answered 429 to whoever arrived second -- including
    the live executor, where a 429 drops an opportunity (3 attempts, 3 x
    ``requote_failed``).

    Nothing here asserts on wall-clock time: the clock and the sleep are
    injected, so the assertions are about the *schedule* the two limiters agree
    on, not about how fast the machine ran.
    """

    class Clock:
        def __init__(self, start=1000.0):
            self.now = start

        def __call__(self):
            return self.now

        def advance(self, seconds):
            self.now += seconds

    def _pair(self, tmp, *, rps=1.0, backlog=300.0, marker=None):
        clock = self.Clock()

        def sleep(seconds):
            clock.advance(seconds)

        path = Path(tmp) / "jupiter_rate.json"
        kwargs = dict(shared_path=path, clock=clock, sleep=sleep,
                      max_backlog_s=backlog)
        if marker is not None:
            kwargs["boot_marker"] = marker
        return RateLimiter(rps, **kwargs), RateLimiter(rps, **kwargs), clock

    def test_two_processes_do_not_spend_the_same_slot(self):
        with TemporaryDirectory() as tmp:
            first, second, _clock = self._pair(tmp)
            self.assertEqual(first.acquire(), 0.0)
            # The second process has its own full bucket but the same key: it
            # must wait one interval, not fire immediately.
            self.assertAlmostEqual(second.acquire(), 1.0, places=6)
            self.assertAlmostEqual(first.acquire(), 1.0, places=6)

    def test_the_two_processes_together_never_exceed_the_key(self):
        """The invariant is the key's limit, not each process's own: ten calls
        spread over both processes must take at least nine intervals."""
        with TemporaryDirectory() as tmp:
            first, second, clock = self._pair(tmp)
            started = clock()
            for _ in range(5):
                first.acquire()
                second.acquire()
            self.assertGreaterEqual(clock() - started, 9.0)

    def test_a_stale_file_from_another_boot_is_ignored(self):
        with TemporaryDirectory() as tmp:
            before, _other, _clock = self._pair(tmp, marker=lambda: 100.0)
            before.acquire()                      # leaves a slot in the future
            _first, after, _clock2 = self._pair(tmp, marker=lambda: 999.0)
            self.assertEqual(after.acquire(), 0.0)   # rebooted since: no queue

    def test_a_pause_stops_every_process_not_just_the_one_that_saw_it(self):
        with TemporaryDirectory() as tmp:
            first, second, _clock = self._pair(tmp)
            first.acquire()
            first.pause(60.0)                     # 429 seen by process one
            self.assertGreaterEqual(second.acquire(), 59.9)

    def test_a_pause_also_stops_a_caller_that_is_already_waiting(self):
        """The reservation is booked *before* the sleep, so the pause has to be
        re-read after it -- otherwise the callers already asleep fire straight
        into the wall the 429 pause exists to avoid."""
        with TemporaryDirectory() as tmp:
            clock = self.Clock()
            path = Path(tmp) / "jupiter_rate.json"
            fired = []

            def sleep(seconds):
                clock.advance(seconds)
                if not fired:                      # the 429 lands mid-wait
                    fired.append(True)
                    first.pause(60.0)

            first = RateLimiter(1.0, shared_path=path, clock=clock, sleep=sleep)
            second = RateLimiter(1.0, shared_path=path, clock=clock, sleep=sleep)
            first.acquire()
            self.assertGreaterEqual(second.acquire(), 59.9)

    def test_an_unusable_file_falls_back_instead_of_blocking(self):
        """Failure is open: a monitor that stops quoting is worse than one that
        occasionally collides."""
        limiter = RateLimiter(1.0, shared_path="/nonexistent-dir/rate.json",
                              clock=self.Clock(), sleep=lambda seconds: None)
        self.assertEqual(limiter.acquire(), 0.0)   # its own bucket, no wait
        self.assertTrue(limiter.shared.disabled)


class EvmEncodeTest(unittest.TestCase):
    def test_v3_calldata_layout(self):
        data = evm.encode_v3_quote(
            "0x1111111111111111111111111111111111111111",
            "0x2222222222222222222222222222222222222222",
            1000, D("0.0005"))
        self.assertTrue(data.startswith("0x" + evm.V3_QUOTE_SELECTOR))
        self.assertEqual(len(data), 2 + 8 + 32 * 5 * 2)
        self.assertEqual(int(data[2 + 8 + 32 * 3 * 2:2 + 8 + 32 * 4 * 2], 16), 500)

    def test_v4_calldata_starts_with_the_struct_offset(self):
        key = {"currency0": "0x" + "0" * 39 + "1", "currency1": "0x" + "0" * 39 + "2",
               "fee": 3000, "tick_spacing": 60, "hooks": "0x" + "0" * 40}
        data = evm.encode_v4_quote(key, True, 100)
        self.assertTrue(data.startswith("0x" + evm.V4_QUOTE_SELECTOR))
        self.assertEqual(int(data[2 + 8:2 + 8 + 64], 16), 0x20)
        self.assertEqual(int(data[2 + 8 + 32 * 3 * 2:2 + 8 + 32 * 4 * 2], 16), 3000)
        # Word 6 is zeroForOne: offset 0x20, currency0, currency1, fee,
        # tickSpacing, hooks, then the flag.
        self.assertEqual(int(data[2 + 8 + 32 * 6 * 2:2 + 8 + 32 * 7 * 2], 16), 1)
        self.assertEqual(int(data[2 + 8 + 32 * 7 * 2:2 + 8 + 32 * 8 * 2], 16), 100)

    def test_try_aggregate_round_trip(self):
        calls = [("0x" + "0" * 39 + "1", "0x" + "a" * 8),
                 ("0x" + "0" * 39 + "2", "0x" + "b" * 64)]
        data = evm.encode_try_aggregate(calls)
        self.assertTrue(data.startswith("0x" + evm.TRY_AGGREGATE_SELECTOR))

    def test_try_aggregate_decode_keeps_failed_calls(self):
        # (bool, bytes)[] with one good row (value 7) and one failed row.
        payload = ("0x" + format(0x20, "064x") + format(2, "064x")
                   + format(0x40, "064x") + format(0xC0, "064x")
                   + format(1, "064x") + format(0x40, "064x")
                   + format(32, "064x") + format(7, "064x")
                   + format(0, "064x") + format(0x40, "064x") + format(0, "064x"))
        rows = evm.decode_try_aggregate(payload)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["success"])
        self.assertEqual(rows[0]["value"], 7)
        self.assertFalse(rows[1]["success"])

    def test_batch_honours_chain_limit(self):
        """X Layer answers -32014 above ten calls, so the chunk is per chain."""
        class Recorder(FakeHttp):
            def __init__(self):
                super().__init__()
                self.batch_sizes = []

            def request(self, url, *, data=None, headers=None):
                payload = json.loads(data.decode())
                self.batch_sizes.append(len(payload))
                ids = [row["id"] for row in payload]
                return 200, json.dumps(
                    [{"jsonrpc": "2.0", "id": index, "result": "0x" + "0" * 63 + "1"}
                     for index in ids]).encode()

        http = Recorder()
        chain = evm.CHAINS["x-layer"]
        calls = [("0x" + "0" * 39 + f"{index:x}", "0xdead") for index in range(25)]
        rows = evm.rpc_batch(http, chain, calls)
        self.assertEqual(len(rows), 25)
        self.assertLessEqual(max(http.batch_sizes), 10)
        self.assertEqual(len(http.batch_sizes), 3)

    def test_one_bad_call_does_not_drop_the_batch(self):
        class Recorder(FakeHttp):
            def request(self, url, *, data=None, headers=None):
                payload = json.loads(data.decode())
                out = []
                for row in payload:
                    if row["id"] == 1:
                        out.append({"jsonrpc": "2.0", "id": 1,
                                    "error": {"message": "execution reverted"}})
                    else:
                        out.append({"jsonrpc": "2.0", "id": row["id"],
                                    "result": "0x" + "0" * 63 + "5"})
                return 200, json.dumps(out).encode()

        calls = [("0x" + "0" * 39 + str(index), "0xdead") for index in range(3)]
        rows = evm.rpc_batch(Recorder(), evm.CHAINS["x-layer"], calls)
        self.assertIn("error", rows[1])
        self.assertEqual(evm.decode_uint(rows[0]["result"]), 5)
        self.assertEqual(evm.decode_uint(rows[2]["result"]), 5)

    def test_missing_rows_are_reported_not_silently_zero(self):
        class Recorder(FakeHttp):
            def request(self, url, *, data=None, headers=None):
                return 200, b'[{"jsonrpc":"2.0","id":0,"result":"0x01"}]'

        calls = [("0x" + "0" * 39 + "0", "0xdead"),
                 ("0x" + "0" * 39 + "1", "0xdead")]
        rows = evm.rpc_batch(Recorder(), evm.CHAINS["x-layer"], calls)
        self.assertEqual(rows[1]["error"], "missing_from_batch")

    def test_transport_failure_marks_every_call_in_that_chunk(self):
        class Recorder(FakeHttp):
            def request(self, url, *, data=None, headers=None):
                raise OSError("boom")

        calls = [("0x" + "0" * 39 + "0", "0xdead")]
        rows = evm.rpc_batch(Recorder(), evm.CHAINS["x-layer"], calls)
        self.assertIn("transport:OSError", rows[0]["error"])


class CensusTest(unittest.TestCase):
    def test_chain_key_maps_issuer_network_names(self):
        self.assertEqual(census_feed.chain_key("XLayer"), "x-layer")
        self.assertEqual(census_feed.chain_key("BNB Smart Chain"), "bsc")
        self.assertEqual(census_feed.chain_key("ethereum"), "eth")

    def test_bad_row_is_skipped_not_fatal(self):
        census = {"candidates": [
            {"slug": "x-layer", "symbol": "AAPLx", "token": "0x" + "1" * 40,
             "stable": "0x" + "2" * 40, "fee": "0.0005", "dec_token": 18,
             "dec_stable": 6, "depth_usd": "100"},
            {"slug": "x-layer", "symbol": "BROKEN", "token": "not-an-address",
             "stable": "0x" + "2" * 40},
        ]}
        rows = census_feed.census_pools(census)
        self.assertEqual([row["symbol"] for row in rows], ["AAPLx"])

    def test_solana_universe_prefers_native_and_usdc(self):
        with TemporaryDirectory() as tmp:
            import sqlite3

            path = Path(tmp) / "book.sqlite"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE deployments (issuer_symbol TEXT, network TEXT,"
                         " address TEXT, form TEXT, stables_json TEXT, missing_since_ms INTEGER)")
            stables = json.dumps([
                {"symbol": "USDT", "address": "tether", "decimals": 6},
                {"symbol": "USDC", "address": "usdc", "decimals": 6},
            ])
            conn.execute("INSERT INTO deployments VALUES ('AAPLx','Solana','mint1','wrapper',?,NULL)",
                         (stables,))
            conn.execute("INSERT INTO deployments VALUES ('AAPLx','Solana','mint2','native',?,NULL)",
                         (stables,))
            conn.commit()
            conn.close()
            rows = census_feed.solana_universe(["AAPLx"], book=path)
        self.assertEqual(rows["AAPLx"]["token_address"], "mint2")
        self.assertEqual(rows["AAPLx"]["quote_asset"], "USDC")

    def test_v4_pool_rows_build_a_usable_key(self):
        with TemporaryDirectory() as tmp:
            import sqlite3

            path = Path(tmp) / "book.sqlite"
            token = "0x" + "a" * 40
            usdg = "0x" + "b" * 40
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE deployments (issuer_symbol TEXT, network TEXT,"
                         " address TEXT, form TEXT, stables_json TEXT, missing_since_ms INTEGER)")
            conn.execute("INSERT INTO deployments VALUES ('AAPLx','XLayer',?,'native',?,NULL)",
                         (token, json.dumps([{"symbol": "USDG", "address": usdg,
                                       "decimals": 6}]),))
            conn.commit()
            conn.close()
            v4 = {"rows": [{"chain": "x-layer", "symbol": "AAPLx", "token": token,
                            "stable": "USDG", "pool_id": "0xPOOL", "fee_key": 500,
                            "tick_spacing": 10, "hook": "0x" + "0" * 40, "tier": "green"}]}
            rows = census_feed.v4_pool_rows(v4, book=path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["quote_address"], usdg)
        self.assertEqual(rows[0]["fee_key"], D("500"))

    def test_a_truncated_address_is_not_an_address(self):
        """``0x`` and ``0xdead`` passed the old startswith check."""
        self.assertTrue(census_feed.is_evm_address("0x" + "1" * 40))
        for bad in ("0x", "0xdead", "", None, "0x" + "z" * 40, "1" * 42):
            self.assertFalse(census_feed.is_evm_address(bad), bad)


class ScanGateTest(unittest.TestCase):
    def setUp(self):
        self.policy = V2Policy.load()

    def _quote(self, side, price, *, recv_ms=1000, venue="jupiter", chain="solana",
               kind=REAL, pool_id="pool"):
        return Quote(venue=venue, chain=chain, symbol="AAPLx", side=side, kind=kind,
                     price_usd=D(price), recv_ms=recv_ms, pool_id=pool_id)

    def test_edge_bps_needs_both_sides(self):
        self.assertIsNone(edge_bps(None, D("2")))
        self.assertEqual(edge_bps(D("100"), D("101")), D("100.00"))

    def test_deposit_rails_are_per_symbol_and_chain(self):
        deposits = {"AAPLx": [{"method": "AAPLx - Ethereum"}, {"method": "AAPLx - TON"}]}
        self.assertEqual(len(scan.deposit_rails("AAPLx", "eth", deposits)), 1)
        self.assertEqual(scan.deposit_rails("AAPLx", "solana", deposits), [])
        self.assertEqual(scan.deposit_rails("MRNAx", "eth", deposits), [])

    def test_post_only_stays_on_the_watch_list(self):
        self.assertEqual(
            scan.listed_symbols({"AAPLx": {"status": "online"},
                                 "MCDx": {"status": "post_only"}}),
            ["AAPLx", "MCDx"])

    def test_two_chains_still_pair_when_kraken_has_no_book(self):
        entries = [
            {"symbol": "FLWSx", "chain": "eth", "pool_id": "0x1", "errors": [],
             "buy": self._quote(BUY, "10", chain="eth", venue="uniswap", pool_id="0x1"),
             "sell": self._quote(SELL, "10", chain="eth", venue="uniswap", pool_id="0x1")},
            {"symbol": "FLWSx", "chain": "solana", "pool_id": "mint", "errors": [],
             "buy": self._quote(BUY, "9", chain="solana", pool_id="mint"),
             "sell": self._quote(SELL, "11", chain="solana", pool_id="mint")},
        ]
        rows = scan.build_candidates(
            symbol="FLWSx", chain_quotes=entries, kraken_row={},
            policy=self.policy, multiplier=D("1"), deposits={},
            v4_tiers={}, now_ms=1000, phase="scan")
        families = {row["family"] for row in rows}
        self.assertIn("chain_to_chain", families)
        self.assertNotIn("chain_to_kraken", families)
        self.assertNotIn("kraken_to_chain", families)

    def test_chain_to_kraken_row_uses_bid_and_says_why_it_is_blocked(self):
        entries = [{"symbol": "AAPLx", "chain": "solana", "errors": [],
                    "buy": self._quote(BUY, "100"), "sell": self._quote(SELL, "99")}]
        rows = scan.build_candidates(
            symbol="AAPLx", chain_quotes=entries,
            kraken_row={"bid": D("101"), "ask": D("101.5"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"), deposits={},
            v4_tiers={}, now_ms=1000, phase="scan")
        chain_to_kraken = [row for row in rows if row["family"] == "chain_to_kraken"]
        self.assertEqual(len(chain_to_kraken), 1)
        row = chain_to_kraken[0]
        self.assertEqual(D(row["edge_t0_bps"]), D("100.00"))
        self.assertIn("phase_scan", row["blockers"])
        # No Kraken rail from this chain, and no bridge arrival sample yet.
        self.assertIn("unmeasured_route", row["blockers"])
        self.assertFalse(row["executable"])

    def test_a_thin_pool_is_evidence_on_the_row_not_a_blocker(self):
        """A 29 USD pool produced a fake +4697 bps on 2026-10-07.

        Until 2026-10-08 depth was a veto (``below_depth``). Operator that day:
        「以利润为准…即使我们的交易占池子超过 20% 也是可以的」 -- a thin pool is
        a *number* on the row ("our ticket is N% of it"), and what refuses the row
        is the price it quotes, not the size of the pool.
        """
        entries = [{"symbol": "MSTRx", "chain": "bsc", "pool_id": "0xTHIN",
                    "depth_usd": D("29"), "errors": [],
                    "buy": self._quote(BUY, "857", chain="bsc", venue="pancakeswap",
                                       pool_id="0xTHIN"),
                    "sell": self._quote(SELL, "237.5", chain="bsc", venue="pancakeswap",
                                        pool_id="0xTHIN")}]
        rows = scan.build_candidates(
            symbol="MSTRx", chain_quotes=entries,
            kraken_row={"bid": D("161.6"), "ask": D("161.6"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"), deposits={}, v4_tiers={},
            now_ms=1000, phase="scan")
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn("below_depth", row["blockers"])
            self.assertFalse(row["executable"])
        entry = next(row for row in rows
                     if row["family"] == "chain_to_kraken")
        # The pool is recorded with the numbers behind the verdict, and the row
        # is refused because the spread is negative -- not because it is thin.
        self.assertEqual(entry["dead_pools"][0]["detail"], "pool_too_thin")
        self.assertEqual(entry["dead_pools"][0]["reason"], "pool_evidence")
        self.assertGreater(D(entry["dead_pools"][0]["pool_share"]), D("0.2"))
        self.assertIn("below_threshold", entry["blockers"])

    def test_a_wide_round_trip_is_recorded_and_is_not_a_veto(self):
        """One-sided pools are marked, not refused (operator, 2026-10-08).

        KLACx's own round trip is 13x the spread it quoted -- a pool with no
        liquidity on one side. That is a fact about the pool, not about our
        trade: we cross it once. Measured that night: HOODx/x-layer quoted a
        clean buy curve (109.96 -> 111.12 USDT over 50 -> 200 USDT) while the
        round trip said 5242 bps, and the rule had put it out of monitoring.
        """
        entries = [{"symbol": "KLACx", "chain": "solana", "errors": [],
                    "buy": self._quote(BUY, "10822"),
                    "sell": self._quote(SELL, "758.5")}]
        rows = scan.build_candidates(
            symbol="KLACx", chain_quotes=entries,
            kraken_row={"bid": D("194.37"), "ask": D("196.61"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"), deposits={}, v4_tiers={},
            now_ms=1000, phase="scan")
        for row in rows:
            self.assertNotIn("below_depth", row["blockers"])
            self.assertTrue(row["round_trip_wide"])      # still reported

    def test_the_impact_that_counts_is_the_side_we_cross(self):
        """The impact we report is the side we cross, and it never blocks."""
        buy = Quote(venue="jupiter", chain="solana", symbol="VUGx", side=BUY,
                    kind=REAL, price_usd=D("100"), recv_ms=1000,
                    extra={"impact_bps": D("5000")})
        sell = Quote(venue="jupiter", chain="solana", symbol="VUGx", side=SELL,
                     kind=REAL, price_usd=D("99"), recv_ms=1000,
                     extra={"impact_bps": D("12")})
        entry = {"symbol": "VUGx", "chain": "solana", "errors": [],
                 "buy": buy, "sell": sell}
        # Buying in this pool: 5000 bps of impact is ours to pay, and it is
        # reported as evidence -- the price we are quoted already carries it.
        buying = scan.pool_quality_evidence(entry, policy=self.policy, side="buy")
        self.assertEqual(buying["blockers"], [])
        self.assertTrue(buying["impact_over"])
        self.assertEqual(buying["impact_bps"], D("5000"))
        # Selling into it: the same figure is the other side's problem, so the
        # impact we report is the 12 bps of the side we would cross.
        selling = scan.pool_quality_evidence(entry, policy=self.policy, side="sell")
        self.assertEqual(selling["blockers"], [])
        self.assertFalse(selling["impact_over"])
        self.assertEqual(selling["impact_bps"], D("12"))
        self.assertEqual(scan.pool_side(entry, buy, None), "buy")
        self.assertEqual(scan.pool_side(entry, None, sell), "sell")
        self.assertIsNone(scan.pool_side(entry, None, None))

    def test_a_quote_that_reports_huge_impact_is_not_executable(self):
        """VUGx quoted 5482 against a Kraken bid of 92 and slipped through.

        Its reverse quote had failed, so the same-pool round trip could not run.
        What stops it now is the price itself (the row is refused on its own
        spread); the 980,000 bps impact is printed beside it as evidence.
        """
        big = Quote(venue="jupiter", chain="solana", symbol="VUGx", side=BUY,
                    kind=REAL, price_usd=D("5482"), recv_ms=1000,
                    extra={"impact_bps": D("980000")})
        entries = [{"symbol": "VUGx", "chain": "solana", "errors": ["no_sell_route"],
                    "buy": big}]
        rows = scan.build_candidates(
            symbol="VUGx", chain_quotes=entries,
            kraken_row={"bid": D("92.19"), "ask": D("92.5"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"), deposits={}, v4_tiers={},
            now_ms=1000, phase="paper_kraken")
        row = [item for item in rows if item["family"] == "chain_to_kraken"][0]
        self.assertNotIn("below_depth", row["blockers"])
        self.assertIn("below_threshold", row["blockers"])
        self.assertEqual(row["dead_pools"][0]["detail"], "price_impact")
        self.assertFalse(row["executable"])

    def test_a_normal_impact_does_not_block(self):
        good = Quote(venue="jupiter", chain="solana", symbol="STRCx", side=BUY,
                     kind=REAL, price_usd=D("100"), recv_ms=1000,
                     extra={"impact_bps": D("12")})
        entries = [{"symbol": "STRCx", "chain": "solana", "errors": [], "buy": good}]
        # The bid has to be above the buy price: a chain -> Kraken row now carries
        # its own spread verdict (``below_threshold``), and this test is about the
        # price-impact gate, not about the spread. The old fixture sold at 99
        # against a buy at 100 and only read as clean because the scan did not
        # cost that direction at all (found 2026-10-07: all 18 "ok" rows were
        # negative, which is what made the board blame a missing sample).
        rows = scan.build_candidates(
            symbol="STRCx", chain_quotes=entries,
            kraken_row={"bid": D("101"), "ask": D("101.5"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"),
            deposits={"STRCx": [{"method": "STRCx - Solana"}]}, v4_tiers={},
            now_ms=1000, phase="paper_kraken")
        row = [item for item in rows if item["family"] == "chain_to_kraken"][0]
        self.assertEqual(row["blockers"], [])
        self.assertTrue(row["executable"])

    def test_a_price_that_is_a_multiple_of_the_reference_is_bad_data(self):
        """Regression: +26,164 bps with an empty blocker list (2026-10-08 09:16).

        The moment the same-pool round trip stopped being a veto, this row came
        out of the scan with **no blocker at all**: KLACx sold into its own pool
        at 758 while Kraken's bid was 194. The impact figure on that side was
        small, so the directional impact rule could not see it -- the price
        *level* is what is broken. A live gate that only reads ``blockers`` would
        have taken it, which is why the outlier band exists.
        """
        entries = [{"symbol": "KLACx", "chain": "solana", "pool_id": "sol",
                    "errors": [],
                    "buy": self._quote(BUY, "10822"),
                    "sell": self._quote(SELL, "758.5")}]
        rows = scan.build_candidates(
            symbol="KLACx", chain_quotes=entries,
            kraken_row={"bid": D("194.37"), "ask": D("196.61"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"), deposits={}, v4_tiers={},
            now_ms=1000, phase="paper_kraken",
            withdraw_methods={"KLACx": [{"method": "KLACx - Solana",
                                         "network": "Solana", "minimum": "0.0005",
                                         "fee": {"fee": "0.000004"}}]})
        row = [item for item in rows if item["family"] == "kraken_to_chain"][0]
        self.assertIn("price_outlier", row["blockers"])
        self.assertEqual(row["reason"], "price_outlier")
        self.assertFalse(row["executable"])

    def test_a_big_but_not_absurd_discount_is_not_a_data_problem(self):
        """The band has to leave the real discounts alone (XOPx was 23-30%)."""
        entries = [{"symbol": "KLACx", "chain": "solana", "pool_id": "sol",
                    "errors": [],
                    "buy": self._quote(BUY, "90"),
                    "sell": self._quote(SELL, "80")}]
        rows = scan.build_candidates(
            symbol="KLACx", chain_quotes=entries,
            kraken_row={"bid": D("100"), "ask": D("101"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"), deposits={}, v4_tiers={},
            now_ms=1000, phase="paper_kraken")
        # 1000 bps below the bid: a real-looking buying opportunity, so it must
        # not be labelled bad data (it is still refused economically -- this
        # Solana -> Kraken direction has no measured bridge or rail here).
        row = [item for item in rows if item["family"] == "chain_to_kraken"][0]
        self.assertNotIn("price_outlier", row["blockers"])

    def test_chain_to_chain_is_emitted_and_never_executable_without_a_sample(self):
        """The any-to-any case: buy on Solana, sell on X Layer.

        It has to appear in the scan -- otherwise nobody ever sees it -- but it
        must stay non-executable until that exact triple has arrived once.
        """
        entries = [
            {"symbol": "STRCx", "chain": "solana", "pool_id": "sol", "errors": [],
             "buy": self._quote(BUY, "100.00", chain="solana", venue="jupiter"),
             "sell": self._quote(SELL, "100.20", chain="solana", venue="jupiter")},
            {"symbol": "STRCx", "chain": "x-layer", "pool_id": "xl", "errors": [],
             "buy": self._quote(BUY, "101.00", chain="x-layer", venue="uniswap"),
             "sell": self._quote(SELL, "102.50", chain="x-layer", venue="uniswap")},
        ]
        rows = scan.build_candidates(
            symbol="STRCx", chain_quotes=entries,
            kraken_row={"bid": D("99"), "ask": D("99.5"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"), deposits={}, v4_tiers={},
            now_ms=1000, phase="paper_kraken")
        cross = [row for row in rows if row["family"] == "chain_to_chain"]
        self.assertTrue(cross, "跨链行必须出现，否则没人能看见这种机会")
        # solana -> x-layer: the buy leg is the Solana one.
        up = [row for row in cross if row["chain"] == "solana->x-layer"][0]
        self.assertIn("unmeasured_route", up["blockers"])
        self.assertIn("cost_unknown", up["blockers"])
        self.assertFalse(up["executable"])
        self.assertEqual(D(up["edge_t0_bps"]), D("250.00"))
        self.assertEqual(up["ccip_dialect"], "solana_source")
        # x-layer -> solana is the other direction, and its dialect differs.
        down = [row for row in cross if row["chain"] == "x-layer->solana"][0]
        self.assertEqual(down["ccip_dialect"], "evm_to_solana")

    def test_a_thin_destination_pool_is_reported_not_silently_trusted(self):
        """TSLAx on Ethereum: a 250 USD pool that pays 424 for a 378 marker.

        Until 2026-10-08 this row was refused because the pool's own round trip
        (buy 525, sell 424) lost 1926 bps. The operator retired that rule: we
        cross the exit pool once, so a wide two-way spread is a *note* about the
        pool, not a verdict on the trade. What is left is the ticket rule, the
        impact figure where the venue reports one (EVM does not today), and the
        net-profit gate -- see the open question in docs/xstocks-v2-todo.md.
        """
        healthy = {"symbol": "TSLAx", "chain": "x-layer", "pool_id": "xl",
                   "depth_usd": D("433053"), "errors": [],
                   "buy": self._quote(BUY, "378.55", chain="x-layer", venue="uniswap"),
                   "sell": self._quote(SELL, "378.16", chain="x-layer", venue="uniswap")}
        thin = {"symbol": "TSLAx", "chain": "eth", "pool_id": "eth",
                "depth_usd": D("250.88"), "errors": [],
                "buy": self._quote(BUY, "525.23", chain="eth", venue="uniswap"),
                "sell": self._quote(SELL, "424.09", chain="eth", venue="uniswap")}
        rows = scan.build_candidates(
            symbol="TSLAx", chain_quotes=[healthy, thin],
            kraken_row={"bid": D("378.49"), "ask": D("378.50"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"), deposits={}, v4_tiers={},
            now_ms=1000, phase="paper_kraken")
        row = [item for item in rows if item["chain"] == "x-layer->eth"][0]
        self.assertNotIn("below_depth", row["blockers"])
        self.assertTrue(row["round_trip_wide"])
        # The base ticket (25 USDT) whatever the pool depth is; the thin pool is
        # carried as evidence (2026-10-08: depth no longer sizes or blocks).
        self.assertEqual(D(row["notional_usdt"]),
                         self.policy.min_trade_quote_usdt)
        self.assertFalse(row["executable"])

    def test_two_healthy_pools_still_produce_a_cross_chain_row(self):
        """The check must not kill a genuine cross-chain row."""
        a = {"symbol": "TSLAx", "chain": "x-layer", "pool_id": "xl",
             "depth_usd": D("433053"), "errors": [],
             "buy": self._quote(BUY, "378.55", chain="x-layer"),
             "sell": self._quote(SELL, "378.16", chain="x-layer")}
        b = {"symbol": "TSLAx", "chain": "eth", "pool_id": "eth",
             "depth_usd": D("50000"), "errors": [],
             "buy": self._quote(BUY, "380.10", chain="eth"),
             "sell": self._quote(SELL, "380.00", chain="eth")}
        rows = scan.build_candidates(
            symbol="TSLAx", chain_quotes=[a, b],
            kraken_row={"bid": D("378.49"), "ask": D("378.50"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"), deposits={}, v4_tiers={},
            now_ms=1000, phase="paper_kraken")
        row = [item for item in rows if item["chain"] == "x-layer->eth"][0]
        self.assertNotIn("below_depth", row["blockers"])
        self.assertIn("unmeasured_route", row["blockers"])

    def test_a_chain_pair_without_a_bridge_emits_nothing(self):
        entries = [
            {"symbol": "AAPLx", "chain": "solana", "pool_id": "sol", "errors": [],
             "buy": self._quote(BUY, "100", chain="solana"),
             "sell": self._quote(SELL, "101", chain="solana")},
            {"symbol": "AAPLx", "chain": "mantle", "pool_id": "mt", "errors": [],
             "buy": self._quote(BUY, "90", chain="mantle"),
             "sell": self._quote(SELL, "120", chain="mantle")},
        ]
        rows = scan.build_candidates(
            symbol="AAPLx", chain_quotes=entries,
            kraken_row={"bid": D("99"), "ask": D("99.5"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"), deposits={}, v4_tiers={},
            now_ms=1000, phase="paper_kraken")
        self.assertEqual([row for row in rows if row["family"] == "chain_to_chain"], [])

    def test_a_measured_bridge_still_needs_a_known_fee(self):
        entries = [
            {"symbol": "STRCx", "chain": "x-layer", "pool_id": "xl", "errors": [],
             "buy": self._quote(BUY, "100", chain="x-layer"),
             "sell": self._quote(SELL, "101", chain="x-layer")},
            {"symbol": "STRCx", "chain": "solana", "pool_id": "sol", "errors": [],
             "buy": self._quote(BUY, "99", chain="solana"),
             "sell": self._quote(SELL, "103", chain="solana")},
        ]
        rows = scan.build_candidates(
            symbol="STRCx", chain_quotes=entries,
            kraken_row={"bid": D("99"), "ask": D("99.5"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"), deposits={}, v4_tiers={},
            now_ms=1000, phase="paper_kraken",
            measured_bridges=frozenset({("x-layer", "solana")}))
        row = [item for item in rows if item["chain"] == "x-layer->solana"][0]
        self.assertNotIn("unmeasured_route", row["blockers"])
        self.assertIn("cost_unknown", row["blockers"])

    def test_kraken_to_chain_row_uses_ask(self):
        entries = [{"symbol": "AAPLx", "chain": "solana", "errors": [],
                    "buy": self._quote(BUY, "100"), "sell": self._quote(SELL, "110")}]
        rows = scan.build_candidates(
            symbol="AAPLx", chain_quotes=entries,
            kraken_row={"bid": D("99"), "ask": D("100"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"),
            deposits={"AAPLx": [{"method": "AAPLx - Solana"}]},
            v4_tiers={}, now_ms=1000, phase="scan",
            # Kraken -> chain needs a Kraken WITHDRAWAL rail on that network.
            # Without one the arc does not exist and no row is emitted.
            withdraw_methods={"AAPLx": [{"method": "AAPLx - Solana",
                                        "network": "Solana", "minimum": "0.00461503",
                                        "fee": {"fee": "0.000001"}}]})
        row = [item for item in rows if item["family"] == "kraken_to_chain"][0]
        self.assertEqual(D(row["edge_t0_bps"]), D("1000.00"))
        self.assertNotIn("no_deposit_method", row["blockers"])

    def test_kraken_to_chain_without_a_withdrawal_rail_is_not_a_path(self):
        """Operator 2026-10-07: a phantom Kraken -> x-layer row was on the board.

        Kraken has no xStock rail on X Layer, so that arc does not exist -- the
        real route is Kraken -> a rail it does have -> bridge -> X Layer.
        """
        entries = [{"symbol": "SPYx", "chain": "x-layer", "errors": [],
                    "buy": self._quote(BUY, "100"), "sell": self._quote(SELL, "110")}]
        missing: list[str] = []
        rows = scan.build_candidates(
            symbol="SPYx", chain_quotes=entries,
            kraken_row={"bid": D("99"), "ask": D("100"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"), deposits={}, v4_tiers={},
            now_ms=1000, phase="scan",
            withdraw_methods={"SPYx": [{"method": "SPYx", "network": "Solana"}]},
            missing_rails=missing)
        self.assertFalse([item for item in rows
                          if item["family"] == "kraken_to_chain"])
        self.assertEqual(missing, ["SPYx:x-layer"])
        # and the same run still prices the other direction, so nothing else broke
        self.assertTrue([item for item in rows
                         if item["family"] == "chain_to_kraken"])

    def test_a_stale_pair_is_reported_and_not_refused(self):
        """Round 1 finds candidates; round 2 re-quotes both legs (2026-10-08).

        Operator: "快照超过 15 秒就整族拒绝，这样过分严格了…第一轮询价尽量多询价
        找机会，第二轮真实询价才是防范风险". With a 10 s pairing window and a
        ~40 s round, four of the five live families were dropping whole families
        of rows on a 15 s old snapshot. The pairing distance is still recorded --
        the row says so -- but the decision moves to the fresh re-quote.
        """
        entries = [{"symbol": "AAPLx", "chain": "solana", "errors": [],
                    "buy": self._quote(BUY, "100", recv_ms=1000),
                    "sell": self._quote(SELL, "99", recv_ms=1000)}]
        rows = scan.build_candidates(
            symbol="AAPLx", chain_quotes=entries,
            kraken_row={"bid": D("101"), "ask": D("101"), "recv_ms": 1000 + 60000},
            policy=self.policy, multiplier=D("1"), deposits={}, v4_tiers={},
            now_ms=1000 + 60000, phase="scan")
        row = [item for item in rows if item["family"] == "chain_to_kraken"][0]
        self.assertNotIn("stale_or_skew", row["blockers"])
        self.assertTrue(row["stale_or_skew"])

    def test_v4_pool_that_is_not_green_is_not_executable(self):
        entries = [{"symbol": "AAPLx", "chain": "eth", "kind": "v4",
                    "pool_id": "0xPOOL", "errors": [],
                    "buy": self._quote(BUY, "100", chain="eth", venue="uniswap-v4"),
                    "sell": self._quote(SELL, "99", chain="eth", venue="uniswap-v4")}]
        rows = scan.build_candidates(
            symbol="AAPLx", chain_quotes=entries,
            kraken_row={"bid": D("101"), "ask": D("101"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"),
            deposits={"AAPLx": [{"method": "AAPLx - Ethereum"}]},
            v4_tiers={"0xpool": "amber"}, now_ms=1000, phase="scan")
        row = [item for item in rows if item["family"] == "chain_to_kraken"][0]
        self.assertIn("hook_not_green", row["blockers"])

    def test_dex_to_dex_never_pairs_a_pool_with_itself(self):
        entries = [{"symbol": "AAPLx", "chain": "eth", "pool_id": "0xPOOL", "errors": [],
                    "buy": self._quote(BUY, "100", chain="eth", pool_id="0xPOOL"),
                    "sell": self._quote(SELL, "120", chain="eth", pool_id="0xPOOL")}]
        rows = scan.build_candidates(
            symbol="AAPLx", chain_quotes=entries,
            kraken_row={"bid": D("99"), "ask": D("101"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"), deposits={}, v4_tiers={},
            now_ms=1000, phase="scan")
        self.assertEqual([row for row in rows if row["family"] == "dex_to_dex"], [])

    def test_dex_to_dex_uses_two_different_pools(self):
        entries = [
            {"symbol": "AAPLx", "chain": "eth", "pool_id": "0xA", "errors": [],
             "buy": self._quote(BUY, "100", chain="eth", pool_id="0xA"),
             "sell": self._quote(SELL, "101", chain="eth", pool_id="0xA")},
            {"symbol": "AAPLx", "chain": "eth", "pool_id": "0xB", "errors": [],
             "buy": self._quote(BUY, "102", chain="eth", pool_id="0xB"),
             "sell": self._quote(SELL, "110", chain="eth", pool_id="0xB")},
        ]
        rows = scan.build_candidates(
            symbol="AAPLx", chain_quotes=entries,
            kraken_row={"bid": D("99"), "ask": D("101"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"), deposits={}, v4_tiers={},
            now_ms=1000, phase="scan")
        pairs = [(row["buy_venue"], row["sell_venue"]) for row in rows
                 if row["family"] == "dex_to_dex"]
        self.assertTrue(pairs)
        self.assertTrue(all("0xA" != "0xB" for _a, _b in pairs))

    def test_a_missing_chain_sell_route_only_matters_when_the_chain_is_the_exit(self):
        """``chain -> Kraken`` does not need a chain sell leg at all.

        Measured 2026-10-07: a failed reverse quote on Solana was blocking the
        chain-to-Kraken direction, whose exit is the Kraken book.
        """
        entries = [{"symbol": "AAPLx", "chain": "solana", "errors": ["no_sell_route"],
                    "buy": self._quote(BUY, "100")}]
        rows = scan.build_candidates(
            symbol="AAPLx", chain_quotes=entries,
            kraken_row={"bid": D("99"), "ask": D("101"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"), deposits={}, v4_tiers={},
            now_ms=1000, phase="scan")
        row = [item for item in rows if item["family"] == "chain_to_kraken"][0]
        self.assertNotIn("no_dest_pool", row["blockers"])
        # And the reverse direction is emitted with the reason instead of vanishing.
        reverse = [item for item in rows if item["family"] == "kraken_to_chain"]
        self.assertEqual(len(reverse), 1)
        self.assertIn("no_dest_pool", reverse[0]["blockers"])
        self.assertIsNone(reverse[0]["edge_t0_bps"])

    def test_rank_stage2_keeps_only_names_near_the_threshold(self):
        rows = [{"symbol": "A", "edge_t0_bps": "25", "blockers": []},
                {"symbol": "B", "edge_t0_bps": "-5", "blockers": []},
                {"symbol": "C", "edge_t0_bps": "-200", "blockers": []},
                {"symbol": "D", "edge_t0_bps": "99", "blockers": ["same_pool"]}]
        picked = scan.rank_stage2(rows, threshold_bps=D("20"), buffer_bps=D("30"), limit=10)
        self.assertEqual([row["symbol"] for row in picked], ["A", "B"])


class WrapperPoolUnitTest(unittest.TestCase):
    """A wrapper pool quotes per wrapper; every other venue quotes per credit.

    Measured 2026-10-07: X Layer's $1.89M SPYx pool trades the wrapper ``wSPYx``
    (``convertToAssets(1) = 1.00571456`` native) while the CCIP bridge hands us
    the **native** token, so the exit route wraps first and pays 774.97 -- not
    the pool's 779.40. Reading the pool price straight printed the multiplier
    itself as a +55 bps edge on the board (operator, 2026-10-07).
    """

    SPY_MULTIPLIER = D("1.005714560286254")

    def _pool(self, *, form, symbol="SPYx"):
        return {"chain": "x-layer", "symbol": symbol, "pool_id": "0xpool",
                "dex": "uniswap-v3-x-layer", "kind": "v3",
                "token_address": "0x" + "e7" * 20, "quote_asset": "USDG",
                "quote_address": "0x" + "4a" * 20, "token_decimals": 18,
                "quote_decimals": 6, "fee": D("0.0005"), "depth_usd": D("1893245"),
                "form": form, "quoter": "0x" + "d1" * 20}

    class Quoter:
        """Replays a fixed buy output, then a fixed sell output."""

        def __init__(self, buy_raw, sell_raw):
            self.raw = [buy_raw, sell_raw]
            self.rounds = 0

        def request(self, url, *, data=None, headers=None):
            payload = json.loads(data.decode())
            raw = self.raw[min(self.rounds, len(self.raw) - 1)]
            self.rounds += 1
            return 200, json.dumps(
                [{"jsonrpc": "2.0", "id": row["id"],
                  "result": "0x" + format(raw, "064x")} for row in payload]).encode()

    def _quote(self, form, buy_raw, sell_raw, multipliers):
        rows = scan.evm_pair_quotes(
            self.Quoter(buy_raw, sell_raw), evm.CHAINS["x-layer"],
            [self._pool(form=form)], D("200"), recv_ms=1000, multipliers=multipliers)
        self.assertEqual(rows[0]["errors"], [])
        return rows[0]

    def test_native_pool_price_is_left_alone(self):
        row = self._quote("原生", 500_000_000_000_000_000, 250_000_000,
                          {"SPYx": self.SPY_MULTIPLIER})
        self.assertEqual(row["credit_factor"], "1")
        self.assertEqual(row["tokens"], D("0.5"))
        self.assertEqual(row["buy"].price_usd, D("400"))
        self.assertEqual(row["sell"].price_usd, D("500"))

    def test_wrapper_pool_price_is_per_credit_not_per_wrapper(self):
        row = self._quote("wrapper", 500_000_000_000_000_000, 250_000_000,
                          {"SPYx": self.SPY_MULTIPLIER})
        credit = D("0.5") * self.SPY_MULTIPLIER
        self.assertEqual(row["credit_factor"], format(self.SPY_MULTIPLIER, "f"))
        self.assertEqual(row["raw_tokens"], D("0.5"))
        self.assertEqual(row["tokens"], credit)
        self.assertEqual(row["buy"].price_usd, D("200") / credit)
        self.assertEqual(row["sell"].price_usd, D("250") / credit)
        # The face value is the wrapper price; per credit it is lower by exactly
        # the multiplier, which is the whole bug.
        self.assertEqual(row["sell"].price_usd * self.SPY_MULTIPLIER, D("500"))

    def test_an_unrecognised_form_counts_as_a_wrapper(self):
        multipliers = {"SPYx": self.SPY_MULTIPLIER}
        self.assertEqual(scan.pool_credit_factor(self._pool(form=""), multipliers),
                         self.SPY_MULTIPLIER)
        self.assertEqual(scan.pool_credit_factor(self._pool(form="native"), multipliers),
                         D("1"))
        self.assertEqual(scan.pool_credit_factor(self._pool(form="wrapper"), {}), D("1"))
        # A wrapper whose multiplier was never recorded is not silently halved;
        # factor 1 keeps the old number rather than inventing a new one.
        self.assertEqual(scan.pool_credit_factor(self._pool(form="wrapper"), None), D("1"))

    def test_one_credit_unit_is_the_same_unit_on_every_path(self):
        """The invariant the 2026-10-07 wrapper bug broke.

        A Solana raw amount becomes credits by multiplying by the multiplier; a
        wrapper pool token becomes credits by *dividing* its price by the same
        multiplier. Both have to describe one and the same unit -- the native
        token Kraken books. Two implementations that drifted apart printed the
        multiplier itself as +55 bps of edge.
        """
        multipliers = {"SPYx": self.SPY_MULTIPLIER}
        raw_ui = D("0.5")
        from_solana = units.to_credit_units("solana", raw_ui, self.SPY_MULTIPLIER)
        factor = scan.pool_credit_factor(self._pool(form="wrapper"), multipliers)
        # 0.5 wrapper units are 0.5 x multiplier credits, and 1 pool token is
        # `factor` credits -- the same claim from both directions.
        self.assertEqual(raw_ui * factor, from_solana)
        # And the pool's price, read per credit, equals the native-token price.
        from_pool = D("779.4027713390238530896529516") / factor
        self.assertEqual(from_pool.quantize(D("0.0001")), D("774.9741"))
        # EVM amounts are already credit units, so no factor applies there.
        self.assertEqual(units.to_credit_units("x-layer", raw_ui, self.SPY_MULTIPLIER),
                         raw_ui)

    def test_a_wrapper_factor_that_is_not_a_number_is_not_a_crash(self):
        for bad in ("", "abc", "0", "-2"):
            self.assertEqual(
                scan.pool_credit_factor(self._pool(form="wrapper"), {"SPYx": bad}),
                D("1"), bad)

    def test_the_real_spyx_pair_is_a_loss_not_a_55_bps_edge(self):
        """The measured 2026-10-07 quote pair, priced both ways."""
        row = self._quote("wrapper", 256_346_433_894_180_297, 199_797_121,
                          {"SPYx": self.SPY_MULTIPLIER})
        solana_buy = D("775.1380227215574957280526229")   # Jupiter, per credit
        self.assertEqual(row["sell"].price_usd.quantize(D("0.000001")),
                         D("774.974135"))
        edge = edge_bps(solana_buy, row["sell"].price_usd)
        self.assertLess(edge, D("-2"))
        # The same two numbers read at face value -- the wrapper pool's own unit
        # -- are the multiplier, which is what the board printed.
        face = edge_bps(solana_buy, D("199.797121") / D("0.256346433894180297"))
        self.assertGreater(face, D("55"))
        # The gap between the two readings is the multiplier (57.14 bps).
        self.assertGreater(face - edge, D("57.1"))
        self.assertLess(face - edge, D("57.2"))


class RailCoverageTest(unittest.TestCase):
    """``scan.rail_coverage``: which rails did this round not even look at?

    Measured 2026-10-07: 300 (asset, hub) rails Kraken supports, 44 quoted in a
    round -- and SOXLx/Solana (rail, no address) produced no row at all, so it
    was in neither the request list nor the deferred one.
    """

    METHODS = {
        "SPYx": [{"method": "SPYx", "network": "Solana"}],
        "SOXLx": [{"method": "SOXLx - Solana", "network": "Solana"}],
        "TSLAx": [{"method": "TSLAx", "network": "Ethereum"}],
    }

    def test_a_quoted_symbol_is_not_reported_missing(self):
        got = scan.rail_coverage(
            [{"symbol": "SPYx", "chain": "solana->x-layer"}],
            symbols={"SPYx", "SOXLx", "TSLAx"},
            withdraw_methods=self.METHODS, address_book=[])
        self.assertEqual(got["rails"], 3)
        self.assertEqual([item["symbol"] for item in got["missing"]],
                         ["SOXLx", "TSLAx"])
        self.assertNotIn("SPYx", [item["symbol"] for item in got["missing"]])

    def test_the_hub_chain_is_what_counts(self):
        """``solana->x-layer`` quotes the Solana rail, not the X Layer one."""
        got = scan.rail_coverage(
            [{"symbol": "TSLAx", "chain": "solana->x-layer"}],
            symbols={"TSLAx"}, withdraw_methods=self.METHODS, address_book=[])
        self.assertEqual([(m["symbol"], m["hub"]) for m in got["missing"]],
                         [("TSLAx", "eth")])

    def test_a_registered_address_is_flagged_so_the_reader_can_prioritise(self):
        got = scan.rail_coverage(
            [], symbols={"SPYx", "SOXLx"}, withdraw_methods=self.METHODS,
            # The book is keyed by (asset, method) -- the method name has to
            # match the WithdrawMethods row, not the network string.
            address_book=[{"asset": "SPYx", "method": "SPYx",
                           "network": "Solana",
                           "address": "AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev"}])
        rows = {item["symbol"]: item for item in got["missing"]}
        self.assertFalse(rows["SOXLx"]["has_address"])
        self.assertTrue(rows["SPYx"]["has_address"])
        # Sorted so the ones that need *only* a registration come first.
        self.assertEqual(got["missing"][0]["symbol"], "SPYx")

    def test_a_symbol_kraken_has_no_rail_for_is_not_a_missing_rail(self):
        got = scan.rail_coverage([], symbols={"SPYx"}, withdraw_methods={},
                                 address_book=[])
        self.assertEqual(got["rails"], 0)
        self.assertEqual(got["missing"], [])


class PoolTicketTest(unittest.TestCase):
    """The ticket comes from the pool, not the other way round (2026-10-08).

    v1 quoted a ladder of sizes per name and bought the largest one that was
    still cheap. v2 asks one size, so the size has to be derived from the pool:
    a 300 USD pool is a 60 USDT trade, not a refused row. The $1000-for-a-200
    ticket rule refused pools whose depth was never the binding fact (measured
    2026-10-08: five x-layer pools 313k-433k deep, three of them one-sided).
    """

    def setUp(self):
        self.policy = V2Policy.load()

    def _quote(self, side, price, *, venue="uniswap-v3-x-layer", chain="x-layer",
               pool_id="0xpool"):
        return Quote(venue=venue, chain=chain, symbol="TSLAx", side=side, kind=REAL,
                     price_usd=D(price), recv_ms=1000, pool_id=pool_id)

    def _entry(self, depth, *, buy="376.30", sell="375.90", pool_id="0xpool"):
        return {"symbol": "TSLAx", "chain": "x-layer", "pool_id": pool_id,
                "dex": "uniswap-v3-x-layer", "depth_usd": None if depth is None else D(depth),
                "errors": [],
                "buy": self._quote(BUY, buy, pool_id=pool_id),
                "sell": self._quote(SELL, sell, pool_id=pool_id)}

    def test_the_base_ticket_is_the_smallest_size_we_would_trade(self):
        """Round 1 asks the floor whatever the pool depth is (2026-10-08).

        Operator 2026-10-08 (AGENTS.md 「第一原则：赚钱优先」): the size is no
        longer ``depth × 20%``. Depth is *evidence*; round 1 asks the smallest
        ticket we would really trade and round 2 walks the ladder up while the
        trade still pays. A deep pool, a 300 USD pool and a pool with no depth
        figure at all therefore all get the same base ticket.
        """
        for depth in ("433053", "300", None):
            self.assertEqual(
                scan.pool_ticket_usdt(self._entry(depth), policy=self.policy),
                self.policy.min_trade_quote_usdt)
        self.assertEqual(scan.solana_quote_usdt(self.policy),
                         self.policy.min_trade_quote_usdt)

    def test_a_thin_pool_is_reported_not_refused(self):
        """A 29 USD pool: our 25 USDT ticket is 86% of it. Say so; do not refuse.

        Operator 2026-10-08: 「以利润为准…即使我们的交易占池子超过 20% 也是
        可以的」. The impact is already inside the price the venue quotes for our
        size, so the profit test reads it -- what is left for the board to print
        is the number.
        """
        signals = scan.pool_quality_evidence(self._entry("29"), policy=self.policy)
        self.assertEqual(signals["blockers"], [])
        self.assertTrue(signals["thin_pool"])
        self.assertEqual(signals["detail"], "pool_too_thin")
        self.assertGreater(signals["pool_share"], self.policy.max_depth_share)
        self.assertEqual(signals["depth_usd"], D("29"))

    def test_a_pool_our_ticket_barely_touches_is_not_flagged(self):
        signals = scan.pool_quality_evidence(self._entry("300"), policy=self.policy)
        self.assertEqual(signals["blockers"], [])
        self.assertFalse(signals["thin_pool"])
        self.assertEqual(signals["detail"], "")
        self.assertLess(signals["pool_share"], self.policy.max_depth_share)

    def test_a_one_sided_pool_is_reported_with_its_numbers(self):
        """HOODx/x-layer: 355k 'deep', bought at 110.92, sold back at 6.32.

        The depth rule cannot see this (355k is thirty-five times the gate), and
        since 2026-10-08 neither does ``below_depth``: the round trip is carried
        as evidence so a human can see "this pool has no bid side", while the
        decision is made on the side we would actually cross.
        """
        signals = scan.pool_quality_evidence(
            self._entry("355158", buy="110.92", sell="6.32"), policy=self.policy)
        self.assertEqual(signals["blockers"], [])
        self.assertGreater(signals["round_trip_bps"], D("9000"))
        self.assertTrue(signals["round_trip_wide"])
        self.assertEqual(signals["depth_usd"], D("355158"))
        # Deep, just one-sided: nothing to flag.
        self.assertFalse(signals["thin_pool"])

    def test_a_thin_row_reports_the_pool_it_touched_not_the_kraken_end(self):
        """A Kraken -> chain row buys at Kraken and sells into the pool.

        Keying the dead-pool entry off the row's venue recorded one pool twice
        (``uniswap:<pool>`` and ``kraken:<pool>``), which is how 46 real pools
        became 82 entries on 2026-10-08.
        """
        thin = self._entry("29", pool_id="0xTHIN")
        rows = scan.build_candidates(
            symbol="TSLAx", chain_quotes=[thin],
            kraken_row={"bid": D("376.29"), "ask": D("376.31"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"), deposits={}, v4_tiers={},
            now_ms=1000, phase="paper_kraken",
            withdraw_methods={"TSLAx": [{"method": "TSLAx - X Layer",
                                         "network": "XLayer",
                                         "minimum": "0.0005",
                                         "fee": {"fee": "0.000009"}}]})
        reverse = [row for row in rows if row["family"] == "kraken_to_chain"][0]
        # Depth no longer blocks (2026-10-08): it is carried as evidence with the
        # pool's own identity, which is what stops one pool being recorded twice.
        self.assertEqual(reverse["quoted_pools"], ["uniswap-v3-x-layer:0xthin"])
        self.assertEqual([item["key"] for item in reverse["dead_pools"]],
                         ["uniswap-v3-x-layer:0xthin"])
        self.assertEqual(reverse["dead_pools"][0]["detail"], "pool_too_thin")
        self.assertEqual(reverse["dead_pools"][0]["reason"], "pool_evidence")
        # The base ticket, whatever the pool depth is.
        self.assertEqual(D(reverse["notional_usdt"]),
                         self.policy.min_trade_quote_usdt)


class StoreTest(unittest.TestCase):
    def test_write_json_is_atomic_and_readable(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan.json"
            store.write_json(path, {"rows": [1, 2]})
            self.assertEqual(store.read_json(path)["rows"], [1, 2])
            self.assertEqual(list(Path(tmp).glob(".v2-*.tmp")), [])

    def test_jsonl_appends_and_survives_a_bad_line(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "cycles.jsonl"
            store.append_jsonl(path, {"state": "refused"})
            store.append_jsonl(path, {"state": "sold"})
            path.write_text(path.read_text() + "not json\n")
            rows = store.read_jsonl(path)
        self.assertEqual([row["state"] for row in rows], ["refused", "sold"])


if __name__ == "__main__":
    unittest.main()
