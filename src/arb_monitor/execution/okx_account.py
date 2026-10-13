"""Read-only OKX v5 account client (balances, positions, orders, fills).

This client is deliberately one-directional: it only ever issues signed ``GET``
requests. There is no order entry, no cancel, no transfer and no place to put a
private key beyond the signing secrets that OKX already requires for reads.

Two callers use it:

* the account dashboard, which shows balances, positions and open orders;
* the cost calibration, which compares the model's fee/slippage assumptions
  against fees that were really charged.

Every failure is raised as ``OKXReadError`` so callers can render ``UNKNOWN``
instead of a stale number. A read-only viewer must never guess.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

DEFAULT_BASE_URL = "https://www.okx.com"


class OKXReadError(RuntimeError):
    """Any read that did not complete. Callers must render UNKNOWN."""


def okx_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def okx_signature(timestamp: str, method: str, path: str, body: str, secret: str) -> str:
    message = timestamp + method.upper() + path + body
    return base64.b64encode(
        hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()
    ).decode()


def _http_get(url: str, headers: Mapping[str, str], timeout: int,
              proxy: str | None) -> Mapping[str, Any]:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
        fetch = opener.open
    else:
        fetch = urllib.request.urlopen
    with fetch(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


@dataclass
class OKXReadOnlyClient:
    """Signed read-only access to one OKX account (demo or live sub-account)."""

    api_key: str
    secret_key: str = field(repr=False)
    passphrase: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL
    simulated: bool = True
    proxy: str | None = None
    timeout: int = 15
    transport: Callable[..., Mapping[str, Any]] | None = None
    #: Bounded read-only retries. Default 0 keeps every existing caller
    #: single-shot; the live stack passes `[network] read_retries` from
    #: `config/automation_policy.toml`, which is where the project already
    #: says reads may be retried and writes may not.
    read_retries: int = 0
    retry_backoff_seconds: float = 0.6
    #: ``[network] routes`` in order (``None`` == direct). A signed *GET* may be
    #: retried and may change route; a write may not, which is why this lives on
    #: the read-only client. Attempts rotate through the routes so the total
    #: budget stays ``read_retries + 1`` and a stuck proxy cannot spend the whole
    #: cycle: measured 2026-09-21, the shared proxy answered one OKX read with a
    #: URLError while the two other configured routes were never tried.
    routes: tuple[str | None, ...] = ()

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **kwargs) -> "OKXReadOnlyClient":
        env = environ if environ is not None else os.environ
        proxy = (kwargs.pop("proxy", None)
                 or env.get("ARB_HTTP_PROXY") or env.get("HTTPS_PROXY") or None)
        return cls(
            api_key=env.get("OKX_API_KEY", ""),
            secret_key=env.get("OKX_SECRET_KEY", ""),
            passphrase=env.get("OKX_PASSPHRASE", ""),
            base_url=env.get("OKX_BASE_URL", DEFAULT_BASE_URL),
            simulated=env.get("OKX_SIMULATED", "1") == "1",
            proxy=proxy,
            **kwargs,
        )

    @property
    def configured(self) -> bool:
        return all((self.api_key, self.secret_key, self.passphrase))

    def get(self, path: str, query: str = "") -> list[dict]:
        if not self.configured:
            raise OKXReadError("missing_okx_credentials")
        full_path = path + (("?" + query) if query else "")
        timestamp = okx_timestamp()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "arb-monitor-readonly/0.1",
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": okx_signature(timestamp, "GET", full_path, "", self.secret_key),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
        }
        if self.simulated:
            headers["x-simulated-trading"] = "1"
        fetch = self.transport or _http_get
        routes = tuple(self.routes) or (self.proxy,)
        attempts = max(1, int(self.read_retries) + 1)
        payload: Mapping[str, Any] | None = None
        last: Exception | None = None
        for attempt in range(attempts):
            route = routes[attempt % len(routes)]
            try:
                if self.transport is None:
                    payload = fetch(self.base_url + full_path, headers,
                                    self.timeout, route)
                else:
                    payload = fetch(self.base_url + full_path, headers)
                break
            except urllib.error.HTTPError as exc:  # OKX returns JSON on 4xx too
                # An HTTP status is an answer, not a transport accident, and
                # retrying a 4xx just spends the same rate limit twice.
                raise OKXReadError(f"okx_http_{exc.code}") from exc
            except Exception as exc:  # timeout, DNS, TLS, proxy refusal
                last = exc
                if attempt + 1 < attempts:
                    time.sleep(self.retry_backoff_seconds * (attempt + 1))
        if payload is None:
            raise OKXReadError(f"okx_transport:{type(last).__name__}") from last
        if not isinstance(payload, Mapping) or payload.get("code") != "0":
            code = payload.get("code") if isinstance(payload, Mapping) else "shape"
            raise OKXReadError(f"okx_api_code_{code}")
        data = payload.get("data")
        return list(data) if isinstance(data, Sequence) else []

    # --- typed helpers -------------------------------------------------
    def account_config(self) -> dict:
        rows = self.get("/api/v5/account/config")
        return rows[0] if rows else {}

    def balances(self, ccy: str | None = None) -> list[dict]:
        rows = self.get("/api/v5/account/balance", f"ccy={ccy}" if ccy else "")
        return list((rows[0].get("details") if rows else []) or [])

    def positions(self, inst_type: str = "SWAP", inst_id: str | None = None) -> list[dict]:
        query = f"instType={inst_type}" + (f"&instId={inst_id}" if inst_id else "")
        rows = self.get("/api/v5/account/positions", query)
        # OKX can return a historical row with pos=0; that is not exposure.
        return [row for row in rows if str(row.get("pos", "0") or "0") not in {"0", ""}]

    def pending_orders(self, inst_type: str = "SWAP", inst_id: str | None = None) -> list[dict]:
        query = f"instType={inst_type}" + (f"&instId={inst_id}" if inst_id else "")
        return self.get("/api/v5/trade/orders-pending", query)

    def fills(self, inst_type: str = "SWAP", inst_id: str | None = None,
              limit: int | None = None) -> list[dict]:
        query = f"instType={inst_type}" + (f"&instId={inst_id}" if inst_id else "")
        if limit:
            query += f"&limit={int(limit)}"
        return self.get("/api/v5/trade/fills", query)

    def instruments(self, inst_type: str = "SWAP", inst_id: str | None = None) -> list[dict]:
        query = f"instType={inst_type}" + (f"&instId={inst_id}" if inst_id else "")
        return self.get("/api/v5/public/instruments", query)

    def trade_fee(self, inst_type: str = "SWAP", inst_id: str | None = None,
                  uly: str | None = None) -> dict:
        query = f"instType={inst_type}"
        if inst_id:
            query += f"&instId={inst_id}"
        if uly:
            query += f"&uly={uly}"
        rows = self.get("/api/v5/account/trade-fee", query)
        return rows[0] if rows else {}


def fee_bps(fee: Any, price: Any, size: Any) -> Decimal | None:
    """Signed fee expressed in bps of the traded notional."""
    try:
        price, size, fee = Decimal(str(price)), Decimal(str(size)), Decimal(str(fee))
    except (InvalidOperation, TypeError, ValueError):
        return None
    notional = abs(price * size)
    if notional <= 0:
        return None
    return abs(fee) / notional * Decimal(10000)


def taker_fee_bps(trade_fee_row: Mapping[str, Any]) -> Decimal | None:
    """OKX reports per-instrument taker rates in several shapes; normalise them."""
    for key in ("takerU", "taker", "takerUSDC"):
        value = trade_fee_row.get(key)
        if value not in (None, ""):
            try:
                return abs(Decimal(str(value))) * Decimal(10000)
            except (InvalidOperation, TypeError, ValueError):
                continue
    for group in trade_fee_row.get("feeGroup") or []:
        for key in ("taker", "takerU"):
            value = group.get(key)
            if value not in (None, ""):
                try:
                    return abs(Decimal(str(value))) * Decimal(10000)
                except (InvalidOperation, TypeError, ValueError):
                    continue
    return None
