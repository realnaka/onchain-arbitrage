"""Token → contract sizing for a token-perp hedge, and the reasons it fails.

The plan's arithmetic, in named units and in one place::

    链上实际 token 数量
    × shares_per_token          (corporate actions: 1 token = N shares)
    ÷ shares_per_perp_unit      (how the venue's contract is quoted)
    ÷ contract_multiplier       (shares per contract)
    → 向下取整到 lot_size

What comes out is not just a contract count. A hedge that cannot be built
exactly leaves an unhedged residual, and this project treats a *negative*
residual (a short bigger than the spot it hedges) as a refusal rather than a
rounding detail. So the same call also reports:

* the residual in tokens and in bps of the hedged size,
* the smallest size this contract lets us actually send (``min_contracts``,
  raised by the venue's minimum notional),
* whether the wallet and the margin allocation can carry it.

Everything is ``Decimal`` and every threshold is passed in. Nothing here reads
a config, a venue or a wallet.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_UP

from arb_monitor.core.hk import BPS, ZERO

__all__ = [
    "SizingInputs", "SizingResult", "plan_size",
    "minimum_hedge_tokens", "largest_hedge_tokens",
]


def _floor_step(value: Decimal, step: Decimal) -> Decimal:
    if step is None or step <= ZERO:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _ceil_step(value: Decimal, step: Decimal) -> Decimal:
    if step is None or step <= ZERO:
        return value
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


def _exact_tokens_for_contracts(
    contracts: Decimal, *, shares_per_token: Decimal, shares_per_perp_unit: Decimal,
    contract_multiplier: Decimal, token_decimals: int,
) -> Decimal | None:
    """Token amount that hedges ``contracts`` with a zero residual, or None.

    The chain counts integer token units. A share count that is not an integer
    number of those units cannot be the exact-out size, and a token amount that
    does not convert back to exactly ``contracts`` would leave a residual the
    strategy refuses to call a hedge.
    """
    if contracts <= ZERO or shares_per_token <= ZERO:
        return None
    scale = Decimal(10) ** int(token_decimals)
    numer = contracts * shares_per_perp_unit * contract_multiplier * scale
    wei = numer / shares_per_token
    if wei != wei.to_integral_value() or wei <= ZERO:
        return None
    tokens = wei / scale
    desired = (tokens * shares_per_token / shares_per_perp_unit
               / contract_multiplier)
    if desired != contracts:
        return None
    return tokens


def _covering_tokens_for_contracts(
    contracts: Decimal, *, shares_per_token: Decimal, shares_per_perp_unit: Decimal,
    contract_multiplier: Decimal, token_decimals: int,
) -> Decimal | None:
    """Token amount that hedges at least ``contracts``.

    An exact integer-wei amount is used when one exists. A wrapper ratio that
    does not divide the contract count into an integer wei has no practical
    exact size; the next representable token (one wei, rounded up) still floors
    back to ``contracts``, so the short is not larger than the spot. That size
    is what the quote probe needs. A non-zero residual is still not an order.
    """
    exact = _exact_tokens_for_contracts(
        contracts, shares_per_token=shares_per_token,
        shares_per_perp_unit=shares_per_perp_unit,
        contract_multiplier=contract_multiplier, token_decimals=token_decimals)
    if exact is not None:
        return exact
    if contracts <= ZERO or shares_per_token <= ZERO:
        return None
    scale = Decimal(10) ** int(token_decimals)
    numer = contracts * shares_per_perp_unit * contract_multiplier * scale
    wei = (numer / shares_per_token).to_integral_value(rounding=ROUND_UP)
    if wei <= ZERO:
        return None
    tokens = wei / scale
    desired = (tokens * shares_per_token / shares_per_perp_unit
               / contract_multiplier)
    if desired < contracts:
        return None
    return tokens


def _minimum_contracts(
    *, lot_size: Decimal, min_contracts: Decimal, min_notional: Decimal,
    perp_price: Decimal, contract_multiplier: Decimal,
) -> Decimal:
    """Smallest legal contract count.

    OKX publishes ``minSz`` and ``lotSz`` in contracts. A USD minimum is applied
    only when the venue actually publishes one (``min_notional > 0``) and a
    price is already known. Zero is not a floor and it is not a guess: it means
    that field is absent.
    """
    if min(lot_size, min_contracts, contract_multiplier) <= ZERO:
        raise ValueError("contract minimum is unknown")
    contracts = _ceil_step(max(min_contracts, lot_size), lot_size)
    # A USD floor can only be converted into contracts once a price exists.
    # Without one, the contract-count floor still stands; the strategy applies
    # the USD check again when it has a book.
    if min_notional > ZERO and perp_price > ZERO:
        needed = min_notional / (perp_price * contract_multiplier)
        contracts = max(contracts, _ceil_step(needed, lot_size))
    return contracts


def minimum_hedge_tokens(
    *, shares_per_token: Decimal, shares_per_perp_unit: Decimal,
    contract_multiplier: Decimal, lot_size: Decimal, min_contracts: Decimal,
    min_notional: Decimal = ZERO, perp_price: Decimal = ZERO,
    token_decimals: int = 18,
) -> Decimal:
    """Smallest token amount that can be hedged with a legal order.

    The spot token is divisible down to its own decimals. The perpetual is not:
    the order has to be at least ``min_contracts`` and a multiple of ``lot_size``.
    Those contract fields are converted back into a token quantity here, so the
    probe and the order share one floor. A missing or non-positive contract
    field is refused rather than replaced with one whole share. When the share
    ratio has no exact wei amount, the size is the next representable token
    that still covers this contract count, so the name stays on the quote grid.
    """
    if shares_per_token <= ZERO or shares_per_perp_unit <= ZERO:
        raise ValueError("share ratio is unknown")
    if token_decimals < 0:
        raise ValueError("token_decimals")
    contracts = _minimum_contracts(
        lot_size=lot_size, min_contracts=min_contracts, min_notional=min_notional,
        perp_price=perp_price, contract_multiplier=contract_multiplier)
    tokens = _covering_tokens_for_contracts(
        contracts, shares_per_token=shares_per_token,
        shares_per_perp_unit=shares_per_perp_unit,
        contract_multiplier=contract_multiplier, token_decimals=token_decimals)
    if tokens is None:
        raise ValueError("no token amount for the contract minimum")
    return tokens


def largest_hedge_tokens(
    *, notional_cap: Decimal, perp_price: Decimal, shares_per_token: Decimal,
    shares_per_perp_unit: Decimal, contract_multiplier: Decimal, lot_size: Decimal,
    min_contracts: Decimal, min_notional: Decimal = ZERO, token_decimals: int = 18,
) -> Decimal:
    """Largest exact hedge whose contract notional fits under ``notional_cap``.

    Never smaller than :func:`minimum_hedge_tokens`. When the cap cannot buy
    even that minimum, the minimum is still returned: the cap is a ceiling, and
    the venue floor is what decides whether a quote is attempted at all.
    """
    floor = minimum_hedge_tokens(
        shares_per_token=shares_per_token, shares_per_perp_unit=shares_per_perp_unit,
        contract_multiplier=contract_multiplier, lot_size=lot_size,
        min_contracts=min_contracts, min_notional=min_notional, perp_price=perp_price,
        token_decimals=token_decimals)
    unit = perp_price * contract_multiplier
    if notional_cap <= ZERO or unit <= ZERO:
        return floor
    contracts = _floor_step(notional_cap / unit, lot_size)
    smallest = _minimum_contracts(
        lot_size=lot_size, min_contracts=min_contracts, min_notional=min_notional,
        perp_price=perp_price, contract_multiplier=contract_multiplier)
    if contracts < smallest:
        return floor
    tokens = _covering_tokens_for_contracts(
        contracts, shares_per_token=shares_per_token,
        shares_per_perp_unit=shares_per_perp_unit,
        contract_multiplier=contract_multiplier, token_decimals=token_decimals)
    if tokens is None or tokens < floor:
        return floor
    return tokens


@dataclass(frozen=True)
class SizingInputs:
    token_quantity: Decimal
    shares_per_token: Decimal
    shares_per_perp_unit: Decimal
    contract_multiplier: Decimal
    lot_size: Decimal
    min_contracts: Decimal
    min_notional: Decimal
    perp_price: Decimal
    spot_open_usdt: Decimal
    perp_open_usdt: Decimal
    perp_close_usdt: Decimal
    wallet_balance_usdt: Decimal | None = None
    margin_allocation_usdt: Decimal | None = None
    max_unhedged_bps: Decimal | None = None
    allow_fractional_contracts: bool = True

    def __post_init__(self) -> None:
        for name in ("shares_per_token", "shares_per_perp_unit",
                     "contract_multiplier", "lot_size"):
            value = getattr(self, name)
            if value is None or value <= ZERO:
                raise ValueError(f"{name} must be positive")
        if self.token_quantity is None or self.token_quantity < ZERO:
            raise ValueError("token_quantity must be non-negative")
        if (self.wallet_balance_usdt is not None
                and self.wallet_balance_usdt < ZERO):
            raise ValueError("wallet_balance_usdt must be non-negative")


@dataclass(frozen=True)
class SizingResult:
    desired_contracts: Decimal
    contracts: Decimal
    residual_tokens: Decimal
    # Residual as a share of what the contracts cover (``residual_bps``) and as
    # a share of what the token quantity asked for (``residual_delta_bps``).
    # The second is the number the strategy has always reported and the
    # configured ``max_unhedged_bps`` is applied to.
    residual_delta_bps: Decimal
    residual_bps: Decimal
    min_executable_contracts: Decimal
    min_executable_notional_usdt: Decimal
    wallet_limited: bool
    margin_limited: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.reasons


def plan_size(inputs: SizingInputs) -> SizingResult:
    """The contract count for a token quantity, plus everything that blocks it."""
    step = inputs.lot_size
    if not inputs.allow_fractional_contracts:
        step = max(step, Decimal(1))
    desired = (inputs.token_quantity * inputs.shares_per_token
               / inputs.shares_per_perp_unit / inputs.contract_multiplier)
    contracts = _floor_step(desired, step)
    reasons: list[str] = []
    if contracts < inputs.min_contracts:
        reasons.append("below_min_contracts")
    # Minimum *executable* size: the venue floor and the notional floor are two
    # different gates, and a contract whose minimum notional is 5 USDT cannot be
    # sent at one 0.01-share lot even if the lot itself is legal.
    floor = max(inputs.min_contracts, step)
    if inputs.min_notional > ZERO and inputs.perp_price > ZERO:
        # One contract is worth ``contract_multiplier`` shares at ``perp_price``.
        notional_floor = (inputs.min_notional
                          / (inputs.perp_price * inputs.contract_multiplier))
        floor = max(floor, _ceil_step(notional_floor, inputs.lot_size))
    min_executable_notional = floor * inputs.contract_multiplier * inputs.perp_price

    # Unhedged residual: tokens received minus the tokens the filled contracts
    # cover. Positive is unhedged long dust; negative is a naked short of the
    # hedge, which is never allowed.
    covered_tokens = (contracts * inputs.shares_per_perp_unit
                      * inputs.contract_multiplier / inputs.shares_per_token)
    residual_tokens = inputs.token_quantity - covered_tokens
    residual_bps = (residual_tokens / covered_tokens * BPS) if covered_tokens > ZERO else ZERO
    residual_delta_bps = ((desired - contracts) / desired * BPS) if desired > ZERO else ZERO
    if residual_tokens < ZERO:
        reasons.append("negative_unhedged_residual")
    if (inputs.max_unhedged_bps is not None
            and abs(residual_delta_bps) > inputs.max_unhedged_bps):
        reasons.append("contract_rounding_leaves_excess_directional_exposure")
    if inputs.min_notional > ZERO and min(inputs.perp_open_usdt,
                                          inputs.perp_close_usdt) < inputs.min_notional:
        reasons.append("below_min_notional")
    wallet_limited = (inputs.wallet_balance_usdt is not None
                      and inputs.spot_open_usdt > inputs.wallet_balance_usdt)
    margin_limited = (inputs.margin_allocation_usdt is not None
                      and max(inputs.perp_open_usdt, inputs.perp_close_usdt)
                      > inputs.margin_allocation_usdt)
    if wallet_limited:
        reasons.append("wallet_balance_below_spot_notional")
    if margin_limited:
        reasons.append("margin_allocation_below_perp_notional")
    return SizingResult(
        desired_contracts=desired, contracts=contracts,
        residual_tokens=residual_tokens, residual_delta_bps=residual_delta_bps,
        residual_bps=residual_bps,
        min_executable_contracts=floor,
        min_executable_notional_usdt=min_executable_notional,
        wallet_limited=wallet_limited, margin_limited=margin_limited,
        reasons=tuple(reasons),
    )
