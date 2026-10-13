"""One conversion between contracts, shares and tokens.

The review of 2026-09-16 found the residual formula written three different
ways in three places:

* ``maker_first.open()`` floored ``filled * multiplier`` with the **contract**
  lot step (``plan.perp_lot_size``), which is only equal to the share step when
  the multiplier happens to be 1;
* ``order_flows.maker_first_open()`` computed the residual as
  ``plan.perp_contract_quantity - spot.token_quantity`` — contracts minus
  tokens, and against the planned rather than the filled contract count;
* ``full_chain._settle_perp()`` compared spot tokens with
  ``filled * multiplier`` directly.

With a 1:1 multiplier all three agree, which is exactly why the disagreement
went unnoticed: every asset in the current allowlist has ``ctVal=1`` and
``multiplier()=1``. The moment one does not — or a partial fill lands — they
disagree, and the number they disagree about is the unhedged residual, which is
the number this project refuses to get wrong.

So the arithmetic lives here, once, in named units:

``contracts``  what the venue counts, an integer multiple of ``contract_step``
``shares``     what the wrapper is collateralised against, ``contracts * multiplier``
``tokens``     what the wallet holds; the on-chain step is ``share_step``

``QuantitySpec.from_plan()`` builds the spec from a plan so callers do not have
to remember which field is which, and refuses to guess when a step is missing.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

__all__ = ["QuantitySpec", "QuantityError"]


class QuantityError(RuntimeError):
    """A quantity could not be converted without guessing."""


def _floor_step(value: Decimal, step: Decimal) -> Decimal:
    if step is None or step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _field(plan, name: str, default: Decimal) -> Decimal:
    """A plan's own value, or the default when it is absent/None.

    ``or`` is wrong here: a zero value is falsy, and reading "0" as "missing"
    would silently substitute 1 for a multiplier the caller deliberately set to
    zero — the difference between "no hedge exists" and "one share per
    contract". Zero is refused loudly by the callers instead.
    """
    value = getattr(plan, name, None)
    if value is None or value == "":
        return default
    return Decimal(str(value))


@dataclass(frozen=True)
class QuantitySpec:
    """How many shares one contract is worth, and how to round each side."""

    shares_per_contract: Decimal = Decimal("1")
    contract_step: Decimal = Decimal("1")
    # The step the *token* side has to land on. Defaults to the share step that
    # one contract corresponds to, which is what "whole shares" means for a
    # 1:1 contract.
    token_step: Decimal = Decimal("1")
    min_contracts: Decimal = Decimal("0")

    @classmethod
    def from_plan(cls, plan) -> "QuantitySpec":
        multiplier = _field(plan, "perp_contract_multiplier", Decimal("1"))
        if multiplier <= 0:
            raise QuantityError("contract_multiplier_must_be_positive")
        contract_step = _field(plan, "perp_lot_size", Decimal("1"))
        if contract_step <= 0:
            raise QuantityError("contract_step_must_be_positive")
        explicit = getattr(plan, "token_step", None)
        token_step = (Decimal(str(explicit)) if explicit
                      else contract_step * multiplier)
        minimum = _field(plan, "perp_min_size", Decimal("0"))
        return cls(shares_per_contract=multiplier, contract_step=contract_step,
                   token_step=token_step, min_contracts=minimum)

    def shares_for_contracts(self, contracts: Decimal) -> Decimal:
        """Shares a contract count corresponds to, before any step rounding."""
        return Decimal(contracts) * self.shares_per_contract

    def tokens_for_contracts(self, contracts: Decimal) -> Decimal:
        """Tokens a contract count *covers*, floored to the token step.

        This is the hedge size: rounding down is deliberate, because a token
        count larger than the hedge would be an unhedged long and the project
        never rounds a hedge up.
        """
        return _floor_step(self.shares_for_contracts(contracts), self.token_step)

    def contracts_for_tokens(self, tokens: Decimal) -> Decimal:
        """Contracts a token count can be hedged with, floored to the contract
        step. Rounding down means the hedge never exceeds the spot."""
        return _floor_step(Decimal(tokens) / self.shares_per_contract,
                           self.contract_step)

    def residual_tokens(self, filled_contracts: Decimal,
                        received_tokens: Decimal) -> Decimal:
        """Received tokens minus the tokens the *filled* contracts cover.

        Positive: unhedged long dust (mild, still reported).
        Negative: naked short of the hedge (never allowed).
        """
        return Decimal(received_tokens) - self.tokens_for_contracts(filled_contracts)

    def residual_bps(self, filled_contracts: Decimal,
                     received_tokens: Decimal) -> Decimal:
        """That residual as bps of the hedged token count.

        Token units over token units, so the price cancels and the number is
        the same for any asset: 0.000752 shares against a 3-share hedge is
        2.5 bps whether the share costs 3 or 300.
        """
        covered = self.tokens_for_contracts(filled_contracts)
        if covered <= 0:
            return Decimal(0)
        return (self.residual_tokens(filled_contracts, received_tokens)
                / covered * Decimal(10000))
