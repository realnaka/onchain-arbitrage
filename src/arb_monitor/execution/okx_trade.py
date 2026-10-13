"""OKX order-entry client for the explicitly authorised execution path.

Reads are delegated to :class:`OKXReadOnlyClient`. Writes exist here and only
here, behind three locks that all have to open:

1. ``allow_orders`` must be true — the default instance cannot trade;
2. the caller must pass a ``guard`` that re-checks the automation policy;
3. the client remembers every ``clOrdId`` it has sent, so an ambiguous write is
   resolved by querying that id and is never replayed.

There is no cancel-and-retry, no replace, no batch and no leverage change.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

from .okx_account import (DEFAULT_BASE_URL, OKXReadError, OKXReadOnlyClient,
                          okx_signature, okx_timestamp)


class OKXTradeError(RuntimeError):
    """Order entry refused, rejected, or left in an unknown state."""


def _http_post(url: str, headers: Mapping[str, str], body: str, timeout: int,
               proxy: str | None) -> Mapping[str, Any]:
    request = urllib.request.Request(url, data=body.encode(), headers=dict(headers),
                                     method="POST")
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        fetch = opener.open
    else:
        fetch = urllib.request.urlopen
    with fetch(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


@dataclass
class OKXTradeClient:
    read: OKXReadOnlyClient
    allow_orders: bool = False
    transport: Callable[..., Mapping[str, Any]] | None = None
    submitted_client_ids: list[str] = field(default_factory=list)

    # --- public, unauthenticated market data ---------------------------
    def public(self, path: str, query: str = "") -> list[dict]:
        url = self.read.base_url + path + (("?" + query) if query else "")
        request = urllib.request.Request(url, headers={"User-Agent": "arb-monitor/0.1"})
        opener = None
        if self.read.proxy:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(
                {"http": self.read.proxy, "https": self.read.proxy}))
        try:
            fetch = opener.open if opener else urllib.request.urlopen
            with fetch(request, timeout=self.read.timeout) as response:
                payload = json.loads(response.read().decode())
        except Exception as exc:
            raise OKXReadError(f"okx_public_transport:{type(exc).__name__}") from exc
        if not isinstance(payload, Mapping) or payload.get("code") != "0":
            # The venue's own words are the whole diagnosis: "okx_public_api_error"
            # alone does not say whether this was a rate limit, a bad instrument
            # id or a maintenance window.
            code = payload.get("code") if isinstance(payload, Mapping) else "shape"
            message = payload.get("msg") if isinstance(payload, Mapping) else ""
            raise OKXReadError(f"okx_public_api_error:{code}:{str(message)[:120]}")
        return list(payload.get("data") or [])

    def ticker(self, inst_id: str) -> dict:
        rows = self.public("/api/v5/market/ticker", f"instId={inst_id}")
        if not rows:
            raise OKXReadError("okx_ticker_unavailable")
        return rows[0]

    def instrument(self, inst_id: str) -> dict:
        rows = self.public("/api/v5/public/instruments", f"instType=SWAP&instId={inst_id}")
        if not rows:
            raise OKXReadError("okx_instrument_unavailable")
        return rows[0]

    # --- order entry ---------------------------------------------------
    def post(self, path: str, body: dict) -> list[dict]:
        """Signed POST. Guarded by ``allow_orders`` and never retried."""
        if not self.allow_orders:
            raise OKXTradeError("order_entry_disabled")
        if not self.read.configured:
            raise OKXTradeError("missing_okx_credentials")
        payload = json.dumps(body, separators=(",", ":"))
        timestamp = okx_timestamp()
        headers = {
            "Accept": "application/json", "Content-Type": "application/json",
            "User-Agent": "arb-monitor-exec/0.1",
            "OK-ACCESS-KEY": self.read.api_key,
            "OK-ACCESS-SIGN": okx_signature(timestamp, "POST", path, payload,
                                            self.read.secret_key),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.read.passphrase,
        }
        if self.read.simulated:
            headers["x-simulated-trading"] = "1"
        fetch = self.transport or _http_post
        try:
            if self.transport is None:
                result = fetch(self.read.base_url + path, headers, payload,
                               self.read.timeout, self.read.proxy)
            else:
                result = fetch(self.read.base_url + path, headers, payload)
        except Exception as exc:
            # The request may have reached OKX: stop and reconcile by clOrdId.
            raise OKXTradeError(f"unknown_write_state:{type(exc).__name__}") from exc
        if not isinstance(result, Mapping) or result.get("code") != "0":
            code = result.get("code") if isinstance(result, Mapping) else "shape"
            detail = ""
            if isinstance(result, Mapping):
                data = result.get("data")
                first = data[0] if isinstance(data, list) and data else {}
                # The per-order reason ("sCode/sMsg") is the useful one; the
                # top-level "All operations failed" says nothing.
                if first.get("sMsg"):
                    detail = f"{first.get('sCode')}:{first['sMsg']}"
                else:
                    detail = str(result.get("msg") or "")[:160]
            # The venue's own words matter: "code 1" alone is not diagnosable.
            raise OKXTradeError(f"okx_api_code_{code}:{detail}")
        return list(result.get("data") or [])

    def place_ioc(self, *, inst_id: str, side: str, size: Decimal, price: Decimal,
                  client_order_id: str, reduce_only: bool = False,
                  td_mode: str = "cross") -> dict:
        if client_order_id in self.submitted_client_ids:
            raise OKXTradeError("clordid_already_submitted")
        if side not in {"buy", "sell"}:
            raise OKXTradeError("invalid_side")
        if size <= 0 or price <= 0:
            raise OKXTradeError("invalid_size_or_price")
        body = {
            "instId": inst_id, "tdMode": td_mode, "side": side, "ordType": "ioc",
            "sz": _plain(size), "px": _plain(price), "clOrdId": client_order_id,
        }
        if reduce_only:
            body["reduceOnly"] = "true"
        # Recorded before the call: an exception must not make this id reusable.
        self.submitted_client_ids.append(client_order_id)
        try:
            rows = self.post("/api/v5/trade/order", body)
        except OKXTradeError as exc:
            # Measured 2026-09-16: OKX answered `code=1, "All operations failed"`
            # for an order that had *filled*. Concluding "not sent" from that
            # response left a hedge unwound and the spot leg unsold. Resolve the
            # order by its own client id before believing the order failed.
            resolved = self._resolve_by_client_id(inst_id, client_order_id)
            if resolved is not None:
                return resolved
            raise
        if not rows or rows[0].get("sCode") not in ("0", None):
            resolved = self._resolve_by_client_id(inst_id, client_order_id)
            if resolved is not None:
                return resolved
            raise OKXTradeError("order_rejected:" + json.dumps(rows)[:200])
        if rows[0].get("sCode") == "0" and not rows[0].get("ordId"):
            raise OKXTradeError("order_id_missing")
        return rows[0]

    def _resolve_by_client_id(self, inst_id: str, client_order_id: str) -> dict | None:
        """Ask the venue what happened to this client order id, if anything.

        Returns the order row when it exists; None when the venue is certain
        there is no such order. A read failure returns None *and* leaves the
        caller's error in place — an unknown state must stay unknown, never be
        reinterpreted as "nothing happened".
        """
        try:
            rows = self.read.get(
                "/api/v5/trade/order", f"instId={inst_id}&clOrdId={client_order_id}")
        except Exception:  # noqa: BLE001 - unknown stays unknown
            return None
        if not rows:
            return None
        row = rows[0]
        if row.get("ordId"):
            return {"ordId": row["ordId"], "sCode": "0", "sMsg": "",
                    "resolvedByClientId": True, "state": row.get("state"),
                    "accFillSz": row.get("accFillSz")}
        return None

    # --- maker orders ---------------------------------------------------
    def set_leverage(self, inst_id: str, lever: str = "1",
                      td_mode: str = "cross") -> dict:
        """Set the instrument leverage before an open. Default is 1x."""
        rows = self.post("/api/v5/account/set-leverage",
                         {"instId": inst_id, "lever": str(lever), "mgnMode": td_mode})
        return rows[0] if rows else {}

    def place_post_only(self, *, inst_id: str, side: str, size: Decimal,
                        price: Decimal, client_order_id: str,
                        td_mode: str = "cross",
                        reduce_only: bool = False) -> dict:
        """Rest an order that must never take liquidity.

        ``post_only`` is the only order type that guarantees this: if the price
        would cross, the venue rejects it instead of filling it as a taker. That
        property is the whole point of the maker-first strategy — the fee and
        the spread both depend on not crossing.

        ``reduce_only`` is what a *closing* resting order needs, for the same
        reason the closing taker order has it: a buy-back must be able to shrink
        the short and must never be able to flip it into a long. It is the
        caller's choice because opening a position must not carry the flag.
        """
        if client_order_id in self.submitted_client_ids:
            raise OKXTradeError("clordid_already_submitted")
        if side not in {"buy", "sell"}:
            raise OKXTradeError("invalid_side")
        if size <= 0 or price <= 0:
            raise OKXTradeError("invalid_size_or_price")
        body = {"instId": inst_id, "tdMode": td_mode, "side": side,
                "ordType": "post_only", "sz": _plain(size), "px": _plain(price),
                "clOrdId": client_order_id}
        if reduce_only:
            body["reduceOnly"] = "true"
        self.submitted_client_ids.append(client_order_id)
        try:
            rows = self.post("/api/v5/trade/order", body)
        except OKXTradeError as exc:
            resolved = self._resolve_by_client_id(inst_id, client_order_id)
            if resolved is not None:
                return resolved
            raise
        first = rows[0] if rows else {}
        if first.get("sCode") not in ("0", None):
            # 51121/51119 family: a post-only price that would cross. That is a
            # refusal to trade as a taker, which is the intended behaviour.
            resolved = self._resolve_by_client_id(inst_id, client_order_id)
            if resolved is not None:
                return resolved
            raise OKXTradeError(
                f"post_only_rejected:{first.get('sCode')}:{first.get('sMsg')}")
        if not first.get("ordId"):
            raise OKXTradeError("order_id_missing")
        return first

    def get_order(self, inst_id: str, ord_id: str) -> dict:
        rows = self.read.get("/api/v5/trade/order", f"instId={inst_id}&ordId={ord_id}")
        if not rows:
            raise OKXTradeError("order_not_found")
        return rows[0]

    def cancel_order(self, inst_id: str, ord_id: str) -> dict:
        rows = self.post("/api/v5/trade/cancel-order",
                         {"instId": inst_id, "ordId": ord_id})
        first = rows[0] if rows else {}
        if first.get("sCode") not in ("0", None):
            # 51402/51400 = already gone; that is an answer, not a failure.
            if str(first.get("sCode")) in {"51400", "51402", "51169"}:
                return {"cancelled": False, "sCode": first.get("sCode")}
            raise OKXTradeError(
                f"cancel_rejected:{first.get('sCode')}:{first.get('sMsg')}")
        return {"cancelled": True, "sCode": "0"}

    def cancel_by_client_id(self, inst_id: str, client_order_id: str) -> dict:
        """Cancel an order by its clOrdId, without needing to read it first.

        `cancel_order` needs the venue's ``ordId``, which can only be learned by
        reading the order back — the read that is unreliable exactly when this
        matters. OKX accepts the client id on the same endpoint, so a halt can
        still pull its own order while every read is down. Measured 2026-09-21:
        the best-effort cancel in the maker flow had no clOrdId path and no
        retry, the order survived the halt, and it filled three minutes later.
        """
        if not client_order_id:
            raise OKXTradeError("missing_client_order_id")
        rows = self.post("/api/v5/trade/cancel-order",
                         {"instId": inst_id, "clOrdId": client_order_id})
        first = rows[0] if rows else {}
        if first.get("sCode") not in ("0", None):
            # 51400/51402/51169 all mean "there is nothing left to cancel",
            # which is an answer: the order is not resting.
            if str(first.get("sCode")) in {"51400", "51402", "51169"}:
                return {"cancelled": False, "sCode": first.get("sCode"),
                        "clOrdId": client_order_id}
            raise OKXTradeError(
                f"cancel_rejected:{first.get('sCode')}:{first.get('sMsg')}")
        return {"cancelled": True, "sCode": "0", "clOrdId": client_order_id}

def _plain(value: Decimal) -> str:
    """Decimal without exponent so OKX accepts it verbatim."""
    try:
        value = Decimal(str(value))
    except InvalidOperation as exc:
        raise OKXTradeError("invalid_decimal") from exc
    return format(value.normalize(), "f")
