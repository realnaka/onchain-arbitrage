"""Once-a-day X Layer depth census. No OnchainOS, no orders.

The continuous loop cannot afford an exact quote for every OKX stock perp.
This pass asks a cheaper question, directly on the chain: does this token have
a pool whose token balances clear the depth floor? Names that do not are
written down and left alone until the next census. Names that do are the only
ones worth a spot price on the arb screen.

Slot0 plus the pool's own balances is a depth measurement, not a fill quote.
"""
from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path

from arb_monitor.core.hk import decimal
from arb_monitor.core.markets import normalize_market
from arb_monitor.data.xlayer_onchain import XLayerOnchainAdapter, now_ms
from arb_monitor.data.xstocks_registry import (
    discovered_token_entry, listing_market, okx_hedge_ticker,
    probe_pools_cheap, render_discovered_toml,
)


def split_measurements(rows: list[dict], min_depth: Decimal) -> dict:
    """Confirmed depth, confirmed absence, and reads that did not finish.

    An RPC error is not "no depth". Dropping a name on a failed read would
    hide a pool that was simply unreachable this pass.
    """
    with_depth, no_depth, unchecked = [], [], []
    floor = Decimal(str(min_depth))
    for row in rows:
        depth = row.get("depth_usd")
        if row.get("error") == "pool_not_initialized" and depth is None:
            # The call returned. The pool exists as an address and holds no price.
            no_depth.append(row)
            continue
        if row.get("error") and depth is None:
            unchecked.append(row)
            continue
        amount = decimal(0) if depth is None else Decimal(str(depth))
        if amount >= floor and row.get("pool"):
            with_depth.append(row)
        else:
            no_depth.append(row)
    return {"with_depth": with_depth, "no_depth": no_depth, "unchecked": unchecked}


def watchlist_is_fresh(document: dict | None, now: int, max_age_ms: int) -> bool:
    if not document or document.get("scanned_at_ms") is None:
        return False
    age = now - int(document["scanned_at_ms"])
    return 0 <= age <= max(0, int(max_age_ms))


def tokens_for_pool_read(tokens: list[dict], excluded: set[str] | None,
                         always: set[str]) -> list[dict]:
    """Pool reads skip names the census confirmed have no depth.

    ``excluded is None`` means there is no fresh census, so nothing is removed.
    Names in ``always`` (the execution quote list) stay even if a census row
    says otherwise.
    """
    if excluded is None:
        return list(tokens)
    return [token for token in tokens
            if str(token.get("asset")) in always
            or str(token.get("asset")) not in excluded]


def census_targets(issuer_assets: list[dict], okx_perps: dict,
                   configured: list[dict]) -> list[dict]:
    """OKX-hedgeable issuer tokens, plus configured tokens OKX already lists.

    Hong Kong names are configured as XIAOMI/TENCENT while the issuer's code
    is 1810/0700, so the issuer join alone would miss them. The configured
    token is the record that already knows which pool we trade.
    """
    targets = []
    seen = set()
    # Configured pools win: the issuer join does not know that 1810 is XIAOMI.
    for token in configured:
        code = str(token.get("hk_code") or "").strip().upper()
        # A previous discovery pass stored IBM's token under asset "TER"
        # because the letters t-e-r occur inside "international". The issuer
        # code is the ticker when OKX lists it.
        name = code if code in okx_perps else str(token.get("asset") or "")
        if not name or name in seen or name not in okx_perps:
            continue
        seen.add(name)
        targets.append({
            "asset": name,
            "okx_ticker": name,
            "symbol": token.get("symbol") or name,
            "name": token.get("name") or name,
            "hk_code": token.get("hk_code") or name,
            "listing_country": token.get("listing_country") or token.get("market"),
            "market": token.get("market") or token.get("listing_country"),
            "native_address": None,
            "wrapper_address": token.get("address"),
            "known_pools": token.get("pools") or [],
        })
    for asset in issuer_assets:
        ticker = okx_hedge_ticker(asset, okx_perps)
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        targets.append({**asset, "asset": ticker, "okx_ticker": ticker,
                        "known_pools": None})
    targets.sort(key=lambda row: row["asset"])
    return targets


