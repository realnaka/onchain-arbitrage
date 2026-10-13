"""Build a human-reviewable chain/CEX mapping report from the registry."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.core.registry_audit import audit_json  # noqa: E402
from arb_monitor.core.venue_registry import read_registry  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", default="config/venue_registry.json")
    parser.add_argument("--output", default="docs/research/venue-mapping-review.json")
    args = parser.parse_args()
    mappings = read_registry(ROOT / args.registry)
    report = []
    for mapping, audit in zip(mappings, audit_json(mappings)):
        report.append({
            "asset": mapping.asset,
            "chain_identity": {
                "symbol": mapping.chain.symbol,
                "chain_id": mapping.chain.chain_id,
                "contract_address": mapping.chain.contract_address,
                "underlying_symbol": mapping.chain.underlying_symbol,
            },
            "cex_identity": {
                "venue": mapping.instrument.venue,
                "instrument_id": mapping.instrument.instrument_id,
                "base_underlying": mapping.instrument.base_underlying,
                "state": mapping.instrument.state,
                "quote": mapping.instrument.quote,
                "settle_currency": mapping.instrument.settle_currency,
                "contract_multiplier": mapping.instrument.contract_multiplier,
                "min_size": mapping.instrument.min_size,
                "lot_size": mapping.instrument.lot_size,
                "source": mapping.instrument.source,
                "observed_at": mapping.instrument.observed_at,
            },
            "status": "verified" if audit["verified"] else (
                "candidate_name_match" if audit["underlying_name_match"]
                else "do_not_map"),
            "verified": audit["verified"],
            "reasons": audit["reasons"],
            "verification_basis": list(mapping.verification_basis),
            "evidence": mapping.evidence,
        })
    target = ROOT / args.output
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    counts = {}
    for row in report:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print(json.dumps({"rows": len(report), "counts": counts, "output": str(target)},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
