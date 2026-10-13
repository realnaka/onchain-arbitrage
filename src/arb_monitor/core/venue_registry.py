"""Explicit asset/venue identity registry.

No symbol is considered executable merely because names look similar.  A
mapping must carry the source response, timestamps, and an explicit
verification flag.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ChainAsset:
    asset: str
    symbol: str
    chain_id: str
    contract_address: str
    decimals: int
    underlying_symbol: str = ""
    source: str = ""
    observed_at: str = ""


@dataclass(frozen=True)
class VenueInstrument:
    venue: str
    instrument_id: str
    instrument_type: str
    state: str
    base_underlying: str
    quote: str
    contract_multiplier: str
    min_size: str
    lot_size: str
    settle_currency: str
    source: str = ""
    observed_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AssetVenueMapping:
    asset: str
    chain: ChainAsset
    instrument: VenueInstrument
    verified: bool = False
    verification_basis: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    def executable(self) -> bool:
        return (
            self.verified
            and self.instrument.state == "live"
            and self.chain.chain_id
            and self.chain.contract_address.startswith("0x")
            and self.instrument.base_underlying.upper() == self.asset.upper()
        )


def discover_okx_instruments(rows: Iterable[dict[str, Any]],
                            observed_at: str | None = None) -> list[VenueInstrument]:
    """Normalize OKX instrument API rows without guessing stock identity."""
    stamp = observed_at or now_iso()
    out = []
    for row in rows:
        if row.get("instType") != "SWAP":
            continue
        out.append(VenueInstrument(
            venue="okx",
            instrument_id=str(row.get("instId", "")),
            instrument_type=str(row.get("instType", "")),
            state=str(row.get("state", "")),
            base_underlying=str(row.get("instFamily", "")).split("-")[0],
            quote=str(row.get("settleCcy") or row.get("instFamily", "").split("-")[-1]),
            contract_multiplier=str(row.get("ctVal", "0")),
            min_size=str(row.get("minSz", "0")),
            lot_size=str(row.get("lotSz", "0")),
            settle_currency=str(row.get("settleCcy", "")),
            source="okx:/api/v5/account/instruments",
            observed_at=stamp,
        ))
    return out


def write_registry(path: str | Path, mappings: Iterable[AssetVenueMapping]) -> None:
    """Write an auditable JSON registry atomically enough for local use."""
    target = Path(path)
    rows = [asdict(item) for item in mappings]
    target.write_text(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")


def merge_registry(existing: Iterable[AssetVenueMapping],
                   discovered: Iterable[AssetVenueMapping]) -> list[AssetVenueMapping]:
    """Merge fresh candidates without erasing verified/manual evidence."""
    merged: dict[tuple[str, str, str], AssetVenueMapping] = {}
    for item in existing:
        key = (item.asset, item.instrument.venue, item.instrument.instrument_id)
        merged[key] = item
    for item in discovered:
        key = (item.asset, item.instrument.venue, item.instrument.instrument_id)
        old = merged.get(key)
        if old is None:
            merged[key] = item
            continue
        # Discovery refreshes market facts, but preserves human verification.
        merged[key] = AssetVenueMapping(
            asset=old.asset, chain=old.chain, instrument=item.instrument,
            verified=old.verified,
            verification_basis=old.verification_basis,
            evidence=old.evidence,
        )
    return sorted(merged.values(), key=lambda x: (
        x.asset, x.instrument.venue, x.instrument.instrument_id))


def read_registry(path: str | Path) -> list[AssetVenueMapping]:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for row in rows:
        chain = ChainAsset(**row["chain"])
        instrument = VenueInstrument(**row["instrument"])
        out.append(AssetVenueMapping(
            row["asset"], chain, instrument, bool(row.get("verified", False)),
            tuple(row.get("verification_basis", ())), row.get("evidence", {}),
        ))
    return out
