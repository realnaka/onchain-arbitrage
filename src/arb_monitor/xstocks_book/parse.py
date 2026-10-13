"""Pure parsers and the discount rule. No network, no sqlite."""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

ASSETS_API = "https://api.backed.fi/api/v2/public/assets"
BRIDGES_API = "https://api.xstocks.fi/api/v2/public/bridges"
KRAKEN_PAIRS = "https://api.kraken.com/0/public/AssetPairs?aclass_base=tokenized_asset"
KRAKEN_ASSETS = "https://api.kraken.com/0/public/Assets?aclass=tokenized_asset"
KRAKEN_DEPTH = "https://api.kraken.com/0/public/Depth"

SCREEN_BPS = Decimal(50)
_BID_BAND = Decimal("0.005")
_SHARE_KEYS = ("sharesPerToken", "shares_per_token", "multiplier", "tokenMultiplier")
_STABLE_KEYS = ("stablecoins", "stables", "supportedStablecoins", "collateralTokens", "tokens")
_NETWORKS = {
    "ethereum": "Ethereum",
    "eth": "Ethereum",
    "solana": "Solana",
    "ton": "TON",
    "arbitrum": "Arbitrum",
    "optimism": "Optimism",
    "bnbsmartchain": "BNB Smart Chain",
    "binancesmartchain": "BNB Smart Chain",
    "bsc": "BNB Smart Chain",
    "mantle": "Mantle",
    "xlayer": "XLayer",
    "hyperevm": "HyperEVM",
    "ink": "Ink",
    # 2026-10-03 发行方登记表新增 Monad。GeckoTerminal 还没有这个网络，
    # 所以 _GECKO 里没有对应 slug——未知就返回 None，不猜。
    "monad": "Monad",
}
# GeckoTerminal's own network slug. Unknown issuer networks are not guessed.
_GECKO = {
    "ethereum": "eth",
    "eth": "eth",
    "solana": "solana",
    "ton": "ton",
    "arbitrum": "arbitrum",
    "optimism": "optimism",
    "bnbsmartchain": "bsc",
    "binancesmartchain": "bsc",
    "bsc": "bsc",
    "mantle": "mantle",
    "xlayer": "x-layer",
    "hyperevm": "hyperevm",
    "ink": "ink",
}
_YAHOO_SUFFIX = {"US": "", "HK": ".HK", "GB": ".L", "DE": ".DE", "ES": ".MC"}


