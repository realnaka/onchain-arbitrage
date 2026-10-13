from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Level:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True)
class OrderBook:
    venue: str
    symbol: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    venue_ts_ms: int | None
    recv_ts_ms: int
    sequence: int | None = None
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def age_ms(self) -> int:
        return self.age_at(self.recv_ts_ms)

    def age_at(self, now_ms: int) -> int:
        if self.venue_ts_ms is None:
            return 2**63 - 1
        return max(0, now_ms - min(self.venue_ts_ms, self.recv_ts_ms))

    def is_fresh(self, now_ms: int, max_age_ms: int, clock_skew_ms: int = 1000) -> bool:
        return (
            self.venue_ts_ms is not None
            and self.venue_ts_ms <= now_ms + clock_skew_ms
            and self.recv_ts_ms <= now_ms + clock_skew_ms
            and self.age_at(now_ms) <= max_age_ms
        )


@dataclass(frozen=True)
class FeeModel:
    buy_bps: Decimal = Decimal("0")
    sell_bps: Decimal = Decimal("0")
    slippage_bps: Decimal = Decimal("0")
    gas_quote: Decimal = Decimal("0")
    funding_quote: Decimal = Decimal("0")
    failure_buffer_bps: Decimal = Decimal("0")


@dataclass(frozen=True)
class Leg:
    venue: str
    symbol: str
    side: Side
    price: Decimal
    quantity: Decimal
    notional: Decimal


@dataclass(frozen=True)
class Opportunity:
    opportunity_id: str
    symbol: str
    buy: Leg
    sell: Leg
    gross_edge_bps: Decimal
    net_edge_bps: Decimal
    expected_profit_quote: Decimal
    created_ts_ms: int
    expires_ts_ms: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Intent:
    strategy: str
    symbol: str
    legs: tuple[Leg, ...]
    net_edge_bps: Decimal
    expected_profit_quote: Decimal
    ttl_ms: int
    created_ts_ms: int
    metadata: dict[str, Any] = field(default_factory=dict)
