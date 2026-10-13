"""Lighter's order-book WebSocket, held as the venue's own clock.

Why this exists (measured 2026-09-22)
-------------------------------------
Lighter is the only venue here whose REST book carries **no** time field at all:
``/api/v1/orderBookOrders`` returns ``code/total_asks/asks/total_bids/bids`` and
nothing else — no ``ts``, no ``offset``, no ``nonce`` (read off the venue's
OpenAPI definition, 2026-09-22). The adapter therefore anchored freshness to the
market's latest *trade*, which answers a different question: a market being
re-quoted every second still read as stale whenever nothing traded. Measured cost
of that: 94.5% of Lighter's rows carried a stale flag over the 54-hour window in
``docs/venue-selection-2026-09-20.md``.

The venue's WebSocket does publish a clock, and this module holds it:

* ``wss://mainnet.zklighter.elliot.ai/stream``,
* subscribe ``{"type":"subscribe","channel":"order_book/{market_id}"}``,
* each message carries ``timestamp`` (venue milliseconds) and
  ``order_book.last_updated_at`` (venue microseconds), plus ``offset``, ``nonce``
  and ``begin_nonce``.

**Why the frames are used as a clock and not as a book.** The venue documents
that this channel "sends a complete snapshot on subscription, but only state
changes after that" (apidocs.lighter.xyz/docs/websocket-reference, read
2026-09-22). Measured live the same day: the frames for TENCENT/XIAOMI usually
carried one side and 1–3 levels, i.e. exactly those state changes. Assembling a
book from deltas would mean trusting a nonce chain we cannot replay — a missed
update would silently become a wrong book, which is the failure this project has
paid for before. So the split is:

* **levels** — the REST snapshot, full depth, one call per market per cycle,
  unchanged from before;
* **clock** — the venue's own timestamp from the WebSocket, read *before* the
  REST call so a change that lands mid-read can never be claimed as included.

What it deliberately does **not** do
------------------------------------
* It never guesses a clock: a frame without a venue timestamp is evidence only.
* It does not serve levels. A delta frame is not a book.
* It does not loosen any gate. The timestamp is the venue's, and the freshness
  budget stays where it was (``risk/hk_basis.py``, 15 s), so a market the venue
  has not touched inside that window still fails closed. Whether "book unchanged
  for N minutes" should stay a rejection is an operator/risk question.
* It is not an execution path: read-only subscribe, no ``sendTx``.

The transport is injectable (``session_factory``) so the tests drive the real
subscribe/parse/store loop against a fake socket and never touch the network.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping

from arb_monitor.data.hk_cex import now_ms

LIGHTER_STREAM_URL = "wss://mainnet.zklighter.elliot.ai/stream"
# The venue requires a client frame at least every 2 minutes and answers
# ``{"type":"ping"}`` with ``{"type":"pong"}``. Half the allowance leaves room
# for a slow round trip without ever tripping the server's own limit.
LIGHTER_PING_SECONDS = 60.0
LIGHTER_RECV_TIMEOUT_SECONDS = 1.0


def _as_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_ms(value: Any) -> int | None:
    """Venue clocks arrive in both ms and µs; normalise to ms.

    ``timestamp`` is milliseconds while the same stamp is published as
    ``last_updated_at`` in microseconds (the venue's own example: 1774884082326
    ms against 1774884082309144 µs).
    """
    raw = _as_int(value)
    if raw is None or raw <= 0:
        return None
    if raw > 10**15:          # microseconds
        return raw // 1000
    return raw


def _level_counts(rows: Any) -> int:
    """How many levels a frame carried — evidence, never a book."""
    return sum(1 for row in rows or [] if isinstance(row, Mapping))


@dataclass(frozen=True)
class LighterBookClock:
    """One order-book message, kept for its clocks and its nonce chain."""

    market_id: str
    recv_ms: int                 # our clock, when the frame came off the socket
    frame_ts_ms: int | None      # venue clock: when the venue emitted the frame
    book_ts_ms: int | None       # venue clock: when this book last changed
    nonce: int | None
    begin_nonce: int | None
    snapshot: bool               # first frame of this subscription = full book
    continuity: bool | None      # begin_nonce == previous nonce (see module doc)
    bid_levels: int
    ask_levels: int
    channel: str = ""

    @property
    def venue_ts_ms(self) -> int | None:
        """The venue's own time for this book, capped by when we received it."""
        stamp = self.frame_ts_ms or self.book_ts_ms
        if stamp is None:
            return None
        return min(stamp, self.recv_ms)


