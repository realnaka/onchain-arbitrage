"""Approve or reject one registry mapping with explicit evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.core.mapping_review import review_mapping  # noqa: E402
from arb_monitor.core.venue_registry import read_registry, write_registry  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", required=True)
    parser.add_argument("--venue", required=True)
    parser.add_argument("--instrument", required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--evidence", required=True,
                        help="JSON object or path to JSON file")
    parser.add_argument("--registry", default="config/venue_registry.json")
    args = parser.parse_args()
    raw = Path(args.evidence)
    evidence = json.loads(raw.read_text() if raw.exists() else args.evidence)
    path = ROOT / args.registry
    rows = read_registry(path)
    found = False
    updated = []
    for row in rows:
        if (row.asset, row.instrument.venue, row.instrument.instrument_id) == (
            args.asset, args.venue, args.instrument):
            row = review_mapping(row, evidence, approve=args.approve,
                                 reviewer=args.reviewer)
            found = True
        updated.append(row)
    if not found:
        raise SystemExit("mapping not found in registry")
    write_registry(path, updated)
    print(json.dumps({"updated": True, "asset": args.asset, "venue": args.venue,
                      "instrument": args.instrument, "verified": args.approve},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
