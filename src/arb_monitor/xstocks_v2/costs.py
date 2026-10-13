"""Path cost. A missing term is unknown, never zero.

One net per row, for every consumer. Measured 2026-10-07: the scan charged the
Kraken taker fee only for paths that actually trade on Kraken, while the live
gate charged it for *every* bridged family -- so the same ``chain_to_chain``
row had two nets 10 bps (0.20 USDT at a 200 ticket) apart. The board printed
one and the gate judged the other. Both now ask this module.
"""
from __future__ import annotations

from decimal import Decimal

#: The families that never touch Kraken, so its taker fee is not a cost of their
#: path. Two of them appear under two names each: the ledger calls the bridged
#: directions ``chain_to_chain``, while the executor that walks them calls them
#: ``xlayer_via_bridge_to_solana`` / ``solana_via_bridge_to_xlayer``. Charging
#: the taker on any of these is a phantom 10 bps that hides opportunities
#: instead of protecting money.
NO_KRAKEN_FAMILIES = frozenset({
    "chain_to_chain",
    "dex_to_dex",
    "xlayer_via_bridge_to_solana",
    "solana_via_bridge_to_xlayer",
})

#: The families whose path trades on Kraken, so its taker fee is real.
#: ``chain_to_kraken`` sells into the Kraken book; ``kraken_to_chain`` buys from
#: it; the two ``bridge`` variants have one Kraken leg each (buy or sell).
KRAKEN_TAKER_FAMILIES = frozenset({
    "chain_to_kraken",
    "kraken_to_chain",
    "chain_via_bridge_to_kraken",
    "kraken_via_bridge_to_chain",
})


def path_cost(parts: list[Decimal | None]) -> Decimal | None:
    if any(part is None for part in parts):
        return None
    total = Decimal(0)
    for part in parts:
        total += part
    return total


def has_kraken_leg(family: str | None) -> bool:
    """Does this family trade on Kraken, so its taker fee applies?

    Only the families in ``NO_KRAKEN_FAMILIES`` are exempt. An unknown or
    missing family charges the fee: a missing cost loses money, an extra cost
    only misses a trade, and this repo's rule is that an unknown term is never
    the cheaper one.
    """
    if not family:
        return True
    return str(family) not in NO_KRAKEN_FAMILIES


def proportional_bps(policy, *, family: str | None = None) -> Decimal:
    """The bps of notional a row pays whatever its size is."""
    bps = Decimal(str(policy.slippage_buffer_bps))
    if has_kraken_leg(family):
        bps += Decimal(str(policy.kraken_taker_bps))
    return bps


def proportional_parts(policy, *, notional, family: str | None = None
                       ) -> tuple[Decimal, Decimal]:
    """``(taker_usdt, buffer_usdt)`` -- the proportional term, split for display.

    The ledger and the board show the two lines separately, so they are derived
    here rather than multiplied out again at each reader's site. Both parts are
    charged on the notional, which is the size we know before the trade.
    """
    notional = Decimal(str(notional))
    buffer = notional * Decimal(str(policy.slippage_buffer_bps)) / Decimal(10000)
    taker = (notional * Decimal(str(policy.kraken_taker_bps)) / Decimal(10000)
             if has_kraken_leg(family) else Decimal(0))
    return taker, buffer


#: ``gas_cost_usd`` left out means "this path has no gas line to add" (0).
#: Passing ``None`` means "the gas is unknown", which refuses the row -- the two
#: are different, so the default is a sentinel rather than ``None``.
_NO_GAS_LINE = object()


