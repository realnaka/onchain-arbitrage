from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .adapters import Balance, InstrumentSpec, OrderRequest, OrderResult


@dataclass
class ReadOnlyCEXAdapter:
    """Safe default adapter; every mutating operation fails closed."""

    venue: str
    instruments: dict[str, InstrumentSpec]
    balances: list[Balance]
    allow_trading: bool = False

    def get_balances(self) -> list[Balance]:
        return list(self.balances)

    def get_instrument(self, symbol: str) -> InstrumentSpec:
        return self.instruments[symbol]

    def place_order(self, request: OrderRequest) -> OrderResult:
        if not self.allow_trading:
            raise PermissionError("CEX trading is disabled; use paper/demo adapter")
        raise NotImplementedError("live CEX adapter not configured")

    def cancel_order(self, symbol: str, order_id: str) -> None:
        if not self.allow_trading:
            raise PermissionError("CEX trading is disabled")
        raise NotImplementedError

    def get_order(self, symbol: str, order_id: str) -> OrderResult:
        raise NotImplementedError("private order endpoint not configured")


class BinanceFuturesAdapter(ReadOnlyCEXAdapter):
    """Interface placeholder for Binance USDⓈ-M Futures private API."""
    venue = "binance"


class OKXPerpetualAdapter(ReadOnlyCEXAdapter):
    """Interface placeholder for OKX SWAP private API."""
    venue = "okx"
