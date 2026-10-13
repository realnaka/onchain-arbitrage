from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from .adapters import Balance, InstrumentSpec, OrderRequest, OrderResult
from .parsers import parse_okx_instrument, parse_okx_order
from .network import NetworkFault, NetworkState, RequestTracker, Resolution, RemoteOrderState
from .recovery import OrderRecovery, RecoveryState, recover_cancel_loop


def okx_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def okx_signature(timestamp: str, method: str, path: str, body: str, secret: str) -> str:
    message = timestamp + method.upper() + path + body
    return base64.b64encode(hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()).decode()


@dataclass
class OKXDemoClient:
    """Transport-injected OKX v5 client. Defaults to no transport and never sends."""
    api_key: str
    secret_key: str = field(repr=False)
    passphrase: str = field(repr=False)
    transport: Callable[..., dict[str, Any]] | None = None
    base_url: str = "https://www.okx.com"
    simulated: bool = True
    request_tracker: RequestTracker = field(default_factory=RequestTracker, repr=False)

    def request(self, method: str, path: str, body: dict | None = None, query: str = ""):
        if not self.simulated:
            raise PermissionError("Only OKX demo trading is supported")
        if not all((self.api_key, self.secret_key, self.passphrase)):
            raise PermissionError("OKX demo credentials are missing")
        if self.transport is None:
            raise RuntimeError("OKX transport is not configured")
        payload = json.dumps(body or {}, separators=(",", ":")) if method.upper() != "GET" else ""
        full_path = path + (("?" + query) if query else "")
        record = self.request_tracker.start("okx", full_path)
        ts = okx_timestamp()
        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": okx_signature(ts, method, full_path, payload, self.secret_key),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        if self.simulated:
            headers["x-simulated-trading"] = "1"
        try:
            result = self.transport(method, self.base_url + full_path, headers, payload)
        except (TimeoutError, ConnectionError, OSError) as exc:
            self.request_tracker.outcome(
                record.request_id, NetworkState.REQUEST_TIMEOUT,
                may_have_reached_remote=method.upper() in {"POST", "PUT"},
            )
            raise NetworkFault("OKX transport failure: " + str(exc),
                               NetworkState.REQUEST_TIMEOUT,
                               may_have_reached_remote=method.upper() in {"POST", "PUT"}) from exc
        except Exception as exc:
            self.request_tracker.outcome(
                record.request_id, NetworkState.RESPONSE_UNKNOWN,
                may_have_reached_remote=method.upper() in {"POST", "PUT"},
            )
            raise NetworkFault("OKX response unavailable: " + str(exc),
                               NetworkState.RESPONSE_UNKNOWN,
                               may_have_reached_remote=method.upper() in {"POST", "PUT"}) from exc
        self.request_tracker.outcome(record.request_id, NetworkState.HEALTHY,
                                     confirmation_source="api_response")
        if result.get("code") != "0":
            raise RuntimeError("OKX API error code: " + str(result.get("code")))
        return result

    def get_instrument(self, inst_type: str, inst_id: str | None = None) -> InstrumentSpec:
        query = "instType=" + inst_type + (("&instId=" + inst_id) if inst_id else "")
        row = self.request("GET", "/api/v5/account/instruments", query=query)
        return parse_okx_instrument(row["data"][0])

    def get_balances(self) -> list[Balance]:
        row = self.request("GET", "/api/v5/account/balance")
        return [Balance(x["ccy"], Decimal(x["availBal"]), Decimal(x["cashBal"])) for x in row["data"][0]["details"]]

    def place_order(self, request: OrderRequest) -> OrderResult:
        if request.client_order_id is not None and (
            not re.fullmatch(r"[A-Za-z0-9]{1,32}", request.client_order_id)
        ):
            raise ValueError("OKX clOrdId must be 1-32 ASCII letters/digits")
        if request.order_type == "ioc" and request.price is None:
            raise ValueError("IOC requires a bounded limit price")
        body = {"instId": request.symbol, "tdMode": "cross", "side": request.side,
                "ordType": request.order_type, "sz": str(request.quantity)}
        if request.price is not None: body["px"] = str(request.price)
        if request.client_order_id: body["clOrdId"] = request.client_order_id
        if request.reduce_only: body["reduceOnly"] = "true"
        data = self.request("POST", "/api/v5/trade/order", body=body)["data"][0]
        if data.get("sCode") != "0":
            raise RuntimeError("OKX order rejected: " + str(data.get("sCode")))
        # An order acknowledgement is NOT a fill; query/order-channel confirmation is required.
        return OrderResult(str(data["ordId"]), "accepted", Decimal("0"))

    def get_order(self, symbol: str, order_id: str) -> OrderResult:
        row = self.request("GET", "/api/v5/trade/order",
                           query=f"instId={symbol}&ordId={order_id}")
        return parse_okx_order(row)

    def get_order_by_client_id(self, symbol: str, client_order_id: str) -> OrderResult:
        """Resolve an ambiguous order without submitting a replacement."""
        row = self.request("GET", "/api/v5/trade/order",
                           query=f"instId={symbol}&clOrdId={client_order_id}")
        return parse_okx_order(row)

    def resolve_client_order(self, symbol: str, client_order_id: str) -> Resolution[OrderResult]:
        try:
            result = self.get_order_by_client_id(symbol, client_order_id)
        except NetworkFault:
            return Resolution(RemoteOrderState.UNKNOWN, source="okx_query_unavailable")
        states = {
            "open": RemoteOrderState.OPEN,
            "partially_filled": RemoteOrderState.PARTIALLY_FILLED,
            "filled": RemoteOrderState.FILLED,
            "cancelled": RemoteOrderState.CANCELED,
            "rejected": RemoteOrderState.REJECTED,
        }
        return Resolution(states.get(result.status, RemoteOrderState.UNKNOWN),
                          result, "okx_order_query")

    def cancel_order(self, symbol: str, order_id: str) -> None:
        row = self.request("POST", "/api/v5/trade/cancel-order",
                           body={"instId": symbol, "ordId": order_id})
        item = row["data"][0]
        if item.get("sCode") != "0":
            raise RuntimeError("OKX cancel rejected: " + str(item.get("sCode")))

    def cancel_with_recovery(self, symbol: str, order_id: str,
                             client_order_id: str,
                             max_attempts: int = 3) -> RecoveryState:
        """Cancel once, then resolve by clOrdId with bounded queries."""
        recovery = OrderRecovery(client_order_id, max_attempts=max_attempts)
        try:
            self.cancel_order(symbol, order_id)
            return RecoveryState.CLOSED_CONFIRMED
        except (NetworkFault, RuntimeError) as exc:
            recovery.cancel_failed(str(exc))
            return recover_cancel_loop(
                recovery, lambda cid: self.resolve_client_order(symbol, cid)
            )

    def get_fills(self, symbol: str | None = None, order_id: str | None = None) -> list[OrderResult]:
        query = "instType=SWAP"
        if symbol: query += "&instId=" + symbol
        if order_id: query += "&ordId=" + order_id
        row = self.request("GET", "/api/v5/trade/fills", query=query)
        return [parse_okx_order({"data": [item]}) for item in row["data"]]

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "OKXDemoClient":
        env = environ or os.environ
        return cls(env.get("OKX_API_KEY", ""), env.get("OKX_SECRET_KEY", ""),
                   env.get("OKX_PASSPHRASE", ""),
                   simulated=env.get("OKX_SIMULATED", "1") == "1")
