"""One page, two tabs: the market board and the account/risk board together.

Operator's request, 2026-09-16: "把这两个看板合并到一起，我打开两个网页太麻烦了".

Both renderers already exist and are kept exactly as they are —
``hk_monitor.dashboard_html`` for the market side and
``account_dashboard.account_html`` for the account side — because they encode
different questions and different failure modes:

* **行情** reads only the monitor database: opportunities, costs, the
  monitored-vs-executable list. It never touches a private endpoint.
* **账户与风控** reads signed account state, wallet balances, the execution
  ledger, the limits in force and the gray-scale quota.

This module is a shell around them: one process, two finished documents, one
tab bar, and a 30-second reload that keeps the tab you were on. Nothing is
merged *into* the documents, so a failure on one side cannot blank the other —
if the account read fails, its tab says UNKNOWN and the market tab still works.

Paths:

``/``               the tabbed page (``#market`` / ``#account`` deep links)
``/api/status``     the market summary as JSON
``/api/account``    the account snapshot as JSON (``?refresh=1`` bypasses the cache)
``/favicon.ico``    204
"""
from __future__ import annotations

import json
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from arb_monitor.apps.account_dashboard import (AUTOMATION_POLICY, EXECUTION_LEDGER,
                                                MONITOR_CONFIG, account_html,
                                                holdings_banner_html,
                                                build_snapshot, okx_read_env)
from arb_monitor.apps.hk_monitor import age_summary, dashboard_html
from arb_monitor.core.hk import dumps
from arb_monitor.execution.okx_account import OKXReadOnlyClient
from arb_monitor.execution.wallet_cli import OnchainOSRunner
from arb_monitor.ledger.store import EventStore
from arb_monitor.ops.venue_scoreboard import (collect as collect_scoreboard,
                                              executable_from_policy,
                                              limits_from_policy,
                                              scoreboard_html)

DEFAULT_MONITOR_DB = "data/hk_monitor.db"
DEFAULT_AUTOMATION_POLICY = "config/automation_policy.toml"
# The per-venue tally re-reads every recorded cycle in its window (~0.5s for a
# day on the 3.9 GB monitor database), so it is cached. It is a *report*, not a
# live number: a minute of staleness costs nothing, and recomputing it per
# request would put a multi-hundred-millisecond scan on every page load.
SCOREBOARD_TTL_SECONDS = 120
_scoreboard_cache: dict = {"at_ms": 0, "html": None, "error": None}
_scoreboard_lock = threading.Lock()


def _snapshot_wallet_quote(snapshot_fn) -> Decimal | None:
    """The wallet's quote balance from an already-taken account snapshot.

    Returns ``None`` rather than raising: the snapshot may be unverified (no
    keys, no network), and an unknown wallet is a case the tally reports
    honestly as "ceiling only". A missing balance must never take the market
    tab down with it.
    """
    try:
        grey = (snapshot_fn() or {}).get("grey_scale") or {}
        value = grey.get("wallet_quote_balance")
        return None if value in (None, "") else Decimal(str(value))
    except Exception:  # noqa: BLE001 - see the docstring
        return None


def scoreboard_section(monitor_db: str = DEFAULT_MONITOR_DB, *,
                       hours: float = 24.0,
                       automation_path: str = DEFAULT_AUTOMATION_POLICY,
                       wallet_quote_usdt: Decimal | None = None,
                       now_ms: int | None = None) -> str:
    """The venue tally block, or a short note. Never raises.

    The market tab must keep rendering even if this scan fails — a reporting
    extra is not allowed to become a single point of failure for the board.

    ``wallet_quote_usdt`` is the balance that pays the spot leg. It is passed in
    rather than fetched: the tally is a read over already-recorded rows and must
    not put a chain call on a page load. The account tab already caches this
    number, so the server hands its own snapshot's value here.
    """
    import time
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    cache_key = (None if wallet_quote_usdt is None else str(wallet_quote_usdt))
    with _scoreboard_lock:
        cached = _scoreboard_cache
        if (cached["html"] is not None
                and cached.get("wallet") == cache_key
                and now_ms - cached["at_ms"] < SCOREBOARD_TTL_SECONDS * 1000):
            return cached["html"]
        try:
            # The affordability column is read against the ceiling actually in
            # force, so raising `per_trade_quote_usdt` changes the board rather
            # than leaving it quoting a limit the operator already replaced.
            limits = limits_from_policy(automation_path)
            per_trade = limits.get("per_trade_quote_usdt")
            board = collect_scoreboard(
                db_path=monitor_db, hours=hours,
                trade_quote_usdt=(per_trade if per_trade is None
                                  else Decimal(per_trade)),
                wallet_quote_usdt=wallet_quote_usdt,
                executable=executable_from_policy(automation_path))
            html = scoreboard_html(board)
        except Exception as exc:  # noqa: BLE001 - reporting must not break the page
            html = (f"<section style='margin:16px;color:#e2a03f'>场馆机会统计读取失败："
                    f"{type(exc).__name__}: {exc}</section>")
        # The wallet is part of the cache key: a balance that changes must
        # re-size the money columns instead of serving a two-minute-old size.
        cached.update(at_ms=now_ms, html=html, wallet=cache_key)
        return html


