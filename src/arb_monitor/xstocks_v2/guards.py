"""Invariants taken from docs/lessons. These functions do not trade."""
from __future__ import annotations

from decimal import Decimal

# Measured SPL transfer into a Kraken deposit account, including rent.
FIRST_SOL_TRANSFER = Decimal("0.00313")
REPEAT_SOL_TRANSFER = Decimal("0.000015")


def sol_transfer_fee(*, first_for_mint: bool) -> Decimal:
    """The first transfer is not the base fee. 0.000015 SOL is only the repeat."""
    return FIRST_SOL_TRANSFER if first_for_mint else REPEAT_SOL_TRANSFER


def per_share_price(per_token: Decimal, shares_per_token: Decimal) -> Decimal:
    """``spot / quantity`` is the token price. A share divides by the multiplier."""
    if shares_per_token <= 0:
        raise ValueError("shares_per_token 必须大于 0")
    return per_token / shares_per_token


def balance_is_known(value: Decimal | None) -> bool:
    """A failed read is unknown. It is not a zero balance."""
    return value is not None


def dust_counts_as_credit(delta: Decimal, minimum: Decimal) -> bool:
    """A dust increase is not a deposit. The delta must clear the minimum."""
    return delta >= minimum


def may_deposit_to_kraken(amount: Decimal, deposit_min: Decimal) -> bool:
    """Below the deposit minimum the coins are lost. Keep the probe in our wallet."""
    return amount >= deposit_min


def arrival_confirmed(*, source_simulated: bool, dest_balance_increased: bool) -> bool:
    """A source-chain simulation does not mean the other chain received the tokens."""
    del source_simulated
    return dest_balance_increased


def route_duration_known(seconds: int | None) -> bool:
    """An unmeasured bridge time is not 'a few minutes'."""
    return seconds is not None


def sandwich_exposure(slippage_bps: Decimal, notional: Decimal) -> Decimal:
    return notional * slippage_bps / Decimal(10000)


def needs_same_chain_redeem(token_kind: str) -> bool:
    return token_kind == "wrapper"


def phase1_sell_is_taker(symbol: str) -> bool:
    """V2 phase 1 does not rest maker sells. XOPx is included."""
    del symbol
    return True


def sol_may_fund(destination: str) -> bool:
    """SOL goes to the wallet. Funding an ATA address can destroy that mint's account."""
    return destination == "wallet"


def drop_bad_row(rows: list[dict]) -> list[dict]:
    """One bad instrument is reported and skipped. It does not drop the venue."""
    return [row for row in rows if not row.get("bad")]


def quote_age_field(name: str) -> bool:
    """``quote_max_age_vs_budget_ms`` is batch time versus budget, not quote age."""
    return name in {"max_age_ms", "recv_age_ms"}


def reconciliation_ok(*, predicted: Decimal, realized: Decimal, notional: Decimal,
                      max_bps: Decimal = Decimal("15")) -> bool:
    """Sign must match. The denominator is notional, not the small net."""
    if predicted == 0 or realized == 0:
        return predicted == realized
    if (predicted > 0) != (realized > 0):
        return False
    if notional <= 0:
        return False
    gap = abs(realized - predicted) / notional * Decimal(10000)
    return gap <= max_bps