def measure_target(rpc, factory: str, target: dict) -> dict:
    """Best pool for one token, via factory getPool and balance reads.

    Configured pools are measured as written: those addresses are the wrapper
    tokens. Issuer tokens are searched only at the wrapper, fee 0.05%. The
    native token often has no pool, and a native pool is not the market the
    wrapper trades in, so it is not a substitute.
    """
    base_row = {
        "asset": target["asset"],
        "symbol": target.get("symbol"),
        "name": target.get("name"),
        "market": normalize_market(target.get("listing_country") or target.get("market")),
        "okx_instrument": f'{target["okx_ticker"]}-USDT-SWAP',
        "pool": None,
        "quote_symbol": None,
        "depth_usd": None,
        "price_usd": None,
        "pools_seen": 0,
        "error": None,
        "base_kind": None,
        "base_address": None,
    }
    candidates = []
    if target.get("known_pools"):
        for pool in target["known_pools"]:
            candidates.append(("configured", pool.get("base_address") or target.get("wrapper_address"), pool))
    else:
        bases = [("wrapper", target.get("wrapper_address"))]
        for kind, address in bases:
            if not address:
                continue
            try:
                found = probe_pools_cheap(rpc, factory, address)
            except Exception as exc:  # noqa: BLE001 - one name must not abort the census
                base_row["error"] = type(exc).__name__ + ":" + str(exc)[:140]
                return base_row
            for pool in found:
                pool = {**pool, "base_address": address, "base_decimals": 18,
                        "quote_decimals": 6}
                candidates.append((kind, address, pool))
    base_row["pools_seen"] = len(candidates)
    if not candidates:
        return base_row
    best = None
    for kind, address, pool in candidates:
        token = {
            "asset": target["asset"], "symbol": target.get("symbol"),
            "address": address, "decimals": 18, "trade_scan": False,
            "pools": [pool],
        }
        try:
            states = XLayerOnchainAdapter({}, [token], rpc).pool_states()
        except Exception as exc:  # noqa: BLE001
            base_row["error"] = type(exc).__name__ + ":" + str(exc)[:140]
            continue
        for state in states:
            if state.get("error"):
                base_row["error"] = str(state.get("error"))
                continue
            depth = state.get("depth_usd")
            if depth is None:
                continue
            if best is None or depth > best[0]:
                best = (depth, kind, address, pool, state)
    if best is None:
        return base_row
    depth, kind, address, pool, state = best
    base_row.update({
        "pool": pool["address"],
        "quote_symbol": pool.get("quote_symbol"),
        "depth_usd": str(depth),
        "price_usd": None if state.get("price_usd") is None else str(state["price_usd"]),
        "base_kind": kind,
        "base_address": address,
        "error": None,
        "_pool": pool,
        "_state": state,
    })
    return base_row


def public_row(row: dict) -> dict:
    """Drop the pool object the writer uses internally."""
    return {key: value for key, value in row.items() if not key.startswith("_")}


def run_census(rpc, factory: str, targets: list[dict], min_depth: Decimal,
               now: int | None = None) -> dict:
    measured = [measure_target(rpc, factory, target) for target in targets]
    groups = split_measurements(measured, min_depth)
    return {
        "scanned_at_ms": now if now is not None else now_ms(),
        "method": "xlayer_rpc_slot0_and_balances",
        "min_depth_usd": str(min_depth),
        "targets": len(targets),
        "with_depth": [public_row(row) for row in groups["with_depth"]],
        "no_depth": [public_row(row) for row in groups["no_depth"]],
        "unchecked": [public_row(row) for row in groups["unchecked"]],
        "_measured": measured,
    }