def tabbed_html(*, market_html: str, account_html_text: str,
                market_badge: str = "", account_badge: str = "",
                holdings_html: str = "") -> str:
    """Wrap the two finished documents in a tab shell.

    The documents are inlined rather than iframed: an iframe would need each
    side to keep its own server alive, which is the thing being removed. The
    tab is remembered in the URL hash, so a refresh — automatic or manual —
    stays where the operator was looking.
    """
    return f"""<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>套利监控 · 行情与账户</title>
<style>
 body{{margin:0;background:#0d141a;color:#e6edf3;font:14px system-ui}}
 .tabbar{{position:sticky;top:0;z-index:9;display:flex;gap:6px;align-items:center;
   padding:10px 16px;background:#111c24;border-bottom:1px solid #2b3b49}}
 .tabbar a{{padding:8px 16px;border-radius:8px;text-decoration:none;color:#acbcc9;
   background:#182632}}
 .tabbar a.active{{background:#31c48d;color:#06251a;font-weight:700}}
 .tabbar .meta{{margin-left:auto;color:#7d8f9c;font-size:12px}}
 .pane{{display:none}} .pane.active{{display:block}}
 iframe,object{{border:0}} .frame{{width:100%;height:calc(100vh - 52px)}}
</style>
<div class="tabbar">
  <a id="tab-market" href="#market">行情 / 候选与成本<span style="opacity:.8">
    {market_badge}</span></a>
  <a id="tab-account" href="#account">账户 / 风控与额度<span style="opacity:.8">
    {account_badge}</span></a>
  <span class="meta">只读 · 30 秒自动刷新 · 提示音需在行情页点一次"启用提示音"</span>
</div>
{holdings_html}
<section id="pane-market" class="pane">{market_html}</section>
<section id="pane-account" class="pane">{account_html_text}</section>
<script>
(function () {{
  function show() {{
    var want = location.hash === '#account' ? 'account' : 'market';
    ['market', 'account'].forEach(function (name) {{
      var pane = document.getElementById('pane-' + name);
      var tab = document.getElementById('tab-' + name);
      if (!pane || !tab) return;
      pane.classList.toggle('active', name === want);
      tab.classList.toggle('active', name === want);
    }});
    // The market page polls /api/status itself; only reload when the operator
    // is on the account side, whose numbers are not pushed to this shell.
    var sound = document.getElementById('soundBtn');
    if (want === 'market' && sound && !sound.dataset.bound) {{
      sound.dataset.bound = '1';
    }}
  }}
  window.addEventListener('hashchange', show);
  show();
  setInterval(function () {{
    if (location.hash === '#account') location.reload();
  }}, 30000);
}})();
</script>
</html>"""


def _error_class(error: dict) -> str:
    text = str(error.get("error") or "").lower()
    # Checked first: a suppression message quotes the failure it is backing off
    # from, so it would otherwise be filed as another 429. "We did not ask" is
    # not the same fact as "we asked and were refused".
    if "public_http_suppressed" in text:
        return "退避中未请求"
    if "429" in text or "rate" in text:
        return "429/限流"
    if any(word in text for word in ("ssl", "timeout", "timed out", "deadline")):
        return "timeout/SSL/超时"
    if "in_flight_not_sampled" in text:
        return "后台未采样"
    if "404" in text or "410" in text:
        return "永久 HTTP 错误"
    return "其他错误"


