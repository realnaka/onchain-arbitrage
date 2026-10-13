"""CCXT public transport + lossless, venue-specific stock contract metadata.

CCXT's unified market model does not expose Gate's stock classification or
Binance's HK_EQUITY/quanto distinction. Use its rate-limited public methods,
retain the full native response, and override JSON decoding before ANY floats.
No private methods, credentials or order endpoints are used.
"""
from __future__ import annotations

import json
import time
from decimal import Decimal

from arb_monitor.core.hk import Contract, decimal
from arb_monitor.core.models import Level, OrderBook


def now_ms() -> int:
    return time.time_ns() // 1_000_000


# These are search/classification candidates, NOT proof of matching exposure.
# A/H dual listings, ADRs and index products remain separate until verified.
HK_CANDIDATES = {
    "XIAOMI", "TENCENT", "MEITUAN", "POPMART", "MINIMAX", "ZHIPU",
    "BYD", "SMIC", "KUAISHOU", "LENOVO", "GIGADEV", "ZHONGJI",
    "AKESO", "ANTA", "GEELY", "LAOPU", "SUNAC", "TRAHK", "XUNCE",
    "CATL", "BEIGENE", "CITIC", "CMOC", "HENGRUI", "HHGRACE", "HSBC",
    "MIDEA", "SHENHUA", "WUXIAPPTEC", "YOFC", "ZIJINMINING",
    "CSOPSKHYNIX2L", "CSOPSAMSUNG2L",
}
SEPARATE_CANDIDATES = {"BABA", "JD", "BIDU", "SHEIN", "UNITREE", "MOONSHOT", "KIMI"}
# US names this registry keeps, and the reason it is a *list* rather than a
# rule ("anything that looks American").
#
# Operator, 2026-09-22: "现在开始对这114个公司也进行监控". The 114 is not a
# preference — it is the **intersection** of the two names that decide whether a
# US row can ever become a two-legged position:
#
#   {issuer US token `hk_code`}  ∩  {OKX stock perpetual `ctValCcy`}
#   = 754 ∩ 181 = 114            (measured 2026-09-22)
#
# Both halves are read from source, not typed by hand: the issuer list is
# `https://api.backed.fi/api/v2/public/assets`, the perp list is
# `https://www.okx.com/api/v5/public/instruments?instType=SWAP` filtered on
# `instCategory == "3"` (OKX's own tokenised-equity classification — without it
# the crypto contracts that share a ticker with a stock, `QNT`/`RAM`/`PURR`/
# `FLY`, would be read as equity). Reproduce with
# `docs/research/us-okx-overlap-2026-09-22.md` §7.
#
# This registry is only the **CEX half**: it decides which perp quotes are kept
# for these names. The spot half arrives through issuer discovery, and a US name
# is monitored only once its X Layer pool actually has depth. So this list being
# 114 does not mean 114 assets are priced — see the run summary's
# `token_universe_scan.report_only` for what discovery has and has not found.
#
# The first seven were reviewed for `mapping-verify` evidence on 2026-09-22
# (`docs/research/us-mapping-2026-09-22.md`); the other 107 are candidates whose
# `shares_per_token` has not been read from the issuer's `multiplier()` yet.
# `verified` / `valid_until_ms` stay empty — that signature is the operator's.
US_CANDIDATES = {
    "AAOI", "AAPL", "ADBE", "ALAB", "AMAT", "AMD", "AMZN", "APLD", "APP", "ARM",
    "ASML", "ASTS", "AVGO", "AXTI", "BE", "BMNR", "BOT", "BSP", "BX", "CBRS",
    "CIEN", "COHR", "COIN", "CRCL", "CRM", "CRWD", "CRWV", "CSCO", "DDOG", "DELL",
    "DKNG", "DRAM", "EWY", "FLNC", "GEV", "GLW", "GME", "GOOGL", "HIMS", "HOOD",
    "HPE", "HUT", "IBM", "INTC", "INTW", "IONQ", "IREN", "ISRG", "IWM", "JNJ",
    "KLAC", "KO", "KORU", "LITE", "LLY", "LRCX", "MARA", "META", "MRK", "MRNA",
    "MRVL", "MSFT", "MSTR", "MU", "MUU", "MVLL", "NBIS", "NET", "NFLX", "NOW",
    "NVDA", "OKLO", "OKTA", "ON", "ONDS", "ORCL", "PLTR", "PYPL", "QCOM", "QQQ",
    "RDDT", "RIOT", "RIVN", "RKLB", "ROK", "SHAZ", "SKHY", "SMCI", "SMH", "SNDK",
    "SNOW", "SNXX", "SOXL", "SOXS", "SPCX", "SPY", "STRC", "TEAM", "TER", "TQQQ",
    "TSLA", "TSM", "TTMI", "TTWO", "TWLO", "UNH", "USAR", "VRT", "WDC", "WEN",
    "WMT", "XLE", "XOM", "ZM",
}
HK_ALIASES = {"HK1810": "XIAOMI", "HK0700": "TENCENT", "HK0992": "LENOVO"}
CORE = {"XIAOMI", "TENCENT", "MEITUAN", "POPMART"}
# HK names that Gate/OKX list in USD terms, verified by price magnitude against
# the X Layer token (KUAISHOU perp ~4.01 vs token ~3.97; SHEIN ~5.07 vs ~5.05).
# Their HKD-number siblings are separate instruments and stay excluded.
USD_LINEAR_ADDITIONS = {"KUAISHOU", "SHEIN"}
SOURCES = {
    "gate": "https://api.gateio.ws/api/v4/futures/usdt/contracts",
    "okx": "https://www.okx.com/api/v5/public/instruments?instType=SWAP",
    "binance": "https://fapi.binance.com/fapi/v1/exchangeInfo",
    # Added 2026-09-16: Bybit and Bitget both list USD perps for the HK names we
    # track (Bybit: KUAISHOU/MEITUAN/POPMART/SHEIN/TENCENT/XIAOMI; Bitget: the
    # same six plus HKD-number siblings that stay excluded). Read through ccxt's
    # *unified* methods: their raw v1/v2 REST shapes have been decommissioned
    # (Bitget answered "The V1 API has been decommissioned" when probed), and the
    # unified market carries `contractSize`, which is the number that decides
    # whether a basis is real or a unit artefact.
    "bybit": "https://api.bybit.com/v5/market/instruments-info?category=linear",
    "bitget": "https://api.bitget.com/api/v2/mix/market/contracts",
    # Added 2026-09-17 on the operator's instruction ("Aster / Lighter / edgeX /
    # HL 要接成只读行情"). All four were probed live first; these three actually
    # list HK stock perps and ccxt carries a client for each:
    #   aster        Binance-compatible REST (fapi.asterdex.com) — POPMART,
    #                XIAOMI, TENCENT, MEITUAN, KUAISHOU
    #   hyperliquid  HIP-3 builder dex "xyz" (api.hyperliquid.xyz/info) — SHEIN
    #                only, of our six
    #   lighter      zk perp DEX (mainnet.zklighter.elliot.ai) — XIAOMI,
    #                TENCENT, POPMART, SHEIN
    # edgeX is deliberately absent: its public meta lists 293 coins and *none* of
    # them is one of our six (checked 2026-09-17), so there is nothing to read.
    "aster": "https://fapi.asterdex.com/fapi/v1/exchangeInfo",
    "hyperliquid": "https://api.hyperliquid.xyz/info (metaAndAssetCtxs, dex=xyz)",
    "lighter": "https://mainnet.zklighter.elliot.ai/api/v1/orderBooks",
}

