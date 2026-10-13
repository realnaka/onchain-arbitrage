#!/usr/bin/env python
"""Read-only Kraken probe for xStocks. Never places an order.

    PYTHONPATH=src .venv/bin/python scripts/kraken_probe.py status
    PYTHONPATH=src .venv/bin/python scripts/kraken_probe.py validate MRNAx --price 191.00
    PYTHONPATH=src .venv/bin/python scripts/kraken_probe.py methods SPYx

Reads credentials from config/kraken.env. Goes through the local proxy by
default (127.0.0.1:4780) because this host has no direct egress.
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.venues.kraken_spot import (  # noqa: E402
    KrakenError, KrakenSpotClient, load_credentials)

WATCH = ["MRNAx", "SPYx", "QQQx", "NVDAx", "TSLAx", "AAPLx", "CRCLx", "GOOGLx",
         "INTCx", "SKHYx", "SPCXx", "MSTRx"]


def make_client(proxy: str | None) -> KrakenSpotClient:
    key, secret = load_credentials(ROOT / "config" / "kraken.env")
    return KrakenSpotClient(key, secret, proxy=proxy)


def cmd_status(client: KrakenSpotClient, args) -> int:
    balance = client.balance()
    print("== 余额（非零）==")
    for asset, amount in sorted(balance.items()):
        if amount != 0:
            print(f"  {asset:12s} {amount}")
    print("== 挂单 ==")
    orders = client.open_orders()
    print(f"  {len(orders)} 笔" if orders else "  无")
    print("== 费率（MRNAxUSD）==")
    volume = client.trade_volume("MRNAxUSD")
    taker = (volume.get("fees") or {}).get("MRNAxUSD", {}).get("fee")
    maker = (volume.get("fees_maker") or {}).get("MRNAxUSD", {}).get("fee")
    print(f"  taker {taker}% / maker {maker}%")
    print("== 充值方式与最小量 ==")
    for symbol in WATCH:
        try:
            methods = client.deposit_methods(symbol)
        except KrakenError as exc:
            print(f"  {symbol:8s} {exc}"); continue
        pairs = ", ".join(f"{m.get('method')}({Decimal(str(m.get('minimum', '0'))):.6f})"
                          for m in methods)
        print(f"  {symbol:8s} {pairs}")
    print("== 下单权限 validate（不产生订单）==")
    for symbol in WATCH:
        try:
            out = client.validate_order(symbol, args.volume, price=args.price,
                                        ordertype="limit")
            descr = (out.get("descr") or {}).get("order", "ok")
            print(f"  {symbol:8s} {descr}")
        except KrakenError as exc:
            print(f"  {symbol:8s} {exc}")
    return 0


def cmd_validate(client: KrakenSpotClient, args) -> int:
    out = client.validate_order(args.symbol, args.volume, price=args.price,
                                ordertype=args.ordertype, oflags=args.oflags)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def cmd_methods(client: KrakenSpotClient, args) -> int:
    for method in client.deposit_methods(args.symbol):
        print(json.dumps(method, ensure_ascii=False))
        if args.address:
            for row in client.deposit_addresses(args.symbol, method.get("method")):
                print("   ", json.dumps(row, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Kraken xStocks read-only probe")
    parser.add_argument("--proxy", default="http://127.0.0.1:4780")
    sub = parser.add_subparsers(dest="cmd", required=True)

    status = sub.add_parser("status")
    status.add_argument("--volume", default="0.08")
    status.add_argument("--price", default="191.00")
    status.set_defaults(func=cmd_status)

    validate = sub.add_parser("validate")
    validate.add_argument("symbol")
    validate.add_argument("--volume", default="0.08")
    validate.add_argument("--price", default="191.00")
    validate.add_argument("--ordertype", default="limit")
    validate.add_argument("--oflags", default=None)
    validate.set_defaults(func=cmd_validate)

    methods = sub.add_parser("methods")
    methods.add_argument("symbol")
    methods.add_argument("--address", action="store_true")
    methods.set_defaults(func=cmd_methods)

    args = parser.parse_args(argv)
    client = make_client(args.proxy or None)
    try:
        return args.func(client, args)
    except KrakenError as exc:
        print(f"Kraken 报错：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