def new_discovered_entries(measured: list[dict], known_assets: set[str],
                           known_addresses: set[str], min_depth: Decimal,
                           now: int) -> list[dict]:
    """with_depth names that are not already configured."""
    entries = []
    for row in measured:
        if row.get("asset") in known_assets:
            continue
        depth = row.get("depth_usd")
        pool = row.get("_pool")
        if not pool or depth is None or Decimal(str(depth)) < min_depth:
            continue
        address = str(row.get("base_address") or "")
        if address.lower() in known_addresses:
            continue
        asset = {
            "symbol": row.get("symbol") or row["asset"],
            "name": row.get("name") or row["asset"],
            "hk_code": row["asset"],
            "listing_country": row.get("market"),
            "market": listing_market({"listing_country": row.get("market")}),
        }
        entries.append(discovered_token_entry(
            asset, row.get("base_kind") or "wrapper", address, pool,
            Decimal(str(depth)), row["asset"], now, trade_scan=False))
    return entries


def write_watchlist(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stored = {key: value for key, value in document.items() if not key.startswith("_")}
    path.write_text(json.dumps(stored, ensure_ascii=False, indent=2) + "\n")


def load_watchlist(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def merge_discovered(path: Path, existing: list[dict], additions: list[dict]) -> int:
    if not additions:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_discovered_toml([*existing, *additions]))
    return len(additions)


def run_depth_census_command(config: dict, proxy: str | None = None) -> int:
    """One issuer∩OKX pass. Writes the watchlist and any new deep pools."""
    from arb_monitor.data.xlayer_onchain import XLayerRPC
    from arb_monitor.data.xstocks_registry import fetch_issuer_assets, fetch_okx_stock_perps

    onchain = config["onchain"]
    proxy = proxy or "http://127.0.0.1:4780"
    min_depth = decimal(onchain["min_pool_depth_usd"])
    print(f"depth census: issuer list + OKX stock perps, proxy={proxy}", flush=True)
    issuer = fetch_issuer_assets(proxy)
    perps = fetch_okx_stock_perps(proxy)
    targets = census_targets(issuer, perps, config["tokens"])
    print(f"depth census: {len(targets)} OKX-hedgeable names, "
          f"min depth {min_depth} USD", flush=True)
    rpc = XLayerRPC(onchain["rpc_url"], proxy)
    measured = []
    for index, target in enumerate(targets, start=1):
        measured.append(measure_target(rpc, onchain["factory"], target))
        if index % 10 == 0 or index == len(targets):
            print(f"depth census: {index}/{len(targets)}", flush=True)
    groups = split_measurements(measured, min_depth)
    document = {
        "scanned_at_ms": now_ms(),
        "method": "xlayer_rpc_slot0_and_balances",
        "min_depth_usd": str(min_depth),
        "targets": len(targets),
        "with_depth": [public_row(row) for row in groups["with_depth"]],
        "no_depth": [public_row(row) for row in groups["no_depth"]],
        "unchecked": [public_row(row) for row in groups["unchecked"]],
    }
    out = Path(onchain.get("depth_watchlist") or "data/depth_watchlist.json")
    if not out.is_absolute():
        out = Path.cwd() / out
    write_watchlist(out, document)
    known_assets = {token["asset"] for token in config["tokens"]}
    known_addresses = {token["address"].lower() for token in config["tokens"]}
    additions = new_discovered_entries(
        measured, known_assets, known_addresses, min_depth, document["scanned_at_ms"])
    discovered_path = Path(onchain.get("discovered_config") or "config/discovered_tokens.toml")
    if not discovered_path.is_absolute():
        discovered_path = Path.cwd() / discovered_path
    existing = [token for token in config["tokens"] if token.get("auto_discovered")]
    added = merge_discovered(discovered_path, existing, additions)
    print(f"with_depth={len(document['with_depth'])} "
          f"no_depth={len(document['no_depth'])} "
          f"unchecked={len(document['unchecked'])} "
          f"new_discovered={added}")
    print(f"wrote {out}")
    return 0 if not document["unchecked"] else 1


def excluded_assets(document: dict | None, now: int, max_age_ms: int) -> set[str] | None:
    """Names a fresh census confirmed have no depth. None = do not filter."""
    if not watchlist_is_fresh(document, now, max_age_ms):
        return None
    return {str(row.get("asset")) for row in document.get("no_depth") or [] if row.get("asset")}
