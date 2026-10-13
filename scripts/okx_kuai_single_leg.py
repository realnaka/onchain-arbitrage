"""Guarded OKX single-leg smoke test.

Requires explicit operator authorization.  It performs a targeted read-only
preflight, submits exactly one minimum-size IOC short, and immediately reads
back order/fills/balance/positions.  It never retries an ambiguous request.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlencode
from urllib.request import Request, ProxyHandler, build_opener


INST = os.environ.get("OKX_TARGET_INST", "KUAI-USDT-SWAP")


def load_env(path: str) -> None:
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ[key] = value.strip().strip("'\"")


class Client:
    def __init__(self):
        self.key = os.environ["OKX_API_KEY"]
        self.secret = os.environ["OKX_SECRET_KEY"]
        self.passphrase = os.environ["OKX_PASSPHRASE"]
        self.base = os.environ.get("OKX_BASE_URL", "https://www.okx.com")
        proxy = os.environ.get("ARB_HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
        self.opener = build_opener(ProxyHandler({"http": proxy, "https": proxy})) if proxy else None

    def call(self, method: str, path: str, params=None, body=None, private=True):
        query = urlencode(params or {})
        full = path + (("?" + query) if query else "")
        payload = json.dumps(body or {}, separators=(",", ":")) if method != "GET" else ""
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "User-Agent": "arb-monitor-okx-smoke/0.1"}
        if private:
            sig = base64.b64encode(hmac.new(
                self.secret.encode(), (ts + method + full + payload).encode(),
                hashlib.sha256,
            ).digest()).decode()
            headers.update({
                "OK-ACCESS-KEY": self.key, "OK-ACCESS-SIGN": sig,
                "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": self.passphrase,
            })
        fetch = self.opener.open if self.opener else __import__("urllib.request", fromlist=["urlopen"]).urlopen
        with fetch(Request(self.base + full, data=payload.encode() if method != "GET" else None,
                           headers=headers, method=method), timeout=15) as response:
            result = json.loads(response.read())
        if result.get("code") != "0":
            raise RuntimeError(f"{path}: OKX code {result.get('code')}")
        return result.get("data", [])


def main() -> int:
    load_env(sys.argv[1] if len(sys.argv) > 1 else "config/okx-demo.env")
    if os.environ.get("OKX_SIMULATED") != "0":
        raise SystemExit("refusing: OKX_SIMULATED must be 0 for this explicitly authorized smoke test")
    c = Client()
    inst = next((x for x in c.call("GET", "/api/v5/public/instruments",
                                   {"instType": "SWAP"}, private=False)
                 if x.get("instId") == INST), None)
    if not inst or inst.get("state") != "live":
        candidates = [x.get("instId") for x in c.call(
            "GET", "/api/v5/account/instruments", {"instType": "SWAP"})
            if any(term in (x.get("instId", "") + x.get("instFamily", "")).upper()
                   for term in ("KUAI", "XIAOMI", "MEITUAN", "POPMART", "SHEIN"))]
        print(json.dumps({"target": INST, "candidates": candidates}, sort_keys=True))
        raise SystemExit(f"refusing: {INST} is not a live OKX SWAP instrument")
    minimum = Decimal(inst["minSz"])
    step = Decimal(inst["lotSz"])
    if minimum <= 0 or minimum % step != 0:
        raise SystemExit("refusing: invalid minimum/lot size")
    ticker = c.call("GET", "/api/v5/market/ticker", {"instId": INST}, private=False)
    if not ticker or not ticker[0].get("bidPx"):
        raise SystemExit("refusing: no bid price for bounded IOC")
    bid = Decimal(ticker[0]["bidPx"])
    balance = c.call("GET", "/api/v5/account/balance", {"ccy": "USDT"})
    positions = c.call("GET", "/api/v5/account/positions", {"instId": INST})
    pending = c.call("GET", "/api/v5/trade/orders-pending",
                     {"instType": "SWAP", "instId": INST})
    # OKX may return a historical position row with pos="0"; only non-zero
    # exposure must block a new opening order.
    active_positions = [
        row for row in positions
        if Decimal(row.get("pos", "0") or "0") != 0
    ]
    if active_positions or pending:
        raise SystemExit("refusing: target has existing positions or pending orders")
    avail = Decimal(balance[0]["details"][0]["availBal"]) if balance and balance[0].get("details") else Decimal("0")
    print(json.dumps({"preflight": "ok", "instrument": INST, "minSz": str(minimum),
                      "lotSz": str(step), "bidPx": str(bid), "availableUSDT": str(avail)},
                     sort_keys=True))
    client_id = "arbkuai" + datetime.now(timezone.utc).strftime("%H%M%S%f")[:16]
    order = c.call("POST", "/api/v5/trade/order", body={
        "instId": INST, "tdMode": "cross", "side": "sell", "ordType": "ioc",
        "sz": str(minimum), "px": str(bid), "clOrdId": client_id,
    })
    if not order or order[0].get("sCode") != "0":
        raise SystemExit("order rejected: " + json.dumps(order, sort_keys=True))
    ord_id = order[0]["ordId"]
    result = c.call("GET", "/api/v5/trade/order",
                    {"instId": INST, "ordId": ord_id})
    fills = c.call("GET", "/api/v5/trade/fills",
                   {"instType": "SWAP", "instId": INST, "ordId": ord_id})
    after_balance = c.call("GET", "/api/v5/account/balance", {"ccy": "USDT"})
    after_positions = c.call("GET", "/api/v5/account/positions", {"instId": INST})
    print(json.dumps({"order": result, "fills": fills, "balance": after_balance,
                      "positions": after_positions, "clientOrderId": client_id},
                     sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
