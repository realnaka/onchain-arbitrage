"""Read-only refresh of CEX instrument candidates for the identity registry.

Usage:
  .venv/bin/python scripts/refresh_venue_registry.py --proxy http://127.0.0.1:4780

The command fetches public market metadata only.  It never uses private
credentials and never marks a chain/CEX mapping verified automatically.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.core.venue_registry import (  # noqa: E402
    AssetVenueMapping, ChainAsset, VenueInstrument, merge_registry,
    read_registry, write_registry,
)
from arb_monitor.data.hk_cex import CexPublicAdapter  # noqa: E402


def load_chain_assets(path: Path) -> list[ChainAsset]:
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    out = []
    for token in config.get("tokens", []):
        if not token.get("address") or not token.get("chain_id"):
            continue
        out.append(ChainAsset(
            asset=str(token["asset"]), symbol=str(token["symbol"]),
            chain_id=str(token["chain_id"]), contract_address=str(token["address"]),
            decimals=int(token["decimals"]),
            underlying_symbol=str(token.get("stock_symbol") or token.get("hk_code") or ""),
            source=str(path), observed_at="",
        ))
    return out


def refresh(venues: list[str], proxy: str | None, output: Path) -> int:
    observed = datetime.now(timezone.utc).isoformat()
    chains = load_chain_assets(ROOT / "config/hk_monitor.toml")
    mappings = []
    for venue in venues:
        adapter = CexPublicAdapter(venue, proxy=proxy)
        contracts, _raw, _received = adapter.instruments()
        for contract in contracts:
            chain = next((x for x in chains if x.asset == contract.asset), None)
            if chain is None:
                continue
            instrument = VenueInstrument(
                venue=contract.venue, instrument_id=contract.instrument_id,
                instrument_type="SWAP", state="live" if contract.active else "inactive",
                base_underlying=contract.asset, quote="USDT",
                contract_multiplier=str(contract.multiplier),
                min_size=str(contract.min_contracts), lot_size=str(contract.lot_size),
                settle_currency="USDT", source=contract.source,
                observed_at=observed,
                metadata={"raw_contract": contract.raw},
            )
            mappings.append(AssetVenueMapping(
                asset=chain.asset, chain=chain, instrument=instrument,
                verified=False,
                verification_basis=(
                    "CEX public instrument metadata observed",
                    "chain identity loaded from config; underlying equivalence pending review",
                ),
            ))
    existing = read_registry(output) if output.exists() else []
    merged = merge_registry(existing, mappings)
    write_registry(output, merged)
    print(f"wrote {len(merged)} registry rows ({len(mappings)} refreshed candidates) to {output}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="gate,okx,binance")
    parser.add_argument("--proxy")
    parser.add_argument("--output", default="config/venue_registry.json")
    args = parser.parse_args()
    return refresh([x.strip() for x in args.venues.split(",") if x.strip()],
                   args.proxy, ROOT / args.output)


if __name__ == "__main__":
    raise SystemExit(main())
