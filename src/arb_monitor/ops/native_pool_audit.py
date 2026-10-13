"""Read-only audit: does the *native* xStock have a pool of its own?

Operator question, 2026-09-24: "你说原生没有深度，但是为什么这个有深度？"

The claim "the native token has no market, the wrapper does" was measured on
2026-09-22 on **seven** US names (AAPL/NVDA/TSLA/MSFT/AMZN/META/GOOGL).  It was
never a per-name fact.  Worse, the monitor's own token-universe discovery only
re-probes the wrapper of a listing it has already seen (``hk_monitor.py``:
``candidate_bases`` keeps ``native`` only while a listing is ``brand_new``), so
a native pool that appears *after* the first probe is invisible in every
downstream report — including ``config/discovered_tokens.toml`` and the depth
watchlist.

This module answers the question directly, per listing: sweep the configured
Uniswap-V3-style factory for pools of **both** the issuer's native ``address``
and its ``wrapperAddressV2``, read each pool's ``slot0``/``liquidity``/balances,
and report which side actually carries depth.

Read-only by construction: ``eth_call`` / ``eth_blockNumber`` only.  No wallet,
no key, no signing, no transaction, no approval, no on-chain state change.  The
caller decides whether the JSON report is written to disk.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
import time
from typing import Callable, Iterable

from arb_monitor.data.xlayer_onchain import XLayerOnchainAdapter
from arb_monitor.data.xstocks_registry import DEFAULT_FEES, DEFAULT_STABLES, discover_pools

NATIVE = "native"
WRAPPER = "wrapper"
SIDES = (NATIVE, WRAPPER)

# Which side carries depth at or above the threshold.  ``no_wrapper_recorded``
# is separate from ``neither``: the issuer has no wrapper address for this
# listing at all, which is a different fact from "the wrapper pool is empty".
BOTH = "both"
NATIVE_ONLY = "native_only"
WRAPPER_ONLY = "wrapper_only"
NEITHER = "neither"
NO_WRAPPER = "no_wrapper_recorded"
# "We could not read" is not "there is nothing there".  A dead RPC or a
# throttled factory sweep must never be reported as an empty pool.
READ_FAILED = "read_failed"

VERDICTS = (NATIVE_ONLY, WRAPPER_ONLY, BOTH, NEITHER, NO_WRAPPER, READ_FAILED)


def display_name(raw: object) -> str:
    """The issuer's name without its marketing suffix ("Moderna xStock")."""
    text = str(raw or "").strip()
    for suffix in (" xStock", " Wrapped", " xStock Wrapped"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text.strip()


def issuer_rows(assets: Iterable[dict]) -> list[dict]:
    """One row per issuer asset, carrying both X Layer addresses.

    ``assets`` is what ``xstocks_registry.fetch_issuer_assets`` returns, but any
    sequence of dicts with ``name``/``symbol``/``native_address``/
    ``wrapper_address`` works, which is what keeps this testable offline.
    """
    rows = []
    for asset in assets:
        native = str(asset.get("native_address") or "").strip().lower() or None
        wrapper = str(asset.get("wrapper_address") or "").strip().lower() or None
        if not native and not wrapper:
            continue
        rows.append({
            "name": display_name(asset.get("name")) or str(asset.get("symbol") or ""),
            "symbol": str(asset.get("symbol") or ""),
            "underlying": str(asset.get("hk_code") or asset.get("underlying_symbol") or ""),
            "market": str(asset.get("market") or asset.get("listing_country") or "").upper(),
            "native": native,
            "wrapper": wrapper,
        })
    rows.sort(key=lambda row: (row["market"], row["underlying"], row["symbol"]))
    return rows


def probe_side(rpc, *, factory: str, base_address: str | None,
               stables: dict[str, str] | None = None, fees=DEFAULT_FEES) -> dict:
    """Every pool that exists for one address, plus the deepest one measured.

    An empty/uninitialized pool stays in ``pools`` with its error or depth so
    "there is no pool" and "the pool exists but holds nothing" never collapse
    into the same answer.
    """
    out: dict = {
        "address": str(base_address or "").strip().lower() or None,
        "pools_found": 0, "pools_with_depth": 0, "pools_not_in_range": 0,
        "best_depth_usd": None, "best_pool": None, "best_quote_symbol": None,
        "best_price_usd": None, "best_liquidity": None, "best_fee": None,
        "untradable_best_depth_usd": None, "pools": [], "errors": [],
    }
    if not base_address:
        return out
    pools = discover_pools(rpc, factory, base_address, stables=stables, fees=fees)
    out["pools_found"] = len(pools)
    if not pools:
        return out
    token = {
        "asset": base_address, "symbol": base_address, "address": base_address,
        "chain_id": "196", "decimals": 18, "trade_scan": False,
        "pools": [{**pool, "base_address": base_address, "base_decimals": 18,
                   "quote_decimals": 6} for pool in pools],
    }
    states = XLayerOnchainAdapter({"min_pool_depth_usd": "0"}, [token], rpc).pool_states()
    for state in states:
        depth = state.get("depth_usd")
        out["pools"].append({
            "pool": state.get("address"),
            "quote_symbol": state.get("quote_symbol"),
            "fee": state.get("fee"),
            "liquidity": state.get("liquidity"),
            "price_usd": _num(state.get("price_usd")),
            "base_amount": _num(state.get("base_amount")),
            "quote_amount": _num(state.get("quote_amount")),
            "depth_usd": _num(depth),
            "error": state.get("error"),
        })
        if state.get("error"):
            out["errors"].append({"pool": state.get("address"), "error": state["error"]})
            continue
        if depth is None:
            continue
    return summarize_pools(out)


def summarize_pools(probe: dict) -> dict:
    """Recompute the side's summary from its pool list.

    Same rule the monitor uses in ``best_pool_reference``: a pool whose in-range
    ``liquidity()`` is 0 holds balances but cannot be swapped against, so its
    "depth" is an artifact of out-of-range positions and must not be ranked as a
    market.  Measured 2026-09-24: CRWDx's native pool reported 264,596 USD of
    balances and reverted every QuoterV2 call, buy and sell.

    Computing this from the pool list (rather than trusting a stored
    ``best_depth_usd``) is what lets ``--from-json`` re-derive verdicts from an
    older report without new chain reads.
    """
    out = dict(probe)  # keeps "pools" (the evidence) alongside the summary
    pools = probe.get("pools") or []
    out.update({"pools_found": len(pools), "pools_with_depth": 0,
                "pools_not_in_range": 0, "best_depth_usd": None, "best_pool": None,
                "best_quote_symbol": None, "best_price_usd": None,
                "best_liquidity": None, "best_fee": None,
                "untradable_best_depth_usd": None})
    for pool in pools:
        if pool.get("error") or pool.get("depth_usd") is None:
            continue
        depth = Decimal(str(pool["depth_usd"]))
        in_range = int(pool.get("liquidity") or 0) > 0
        if not in_range:
            if depth > 0 and out["untradable_best_depth_usd"] is None:
                out["untradable_best_depth_usd"] = _num(depth)
            out["pools_not_in_range"] += 1
            continue
        if depth > 0:
            out["pools_with_depth"] += 1
        best = out["best_depth_usd"]
        if best is None or depth > Decimal(best):
            out["best_depth_usd"] = _num(depth)
            out["best_pool"] = pool.get("pool")
            out["best_quote_symbol"] = pool.get("quote_symbol")
            out["best_price_usd"] = pool.get("price_usd")
            out["best_liquidity"] = pool.get("liquidity")
            out["best_fee"] = pool.get("fee")
    return out


def _clears(depth: str | None, min_depth_usd: Decimal) -> bool:
    if depth is None:
        return False
    return Decimal(str(depth)) >= min_depth_usd


def _num(value) -> str | None:
    """Decimal as a string, with the zero-exponent artifacts collapsed."""
    if value is None:
        return None
    return "0" if Decimal(value) == 0 else str(value)


def verdict(*, native_depth: str | None, wrapper_depth: str | None,
            has_wrapper: bool, min_depth_usd: Decimal) -> str:
    """Which side carries depth at or above the threshold."""
    if not has_wrapper:
        return NO_WRAPPER
    native_deep = _clears(native_depth, min_depth_usd)
    wrapper_deep = _clears(wrapper_depth, min_depth_usd)
    if native_deep and wrapper_deep:
        return BOTH
    if native_deep:
        return NATIVE_ONLY
    if wrapper_deep:
        return WRAPPER_ONLY
    return NEITHER


def audit_row(universe_row: dict, probes: dict[str, dict],
              min_depth_usd: Decimal) -> dict:
    """One output row: both addresses, both measured depths, one verdict."""
    native = summarize_pools(probes.get(NATIVE) or {})
    wrapper = summarize_pools(probes.get(WRAPPER) or {})
    native_depth = native.get("best_depth_usd")
    wrapper_depth = wrapper.get("best_depth_usd")
    result = verdict(native_depth=native_depth, wrapper_depth=wrapper_depth,
                     has_wrapper=bool(universe_row.get("wrapper")),
                     min_depth_usd=min_depth_usd)
    addressed = [side for side in SIDES if universe_row.get(side)]
    if addressed and all(
            (probes.get(side) or {}).get("errors")
            and (probes.get(side) or {}).get("best_depth_usd") is None
            for side in addressed):
        result = READ_FAILED
    return {
        **universe_row,
        "native_pools_found": native.get("pools_found"),
        "native_pools_with_depth": native.get("pools_with_depth"),
        "native_pools_not_in_range": native.get("pools_not_in_range"),
        "native_untradable_best_depth_usd": native.get("untradable_best_depth_usd"),
        "native_best_depth_usd": native_depth,
        "native_best_price_usd": native.get("best_price_usd"),
        "native_best_pool": native.get("best_pool"),
        "native_best_quote_symbol": native.get("best_quote_symbol"),
        "native_errors": native.get("errors") or [],
        "wrapper_pools_found": wrapper.get("pools_found"),
        "wrapper_pools_with_depth": wrapper.get("pools_with_depth"),
        "wrapper_pools_not_in_range": wrapper.get("pools_not_in_range"),
        "wrapper_untradable_best_depth_usd": wrapper.get("untradable_best_depth_usd"),
        "wrapper_best_depth_usd": wrapper_depth,
        "wrapper_best_price_usd": wrapper.get("best_price_usd"),
        "wrapper_best_pool": wrapper.get("best_pool"),
        "wrapper_best_quote_symbol": wrapper.get("best_quote_symbol"),
        "wrapper_errors": wrapper.get("errors") or [],
        "verdict": result,
    }


def build_rows(universe: list[dict], probe_for: Callable[[dict, str], dict],
               min_depth_usd: Decimal,
               on_progress: Callable[[int, int, dict], None] | None = None,
               workers: int = 1, pause_s: float = 0.0) -> list[dict]:
    """Probe both sides for every row. ``probe_for`` is injected (network or fake).

    ``workers > 1`` runs the probes concurrently — the chain reads are pure I/O
    and a serial sweep of the whole issuer universe measures ~5 s per listing.
    ``pause_s`` is applied inside each probe so a concurrent run still paces
    itself against the shared proxy instead of bursting.
    """
    def failed(universe_row: dict, side: str, exc: Exception) -> dict:
        return {"address": universe_row.get(side), "pools_found": 0,
                "pools_with_depth": 0, "best_depth_usd": None, "pools": [],
                "errors": [{"pool": None, "error": f"{type(exc).__name__}:{exc}"}]}

    def work(index: int, side: str) -> tuple[int, str, dict]:
        row = universe[index]
        if pause_s > 0:
            time.sleep(pause_s)
        try:
            return index, side, probe_for(row, side)
        except Exception as exc:  # noqa: BLE001 - one bad name must not end the audit
            return index, side, failed(row, side, exc)

    probes_by_row: list[dict] = [{} for _ in universe]
    tasks = [(index, side) for index in range(len(universe)) for side in SIDES]
    if workers > 1 and tasks:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [pool.submit(work, index, side) for index, side in tasks]
            for future in as_completed(futures):
                index, side, result = future.result()
                probes_by_row[index][side] = result
    else:
        for index, side in tasks:
            _, _, result = work(index, side)
            probes_by_row[index][side] = result

    rows = []
    for index, universe_row in enumerate(universe, start=1):
        probes = probes_by_row[index - 1]
        row = audit_row(universe_row, probes, min_depth_usd)
        row["native_probe"] = probes.get(NATIVE, {})
        row["wrapper_probe"] = probes.get(WRAPPER, {})
        rows.append(row)
        if on_progress is not None:
            on_progress(index, len(universe), row)
    return rows


def summarize(rows: list[dict]) -> dict:
    counts = {name: 0 for name in VERDICTS}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    return {
        "assets": len(rows),
        "lists": {name: [r["symbol"] for r in rows if r["verdict"] == name]
                  for name in VERDICTS},
        "counts": counts,
    }


def _depth_text(row: dict, side: str) -> str:
    if not row.get(side):
        return "—"
    depth = row.get(f"{side}_best_depth_usd")
    if depth is None and row.get(f"{side}_errors"):
        return "读取失败"
    if depth is None:
        if row.get(f"{side}_untradable_best_depth_usd"):
            return "池不可成交"
        if row.get(f"{side}_pools_not_in_range"):
            return "空池"
        return "无池"
    return f"{Decimal(str(depth)):.2f}"


def _price_text(row: dict, side: str) -> str:
    price = row.get(f"{side}_best_price_usd")
    if not row.get(side) or price is None:
        return "—"
    return f"{Decimal(str(price)):.4f}"


CSV_COLUMNS = (
    "name", "symbol", "underlying", "market",
    "native", "native_price_usd", "native_depth_usd", "native_pool",
    "wrapper", "wrapper_price_usd", "wrapper_depth_usd", "wrapper_pool",
    "verdict",
)


def csv_rows(rows: list[dict]) -> list[dict]:
    """Flat rows for a spreadsheet: name, both addresses, both prices, verdict."""
    out = []
    for row in rows:
        out.append({
            "name": row.get("name"), "symbol": row.get("symbol"),
            "underlying": row.get("underlying"), "market": row.get("market"),
            "native": row.get("native") or "",
            "native_price_usd": row.get("native_best_price_usd") or "",
            "native_depth_usd": row.get("native_best_depth_usd") or "",
            "native_pool": row.get("native_best_pool") or "",
            "wrapper": row.get("wrapper") or "",
            "wrapper_price_usd": row.get("wrapper_best_price_usd") or "",
            "wrapper_depth_usd": row.get("wrapper_best_depth_usd") or "",
            "wrapper_pool": row.get("wrapper_best_pool") or "",
            "verdict": row.get("verdict"),
        })
    return out


def render_table(rows: list[dict], min_depth_usd: Decimal) -> str:
    """The human list: stock name, both contract addresses, prices, depths."""
    header = (f"{'股票':20s} {'代码':9s} {'市场':4s} "
              f"{'原生合约':44s} {'原生价':>10s} {'原生深度':>11s} "
              f"{'wrapper 合约':44s} {'wrapper价':>10s} {'wrapper深度':>12s}  结论")
    lines = [header, "-" * (len(header) + 8)]
    for row in rows:
        lines.append(
            f"{row['name'][:20]:20s} {row['underlying'][:9]:9s} {row['market'][:4]:4s} "
            f"{row.get('native') or '—':44s} {_price_text(row, 'native'):>10s} "
            f"{_depth_text(row, 'native'):>11s} "
            f"{row.get('wrapper') or '—':44s} {_price_text(row, 'wrapper'):>10s} "
            f"{_depth_text(row, 'wrapper'):>12s}  {row['verdict']}")
    lines.append(f"\n阈值：深度 ≥ {min_depth_usd} USD 才算「有深度」"
                 f"（config [onchain].min_pool_depth_usd）")
    return "\n".join(lines)
