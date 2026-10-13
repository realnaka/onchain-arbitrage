"""Shared HTTP and rate limiting for the v2 feeds.

Two rules from the lessons library are enforced here rather than in each feed:

* **One budget per upstream.** Every Jupiter call shares one limiter (the v1
  board measured 2026-10-03 that per-thread retries kept the window full and
  every quote started failing).
* **A 429 pauses everyone.** Retrying at full speed after a 429 is what turns a
  brief limit into a dead round.

The limiter holds its lock for accounting only. It is never held while a
request is on the wire, so workers still overlap.
"""
from __future__ import annotations

import fcntl
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# Every outbound request on this machine shares one local proxy. The sandbox
# cannot reach 127.0.0.1:4780, so anything network-facing runs outside it.
PROXY = "http://127.0.0.1:4780"


class HttpError(RuntimeError):
    """Transport failure. Not an HTTP status: those are returned, not raised."""

    def __init__(self, url: str, detail: str):
        self.url = url
        self.detail = detail
        super().__init__(f"{url}: {detail}")


class RateLimiter:
    """Tokens over a sliding window, plus a global pause.

    ``capacity`` is how many calls may be in flight at once. It defaults to at
    least 2 so a caller with a worker pool can overlap requests instead of
    serialising on this lock.
    """

    def __init__(self, rps: float, *, capacity: float | None = None,
                 clock=time.monotonic, sleep=time.sleep,
                 shared_path: Path | str | None = None,
                 boot_marker=None, max_backlog_s: float = 300.0):
        if rps <= 0:
            raise ValueError("rps 必须大于 0")
        self.rps = float(rps)
        self.capacity = float(max(2.0, capacity if capacity is not None else 2.0))
        self.pauses = 0
        self.waits = 0.0
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._tokens = self.capacity
        self._at = clock()
        self._paused_until = 0.0
        # One budget, not one per process. See ``SharedSlot``.
        self.shared = (SharedSlot(shared_path, self.rps, clock=clock, sleep=sleep,
                                  boot_marker=boot_marker,
                                  max_backlog_s=max_backlog_s)
                       if shared_path else None)

    def pause(self, seconds: float) -> None:
        """Stop every worker until the window frees. Called on 429."""
        with self._lock:
            self._paused_until = max(self._paused_until, self._clock() + max(0.0, seconds))
            self.pauses += 1
        if self.shared is not None:
            # A 429 is the *key* being over budget, so every process backs off,
            # not only the one that happened to see the answer.
            self.shared.pause(seconds)

    def acquire(self) -> float:
        """Return once one token is spent. Returns the seconds waited."""
        if self.shared is not None and not self.shared.disabled:
            # The shared slot already paces the whole group; the per-process
            # bucket would only add a second, redundant wait on top of it.
            return self.shared.acquire()
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                if now < self._paused_until:
                    wait = self._paused_until - now
                else:
                    self._tokens = min(
                        self.capacity, self._tokens + (now - self._at) * self.rps)
                    self._at = now
                    if self._tokens >= 1.0:
                        self._tokens -= 1.0
                        self.waits += waited
                        return waited
                    wait = (1.0 - self._tokens) / self.rps
            self._sleep(wait)
            waited += wait


