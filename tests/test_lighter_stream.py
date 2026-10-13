"""Lighter's order-book WebSocket as a venue clock (added 2026-09-22).

The bug this pins down: Lighter's REST book carries no time field at all, so the
adapter used the market's latest *trade* as the venue clock. A market being
re-quoted but never traded therefore read as stale — 94.5% of Lighter's rows
carried a stale flag over the 54-hour window measured on 2026-09-20.

The fix has two halves that must not be confused, and most of these tests exist
to keep them apart:

* the **clock** comes from the venue's WebSocket ``timestamp``;
* the **levels** still come from the REST snapshot, because the venue documents
  that after the first frame the channel sends "only state changes".

Everything here is offline: frames are the shapes published in the venue's own
WebSocket reference and the socket is a fake. No test asserts on elapsed time —
the ones that wait poll for a state change with a generous budget.
"""
from __future__ import annotations

import asyncio
import json
import time
import unittest
from decimal import Decimal

from arb_monitor.core.hk import Contract
from arb_monitor.data.hk_cex import CexPublicAdapter, now_ms, parse_book
from arb_monitor.data.lighter_stream import (
    LighterOrderBookStream, LighterStreamState, lighter_market_ids,
)

# "Now", as a real 2026 millisecond value: clock fixtures have to be plausible,
# since an implausible receive time is itself a staleness.
RECV_MS = 1_789_624_601_961

# The venue's own example frame (apidocs.lighter.xyz/docs/websocket-reference).
BOOK_FRAME = {
    "channel": "order_book:203",
    "last_updated_at": 1774884082309144,
    "offset": 1558300,
    "order_book": {
        "code": 0,
        "asks": [{"price": "3.33", "size": "5602.72"}],
        "bids": [{"price": "3.30", "size": "15.00"}],
        "offset": 1558300,
        "nonce": 9182390020,
        "last_updated_at": 1774884082309144,
        "begin_nonce": 9182389998,
    },
    "timestamp": 1774884082326,
    "type": "update/order_book",
}


def delta_frame(market_id, nonce, begin_nonce, bid_levels=1, price="3.30"):
    """A follow-up frame: only the levels that changed, per the venue's docs."""
    return {
        "channel": f"order_book:{market_id}",
        "order_book": {
            "code": 0,
            "asks": [], "bids": [{"price": price, "size": "1"}],
            "nonce": nonce, "begin_nonce": begin_nonce,
        },
        "timestamp": RECV_MS - 10,
        "type": "update/order_book",
    }


def lighter_contract(market_id="203"):
    return Contract("lighter", market_id, "XIAOMI", "hk_stock_candidate",
                    "usd_linear", Decimal("1"), Decimal("1"), Decimal("1"),
                    Decimal("10"), Decimal("0.0001"), True, "src", 1, {})


