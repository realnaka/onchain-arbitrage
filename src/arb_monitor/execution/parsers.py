from __future__ import annotations

from decimal import Decimal
from .adapters import InstrumentSpec, OrderResult


def _d(v, default="0"): return Decimal(str(v if v is not None else default))


def parse_binance_instrument(row: dict) -> InstrumentSpec:
    filters = {f["filterType"]: f for f in row.get("filters", [])}
    return InstrumentSpec(row["symbol"], _d(row.get("contractSize", "1")),
        _d(filters["LOT_SIZE"]["minQty"]), _d(filters["LOT_SIZE"]["stepSize"]),
        _d(filters["PRICE_FILTER"]["tickSize"]), row.get("marginAsset") or row["quoteAsset"],
        linear=row.get("contractType") == "PERPETUAL", metadata={"source": "binance"})


def parse_okx_instrument(row: dict) -> InstrumentSpec:
    return InstrumentSpec(row["instId"], _d(row.get("ctVal", "1")), _d(row["minSz"]),
        _d(row["lotSz"]), _d(row["tickSz"]), row["settleCcy"],
        linear=row.get("instType") == "SWAP", metadata={"source": "okx", "state": row.get("state")})


def parse_binance_order(row: dict) -> OrderResult:
    status = row["status"].lower()
    status = {"partially_filled": "partially_filled", "filled": "filled",
              "new": "open", "canceled": "cancelled"}.get(status, status)
    return OrderResult(str(row["orderId"]), status, _d(row.get("executedQty")),
                       _d(row["avgPrice"]) if row.get("avgPrice") else None,
                       _d(row.get("commission", "0")), metadata={"source": "binance", "raw": row})


def parse_okx_order(row: dict) -> OrderResult:
    item = row["data"][0] if "data" in row else row
    status = {"partially-filled": "partially_filled", "filled": "filled",
              "live": "open", "canceled": "cancelled"}.get(item["state"], item["state"])
    return OrderResult(str(item["ordId"]), status, _d(item.get("accFillSz")),
                       _d(item["avgPx"]) if item.get("avgPx") else None,
                       abs(_d(item.get("fee", "0"))), metadata={
                           "source": "okx", "raw": row, "fee_currency": item.get("feeCcy"),
                           "execution_type": item.get("execType"),
                           "trade_id": item.get("tradeId"),
                           "fill_price": item.get("fillPx"),
                           "fill_size": item.get("fillSz"),
                           "reduce_only": item.get("reduceOnly") == "true",
                       })