def _market_diagnostics(store: EventStore, summary: dict, *, now: int) -> dict:
    """Build read-only operator diagnostics from recent summaries.

    These fields are deliberately computed by the board, not the monitor loop:
    a slow report or a broken page must never affect pricing or execution.
    """
    recent = store.recent_summaries(31)
    recv = [int(row["_recv_ts_ms"]) for row in recent if row.get("_recv_ts_ms")]
    intervals = [recv[i] - recv[i + 1] for i in range(len(recv) - 1)]
    errors = list(summary.get("source_errors") or [])
    errors.extend(summary.get("onchain_errors") or [])
    grouped: dict[str, int] = {}
    for error in errors:
        key = _error_class(error)
        grouped[key] = grouped.get(key, 0) + 1
    max_age = int(summary.get("max_age_ms") or 15000)
    batch = summary.get("quote_batch_ms")
    coverage = str(summary.get("quote_probe_coverage") or "")
    try:
        got, total = (int(x) for x in coverage.split("/", 1))
    except (ValueError, TypeError):
        got = total = 0
    auto = summary.get("auto_execution") or {}
    halt = bool(auto.get("halt") or auto.get("halt_file")
                or str(auto.get("reason") or "").startswith("halted:"))
    blockers = []
    if halt:
        blockers.append("AUTO_HALT")
    if not auto.get("enabled"):
        blockers.append("自动执行未启用")
    if summary.get("quote_status") != "quotes_observed":
        blockers.append("报价未完整观测")
    if total and got < total:
        blockers.append(f"报价覆盖 {got}/{total}")
    if batch is not None and int(batch) > max_age:
        blockers.append(f"报价批次 {int(batch)/1000:.1f}s > {max_age/1000:.1f}s")
    if summary.get("onchain_status") == "unavailable":
        blockers.append("链上不可用")
    # Errors that describe optional reference enrichment are shown, but do not
    # claim to block the executable quote path. The strategy itself remains the
    # authority for whether a candidate is actionable.
    if not blockers:
        readiness = "本轮报价链路可评估"
    else:
        readiness = "本轮不具备完整自动执行条件"
    return {
        "as_of_ms": now,
        "recent_summary_count": len(recent),
        "last_sample_age_ms": max(0, now - int(summary.get("evaluated_at_ms", now))),
        "recent_interval_ms": intervals[0] if intervals else None,
        "max_interval_10_ms": max(intervals[:9], default=None),
        "max_interval_30_ms": max(intervals, default=None),
        "interval_count": len(intervals),
        "recent_30m_count": sum(
            1 for row in recent
            if now - int(row.get("_recv_ts_ms") or now) <= 30 * 60 * 1000),
        "recent_30m_degraded_count": sum(
            1 for row in recent
            if now - int(row.get("_recv_ts_ms") or now) <= 30 * 60 * 1000
            and (row.get("source_errors")
                 or row.get("onchain_status") in {"degraded", "unavailable"}
                 or (row.get("quote_batch_ms") is not None
                     and int(row["quote_batch_ms"]) > int(row.get("max_age_ms") or 15000)))),
        "recent_30m_pools_only_count": sum(
            1 for row in recent
            if now - int(row.get("_recv_ts_ms") or now) <= 30 * 60 * 1000
            and row.get("onchain_status") == "pools_only"),
        "quote_batch_ms": batch,
        "quote_budget_ms": max_age,
        "quote_over_budget_ms": (max(0, int(batch) - max_age)
                                  if batch is not None else None),
        "quote_coverage": coverage or "未知",
        "source_error_groups": grouped,
        "background_not_sampled": sum(1 for e in errors
                                       if _error_class(e) == "后台未采样"),
        # Per-host reference state, straight from the monitor's own adapter —
        # the board does not summarise it into a count, because "5 errors" is
        # exactly the number that made a backing-off host look like a broken
        # one. `state`, `last_reason` and `retry_in_ms` are what the operator
        # needs to decide whether to wait or to intervene.
        "reference_hosts": summary.get("reference_hosts") or {},
        "reference_worker": summary.get("reference_worker") or {},
        "reference_tiers": summary.get("reference_tiers") or {},
        "readiness": readiness,
        "blockers": blockers,
    }


