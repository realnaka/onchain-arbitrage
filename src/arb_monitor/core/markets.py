"""Listing-market vocabulary: which market an xStock is listed on, and what
that market is allowed to decide.

The issuer states the listing venue in ``underlying.listingCountry``. That is
the only field that decides the market:

* an ISIN prefix is the *issuer's domicile* (KY / CN / BM / CH), not the
  listing venue — of the 79 HK-listed assets sampled on 2026-09-15 only 23
  carry an HK prefix, so ISIN must never be used to pick a market;
* a numeric code is an HKEX-shaped *cross-check* (HK codes are 1-5 digits,
  79/79 and 741/741 on 2026-09-15), not an authority;
* a missing country stays ``UNKNOWN`` and is refused, never guessed.

Both supported markets trade 24/7 here: the spot leg is a stock token on X
Layer and the perp leg is a USD-linear contract on a CEX. Whether the cash
market for the underlying happens to be open is not a property of either leg,
so a cash-session calendar must not gate an order in *either* market. The
calendar phase is kept as a report grouping and a diagnostic, nothing more.

The distinction that does matter is evidence: a mapping verified for one
market cannot be reused for another. Every US row has to carry its own issuer
ISINs, wrapper reserve, multiplier and venue contract spec, checked against a
US identity — see ``core/mapping_evidence.py``.
"""
from __future__ import annotations

from typing import Iterable

HK = "HK"
US = "US"
UNKNOWN = "UNKNOWN"

SUPPORTED_MARKETS = (HK, US)
# Markets the monitor may be told to scan. UNKNOWN is deliberately absent: a
# listing whose country the issuer left null is never adopted into a scan scope.
SCANNABLE_MARKETS = (HK, US)

_LABELS = {HK: "港股", US: "美股"}

__all__ = [
    "HK", "US", "UNKNOWN", "SUPPORTED_MARKETS", "SCANNABLE_MARKETS",
    "normalize_market", "validate_scan_countries", "is_supported_market",
    "market_label", "describe_scope",
]


def normalize_market(value: object) -> str:
    """Upper-cased market name, or ``UNKNOWN`` when the issuer said nothing."""
    text = str(value or "").strip().upper()
    return text or UNKNOWN


def is_supported_market(value: object) -> bool:
    return normalize_market(value) in SUPPORTED_MARKETS


def validate_scan_countries(values: Iterable[object]) -> tuple[str, ...]:
    """Reviewed scan scope, or a refusal.

    A scope naming a market we cannot attribute (``UNKNOWN``) or one we have no
    mapping evidence for (``GB``, say) is a configuration error, not something
    to silently drop: dropping it would look like "no listings there" in the
    summary, which is a different statement from "we are not looking".
    """
    out: list[str] = []
    for value in values:
        market = normalize_market(value)
        if market not in SCANNABLE_MARKETS:
            raise ValueError(
                f"token_scan_countries must be a subset of "
                f"{'/'.join(SCANNABLE_MARKETS)}, got {market}")
        if market not in out:
            out.append(market)
    if not out:
        raise ValueError("token_scan_countries must name at least one market")
    return tuple(out)


def describe_scope(markets: Iterable[object]) -> str:
    """Readable scope for alerts, e.g. ``港股/美股``."""
    labels = [market_label(market, "").strip() or normalize_market(market)
              for market in markets]
    return "/".join(labels) or "未限定"


def market_label(market: object, code: object) -> str:
    """Readable label for alerts, e.g. ``港股 1810`` / ``美股 AAPL``."""
    normalized = normalize_market(market)
    chinese = _LABELS.get(normalized, normalized)
    text = str(code or "").strip()
    if normalized == UNKNOWN:
        return f"上市地未标注 {text}".strip()
    return f"{chinese} {text}".strip()
