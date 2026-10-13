#!/usr/bin/env python3
"""Can this API key add a withdrawal address? Probe, do not guess.

    PYTHONPATH=src .venv/bin/python scripts/kraken_address_probe.py

Every call here sends an **empty payload**. That is deliberate:

* a method that does not exist answers ``EGeneral:Unknown method`` (or 404), so
  an empty body costs nothing to find out;
* a method that does exist answers with a missing-parameter error;
* an empty body cannot register anything usable even if the method exists.

It also lists what the key can read (``WithdrawMethods`` /
``WithdrawAddresses``), because a permission that is ticked and a permission
that is honoured are two different things.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.venues.kraken_spot import (  # noqa: E402
    KrakenError, KrakenSpotClient, load_credentials)

PROXY = "http://127.0.0.1:4780"

# Names worth trying. The first was measured 404 on 2026-09-24; the rest are the
# spellings Kraken uses elsewhere (plural collections, verb prefixes).
CANDIDATES = [
    "/0/private/AddWithdrawAddress",
    "/0/private/WithdrawAddresses/Add",
    "/0/private/AddAddress",
    "/0/private/WithdrawAddress",
    "/0/private/WithdrawAddressAdd",
    "/0/private/WithdrawAddresses",
]

MISSING_METHOD = ("unknown method", "not found", "no such method", "invalid method")
MISSING_PARAM = ("missing", "required", "invalid arguments", "invalid nonce")
PERMISSION = ("permission", "denied", "not allowed", "api key")


def classify(status: int, errors: list[str]) -> str:
    text = " ".join(errors).lower()
    if status == 404 or any(token in text for token in MISSING_METHOD):
        return "没有这个方法"
    if any(token in text for token in PERMISSION):
        return "方法在，但 key 没这个权限"
    if any(token in text for token in MISSING_PARAM):
        return "方法存在（缺参数）"
    return "有响应，需要人工看"


def main() -> int:
    key, secret = load_credentials(ROOT / "config" / "kraken.env")
    client = KrakenSpotClient(key, secret, proxy=PROXY)
    print(f"key 前缀 {key[:8]}…")

    print("\n== 探测「新增提币地址」类端点（空 payload）==")
    for path in CANDIDATES:
        try:
            client._post(path, {})
        except KrakenError as exc:
            errors = [str(item) for item in exc.errors]
            print(f"  {path:44s} {classify(getattr(exc, 'status', 200), errors)}")
            print(f"      -> {errors}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {path:44s} 传输失败 {type(exc).__name__}: {exc}")

    print("\n== 这个 key 现在能读到什么 ==")
    for label, path, payload in (
        ("WithdrawMethods", "/0/private/WithdrawMethods", {"asset": "USDC"}),
        ("WithdrawAddresses", "/0/private/WithdrawAddresses", {}),
    ):
        try:
            rows = client._post(path, payload)
        except KrakenError as exc:
            print(f"  {label:18s} 失败：{exc.errors}")
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"  {label:18s} 传输失败 {type(exc).__name__}")
            continue
        count = len(rows) if isinstance(rows, list) else "?"
        print(f"  {label:18s} 可读，{count} 条")
        if isinstance(rows, list) and rows:
            print("      " + json.dumps(rows[:3], ensure_ascii=False)[:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