# Venues whose USD-denominated HK names may be compared with the X Layer token.
# Gate/OKX were the reviewed pair; Bybit/Bitget joined on 2026-09-16 after their
# contract sizes (1 contract = 1 share) and price magnitudes were read from the
# live market: KUAISHOU perp 3.80/3.81 vs token ask 3.77, MEITUAN 9.50–9.52 vs
# 9.51. Binance is deliberately absent: its HK symbols mix HKD-number quote
# conventions, so they stay unverified until each one is checked by hand.
USD_LINEAR_VENUES = {"gate", "okx", "bybit", "bitget", "aster", "hyperliquid",
                     "lighter"}


def classify(base: str, is_hk: bool = False) -> tuple[str, str, str]:
    asset = HK_ALIASES.get(base, base.removesuffix("HKD"))
    if base.startswith("HK") or base.endswith("HKD"):
        return asset, "hk_quote_candidate", "hkd_quanto"
    if asset in CORE:
        return asset, "hk_stock_candidate", "usd_linear"
    if asset in US_CANDIDATES:
        return asset, "us_stock_candidate", "usd_linear"
    if asset in HK_CANDIDATES or is_hk:
        return asset, "hk_related_unmatched", "unknown"
    return asset, "separate_listing_or_unverified", "unknown"


def promote_usd_linear(venue: str, asset: str, category: str,
                       model: str) -> tuple[str, str]:
    """Gate/OKX USD-denominated HK names are comparable to the X Layer token.

    Only applied when the venue is Gate or OKX (Binance's HK tradifi symbols
    mix HKD-number quote conventions, so they stay unverified here), and only to
    the reviewed name list. Everything else keeps its unknown/hkd_quanto label.
    """
    if venue in USD_LINEAR_VENUES and asset in USD_LINEAR_ADDITIONS and model == "unknown":
        return "hk_stock_candidate", "usd_linear"
    return category, model


def _tick_from_places(places) -> Decimal:
    """Price tick from a venue's "decimal places" field (Bitget's pricePlace)."""
    try:
        return Decimal(10) ** -int(places)
    except (TypeError, ValueError):
        return Decimal("0.001")


