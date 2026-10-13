from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Protocol

from arb_monitor.core.models import OrderBook


class MarketDataAdapter(Protocol):
    venue: str

    async def subscribe(self, symbols: Iterable[str]) -> AsyncIterator[OrderBook]:
        """Yield normalized order books and reconnect internally."""
        yield  # pragma: no cover


class InMemoryAdapter:
    """Deterministic adapter used by tests and the first demo."""

    def __init__(self, venue: str, books: Iterable[OrderBook]):
        self.venue = venue
        self.books = tuple(books)

    async def subscribe(self, symbols: Iterable[str]) -> AsyncIterator[OrderBook]:
        wanted = set(symbols)
        for book in self.books:
            if book.symbol in wanted:
                yield book

