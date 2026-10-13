"""Is this X Layer pool actually tradeable, and at which of five states?

Several US xStocks pools on X Layer exist but hold nothing, and some hold a
little and have never printed a trade. A pool's ``slot0`` price is the price
the pool was *initialised* at, and a historical print is not a current quote,
so neither may become an execution price. Collapsing all of that into "we have
a pool" is how a router quote turns into a naked leg.

So the observation is classified explicitly, and only one of the states is
executable::

    no_pool                     no pool configured at all
    empty_pool                  pool exists, zero liquidity
    pool_without_liquidity      some state, depth below the reviewed floor
    prints_without_exact_in     trade prints exist, no current exact-in quote
    quote_unusable              the price we would size against may not be
                                used: it is stale, the read errored, or it is
                                denominated in a stablecoin whose USDT rate is
                                missing or untrustworthy
    exact_in_quote_available    a fresh exact-in quote we may size against

Read-only and dependency-free: the caller supplies what it read.

The conversion check runs *before* the "do we have a quote" check on purpose.
A stablecoin whose USDT rate is missing or untrusted is a blocker that no
amount of probing will clear, so it is reported even when there is also no
exact-in quote yet: otherwise the board would show "waiting for a quote" on a
pool that could never be priced.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from arb_monitor.core.hk import ZERO

NO_POOL = "no_pool"
EMPTY_POOL = "empty_pool"
POOL_WITHOUT_LIQUIDITY = "pool_without_liquidity"
PRINTS_WITHOUT_EXACT_IN = "prints_without_exact_in_quote"
QUOTE_UNUSABLE = "quote_unusable"
EXACT_IN_QUOTE_AVAILABLE = "exact_in_quote_available"

STATES = (NO_POOL, EMPTY_POOL, POOL_WITHOUT_LIQUIDITY, PRINTS_WITHOUT_EXACT_IN,
          QUOTE_UNUSABLE, EXACT_IN_QUOTE_AVAILABLE)

__all__ = ["PoolObservation", "PoolVerdict", "classify_pool", "STATES"]


@dataclass(frozen=True)
class PoolObservation:
    asset: str
    pool_address: str = ""
    #: Pool liquidity (Uniswap-V3 ``liquidity()``) — zero means initialised but
    #: unfunded, which is not the same as "no pool".
    liquidity: Decimal | None = None
    #: Depth in USDT computed from the pool's own state. Informational: it is
    #: *not* an execution price.
    depth_usd: Decimal | None = None
    #: ``slot0`` price. Recorded as evidence of initialisation only.
    slot0_price_usd: Decimal | None = None
    last_print_ts_ms: int = 0
    exact_in_quote_ts_ms: int = 0
    exact_in_quote_usdt: Decimal | None = None
    quote_conversion_ok: bool = False
    quote_conversion_reason: str = ""
    observed_ts_ms: int = 0
    error: str = ""


@dataclass(frozen=True)
class PoolVerdict:
    asset: str
    state: str
    executable: bool
    reasons: tuple[str, ...]
    detail: str = ""


def classify_pool(observation: PoolObservation, *, now_ms: int,
                  min_depth_usd: Decimal, max_quote_age_ms: int) -> PoolVerdict:
    """One state per pool, and exactly one of them is executable."""
    if observation.error:
        return PoolVerdict(observation.asset, NO_POOL, False,
                           ("pool_state_unreadable",), observation.error)
    if not observation.pool_address:
        return PoolVerdict(observation.asset, NO_POOL, False, ("no_pool",))
    liquidity = observation.liquidity
    depth = observation.depth_usd
    if (liquidity is not None and liquidity <= ZERO) or (
            depth is not None and depth <= ZERO):
        return PoolVerdict(observation.asset, EMPTY_POOL, False, ("empty_pool",),
                           f"liquidity={liquidity} depth_usd={depth}")
    if depth is None or depth < min_depth_usd:
        return PoolVerdict(
            observation.asset, POOL_WITHOUT_LIQUIDITY, False,
            ("pool_below_min_depth",),
            # Say it out loud: an initialisation price is not a price.
            f"depth_usd={depth} < min_pool_depth_usd={min_depth_usd}; "
            f"slot0_price_usd={observation.slot0_price_usd} is not an execution price")
    if not observation.quote_conversion_ok:
        # First, because a bad or missing stablecoin→USDT rate cannot be fixed
        # by quoting more: everything downstream would be priced with an
        # assumption we have already rejected.
        return PoolVerdict(
            observation.asset, QUOTE_UNUSABLE, False,
            (observation.quote_conversion_reason or "quote_token_conversion_untrusted",))
    if not observation.exact_in_quote_usdt or not observation.exact_in_quote_ts_ms:
        reason = ("prints_without_exact_in_quote"
                  if observation.last_print_ts_ms else "no_exact_in_quote")
        return PoolVerdict(
            observation.asset, PRINTS_WITHOUT_EXACT_IN, False, (reason,),
            f"last_print_ts_ms={observation.last_print_ts_ms or None}")
    if now_ms - observation.exact_in_quote_ts_ms > max(0, int(max_quote_age_ms)):
        return PoolVerdict(observation.asset, QUOTE_UNUSABLE, False,
                           ("exact_in_quote_stale",),
                           f"quote age {now_ms - observation.exact_in_quote_ts_ms}ms")
    return PoolVerdict(observation.asset, EXACT_IN_QUOTE_AVAILABLE, True, ())
