#!/usr/bin/env python3
"""Compare the monitor's cost model against fills that really happened.

Read-only. It answers four questions with evidence rather than opinion:

1. what the perpetual leg actually cost per fill, in bps, taker or maker;
2. what the exchange's own published taker rate is for that instrument;
3. what the on-chain leg really paid, versus the pool ask the monitor had
   stored at that moment;
4. how far the model's ``perp_fee_bps`` / ``slippage_bps`` / gas assumption is
   from the measured values.

Usage::

    PYTHONPATH=src .venv/bin/python scripts/cost_calibration.py \
        --env config/okx-demo.env --inst XIAOMI-USDT-SWAP --asset XIAOMI
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arb_monitor.apps.account_dashboard import load_toml, okx_read_env  # noqa: E402
from arb_monitor.execution.okx_account import (OKXReadError, OKXReadOnlyClient,  # noqa: E402
                                               fee_bps, taker_fee_bps)
from arb_monitor.execution.wallet_cli import (OnchainOSRunner, WalletCliError,  # noqa: E402
                                              wallet_history)

CST = timezone(timedelta(hours=8))
STABLE_SYMBOLS = {"USDC", "USDT", "USDT0", "USDG", "DAI", "USDE"}


def decimal(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def model_rows(db: str, asset: str, since_ms: int, until_ms: int) -> list[dict]:
    """Stored monitor snapshots for one asset: the model price at that moment."""
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT recv_ts_ms, payload_json FROM monitor_events "
        "WHERE kind='run_summary' AND recv_ts_ms BETWEEN ? AND ? ORDER BY recv_ts_ms",
        (since_ms, until_ms),
    ).fetchall()
    connection.close()
    found = []
    for row in rows:
        summary = json.loads(row["payload_json"])
        for reference in summary.get("token_references") or []:
            if reference.get("asset") != asset:
                continue
            direction = (reference.get("directions") or {}).get("long_spot_short_perp") or {}
            perp = direction.get("perp") or {}
            found.append({
                "ts_ms": row["recv_ts_ms"],
                "pool_mid_usd": decimal(reference.get("onchain_pool_price_usd")),
                "pool_ask_100_usd": decimal(reference.get("onchain_pool_ask_usd")),
                "pool_bid_100_usd": decimal(reference.get("onchain_pool_bid_usd")),
                "pool_depth_usd": decimal(reference.get("onchain_pool_depth_usd")),
                "pool_fee_bps": decimal(reference.get("onchain_pool_fee_bps")),
                "perp_venue": perp.get("venue"), "perp_instrument": perp.get("instrument"),
                "perp_bid_usd": decimal(direction.get("perp_price_usd")),
                "model_gross_bps": decimal(direction.get("gross_entry_basis_bps")),
                "model_cost_bps": decimal(direction.get("estimated_entry_cost_bps")),
                "model_net_bps": decimal(direction.get("estimated_net_entry_bps")),
                "model_cost_breakdown": direction.get("cost_breakdown") or {},
            })
            break
    return found


def nearest_before(rows: list[dict], ts_ms: int) -> dict | None:
    candidates = [row for row in rows if row["ts_ms"] <= ts_ms]
    return candidates[-1] if candidates else (rows[0] if rows else None)


def spot_legs(orders: list[dict], helper_tokens: set[str]) -> list[dict]:
    """Realised price of every swap leg the wallet did, buys and sells.

    A round trip is only measured if both directions are read: the buy pays the
    pool ask, the sell receives the pool bid, and the difference between those
    two is the cost the model has to predict.
    """
    legs = []
    for order in orders:
        changes = order.get("assetChange") or []
        token_in = next((c for c in changes if c.get("direction") == "IN"
                         and str(c.get("coinSymbol", "")).lower() in helper_tokens), None)
        stable_out = next((c for c in changes if c.get("direction") == "OUT"
                           and str(c.get("coinSymbol", "")).upper() in STABLE_SYMBOLS), None)
        token_out = next((c for c in changes if c.get("direction") == "OUT"
                          and str(c.get("coinSymbol", "")).lower() in helper_tokens), None)
        stable_in = next((c for c in changes if c.get("direction") == "IN"
                          and str(c.get("coinSymbol", "")).upper() in STABLE_SYMBOLS), None)
        if token_in and stable_out:
            side, token_change, stable_change = "buy", token_in, stable_out
        elif token_out and stable_in:
            side, token_change, stable_change = "sell", token_out, stable_in
        else:
            continue
        quantity = decimal(token_change.get("coinAmount"))
        quote = decimal(stable_change.get("coinAmount"))
        if not quantity or not quote or quantity <= 0 or quote <= 0:
            continue
        legs.append({
            "ts_ms": int(order.get("txTime") or order.get("txCreateTime") or 0),
            "tx_hash": order.get("txHash"), "status": order.get("txStatus"),
            "side": side, "symbol": token_change.get("coinSymbol"),
            "quantity": str(quantity), "quote_amount": str(quote),
            "quote_symbol": stable_change.get("coinSymbol"),
            "realised_price_usd": str(quote / quantity),
        })
    return legs


def attach_model(leg: dict, rows: list[dict]) -> dict:
    row = nearest_before(rows, leg["ts_ms"])
    if not row:
        return {**leg, "model": None}
    realised = decimal(leg["realised_price_usd"])
    # The buy pays the ask; the sell receives the bid. Comparing a sale against
    # the ask would invent a cost that does not exist.
    model_ask = (row.get("pool_ask_100_usd") if leg.get("side") == "buy"
                 else row.get("pool_bid_100_usd"))
    delta_bps = None
    if realised and model_ask:
        if leg.get("side") == "buy":
            # Positive = the model's ask was more expensive than what we paid,
            # i.e. the model was conservative on the entry.
            delta_bps = (model_ask - realised) / model_ask * Decimal(10000)
        else:
            # Positive = the model's bid was higher than what we received, i.e.
            # the model was optimistic about the exit.
            delta_bps = (realised - model_ask) / model_ask * Decimal(10000)
    return {**leg, "model": {
        "snapshot_age_ms": leg["ts_ms"] - row["ts_ms"],
        "pool_mid_usd": str(row.get("pool_mid_usd")),
        "pool_ask_100_usd": str(model_ask),
        "side_price_used": "pool_ask" if leg.get("side") == "buy" else "pool_bid",
        "pool_depth_usd": str(row.get("pool_depth_usd")),
        "model_cost_bps": str(row.get("model_cost_bps")),
        "model_cost_breakdown": row.get("model_cost_breakdown"),
        "model_vs_realised_bps": str(delta_bps) if delta_bps is not None else None,
    }}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="config/okx-demo.env")
    parser.add_argument("--db", default="data/hk_monitor.db")
    parser.add_argument("--config", default="config/hk_monitor.toml")
    parser.add_argument("--inst", default="XIAOMI-USDT-SWAP")
    parser.add_argument("--asset", default="XIAOMI")
    parser.add_argument("--fills", type=int, default=20)
    parser.add_argument("--window-hours", type=float, default=12.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    config = load_toml(args.config)
    symbol_to_asset = {str(t.get("symbol", "")).lower(): str(t.get("asset"))
                       for t in config.get("tokens") or []}
    helpers = set(symbol_to_asset)
    helpers.discard("")
    client = OKXReadOnlyClient.from_env(okx_read_env(args.env))

    report: dict = {"instrument": args.inst, "asset": args.asset}
    if not client.configured:
        print("okx credentials missing; the perp side cannot be calibrated", file=sys.stderr)
        return 2

    fills = []
    try:
        for row in client.fills(inst_id=args.inst, limit=args.fills):
            rate = fee_bps(row.get("fee"), row.get("fillPx"), row.get("fillSz"))
            notional = (decimal(row.get("fillPx")) or Decimal(0)) * (decimal(row.get("fillSz")) or Decimal(0))
            fills.append({
                "ts_ms": int(row.get("ts") or 0),
                "time_cst": datetime.fromtimestamp(int(row.get("ts") or 0) / 1000, CST).isoformat(),
                "side": row.get("side"), "size": row.get("fillSz"),
                "price": row.get("fillPx"), "notional_usdt": str(abs(notional)),
                "fee": row.get("fee"), "fee_ccy": row.get("feeCcy"),
                "fee_bps": str(rate) if rate is not None else None,
                "exec_type": row.get("execType"), "order_id": row.get("ordId"),
                "pnl": row.get("fillPnl"),
            })
    except OKXReadError as exc:
        print(f"okx fills unavailable: {exc}", file=sys.stderr)
        return 3
    report["perp_fills"] = fills
    rates = [decimal(f["fee_bps"]) for f in fills if f["fee_bps"]]
    report["perp_fee_bps_measured"] = {
        "count": len(rates),
        "min": str(min(rates)) if rates else None,
        "max": str(max(rates)) if rates else None,
        "average": str(sum(rates) / len(rates)) if rates else None,
        "taker_only": all(f["exec_type"] == "T" for f in fills) if fills else None,
    }
    try:
        uly = args.inst.rsplit("-", 1)[0] if args.inst.count("-") >= 2 else args.inst
        row = client.trade_fee(inst_type="SWAP", uly=uly)
        report["perp_fee_schedule"] = {
            "level": row.get("level"), "taker_bps": str(taker_fee_bps(row)),
            "family": uly,
        }
    except OKXReadError as exc:
        report["perp_fee_schedule"] = {"error": str(exc)}

    # On-chain leg: realised price from the wallet, model price from the DB.
    spot: list[dict] = []
    try:
        orders = wallet_history(OnchainOSRunner(), "xlayer", limit=50)
        # Only legs of the requested asset: pairing a XIAOMI model price with a
        # KUAISHOU fill would invent a 1,000 bps "model error".
        spot = [leg for leg in spot_legs(orders, helpers)
                if symbol_to_asset.get(str(leg.get("symbol", "")).lower()) == args.asset]
    except WalletCliError as exc:
        report["wallet_history_error"] = str(exc)
    window_ms = int(args.window_hours * 3600 * 1000)
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    if spot and Path(args.db).exists():
        rows = model_rows(args.db, args.asset, spot[-1]["ts_ms"] - window_ms, now + 60_000)
        spot = [attach_model(leg, rows) for leg in spot]
    report["spot_legs"] = spot

    costs = (config.get("costs") or {})
    report["model_assumptions"] = {
        "spot_fee_bps": costs.get("spot_fee_bps"),
        "perp_fee_bps": costs.get("perp_fee_bps"),
        "slippage_bps": costs.get("slippage_bps"),
        "failure_bps": costs.get("failure_bps"),
        "verified": costs.get("verified"),
        "roundtrip_gas_budget_usdt": costs.get("roundtrip_gas_budget_usdt"),
    }
    deltas = [decimal(leg["model"]["model_vs_realised_bps"]) for leg in spot
              if leg.get("model") and leg["model"].get("model_vs_realised_bps")
              # A snapshot older than the monitor's own cadence is a different
              # market, not a model error; keep it visible but out of the mean.
              and abs(int(leg["model"].get("snapshot_age_ms") or 0)) <= 30_000]
    report["model_vs_realised"] = {
        "spot_legs_compared": len(deltas),
        "model_minus_realised_bps": [str(x) for x in deltas],
        "excluded_stale_references": [
            {"tx_hash": leg.get("tx_hash"), "side": leg.get("side"),
             "snapshot_age_ms": (leg.get("model") or {}).get("snapshot_age_ms"),
             "delta_bps": (leg.get("model") or {}).get("model_vs_realised_bps")}
            for leg in spot
            if leg.get("model") and leg["model"].get("model_vs_realised_bps")
            and abs(int(leg["model"].get("snapshot_age_ms") or 0)) > 30_000],
        "note": ("positive = the model's on-chain ask was more expensive than the "
                 "realised fill (model conservative)"),
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(f'== {args.asset} / {args.inst} ==')
    print("永续腿真实成交（OKX）")
    for fill in fills:
        print(f'  {fill["time_cst"]} {fill["side"]:4} {fill["size"]}@{fill["price"]} '
              f'fee={fill["fee"]} {fill["fee_ccy"]} = {fill["fee_bps"]} bps '
              f'[{fill["exec_type"]}]')
    print(f'  实测费率: {report["perp_fee_bps_measured"]["average"]} bps '
          f'(n={report["perp_fee_bps_measured"]["count"]}), '
          f'全部 taker={report["perp_fee_bps_measured"]["taker_only"]}')
    print(f'  交易所公布: {report["perp_fee_schedule"]}')
    print("链上腿：实际成交 vs 模型池价")
    for leg in spot:
        model = leg.get("model") or {}
        print(f'  {datetime.fromtimestamp(leg["ts_ms"] / 1000, CST).isoformat()} '
              f'{leg.get("side", "buy"):4} {leg["quantity"]} {leg["symbol"]} ↔ '
              f'{leg["quote_amount"]} {leg["quote_symbol"]} '
              f'实际价={leg["realised_price_usd"]}')
        if model:
            print(f'    模型 ask(100u)={model.get("pool_ask_100_usd")} '
                  f'中价={model.get("pool_mid_usd")} 快照年龄={model.get("snapshot_age_ms")}ms '
                  f'→ 模型比实际贵 {model.get("model_vs_realised_bps")} bps')
    print("模型假设")
    for key, value in report["model_assumptions"].items():
        print(f'  {key} = {value}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
