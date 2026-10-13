"""Normalize CEX contract prices for comparison with HK-listed token prices."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class QuoteSemantic(str, Enum):
    HKD_ABSOLUTE = "hkd_absolute"
    USD_CONVERT_TO_HKD = "usd_convert_to_hkd"


@dataclass(frozen=True)
class NormalizedPrice:
    venue: str
    instrument: str
    raw_price: Decimal
    quote_semantic: QuoteSemantic
    fx_usd_hkd: Decimal | None
    hkd_price: Decimal
    source: str
    observed_at: str


def normalize_price(
    venue: str,
    instrument: str,
    raw_price: Decimal,
    semantic: QuoteSemantic,
    *,
    fx_usd_hkd: Decimal | None = None,
    source: str = "",
    observed_at: str = "",
) -> NormalizedPrice:
    raw_price = Decimal(raw_price)
    if raw_price <= 0:
        raise ValueError("price must be positive")
    if semantic is QuoteSemantic.HKD_ABSOLUTE:
        hkd = raw_price
        fx = None
    else:
        if fx_usd_hkd is None or Decimal(fx_usd_hkd) <= 0:
            raise ValueError("USD-to-HKD FX is required and must be positive")
        fx = Decimal(fx_usd_hkd)
        hkd = raw_price * fx
    return NormalizedPrice(venue, instrument, raw_price, semantic, fx, hkd,
                           source, observed_at)


def compare_to_spot_hkd(price: NormalizedPrice, spot_hkd: Decimal) -> Decimal:
    """Return contract-minus-spot in HKD per underlying unit."""
    return price.hkd_price - Decimal(spot_hkd)