@dataclass
class LighterStreamState:
    """Latest clock per market. Pure state: no sockets, no threads.

    Every mutation happens under ``lock`` because the monitor samples on its own
    thread while the socket thread stores frames.
    """

    markets: set[str] = field(default_factory=set)
    clock_samples: int = 20

    def __post_init__(self) -> None:
        self.lock = threading.Lock()
        self._clocks: dict[str, LighterBookClock] = {}
        self._offsets: deque[int] = deque(maxlen=max(1, int(self.clock_samples)))
        self.frames = 0
        self.snapshots = 0
        self.continuity_breaks = 0
        self.ignored = 0
        self.pongs = 0
        self.last_frame_ms: int | None = None
        self.last_frame_market: str | None = None
        self.last_ignored_reason: str | None = None

    # --- ingest ---------------------------------------------------------
    def apply(self, payload: Mapping[str, Any], recv_ms: int) -> str:
        """Store one WebSocket message. Returns why it was kept or dropped."""
        if not isinstance(payload, Mapping):
            return self._ignore("not_an_object")
        if str(payload.get("type") or "") == "pong":
            with self.lock:
                self.pongs += 1
            return "pong"
        book = payload.get("order_book")
        if not isinstance(book, Mapping):
            # Lifecycle/ack frames: counted by the caller, not here.
            return "not_a_book"
        channel = str(payload.get("channel") or "")
        market_id = self._market_of(channel, book)
        if market_id is None:
            return self._ignore("no_market_id")
        if self.markets and market_id not in self.markets:
            return self._ignore("unsubscribed_market")
        bids, asks = book.get("bids") or [], book.get("asks") or []
        nonce = _as_int(book.get("nonce"))
        begin_nonce = _as_int(book.get("begin_nonce"))
        with self.lock:
            previous = self._clocks.get(market_id)
            snapshot = previous is None
            continuity = (None if snapshot or previous.nonce is None
                          or begin_nonce is None
                          else begin_nonce == previous.nonce)
            if continuity is False:
                self.continuity_breaks += 1
            clock = LighterBookClock(
                market_id=market_id, recv_ms=int(recv_ms),
                frame_ts_ms=_to_ms(payload.get("timestamp")),
                book_ts_ms=_to_ms(book.get("last_updated_at")
                                  or payload.get("last_updated_at")),
                nonce=nonce, begin_nonce=begin_nonce, snapshot=snapshot,
                continuity=continuity, bid_levels=_level_counts(bids),
                ask_levels=_level_counts(asks), channel=channel)
            self._clocks[market_id] = clock
            if clock.frame_ts_ms is not None:
                # venue_clock - our_clock at the moment we read it. Diagnostic:
                # nothing in the freshness path applies a corrected clock.
                self._offsets.append(clock.frame_ts_ms - int(recv_ms))
            self.frames += 1
            if snapshot:
                self.snapshots += 1
            self.last_frame_ms = int(recv_ms)
            self.last_frame_market = market_id
        return "applied"

    @staticmethod
    def _market_of(channel: str, book: Mapping[str, Any]) -> str | None:
        if channel:
            suffix = channel.split(":", 1)[1] if ":" in channel else (
                channel.split("/", 1)[1] if "/" in channel else "")
            if suffix:
                return str(suffix)
        for key in ("market_id", "market_index"):
            if book.get(key) is not None:
                return str(book.get(key))
        return None

    def _ignore(self, reason: str) -> str:
        with self.lock:
            self.ignored += 1
            self.last_ignored_reason = reason
        return f"ignored:{reason}"

    # --- read -----------------------------------------------------------
    def clock_for(self, market_id: str, now: int | None = None) -> dict[str, Any] | None:
        """The venue's clock for this market, or None if it has not spoken.

        None means "the stream cannot vouch for this market" — the caller then
        keeps the old last-trade anchor. It is never a substitute timestamp.
        """
        now = int(now if now is not None else now_ms())
        with self.lock:
            clock = self._clocks.get(str(market_id))
        if clock is None or clock.venue_ts_ms is None:
            return None
        return {
            "ts": clock.venue_ts_ms,
            "frame_ts_ms": clock.frame_ts_ms,
            "book_ts_ms": clock.book_ts_ms,
            "recv_ms": clock.recv_ms,
            "frame_age_ms": now - clock.recv_ms,
            "nonce": clock.nonce,
            "begin_nonce": clock.begin_nonce,
            "snapshot": clock.snapshot,
            "continuity": clock.continuity,
            # Carried so a reader of the stored row can see that these were the
            # levels of a *state change*, not a book — the reason they are not
            # used as one.
            "frame_bid_levels": clock.bid_levels,
            "frame_ask_levels": clock.ask_levels,
        }

    def clock_offset_ms(self) -> int | None:
        with self.lock:
            samples = sorted(self._offsets)
        if not samples:
            return None
        return samples[len(samples) // 2]

    def health(self, now: int | None = None) -> dict[str, Any]:
        now = int(now if now is not None else now_ms())
        with self.lock:
            clocks = dict(self._clocks)
            stats = {
                "frames": self.frames, "snapshots": self.snapshots,
                "continuity_breaks": self.continuity_breaks,
                "ignored": self.ignored, "pongs": self.pongs,
                "last_frame_ms": self.last_frame_ms,
                "last_frame_market": self.last_frame_market,
                "last_ignored_reason": self.last_ignored_reason,
            }
        markets = {}
        for market_id, clock in sorted(clocks.items()):
            markets[market_id] = {
                "recv_age_ms": now - clock.recv_ms,
                "venue_ts_ms": clock.venue_ts_ms,
                "book_ts_ms": clock.book_ts_ms,
                "frame_bid_levels": clock.bid_levels,
                "frame_ask_levels": clock.ask_levels,
                "nonce": clock.nonce,
                "continuity": clock.continuity,
            }
        return {**stats, "markets": markets,
                "clock_offset_ms": self.clock_offset_ms()}


def _aiohttp_session_factory():
    try:
        import aiohttp
    except ImportError as exc:  # pragma: no cover - environment guard
        raise RuntimeError("Install the live extra: pip install -e '.[live]'") from exc
    return aiohttp.ClientSession()


def lighter_market_ids(contracts: Iterable[Any], assets: Iterable[str]) -> list[str]:
    """The Lighter markets this monitor actually samples.

    Lighter lists far more of the HK-name universe than the six assets we watch,
    and the socket is cheap but not free: the first version of this stream
    subscribed to all 17 Lighter contracts the metadata scan knows about, on the
    same local proxy the rest of the grid shares. The subscription set is
    therefore derived from the *monitored asset list*, not from "everything the
    venue lists".
    """
    wanted = {str(a) for a in assets}
    return sorted({str(c.instrument_id) for c in contracts
                   if str(c.venue) == "lighter" and str(c.asset) in wanted})


class LighterOrderBookStream:
    """Background WebSocket subscription feeding a :class:`LighterStreamState`.

    One daemon thread, one connection, automatic reconnect with backoff. The
    thread only reads: nothing here can place, modify or cancel an order.
    """

    def __init__(
        self,
        markets: Iterable[str],
        *,
        url: str = LIGHTER_STREAM_URL,
        proxy: str | None = None,
        subscribe_timeout_s: float = 20.0,
        backoff_seconds: tuple[float, ...] = (2.0, 5.0, 15.0, 30.0, 60.0),
        session_factory: Callable[[], Awaitable[Any]] | None = None,
    ):
        self.url = str(url)
        self.proxy = proxy
        self.subscribe_timeout_s = float(subscribe_timeout_s)
        self.backoff_seconds = tuple(backoff_seconds) or (5.0,)
        self.state = LighterStreamState(markets={str(m) for m in markets})
        self._session_factory = session_factory or _aiohttp_session_factory
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connects = 0
        self._reconnects = 0
        self._connected = False
        self._connected_at_ms: int | None = None
        self._last_error: str | None = None
        self._last_error_ms: int | None = None
        self._sent_subscriptions: set[str] = set()

    # --- lifecycle ------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="lighter-order-book", daemon=True)
        self._thread.start()

    def close(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout_s)
        self._thread = None
        self._connected = False

    def set_markets(self, markets: Iterable[str]) -> None:
        """Add markets; the socket thread subscribes on its next wake-up."""
        with self.state.lock:
            self.state.markets = {str(m) for m in markets}

    @property
    def connected(self) -> bool:
        return self._connected

    def clock_for(self, market_id: str, now: int | None = None) -> dict[str, Any] | None:
        return self.state.clock_for(market_id, now)

    def health(self, now: int | None = None) -> dict[str, Any]:
        payload = self.state.health(now)
        now = int(now if now is not None else now_ms())
        payload.update({
            "enabled": True,
            "connected": self._connected,
            "connected_at_ms": self._connected_at_ms,
            "connection_age_ms": (now - self._connected_at_ms
                                  if self._connected_at_ms else None),
            "connects": self._connects,
            "reconnects": self._reconnects,
            "url": self.url,
            "subscribed_markets": sorted(self._sent_subscriptions),
            "last_error": self._last_error,
            "last_error_age_ms": (now - self._last_error_ms
                                  if self._last_error_ms else None),
            "thread_alive": bool(self._thread and self._thread.is_alive()),
            "last_frame_age_ms": (now - payload["last_frame_ms"]
                                  if payload.get("last_frame_ms") else None),
        })
        return payload

    # --- thread ---------------------------------------------------------
    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                asyncio.run(self._stream_once())
                attempt = 0
            except Exception as exc:  # noqa: BLE001 - a stream must not kill the monitor
                self._record_error(f"{type(exc).__name__}:{str(exc)[:120]}")
            finally:
                self._connected = False
                self._sent_subscriptions = set()
            if self._stop.is_set():
                break
            delay = self.backoff_seconds[min(attempt, len(self.backoff_seconds) - 1)]
            attempt += 1
            self._stop.wait(delay)

    def _record_error(self, message: str) -> None:
        self._last_error = message
        self._last_error_ms = now_ms()

    async def _stream_once(self) -> None:
        """One connection: subscribe, then read frames until it drops."""
        async with await self._open_session() as session:
            ws = await session.ws_connect(
                self.url, proxy=self.proxy, timeout=self.subscribe_timeout_s,
                heartbeat=None, max_msg_size=8 * 1024 * 1024)
            self._connects += 1
            if self._connects > 1:
                self._reconnects += 1
            self._connected = True
            self._connected_at_ms = now_ms()
            self._sent_subscriptions = set()
            next_ping = time.monotonic() + LIGHTER_PING_SECONDS
            while not self._stop.is_set():
                await self._subscribe_new(ws)
                if time.monotonic() >= next_ping:
                    await ws.send_str(json.dumps({"type": "ping"}))
                    next_ping = time.monotonic() + LIGHTER_PING_SECONDS
                try:
                    # aiohttp cancels the pending receive itself on timeout, so a
                    # quiet market lets the loop re-check subscribe/ping.
                    message = await ws.receive(timeout=LIGHTER_RECV_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    continue
                event = self._message_payload(message)
                if event is None:
                    raise ConnectionError("lighter_stream_closed")
                if isinstance(event, Mapping):
                    self.state.apply(event, now_ms())

    async def _open_session(self):
        """Open whatever the injected factory returns (sync or awaitable)."""
        session = self._session_factory()
        if inspect.isawaitable(session):
            session = await session
        return session

    async def _subscribe_new(self, ws) -> None:
        with self.state.lock:
            wanted = set(self.state.markets)
        for market_id in sorted(wanted - self._sent_subscriptions):
            await ws.send_str(json.dumps({
                "type": "subscribe", "channel": f"order_book/{market_id}"}))
            self._sent_subscriptions.add(market_id)

    @staticmethod
    def _message_payload(message: Any) -> Mapping[str, Any] | None:
        """aiohttp-shaped message → parsed object, or None when the link closed."""
        kind = getattr(message, "type", None)
        name = getattr(kind, "name", str(kind or ""))
        text = getattr(message, "data", None)
        if name in {"CLOSED", "CLOSING"}:
            return None
        if name == "ERROR":
            raise ConnectionError(f"lighter_stream_error:{text}")
        if name not in {"TEXT", "BINARY"}:
            return {}
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return {}
