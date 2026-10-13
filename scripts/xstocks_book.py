#!/usr/bin/env python3
"""xStocks catalog and Kraken-exit discount book.

This process is separate from the live basis monitor. It writes only
``data/xstocks_book.sqlite`` and does not read or restart hk-monitor.

Daily catalog, on its own cron, not inside fx-daily:

    PYTHONPATH=src .venv/bin/python scripts/xstocks_book.py refresh \\
        --proxy http://127.0.0.1:4780

Slow sample (15 minutes between round starts when the round finishes sooner):

    scripts/xstocks-book-ctl.sh start
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arb_monitor.xstocks_book.http import JsonClient  # noqa: E402
from arb_monitor.xstocks_book.jobs import refresh, sample_once  # noqa: E402
from arb_monitor.xstocks_book.report import render  # noqa: E402
from arb_monitor.xstocks_book.store import BookStore  # noqa: E402


def _client(args) -> JsonClient:
    proxy = None if args.no_proxy else args.proxy
    return JsonClient(proxy=proxy)


def main(argv: list[str] | None = None) -> int:
    # The flags sit on the top-level parser and on every subcommand, so the
    # documented ``refresh --proxy ...`` works as well as ``--proxy ... refresh``.
    # Until 2026-10-09 only the second form parsed, so the command in this
    # docstring exited 2 and the daily catalog refresh was easy to get wrong.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default="data/xstocks_book.sqlite")
    common.add_argument("--proxy", default="http://127.0.0.1:4780")
    common.add_argument("--no-proxy", action="store_true")
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    sub = parser.add_subparsers(dest="cmd", required=True)

    refresh_cmd = sub.add_parser("refresh", parents=[common],
                                 help="sync issuer deployments, bridges, and Kraken pairs")
    refresh_cmd.add_argument("--pause", type=float, default=0.25)

    sample_cmd = sub.add_parser("sample", parents=[common],
                                help="price Kraken-listed names against the stock")
    sample_cmd.add_argument("--interval", type=int, default=900)
    sample_cmd.add_argument("--once", action="store_true")
    sample_cmd.add_argument("--limit", type=int, default=None)
    sample_cmd.add_argument("--symbol", default=None)
    sample_cmd.add_argument("--pause", type=float, default=2.0,
                            help="seconds between requests; GeckoTerminal 429s if this is small")

    report_cmd = sub.add_parser("report", parents=[common],
                                help="print the catalog, Kraken bids, and screen hits")
    report_cmd.add_argument("--symbol", default=None)

    args = parser.parse_args(argv)
    store = BookStore(args.db)
    try:
        if args.cmd == "report":
            sys.stdout.write(render(store, args.symbol))
            return 0
        client = _client(args)
        if args.cmd == "refresh":
            summary = refresh(store, client, pause_s=args.pause)
            print(summary)
            failed = {item.split(":", 1)[0] for item in summary["errors"]}
            return 0 if not failed & {"assets", "bridges", "kraken"} else 1
        while True:
            started = time.monotonic()
            summary = sample_once(store, client, pause_s=args.pause, limit=args.limit,
                                  symbol=args.symbol)
            print(summary)
            if args.once:
                return 0 if summary["errors"] == 0 else 1
            # The interval is the gap between round starts when the round is
            # shorter than that. A round that overruns still leaves five seconds
            # before the next one, so a slow pass does not immediately restart.
            time.sleep(max(5, args.interval - (time.monotonic() - started)))
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