def parse_contracts(venue: str, raw: object, received: int,
                    skipped: list[dict] | None = None) -> list[Contract]:
    """One venue's catalogue, as contracts we can size an order from.

    A row whose own specification cannot describe an order (a zero lot, a zero
    minimum, a zero tick) is **dropped and reported**, not fatal. It used to
    raise, which meant one broken row in one venue's catalogue deleted that
    entire venue from the registry: measured 2026-09-22, Gate lists
    ``SOXS_USDT`` with ``order_size_min = 0`` — and because the discovery loop
    keeps the union of the venues that parsed, Gate disappeared from
    ``perp_book`` for as long as that row existed (every other venue kept
    sampling, so nothing looked broken). One unusable row cannot be traded;
    it must not take 152 usable ones down with it. The rows that were skipped
    are handed to the caller in ``skipped``, which the monitor logs, so a
    wholesale field-name change still shows up as "all rows skipped".
    """
    if venue == "bybit":
        return _parse_bybit(raw, received)
    if venue == "bitget":
        return _parse_bitget(raw, received)
    if venue == "aster":
        return _parse_aster(raw, received)
    if venue == "hyperliquid":
        return _parse_hyperliquid(raw, received)
    if venue == "lighter":
        return _parse_lighter(raw, received)
    records = raw if venue == "gate" else raw["data" if venue == "okx" else "symbols"]
    result = []
    for r in records:
        if venue == "gate":
            instrument = r["name"]
            base = instrument.removesuffix("_USDT")
            candidate = base.removesuffix("HKD")
            if r.get("contract_type") not in ("stocks", "indices"):
                continue
            if candidate not in HK_CANDIDATES | SEPARATE_CANDIDATES | US_CANDIDATES and base not in {"HK50", "HSCHKD"}:
                continue
            multiplier = decimal(r["quanto_multiplier"])
            lot = decimal(r.get("order_size_min", "1"))
            minimum, min_notional = lot, Decimal("0")
            tick = decimal(r["order_price_round"])
            active = r.get("status") == "trading" and not r.get("in_delisting", False)
            asset, category, model = classify(base)
            if r.get("contract_type") == "indices":
                category, model = "index_not_single_stock", "unknown"
            if r.get("type") != "direct":
                model = "unknown"
            category, model = promote_usd_linear(venue, asset, category, model)
        elif venue == "okx":
            instrument = r["instId"]
            if not instrument.endswith("-USDT-SWAP"):
                continue
            # `instCategory == "3"` is OKX's own tokenised-equity class. The
            # name list alone is not enough once it is 114 names wide: OKX also
            # quotes crypto contracts whose ticker collides with a US stock
            # (`QNT`, `RAM`, `PURR`, `FLY` were the ones found on 2026-09-22),
            # and a collision would be read as a hedgeable equity leg. Measured
            # the same day: XIAOMI / POPMART / SHEIN are all `"3"`, so this
            # guard narrows the US names without touching the HK ones.
            if str(r.get("instCategory") or "") != "3":
                continue
            base = instrument.removesuffix("-USDT-SWAP")
            if base not in HK_CANDIDATES | SEPARATE_CANDIDATES | US_CANDIDATES:
                continue
            multiplier = decimal(r["ctVal"]) * decimal(r.get("ctMult") or "1")
            lot, minimum = decimal(r["lotSz"]), decimal(r["minSz"])
            min_notional, tick = Decimal("0"), decimal(r["tickSz"])
            active = r["state"] == "live"
            asset, category, model = classify(base)
            if r.get("ruleType") == "pre_market":
                category, model = "pre_market_unmatched", "unknown"
            if r.get("ctType") != "linear" or r.get("settleCcy") != "USDT":
                model = "unknown"
            category, model = promote_usd_linear(venue, asset, category, model)
        else:
            instrument = r["symbol"]
            is_hk = r.get("underlyingType") == "HK_EQUITY"
            base = r["baseAsset"]
            if not is_hk and base not in HK_CANDIDATES | SEPARATE_CANDIDATES | US_CANDIDATES:
                continue
            if r.get("quoteAsset") != "USDT" or r.get("marginAsset") != "USDT":
                continue
            filters = {f["filterType"]: f for f in r["filters"]}
            lot_filter = filters["LOT_SIZE"]
            multiplier = Decimal("1")  # native depth is base contract units
            lot, minimum = decimal(lot_filter["stepSize"]), decimal(lot_filter["minQty"])
            min_notional = decimal(filters.get("MIN_NOTIONAL", {}).get("notional", "0"))
            tick = decimal(filters["PRICE_FILTER"]["tickSize"])
            active = r["status"] == "TRADING"
            asset, category, model = classify(base, is_hk)
        if min(multiplier, lot, minimum, tick) <= 0:
            # A zero here is the venue's own data saying "no floor/no step".
            # Treating it as a number would be exactly the mistake the OKX
            # `min_notional = 0` investigation warned about: 0 is not "no
            # minimum", it is "unknown". Drop the row, keep the venue.
            dropped = {"venue": venue, "instrument": instrument, "asset": asset,
                       "reason": "nonpositive_spec",
                       "spec": {"multiplier": str(multiplier), "lot": str(lot),
                                "min_contracts": str(minimum), "tick": str(tick)}}
            if skipped is None:
                raise ValueError(
                    f"invalid contract specification: {venue}:{instrument}")
            skipped.append(dropped)
            continue
        result.append(Contract(
            venue, instrument, asset, category, model, multiplier, lot,
            minimum, min_notional, tick, active, SOURCES[venue], received, r,
        ))
    return result


