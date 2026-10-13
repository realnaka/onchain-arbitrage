#!/usr/bin/env python3
"""24 小时机会漏斗（只读）。复现 docs/review-2026-10-09.md 的 Q6 数字。

用法：
    .venv/bin/python scripts/review-2026-10-09-funnel.py [--hours 24]

读 data-v2/xstocks_v2_cycles.jsonl，不改任何文件。
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

LEDGER = Path("data-v2/xstocks_v2_cycles.jsonl")
MISSING_SAMPLE = {"unmeasured_route", "bridge_unmeasured_symbol"}


def dec(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--path", default=str(LEDGER))
    args = parser.parse_args()

    now_ms = int(time.time() * 1000)
    cut = now_ms - int(args.hours * 3600 * 1000)
    rows: list[dict] = []
    stamps: list[int] = []
    for line in Path(args.path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        ts = row.get("ts_ms") or 0
        if ts >= cut:
            rows.append(row)
            stamps.append(ts)
    if not rows:
        print("窗口内没有账本行")
        return 1

    stamps.sort()
    rounds = 1
    for a, b in zip(stamps, stamps[1:]):
        if b - a > 60_000:
            rounds += 1

    def show(title: str, counter: collections.Counter, limit: int = 12) -> None:
        print(title)
        for key, count in counter.most_common(limit):
            print(f"  {str(key) or '(空)':<34} {count:>6}")

    print(f"窗口 {datetime.datetime.fromtimestamp(min(stamps) / 1000)}"
          f" → {datetime.datetime.fromtimestamp(max(stamps) / 1000)}")
    print(f"账本行 {len(rows)} · 轮次≈{rounds} · 唯一标的 "
          f"{len({r.get('symbol') for r in rows})}")
    show("mode:", collections.Counter(r.get("mode") for r in rows))
    show("state:", collections.Counter(r.get("state") for r in rows))
    show("family:", collections.Counter(r.get("family") for r in rows))
    show("refused 原因:", collections.Counter(r.get("reason") for r in rows))

    edge = dec(20)
    passers = [r for r in rows if (dec(r.get("edge_t0_bps")) or Decimal("-99")) >= edge]
    print(f"\n价差 >= 20 bps 的行: {len(passers)}"
          f"（{len(passers) / len(rows) * 100:.1f}%）")
    show("  其中 refusal 原因:", collections.Counter(r.get("reason") for r in passers))

    blockers = collections.Counter()
    for row in rows:
        for item in (row.get("extra") or {}).get("blockers") or []:
            blockers[str(item)] += 1
    show("\nblockers:", blockers)

    missing = [r for r in rows
               if set((r.get("extra") or {}).get("blockers") or []) & MISSING_SAMPLE]
    missing_hi = [r for r in missing
                  if (dec(r.get("edge_t0_bps")) or Decimal("-99")) >= edge]
    print(f"\n带『缺样本』blocker 的行: {len(missing)}"
          f" · 其中够探针门槛(>=20bps): {len(missing_hi)}"
          f" · 永远不探: {len(missing) - len(missing_hi)}")

    net = [r for r in rows if (dec(r.get("net_usdt")) or Decimal("-99")) > Decimal("0.1")]
    print(f"净利 > 0.1 USDT 的行: {len(net)}")
    show("  其中 state:", collections.Counter(r.get("state") for r in net))

    chains = collections.Counter(str(r.get("chain")) for r in rows)
    xlayer = sum(v for k, v in chains.items() if k.startswith("x-layer"))
    symbols = collections.Counter(r.get("symbol") for r in rows)
    top2 = symbols.most_common(2)
    print(f"\nx-layer 行占比 {xlayer / len(rows) * 100:.1f}%"
          f" · top2 标的 {top2} 占比 "
          f"{sum(c for _s, c in top2) / len(rows) * 100:.1f}%")
    show("chain:", chains)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
