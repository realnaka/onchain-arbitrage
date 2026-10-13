#!/usr/bin/env python3
"""Ink 报价能力只读探针（复现 docs/review-2026-10-09.md 的 Q5 证据）。

只发 eth_call / eth_getCode，不签名、不广播。需要本机代理：

    HTTPS_PROXY=http://127.0.0.1:4780 HTTP_PROXY=http://127.0.0.1:4780 \
        .venv/bin/python scripts/review-2026-10-09-ink-quoter.py
"""
from __future__ import annotations

import json
import sys
import urllib.request
from decimal import Decimal

sys.path.insert(0, "src")

RPC = "https://rpc-gel.inkonchain.com"
CENSUS_QUOTER = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"  # ink 的 default_quoter
POOL = "0x383d9dd7f923dd967b23a419a9b912af8d96ca3b"          # SPYx / USDG 0.3%
USDG = "0xe343167631d89b6ffc58b88d6b7fb0228795491d"
SPYX = "0xe7e553cd128f0011777323a0b44a7b96ea1cb540"
SELECTOR = {
    "chainId": ("eth_chainId", []),
    "code": ("eth_getCode", [CENSUS_QUOTER, "latest"]),
    "quoter.factory()": ("eth_call", [{"to": CENSUS_QUOTER, "data": "0xc45a0155"}, "latest"]),
    "pool.slot0()": ("eth_call", [{"to": POOL, "data": "0x3850c7bd"}, "latest"]),
    "pool.token0()": ("eth_call", [{"to": POOL, "data": "0x0dfe1681"}, "latest"]),
    "pool.liquidity()": ("eth_call", [{"to": POOL, "data": "0x1a686502"}, "latest"]),
    "pool.factory()": ("eth_call", [{"to": POOL, "data": "0xc45a0155"}, "latest"]),
    "pool.fee()": ("eth_call", [{"to": POOL, "data": "0xddca3f43"}, "latest"]),
}


def rpc(method: str, params: list):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    request = urllib.request.Request(
        RPC, data=body, headers={"content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def main() -> int:
    from arb_monitor.xstocks_v2.feeds import evm

    for label, (method, params) in SELECTOR.items():
        try:
            result = rpc(method, params).get("result")
        except Exception as error:  # noqa: BLE001 - diagnostic script
            print(f"{label:<18} ERR {type(error).__name__}: {error}")
            continue
        shown = result if label != "code" else f"{len(str(result))} hex chars"
        print(f"{label:<18} {str(shown)[:80]}")

    # census 写的 quoter 能不能报价
    data = evm.encode_v3_quote(USDG, SPYX, 10 ** 18, 3000)
    try:
        out = rpc("eth_call", [{"to": CENSUS_QUOTER, "data": data}, "latest"])
        print(f"{'quote(1 USDG)':<18} {str(out.get('result'))[:80]}"
              "   <= 空(0x) 表示报不出价")
    except Exception as error:  # noqa: BLE001
        print(f"{'quote(1 USDG)':<18} ERR {type(error).__name__}: {error}")
    print("\n判据：pool.slot0() 有值而 quoter.factory() 为 0x ⇒ 池子是真的、"
          "quoter 地址选错了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
