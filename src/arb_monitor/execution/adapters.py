from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    contract_multiplier: Decimal
    min_quantity: Decimal
    quantity_step: Decimal
    price_tick: Decimal
    settle_currency: str
    linear: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Balance:
    currency: str
    available: Decimal
    total: Decimal


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: str
    quantity: Decimal
    order_type: str
    price: Decimal | None = None
    reduce_only: bool = False
    client_order_id: str | None = None


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    status: str
    filled_quantity: Decimal
    average_price: Decimal | None = None
    fee: Decimal = Decimal("0")
    metadata: dict[str, Any] = field(default_factory=dict)


class CEXAdapter(Protocol):
    venue: str
    def get_balances(self) -> list[Balance]: ...
    def get_instrument(self, symbol: str) -> InstrumentSpec: ...
    def place_order(self, request: OrderRequest) -> OrderResult: ...
    def cancel_order(self, symbol: str, order_id: str) -> None: ...
    def get_order(self, symbol: str, order_id: str) -> OrderResult: ...


class WalletAdapter(Protocol):
    chain_id: str
    def validate_token(self, address: str) -> dict[str, Any]: ...
    def get_balance(self, token_address: str) -> Decimal: ...
    def quote_exact_in(self, token_in: str, token_out: str, amount_in: Decimal) -> dict[str, Any]: ...
    def swap_exact_in(self, quote: dict[str, Any], amount_out_minimum: Decimal) -> OrderResult: ...
    def sell_exact_in(self, quote: dict[str, Any], amount_out_minimum: Decimal) -> OrderResult: ...
