"""Declared processing policy for each monitored asset and venue."""
from __future__ import annotations

from dataclasses import dataclass
from .quote_normalization import QuoteSemantic


@dataclass(frozen=True)
class VenueProcessingRule:
    asset: str
    venue: str
    instrument: str
    semantic: QuoteSemantic
    enabled: bool
    status: str
    note: str = ""


DEFAULT_PROCESSING_RULES = (
    VenueProcessingRule("XIAOMI", "okx", "XIAOMI-USDT-SWAP",
                         QuoteSemantic.USD_CONVERT_TO_HKD, True, "price_probe_pending"),
    VenueProcessingRule("XIAOMI", "gate", "XIAOMI_USDT",
                         QuoteSemantic.USD_CONVERT_TO_HKD, False, "fallback_todo"),
    VenueProcessingRule("XIAOMI", "binance", "HK1810USDT",
                         QuoteSemantic.HKD_ABSOLUTE, False, "fallback_todo"),
    VenueProcessingRule("TENCENT", "binance", "HK0700USDT",
                         QuoteSemantic.HKD_ABSOLUTE, False, "fallback_todo"),
    VenueProcessingRule("TENCENT", "binance", "TENCENTUSDT",
                         QuoteSemantic.USD_CONVERT_TO_HKD, False, "fallback_todo"),
    VenueProcessingRule("TENCENT", "gate", "TENCENTHKD_USDT",
                         QuoteSemantic.HKD_ABSOLUTE, False, "fallback_todo"),
    VenueProcessingRule("TENCENT", "gate", "TENCENT_USDT",
                         QuoteSemantic.USD_CONVERT_TO_HKD, False, "fallback_todo"),
)
