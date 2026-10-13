"""Kraken spot client, tokenized-asset (xStocks) aware. Read-only by default.

Two things bit us on 2026-10-05 and are pinned by tests here:

1. **Nonce must be nanoseconds.** This API key had already been used with a
   nanosecond nonce, so every millisecond/microsecond nonce came back
   ``EAPI:Invalid nonce``. A wrong signature reports ``EAPI:Invalid key``
   instead, which is what made the key look dead.
2. **Tokenized pairs need the ``BASE/QUOTE`` spelling plus
   ``asset_class=tokenized_asset``.** ``MRNAXUSD`` alone answers
   ``EGeneral:Internal error``. ``MRNAx/USD`` + ``asset_class`` works.

Nothing in this module trades unless the caller passes ``confirm="live"``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from decimal import Decimal
from pathlib import Path

BASE_URL = "https://api.kraken.com"
TOKENIZED = "tokenized_asset"
LIVE_CONFIRM = "live"


class KrakenError(RuntimeError):
    """Kraken answered with a non-empty ``error`` list."""

    def __init__(self, errors, path: str):
        self.errors = list(errors)
        self.path = path
        super().__init__(f"{path}: {'; '.join(self.errors)}")


def tokenized_pair(symbol: str, quote: str = "USD") -> str:
    """``MRNAx`` -> ``MRNAx/USD``. Private endpoints reject ``MRNAXUSD``."""
    return f"{symbol}/{quote}"


def signature_message(path: str, nonce: str, postdata: str) -> bytes:
    """Kraken: ``path + SHA256(nonce || postdata)``.

    The nonce string is prepended, and it is also the first field of
    ``postdata``. That is Kraken's documented example, not a bug.
    """
    return path.encode() + hashlib.sha256((nonce + postdata).encode()).digest()


def sign(secret_b64: str, path: str, nonce: str, postdata: str) -> str:
    mac = hmac.new(base64.b64decode(secret_b64), signature_message(path, nonce, postdata),
                   hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


class Nonce:
    """Strictly increasing nanosecond nonce. Never reuse, never go backwards."""

    def __init__(self, clock=time.time_ns):
        self._clock = clock
        self._last = 0

    def next(self) -> str:
        value = self._clock()
        if value <= self._last:
            value = self._last + 1
        self._last = value
        return str(value)


def _default_transport(path: str, data: dict, headers: dict, timeout: float) -> dict:
    import requests  # imported lazily so hermetic tests never touch it

    url = BASE_URL + path
    response = requests.post(url, data=data, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


class KrakenSpotClient:
    """Read-first Kraken spot client for xStocks.

    ``transport`` is injectable: tests pass a fake, production uses requests.
    ``proxy`` is passed through as ``proxies`` by the default transport.
    """

    def __init__(self, api_key: str | None = None, secret_b64: str | None = None, *,
                 transport=None, nonce: Nonce | None = None, timeout: float = 40.0,
                 proxy: str | None = None):
        self.api_key = api_key
        self.secret_b64 = secret_b64
        self.nonce = nonce or Nonce()
        self.timeout = timeout
        self.proxy = proxy
        self.calls: list[tuple[str, dict]] = []
        self._transport = transport

    # ---- plumbing -------------------------------------------------------

    def _post(self, path: str, data: dict | None = None) -> dict:
        payload = {k: str(v) for k, v in (data or {}).items()}
        nonce = self.nonce.next()
        payload["nonce"] = nonce
        # nonce first, exactly like Kraken's own example
        ordered = {"nonce": nonce, **{k: v for k, v in payload.items() if k != "nonce"}}
        postdata = urllib.parse.urlencode(ordered)
        self.calls.append((path, dict(payload)))
        if self.secret_b64 is None:
            raise KrakenError(["EAPI:Missing credentials"], path)
        headers = {
            "API-Key": self.api_key or "",
            "API-Sign": sign(self.secret_b64, path, nonce, postdata),
        }
        if self._transport is None:
            if self.proxy:
                import requests
                response = requests.post(BASE_URL + path, data=postdata, headers=headers,
                                         timeout=self.timeout,
                                         proxies={"http": self.proxy, "https": self.proxy})
                response.raise_for_status()
                body = response.json()
            else:
                body = _default_transport(path, postdata, headers, self.timeout)
        else:
            body = self._transport(path, postdata, headers, self.timeout)
        errors = body.get("error") or []
        if errors:
            raise KrakenError(errors, path)
        return body.get("result")

    # ---- private --------------------------------------------------------

    def balance(self) -> dict[str, Decimal]:
        result = self._post("/0/private/Balance") or {}
        return {asset: Decimal(str(amount)) for asset, amount in result.items()}

    def open_orders(self) -> list[dict]:
        result = self._post("/0/private/OpenOrders") or {}
        return list((result.get("open") or {}).values())

    def trade_volume(self, pair: str | None = None) -> dict:
        data = {"pair": pair} if pair else {}
        return self._post("/0/private/TradeVolume", data) or {}

    def deposit_methods(self, asset: str, aclass: str = TOKENIZED) -> list[dict]:
        return self._post("/0/private/DepositMethods",
                          {"asset": asset, "aclass": aclass}) or []

    def deposit_addresses(self, asset: str, method: str,
                          aclass: str = TOKENIZED) -> list[dict]:
        return self._post("/0/private/DepositAddresses",
                          {"asset": asset, "aclass": aclass, "method": method}) or []

    def validate_order(self, symbol: str, volume, *, side: str = "sell",
                       ordertype: str = "limit", price=None,
                       quote: str = "USD", oflags: str | None = None,
                       tokenized: bool = True, aclass: str = TOKENIZED) -> dict:
        """``validate=true``. Kraken documents it as not placing an order."""
        data = {
            "type": side,
            "ordertype": ordertype,
            "volume": str(volume),
            "validate": "true",
        }
        # xStocks 要写 ``MRNAx/USD`` 加 ``asset_class``；普通币对（如 USDGUSD）
        # 用交易对代码本身，多带 asset_class 会得到 EQuery:Unknown asset pair。
        if tokenized:
            data["pair"] = tokenized_pair(symbol, quote)
            data["asset_class"] = aclass
        else:
            data["pair"] = symbol
        if price is not None:
            data["price"] = str(price)
        if oflags:
            data["oflags"] = oflags
        return self._post("/0/private/AddOrder", data) or {}

    def order(self, symbol: str, volume, *, side: str = "sell",
              ordertype: str = "limit", price=None, quote: str = "USD",
              oflags: str | None = None, aclass: str = TOKENIZED,
              tokenized: bool = True, confirm: str = "") -> dict:
        """Place a real order. Refuses unless ``confirm="live"`` is passed."""
        if confirm != LIVE_CONFIRM:
            raise KrakenError(["EAPI:Refused:missing live confirmation"],
                              "/0/private/AddOrder")
        data = {
            "type": side,
            "ordertype": ordertype,
            "volume": str(volume),
        }
        if tokenized:
            data["pair"] = tokenized_pair(symbol, quote)
            data["asset_class"] = aclass
        else:
            data["pair"] = symbol
        if price is not None:
            data["price"] = str(price)
        if oflags:
            data["oflags"] = oflags
        return self._post("/0/private/AddOrder", data) or {}

    # ---- public ---------------------------------------------------------

    def asset_pairs(self, aclass_base: str = TOKENIZED) -> dict:
        import requests

        kwargs = {"params": {"aclass_base": aclass_base}, "timeout": self.timeout}
        if self.proxy:
            kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
        response = requests.get(BASE_URL + "/0/public/AssetPairs", **kwargs)
        response.raise_for_status()
        body = response.json()
        if body.get("error"):
            raise KrakenError(body["error"], "/0/public/AssetPairs")
        return body["result"]

    def depth(self, pair: str, *, tokenized: bool = True, count: int = 25) -> dict:
        """公开盘口。``pair`` 传 ``VIDAx``（tokenized）或 ``USDGUSD``（普通）。"""
        import requests

        params = {"pair": tokenized_pair(pair) if tokenized else pair, "count": count}
        if tokenized:
            params.update({"asset_class": TOKENIZED, "assetVersion": 1})
        kwargs = {"params": params, "timeout": self.timeout}
        if self.proxy:
            kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
        response = requests.get(BASE_URL + "/0/public/Depth", **kwargs)
        response.raise_for_status()
        body = response.json()
        if body.get("error"):
            raise KrakenError(body["error"], "/0/public/Depth")
        rows = body.get("result") or {}
        return list(rows.values())[0] if rows else {}


def load_credentials(env_path: str | Path) -> tuple[str, str]:
    """Read ``config/kraken.env``. Never log the values."""
    key = secret = None
    for line in Path(env_path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name == "KRAKEN_API_KEY":
            key = value
        elif name == "KRAKEN_API_SECRET":
            secret = value
    if not key or not secret:
        raise KrakenError(["EAPI:Missing credentials"], str(env_path))
    return key, secret
