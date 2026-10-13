"""Catalog refresh and the slow Kraken-exit price sample.

Both talk only to the issuer, the public bridge list, Kraken, Yahoo and
GeckoTerminal. Other exchanges are not requested.
"""
from __future__ import annotations

import json
import time
from decimal import Decimal

from arb_monitor.xstocks_book import parse
from arb_monitor.xstocks_book.http import NotFound, RateLimit
from arb_monitor.xstocks_book.store import BookStore


def _pause(sleep, pause_s: float) -> None:
    if pause_s > 0:
        sleep(pause_s)


def fetch_paged_assets(client, pause_s: float, sleep) -> list:
    rows: list = []
    page = 0
    while True:
        payload = client.get_json(f"{parse.ASSETS_API}?page={page}")
        if not isinstance(payload, dict) or "nodes" not in payload or "page" not in payload:
            raise RuntimeError("issuer payload missing page/nodes; catalog left unchanged")
        nodes = payload.get("nodes")
        if not isinstance(nodes, list):
            raise RuntimeError("issuer nodes is not a list; catalog left unchanged")
        rows.extend(nodes)
        if not (payload.get("page") or {}).get("hasNextPage"):
            return rows
        page += 1
        if page > 100:
            raise RuntimeError("issuer paging exceeded 100 pages; catalog left unchanged")
        _pause(sleep, pause_s)


def _save_assets(store: BookStore, nodes: list, now_ms: int) -> dict:
    underlyings, deployments = parse.parse_assets(nodes)
    for row in underlyings:
        store.upsert_underlying(row, now_ms)
    for row in deployments:
        store.upsert_deployment(row, now_ms)
    missing_under = store.mark_missing(
        "underlyings", ["issuer_symbol"],
        {(row["issuer_symbol"],) for row in underlyings}, now_ms)
    missing_dep = store.mark_missing(
        "deployments", ["issuer_symbol", "network", "form", "address"],
        {(row["issuer_symbol"], row["network"], row["form"], row["address"]) for row in deployments},
        now_ms)
    store.commit()
    return {
        "underlyings": len(underlyings),
        "deployments": len(deployments),
        "missing_underlyings": missing_under,
        "missing_deployments": missing_dep,
    }


def _save_bridges(store: BookStore, payload: object, now_ms: int) -> dict:
    bridges = parse.parse_bridges(payload)
    for row in bridges:
        store.upsert_bridge(row, now_ms)
    missing = store.mark_missing(
        "bridges", ["source_network", "contract"],
        {(row["source_network"], row["contract"]) for row in bridges}, now_ms)
    # Deployments do not carry a chain id. The bridge list does, for the
    # networks that have a public bridge. Copy that id across; leave the rest.
    for network, chain_id in parse.chain_ids_in_bridges(
            [dict(row) for row in store.live_bridges()]).items():
        store.conn.execute(
            """UPDATE deployments SET chain_id=?
               WHERE network=? AND chain_id IS NULL""",
            (chain_id, network),
        )
    store.commit()
    return {"bridges": len(bridges), "missing_bridges": missing}


def _save_kraken(store: BookStore, pairs_payload: object, assets_payload: object | None,
                 now_ms: int) -> dict:
    pairs = parse.parse_kraken_pairs(pairs_payload)
    deposits = {} if assets_payload is None else parse.deposit_networks_by_base(assets_payload)
    symbols = store.issuer_symbols()
    saved = []
    for row in pairs:
        issuer = parse.match_issuer(row["base"], row.get("wsname"), symbols)
        deposit = deposits.get(row["base"].casefold(), "unknown")
        saved.append({
            **row,
            "issuer_symbol": issuer,
            "product_form": "native_xstock",
            "deposit_networks": deposit,
        })
        store.upsert_exit(saved[-1], now_ms)
    missing = store.mark_missing(
        "spot_exits", ["venue", "pair"],
        {(row["venue"], row["pair"]) for row in saved}, now_ms)
    store.commit()
    venues = store.exit_venues()
    if any(venue != "kraken" for venue in venues):
        raise RuntimeError(f"spot_exits picked up a non-Kraken venue: {venues}")
    return {
        "kraken_pairs": len(saved),
        "kraken_matched": sum(1 for row in saved if row["issuer_symbol"]),
        "missing_exits": missing,
    }


