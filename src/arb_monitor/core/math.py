from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Iterable

from .models import FeeModel, Level

ZERO = Decimal("0")
HUNDRED = Decimal("100")
TEN_THOUSAND = Decimal("10000")


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= ZERO:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def walk_book(levels: Iterable[Level], quantity: Decimal) -> tuple[Decimal, Decimal]:
    """Return (filled quantity, quote notional) for a marketable order."""
    remaining = quantity
    filled = ZERO
    notional = ZERO
    for level in levels:
        if remaining <= ZERO:
            break
        if level.price <= ZERO or level.quantity < ZERO:
            raise ValueError("invalid order-book level")
        take = min(remaining, level.quantity)
        filled += take
        notional += take * level.price
        remaining -= take
    return filled, notional


def quantity_for_budget(levels: Iterable[Level], budget: Decimal) -> Decimal:
    """Walk asks without exceeding the actual quote budget at deeper prices."""
    remaining, quantity = max(ZERO, budget), ZERO
    for level in levels:
        if level.price <= ZERO or level.quantity < ZERO:
            raise ValueError("invalid order-book level")
        take = min(level.quantity, remaining / level.price)
        quantity += take
        remaining -= take * level.price
        if remaining <= ZERO:
            break
    return quantity


def effective_fee_bps(fee: FeeModel) -> Decimal:
    return fee.buy_bps + fee.sell_bps + fee.slippage_bps + fee.failure_buffer_bps


def net_edge(
    buy_price: Decimal,
    sell_price: Decimal,
    fee: FeeModel,
    notional: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return gross bps, net bps and expected quote profit."""
    if buy_price <= ZERO or sell_price <= ZERO or notional <= ZERO:
        return ZERO, ZERO, ZERO
    gross_bps = (sell_price - buy_price) / buy_price * TEN_THOUSAND
    sell_notional = notional * sell_price / buy_price
    costs = (
        notional * (fee.buy_bps + fee.slippage_bps + fee.failure_buffer_bps)
        + sell_notional * fee.sell_bps
    ) / TEN_THOUSAND + fee.gas_quote + fee.funding_quote
    profit = sell_notional - notional - costs
    net_bps = profit / notional * TEN_THOUSAND
    return gross_bps, net_bps, profit
