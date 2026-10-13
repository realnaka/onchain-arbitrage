from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from .adapters import OrderRequest


class OrderPolicy:
    """Future home for venue/strategy-specific order rules."""
    def build_open(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderRequest:
        return OrderRequest(symbol, side, quantity, "ioc", price=price,
                            client_order_id="arb" + "open" + "0" * 8)

    def build_close(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderRequest:
        return OrderRequest(symbol, side, quantity, "ioc", price=price,
                            reduce_only=True, client_order_id="arb" + "close" + "0" * 8)