def refresh(store: BookStore, client, *, now_ms: int | None = None,
            pause_s: float = 0, sleep=time.sleep) -> dict:
    """Daily catalog. A failed source does not mark that source's rows missing."""
    started = int(now_ms if now_ms is not None else time.time() * 1000)
    summary: dict = {"errors": []}
    try:
        nodes = fetch_paged_assets(client, pause_s, sleep)
        summary["assets"] = _save_assets(store, nodes, started)
    except Exception as exc:
        summary["errors"].append(f"assets:{exc}")
    _pause(sleep, pause_s)
    try:
        summary["bridges"] = _save_bridges(store, client.get_json(parse.BRIDGES_API), started)
    except Exception as exc:
        summary["errors"].append(f"bridges:{exc}")
    _pause(sleep, pause_s)
    try:
        pairs_payload = client.get_json(parse.KRAKEN_PAIRS)
        assets_payload = None
        try:
            assets_payload = client.get_json(parse.KRAKEN_ASSETS)
        except Exception as exc:
            summary["errors"].append(f"kraken_assets:{exc}")
        summary["kraken"] = _save_kraken(store, pairs_payload, assets_payload, started)
    except Exception as exc:
        summary["errors"].append(f"kraken:{exc}")
    fatal = {"assets", "bridges", "kraken"}
    ok = not any(item.split(":", 1)[0] in fatal for item in summary["errors"])
    store.record_run(
        started_at_ms=started, finished_at_ms=int(time.time() * 1000),
        kind="refresh", ok=ok, summary_json=json.dumps(summary, default=str))
    return summary


def _equity_for(client, underlying: dict, now_ms: int, fx_cache: dict) -> dict:
    symbol = parse.yahoo_symbol(underlying["underlying_symbol"], underlying["listing_country"])
    if symbol is None:
        return {"equity_usd": None, "session": "calendar_unknown", "kind": "previous_close",
                "local_price": None, "currency": None, "quality": "calendar_unknown",
                "yahoo_symbol": None}
    parsed = parse.parse_equity(client.get_json(parse.yahoo_chart_url(symbol)), now_ms)
    local = parsed["local_price"]
    currency = parsed["currency"]
    if local is None:
        return {**parsed, "equity_usd": None, "quality": "equity_missing", "yahoo_symbol": symbol}
    spec = parse.fx_request(currency or "")
    if spec is None:
        return {**parsed, "equity_usd": local, "quality": "ok", "yahoo_symbol": symbol}
    fx_symbol, mode = spec
    try:
        if fx_symbol not in fx_cache:
            fx_cache[fx_symbol] = parse.parse_fx_rate(client.get_json(parse.yahoo_chart_url(fx_symbol)))
        equity_usd = parse.apply_fx(local, mode, fx_cache[fx_symbol])
    except Exception as exc:
        return {**parsed, "equity_usd": None, "quality": "fx_unknown", "yahoo_symbol": symbol,
                "fx_error": str(exc)}
    return {**parsed, "equity_usd": equity_usd, "quality": "ok", "yahoo_symbol": symbol,
            "fx_symbol": fx_symbol, "fx_mode": mode}


def _book_for(client, exit_row) -> dict:
    treatment = parse.quote_treatment(exit_row["quote"])
    try:
        depth = parse.parse_depth(client.get_json(parse.depth_url(exit_row["pair"])))
    except RateLimit:
        raise
    except Exception as exc:
        return {"pair": exit_row["pair"], "quote": exit_row["quote"], "treatment": treatment,
                "bid": None, "bid_size": None, "ask": None, "notional": None,
                "quality": "source_error", "error": str(exc)}
    if treatment == "not_usd" and depth["quality"] == "ok":
        depth = {**depth, "quality": "quote_not_usd", "notional": None}
    depth = {**depth, "quality": parse.book_quality(depth["quality"], exit_row["status"])}
    return {"pair": exit_row["pair"], "quote": exit_row["quote"], "treatment": treatment, **depth}


