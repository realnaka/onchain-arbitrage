"""Read-only OKX sub-account audit; never places, cancels, or modifies orders."""
from __future__ import annotations

import base64, hashlib, hmac, json, os, sys
from datetime import datetime, timezone
from urllib.request import Request, urlopen, ProxyHandler, build_opener


def load_env(path: str) -> None:
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ[key] = value.strip().strip("'\"")


def main() -> int:
    load_env(sys.argv[1] if len(sys.argv) > 1 else "config/okx-demo.env")
    key, secret, phrase = (os.environ.get(x, "") for x in
                           ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"))
    if not all((key, secret, phrase)):
        raise SystemExit("missing OKX_API_KEY/OKX_SECRET_KEY/OKX_PASSPHRASE")
    base = os.environ.get("OKX_BASE_URL", "https://www.okx.com")
    simulated = os.environ.get("OKX_SIMULATED", "1") == "1"
    proxy = os.environ.get("ARB_HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
    opener = build_opener(ProxyHandler({"http": proxy, "https": proxy})) if proxy else None

    def get(label: str, path: str, query: str = "") -> None:
        full = path + (("?" + query) if query else "")
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        sig = base64.b64encode(hmac.new(
            secret.encode(), (ts + "GET" + full).encode(), hashlib.sha256
        ).digest()).decode()
        headers = {"User-Agent": "arb-monitor-readonly/0.1", "Accept": "application/json",
                   "OK-ACCESS-KEY": key, "OK-ACCESS-SIGN": sig,
                   "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": phrase}
        if simulated:
            headers["x-simulated-trading"] = "1"
        try:
            fetch = opener.open if opener else urlopen
            with fetch(Request(base + full, headers=headers), timeout=15) as response:
                data = json.loads(response.read())
            print(json.dumps({"check": label, "ok": data.get("code") == "0",
                              "code": data.get("code"), "data": data.get("data")},
                             ensure_ascii=False, default=str))
        except Exception as exc:
            detail = ""
            if hasattr(exc, "read"):
                try:
                    detail = exc.read(300).decode("utf-8", "replace")
                except Exception:
                    pass
            print(json.dumps({"check": label, "ok": False,
                              "error": type(exc).__name__ + ": " + str(exc),
                              "detail": detail[:300]},
                             ensure_ascii=False))

    get("account_config", "/api/v5/account/config")
    get("account_balance_all", "/api/v5/account/balance")
    get("account_balance_usdt", "/api/v5/account/balance", "ccy=USDT")
    get("asset_balances", "/api/v5/asset/balances")
    get("positions", "/api/v5/account/positions")
    get("account_instruments", "/api/v5/account/instruments", "instType=SWAP")
    get("public_instruments", "/api/v5/public/instruments", "instType=SWAP")
    get("open_orders", "/api/v5/trade/orders-pending", "instType=SWAP")
    get("fills", "/api/v5/trade/fills", "instType=SWAP")
    get("trade_fee", "/api/v5/account/trade-fee", "instType=SWAP&uly=BTC-USDT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