class StreamClockTests(unittest.TestCase):
    def test_only_the_monitored_lighter_markets_are_subscribed(self):
        contracts = [
            lighter_contract("203"),                       # XIAOMI, monitored
            Contract("lighter", "201", "TENCENT", "hk_stock_candidate",
                     "usd_linear", Decimal("1"), Decimal("1"), Decimal("1"),
                     Decimal("10"), Decimal("0.0001"), True, "src", 1, {}),
            Contract("lighter", "200", "BYD", "hk_related_unmatched",
                     "usd_linear", Decimal("1"), Decimal("1"), Decimal("1"),
                     Decimal("10"), Decimal("0.0001"), True, "src", 1, {}),
            Contract("aster", "XIAOMIUSDT", "XIAOMI", "hk_stock_candidate",
                     "usd_linear", Decimal("1"), Decimal("1"), Decimal("1"),
                     Decimal("5"), Decimal("0.001"), True, "src", 1, {}),
        ]
        # Lighter lists ~17 of our HK candidates; only the monitored assets get
        # a subscription, and a non-Lighter venue never contributes a market id.
        self.assertEqual(lighter_market_ids(contracts, {"XIAOMI", "TENCENT"}),
                         ["201", "203"])
        self.assertEqual(lighter_market_ids(contracts, {"SHEIN"}), [])

    def test_a_frame_yields_the_venue_own_clock(self):
        state = LighterStreamState(markets={"203"})
        self.assertEqual(state.apply(BOOK_FRAME, RECV_MS), "applied")
        clock = state.clock_for("203", now=RECV_MS + 1_000)
        # The venue's `timestamp` — not our receive time, not a trade time.
        self.assertEqual(clock["ts"], 1774884082326)
        self.assertEqual(clock["recv_ms"], RECV_MS)
        self.assertEqual(clock["book_ts_ms"], 1774884082309)
        self.assertEqual(clock["nonce"], 9182390020)
        self.assertEqual(clock["frame_age_ms"], 1_000)
        # The first frame of a subscription is the full snapshot; the levels it
        # carried are recorded as evidence, and used as nothing else.
        self.assertTrue(clock["snapshot"])
        self.assertEqual(clock["frame_bid_levels"], 1)

    def test_the_clock_stamps_a_rest_book_and_keeps_its_levels(self):
        state = LighterStreamState(markets={"203"})
        # The adapter stamps the REST read with the live clock, so the frame has
        # to be one the live clock could actually have just received.
        received = now_ms()
        state.apply(dict(BOOK_FRAME, timestamp=received - 1_200), received)
        adapter = CexPublicAdapter("lighter", None)
        adapter.stream = LighterOrderBookStream(["203"])
        adapter.stream.state = state
        # REST levels are full depth (20 levels here); the frame had one level.
        adapter._lighter_rest_book = lambda contract, clock=None: {
            "bids": [["3.29", "40"]] * 20, "asks": [["3.34", "40"]] * 20,
            "ts": clock["ts"], "lastUpdateId": None,
            "_arb_source": "ccxt_public:lighter:depth+ws_clock", "clock": clock,
        }
        book = adapter.book(lighter_contract())
        self.assertEqual(book.source, "ccxt_public:lighter:depth+ws_clock")
        self.assertEqual(book.venue_ts_ms, received - 1_200)
        self.assertEqual(len(book.bids), 20)
        self.assertIsNone(book.sequence)
        self.assertTrue(book.is_fresh(now_ms(), 15_000))
        self.assertEqual(book.metadata["raw"]["clock"]["nonce"], 9182390020)

    def test_microsecond_book_clock_is_normalised_when_the_frame_clock_is_missing(self):
        state = LighterStreamState(markets={"203"})
        state.apply({k: v for k, v in BOOK_FRAME.items() if k != "timestamp"},
                    RECV_MS)
        # last_updated_at is microseconds: 1774884082309144 µs -> 1774884082309 ms.
        self.assertEqual(state.clock_for("203", now=RECV_MS)["ts"], 1774884082309)

    def test_a_frame_without_a_venue_clock_is_never_vouched_for(self):
        state = LighterStreamState(markets={"203"})
        frame = {k: v for k, v in BOOK_FRAME.items()
                 if k not in {"timestamp", "last_updated_at"}}
        frame["order_book"] = {k: v for k, v in BOOK_FRAME["order_book"].items()
                               if k != "last_updated_at"}
        state.apply(frame, RECV_MS)
        self.assertIsNone(state.clock_for("203", now=RECV_MS))
        # The frame is still counted: "we heard from the venue, without a clock"
        # is different evidence from "we heard nothing".
        self.assertEqual(state.health(now=RECV_MS)["frames"], 1)

    def test_a_venue_clock_running_ahead_cannot_make_a_book_look_young(self):
        state = LighterStreamState(markets={"203"})
        state.apply(dict(BOOK_FRAME, timestamp=RECV_MS + 30_000), RECV_MS)
        # Our receive time caps the claim: +30 s of venue clock cannot buy 30 s
        # of freshness for a frame we already hold.
        self.assertEqual(state.clock_for("203", now=RECV_MS)["ts"], RECV_MS)

    def test_the_nonce_chain_is_tracked_and_a_break_is_counted(self):
        state = LighterStreamState(markets={"203"})
        state.apply(BOOK_FRAME, RECV_MS)
        chained = delta_frame("203", nonce=9182390030, begin_nonce=9182390020)
        state.apply(chained, RECV_MS + 50)
        self.assertTrue(state.clock_for("203")["continuity"])
        self.assertFalse(state.clock_for("203")["snapshot"])
        gap = delta_frame("203", nonce=9182390099, begin_nonce=9182390050)
        state.apply(gap, RECV_MS + 100)
        self.assertFalse(state.clock_for("203")["continuity"])
        health = state.health()
        self.assertEqual(health["continuity_breaks"], 1)
        self.assertEqual(health["snapshots"], 1)
        self.assertEqual(health["frames"], 3)

    def test_bad_frames_do_not_clobber_the_last_good_clock(self):
        state = LighterStreamState(markets={"203"})
        state.apply(BOOK_FRAME, RECV_MS)
        self.assertEqual(state.apply({"type": "pong"}, RECV_MS + 1), "pong")
        # A lifecycle frame is not a book and is not an error either.
        self.assertEqual(state.apply({"type": "connected"}, RECV_MS + 2),
                         "not_a_book")
        self.assertEqual(
            state.apply({"channel": "order_book:9",
                         "order_book": {"bids": [{"price": "1", "size": "1"}],
                                        "asks": []}}, RECV_MS + 4),
            "ignored:unsubscribed_market")
        clock = state.clock_for("203", now=RECV_MS + 1_000)
        self.assertEqual(clock["ts"], 1774884082326)
        health = state.health(now=RECV_MS + 1_000)
        self.assertEqual(health["frames"], 1)
        self.assertEqual(health["ignored"], 1)
        self.assertEqual(health["pongs"], 1)

    def test_an_empty_state_clock_is_none_not_a_timestamp(self):
        state = LighterStreamState(markets={"203"})
        self.assertIsNone(state.clock_for("203"))
        self.assertIsNone(state.clock_for("not_subscribed"))