def _choose_exit(books: list[dict]) -> dict | None:
    liquid = [
        book for book in books
        if book.get("bid_size") is not None and book["bid_size"] > 0
        and book.get("treatment") in {"usd", "assumed_par"}
        and book.get("quality") in {"ok", "assumed_par"}
    ]
    # quote_not_usd books are excluded above. assumed_par keeps quality "ok"
    # from summarize_book; the treatment flag carries the assumption.
    if not liquid:
        return None

    def rank(book: dict) -> tuple:
        prefer = 0 if book["treatment"] == "usd" else 1
        notional = book["notional"] if isinstance(book["notional"], Decimal) else Decimal(0)
        return (prefer, -notional)

    return min(liquid, key=rank)


def _shares(deployment, underlying) -> Decimal | None:
    raw = deployment["shares_per_token"] or underlying["shares_per_token"]
    if raw in (None, ""):
        return None
    return Decimal(str(raw))


def _chain_for(client, deployment) -> dict:
    url = parse.gecko_pools_url(deployment["network"], deployment["address"])
    if url is None:
        return {"price": None, "reserve_usd": None, "pool_id": None,
                "quality": "gecko_network_unknown"}
    try:
        return parse.parse_gecko_pools(client.get_json(url), deployment["address"])
    except NotFound:
        return {"price": None, "reserve_usd": None, "pool_id": None, "quality": "no_pool"}
    except RateLimit:
        raise
    except Exception as exc:
        return {"price": None, "reserve_usd": None, "pool_id": None,
                "quality": "source_error", "error": str(exc)}


def _obs(**kwargs) -> dict:
    kwargs.setdefault("screen_hit", 0)
    kwargs.setdefault("detail_json", "{}")
    return kwargs


