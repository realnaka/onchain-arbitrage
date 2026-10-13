"""Evidence-gated mapping review and execution eligibility."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from .venue_registry import AssetVenueMapping


REQUIRED_EVIDENCE = (
    "issuer_asset_source",
    "cex_instrument_source",
    "underlying_match_source",
    "contract_spec_source",
    "observed_at",
    "reviewed_by",
    "reviewed_at",
)


def review_mapping(mapping: AssetVenueMapping, evidence: dict[str, Any],
                   *, approve: bool, reviewer: str,
                   reviewed_at: str | None = None) -> AssetVenueMapping:
    """Return a reviewed copy; approval is impossible without all evidence."""
    merged = dict(mapping.evidence)
    merged.update(evidence)
    merged["reviewed_by"] = reviewer
    merged["reviewed_at"] = reviewed_at or datetime.now(timezone.utc).isoformat()
    missing = [key for key in REQUIRED_EVIDENCE if not merged.get(key)]
    if approve and missing:
        raise ValueError("cannot approve mapping; missing evidence: " + ",".join(missing))
    basis = tuple(dict.fromkeys((*mapping.verification_basis,
                                 "manual_review" if approve else "manual_rejection")))
    return replace(mapping, verified=approve, verification_basis=basis, evidence=merged)


def eligibility_reason(mapping: AssetVenueMapping) -> str:
    if not mapping.verified:
        return "mapping_not_verified"
    missing = [key for key in REQUIRED_EVIDENCE if not mapping.evidence.get(key)]
    if missing:
        return "missing_evidence:" + ",".join(missing)
    if not mapping.executable():
        return "mapping_shape_not_executable"
    return "eligible"