class SharedSlot:
    """One request slot per interval, shared by every process that can see a file.

    Measured 2026-10-08: the free Jupiter key allows 1 request/s, and the v2
    stack runs as several processes (the paper runner re-scans the whole
    universe itself, the live executor quotes its candidates, and a manual
    probe is a third). Each process paced itself, so together they asked for
    ~1.8 rps against a 1 rps budget, and Jupiter answered 429 to whichever call
    arrived second. That is not a dead pool: on the live executor's second-round
    quote it means an opportunity is dropped (3 live attempts, 3 x
    ``requote_failed`` on that day).

    The whole shared state is **two numbers in one file**: a marker for the
    running boot and the earliest time the next request may go out. ``flock``
    makes the read-modify-write atomic across processes, and the sleep happens
    *outside* the lock, so the process that is waiting never blocks the others.

    The boot marker matters because the clock is monotonic: a number written
    before a reboot reads as "long past" and would be honoured as a slot in the
    past. The marker is ``wall − monotonic``, which is stable inside one boot
    and different after the next, so a stale file is ignored instead of trusted.

    Failure is open, never closed: if the file cannot be opened the caller is
    paced by its own in-process bucket instead of waiting on a lock file that
    does not work. A monitoring process that stops quoting is worse than one
    that occasionally collides.
    """

    def __init__(self, path, rps: float, *, clock=time.monotonic,
                 sleep=time.sleep, boot_marker=None,
                 max_backlog_s: float = 300.0):
        self.path = Path(path)
        self.interval = 1.0 / max(0.001, float(rps))
        # Sane bound on a queue of our own making: a burst must not push the
        # last caller into the far future. Well above a 429 pause (60 s), so a
        # deliberate back-off is never cut short.
        self.max_backlog_s = max(0.0, float(max_backlog_s))
        self._marker = (boot_marker if boot_marker is not None
                        else lambda: time.time() - time.monotonic())
        self._our_boot = self._marker()
        self.disabled = False
        self.waits = 0.0
        self.calls = 0
        self._clock = clock
        self._sleep = sleep

    def _reserve(self) -> float:
        """Take the next slot, then hand the file's number to the next caller."""
        now = self._clock()
        try:
            with open(self.path, "a+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    slot, paused_until = self._read(handle)
                    slot = max(now, slot)
                    self._write(handle, slot + self.interval, paused_until)
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            self.disabled = True
            return now
        return slot

    def _read(self, handle) -> tuple[float, float]:
        """``(next slot, paused until)`` -- or ``(0, 0)`` from another boot."""
        handle.seek(0)
        parts = handle.read().split()
        try:
            marker, value = float(parts[0]), float(parts[1])
            paused = float(parts[2]) if len(parts) > 2 else 0.0
        except (IndexError, ValueError):
            return 0.0, 0.0
        if abs(marker - self._our_boot) > 1.0:
            return 0.0, 0.0
        now = self._clock()
        return min(max(value, 0.0), now + self.max_backlog_s), max(paused, 0.0)

    def _write(self, handle, value: float, paused_until: float = 0.0) -> None:
        handle.seek(0)
        handle.truncate()
        handle.write(f"{self._our_boot:.3f} {value:.6f} {paused_until:.6f}")
        handle.flush()

    def acquire(self) -> float:
        slot = self._reserve()
        self.calls += 1
        delay = max(0.0, slot - self._clock())
        if delay > 0:
            self._sleep(delay)
            self.waits += delay
        # A 429 can land *while* this caller is waiting. The slot above was
        # already booked, so without this check the back-off would only apply
        # to the reservations made after it -- and the callers already asleep
        # would fire straight into the wall that the pause exists to avoid.
        while True:
            wait = self._paused_for()
            if wait <= 0:
                break
            self._sleep(wait)
            self.waits += wait
            delay += wait
        return delay

    def pause(self, seconds: float) -> None:
        seconds = max(0.0, float(seconds))
        if seconds <= 0 or self.disabled:
            return
        now = self._clock()
        try:
            with open(self.path, "a+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    slot, paused = self._read(handle)
                    self._write(handle, max(slot, now + seconds),
                                max(paused, now + seconds))
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            self.disabled = True

    def _paused_for(self) -> float:
        """Seconds this caller still has to wait, read fresh from the file."""
        try:
            with open(self.path, "a+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    _slot, paused = self._read(handle)
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            self.disabled = True
            return 0.0
        return max(0.0, paused - self._clock())


class Http:
    """One opener per process, always through the local proxy."""

    def __init__(self, *, proxy: str = PROXY, user_agent: str = "arb-xstocks-v2/0.1",
                 timeout: float = 20.0, opener=None):
        if opener is None:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        self.opener = opener
        self.user_agent = user_agent
        self.timeout = timeout

    def request(self, url: str, *, data: bytes | None = None,
                headers: dict | None = None) -> tuple[int, bytes]:
        head = {"User-Agent": self.user_agent}
        if data is not None:
            head["Content-Type"] = "application/json"
        head.update(headers or {})
        request = urllib.request.Request(url, data=data, headers=head)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            # Kraken and Jupiter both put the reason in the body. Reading it is
            # how a NO_ROUTES_FOUND is told apart from a real outage.
            return exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HttpError(url, str(exc)) from exc

    def get_json(self, url: str, *, headers: dict | None = None,
                 timeout: float | None = None) -> tuple[int, object | None, dict]:
        """Return ``(status, json_or_None, response_headers)``."""
        # Do not write ``timeout`` back onto self: one feed shares one Http, and
        # a short timeout for one caller must not shorten everyone else's.
        saved, self.timeout = self.timeout, float(timeout) if timeout else self.timeout
        try:
            status, body = self.request(url, headers=headers)
        finally:
            self.timeout = saved
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except (ValueError, AttributeError):
            payload = None
        return status, payload, {}

    def post_json(self, url: str, payload: object, *,
                  headers: dict | None = None) -> tuple[int, object | None]:
        body = json.dumps(payload).encode()
        status, raw = self.request(url, data=body, headers=headers)
        try:
            decoded = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, AttributeError):
            decoded = None
        return status, decoded


def first_decimal(value: object):
    """Kraken wraps book rows as ``["123.4", "1.2", "ts"]``."""
    from decimal import Decimal, InvalidOperation

    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value in (None, ""):
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return None if out <= 0 else out
