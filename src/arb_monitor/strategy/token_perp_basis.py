"""Long stock token + short USD-linear perpetual — the generic basis strategy.

No IO; snapshot evaluation is replayable. A positive entry basis is NOT booked
profit. Future exit basis and four-leg costs are required for an entry Intent.
Retaining the short after the spot sale is deliberately not emitted here.

The strategy is not Hong Kong specific and never was: both legs quote around
the clock (an X Layer stock token and a CEX USD-linear perp), so the cash
session of the underlying is not a property of either leg. What differs
between an HK and a US listing is *evidence*, not arithmetic:

* which market the row is (``listing_market``), carried into every record so a
  US row can never be reported as a Hong Kong one;
* the underlying symbol and the on-chain token/wrapper addresses the ratio was
  read for;
* a quote-token → USDT conversion that must be an observation with a source and
  an expiry whenever the pool is denominated in USDG/USDT0/USDC rather than the
  probe's own quote token (see ``core.hk.QuoteConversion``).

``HKBasisStrategy`` in ``strategy.hk_basis`` is the historical entry point and
keeps the Hong Kong defaults (including the calendar-phase gate) so existing
configuration and tests behave exactly as before.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Sequence

from arb_monitor.core.hk import (
    BPS, DEFAULT_MAX_PEG_DEVIATION_BPS, DEFAULT_MAX_QUOTE_CONVERSION_AGE_MS,
    ZERO, Contract, PairMapping, RoundTripCosts, SpotQuote,
)
from arb_monitor.core.markets import HK, SUPPORTED_MARKETS, UNKNOWN, normalize_market
from arb_monitor.core.math import quantize_down, walk_book
from arb_monitor.core.models import Intent, Leg, OrderBook, Side
from arb_monitor.core.sizing import SizingInputs, plan_size
from arb_monitor.risk.hk_basis import BasisLimits, input_rejections


# Both legs of this strategy quote around the clock: the spot leg is a stock
# token on X Layer and the perp leg is a USD-linear contract on a CEX. Whether
# the cash market happens to be open is not a property of either leg, so it
# must not gate the strategy.
#
# Operator decision 2026-09-20 (twice, deliberately): the strategy trades
# 24/7. The list below is therefore every phase the calendar can produce.
# `closed` (weekends/holidays) and the continuous session were both excluded
# before; both are in scope now. An unrecognised phase string is still refused
# — that is fail-closed for a calendar we do not understand, not a cash-session
# rule. Reasoning: AGENTS.md "全天候原则".
#
# Operator decision 2026-09-22: this applies to US listings too. For a US row
# the calendar is a report grouping only and never a gate, so US behaviour does
# not depend on the HK holiday table at all.
STRATEGY_PHASES = frozenset({
    "before_0900",
    "preopen_0900_0930",
    "continuous_am",
    "lunch",
    "continuous_pm",
    "closing_auction",
    "after_close",
    "closed",
})

# Markets whose evidence chain carries a reviewed calendar. Only Hong Kong has
# one today; the gate exists for continuity with the pre-2026-09-22 behaviour
# and applies to nothing else.
CALENDAR_GATED_MARKETS = frozenset({HK})

SUPPORTED_LISTING_MARKETS = SUPPORTED_MARKETS
UNKNOWN_LISTING_MARKET = UNKNOWN


class TokenPerpBasisStrategy:
    name = "token_spot_long_perp_short"

    def __init__(
        self, contract: Contract, mapping: PairMapping, buy: SpotQuote | None,
        sell: SpotQuote | None, costs: RoundTripCosts, limits: BasisLimits,
        target_exit_basis_bps: Decimal | None = None,
        phase: str = "closed",
        *,
        listing_market: str = "",
        underlying_symbol: str = "",
        wallet_balance_usdt: Decimal | None = None,
        require_quote_conversion: bool | None = None,
        calendar_phase_gate: bool | None = None,
        max_peg_deviation_bps: Decimal | None = DEFAULT_MAX_PEG_DEVIATION_BPS,
        max_quote_conversion_age_ms: int = DEFAULT_MAX_QUOTE_CONVERSION_AGE_MS,
    ):
        self.contract, self.mapping, self.buy, self.sell = contract, mapping, buy, sell
        self.costs, self.limits = costs, limits
        self.target = target_exit_basis_bps
        self.phase = phase
        self.listing_market = normalize_market(
            listing_market or getattr(mapping, "listing_market", "") or "")
        self.underlying_symbol = str(
            underlying_symbol or getattr(mapping, "underlying_symbol", "") or "")
        self.wallet_balance_usdt = wallet_balance_usdt
        if require_quote_conversion is None:
            # HK rows were reviewed with an explicit quote_to_usdt in their own
            # evidence string; anything else has to carry the observation.
            require_quote_conversion = self.listing_market != HK
        self.require_quote_conversion = require_quote_conversion
        if calendar_phase_gate is None:
            calendar_phase_gate = self.listing_market in CALENDAR_GATED_MARKETS
        self.calendar_phase_gate = calendar_phase_gate
        self.max_peg_deviation_bps = max_peg_deviation_bps
        self.max_quote_conversion_age_ms = int(max_quote_conversion_age_ms)

    # -- identity ---------------------------------------------------------
    def identity(self) -> dict:
        """Which instrument this evaluation is about, and on what evidence."""
        return {
            "listing_market": self.listing_market,
            "underlying_symbol": self.underlying_symbol,
            "token_address": getattr(self.mapping, "token_address", "") or "",
            "wrapper_address": getattr(self.mapping, "wrapper_address", "") or "",
            "calendar_phase_is_a_gate": bool(self.calendar_phase_gate),
        }

    def contract_spec(self) -> dict:
        """The venue's own contract specification, as read from its catalogue."""
        c = self.contract
        return {
            "venue": c.venue, "instrument": c.instrument_id,
            "contract_multiplier": str(c.multiplier),
            "lot_size": str(c.lot_size), "min_contracts": str(c.min_contracts),
            "min_notional": str(c.min_notional), "tick_size": str(c.tick_size),
            "payout_model": c.payout_model, "active": bool(c.active),
            "source": c.source, "recv_ts_ms": c.recv_ts_ms,
        }

    # -- evaluation -------------------------------------------------------
    def evaluate(self, books: Sequence[OrderBook], now_ms: int) -> dict:
        c, m, buy, sell = self.contract, self.mapping, self.buy, self.sell
        book = next((b for b in books if (b.venue, b.symbol) == (c.venue, c.instrument_id)), None)
        reasons = input_rejections(
            c, m, self.costs, buy, sell, book, self.limits, now_ms,
            listing_market=self.listing_market,
            require_quote_conversion=self.require_quote_conversion,
            max_quote_conversion_age_ms=self.max_quote_conversion_age_ms,
            max_peg_deviation_bps=self.max_peg_deviation_bps,
        )
        result = {
            "asset": c.asset, "instrument": c.key, "status": "watch_only",
            "payout_model": c.payout_model, "phase": self.phase, "rejections": reasons,
            "entry_basis_bps": None, "current_exit_basis_bps": None,
            "immediate_roundtrip_net_usdt": None, "conditional_profit_usdt": None,
            **self.identity(),
            "contract_spec": self.contract_spec(),
        }
        conversion = getattr(m, "quote_conversion", None)
        if conversion is not None:
            ok, reason = conversion.usable(
                now_ms, max_age_ms=self.max_quote_conversion_age_ms,
                max_peg_deviation_bps=self.max_peg_deviation_bps)
            result["quote_conversion"] = {
                "quote_symbol": conversion.quote_symbol,
                "quote_address": conversion.quote_address,
                "quote_to_usdt": (None if conversion.quote_to_usdt is None
                                  else str(conversion.quote_to_usdt)),
                "source": conversion.source,
                "observed_ts_ms": conversion.observed_ts_ms,
                "valid_until_ms": conversion.valid_until_ms,
                "peg_deviation_bps": (None if conversion.peg_deviation_bps is None
                                      else str(conversion.peg_deviation_bps)),
                "usable": ok, "reason": reason,
            }
        if self.target is None:
            reasons.append("no_future_exit_basis_assumption")
        if self.calendar_phase_gate and self.phase not in STRATEGY_PHASES:
            reasons.append("outside_strategy_window")
        # Missing conversion is not filled with 1. Reference-only comparisons
        # live in the app report and never reach this path.
        if not m.valid(now_ms) or not buy or not sell or not book:
            return result
        if not buy.price or not sell.price or not book.bids or not book.asks:
            return result
        if c.payout_model != "usd_linear":
            return result
        quantity = buy.token_quantity
        desired_contracts = quantity * m.shares_per_token / m.shares_per_perp_unit / c.multiplier
        contracts = quantize_down(desired_contracts, c.lot_size)
        if contracts < c.min_contracts:
            reasons.append("below_min_contracts")
            return result
        filled_bid, raw_sell = walk_book(book.bids, contracts)
        filled_ask, raw_buyback = walk_book(book.asks, contracts)
        if filled_bid != contracts or filled_ask != contracts:
            reasons.append("insufficient_perp_depth")
            return result
        perp_open, perp_close = raw_sell * c.multiplier, raw_buyback * c.multiplier
        spot_open, spot_close = buy.quote_amount * m.quote_to_usdt, sell.quote_amount * m.quote_to_usdt
        unhedged = (desired_contracts - contracts) / desired_contracts * BPS
        covered_tokens = (contracts * m.shares_per_perp_unit * c.multiplier
                          / m.shares_per_token)
        residual_tokens = quantity - covered_tokens
        residual_quote = (residual_tokens * spot_open / quantity) if quantity > ZERO else None
        # Long dust at or under the cash cap is an acceptable hedge. A negative
        # residual is a naked short and stays refused at any dollar size.
        residual_within_cash = (residual_quote is not None and ZERO <= residual_quote
                                <= self.limits.max_unhedged_quote_usdt)
        if unhedged > self.limits.max_unhedged_bps and not residual_within_cash:
            reasons.append("contract_rounding_leaves_excess_directional_exposure")
        if min(perp_open, perp_close) < c.min_notional:
            reasons.append("below_min_notional")
        # Conservative 1x short collateral check; not an invented venue margin requirement.
        if spot_open > self.limits.spot_allocation or max(perp_open, perp_close) > self.limits.margin_allocation:
            reasons.append("capital_split_exceeded")
        entry_basis, exit_basis = perp_open - spot_open, perp_close - spot_close
        result.update(
            spot_open_usdt=spot_open, spot_close_usdt=spot_close,
            perp_open_usdt=perp_open, perp_close_usdt=perp_close,
            spot_quantity=quantity, contracts=contracts, residual_delta_bps=unhedged,
            residual_quote_usdt=residual_quote,
            entry_basis_bps=entry_basis / spot_open * BPS,
            current_exit_basis_bps=exit_basis / spot_open * BPS,
            immediate_roundtrip_gross_usdt=entry_basis - exit_basis,
        )
        # Everything the plan asks to be able to answer per asset: the residual
        # the contract grid leaves, the smallest size this contract may send,
        # and whether the wallet and the margin split can carry the pair.
        sizing = plan_size(SizingInputs(
            token_quantity=quantity, shares_per_token=m.shares_per_token,
            shares_per_perp_unit=m.shares_per_perp_unit,
            contract_multiplier=c.multiplier, lot_size=c.lot_size,
            min_contracts=c.min_contracts, min_notional=c.min_notional,
            # ``walk_book`` returns the *notional* of the levels it walked, so
            # the per-unit price is that notional divided by the contracts it
            # covers. Passing the notional here would understate the venue's
            # minimum-notional floor by a factor of the size.
            perp_price=(perp_open / contracts if contracts > ZERO else raw_sell),
            spot_open_usdt=spot_open,
            perp_open_usdt=perp_open, perp_close_usdt=perp_close,
            wallet_balance_usdt=self.wallet_balance_usdt,
            margin_allocation_usdt=self.limits.margin_allocation,
            max_unhedged_bps=self.limits.max_unhedged_bps,
        ))
        result.update(
            desired_contracts=sizing.desired_contracts,
            residual_tokens=sizing.residual_tokens,
            residual_bps=sizing.residual_bps,
            min_executable_contracts=sizing.min_executable_contracts,
            min_executable_notional_usdt=sizing.min_executable_notional_usdt,
            wallet_limited=sizing.wallet_limited,
            margin_limited=sizing.margin_limited,
            sizing_rejections=list(sizing.reasons),
        )
        if buy.gas_quote is None or sell.gas_quote is None:
            if self.costs.roundtrip_gas_budget_usdt is None:
                return result
            gas = self.costs.roundtrip_gas_budget_usdt
            result["gas_basis"] = "explicit_roundtrip_USDT_budget_not_provider_estimate"
        else:
            gas = (buy.gas_quote + sell.gas_quote) * m.quote_to_usdt
            result["gas_basis"] = "quote_currency_estimate"
        cost = self.costs.total(
            spot_open, spot_close, perp_open, perp_close, gas,
        )
        result.update(
            roundtrip_cost_usdt=cost,
            immediate_roundtrip_net_usdt=entry_basis - exit_basis - cost,
            break_even_exit_basis_bps=(entry_basis - cost) / spot_open * BPS,
        )
        # Costs use currently observed turnover as an explicit scenario estimate;
        # exit prices, funding and the eventual basis are not guaranteed.
        if self.target is not None:
            profit = entry_basis - spot_open * self.target / BPS - cost
            edge = profit / spot_open * BPS
            result.update(conditional_profit_usdt=profit, conditional_net_bps=edge,
                          assumed_exit_basis_bps=self.target)
            if profit < self.limits.min_profit or edge < self.limits.min_net_edge_bps:
                reasons.append("conditional_profit_below_threshold")
            # A zero residual is exact. Long dust at or under the cash cap is
            # still an order. Anything larger, or a naked short, is not.
            if unhedged != 0 and not residual_within_cash:
                reasons.append("exact_hedge_required_for_intent")
            reserve = max(Decimal("0"), cost)
            if spot_open + max(perp_open, perp_close) + reserve > self.limits.total_capital:
                reasons.append("cost_reserve_exceeds_total_capital")
            if not reasons:
                result["status"] = "conditional_signal"
        return result

    def on_snapshot(self, books: Sequence[OrderBook], now_ms: int) -> Iterable[Intent]:
        result = self.evaluate(books, now_ms)
        if result["status"] != "conditional_signal":
            return
        c = self.contract
        n, q = result["contracts"], result["spot_quantity"]
        expiry = min(self.buy.expires_ts_ms, self.sell.expires_ts_ms, now_ms + 1000)
        if expiry <= now_ms:
            return
        yield Intent(
            self.name, c.asset,
            (
                Leg("onchainos@196", self.buy.token_address, Side.BUY,
                    result["spot_open_usdt"] / q, q, result["spot_open_usdt"]),
                Leg(c.venue, c.instrument_id, Side.SELL,
                    result["perp_open_usdt"] / (n * c.multiplier), n, result["perp_open_usdt"]),
            ),
            result["conditional_net_bps"], result["conditional_profit_usdt"],
            expiry - now_ms, now_ms,
            {**result, "kind": "conditional_open", "execution_allowed": False,
             "not_locked_profit": True, "contract_multiplier": c.multiplier,
             "dedupe_key": f"{self.name}:{c.key}:open",
             # Both legs quote 24/7, so exiting is not tied to the HK cash
             # session any more (it used to read "before 09:30 HK"). What still
             # holds: the short is never left running after the spot sale.
             "exit_policy": "sell spot and buy back the entire short when the basis converges (24/7 legs)",
             "directional_short_after_spot_sale": "excluded"},
        )