def net_after_costs(*, gross_usdt, notional_usdt, fixed_cost_usd, policy,
                    family: str | None = None,
                    gas_cost_usd=_NO_GAS_LINE) -> Decimal | None:
    """``gross - fixed - proportional``, or ``None`` when a term is unknown.

    ``fixed_cost_usd`` is the per-message bridge fee (and any measured transfer
    fee) and is charged once, at any size. ``gas_cost_usd`` is the on-chain gas
    for the path's EVM action, kept separate so a reader can see it; a ``None``
    there means "not known" and makes the whole net unknown, exactly like a
    missing bridge fee. ``proportional`` is the slippage buffer plus the Kraken
    taker fee when the path has a Kraken leg (``family``).
    """
    if gross_usdt is None or notional_usdt is None or fixed_cost_usd is None:
        return None
    if gas_cost_usd is _NO_GAS_LINE:
        gas_cost_usd = Decimal(0)
    elif gas_cost_usd is None:
        return None          # unknown gas is a refusal, not a free action
    notional = Decimal(str(notional_usdt))
    fixed = Decimal(str(fixed_cost_usd))
    if gas_cost_usd is not None:
        fixed += Decimal(str(gas_cost_usd))
    return (Decimal(str(gross_usdt)) - fixed
            - notional * proportional_bps(policy, family=family) / Decimal(10000))


def judged_notional(notional, available_usdt, *,
                    safety: Decimal = Decimal("0.999")) -> Decimal | None:
    """The size a gate judges a fixed-fee path at, or ``None`` to refuse.

    A fixed fee makes the same row profitable at one size and not at another, so
    the gate must judge it at the money the wallet can actually spend. An
    unreadable or empty balance is a refusal -- never the policy notional
    (measured 2026-10-07: 8.481916 USDC of real balance vs a 200 USDT default).
    ``safety`` leaves room for fees the buy leg has not charged yet.
    """
    if available_usdt is None:
        return Decimal(str(notional))
    available = Decimal(str(available_usdt))
    if available <= 0:
        return None
    return min(Decimal(str(notional)), available * safety)


#: One on-chain action on an EVM chain costs this many gas units, per the
#: measured policy (``evm_gas_units``: approve + swap + deposit, 2026-10-07).
def gas_cost_usd(policy, *, chain: str, gas_price_wei=None,
                 native_usd=None, native_as_of_ms=None,
                 now_ms=None, max_native_age_ms=None) -> tuple[Decimal | None, str]:
    """What one on-chain action on this chain costs, in USD.

    Solana is 0 here on purpose: its network fee is a measured per-transfer line
    (``guards.sol_transfer_fee``), not a gas market. An EVM chain needs both a
    gas price and the native token's USD price -- missing either is unknown, and
    unknown is a refusal, never a free action.

    The native token's price carries its own **age check** (A2, 2026-10-09). It
    used to be read straight out of the price file, so a 19.7 hour old OKB price
    still produced a confident "one X Layer action = 0.00114 USDT" while the
    same file's bridge fees had already gone ``gas_price_stale``. A number whose
    age is past the allowance is not a number: give ``native_as_of_ms`` and the
    two limits, and a stale price answers ``native_price_stale`` instead.

    The quoted price of a swap already contains the pool fee and the price
    impact; what it does not contain is gas. That is the term this adds, and it
    is why a bridge row's net is lower than its spread by a hair.
    """
    if str(chain or "").lower() in {"solana", "ton"}:
        return Decimal(0), "not_applicable"
    if gas_price_wei is None or native_usd is None:
        return None, "gas_unknown"
    if (native_as_of_ms is not None and now_ms is not None
            and max_native_age_ms is not None
            and now_ms - int(native_as_of_ms) > int(max_native_age_ms)):
        return None, "native_price_stale"
    units = Decimal(str(policy.evm_gas_units))
    return (units * Decimal(str(gas_price_wei)) / (Decimal(10) ** 18)
            * Decimal(str(native_usd))), "estimated"


def net_clears(*, edge_bps: Decimal | None, net_usdt: Decimal | None,
               min_bps: Decimal, min_usdt: Decimal) -> bool:
    if edge_bps is None or net_usdt is None:
        return False
    return edge_bps >= min_bps and net_usdt >= min_usdt
