"""Can this wallet pay for the next signature? Decide it *before* signing.

A94 (2026-10-09). The wallet service reserves ``gas limit x its own fee cap``
before it signs, and refuses with ``10004`` when the native balance is below
that. Measured on Ethereum: ``balance 246927887542942, tx cost 539589813939648``
-- 0.000247 ETH against 0.00054 ETH -- while ``eth_gasPrice`` read **0.10
gwei**. The wallet prices its own cap, not the chain's, so a reserve computed
from the network price alone passes wallets the signer is about to refuse.

That refusal used to be classified as ``unknown`` and halted the whole live
runner for two hours (A99). ``10004`` with an unchanged nonce is now a
``refused`` (A95); this module is the half that stops the call from happening
at all, and names the reason ``gas_short`` so the board can say "top up gas"
instead of quoting a wallet error.

Nothing here signs, and nothing here guesses: an unreadable balance or an
unreadable gas price is a **refusal**, never a free action.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

#: The fee cap to reserve at, as a floor. Measured 2026-10-09: the wallet wanted
#: 0.00054 ETH for a swap while the network price was 0.10 gwei, i.e. ~1.8 gwei
#: over a 300k-gas limit. Keeping the measured value as a floor means the check
#: agrees with the signer on a quiet chain and still scales up when gas rises.
FEE_CAP_FLOOR_WEI = 1_800_000_000

#: Still generous on top of the network price, for the same reason.
FEE_CAP_MARGIN = Decimal("2")

#: Used when a leg does not hand us its own limit (the swap aggregator usually
#: does; the ERC-20 transfer leg passes 90 000 explicitly).
FALLBACK_GAS_UNITS = 300_000


def _int(value) -> int | None:
    """``int`` or ``None``. A string of digits counts; garbage does not."""
    if value in (None, ""):
        return None
    try:
        number = int(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number > 0 else None


def fee_cap_wei(gas_price_wei) -> int | None:
    """The fee cap this wallet will reserve at, or ``None`` if unreadable."""
    price = _int(gas_price_wei)
    if price is None:
        return None
    return max(int(Decimal(price) * FEE_CAP_MARGIN), FEE_CAP_FLOOR_WEI)


def need_native(*, gas_limit=None, gas_price_wei) -> Decimal | None:
    """Native wei the wallet reserves for one transaction, in whole coins."""
    cap = fee_cap_wei(gas_price_wei)
    if cap is None:
        return None
    units = _int(gas_limit) or FALLBACK_GAS_UNITS
    return Decimal(max(units, FALLBACK_GAS_UNITS)) * Decimal(cap) / Decimal(10 ** 18)


def shortfall(*, balance, gas_limit=None, gas_price_wei) -> str:
    """``""`` when the wallet can sign; otherwise the refusal reason.

    The reasons are deliberately not ``unknown``: nothing has been signed, so
    there is nothing to reconcile and nothing to halt for.
    """
    if balance in (None, ""):
        return "native_balance_unreadable"
    try:
        have = Decimal(str(balance))
    except (InvalidOperation, TypeError, ValueError):
        return "native_balance_unreadable"
    need = need_native(gas_limit=gas_limit, gas_price_wei=gas_price_wei)
    if need is None:
        return "gas_price_unreadable"
    if have < need:
        return f"gas_short:{have:f}<{need:f}"
    return ""
