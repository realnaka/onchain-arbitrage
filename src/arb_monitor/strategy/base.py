from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Protocol

from arb_monitor.core.models import Intent, OrderBook


class Strategy(Protocol):
    name: str

    def on_snapshot(
        self, books: Sequence[OrderBook], now_ms: int
    ) -> Iterable[Intent]:
        ...