class AdapterWiringTests(unittest.TestCase):
    """`book()` takes levels from REST and the clock from the stream."""

    REST_TS = 1_789_622_878_501

    def setUp(self):
        self.adapter = CexPublicAdapter("lighter", None)
        self.seen = {}

        def rest(contract, clock=None):
            self.seen["clock"] = clock
            return {"bids": [["3.29", "2"]], "asks": [["3.34", "2"]],
                    "ts": (clock or {}).get("ts") or self.REST_TS,
                    "lastUpdateId": None,
                    "_arb_source": ("ccxt_public:lighter:depth+ws_clock" if clock
                                    else "ccxt_public:lighter:depth+recent_trades_anchor"),
                    "clock": clock}

        self.adapter._lighter_rest_book = rest

    def test_the_stream_clock_is_read_before_the_rest_call(self):
        stream = LighterOrderBookStream(["203"])
        received = now_ms()
        stream.state.apply(dict(BOOK_FRAME, timestamp=received - 1_000), received)
        self.adapter.stream = stream
        book = self.adapter.book(lighter_contract())
        self.assertEqual(book.venue_ts_ms, received - 1_000)
        # The reader is handed the *stream's* clock, stamped with the moment the
        # frame came off the socket. Asserting that it equals the book's own
        # ``recv_ts_ms`` (as this did until 2026-10-10) is a millisecond race:
        # the frame stamp and the book stamp come from two separate ``now_ms()``
        # calls, and CI failed on "1791613083852 != 1791613083851". What has to
        # hold is the ordering: the clock was read before the book was stamped,
        # and it carries the frame's own receive time.
        clock = self.seen["clock"]
        self.assertIsNotNone(clock)
        self.assertEqual(clock["recv_ms"], received)
        self.assertLessEqual(clock["recv_ms"], book.recv_ts_ms)
        self.assertEqual(book.source, "ccxt_public:lighter:depth+ws_clock")
        self.assertTrue(book.is_fresh(received, 15_000))

    def test_a_delta_frame_is_never_turned_into_a_book(self):
        stream = LighterOrderBookStream(["203"])
        received = now_ms()
        stream.state.apply(
            delta_frame("203", nonce=5, begin_nonce=4), received)
        self.adapter.stream = stream
        book = self.adapter.book(lighter_contract())
        # The frame carried exactly one bid level and no asks; the served book
        # is the two-sided REST snapshot.
        self.assertEqual([(str(x.price), str(x.quantity)) for x in book.bids],
                         [("3.29", "2")])
        self.assertEqual([(str(x.price), str(x.quantity)) for x in book.asks],
                         [("3.34", "2")])

    def test_rest_with_the_trade_anchor_is_the_fallback(self):
        cases = {"no_stream": False, "no_frame": True, "unclocked_frame": True}
        for name, attach in cases.items():
            with self.subTest(case=name):
                if not attach:
                    self.adapter.stream = None
                else:
                    stream = LighterOrderBookStream(["203"])
                    if name == "unclocked_frame":
                        payload = {k: v for k, v in BOOK_FRAME.items()
                                   if k not in {"timestamp", "last_updated_at"}}
                        payload["order_book"] = {
                            k: v for k, v in BOOK_FRAME["order_book"].items()
                            if k != "last_updated_at"}
                        stream.state.apply(payload, now_ms())
                    self.adapter.stream = stream
                book = self.adapter.book(lighter_contract())
                self.assertIsNone(self.seen["clock"])
                self.assertEqual(book.source,
                                 "ccxt_public:lighter:depth+recent_trades_anchor")
                self.assertEqual(book.venue_ts_ms, self.REST_TS)

    def test_a_stream_that_has_only_spoken_about_another_market_falls_back(self):
        stream = LighterOrderBookStream(["201"])
        stream.state.apply(dict(BOOK_FRAME, channel="order_book:201"), now_ms())
        self.adapter.stream = stream
        self.adapter.book(lighter_contract("203"))
        self.assertIsNone(self.seen["clock"])


