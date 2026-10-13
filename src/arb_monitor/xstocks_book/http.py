"""Small JSON client for the xStocks book process.

Kept here, with its own user agent, so a refresh never goes through the
monitor's HTTP stack or its caches.
"""
from __future__ import annotations

import json
import time
import urllib.request
from urllib.error import HTTPError, URLError


class RateLimit(RuntimeError):
    """The remote host asked us to slow down. The caller skips this item."""


class NotFound(RuntimeError):
    """The URL is not on that host. For a pool lookup this means no listing."""


class JsonClient:
    def __init__(self, proxy: str | None = None, timeout: int = 30,
                 retries: int = 3, sleep=time.sleep, rate_limit_sleep: float = 15):
        self.proxy = proxy or None
        self.timeout = timeout
        self.retries = retries
        self.sleep = sleep
        self.rate_limit_sleep = rate_limit_sleep

    def get_json(self, url: str) -> object:
        last: Exception | None = None
        limited = False
        for attempt in range(self.retries):
            try:
                return self._once(url)
            except RateLimit:
                # One pause, then a single retry. Hammering through a 429 makes
                # the rest of the round fail the same way.
                if limited:
                    raise
                limited = True
                self.sleep(self.rate_limit_sleep)
                continue
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                last = exc
                if attempt + 1 < self.retries:
                    self.sleep(1.0 * (attempt + 1))
        raise RuntimeError(f"unreadable response from {url}: {last}")

    def _once(self, url: str) -> object:
        request = urllib.request.Request(url, headers={"User-Agent": "xstocks-book/0.1"})
        if self.proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
            open_call = opener.open
        else:
            open_call = urllib.request.urlopen
        try:
            with open_call(request, timeout=self.timeout) as response:
                if response.status == 429:
                    raise RateLimit(url)
                return json.loads(response.read().decode())
        except HTTPError as exc:
            if exc.code == 429:
                raise RateLimit(url) from exc
            if exc.code == 404:
                raise NotFound(url) from exc
            raise
