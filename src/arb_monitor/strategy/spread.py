from __future__ import annotations

import hashlib
from decimal import Decimal
from collections.abc import Iterable, Sequence

from arb_monitor.core.math import net_edge, quantity_for_budget, walk_book
from arb_monitor.core.models import FeeModel, Intent, Leg, OrderBook, Side


class CrossVenueSpreadStrategy:
    """Simple executable top-of-book/depth scanner, intentionally no I/O."""

    name = "cross_venue_spread"

    def __init__(
        self,
        buy_venue: str,
        sell_venue: str,
        fee: FeeModel,
        max_notional_quote: Decimal,
        min_net_edge_bps: Decimal,
        min_profit_quote: Decimal,
        ttl_ms: int = 1000,
        max_age_ms: int = 1500,
    ):
        self.buy_venue = buy_venue
        self.sell_venue = sell_venue
        self.fee = fee
        self.max_notional_quote = max_notional_quote
        self.min_net_edge_bps = min_net_edge_bps
        self.min_profit_quote = min_profit_quote
        self.ttl_ms = ttl_ms
        self.max_age_ms = max_age_ms

    def on_snapshot(
        self, books: Sequence[OrderBook], now_ms: int
    ) -> Iterable[Intent]:
        by_key = {(b.venue, b.symbol): b for b in books}
        symbols = {b.symbol for b in books}
        for symbol in symbols:
            buy_book = by_key.get((self.buy_venue, symbol))
            sell_book = by_key.get((self.sell_venue, symbol))
            if not buy_book or not sell_book:
                continue
            if not all(b.is_fresh(now_ms, self.max_age_ms) for b in (buy_book, sell_book)):
                continue
            if not buy_book.asks or not sell_book.bids:
                continue
            buy_price = buy_book.asks[0].price
            sell_price = sell_book.bids[0].price
            if sell_price <= buy_price:
                continue

            max_qty = quantity_for_budget(buy_book.asks, self.max_notional_quote)
            buy_capacity, _ = walk_book(buy_book.asks, max_qty)
            sell_capacity, _ = walk_book(sell_book.bids, buy_capacity)
            quantity = min(buy_capacity, sell_capacity)
            if quantity <= Decimal("0"):
                continue
            buy_qty, buy_notional = walk_book(buy_book.asks, quantity)
            sell_qty, sell_notional = walk_book(sell_book.bids, quantity)
            if buy_qty < quantity or sell_qty < quantity:
                continue
            avg_buy = buy_notional / quantity
            avg_sell = sell_notional / quantity
            notional = quantity * avg_buy
            gross, net, profit = net_edge(avg_buy, avg_sell, self.fee, notional)
            if net < self.min_net_edge_bps or profit < self.min_profit_quote:
                continue
            digest = hashlib.sha1(
                f"{self.name}|{symbol}|{self.buy_venue}|{self.sell_venue}".encode()
            ).hexdigest()[:16]
            yield Intent(
                strategy=self.name,
                symbol=symbol,
                legs=(
                    Leg(self.buy_venue, symbol, Side.BUY, avg_buy, quantity, notional),
                    Leg(
                        self.sell_venue,
                        symbol,
                        Side.SELL,
                        avg_sell,
                        quantity,
                        quantity * avg_sell,
                    ),
                ),
                net_edge_bps=net,
                expected_profit_quote=profit,
                ttl_ms=self.ttl_ms,
                created_ts_ms=now_ms,
                metadata={"gross_edge_bps": str(gross), "dedupe_key": digest},
            )
