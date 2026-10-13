#!/usr/bin/env python3
"""One-shot price gap scan for Kraken-listed xStocks. Writes JSON, no monitor DB."""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "xstocks_book.sqlite"
MULT = ROOT / "data" / "xstocks_multipliers.json"
OUT = ROOT / "data" / "xstocks_gap_2026-09-26.json"
GECKO_CACHE = ROOT / "data" / "xstocks_gecko_cache.json"
PROXY = "http://127.0.0.1:4780"

GECKO = {
    "ethereum": "eth",
    "solana": "solana",
    "ton": "ton",
    "arbitrum": "arbitrum",
    "optimism": "optimism",
    "bnbsmartchain": "bsc",
    "mantle": "mantle",
    "xlayer": "x-layer",
    "hyperevm": "hyperevm",
    "ink": "ink",
    "tron": "tron",
}
YAHOO = {"US": "", "HK": ".HK", "GB": ".L", "DE": ".DE", "ES": ".MC"}


def net_key(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def get(url: str, timeout: int = 25) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "xstocks-book/0.1", "Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    last: Exception | None = None
    for attempt in range(3):
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"unreachable {url}: {last}")


def gecko_batches(addresses: list[str], slug: str) -> dict[str, dict]:
    found: dict[str, dict] = {}
    i = 0
    chunk = 30
    while i < len(addresses):
        batch = addresses[i:i + chunk]
        joined = ",".join(urllib.parse.quote(addr, safe="") for addr in batch)
        url = f"https://api.geckoterminal.com/api/v2/networks/{slug}/tokens/multi/{joined}"
        try:
            status, body = get(url)
        except RuntimeError as exc:
            print(f"gecko skip {slug} {exc}", flush=True)
            i += chunk
            continue
        if status == 429:
            time.sleep(20)
            status, body = get(url)
        if status == 404:
            i += chunk
            time.sleep(0.4)
            continue
        if status != 200:
            print(f"gecko {slug} {status} {body[:120]!r}", flush=True)
            i += chunk
            time.sleep(1.2)
            continue
        payload = json.loads(body)
        for row in payload.get("data") or []:
            attr = row.get("attributes") or {}
            addr = str(attr.get("address") or "")
            price = attr.get("price_usd")
            vol = attr.get("volume_usd")
            if isinstance(vol, dict):
                vol = vol.get("h24")
            stored = addr.lower() if addr.lower().startswith("0x") else addr
            found[stored] = {
                "price_usd": price,
                "volume_h24": vol,
                "reserve": (attr.get("market_cap_usd") or attr.get("fdv_usd")),
                "name": attr.get("name"),
            }
        i += chunk
        time.sleep(1.1)
    return found


def okx_stock_prices() -> dict[str, dict]:
    status, body = get("https://www.okx.com/api/v5/public/instruments?instType=SWAP")
    instruments = json.loads(body).get("data") or []
    status, body = get("https://www.okx.com/api/v5/market/tickers?instType=SWAP")
    tickers = {row["instId"]: row for row in (json.loads(body).get("data") or [])}
    out = {}
    for inst in instruments:
        if str(inst.get("instCategory") or "") != "3":
            continue
        ticker = str(inst.get("ctValCcy") or "").upper()
        inst_id = inst.get("instId") or ""
        if not ticker or not str(inst_id).endswith("-USDT-SWAP"):
            continue
        quote = tickers.get(inst_id) or {}
        bid = quote.get("bidPx")
        ask = quote.get("askPx")
        last = quote.get("last")
        try:
            bid_d = Decimal(str(bid)) if bid else None
            ask_d = Decimal(str(ask)) if ask else None
            last_d = Decimal(str(last)) if last else None
        except Exception:
            continue
        if bid_d and ask_d and bid_d > 0 and ask_d > 0:
            ref = (bid_d + ask_d) / 2
            kind = "okx_mid"
        elif last_d and last_d > 0:
            ref = last_d
            kind = "okx_last"
        else:
            continue
        out[ticker] = {
            "inst_id": inst_id,
            "price": format(ref, "f"),
            "kind": kind,
            "bid": None if bid_d is None else format(bid_d, "f"),
            "ask": None if ask_d is None else format(ask_d, "f"),
            "last": None if last_d is None else format(last_d, "f"),
        }
    return out


