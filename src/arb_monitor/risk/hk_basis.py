from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from arb_monitor.core.hk import (
    DEFAULT_MAX_PEG_DEVIATION_BPS, DEFAULT_MAX_QUOTE_CONVERSION_AGE_MS, Contract,
    PairMapping, RoundTripCosts, SpotQuote,
)
from arb_monitor.core.markets import SUPPORTED_MARKETS, UNKNOWN, normalize_market
from arb_monitor.core.models import OrderBook


@dataclass(frozen=True)
class BasisLimits:
    total_capital: Decimal = Decimal("10000")
    spot_allocation: Decimal = Decimal("5000")
    margin_allocation: Decimal = Decimal("5000")
    max_age_ms: int = 15000
    max_pair_skew_ms: int = 10000
    max_unhedged_bps: Decimal = Decimal("5")
    # Unhedged long dust whose cash value is at or under this is still an
    # entry. A short larger than the spot has no cash tolerance.
    max_unhedged_quote_usdt: Decimal = Decimal("0.5")
    min_net_edge_bps: Decimal = Decimal("20")
    min_profit: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if not 0 < self.total_capital <= Decimal("10000"):
            raise ValueError("initial capital must be in (0, 10000] USDT")
        if min(self.spot_allocation, self.margin_allocation) <= 0:
            raise ValueError("capital allocations must be positive")
        if self.spot_allocation + self.margin_allocation > self.total_capital:
            raise ValueError("capital split exceeds total capital")
        if (min(self.max_age_ms, self.max_pair_skew_ms) <= 0
                or self.max_unhedged_bps < 0 or self.max_unhedged_quote_usdt < 0):
            raise ValueError("invalid freshness/exposure limits")


def input_rejections(
    contract: Contract, mapping: PairMapping, costs: RoundTripCosts,
    buy: SpotQuote | None, sell: SpotQuote | None, book: OrderBook | None,
    limits: BasisLimits, now_ms: int,
    *,
    listing_market: str = "",
    require_quote_conversion: bool = False,
    max_quote_conversion_age_ms: int = DEFAULT_MAX_QUOTE_CONVERSION_AGE_MS,
    max_peg_deviation_bps: Decimal | None = DEFAULT_MAX_PEG_DEVIATION_BPS,
) -> list[str]:
    """Everything that has to hold before a pair may be called a candidate.

    The two market-aware arguments are additive and default to the historical
    Hong Kong behaviour, so an existing HK call site is unaffected. For a US
    listing the strategy passes ``require_quote_conversion=True``: a US pool is
    usually denominated in USDG/USDT0/USDC, and the stablecoin→USDT rate has to
    be an observation with a source and an expiry rather than an assumed 1.0.
    """
    reasons = []
    if mapping.asset != contract.asset:
        reasons.append("mapping_asset_mismatch")
    market = normalize_market(
        listing_market or getattr(mapping, "listing_market", "") or "")
    if market not in SUPPORTED_MARKETS:
        # Fail closed on a market we cannot attribute. This is not a
        # cash-session rule: it says the evidence chain has no venue to bind to.
        reasons.append("unknown_listing_market")
    if not contract.active:
        reasons.append("inactive_contract")
    if contract.payout_model != "usd_linear":
        reasons.append("quanto_or_unknown_payout_requires_separate_hedge_model")
    if not mapping.valid(now_ms):
        reasons.append("unverified_or_expired_share_ratio_fx_mapping")
    # The venue's own contract size versus the value the ratio was reviewed
    # against. With 1:1 everywhere this never fires; the moment a venue changes
    # ctMult (or we map a fractional contract) it fires instead of mis-sizing.
    observed = getattr(mapping, "perp_contract_multiplier", None)
    if observed is not None:
        try:
            observed = Decimal(str(observed))
        except (InvalidOperation, ValueError):
            observed = None
        if observed is None or observed <= 0 or observed != contract.multiplier:
            reasons.append("perp_contract_size_mismatch")
    if require_quote_conversion:
        conversion = getattr(mapping, "quote_conversion", None)
        if conversion is None:
            reasons.append("quote_conversion_missing")
        else:
            ok, reason = conversion.usable(
                now_ms, max_age_ms=max_quote_conversion_age_ms,
                max_peg_deviation_bps=max_peg_deviation_bps)
            if not ok:
                reasons.append(reason)
    if not costs.verified or costs.valid_until_ms <= now_ms:
        reasons.append("unverified_or_expired_full_roundtrip_costs")
    if book is None or not book.is_fresh(now_ms, limits.max_age_ms):
        reasons.append("missing_or_stale_perp_book")
    for side, quote in (("buy", buy), ("sell", sell)):
        if quote is None or not quote.is_fresh(now_ms, limits.max_age_ms):
            reasons.append(f"missing_or_stale_spot_{side}_quote")
        if quote is not None and quote.gas_quote is None and costs.roundtrip_gas_budget_usdt is None:
            reasons.append(f"unknown_{side}_gas_in_quote_currency")
    if book and buy and sell:
        if (buy.asset != contract.asset or sell.asset != contract.asset
                or buy.side != "buy" or sell.side != "sell"
                or buy.chain_id != sell.chain_id or buy.quote_address != sell.quote_address):
            reasons.append("spot_asset_direction_chain_mismatch")
        times = [book.venue_ts_ms, buy.request_ts_ms, sell.request_ts_ms]
        if None in times or max(times) - min(times) > limits.max_pair_skew_ms:
            reasons.append("non_simultaneous_quotes")
        if buy.token_quantity != sell.token_quantity:
            reasons.append("spot_exit_quote_quantity_mismatch")
        if buy.quote_currency != sell.quote_currency or buy.token_address != sell.token_address:
            reasons.append("spot_quote_identity_mismatch")
    return reasons