def _parse_bybit(raw: object, received: int) -> list[Contract]:
    """Bybit linear instruments (``/v5/market/instruments-info?category=linear``).

    One contract is one base coin on Bybit's linear perps — the venue's own row
    carries no size field, only the order granularity — and the row *does* say
    which stock it is: ``symbolType: "stock"``, ``marketRegion: "HK"``,
    ``underlyingTicker: "01024"``, ``fullName: "Kuaishou Technology"``. Those are
    kept in ``raw`` as corroboration; the authority on "1 contract = 1 share"
    stays this project's own classified contract plus the verified token mapping.
    """
    result = []
    for r in (raw.get("result") or {}).get("list") or []:
        instrument = str(r.get("symbol") or "")
        base = str(r.get("baseCoin") or "").upper()
        if not instrument or not base:
            continue
        if str(r.get("contractType") or "") not in {"LinearPerpetual"}:
            continue
        if str(r.get("quoteCoin") or "").upper() != "USDT":
            continue
        asset, category, model = classify(base)
        if base.startswith("HK") or base.endswith("HKD"):
            continue
        if asset not in HK_CANDIDATES | SEPARATE_CANDIDATES | US_CANDIDATES:
            continue
        multiplier = Decimal("1")
        lot_filter = r.get("lotSizeFilter") or {}
        lot = decimal(lot_filter.get("qtyStep") or "1")
        minimum = decimal(lot_filter.get("minOrderQty") or lot)
        min_notional = decimal(lot_filter.get("minNotionalValue") or "0")
        tick = decimal((r.get("priceFilter") or {}).get("tickSize") or "0.001")
        active = str(r.get("status") or "") == "Trading"
        category, model = promote_usd_linear(venue="bybit", asset=asset,
                                            category=category, model=model)
        if min(multiplier, lot, minimum, tick) <= 0:
            continue
        result.append(Contract(
            "bybit", instrument, asset, category, model, multiplier, lot,
            minimum, min_notional, tick, active, SOURCES["bybit"], received, r,
        ))
    return result


def _parse_bitget(raw: object, received: int) -> list[Contract]:
    """Bitget USDT-FUTURES contracts (``/api/v2/mix/market/contracts``).

    ``sizeMultiplier`` is **the size of one contract in base coins**, and it is
    not 1: KUAISHOU is 0.01, BTCUSDT is 0.0001. Measured 2026-09-16: ccxt's
    unified market map reports ``contractSize = 1`` for the same Bitget symbol,
    which is wrong by 100x for this instrument — so the venue's own field wins
    and the unified value is not used anywhere in this module. Getting this wrong
    is precisely how a hedge ends up 100x the size of the spot leg.
    """
    result = []
    for r in raw.get("data") or []:
        instrument = str(r.get("symbol") or "")
        base = str(r.get("baseCoin") or "").upper()
        if not instrument or not base:
            continue
        if str(r.get("quoteCoin") or "").upper() != "USDT":
            continue
        asset, category, model = classify(base)
        if base.startswith("HK") or base.endswith("HKD"):
            continue
        if asset not in HK_CANDIDATES | SEPARATE_CANDIDATES | US_CANDIDATES:
            continue
        multiplier = decimal(r.get("sizeMultiplier") or "0")
        lot = decimal(r.get("minTradeNum") or "0")
        minimum = lot
        min_notional = decimal(r.get("minTradeUSDT") or "0")
        tick = _tick_from_places(r.get("pricePlace"))
        active = str(r.get("symbolStatus") or "") == "normal"
        category, model = promote_usd_linear(venue="bitget", asset=asset,
                                             category=category, model=model)
        if min(multiplier, lot, minimum, tick) <= 0:
            continue
        result.append(Contract(
            "bitget", instrument, asset, category, model, multiplier, lot,
            minimum, min_notional, tick, active, SOURCES["bitget"], received, r,
        ))
    return result


def _parse_aster(raw: object, received: int) -> list[Contract]:
    """Aster's perpetual catalogue (Binance-shaped REST).

    Aster is a perp DEX whose REST mirrors Binance's, so the interesting part is
    the same one that mattered for Binance: ``baseAsset`` is the *stock ticker*
    (XIAOMI, TENCENT, …) and the quantity unit is the share, i.e. one contract is
    one share — corroborated against Aster's own 24h volume/turnover, not
    assumed. Its HKD-code siblings, if it ever lists any, stay excluded by the
    same rule that excludes Binance's.
    """
    result = []
    for r in (raw.get("symbols") or []):
        instrument = str(r.get("symbol") or "")
        base = str(r.get("baseAsset") or "").upper()
        if not instrument or not base:
            continue
        if str(r.get("contractType") or "") not in {"PERPETUAL", "TRADIFI_PERPETUAL"}:
            continue
        if str(r.get("quoteAsset") or "").upper() != "USDT":
            continue
        asset, category, model = classify(base)
        if base.startswith("HK") or base.endswith("HKD"):
            continue
        if asset not in HK_CANDIDATES | SEPARATE_CANDIDATES | US_CANDIDATES:
            continue
        filters = {f["filterType"]: f for f in r.get("filters") or []}
        lot_filter = filters.get("LOT_SIZE") or {}
        lot = decimal(lot_filter.get("stepSize") or "0")
        minimum = decimal(lot_filter.get("minQty") or lot)
        min_notional = decimal((filters.get("MIN_NOTIONAL") or {}).get("notional") or "0")
        tick = decimal((filters.get("PRICE_FILTER") or {}).get("tickSize") or "0")
        active = str(r.get("status") or "").upper() == "TRADING"
        multiplier = Decimal("1")  # linear perp: quantity is in base shares
        category, model = promote_usd_linear("aster", asset, category, model)
        if min(multiplier, lot, minimum, tick) <= 0:
            continue
        result.append(Contract(
            "aster", instrument, asset, category, model, multiplier, lot,
            minimum, min_notional, tick, active, SOURCES["aster"], received, r,
        ))
    return result