def yahoo_symbol(code: str, country: str | None) -> str | None:
    if not country or country not in YAHOO:
        return None
    text = (code or "").strip()
    if not text:
        return None
    if country == "HK" and text.isdigit():
        text = text.zfill(4)
    return text + YAHOO[country]


def yahoo_ref(symbol: str) -> dict | None:
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(symbol, safe="")
        + "?range=5d&interval=1d"
    )
    status, body = get(url, timeout=20)
    if status != 200:
        return None
    payload = json.loads(body, parse_float=Decimal)
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, dict):
        return None
    meta = result.get("meta") or {}
    regular = meta.get("regularMarketPrice")
    post = meta.get("postMarketPrice")
    currency = str(meta.get("currency") or "")
    chosen = post if post not in (None, 0, "0") else regular
    kind = "yahoo_post" if post not in (None, 0, "0") else "yahoo_regular"
    if chosen in (None, 0, "0"):
        return None
    return {
        "price": format(Decimal(str(chosen)), "f"),
        "kind": kind,
        "currency": currency,
        "regular": None if regular in (None, "") else format(Decimal(str(regular)), "f"),
        "post": None if post in (None, "", 0, "0") else format(Decimal(str(post)), "f"),
        "symbol": symbol,
    }


def main() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    exits = list(conn.execute(
        "SELECT * FROM spot_exits WHERE missing_since_ms IS NULL AND issuer_symbol IS NOT NULL"))
    symbols = sorted({row["issuer_symbol"] for row in exits})
    under = {
        row["issuer_symbol"]: row
        for row in conn.execute("SELECT * FROM underlyings WHERE missing_since_ms IS NULL")
        if row["issuer_symbol"] in symbols
    }
    deps = list(conn.execute(
        """SELECT * FROM deployments
           WHERE missing_since_ms IS NULL AND issuer_symbol IN (%s)"""
        % ",".join("?" * len(symbols)),
        symbols,
    ))
    conn.close()
    mult = {}
    if MULT.exists():
        mult = json.loads(MULT.read_text())

    print(f"kraken {len(symbols)} deployments {len(deps)}", flush=True)
    by_slug: dict[str, list[str]] = {}
    for dep in deps:
        slug = GECKO.get(net_key(dep["network"]))
        if not slug:
            continue
        addr = dep["address"]
        by_slug.setdefault(slug, [])
        if addr not in by_slug[slug]:
            by_slug[slug].append(addr)

    prices: dict[tuple[str, str], dict] = {}
    cached_slugs: dict = {}
    if GECKO_CACHE.exists():
        cached_slugs = json.loads(GECKO_CACHE.read_text())
        for slug, rows in cached_slugs.items():
            for addr, row in rows.items():
                prices[(slug, addr)] = row
    for slug, addrs in by_slug.items():
        if slug in cached_slugs:
            print(f"gecko {slug} cached {len(cached_slugs[slug])}", flush=True)
            continue
        print(f"gecko {slug} {len(addrs)}", flush=True)
        found = gecko_batches(addrs, slug)
        cached_slugs[slug] = found
        GECKO_CACHE.write_text(json.dumps(cached_slugs))
        for addr, row in found.items():
            prices[(slug, addr)] = row
        print(f"  priced {len(found)}", flush=True)

    print("okx", flush=True)
    okx = okx_stock_prices()
    print(f"okx stock perps {len(okx)}", flush=True)

    refs = {}
    for symbol in symbols:
        info = under.get(symbol)
        if info is None:
            continue
        ticker = str(info["underlying_symbol"] or "").upper()
        if ticker in okx:
            refs[symbol] = {"source": "okx", **okx[ticker], "ticker": ticker}
            continue
    need_yahoo = [s for s in symbols if s not in refs]
    print(f"yahoo {len(need_yahoo)}", flush=True)
    fx_cache = {}
    for idx, symbol in enumerate(need_yahoo):
        info = under[symbol]
        ys = yahoo_symbol(str(info["underlying_symbol"] or ""), info["listing_country"])
        if ys is None:
            refs[symbol] = {"source": "none", "price": None, "kind": "no_yahoo_symbol"}
            continue
        row = yahoo_ref(ys)
        if row is None:
            refs[symbol] = {"source": "yahoo", "price": None, "kind": "yahoo_missing", "symbol": ys}
        else:
            ccy = row["currency"]
            price = Decimal(row["price"])
            if ccy and ccy not in {"USD", "USDT"}:
                if ccy not in fx_cache:
                    if ccy in {"HKD", "GBX", "GBP"}:
                        fx_sym = "HKD=X" if ccy == "HKD" else "GBPUSD=X"
                    else:
                        fx_sym = f"{ccy}USD=X"
                    fx_cache[ccy] = yahoo_ref(fx_sym)
                fx = fx_cache[ccy]
                if fx and fx.get("price"):
                    rate = Decimal(fx["price"])
                    if ccy == "HKD":
                        price = price / rate
                    elif ccy in {"GBX", "GBp"}:
                        price = price / Decimal(100) * rate
                    else:
                        price = price * rate
                    row["price_usd"] = format(price, "f")
                    row["fx"] = ccy
                else:
                    row["price_usd"] = None
                    row["fx_missing"] = ccy
            else:
                row["price_usd"] = row["price"]
            refs[symbol] = {"source": "yahoo", **row, "ticker": info["underlying_symbol"]}
        if (idx + 1) % 25 == 0:
            print(f"  yahoo {idx+1}/{len(need_yahoo)}", flush=True)
        time.sleep(0.15)

    rows_out = []
    for dep in deps:
        slug = GECKO.get(net_key(dep["network"]))
        addr = dep["address"]
        key = addr.lower() if addr.lower().startswith("0x") else addr
        quote = prices.get((slug, key)) or prices.get((slug, addr)) if slug else None
        info = under.get(dep["issuer_symbol"])
        ref = refs.get(dep["issuer_symbol"]) or {}
        mult_row = mult.get(dep["issuer_symbol"]) or {}
        token_price = None
        if quote and quote.get("price_usd") not in (None, "", "0"):
            try:
                token_price = Decimal(str(quote["price_usd"]))
            except Exception:
                token_price = None
        ref_price = None
        raw_ref = ref.get("price_usd") or ref.get("price")
        if ref.get("source") == "okx":
            raw_ref = ref.get("price")
        if raw_ref not in (None, ""):
            try:
                ref_price = Decimal(str(raw_ref))
            except Exception:
                ref_price = None
        gap = None
        if token_price and ref_price and ref_price > 0 and token_price > 0:
            gap = (token_price - ref_price) / ref_price * Decimal(10000)
        rows_out.append({
            "symbol": dep["issuer_symbol"],
            "ticker": None if info is None else info["underlying_symbol"],
            "country": None if info is None else info["listing_country"],
            "name": None if info is None else info["name"],
            "kraken_status": next(e["status"] for e in exits if e["issuer_symbol"] == dep["issuer_symbol"]),
            "network": dep["network"],
            "form": "wrapper" if dep["form"] == "wrapper_v2" else "原生",
            "address": dep["address"],
            "token_usd": None if token_price is None else format(token_price, "f"),
            "volume_h24": None if not quote else quote.get("volume_h24"),
            "ref_source": ref.get("source"),
            "ref_kind": ref.get("kind"),
            "ref_label": ref.get("inst_id") or ref.get("symbol") or ref.get("ticker"),
            "ref_usd": None if ref_price is None else format(ref_price, "f"),
            "gap_bps": None if gap is None else format(gap, "f"),
            "multiplier": mult_row.get("current"),
            "gecko": slug,
        })

    priced = [row for row in rows_out if row["gap_bps"] is not None]
    priced.sort(key=lambda row: abs(Decimal(row["gap_bps"])), reverse=True)
    summary = {
        "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "kraken_symbols": len(symbols),
        "deployments": len(deps),
        "with_token_price": sum(1 for row in rows_out if row["token_usd"]),
        "with_gap": len(priced),
        "okx_refs": sum(1 for row in refs.values() if row.get("source") == "okx"),
        "yahoo_refs": sum(1 for row in refs.values() if row.get("source") == "yahoo" and row.get("price_usd")),
        "rows": priced,
        "unpriced_sample": [row for row in rows_out if row["token_usd"] is None][:30],
    }
    OUT.write_text(json.dumps(summary, ensure_ascii=False))
    print(f"wrote {OUT} gaps {len(priced)}", flush=True)
    if priced:
        top = priced[0]
        print("top", top["symbol"], top["network"], top["form"], top["gap_bps"], top["ref_source"], flush=True)


if __name__ == "__main__":
    main()
