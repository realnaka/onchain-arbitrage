"""HK basis data contracts. No network, wallets or execution capabilities."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from decimal import Decimal
from enum import Enum
import json
from typing import Any

ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")
# How long a stablecoin→USDT rate stays usable. One number, shared by the risk
# gate, the strategy and the boards, so "stale" cannot mean three things.
DEFAULT_MAX_QUOTE_CONVERSION_AGE_MS = 300_000
# A stablecoin this far from 1.0 is a depeg, not rounding.
DEFAULT_MAX_PEG_DEVIATION_BPS = Decimal("25")


def decimal(value: Any) -> Decimal:
    # Fail closed: converting a binary float to a string is not lossless parsing.
    if isinstance(value, (float, bool)):
        raise ValueError("financial numbers must be Decimal, integer or decimal string")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError("non-finite financial number")
    return result


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(type(value).__name__)


def dumps(value: Any) -> str:
    return json.dumps(value, default=json_default, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class Contract:
    venue: str
    instrument_id: str
    asset: str
    category: str
    payout_model: str  # usd_linear / hkd_quanto / unknown
    multiplier: Decimal  # payout per one price-point per contract
    lot_size: Decimal
    min_contracts: Decimal
    min_notional: Decimal
    tick_size: Decimal
    active: bool
    source: str
    recv_ts_ms: int
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.venue}:{self.instrument_id}"


@dataclass(frozen=True)
class SpotQuote:
    asset: str
    side: str
    chain_id: str
    token_address: str
    quote_address: str
    quote_currency: str
    requested_amount: Decimal
    token_quantity: Decimal | None
    quote_amount: Decimal | None
    status: str
    request_ts_ms: int
    recv_ts_ms: int
    expires_ts_ms: int
    source: str
    routes: tuple[str, ...] = ()
    gas_quote: Decimal | None = None
    price_impact_pct: Decimal | None = None
    venue_ts_ms: int | None = None
    sequence: int | None = None
    error_code: str = ""
    error: str = ""
    raw: Any = None

    @property
    def price(self) -> Decimal | None:
        if self.token_quantity and self.quote_amount:
            return self.quote_amount / self.token_quantity
        return None

    def is_fresh(self, now_ms: int, max_age_ms: int) -> bool:
        # A local conservative deadline, NOT a provider-guaranteed fill TTL.
        return (
            self.status == "ok"
            and self.price is not None
            and self.request_ts_ms <= self.recv_ts_ms <= now_ms + 1000
            and 0 <= now_ms - self.request_ts_ms <= max_age_ms
            and now_ms < self.expires_ts_ms
        )


@dataclass(frozen=True)
class QuoteConversion:
    """A quote-token → USDT conversion, with the peg band it was read in.

    An xStocks pool may be denominated in USDG, USDT0 or USDC rather than the
    quote token the probe normally uses. Treating any of those as "1 USDT" is
    an assumption, and it is the kind that reads as a few bps of free edge
    right up until the stablecoin trades at 0.985.

    So the conversion is an observation with a source, a timestamp and an
    expiry. ``usable()`` returns the reason it may not be used instead of
    substituting 1.0 — a missing or stale conversion is a refusal, never a
    default.
    """

    quote_symbol: str = ""
    quote_address: str = ""
    quote_to_usdt: Decimal | None = None
    source: str = ""
    observed_ts_ms: int = 0
    valid_until_ms: int = 0
    # Absolute deviation from 1.0 observed for this stablecoin, in bps. Not a
    # forecast: the number that was measured when the rate was read.
    peg_deviation_bps: Decimal | None = None
    verified: bool = False

    def usable(self, now_ms: int, *, max_age_ms: int,
               max_peg_deviation_bps: Decimal | None = None) -> tuple[bool, str]:
        """``(ok, reason)`` — the reason is empty when the rate may be used."""
        if not self.verified or not self.source:
            return False, "quote_conversion_unverified"
        if self.quote_to_usdt is None or self.quote_to_usdt <= ZERO:
            return False, "quote_conversion_missing"
        if not self.observed_ts_ms or self.observed_ts_ms > now_ms + 1000:
            return False, "quote_conversion_missing"
        if now_ms - self.observed_ts_ms > max(0, int(max_age_ms)):
            return False, "quote_conversion_stale"
        if not self.valid_until_ms or self.valid_until_ms <= now_ms:
            return False, "quote_conversion_stale"
        if (max_peg_deviation_bps is not None
                and self.peg_deviation_bps is not None
                and abs(self.peg_deviation_bps) > max_peg_deviation_bps):
            return False, "quote_conversion_depeg"
        return True, ""


@dataclass(frozen=True)
class PairMapping:
    asset: str
    shares_per_token: Decimal | None = None
    shares_per_perp_unit: Decimal | None = None
    quote_to_usdt: Decimal | None = None
    verified: bool = False
    evidence: str = ""
    valid_until_ms: int = 0
    # Which market the evidence was read for. A mapping verified against an HK
    # identity may not be reused for a US row (and the other way round), so the
    # market travels with the mapping instead of being assumed at the call site.
    listing_market: str = "UNKNOWN"
    underlying_symbol: str = ""
    # One contract's worth of the underlying, as the venue publishes it. When
    # present this is a drift alarm: the strategy refuses the pair if the
    # catalogue row and the reviewed value disagree.
    perp_contract_multiplier: Decimal | None = None
    # The token/contract identities the ratio was read for, so a report can
    # answer "which addresses" without re-reading the config.
    token_address: str = ""
    wrapper_address: str = ""
    quote_conversion: QuoteConversion | None = None

    def valid(self, now_ms: int) -> bool:
        return (
            self.verified and bool(self.evidence) and self.valid_until_ms > now_ms
            and all(x is not None and x > ZERO for x in (
                self.shares_per_token, self.shares_per_perp_unit, self.quote_to_usdt
            ))
        )


@dataclass(frozen=True)
class RoundTripCosts:
    spot_fee_bps: Decimal = Decimal("0")
    perp_fee_bps: Decimal = Decimal("8")
    # Maker fee, charged by this account's schedule when the order rests
    # instead of crossing (measured 1.6 bps against 4.5 bps taker on
    # 2026-09-16). Only used when the round trip is priced as maker.
    perp_maker_fee_bps: Decimal = Decimal("1.6")
    slippage_bps: Decimal = Decimal("5")
    failure_bps: Decimal = Decimal("5")
    financing_quote: Decimal = ZERO
    funding_cashflow_quote: Decimal = ZERO  # positive = received by the short
    verified: bool = False
    valid_until_ms: int = 0
    roundtrip_gas_budget_usdt: Decimal | None = None

    def total(
        self, spot_open: Decimal, spot_close: Decimal,
        perp_open: Decimal, perp_close: Decimal, gas: Decimal,
    ) -> Decimal:
        if min(self.spot_fee_bps, self.perp_fee_bps, self.slippage_bps,
               self.failure_bps, self.financing_quote, gas) < ZERO:
            raise ValueError("negative unsigned cost")
        all_turnover = spot_open + spot_close + perp_open + perp_close
        return (
            (spot_open + spot_close) * self.spot_fee_bps / BPS
            + (perp_open + perp_close) * self.perp_fee_bps / BPS
            + all_turnover * (self.slippage_bps + self.failure_bps) / BPS
            + gas + self.financing_quote - self.funding_cashflow_quote
        )


def paired_pnl(
    spot_qty: Decimal, spot_entry: Decimal, spot_exit: Decimal,
    contracts: Decimal, multiplier: Decimal,
    perp_entry: Decimal, perp_exit: Decimal,
    costs_quote: Decimal, funding_cashflow_quote: Decimal = ZERO,
) -> Decimal:
    """Both legs' cash P&L in ONE currency; funding is signed, not an APR."""
    return (
        spot_qty * (spot_exit - spot_entry)
        + contracts * multiplier * (perp_entry - perp_exit)
        - costs_quote + funding_cashflow_quote
    )