def _parse_hyperliquid(raw: object, received: int) -> list[Contract]:
    """Hyperliquid's builder-deployed ``xyz`` perp dex.

    Stock perps live in their own dex, so the request is
    ``{"type":"metaAndAssetCtxs","dex":"xyz"}`` and the asset is named
    ``xyz:SHEIN``. Sizes are in coins (one contract is one coin), and the price
    tick follows the venue's own rule of five significant figures, derived here
    from the mark price rather than guessed.

    The venue's global minimum order value is *not* in this payload, so
    ``min_notional`` is recorded as 0 (unknown) instead of invented — a made-up
    floor would silently reject depth that really exists.
    """
    meta, ctxs = raw[0], raw[1]
    universe = meta.get("universe") or []
    result = []
    for index, a in enumerate(universe):
        name = str(a.get("name") or "")
        base = name.split(":")[-1].upper()
        if not name or not base:
            continue
        asset, category, model = classify(base)
        if asset not in HK_CANDIDATES | SEPARATE_CANDIDATES | US_CANDIDATES:
            continue
        ctx = ctxs[index] if index < len(ctxs) else {}
        if str(a.get("isDelisted") or "").lower() in {"true", "1"}:
            continue
        sz_decimals = int(a.get("szDecimals") or 0)
        step = Decimal(10) ** -sz_decimals if sz_decimals else Decimal("1")
        tick = _hl_tick(ctx.get("markPx"))
        category, model = promote_usd_linear("hyperliquid", asset, category, model)
        result.append(Contract(
            "hyperliquid", name, asset, category, "usd_linear", Decimal("1"),
            step, step, Decimal("0"), tick, True, SOURCES["hyperliquid"],
            received, {**a, "ctx": ctx},
        ))
    return result


def _hl_tick(mark) -> Decimal:
    """Hyperliquid quotes five significant figures; that *is* the price grid."""
    try:
        price = Decimal(str(mark))
    except Exception:  # noqa: BLE001 - a missing mark is not a crash
        return Decimal("0.0001")
    if price <= 0:
        return Decimal("0.0001")
    exponent = price.adjusted() - 4  # adjusted() = floor(log10(price))
    exponent = max(exponent, -6)     # the venue caps precision at 6 decimals
    return Decimal(1).scaleb(exponent)


def _parse_lighter(raw: object, received: int) -> list[Contract]:
    """Lighter's perp markets.

    ``multiplier`` is published per market ("1.000000000000000000" for XIAOMI),
    which is exactly the unit this project cares about, so it is read rather
    than assumed. ``min_base_amount`` / ``min_quote_amount`` are the venue's own
    order floors.
    """
    result = []
    for r in (raw.get("order_books") or []):
        if str(r.get("market_type") or "") != "perp":
            continue
        symbol = str(r.get("symbol") or "").upper()
        base = symbol.removesuffix("HKD")
        asset, category, model = classify(base)
        if base.startswith("HK") or base.endswith("HKD"):
            continue
        if asset not in HK_CANDIDATES | SEPARATE_CANDIDATES | US_CANDIDATES:
            continue
        multiplier = decimal(r.get("multiplier") or "0")
        size_decimals = int(r.get("supported_size_decimals") or 0)
        price_decimals = int(r.get("supported_price_decimals") or 0)
        lot = Decimal(10) ** -size_decimals if size_decimals else Decimal("1")
        tick = Decimal(10) ** -price_decimals if price_decimals else Decimal("0.0001")
        minimum = decimal(r.get("min_base_amount") or lot)
        min_notional = decimal(r.get("min_quote_amount") or "0")
        active = str(r.get("status") or "") == "active"
        category, model = promote_usd_linear("lighter", asset, category, model)
        if min(multiplier, lot, minimum, tick) <= 0:
            continue
        result.append(Contract(
            "lighter", str(r.get("market_id")), asset, category, "usd_linear",
            multiplier, lot, minimum, min_notional, tick, active,
            SOURCES["lighter"], received, r,
        ))
    return result