def sample_once(store: BookStore, client, *, now_ms: int | None = None,
                pause_s: float = 0, limit: int | None = None,
                symbol: str | None = None, sleep=time.sleep) -> dict:
    """Price Kraken-listed names only. One serial pass, then the caller waits."""
    started = int(now_ms if now_ms is not None else time.time() * 1000)
    kraken_rows = [row for row in store.live_exits() if row["venue"] == "kraken"]
    exits = [row for row in kraken_rows if row["issuer_symbol"]]
    by_symbol: dict[str, list] = {}
    for row in exits:
        by_symbol.setdefault(row["issuer_symbol"], []).append(row)
    symbols = sorted(by_symbol)
    if symbol is not None:
        symbols = [symbol] if symbol in by_symbol else []
    elif limit is not None:
        symbols = symbols[:limit]
    under_by_symbol = {row["issuer_symbol"]: row for row in store.live_underlyings()}
    bridges = [dict(row) for row in store.live_bridges()]
    fx_cache: dict[str, Decimal] = {}
    screened = 0
    errors = 0
    for symbol in symbols:
        underlying = under_by_symbol.get(symbol)
        if underlying is None:
            continue
        try:
            equity = _equity_for(client, underlying, started, fx_cache)
        except Exception as exc:
            equity = {"equity_usd": None, "session": "calendar_unknown", "kind": "previous_close",
                      "local_price": None, "currency": None, "quality": "source_error",
                      "error": str(exc)}
            errors += 1
        _pause(sleep, pause_s)
        store.add_observation(_obs(
            observed_at_ms=started, kind="equity", issuer_symbol=symbol,
            price=equity.get("local_price"), equity_usd=equity.get("equity_usd"),
            equity_session=equity.get("session"), equity_price_kind=equity.get("kind"),
            quality=equity.get("quality") or "source_error", source="yahoo",
            detail_json=json.dumps({
                "yahoo_symbol": equity.get("yahoo_symbol"),
                "currency": equity.get("currency"),
                "fx_symbol": equity.get("fx_symbol"),
                "error": equity.get("error") or equity.get("fx_error"),
            }, default=str),
        ))
        books = []
        for exit_row in by_symbol[symbol]:
            try:
                book = _book_for(client, exit_row)
            except RateLimit as exc:
                book = {"pair": exit_row["pair"], "quote": exit_row["quote"],
                        "treatment": parse.quote_treatment(exit_row["quote"]),
                        "bid": None, "bid_size": None, "ask": None, "notional": None,
                        "quality": "source_error", "error": f"rate_limit:{exc}"}
            if book["quality"] == "source_error":
                errors += 1
            books.append(book)
            store.add_observation(_obs(
                observed_at_ms=started, kind="kraken_book", issuer_symbol=symbol,
                venue="kraken", pair=book["pair"], bid=book.get("bid"),
                bid_size=book.get("bid_size"), ask=book.get("ask"),
                bid_notional_within_50bps=book.get("notional"),
                quality=book["quality"], source="kraken",
                detail_json=json.dumps({
                    "quote": book["quote"], "treatment": book["treatment"],
                    "error": book.get("error"),
                }, default=str),
            ))
            _pause(sleep, pause_s)
        chosen = _choose_exit(books)
        for deployment in store.live_deployments(symbol):
            try:
                chain = _chain_for(client, deployment)
            except RateLimit as exc:
                chain = {"price": None, "quality": "source_error", "reserve_usd": None,
                         "pool_id": None, "error": f"rate_limit:{exc}"}
            if chain["quality"] == "source_error":
                errors += 1
            shares = _shares(deployment, underlying)
            decision = parse.decide_screen(
                equity_usd=equity.get("equity_usd"),
                equity_session=equity.get("session"),
                token_usd=chain.get("price"),
                shares=shares,
                chain_quality=chain["quality"],
                bid_size=None if chosen is None else chosen.get("bid_size"),
                quote_treatment_name="not_usd" if chosen is None else chosen["treatment"],
            )
            if equity.get("quality") == "fx_unknown" and decision["quality"] == "ok":
                decision = {**decision, "quality": "fx_unknown", "screen_hit": 0, "discount_bps": None}
            if equity.get("session") == "calendar_unknown" and decision["screen_hit"]:
                decision = {**decision, "screen_hit": 0}
            screened += decision["screen_hit"]
            path = parse.exit_path(
                deployment["network"], deployment["address"], symbol,
                deployment["stables_json"], bridges)
            store.add_observation(_obs(
                observed_at_ms=started, kind="chain_ref", issuer_symbol=symbol,
                network=deployment["network"], form=deployment["form"],
                address=deployment["address"], price=chain.get("price"),
                equity_usd=equity.get("equity_usd"), equity_session=equity.get("session"),
                equity_price_kind=equity.get("kind"),
                discount_bps=decision["discount_bps"], screen_hit=decision["screen_hit"],
                quality=decision["quality"], source="geckoterminal",
                pair=None if chosen is None else chosen["pair"],
                bid_size=None if chosen is None else chosen.get("bid_size"),
                bid_notional_within_50bps=None if chosen is None else chosen.get("notional"),
                detail_json=json.dumps({
                    "not_executable": True,
                    "exit_path": path,
                    "pool_id": chain.get("pool_id"),
                    "reserve_usd": None if chain.get("reserve_usd") is None else str(chain["reserve_usd"]),
                    "shares_per_token": None if shares is None else format(shares, "f"),
                    "kraken_pair": None if chosen is None else chosen["pair"],
                    "quote_treatment": None if chosen is None else chosen["treatment"],
                    "error": chain.get("error"),
                }, default=str),
            ))
            _pause(sleep, pause_s)
        store.commit()
    if limit is None and symbol is None:
        for exit_row in kraken_rows:
            if exit_row["issuer_symbol"]:
                continue
            try:
                book = _book_for(client, exit_row)
            except RateLimit as exc:
                book = {"pair": exit_row["pair"], "quote": exit_row["quote"],
                        "treatment": parse.quote_treatment(exit_row["quote"]),
                        "bid": None, "bid_size": None, "ask": None, "notional": None,
                        "quality": "source_error", "error": f"rate_limit:{exc}"}
            if book["quality"] == "source_error":
                errors += 1
            store.add_observation(_obs(
                observed_at_ms=started, kind="kraken_book", issuer_symbol=None,
                venue="kraken", pair=book["pair"], bid=book.get("bid"),
                bid_size=book.get("bid_size"), ask=book.get("ask"),
                bid_notional_within_50bps=book.get("notional"),
                quality=book["quality"], source="kraken",
                detail_json=json.dumps({
                    "quote": book["quote"], "treatment": book["treatment"],
                    "issuer_unmatched": True, "error": book.get("error"),
                }, default=str),
            ))
            _pause(sleep, pause_s)
        store.commit()
    summary = {"symbols": len(symbols), "screen_hits": screened, "errors": errors}
    store.record_run(
        started_at_ms=started, finished_at_ms=int(time.time() * 1000),
        kind="sample", ok=errors == 0, summary_json=json.dumps(summary))
    return summary
