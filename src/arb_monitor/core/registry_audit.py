"""Conservative audit of chain/CEX identity mappings."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable

from .venue_registry import AssetVenueMapping


@dataclass(frozen=True)
class AuditResult:
    asset: str
    venue: str
    instrument_id: str
    api_present: bool
    chain_identity_complete: bool
    underlying_name_match: bool
    executable_shape: bool
    verified: bool
    reasons: tuple[str, ...]


def audit_mapping(mapping: AssetVenueMapping) -> AuditResult:
    reasons: list[str] = []
    instrument = mapping.instrument
    chain = mapping.chain
    api_present = bool(instrument.instrument_id and instrument.source)
    chain_complete = (
        bool(chain.chain_id and chain.contract_address.startswith("0x")
             and len(chain.contract_address) == 42 and chain.decimals >= 0)
    )
    name_match = (
        mapping.asset.upper() == instrument.base_underlying.upper()
        and bool(chain.underlying_symbol)
    )
    shape = instrument.state == "live" and instrument.instrument_type == "SWAP"
    try:
        valid_size = Decimal(instrument.min_size) > 0 and Decimal(instrument.lot_size) > 0
    except (InvalidOperation, ValueError):
        valid_size = False
    if not api_present:
        reasons.append("missing_api_evidence")
    if not chain_complete:
        reasons.append("incomplete_chain_identity")
    if not name_match:
        reasons.append("underlying_not_explicitly_matched")
    if not shape:
        reasons.append("not_live_swap")
    if not valid_size:
        reasons.append("invalid_contract_size")
    if not mapping.verification_basis:
        reasons.append("missing_human_or_external_verification_basis")
    if not mapping.verified:
        reasons.append("mapping_not_verified")
    return AuditResult(
        mapping.asset, instrument.venue, instrument.instrument_id,
        api_present, chain_complete, name_match, shape and valid_size,
        mapping.verified and not reasons, tuple(reasons),
    )


def audit_registry(mappings: Iterable[AssetVenueMapping]) -> list[AuditResult]:
    return [audit_mapping(mapping) for mapping in mappings]


def audit_json(mappings: Iterable[AssetVenueMapping]) -> list[dict]:
    return [asdict(result) for result in audit_registry(mappings)]