def parse_book(contract: Contract, raw: dict, received: int) -> OrderBook:
    venue = contract.venue
    data = raw["data"][0] if venue == "okx" else raw
    if venue == "gate":
        venue_ts = int(decimal(data["update"]) * 1000) if data.get("update") else None
        sequence = data.get("id")
        bids = [(x["p"], x["s"]) for x in data["bids"]]
        asks = [(x["p"], x["s"]) for x in data["asks"]]
    elif venue == "hyperliquid" or venue == "lighter":
        # Both were normalised to {"bids": [[px, sz], ...], "asks": [...]} by the
        # adapter; neither publishes a Binance-shaped envelope, and neither's
        # raw shape may leak into the shared branches below.
        venue_ts = data.get("ts")
        venue_ts = int(venue_ts) if venue_ts is not None else None
        sequence = data.get("lastUpdateId")
        bids, asks = data["bids"], data["asks"]
    elif venue in {"bybit", "bitget"}:
        # Normalised by the adapter: see CexPublicAdapter.book().
        venue_ts = data.get("ts")
        venue_ts = int(venue_ts) if venue_ts is not None else None
        sequence = data.get("lastUpdateId")
        bids, asks = data["bids"], data["asks"]
    else:
        venue_ts = data.get("ts") if venue == "okx" else data.get("T", data.get("E"))
        venue_ts = int(venue_ts) if venue_ts is not None else None
        sequence = data.get("seqId") if venue == "okx" else data.get("lastUpdateId")
        bids, asks = data["bids"], data["asks"]
    def levels(rows: list) -> tuple[Level, ...]:
        result = tuple(Level(decimal(x[0]), decimal(x[1])) for x in rows)
        if any(x.price <= 0 or x.quantity < 0 for x in result):
            raise ValueError("invalid depth")
        return tuple(x for x in result if x.quantity > 0)
    bid_levels, ask_levels = levels(bids), levels(asks)
    if any(a.price < b.price for a, b in zip(bid_levels, bid_levels[1:])):
        raise ValueError("unsorted bids")
    if any(a.price > b.price for a, b in zip(ask_levels, ask_levels[1:])):
        raise ValueError("unsorted asks")
    if bid_levels and ask_levels and bid_levels[0].price >= ask_levels[0].price:
        raise ValueError("crossed/locked book")
    return OrderBook(
        venue, contract.instrument_id, bid_levels, ask_levels,
        venue_ts, received, int(sequence) if sequence is not None else None,
        # A venue may serve a book from somewhere other than this REST call
        # (Lighter's WebSocket clock); the source travels with the payload so the
        # stored row says which feed produced it. Everything else keeps the REST
        # name it has always had.
        str(raw.get("_arb_source") or f"ccxt_public:{venue}:depth"),
        {"quantity_unit": "contracts", "contract_multiplier": str(contract.multiplier),
         "payout_model": contract.payout_model, "raw": raw},
    )