class _FakeType:
    def __init__(self, name):
        self.name = name


class _FakeMessage:
    def __init__(self, name, data=None):
        self.type = _FakeType(name)
        self.data = data


class _FakeSocket:
    def __init__(self, script):
        self.script = list(script)
        self.sent = []

    async def send_str(self, text):
        self.sent.append(json.loads(text))

    async def receive(self, timeout=None):
        if not self.script:
            await asyncio.sleep(0.01)
            raise asyncio.TimeoutError
        return self.script.pop(0)


class _FakeSession:
    def __init__(self, socket):
        self.socket = socket

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def ws_connect(self, url, **kwargs):
        self.url, self.kwargs = url, kwargs
        return self.socket


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class StreamLoopTests(unittest.TestCase):
    """The socket loop: subscribe, store, reconnect — with a fake transport."""

    def test_it_subscribes_to_its_markets_and_reconnects_after_a_drop(self):
        first = _FakeSocket([
            _FakeMessage("TEXT", json.dumps({"type": "subscribed/order_book",
                                             "channel": "order_book/203"})),
            _FakeMessage("TEXT", json.dumps(BOOK_FRAME)),
            _FakeMessage("CLOSED"),
        ])
        queue = [first, _FakeSocket([])]

        def factory():
            socket = queue.pop(0) if queue else _FakeSocket([])
            return _FakeSession(socket)

        stream = LighterOrderBookStream(
            ["203"], session_factory=factory, backoff_seconds=(0.01,),
            subscribe_timeout_s=1.0)
        try:
            stream.start()
            self.assertTrue(_wait_for(lambda: stream.state.frames >= 1))
            self.assertEqual(first.sent[0],
                             {"type": "subscribe", "channel": "order_book/203"})
            self.assertTrue(_wait_for(lambda: stream._reconnects >= 1))
        finally:
            stream.close()
        self.assertIsNone(stream._thread)
        health = stream.health()
        self.assertGreaterEqual(health["connects"], 2)
        self.assertEqual(health["markets"]["203"]["venue_ts_ms"], 1774884082326)
        self.assertTrue(health["snapshots"] >= 1)

    def test_close_stops_the_thread_even_with_an_idle_socket(self):
        stream = LighterOrderBookStream(
            ["203"], session_factory=lambda: _FakeSession(_FakeSocket([])),
            backoff_seconds=(0.01,))
        stream.start()
        self.assertTrue(_wait_for(lambda: stream.connected))
        stream.close()
        self.assertIsNone(stream._thread)
        self.assertFalse(stream.connected)
        stream.close()  # idempotent


if __name__ == "__main__":
    unittest.main()
