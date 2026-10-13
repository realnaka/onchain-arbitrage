"""The spot venue interface: two sides, one record shape.

Strategy code never calls an exchange, an RPC or a wallet. It reads ``Quote``
records that a feed produced (spec section 11). Every quote carries the two
timestamps the freshness gate needs, because the whole point of the any-to-any
upgrade is that the two sides are not read at the same instant.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

BUY = "buy"
SELL = "sell"

# A reference price may rank a candidate. It may never enter net profit.
REFERENCE = "reference"
REAL = "real"


@dataclass(frozen=True)
class Quote:
    venue: str
    chain: str
    symbol: str
    side: str
    kind: str
    price_usd: Decimal | None
    recv_ms: int
    qty_token: Decimal | None = None
    qty_quote: Decimal | None = None
    venue_ms: int | None = None
    source: str = ""
    depth_usd: Decimal | None = None
    fee_bps: Decimal | None = None
    pool_id: str = ""
    note: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def real(self) -> bool:
        return self.kind == REAL and self.price_usd is not None

    def as_row(self) -> dict:
        return {
            "venue": self.venue, "chain": self.chain, "symbol": self.symbol,
            "side": self.side, "kind": self.kind,
            "price_usd": None if self.price_usd is None else format(self.price_usd, "f"),
            "recv_ms": self.recv_ms, "venue_ms": self.venue_ms,
            "source": self.source, "pool_id": self.pool_id, "note": self.note,
            "depth_usd": None if self.depth_usd is None else format(self.depth_usd, "f"),
        }


class SpotVenue(Protocol):
    """A place that quotes both sides of one symbol."""

    name: str
    chain: str

    def quote_buy(self, symbol: str, notional_usd: Decimal) -> Quote | None:
        """What we pay to acquire ``notional_usd`` of the token."""

    def quote_sell(self, symbol: str, quantity: Decimal) -> Quote | None:
        """What we receive for selling ``quantity`` Kraken credit units."""


def edge_bps(buy_price: Decimal | None, sell_price: Decimal | None) -> Decimal | None:
    """Net-of-nothing spread, in bps. Costs are applied by the caller."""
    if buy_price is None or sell_price is None or buy_price <= 0:
        return None
    return (sell_price / buy_price - Decimal(1)) * Decimal(10000)


def best_buy(quotes) -> Quote | None:
    usable = [quote for quote in quotes if quote and quote.price_usd is not None]
    return min(usable, key=lambda quote: quote.price_usd) if usable else None


def best_sell(quotes) -> Quote | None:
    usable = [quote for quote in quotes if quote and quote.price_usd is not None]
    return max(usable, key=lambda quote: quote.price_usd) if usable else None