def network_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def canon_network(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        return ""
    return _NETWORKS.get(network_key(text), text)


def gecko_slug(network: str) -> str | None:
    return _GECKO.get(network_key(network))


def gecko_pools_url(network: str, address: str) -> str | None:
    slug = gecko_slug(network)
    if not slug or not address:
        return None
    return (
        "https://api.geckoterminal.com/api/v2/networks/"
        f"{slug}/tokens/{quote(address, safe='')}/pools"
        "?include=base_token,quote_token"
    )


def norm_address(address: str) -> str:
    text = str(address or "").strip()
    if text.lower().startswith("0x"):
        return text.lower()
    return text


def _decimal_or_none(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _decimal_text(value) -> str | None:
    number = _decimal_or_none(value)
    if number is None:
        return None
    return format(number, "f")


def _sane_ratio(value) -> Decimal | None:
    """A share-per-token ratio the issuer API actually stated.

    On-chain ``multiplier()`` is a 1e18 fixed-point integer. A value that large
    in this JSON is not a share count, and treating it as one would invent a
    discount. Ratios outside (0, 1000) stay unknown.
    """
    number = _decimal_or_none(value)
    if number is None or number <= 0 or number >= 1000:
        return None
    return number


def shares_per_token(asset: dict, deployment: dict | None = None) -> Decimal | None:
    sources: list[dict] = []
    if deployment:
        sources.append(deployment)
    sources.append(asset)
    underlying = asset.get("underlying")
    if isinstance(underlying, dict):
        sources.append(underlying)
    for source in sources:
        for key in _SHARE_KEYS:
            if key in source and source.get(key) not in (None, ""):
                ratio = _sane_ratio(source.get(key))
                if ratio is not None:
                    return ratio
    return None


def extract_stables(deployment: dict) -> list:
    for key in _STABLE_KEYS:
        value = deployment.get(key)
        if isinstance(value, list):
            return value
    return []


def has_redemption(stables: list) -> bool:
    return any(isinstance(item, dict) and item.get("redemption") is True for item in stables)


def parse_assets(nodes: list) -> tuple[list[dict], list[dict]]:
    underlyings: list[dict] = []
    deployments: list[dict] = []
    for asset in nodes:
        if not isinstance(asset, dict):
            continue
        symbol = str(asset.get("symbol") or "").strip()
        if not symbol:
            continue
        underlying = asset.get("underlying") if isinstance(asset.get("underlying"), dict) else {}
        code = str(asset.get("underlyingSymbol") or underlying.get("symbol") or "").strip()
        country_raw = str(underlying.get("listingCountry") or asset.get("listingCountry") or "").strip()
        country = country_raw.upper() or None
        ratio = shares_per_token(asset, None)
        underlyings.append({
            "issuer_symbol": symbol,
            "underlying_symbol": code,
            "listing_country": country,
            "token_isin": str(asset.get("isin") or "").strip() or None,
            "underlying_isin": str(asset.get("underlyingIsin") or underlying.get("isin") or "").strip() or None,
            "name": str(asset.get("name") or symbol),
            "company_name": str(underlying.get("name") or asset.get("underlyingName") or asset.get("name") or symbol),
            "min_order_fiat_value": _decimal_text(asset.get("minOrderFiatValue")),
            "shares_per_token": format(ratio, "f") if ratio is not None else None,
        })
        for dep in asset.get("deployments") or []:
            if not isinstance(dep, dict):
                continue
            network = canon_network(str(dep.get("network") or ""))
            if not network:
                continue
            chain_raw = dep.get("chainId", dep.get("chain_id"))
            chain_id = None if chain_raw is None else str(chain_raw)
            # The list payload calls this supportsAtomicSwaps. The older name is kept
            # so a fixture or a future rename does not silently become "unknown".
            atomic = dep.get("supportsAtomicSwaps", dep.get("atomicSwaps"))
            if not isinstance(atomic, bool):
                atomic = None
            stables = extract_stables(dep)
            dep_ratio = shares_per_token(asset, dep)
            common = {
                "issuer_symbol": symbol,
                "network": network,
                "chain_id": chain_id,
                "atomic_swaps": atomic,
                "stables_json": json.dumps(stables, separators=(",", ":"), default=str),
                "shares_per_token": format(dep_ratio, "f") if dep_ratio is not None else None,
            }
            native = norm_address(str(dep.get("address") or ""))
            wrapper = norm_address(str(dep.get("wrapperAddressV2") or ""))
            if native:
                deployments.append({**common, "form": "native", "address": native})
            if wrapper:
                deployments.append({**common, "form": "wrapper_v2", "address": wrapper})
    return underlyings, deployments


def _bridge_items(payload: object) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise RuntimeError("bridges payload is not a list or object")
    for key in ("nodes", "data", "bridges"):
        if isinstance(payload.get(key), list):
            return payload[key]
    found = [
        value for value in payload.values()
        if isinstance(value, dict) and (
            "products" in value or "destinationNetworks" in value or "contract" in value
        )
    ]
    if found:
        return found
    raise RuntimeError("bridges payload has no bridge list")


def parse_bridges(payload: object) -> list[dict]:
    out = []
    for item in _bridge_items(payload):
        if not isinstance(item, dict):
            continue
        network = canon_network(str(item.get("network") or item.get("sourceNetwork") or ""))
        contract = norm_address(str(item.get("contract") or item.get("address") or ""))
        if not network or not contract:
            continue
        destinations = item.get("destinationNetworks")
        if not isinstance(destinations, list):
            destinations = item.get("destinations") if isinstance(item.get("destinations"), list) else []
        products = item.get("products") if isinstance(item.get("products"), list) else []
        chain_raw = item.get("chainId", item.get("chain_id"))
        out.append({
            "source_network": network,
            "contract": contract,
            "chain_id": None if chain_raw is None else str(chain_raw),
            "managed_by": None if item.get("managedBy") is None else str(item.get("managedBy")),
            "destinations_json": json.dumps(destinations, separators=(",", ":"), default=str),
            "products_json": json.dumps(products, separators=(",", ":"), default=str),
        })
    return out


def _products(products_json: str) -> list:
    try:
        products = json.loads(products_json or "[]")
    except json.JSONDecodeError:
        return []
    return products if isinstance(products, list) else []


def _product_addresses(item: dict, network: str) -> list[str]:
    """Contract addresses this product lists for one network.

    The live bridge payload nests them under ``deployments``. A flat
    ``address`` is the older shape used when the product is already about one
    chain. A symbol with no contract does not make every wrapper bridgeable.
    """
    found = []
    deployments = item.get("deployments")
    if isinstance(deployments, list) and deployments:
        for dep in deployments:
            if not isinstance(dep, dict) or not dep.get("address"):
                continue
            # "BinanceSmartChain" and "BNB Smart Chain" are the same network.
            if canon_network(str(dep.get("network") or "")) != canon_network(network):
                continue
            found.append(norm_address(str(dep["address"])))
        return found
    if item.get("address"):
        found.append(norm_address(str(item["address"])))
    return found


def chain_ids_in_bridges(bridges: list[dict]) -> dict[str, str]:
    """Chain ids the bridge list actually states, keyed by canonical network."""
    found: dict[str, str] = {}
    for bridge in bridges:
        source = bridge.get("source_network")
        if source and bridge.get("chain_id"):
            found.setdefault(source, str(bridge["chain_id"]))
        for item in _products(bridge.get("products_json") or "[]"):
            if not isinstance(item, dict):
                continue
            for dep in item.get("deployments") or []:
                if not isinstance(dep, dict) or dep.get("chainId") is None:
                    continue
                name = canon_network(str(dep.get("network") or ""))
                if name:
                    found.setdefault(name, str(dep["chainId"]))
    return found


def exit_path(network: str, address: str, issuer_symbol: str,
              stables_json: str, bridges: list[dict]) -> str:
    """How a holder could leave this deployment. Recording only, not a route."""
    try:
        stables = json.loads(stables_json or "[]")
    except json.JSONDecodeError:
        stables = []
    wanted = norm_address(address)
    saw_contract = False
    symbol_only = False
    for bridge in bridges:
        if network_key(bridge["source_network"]) != network_key(network):
            continue
        for item in _products(bridge.get("products_json") or "[]"):
            if isinstance(item, str):
                if item == issuer_symbol:
                    symbol_only = True
                continue
            if not isinstance(item, dict):
                continue
            addresses = _product_addresses(item, network)
            if addresses:
                saw_contract = True
                if wanted and wanted in addresses:
                    return "bridgeable"
            elif item.get("symbol") == issuer_symbol or item.get("issuerSymbol") == issuer_symbol:
                symbol_only = True
    if saw_contract:
        symbol_only = False
    if symbol_only:
        return "bridgeable"
    if has_redemption(stables if isinstance(stables, list) else []):
        return "redeem_only"
    return "unknown"


def parse_kraken_pairs(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        raise RuntimeError("kraken pairs payload is not an object")
    errors = payload.get("error") or []
    if errors:
        raise RuntimeError("kraken pairs: " + ";".join(str(item) for item in errors))
    if "result" not in payload:
        raise RuntimeError("kraken pairs payload missing result")
    result = payload.get("result") or {}
    if not isinstance(result, dict):
        raise RuntimeError("kraken pairs result is not an object")
    out: dict[str, dict] = {}
    for key, row in result.items():
        if not isinstance(row, dict):
            continue
        # A currency pair that leaked into this response is not an xStock exit.
        if row.get("aclass_base") not in (None, "tokenized_asset"):
            continue
        base = str(row.get("base") or "").strip()
        quote_ccy = str(row.get("quote") or "").strip()
        if not base or not quote_ccy:
            continue
        pair = str(row.get("altname") or key)
        # Kraken returns each xStock twice, once under an internal key and once
        # under the altname. The status matches. Keep one row per altname.
        if pair in out and key != pair:
            continue
        out[pair] = {
            "venue": "kraken",
            "pair": pair,
            "base": base,
            "quote": quote_ccy,
            "wsname": None if row.get("wsname") is None else str(row.get("wsname")),
            "status": None if row.get("status") is None else str(row.get("status")),
            "ordermin": _decimal_text(row.get("ordermin")),
            "lot_decimals": row.get("lot_decimals"),
            "pair_decimals": row.get("pair_decimals"),
        }
    return list(out.values())


def match_issuer(base: str, wsname: str | None, symbols: set[str]) -> str | None:
    candidates = [base]
    if wsname and "/" in wsname:
        candidates.append(wsname.split("/", 1)[0])
    folded = {symbol.casefold(): symbol for symbol in symbols}
    for candidate in candidates:
        if candidate in symbols:
            return candidate
        hit = folded.get(candidate.casefold())
        if hit:
            return hit
    return None


def deposit_networks_by_base(payload: object) -> dict[str, str]:
    """Public asset rows rarely list deposit chains. Missing stays ``unknown``."""
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    if not isinstance(result, dict):
        return {}
    out = {}
    for key, row in result.items():
        if not isinstance(row, dict):
            continue
        found = None
        for field in ("deposit_networks", "networks", "deposit_methods"):
            if row.get(field):
                found = json.dumps(row[field], separators=(",", ":"), default=str)
                break
        name = str(row.get("altname") or key)
        out[name.casefold()] = found or "unknown"
        out[str(key).casefold()] = found or "unknown"
    return out


def book_quality(depth_quality: str, status: str | None) -> str:
    """A post-only market can show a bid that a taker sell cannot hit.

    Kraken's pair ``status`` is ``online`` when the book is fully tradable.
    ``post_only`` still has a book, but it is not an exit for a market sell.
    An empty book stays empty: that fact is more specific than the mode.
    """
    if depth_quality == "ok" and status not in (None, "", "online"):
        return str(status)
    return depth_quality


def quote_treatment(quote_ccy: str) -> str:
    """How a Kraken quote converts into the dollar notional we record.

    ``usd`` is a dollar. ``assumed_par`` is a stablecoin we do not reprice in
    this process. Anything else is not a dollar exit and cannot screen.
    """
    quote = str(quote_ccy or "").upper()
    if quote in {"USD", "ZUSD"}:
        return "usd"
    if quote in {"USDG", "USDT", "USDC"}:
        return "assumed_par"
    return "not_usd"


def depth_url(pair: str) -> str:
    return (
        f"{KRAKEN_DEPTH}?pair={quote(pair, safe='')}"
        "&count=50&asset_class=tokenized_asset"
    )


def _level(row) -> tuple[Decimal, Decimal] | None:
    if not isinstance(row, (list, tuple)) or len(row) < 2:
        return None
    price = _decimal_or_none(row[0])
    size = _decimal_or_none(row[1])
    if price is None or size is None or price <= 0 or size < 0:
        return None
    return price, size


def parse_depth(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise RuntimeError("kraken depth payload is not an object")
    errors = payload.get("error") or []
    if errors:
        raise RuntimeError("kraken depth: " + ";".join(str(item) for item in errors))
    result = payload.get("result") or {}
    if not isinstance(result, dict) or not result:
        return {"bid": None, "bid_size": None, "ask": None, "notional": None, "quality": "empty_book"}
    book = next(iter(result.values()))
    if not isinstance(book, dict):
        return {"bid": None, "bid_size": None, "ask": None, "notional": None, "quality": "empty_book"}
    bids = [level for level in (_level(row) for row in book.get("bids") or []) if level]
    asks = [level for level in (_level(row) for row in book.get("asks") or []) if level]
    return summarize_book(bids, asks)


def summarize_book(bids: list[tuple[Decimal, Decimal]],
                   asks: list[tuple[Decimal, Decimal]]) -> dict:
    if not bids or bids[0][1] <= 0:
        return {"bid": None, "bid_size": Decimal(0) if bids else None,
                "ask": asks[0][0] if asks else None, "notional": Decimal(0),
                "quality": "empty_book"}
    bid, bid_size = bids[0]
    ask = asks[0][0] if asks else None
    mid = (bid + ask) / 2 if ask is not None and ask > 0 else bid
    floor = mid * (Decimal(1) - _BID_BAND)
    notional = sum((price * size for price, size in bids if price >= floor), Decimal(0))
    return {"bid": bid, "bid_size": bid_size, "ask": ask, "notional": notional, "quality": "ok"}


def yahoo_symbol(underlying: str, country: str | None) -> str | None:
    if not country or country not in _YAHOO_SUFFIX:
        return None
    code = str(underlying or "").strip()
    if not code:
        return None
    if country == "HK" and code.isdigit():
        code = code.zfill(4)
    return code + _YAHOO_SUFFIX[country]


def yahoo_chart_url(symbol: str) -> str:
    return (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + quote(symbol, safe="")
        + "?range=1mo&interval=1d"
    )


def fx_request(currency: str) -> tuple[str, str] | None:
    """Yahoo symbol and how to turn a local price into dollars.

    ``HKD=X`` is HKD per 1 USD, so a Hong Kong price is divided by it.
    ``EURUSD=X`` style symbols are dollars per 1 unit.
    """
    ccy = str(currency or "").upper()
    if ccy in {"", "USD"}:
        return None
    if ccy == "HKD":
        return "HKD=X", "divide"
    if ccy == "GBP":
        return "GBPUSD=X", "multiply"
    if ccy == "GBX":
        return "GBPUSD=X", "pence"
    return f"{ccy}USD=X", "multiply"


def _chart_result(payload: object) -> tuple[dict, list[tuple[int, Decimal]]]:
    if not isinstance(payload, dict):
        raise RuntimeError("yahoo payload is not an object")
    chart = payload.get("chart") or {}
    result = chart.get("result")
    if not isinstance(result, list) or not result or not isinstance(result[0], dict):
        error = chart.get("error")
        raise RuntimeError(f"yahoo chart has no result: {error}")
    row = result[0]
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    timestamps = row.get("timestamp") or []
    quotes = ((row.get("indicators") or {}).get("quote") or [{}])
    closes = quotes[0].get("close") if quotes and isinstance(quotes[0], dict) else []
    bars = []
    for ts, close in zip(timestamps, closes or []):
        price = _decimal_or_none(close)
        if price is None or price <= 0 or ts is None:
            continue
        bars.append((int(ts) * 1000, price))
    return meta, bars


def parse_fx_rate(payload: object) -> Decimal:
    meta, bars = _chart_result(payload)
    live = _decimal_or_none(meta.get("regularMarketPrice"))
    if live is not None and live > 0:
        return live
    if not bars:
        raise RuntimeError("fx chart has no positive close")
    return bars[-1][1]


def apply_fx(price: Decimal, mode: str, rate: Decimal) -> Decimal:
    if rate <= 0:
        raise RuntimeError("fx rate is not positive")
    if mode == "divide":
        return price / rate
    if mode == "pence":
        return price / Decimal(100) * rate
    if mode == "multiply":
        return price * rate
    raise RuntimeError(f"unknown fx mode: {mode}")


def parse_equity(payload: object, now_ms: int) -> dict:
    """Regular-session price, or the last daily close when that session is shut.

    Yahoo's ``currentTradingPeriod.regular`` is the session clock. If it is
    missing, the session is ``calendar_unknown``: the last close is still
    returned, and the caller must not screen on it.
    """
    meta, bars = _chart_result(payload)
    currency = str(meta.get("currency") or "").upper() or None
    regular = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    start = regular.get("start")
    end = regular.get("end")
    last = bars[-1][1] if bars else None
    if start is None or end is None or last is None:
        return {
            "session": "calendar_unknown",
            "kind": "previous_close",
            "local_price": last,
            "currency": currency,
        }
    start_ms = int(start) * 1000
    end_ms = int(end) * 1000
    if start_ms <= now_ms < end_ms:
        live = _decimal_or_none(meta.get("regularMarketPrice"))
        return {
            "session": "open",
            "kind": "live",
            "local_price": live if live is not None and live > 0 else last,
            "currency": currency,
        }
    # The daily ``close`` array is often a binary float of the same print Yahoo
    # publishes as regularMarketPrice. Keep the cleaner meta value when the two
    # are the same price, and keep the bar when they actually differ.
    live = _decimal_or_none(meta.get("regularMarketPrice"))
    return {
        "session": "closed",
        "kind": "previous_close",
        "local_price": _coalesce_close(live, last),
        "currency": currency,
    }


def _coalesce_close(meta_price: Decimal | None, bar_close: Decimal | None) -> Decimal | None:
    if bar_close is None:
        return meta_price
    if meta_price is None or bar_close == 0:
        return bar_close
    if abs(meta_price - bar_close) <= abs(bar_close) * Decimal("0.0000001"):
        return meta_price
    return bar_close


def _h24_volume(attributes: dict) -> Decimal | None:
    volume = attributes.get("volume_usd")
    if isinstance(volume, dict):
        volume = volume.get("h24")
    return _decimal_or_none(volume)


def _base_matches(base_id: str, address: str) -> bool:
    tail = str(base_id or "").split("_")[-1]
    if address.lower().startswith("0x"):
        return tail.lower() == address.lower()
    return tail == address


def parse_gecko_pools(payload: object, address: str) -> dict:
    """Deepest pool whose base token is this deployment.

    A pool with reserve 0 is not a price. Among pools that have a price, one
    with 24h volume is the reference; volume 0 is stale; a missing volume
    figure is not treated as fresh.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return {"price": None, "reserve_usd": None, "pool_id": None, "quality": "no_pool"}
    candidates = []
    for row in data:
        if not isinstance(row, dict):
            continue
        attributes = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
        relationships = row.get("relationships") if isinstance(row.get("relationships"), dict) else {}
        base = ((relationships.get("base_token") or {}).get("data") or {}).get("id") or ""
        if not _base_matches(str(base), address):
            continue
        reserve = _decimal_or_none(attributes.get("reserve_in_usd"))
        price = _decimal_or_none(attributes.get("base_token_price_usd"))
        if reserve is None or price is None or reserve <= 0 or price <= 0:
            continue
        candidates.append({
            "price": price,
            "reserve_usd": reserve,
            "pool_id": row.get("id"),
            "volume": _h24_volume(attributes),
        })
    if not candidates:
        return {"price": None, "reserve_usd": None, "pool_id": None, "quality": "no_pool"}
    live = [row for row in candidates if row["volume"] is not None and row["volume"] > 0]
    if live:
        chosen = max(live, key=lambda row: row["reserve_usd"])
        quality = "ok"
    else:
        zeros = [row for row in candidates if row["volume"] is not None and row["volume"] == 0]
        chosen = max(zeros or candidates, key=lambda row: row["reserve_usd"])
        quality = "stale" if zeros else "freshness_unknown"
    return {
        "price": chosen["price"],
        "reserve_usd": chosen["reserve_usd"],
        "pool_id": chosen["pool_id"],
        "quality": quality,
    }


def discount_bps(equity_usd: Decimal, token_usd: Decimal, shares: Decimal) -> Decimal:
    per_share = token_usd / shares
    return (equity_usd - per_share) / equity_usd * Decimal(10000)


def decide_screen(*, equity_usd: Decimal | None, equity_session: str | None,
                  token_usd: Decimal | None, shares: Decimal | None,
                  chain_quality: str, bid_size: Decimal | None,
                  quote_treatment_name: str) -> dict:
    """50 bps is a screen, not an executable edge.

    A missing share ratio stores the two prices and does not produce a bps
    figure. A closed session may still screen. ``calendar_unknown`` may not.
    """
    bps = None
    if (shares is not None and equity_usd is not None and equity_usd > 0
            and token_usd is not None and token_usd > 0):
        bps = discount_bps(equity_usd, token_usd, shares)
    quality = chain_quality
    if chain_quality == "ok" and shares is None and token_usd is not None:
        quality = "share_ratio_unknown"
    hit = 0
    if (quality == "ok" and bps is not None and bps >= SCREEN_BPS
            and equity_session in {"open", "closed"}
            and quote_treatment_name in {"usd", "assumed_par"}
            and bid_size is not None and bid_size > 0):
        hit = 1
    return {"discount_bps": bps, "screen_hit": hit, "quality": quality}
