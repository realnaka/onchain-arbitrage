from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any


class LegKind(str, Enum):
    SPOT = "spot"
    PERPETUAL = "perpetual"


class OrderType(str, Enum):
    IOC = "ioc"
    LIMIT = "limit"
    MARKET = "market"


class LiquidityRole(str, Enum):
    """How the leg takes liquidity. This project only ever takes."""

    TAKER = "taker"
    MAKER = "maker"


@dataclass(frozen=True)
class ExecutionLeg:
    kind: LegKind
    venue: str
    instrument: str
    side: str
    quantity: Decimal
    limit_price: Decimal | None = None
    order_type: OrderType = OrderType.IOC
    reduce_only: bool = False
    liquidity_role: LiquidityRole = LiquidityRole.TAKER
    quote_token: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionIntent:
    signal_id: str
    asset: str
    chain_id: str
    token_address: str
    quote_token_address: str
    spot_amount: Decimal
    expected_token_quantity: Decimal
    perp_contract_quantity: Decimal
    perp_venue: str
    perp_instrument: str
    net_edge_bps: Decimal
    expected_profit_quote: Decimal
    created_ts_ms: int
    expires_ts_ms: int
    max_slippage_bps: Decimal
    gas_budget_quote: Decimal
    # Both legs are taker. The perpetual leg is a bounded IOC priced off the
    # live best bid: it crosses the spread and cancels the remainder, so it can
    # never rest in the book as an accidental maker order.
    perp_limit_price: Decimal | None = None
    stablecoin_conversion_cost: Decimal = Decimal("0")
    funding_assumption: Decimal = Decimal("0")
    execution_allowed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_expired(self, now_ms: int) -> bool:
        return now_ms >= self.expires_ts_ms

    def legs(self) -> tuple[ExecutionLeg, ExecutionLeg]:
        return (
            ExecutionLeg(
                LegKind.PERPETUAL, self.perp_venue, self.perp_instrument, "sell",
                self.perp_contract_quantity, limit_price=self.perp_limit_price,
                order_type=OrderType.IOC,
            ),
            ExecutionLeg(
                LegKind.SPOT, "xlayer", self.token_address, "buy",
                self.expected_token_quantity, order_type=OrderType.LIMIT,
                quote_token=self.quote_token_address,
                metadata={"exact_in": str(self.spot_amount),
                          "liquidity_role": LiquidityRole.TAKER.value,
                          "execution": "exact_in_swap_against_pool_or_route"},
            ),
        )


def taker_limit_price(reference_price: Decimal, slippage_bps: Decimal,
                      *, side: str = "sell") -> Decimal:
    """Bounded IOC price for a taker order that must not rest in the book.

    Selling takes the best bid and is willing to pay down to
    ``reference × (1 − slippage)``; buying takes the best ask and is willing to
    pay up to ``reference × (1 + slippage)``. Anything outside the band simply
    does not fill, which is the intended failure mode for a taker hedge.
    """
    reference_price = Decimal(str(reference_price))
    slippage = Decimal(str(slippage_bps)) / Decimal(10000)
    if reference_price <= 0:
        raise ValueError("reference price must be positive")
    if slippage < 0:
        raise ValueError("slippage band must not be negative")
    factor = (Decimal(1) - slippage) if side == "sell" else (Decimal(1) + slippage)
    return reference_price * factor
