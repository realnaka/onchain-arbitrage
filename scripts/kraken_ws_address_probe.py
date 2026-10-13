#!/usr/bin/env python3
"""Same question as kraken_address_probe.py, over the WebSocket API v2.

    PYTHONPATH=src .venv/bin/python scripts/kraken_ws_address_probe.py

Kraken is moving private calls to ``wss://ws-auth.kraken.com/v2``. If the
address book were writable anywhere programmatically, it would most likely be
there. Each probe sends an empty request; an unknown method answers with an
unknown-method error and changes nothing.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.venues.kraken_spot import (  # noqa: E402
    KrakenError, KrakenSpotClient, load_credentials)

PROXY = "http://127.0.0.1:4780"
WS_PRIVATE = "wss://ws-auth.kraken.com/v2"

PROBES = [
    "add_withdraw_address",
    "add_address",
    "withdraw_addresses_add",
    "withdraw_address_add",
]


async def probe(token: str) -> None:
    headers = {"User-Agent": "arb-xstocks-v2-probe/0.1"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.ws_connect(WS_PRIVATE, proxy=PROXY, heartbeat=20,
                                      timeout=aiohttp.ClientTimeout(total=25)) as ws:
            for method in PROBES:
                await ws.send_json({"method": method, "req_id": len(method),
                                    "params": {"token": token}})
                try:
                    message = await asyncio.wait_for(ws.receive_json(), timeout=10)
                except asyncio.TimeoutError:
                    print(f"  {method:26s} 超时，无响应")
                    continue
                print(f"  {method:26s} {json.dumps(message, ensure_ascii=False)[:220]}")


def main() -> int:
    key, secret = load_credentials(ROOT / "config" / "kraken.env")
    client = KrakenSpotClient(key, secret, proxy=PROXY)
    try:
        token = (client._post("/0/private/GetWebSocketsToken", {}) or {}).get("token")
    except KrakenError as exc:
        print(f"拿不到 WS token：{exc.errors}")
        return 1
    if not token:
        print("拿不到 WS token：响应里没有 token")
        return 1
    print("WS 已鉴权，开始探测地址簿写方法：")
    asyncio.run(probe(token))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
