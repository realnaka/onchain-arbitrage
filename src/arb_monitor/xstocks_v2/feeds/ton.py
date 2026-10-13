"""TON quotes through ston.fi.

TON is the one non-EVM venue where Kraken actually has an xStock rail
(``SPYx - TON`` and friends), so it has to be priced. v2 ignored it because the
EVM address filter dropped every ``EQ...`` jetton address.

The endpoint is ``POST`` with the parameters in the query string and an empty
body -- ston.fi rejects a JSON body, and Cloudflare answers 403 without a
User-Agent (both measured on the v1 board, ported here unchanged).

It answers both directions: buy is ``USDT -> jetton``, sell is
``jetton -> USDT``.
"""
from __future__ import annotations

import json
import urllib.parse

from arb_monitor.xstocks_v2.feeds.http import Http

STONFI_SIMULATE = "https://api.ston.fi/v1/swap/simulate"
DEFAULT_SLIPPAGE = "0.01"


def parse_units(payload: object) -> int | None:
    """``ask_units`` as an integer. A missing or zero answer is not a price."""
    if not isinstance(payload, dict):
        return None
    raw = payload.get("ask_units")
    if raw in (None, ""):
        return None
    try:
        units = int(str(raw))
    except ValueError:
        return None
    return units if units > 0 else None


def simulate_url(offer: str, ask: str, units: int,
                 slippage: str = DEFAULT_SLIPPAGE) -> str:
    query = urllib.parse.urlencode({
        "offer_address": offer, "ask_address": ask, "units": int(units),
        "slippage_tolerance": slippage,
    })
    return f"{STONFI_SIMULATE}?{query}"


class TonFeed:
    """One ston.fi client. No signature, no order, quote only."""

    def __init__(self, http: Http | None = None, *, slippage: str = DEFAULT_SLIPPAGE):
        self.http = http or Http()
        self.slippage = slippage
        self.calls = 0
        self.failures: dict[str, int] = {}

    def _note(self, reason: str) -> None:
        self.failures[reason] = self.failures.get(reason, 0) + 1

    def swap(self, offer: str, ask: str, units: int) -> dict | None:
        """The whole simulate payload, not just ``ask_units``.

        The executor needs the router address and ``min_ask_units`` from the
        same response the quote priced. A second request would be a different
        trade.
        """
        if not offer or not ask or units <= 0:
            return None
        url = simulate_url(offer, ask, units, self.slippage)
        self.calls += 1
        try:
            status, body = self.http.request(
                url, data=b"", headers={"Accept": "application/json"})
        except Exception as exc:  # noqa: BLE001
            self._note(f"transport:{type(exc).__name__}")
            return None
        if status != 200:
            self._note(f"http_{status}")
            return None
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except (ValueError, AttributeError):
            self._note("bad_payload")
            return None
        if not isinstance(payload, dict) or parse_units(payload) is None:
            self._note("zero_out")
            return None
        return payload

    def quote(self, offer: str, ask: str, units: int) -> int | None:
        payload = self.swap(offer, ask, units)
        return None if payload is None else parse_units(payload)