def market_summary(monitor_db: str = DEFAULT_MONITOR_DB) -> dict:
    """The newest run summary, or an explicit empty one — never a stale page."""
    store = EventStore(monitor_db)
    try:
        summary = store.latest_summary()
        if summary:
            now = int(time.time() * 1000)
            summary = age_summary(summary, now)
            summary["board_diagnostics"] = _market_diagnostics(
                store, summary, now=now)
    finally:
        store.close()
    if not summary:
        return {"status": "no_data", "candidates": [],
                "note": f"monitoring database has no run summary yet: {monitor_db}"}
    return summary


def route(path: str, *, summary_fn, snapshot_fn,
          scoreboard_fn=None) -> tuple[int, str, str]:
    """``(status_code, mime, body)`` for one request path.

    Kept separate from the HTTP handler so the routing can be tested without a
    socket — the sandbox this project is developed in refuses to ``bind()``.
    ``scoreboard_fn`` is optional so existing callers and tests keep their
    behaviour, and the section it returns is appended to the market tab rather
    than merged into it.
    """
    target = path.split("?")[0]
    if target == "/favicon.ico":
        return 204, "image/x-icon", ""
    if target == "/api/status":
        return 200, "application/json", dumps(summary_fn())
    if target == "/api/account":
        return 200, "application/json", dumps(snapshot_fn(force="refresh=1" in path))
    if target != "/":
        return 404, "text/plain; charset=utf-8", "not found\n"
    summary = summary_fn()
    account = snapshot_fn(force="refresh=1" in path)
    auto = summary.get("auto_execution") or {}
    badge = "·".join(filter(None, [
        (f'{summary["candidate_count"]} 候选'
         if summary.get("candidate_count") is not None else None),
        auto.get("order_mode") or None]))
    market = dashboard_html(summary)
    if scoreboard_fn is not None:
        market += scoreboard_fn()
    return 200, "text/html", tabbed_html(
        market_html=market,
        account_html_text=account_html(account),
        market_badge=badge,
        account_badge=("已验证" if account.get("verified") else "未验证"),
        holdings_html=holdings_banner_html(account))


def serve_board(port: int = 8765, *, ttl_seconds: int = 15,
                monitor_db: str = DEFAULT_MONITOR_DB,
                monitor_path: str = MONITOR_CONFIG,
                automation_path: str = AUTOMATION_POLICY,
                ledger_path: str = EXECUTION_LEDGER,
                okx_env: str | None = "config/okx-demo.env") -> int:
    from arb_monitor.apps.account_dashboard import now_ms

    client = OKXReadOnlyClient.from_env(okx_read_env(okx_env))
    runner = OnchainOSRunner()
    cache: dict = {"at_ms": 0, "snapshot": None}

    def snapshot(force: bool = False) -> dict:
        if (not force and cache["snapshot"] is not None
                and now_ms() - cache["at_ms"] < ttl_seconds * 1000):
            return cache["snapshot"]
        # Only the account side is expensive (signed GETs + the wallet CLI); the
        # market side is one indexed row from the monitor database, so it is
        # read fresh every request.
        value = build_snapshot(client=client if client.configured else None,
                               runner=runner, monitor_path=monitor_path,
                               automation_path=automation_path,
                               ledger_path=ledger_path)
        cache.update(at_ms=now_ms(), snapshot=value)
        return value

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # keep the console readable
            pass

        def _send(self, body: str, mime: str) -> None:
            payload = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            status, mime, body = route(
                self.path,
                summary_fn=lambda: market_summary(monitor_db),
                snapshot_fn=snapshot,
                scoreboard_fn=lambda: scoreboard_section(
                    monitor_db, automation_path=automation_path,
                    # Read from the snapshot route() has already taken — it is
                    # cached, so this costs no second chain call, and it is the
                    # same number the account tab shows under 「灰度额度」.
                    wallet_quote_usdt=_snapshot_wallet_quote(snapshot)))
            if status != 200:
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send(body, mime)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Read-only combined board: http://127.0.0.1:{port}/", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
