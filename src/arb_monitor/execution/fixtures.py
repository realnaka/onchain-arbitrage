"""Small, versioned-shaped response fixtures for adapter contract tests."""

BINANCE_EXCHANGE_INFO = {
    "symbols": [{
        "symbol": "XIAOMIUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT",
        "marginAsset": "USDT", "status": "TRADING", "contractSize": "1",
        "filters": [{"filterType": "LOT_SIZE", "minQty": "1", "stepSize": "1"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.001"}]
    }]
}

OKX_INSTRUMENT = {
    "code": "0", "data": [{
        "instId": "XIAOMI-USDT-SWAP", "instType": "SWAP", "settleCcy": "USDT",
        "ctVal": "1", "minSz": "1", "lotSz": "1", "tickSz": "0.001", "state": "live"
    }]
}

BINANCE_ORDER = {"orderId": 123, "status": "PARTIALLY_FILLED", "executedQty": "4", "avgPrice": "10"}
OKX_ORDER = {"code": "0", "data": [{"ordId": "456", "state": "partially-filled", "accFillSz": "4", "avgPx": "10"}]}