class CexPublicAdapter:
    def __init__(self, venue: str, proxy: str | None = None, stream=None):
        try:
            import ccxt
        except ImportError as exc:
            raise RuntimeError("Install the live extra: pip install -e '.[live]'") from exc
        self.venue = venue
        # Optional venue WebSocket that can vouch for a book with the venue's
        # own clock. Today only Lighter needs one (its REST book has no time
        # field at all — see `data/lighter_stream.py`); everything else keeps
        # reading the clock off its REST response.
        self.stream = stream
        # Catalogue rows this adapter could not turn into a contract, from the
        # most recent ``instruments()`` call. The monitor reads this right after
        # the call and reports it, so a skipped row is visible rather than
        # silent (see ``parse_contracts``).
        self.skipped_specs: list[dict] = []
        cls = getattr(ccxt, venue)
        options = {"enableRateLimit": True, "timeout": 15000}
        if proxy:
            options["httpsProxy"] = proxy
        self.exchange = cls(options)
        self.exchange.parse_json = lambda text: json.loads(text, parse_float=Decimal)
        self.version = ccxt.__version__

    def instruments(self) -> tuple[list[Contract], object, int]:
        if self.venue == "bybit":
            # Paginated on purpose: Bybit's default page is 500 rows and its
            # linear catalogue is already 873, so a single default call silently
            # drops symbols — measured 2026-09-16, the first unpaginated read
            # returned only KUAISHOU and MEITUAN out of our six assets while the
            # other four existed. `limit` is 1000 here, and the cursor loop keeps
            # working when the catalogue outgrows that.
            rows: list = []
            cursor = None
            for _ in range(10):
                params = {"category": "linear", "limit": 1000}
                if cursor:
                    params["cursor"] = cursor
                page = self.exchange.publicGetV5MarketInstrumentsInfo(params)
                result = page.get("result") or {}
                rows.extend(result.get("list") or [])
                cursor = result.get("nextPageCursor")
                if not cursor:
                    break
            raw = {"result": {"list": rows, "nextPageCursor": None}}
        elif self.venue == "bitget":
            # V2, not V1: the V1 mix endpoints answer
            # "The V1 API has been decommissioned" (probed 2026-09-16).
            raw = self.exchange.publicMixGetV2MixMarketContracts(
                {"productType": "USDT-FUTURES"})
        elif self.venue == "gate":
            raw = self.exchange.publicFuturesGetSettleContracts({"settle": "usdt"})
        elif self.venue == "okx":
            raw = self.exchange.publicGetPublicInstruments({"instType": "SWAP"})
        elif self.venue == "aster":
            raw = self.exchange.fapiPublicGetV1ExchangeInfo()
        elif self.venue == "hyperliquid":
            # Stock perps are not in Hyperliquid's main universe: they live in a
            # builder-deployed dex, so the request names it explicitly.
            raw = self.exchange.publicPostInfo({"type": "metaAndAssetCtxs",
                                                "dex": "xyz"})
        elif self.venue == "lighter":
            raw = self.exchange.publicGetOrderBooks()
        else:
            raw = self.exchange.fapiPublicGetExchangeInfo()
        received = now_ms()
        skipped: list[dict] = []
        contracts = parse_contracts(self.venue, raw, received, skipped=skipped)
        self.skipped_specs = skipped
        return contracts, raw, received

    def book(self, contract: Contract) -> OrderBook:
        if self.venue == "bybit":
            payload = self.exchange.publicGetV5MarketOrderbook(
                {"category": "linear", "symbol": contract.instrument_id, "limit": 20})
            data = (payload.get("result") or {})
            raw = {"bids": data.get("b") or [], "asks": data.get("a") or [],
                   "ts": data.get("ts"), "lastUpdateId": data.get("u")}
            return parse_book(contract, raw, now_ms())
        if self.venue == "bitget":
            # merge-depth is the V2 shape of the book; "scale0" is the venue's
            # finest available aggregation.
            payload = self.exchange.publicMixGetV2MixMarketMergeDepth(
                {"symbol": contract.instrument_id, "productType": "USDT-FUTURES",
                 "precision": "scale0", "limit": "20"})
            data = payload.get("data") or {}
            raw = {"bids": data.get("bids") or [], "asks": data.get("asks") or [],
                   "ts": data.get("ts"), "lastUpdateId": None}
            return parse_book(contract, raw, now_ms())
        if self.venue == "gate":
            raw = self.exchange.publicFuturesGetSettleOrderBook({
                "settle": "usdt", "contract": contract.instrument_id, "limit": 20, "with_id": True,
            })
        elif self.venue == "okx":
            raw = self.exchange.publicGetMarketBooks({"instId": contract.instrument_id, "sz": 20})
        elif self.venue == "aster":
            raw = self.exchange.fapiPublicGetV1Depth(
                {"symbol": contract.instrument_id, "limit": 20})
        elif self.venue == "hyperliquid":
            payload = self.exchange.publicPostInfo({"type": "l2Book",
                                                    "coin": contract.instrument_id})
            levels = payload.get("levels") or [[], []]
            # Hyperliquid returns [{px, sz, n}, ...] per side, not [price, size]
            # pairs; the shared level parser expects pairs, so convert here
            # rather than teaching every venue's shape to parse_book.
            def pairs(side):
                return [[row.get("px"), row.get("sz")] for row in (side or [])]
            raw = {"bids": pairs(levels[0] if len(levels) > 0 else []),
                   "asks": pairs(levels[1] if len(levels) > 1 else []),
                   "ts": payload.get("time"), "lastUpdateId": None}
        elif self.venue == "lighter":
            # Levels always come from the REST snapshot (full depth); the clock
            # comes from the venue's WebSocket when it has spoken for this
            # market. Read the clock *before* the read so a change landing
            # mid-request can never be claimed as part of this snapshot.
            raw = self._lighter_rest_book(contract, clock=self._lighter_clock(contract))
        else:
            raw = self.exchange.fapiPublicGetDepth({"symbol": contract.instrument_id, "limit": 20})
        # A payload may carry the moment *it* came off the socket; every other
        # venue is stamped with this cycle's time.
        return parse_book(contract, raw, int(raw.get("recv_ms") or now_ms()))

    def _lighter_clock(self, contract: Contract) -> dict | None:
        """The venue's own clock for this market, or None when the stream is mute.

        The stream's frames are *state changes* after the first snapshot (the
        venue documents this), so they are never used as levels — only as the
        venue's answer to "what time is it where you are".
        """
        stream = getattr(self, "stream", None)
        if stream is None:
            return None
        try:
            return stream.clock_for(contract.instrument_id)
        except Exception:  # noqa: BLE001 - a broken stream is a fallback, not an error
            return None

    def _lighter_rest_book(self, contract: Contract, clock: dict | None = None) -> dict:
        """REST book, stamped with the venue's clock when the stream has one."""
        payload = self.exchange.publicGetOrderBookOrders(
            {"market_id": contract.instrument_id, "limit": 20})
        rows = {"bids": payload.get("bids") or [], "asks": payload.get("asks") or []}
        raw = {
            "bids": [[x.get("price"), x.get("remaining_base_amount")] for x in rows["bids"]],
            "asks": [[x.get("price"), x.get("remaining_base_amount")] for x in rows["asks"]],
            "lastUpdateId": None,
        }
        if clock and clock.get("ts"):
            raw["ts"] = clock["ts"]
            raw["_arb_source"] = "ccxt_public:lighter:depth+ws_clock"
            raw["clock"] = clock
            return raw
        # No socket clock for this market: fall back to the freshest instrument
        # the REST side offers — the market's latest *trade*. That answers a
        # different question (an unchanged book with no trades reads as stale),
        # but it fails closed, and a dead socket must degrade to *less* data
        # rather than to an unclocked price.
        trade_ts = None
        try:
            trades = (self.exchange.publicGetRecentTrades(
                {"market_id": contract.instrument_id, "limit": 1}).get("trades") or [])
            if trades:
                trade_ts = trades[0].get("timestamp")
        except Exception:  # noqa: BLE001 - a missing anchor is not fatal
            trade_ts = None
        raw["ts"] = trade_ts
        raw["_arb_source"] = "ccxt_public:lighter:depth+recent_trades_anchor"
        return raw

    def funding(self, contract: Contract) -> dict:
        if self.venue == "bybit":
            payload = self.exchange.publicGetV5MarketTickers(
                {"category": "linear", "symbol": contract.instrument_id})
            row = ((payload.get("result") or {}).get("list") or [{}])[0]
            return {
                "rate": decimal(row.get("fundingRate")) if row.get("fundingRate") not in (None, "") else None,
                "next_funding_ts_ms": int(row["nextFundingTime"]) if row.get("nextFundingTime") else None,
                "venue_ts_ms": int(row["time"]) if row.get("time") else None,
                "recv_ts_ms": now_ms(),
                "source": "ccxt_public:bybit:funding",
                "mark_price": decimal(row.get("markPrice")) if row.get("markPrice") not in (None, "") else None,
                "note": "Indicative rate, not a guaranteed holding-period cashflow",
                "raw": row,
            }
        if self.venue == "bitget":
            payload = self.exchange.publicMixGetV2MixMarketCurrentFundRate(
                {"symbol": contract.instrument_id, "productType": "USDT-FUTURES"})
            row = (payload.get("data") or [{}])[0]
            ticker = self.exchange.publicMixGetV2MixMarketTicker(
                {"symbol": contract.instrument_id, "productType": "USDT-FUTURES"})
            trow = (ticker.get("data") or [{}])[0]
            return {
                "rate": decimal(row.get("fundingRate")) if row.get("fundingRate") not in (None, "") else None,
                "next_funding_ts_ms": int(row["nextUpdate"]) if row.get("nextUpdate") else None,
                "venue_ts_ms": int(trow["ts"]) if trow.get("ts") else None,
                "recv_ts_ms": now_ms(),
                "source": "ccxt_public:bitget:funding",
                "mark_price": decimal(trow.get("markPrice")) if trow.get("markPrice") not in (None, "") else None,
                "note": "Indicative rate, not a guaranteed holding-period cashflow",
                "raw": {"funding": row, "ticker": trow},
            }
        if self.venue == "gate":
            raw = self.exchange.publicFuturesGetSettleContractsContract({
                "settle": "usdt", "contract": contract.instrument_id,
            })
            rate, next_ts = raw.get("funding_rate"), int(raw["funding_next_apply"]) * 1000
            venue_ts = None  # API doesn't timestamp this rate observation
            mark = raw.get("mark_price")
        elif self.venue == "okx":
            raw = self.exchange.publicGetPublicFundingRate({"instId": contract.instrument_id})
            data = raw["data"][0]
            rate, next_ts = data.get("fundingRate"), int(data["fundingTime"])
            venue_ts, mark = int(data["ts"]), None
        elif self.venue == "aster":
            # Aster's futures API is the V1-prefixed family (its book read is
            # `fapiPublicGetV1Depth`), so the premium index has to carry the same
            # prefix. Before 2026-09-17 this fell through to the bare Binance
            # name below, which does not exist on Aster's client: every funding
            # read raised `AttributeError: 'aster' object has no attribute
            # 'fapiPublicGetPremiumIndex'` and was recorded as a source_error.
            # Measured 2026-09-17 15:47–16:47: 115 such errors in one hour, one
            # per 5 contracts per funding cadence — i.e. Aster's funding series
            # was never collected at all, and the noise buried the real 429s.
            raw = self.exchange.fapiPublicGetV1PremiumIndex(
                {"symbol": contract.instrument_id})
            rate, next_ts = raw.get("lastFundingRate"), int(raw["nextFundingTime"])
            venue_ts, mark = int(raw["time"]), raw.get("markPrice")
        elif self.venue == "binance":
            raw = self.exchange.fapiPublicGetPremiumIndex({"symbol": contract.instrument_id})
            rate, next_ts = raw.get("lastFundingRate"), int(raw["nextFundingTime"])
            venue_ts, mark = int(raw["time"]), raw.get("markPrice")
        elif self.venue in ("hyperliquid", "lighter"):
            # Deliberately not read yet, and deliberately not guessed. Both
            # venues settle funding **hourly**, so their native `funding` field
            # is not the 8-hourly rate this project's columns (`rate`,
            # `next_funding_ts_ms`) are shaped for; copying it across would
            # invent an 8x error in a cost. Until a per-venue probe confirms the
            # interval and the settlement clock, the honest answer is "no rate",
            # recorded as a result rather than raised as an exception — the
            # venue has no execution adapter, so this field is display-only.
            return {
                "rate": None, "next_funding_ts_ms": None, "venue_ts_ms": None,
                "recv_ts_ms": now_ms(),
                "source": f"ccxt_public:{self.venue}:funding_unwired",
                "mark_price": None,
                "note": ("funding not read: this venue settles hourly and the "
                         "hourly->8h mapping is unverified (待确认)"),
                "raw": None,
            }
        else:
            # An unknown venue is a wiring bug, not a market state: say so with
            # the venue's name instead of raising an AttributeError that reads
            # like a ccxt version problem.
            raise ValueError(f"funding_not_wired_for_venue:{self.venue}")
        return {
            "rate": decimal(rate) if rate not in (None, "") else None,
            "next_funding_ts_ms": next_ts, "venue_ts_ms": venue_ts,
            "recv_ts_ms": now_ms(), "source": f"ccxt_public:{self.venue}:funding",
            "mark_price": decimal(mark) if mark not in (None, "") else None,
            "note": "Indicative rate, not a guaranteed holding-period cashflow", "raw": raw,
        }
