"""New xStock listings that our on-disk universe does not watch yet.

The v2 universe is not discovered at run time. It is assembled from artifacts a
human refreshes:

* ``config/kraken_xstocks_units.toml`` -- the Kraken side the scan reads;
* ``data/xstocks_book.sqlite`` -- the issuer catalog; the Solana universe and
  every deployment come from here.

Nothing regenerates those two on a timer (crontab is empty, the one LaunchAgent
is disabled). So a symbol that lists upstream stays invisible to the scan until
somebody runs the refresh. On 2026-10-09 the operator asked whether a new token
could be monitored "from the first moment" -- with the old shape the answer was
no, and this module is the missing piece.

Two read-only calls answer it:

* Kraken tokenized assets -- the CEX side, a pair must exist to sell there;
* Backed/xStocks issuer catalog (paged) -- the token side.

A symbol that is live upstream but absent from the on-disk universe is reported
as new. This module never refreshes anything: it reports, and the refresh (or a
human) turns the report into a widened universe. A source that cannot be read
is an error, never "no new symbols".
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from arb_monitor.xstocks_v2.feeds import census as census_feed
from arb_monitor.xstocks_v2.feeds.http import Http, HttpError
from arb_monitor.xstocks_v2.units import units_config_path

ROOT = Path(__file__).resolve().parents[3]
KRAKEN_PAIRS = "https://api.kraken.com/0/public/AssetPairs?aclass_base=tokenized_asset"
ISSUER_ASSETS = "https://api.backed.fi/api/v2/public/assets"
DEFAULT_UNITS = units_config_path(ROOT)
DEFAULT_BOOK = ROOT / "data" / "xstocks_book.sqlite"
DEFAULT_CENSUS = ROOT / "data" / "xstocks_chain_census.json"
DEFAULT_REPORT = ROOT / "data-v2" / "xstocks_v2_discovery.json"
#: The issuer catalog is ~12 pages of 100. The cap is a runaway guard, not a
#: limit we expect to hit.
MAX_PAGES = 60


def _get_json(http, url: str):
    status, body = http.request(url)
    if status != 200:
        raise HttpError(url, f"http_{status}")
    try:
        return json.loads(body.decode())
    except (AttributeError, ValueError) as exc:
        raise HttpError(url, f"bad_json:{exc}") from exc


def kraken_live_symbols(http) -> dict[str, dict]:
    """``{base_symbol: {"pair": wsname}}`` for every tokenized Kraken pair."""
    payload = _get_json(http, KRAKEN_PAIRS)
    errors = payload.get("error") or []
    if errors:
        raise HttpError(KRAKEN_PAIRS, "kraken:" + ",".join(str(e) for e in errors))
    out: dict[str, dict] = {}
    for name, row in (payload.get("result") or {}).items():
        if not isinstance(row, dict):
            continue
        base = str(row.get("base") or "").strip()
        if base:
            out[base] = {"pair": str(row.get("wsname") or name)}
    return out


def _absorb_issuer_node(node, out: dict[str, dict]) -> None:
    if not isinstance(node, dict):
        return
    symbol = str(node.get("symbol") or "").strip()
    if not symbol:
        return
    networks = sorted({
        str(dep.get("network"))
        for dep in (node.get("deployments") or [])
        if isinstance(dep, dict) and dep.get("network")
    })
    out[symbol] = {
        "isin": node.get("isin"),
        "underlying": node.get("underlyingSymbol"),
        "networks": networks,
    }


def issuer_live_symbols(http, *, max_pages: int = MAX_PAGES) -> dict[str, dict]:
    """``{issuer_symbol: {"isin", "underlying", "networks"}}``, all pages.

    The first call carries no ``page`` parameter (the API reports that as page
    0), then ``?page=N`` walks forward until the API says there is no next page.
    A page that repeats the previous page's symbol set ends the walk: an API
    that silently ignores an out-of-range page would otherwise loop forever.
    """
    out: dict[str, dict] = {}
    payload = _get_json(http, ISSUER_ASSETS)
    pages = 0
    previous: frozenset[str] | None = None
    page = 1
    while True:
        nodes = payload.get("nodes") or []
        for node in nodes:
            _absorb_issuer_node(node, out)
        pages += 1
        current = frozenset(
            str(n.get("symbol")) for n in nodes if isinstance(n, dict) and n.get("symbol"))
        if current and current == previous:
            break
        previous = current
        if not (payload.get("page") or {}).get("hasNextPage") or pages >= max_pages:
            break
        payload = _get_json(http, f"{ISSUER_ASSETS}?page={page}")
        page += 1
    return out


def known_kraken_symbols(units_path: Path | str = DEFAULT_UNITS) -> set[str]:
    """The Kraken side of the on-disk universe.

    One implementation, shared with the scan: ``census.kraken_symbols`` knows
    how to read a dotted symbol like ``BRK.Bx`` out of the TOML table. Reading
    the table here directly is how ``BRK.Bx`` was mistaken for ``BRK``.
    """
    return set(census_feed.kraken_symbols(units_path))


def known_issuer_symbols(book_path: Path | str = DEFAULT_BOOK) -> set[str]:
    try:
        conn = sqlite3.connect(f"file:{book_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return set()
    try:
        rows = conn.execute(
            "SELECT issuer_symbol FROM underlyings WHERE missing_since_ms IS NULL")
        return {str(row[0]) for row in rows if row and row[0]}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def detect(*, known_kraken: set[str], known_issuer: set[str],
           kraken_live: dict[str, dict], issuer_live: dict[str, dict]) -> dict:
    """Diff live listings against the on-disk universe."""
    new_on_kraken = sorted(set(kraken_live) - set(known_kraken))
    new_at_issuer = sorted(set(issuer_live) - set(known_issuer))
    rows = []
    for symbol in sorted(set(new_on_kraken) | set(new_at_issuer)):
        detail = issuer_live.get(symbol) or {}
        rows.append({
            "symbol": symbol,
            "on_kraken": symbol in kraken_live,
            "kraken_pair": (kraken_live.get(symbol) or {}).get("pair"),
            "underlying": detail.get("underlying"),
            "networks": detail.get("networks") or [],
        })
    return {
        "new_on_kraken": new_on_kraken,
        "new_at_issuer": new_at_issuer,
        "new_symbols": [row["symbol"] for row in rows],
        "new_rows": rows,
        "kraken_live_count": len(kraken_live),
        "issuer_live_count": len(issuer_live),
        "known_kraken_count": len(known_kraken),
        "known_issuer_count": len(known_issuer),
    }


def check(http, *, units_path: Path | str = DEFAULT_UNITS,
          book_path: Path | str = DEFAULT_BOOK, now_ms: int | None = None) -> dict:
    """One full look. A source that fails is recorded, not counted as empty."""
    import time

    report: dict = {
        "checked_at_ms": int(now_ms if now_ms is not None else time.time() * 1000),
        "units_path": str(units_path),
        "book_path": str(book_path),
        "sources": {},
        "errors": {},
    }
    known_kraken = known_kraken_symbols(units_path)
    known_issuer = known_issuer_symbols(book_path)
    kraken_live: dict[str, dict] = {}
    issuer_live: dict[str, dict] = {}
    try:
        kraken_live = kraken_live_symbols(http)
        report["sources"]["kraken"] = "ok"
    except Exception as exc:  # noqa: BLE001 -- a failed source must reach the report
        report["sources"]["kraken"] = "error"
        report["errors"]["kraken"] = f"{type(exc).__name__}: {exc}"
    try:
        issuer_live = issuer_live_symbols(http)
        report["sources"]["issuer"] = "ok"
    except Exception as exc:  # noqa: BLE001
        report["sources"]["issuer"] = "error"
        report["errors"]["issuer"] = f"{type(exc).__name__}: {exc}"
    report.update(detect(known_kraken=known_kraken, known_issuer=known_issuer,
                         kraken_live=kraken_live, issuer_live=issuer_live))
    report["kraken_symbols"] = sorted(kraken_live)
    report["ok"] = not report["errors"]
    return report


def census_pool_symbols(path: Path | str = DEFAULT_CENSUS) -> set[str]:
    """Names the chain census found at least one pool for.

    Deliberately not "names the census looked at": a name with no pool anywhere
    is still unpriceable, and the pending list is there to keep exactly that
    visible. ``symbols_in_scope`` says we looked; these four lists say we found
    something to quote.
    """
    try:
        payload = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return set()
    out: set[str] = set()
    for name in ("candidates", "traps", "rejected", "unresolved"):
        for row in payload.get(name) or []:
            symbol = str((row or {}).get("symbol") or "")
            if symbol:
                out.add(symbol)
    return out


def update_pending_rail(report: dict, previous: dict | None = None,
                        *, priced_symbols: set[str] | None = None) -> list[str]:
    """Symbols we have ingested but cannot price on any edge yet.

    A symbol leaves this list when it has *some* priceable route, and that is
    not the same as "a Kraken pair appeared": ``dex_to_dex`` and
    ``chain_to_chain`` need no Kraken leg at all (any-to-any). So an entry is
    cleared once the name shows up on Kraken **or** the chain census has a pool
    for it. The list means "we know this token exists and cannot price it
    anywhere yet" -- it is not about a Kraken rail.
    """
    pending = set((previous or {}).get("pending_rail") or [])
    pending |= set(report.get("new_at_issuer") or [])
    pending -= set(report.get("kraken_symbols") or [])
    pending -= set(priced_symbols or ())
    report["pending_rail"] = sorted(pending)
    return report["pending_rail"]


def format_message(report: dict) -> str:
    lines = ["xStocks 新标的探测"]
    if report.get("errors"):
        for source, detail in sorted(report["errors"].items()):
            lines.append(f"读不到 {source}：{detail}")
    new = report.get("new_symbols") or []
    if not new:
        if report.get("ok"):
            lines.append("没有新标的（Kraken "
                         f"{report.get('kraken_live_count')} / 发行方 "
                         f"{report.get('issuer_live_count')} 个，全部已在册）")
    else:
        lines.append(f"发现 {len(new)} 个未在册的标的：")
        for row in report.get("new_rows") or []:
            where = "Kraken" if row.get("on_kraken") else "发行方"
            extra = f"，{row['underlying']}" if row.get("underlying") else ""
            nets = ("；链：" + ",".join(row["networks"])) if row.get("networks") else ""
            lines.append(f"- {row['symbol']}（{where}{extra}）{nets}")
        lines.append("下一步：跑目录刷新把新标的接进扫描（见 docs/xstocks-v2-todo.md）。")
    if report.get("pending_rail"):
        lines.append("已入库、但还没有任何可定价的路（不在 Kraken，链上也没有池）："
                     + "、".join(report["pending_rail"]))
    return "\n".join(lines)
