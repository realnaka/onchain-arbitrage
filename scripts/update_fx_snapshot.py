#!/usr/bin/env python3
"""Refresh the local USD/HKD snapshot used by every price comparison.

Two independent live sources, averaged:

  1. Binance's own HKD/USD contract pair for the same stock
     (HK0700USDT ÷ TENCENTUSDT) — same venue, so no cross-venue basis leaks in;
  2. OKX's published index conversion for XIAOMI (HK1810/USDT ÷ its USD
     conversion) — a different venue and a different stock.

The stored value is what the strategy reads; the strategy itself never calls a
remote service while calculating. Run this daily (see the `fx-daily` automation)
and whenever a comparison against an HKD-quoted contract matters: measured
2026-09-16, the previous stored rate was 38 bps away from the market, which is
larger than the edge the strategy is hunting.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arb_monitor.core.fx_store import FxSnapshot, FxStore  # noqa: E402


def fetch(url: str, proxy: str | None, timeout: int = 20) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "arb-monitor-fx/0.1"})
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        open_call = opener.open
    else:
        open_call = urllib.request.urlopen
    with open_call(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def binance_pair(proxy: str | None) -> Decimal:
    """Same-venue HKD/USD pair: Binance's HK0700USDT ÷ TENCENTUSDT."""
    hkd = fetch("https://www.binance.com/fapi/v1/ticker/bookTicker?symbol=HK0700USDT", proxy)
    usd = fetch("https://www.binance.com/fapi/v1/ticker/bookTicker?symbol=TENCENTUSDT", proxy)
    hkd_mid = (Decimal(hkd["bidPrice"]) + Decimal(hkd["askPrice"])) / 2
    usd_mid = (Decimal(usd["bidPrice"]) + Decimal(usd["askPrice"])) / 2
    if hkd_mid <= 0 or usd_mid <= 0:
        raise ValueError("binance returned a non-positive price")
    return hkd_mid / usd_mid


def okx_index(proxy: str | None) -> Decimal:
    """OKX's own conversion inside the XIAOMI index."""
    data = fetch("https://www.okx.com/api/v5/market/index-components?index=XIAOMI-USDT",
                 proxy)["data"]
    component = next((c for c in data["components"] if "HK1810" in str(c.get("symbol"))), None)
    if component is None:
        raise ValueError("OKX index no longer carries the HK1810 component")
    return Decimal(str(component["symPx"])) / Decimal(str(component["cnvPx"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/fx.sqlite")
    parser.add_argument("--proxy", default="http://127.0.0.1:4780")
    parser.add_argument("--max-spread-bps", default="80",
                        help="refuse to store an average when the two sources disagree by more")
    args = parser.parse_args()

    store = FxStore(args.db)
    previous = store.latest("USD/HKD")
    sources: list[tuple[str, Decimal]] = []
    errors: list[str] = []
    for name, fn in (("binance:HK0700USDT/TENCENTUSDT", binance_pair),
                     ("okx:index XIAOMI-USDT", okx_index)):
        try:
            sources.append((name, fn(args.proxy)))
        except Exception as exc:  # noqa: BLE001 - fall back to the other source
            errors.append(f"{name}:{type(exc).__name__}")
    if not sources:
        print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False))
        return 2
    values = [value for _name, value in sources]
    average = sum(values) / len(values)
    spread_bps = (max(values) - min(values)) / min(values) * Decimal(10000)
    if spread_bps > Decimal(str(args.max_spread_bps)):
        # A disagreement this large means one source is not what we think it is.
        print(json.dumps({"ok": False, "reason": "sources_disagree",
                          "spread_bps": str(spread_bps),
                          "sources": {n: str(v) for n, v in sources},
                          "errors": errors}, ensure_ascii=False))
        return 3
    source = " + ".join(f"{name}={value}" for name, value in sources)
    store.put(FxSnapshot("USD/HKD", average, time.time_ns() // 1_000_000,
                         f"average of {len(sources)} live sources: {source}"))
    result = {
        "ok": True, "rate": str(average),
        "previous": str(previous.rate) if previous else None,
        "change_bps": (str((average / previous.rate - 1) * Decimal(10000))
                       if previous else None),
        "sources": {name: str(value) for name, value in sources},
        "source_spread_bps": str(spread_bps), "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
