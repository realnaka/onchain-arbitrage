"""Audit the saved venue registry without changing execution eligibility."""
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
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    report = audit_json(read_registry(ROOT / args.registry))
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        (ROOT / args.output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
