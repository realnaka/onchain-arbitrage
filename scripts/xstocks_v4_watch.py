#!/usr/bin/env python3
"""Read-only Uniswap v4 watch for xStocks on all supported chains.

Why this is a separate watch:

* v3 pools expose one static fee. v4 pools have a PoolKey fee, an optional
  dynamic-fee flag, and an optional hook contract.
* A hook can charge its own fee, return deltas, override the LP fee per swap,
  gate the swap, or replace the curve. The official V4Quoter is therefore the
  price source: it executes the hook callbacks and returns the real swap delta.
* Quote success is not enough. We also inspect the hook permission bits from
  the hook address and the hook bytecode for simulation-detection patterns.

This script writes ``data/xstocks_v4_watch.json``.  It is read-only and is not
connected to the execution chain.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import xstocks_chain_census as census  # noqa: E402
import xstocks_v4_audit as v4  # noqa: E402
import xstocks_route_matrix as route_matrix  # noqa: E402

# The v4 audit script was written before the RPC host moved.  Keep one source
# of truth for the Ethereum RPC.
v4.CHAINS["eth"]["rpc"] = census.CHAINS["eth"]["rpc"]


def _fast_http_json(url: str, data: bytes | None = None,
                    headers: dict | None = None, timeout: int = 30):
    """Short, bounded version of the audit helper used by the resident watch."""
    request = urllib.request.Request(
        url, data=data,
        headers=headers or {"User-Agent": "xstocks-v4-watch/0.1",
                            "Content-Type": "application/json"})
    last: Exception | None = None
    for _attempt in range(2):
        try:
            with v4.OPENER.open(request, timeout=min(timeout, 12)) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
    raise RuntimeError(str(last))


v4.http_json = _fast_http_json

# Official Uniswap v4 deployments for the xStocks chains.
# Source: https://developers.uniswap.org/deployments.json (read 2026-10-07).
V4_DEPLOYMENTS = {
    "eth": {
        "network": "Ethereum",
        "manager": "0x000000000004444c5dc75cB358380D2e3dE08A90",
        "position": "0xbd216513d74c8cf14cf4747e6aaa6420ff64ee9e",
        "quoter": "0x52f0e24d1c21c8a0cb1e5a5dd6198556bd9e1203",
        "state": "0x7ffe42c4a5deea5b0fec41c94c136cf115597227",
        "dexscreener": "ethereum",
        "gecko": "eth",
    },
    "bsc": {
        "network": "BNB Smart Chain",
        "manager": "0x28e2ea090877bf75740558f6bfb36a5ffee9e9df",
        "position": "0x7a4a5c919ae2541aed11041a1aeee68f1287f95b",
        "quoter": "0x9f75dd27d6664c475b90e105573e550ff69437b0",
        "state": "0xd13dd3d6e93f276fafc9db9e6bb47c1180aee0c4",
        "dexscreener": "bsc",
        "gecko": "bsc",
    },
    "arbitrum": {
        "network": "Arbitrum",
        "manager": "0x360e68faccca8ca495c1b759fd9eee466db9fb32",
        "position": "0xd88f38f930b7952f2db2432cb002e7abbf3dd869",
        "quoter": "0x3972c00f7ed4885e145823eb7c655375d275a1c5",
        "state": "0x76fd297e2d437cd7f76d50f01afe6160f86e9990",
        "dexscreener": "arbitrum",
        "gecko": "arbitrum",
    },
    "optimism": {
        "network": "Optimism",
        "manager": "0x9a13f98cb987694c9f086b1f5eb990eea8264ec3",
        "position": "0x3c3ea4b57a46241e54610e5f022e5c45859a1017",
        "quoter": "0x1f3131a13296fb91c90870043742c3cdbff1a8d7",
        "state": "0xc18a3169788f4f75a170290584eca6395c75ecdb",
        "dexscreener": "optimism",
        "gecko": "optimism",
    },
    "monad": {
        "network": "Monad",
        "manager": "0x188d586ddcf52439676ca21a244753fa19f9ea8e",
        "position": "0x5b7ec4a94ff9bedb700fb82ab09d5846972f4016",
        "quoter": "0xa222dd357a9076d1091ed6aa2e16c9742dd26891",
        "state": "0x77395f3b2e73ae90843717371294fa97cc419d64",
        "dexscreener": "monad",
        "gecko": None,
    },
    "hyperevm": {
        "network": "HyperEVM",
        "manager": "0x12d4fd9c5dedd00ab8a0bce2cf0167bbf94b6b1f",
        "position": "0x0d7ab5b3db668128aff6f70c4ebc71d7d4da9bf9",
        "quoter": "0x108bfe38532c98f8cd8d7ab49c9ddaa3675a1c5e",
        "state": "0x1656326235cb9e34cb58cade53ae30789ab32a1a",
        "dexscreener": "hyperliquid",
        "gecko": "hyperevm",
    },
    "x-layer": {
        "network": "XLayer",
        "manager": "0x360e68faccca8ca495c1b759fd9eee466db9fb32",
        "position": "0xcf1eafc6928dc385a342e7c6491d371d2871458b",
        "quoter": "0x8928074ca1b241d8ec02815881c1af11e8bc5219",
        "state": "0x76fd297e2d437cd7f76d50f01afe6160f86e9990",
        "dexscreener": None,
        "gecko": "x-layer",
    },
    "ink": {
        "network": "Ink",
        "manager": "0x360e68faccca8ca495c1b759fd9eee466db9fb32",
        "position": "0x1b35d13a2e2528f192637f14b05f0dc0e7deb566",
        "quoter": "0x3972c00f7ed4885e145823eb7c655375d275a1c5",
        "state": "0x76fd297e2d437cd7f76d50f01afe6160f86e9990",
        "dexscreener": None,
        "gecko": "ink",
    },
}
for _slug, _row in V4_DEPLOYMENTS.items():
    _chain = census.CHAINS.get(_slug) or {}
    v4.CHAINS[_slug] = {
        "rpc": _chain.get("rpc") or V4_DEPLOYMENTS[_slug].get("rpc") or "",
        "manager": _row["manager"],
        "state": _row["state"],
        "quoter": _row["quoter"],
        "position": _row["position"],
        "block_time": Decimal("1"),
    }
v4.CHAINS["eth"]["rpc"] = census.CHAINS["eth"]["rpc"]

BOOK = ROOT / "data" / "xstocks_book.sqlite"
OUT = ROOT / "data" / "xstocks_v4_watch.json"
GAP = ROOT / "data" / "xstocks_gap_live.json"
STOCK_REF = ROOT / "data" / "xstocks_stock_ref.json"
EXIT_CACHE = ROOT / "data" / "kraken_exit_cache.json"
MULT_CACHE = ROOT / "data" / "xstocks_v4_multipliers.json"
ROUTE_MATRIX = ROOT / "data" / "xstocks_route_matrix.json"
POOL_CACHE = ROOT / "data" / "xstocks_v4_pools.json"
PROXY = "http://127.0.0.1:4780"

SIZES = (Decimal(1), Decimal(5), Decimal(20), Decimal(100), Decimal(500))
KRAKEN_FEE_BPS = Decimal("10")
SLIPPAGE_BUFFER_BPS = Decimal("10")
GAS_BUFFER_UNITS = 120_000
GAS_FALLBACK_UNITS = 300_000
DEFAULT_INTERVAL_S = 300
REF_MAX_AGE_S = 900
MULT_TTL_S = 6 * 3600
LOG_SCAN_ENABLED = False
GECKO_REFRESH_PER_CHAIN = 2
DISCOVERY_TTL_S = 1800
FORCE_DISCOVERY = False
# Same rule as ``arb_monitor.xstocks_v2.units.units_config_path``: the runner
# measures its own table under data-v2/ (gitignored), a fresh clone reads the
# committed seed in config/. This script is launched without PYTHONPATH, so it
# repeats the two-line rule instead of importing the package.
KRAKEN_UNITS_CONFIG = next(
    (p for p in (ROOT / "data-v2" / "kraken_xstocks_units.toml",
                 ROOT / "config" / "kraken_xstocks_units.toml") if p.exists()),
    ROOT / "config" / "kraken_xstocks_units.toml")


def load_kraken_units_config() -> dict:
    try:
        return (tomllib.loads(KRAKEN_UNITS_CONFIG.read_text()).get("kraken_units")
                or {})
    except (OSError, tomllib.TOMLDecodeError):
        return {}


KRAKEN_UNITS = load_kraken_units_config()

HOUR = 3600
DAY = 24 * HOUR
ZERO = "0x" + "0" * 40
STABLE_SYMBOLS = {
    "USDC", "USDT", "USDG", "USD1", "USDT0", "DAI", "USDE", "PYUSD",
    "FDUSD", "USDS", "USDC.E", "USDE.E",
}

PERMISSION_BITS = {
    13: "beforeInitialize",
    12: "afterInitialize",
    11: "beforeAddLiquidity",
    10: "afterAddLiquidity",
    9: "beforeRemoveLiquidity",
    8: "afterRemoveLiquidity",
    7: "beforeSwap",
    6: "afterSwap",
    5: "beforeDonate",
    4: "afterDonate",
    3: "beforeSwapReturnDelta",
    2: "afterSwapReturnDelta",
    1: "afterAddLiquidityReturnDelta",
    0: "afterRemoveLiquidityReturnDelta",
}
SWAP_RETURN_DELTA = {"beforeSwapReturnDelta", "afterSwapReturnDelta"}

OP_NAMES = {
    0x32: "ORIGIN",
    0x55: "SSTORE",
    0x54: "SLOAD",
    0xF1: "CALL",
    0xF4: "DELEGATECALL",
    0xFA: "STATICCALL",
    0xF5: "CREATE2",
    0xFF: "SELFDESTRUCT",
}


def _decimal(value, default: str | None = None) -> Decimal | None:
    try:
        if value in (None, ""):
            return Decimal(default) if default is not None else None
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(default) if default is not None else None


def _addr(value: str) -> str:
    text = str(value or "").strip()
    return text.lower() if text.lower().startswith("0x") else text


def _money(value: Decimal | None, places: int = 6) -> str | None:
    if value is None:
        return None
    try:
        return format(Decimal(str(value)), f".{places}f")
    except (InvalidOperation, ValueError):
        return None


def kraken_units_per_balance_unit(symbol: str, multiplier: Decimal) -> Decimal:
    """Kraken market units per user-visible balance unit.

    EVM ``balanceOf()`` is already the official scaled amount, so it maps 1:1.
    Solana/TON raw amounts must be scaled by the multiplier before this lookup.
    """
    value = _decimal(
        (KRAKEN_UNITS.get(symbol) or {}).get("market_units_per_balance_unit"))
    return value if value and value > 0 else Decimal(1)


def kraken_order_minimum(symbol: str, raw_min: Decimal | None,
                         multiplier: Decimal) -> Decimal | None:
    """Effective minimum in Kraken market units, from live validate results."""
    value = _decimal((KRAKEN_UNITS.get(symbol) or {}).get("effective_min_units"))
    if value is not None:
        return value
    return raw_min


def permission_names(hook: str | None) -> list[str]:
    """Decode the Uniswap v4 hook permission bits from the hook address."""
    if not hook or _addr(hook) == ZERO:
        return []
    try:
        value = int(hook, 16)
    except (TypeError, ValueError):
        return []
    return [name for bit, name in sorted(PERMISSION_BITS.items(), reverse=True)
            if value & (1 << bit)]


def scan_opcodes(code_hex: str) -> dict[str, bool]:
    """Return opcode presence while skipping PUSH data."""
    found = {name: False for name in OP_NAMES.values()}
    raw = bytes.fromhex((code_hex or "0x")[2:]) if (code_hex or "").startswith("0x") else b""
    i = 0
    while i < len(raw):
        op = raw[i]
        name = OP_NAMES.get(op)
        if name:
            found[name] = True
        if 0x60 <= op <= 0x7F:
            i += 1 + (op - 0x5F)
        else:
            i += 1
    return found


def decode_quote_result(result: str | None) -> tuple[int | None, int | None]:
    """Decode ``quoteExactInputSingle`` return data: (amountOut, gasEstimate)."""
    if not result or not result.startswith("0x") or len(result) < 2 + 64 * 2:
        return None, None
    try:
        return int(result[2:66], 16), int(result[66:130], 16)
    except ValueError:
        return None, None


def decode_protocol_fee(raw: int | None, zero_for_one: bool) -> Decimal | None:
    """Protocol fee pips -> bps. Low 12 bits are zeroForOne, high are oneForZero."""
    if raw is None:
        return None
    pips = raw & 0xFFF if zero_for_one else (raw >> 12) & 0xFFF
    return Decimal(pips) / Decimal(100)


def classify_quote_error(error: object) -> str:
    text = str(error or "")
    if "7a5ed734" in text:
        return "V4Quoter: 当前方向没有足够活跃流动性"
    if "execution reverted" in text:
        return "V4Quoter: 交易回滚"
    return text[:240] or "V4Quoter: 未知错误"


def sell_vwap(bids: list | None, shares: Decimal) -> tuple[Decimal | None, Decimal | None]:
    """Kraken bid depth -> (gross USD, VWAP USD per Kraken unit)."""
    if not bids or shares <= 0:
        return None, None
    left = shares
    gross = Decimal(0)
    for row in bids:
        try:
            price = Decimal(str(row[0]))
            size = Decimal(str(row[1]))
        except (IndexError, TypeError, InvalidOperation):
            continue
        take = min(left, size)
        gross += take * price
        left -= take
        if left <= 0:
            return gross, gross / shares
    return None, None


def hook_classification(hook: str | None, fee_key: int | None,
                        code_flags: list[str], opcodes: dict[str, bool] | None = None
                        ) -> dict:
    """Classify a v4 hook by permissions and common risky bytecode patterns."""
    perms = permission_names(hook)
    opcodes = opcodes or {}
    dynamic_fee = fee_key == 0x800000
    sim_flags = [flag for flag in code_flags if "ORIGIN" in flag or "报价合约" in flag]
    reasons: list[str] = []
    tier = "green"
    label = "无 hook / 纯 v4"

    if sim_flags:
        tier, label = "red", "hook 可识别模拟"
        reasons.extend(sim_flags)
    elif dynamic_fee:
        tier, label = "amber", "动态费率池"
        reasons.append("beforeSwap 可逐笔覆盖 LP fee")
    elif any(name in SWAP_RETURN_DELTA for name in perms):
        tier, label = "red", "hook 费用 / 自定义曲线"
        reasons.append("return-delta 权限允许 hook 改余额")
    elif "beforeSwap" in perms or "afterSwap" in perms:
        tier, label = "amber", "swap hook"
        reasons.append("swap 回调可改价、限流或拒绝交易")
    elif perms:
        tier, label = "green", "非 swap hook"
        reasons.append("只注册初始化或流动性回调")
    elif hook and _addr(hook) != ZERO:
        tier, label = "amber", "hook 权限未解"
        reasons.append("hook 地址非零但没有可解码的权限位")

    risky_ops = [
        ("DELEGATECALL", "包含 delegatecall，可能使用代理逻辑"),
        ("CREATE2", "包含 create2，可能创建或升级合约"),
        ("SELFDESTRUCT", "包含 selfdestruct"),
        ("SSTORE", "会写 hook 状态，行为可能随时间变化"),
        ("SLOAD", "会读 hook 状态，行为可能随时间变化"),
        ("CALL", "会调用外部合约"),
        ("STATICCALL", "会读取外部合约"),
    ]
    for name, why in risky_ops:
        if opcodes.get(name):
            reasons.append(why)
            if name in {"DELEGATECALL", "CREATE2", "SELFDESTRUCT"} and tier == "green":
                tier = "amber"

    return {
        "tier": tier,
        "label": label,
        "permissions": perms,
        "dynamic_fee": dynamic_fee,
        "hook_fee_capable": any(name in SWAP_RETURN_DELTA for name in perms),
        "simulation_guard": bool(sim_flags),
        "reasons": reasons,
    }


def quote_economics(size_usd: Decimal, shares: Decimal, kraken_bid: Decimal | None,
                    kraken_fee_bps: Decimal = KRAKEN_FEE_BPS,
                    slippage_bps: Decimal = SLIPPAGE_BUFFER_BPS,
                    gas_usd: Decimal = Decimal(0),
                    deposit_min: Decimal | None = None,
                    kraken_depth: list | None = None,
                    order_min: Decimal | None = None) -> dict:
    """One size's exit economics after Kraken fees, buffer and gas."""
    out = {
        "usd": size_usd,
        "shares": shares,
        "gross_usdt": None,
        "kraken_fee_usdt": None,
        "slippage_buffer_usdt": size_usd * slippage_bps / Decimal(10000),
        "gas_usdt": gas_usd,
        "net_usdt": None,
        "net_bps": None,
        "deposit_ok": None,
        "order_ok": None,
        "sell_vwap_usd": None,
    }
    if deposit_min is not None:
        out["deposit_ok"] = shares >= deposit_min
    if order_min is not None:
        out["order_ok"] = shares >= order_min
    if shares <= 0 or (kraken_bid is None and not kraken_depth):
        return out
    gross, vwap = sell_vwap(kraken_depth, shares)
    if gross is None:
        if kraken_bid is None or kraken_bid <= 0:
            return out
        gross = shares * kraken_bid
        vwap = kraken_bid
    fee = gross * kraken_fee_bps / Decimal(10000)
    net = gross - fee - out["slippage_buffer_usdt"] - gas_usd - size_usd
    out["gross_usdt"] = gross
    out["sell_vwap_usd"] = vwap
    out["kraken_fee_usdt"] = fee
    out["net_usdt"] = net
    if size_usd > 0:
        out["net_bps"] = net / size_usd * Decimal(10000)
    return out


def pick_economics(quotes: list[dict], kraken_bid: Decimal | None,
                   gas_usd: Decimal, deposit_min: Decimal | None,
                   kraken_depth: list | None = None,
                   order_min: Decimal | None = None,
                   min_profit_usd: Decimal = Decimal("0.1")) -> dict:
    """Build per-size economics and choose the best and largest profitable rows."""
    rows: list[dict] = []
    for quote in quotes:
        row = quote_economics(
            size_usd=quote["usd"],
            shares=quote["shares"],
            kraken_bid=kraken_bid,
            gas_usd=gas_usd,
            deposit_min=deposit_min,
            kraken_depth=kraken_depth,
            order_min=order_min,
        )
        row["buy_share_usd"] = quote["share"]
        row["quoted_vs_mid_bps"] = quote.get("quoted_vs_mid_bps")
        rows.append(row)
    valued = [row for row in rows if row["net_usdt"] is not None]
    best = max(valued, key=lambda row: row["net_usdt"]) if valued else None
    profit = [
        row for row in valued
        if row["net_usdt"] > min_profit_usd
        and row["deposit_ok"] is not False
        and row["order_ok"] is not False
    ]
    largest = max(profit, key=lambda row: row["usd"]) if profit else None
    return {"rows": rows, "best": best, "largest_profitable": largest}


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))


def http_json(url: str, timeout: int = 30, attempts: int = 3,
              headers: dict | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url, headers={
            "User-Agent": "xstocks-v4-watch/0.1",
            "Accept": "application/json",
            **(headers or {}),
        })
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with _opener().open(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(str(last))


def kraken_public_ticker(pairs: list[str], asset_class: str = "tokenized_asset") -> dict:
    query = ",".join(pairs)
    url = "https://api.kraken.com/0/public/Ticker?pair=" + urllib.parse.quote(query)
    if asset_class:
        url += "&asset_class=" + urllib.parse.quote(asset_class) + "&assetVersion=1"
    status, body = http_json(url, timeout=30)
    if status != 200:
        return {}
    payload = json.loads(body)
    if payload.get("error"):
        return {}
    return payload.get("result") or {}


def load_refs(now: float) -> dict[str, dict]:
    """Current stock reference, preferring the live board's fresh snapshot."""
    refs: dict[str, dict] = {}
    try:
        live = json.loads(GAP.read_text())
        finished = live.get("finished_ms")
        age = max(0.0, now - float(finished) / 1000) if finished else None
        for ticker, price in (live.get("refs") or {}).items():
            value = _decimal(price)
            if value and value > 0:
                refs[ticker] = {
                    "price": value,
                    "kind": "live-board",
                    "age_s": age,
                    "stale": age is None or age > REF_MAX_AGE_S,
                }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    try:
        cached = json.loads(STOCK_REF.read_text())
    except (OSError, json.JSONDecodeError):
        cached = {}
    for ticker, row in cached.items():
        if ticker in refs:
            continue
        price = _decimal((row or {}).get("price"))
        if not price or price <= 0:
            continue
        at = _decimal((row or {}).get("at"))
        refs[ticker] = {
            "price": price,
            "kind": (row or {}).get("kind") or "cache",
            "age_s": None if at is None else max(0.0, now - float(at)),
            "stale": True,
        }
    return refs


def load_exit_methods() -> dict[str, list[dict]]:
    try:
        payload = json.loads(EXIT_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload.get("deposits") or {}


def load_exit_cache() -> dict:
    try:
        return json.loads(EXIT_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def read_multipliers(pairs: list[tuple[str, str]], now: float) -> dict[tuple[str, str], dict]:
    try:
        cache = json.loads(MULT_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        cache = {}
    out: dict[tuple[str, str], dict] = {}
    changed = False
    for symbol, network in dict.fromkeys(pairs):
        cache_key = f"{symbol}|{network}"
        row = cache.get(cache_key) or {}
        cached_at = _decimal(row.get("at"))
        if row.get("current") and cached_at is not None and now - float(cached_at) < MULT_TTL_S:
            out[(symbol, network)] = row
            continue
        url = ("https://api.xstocks.fi/api/v2/public/assets/"
               + urllib.parse.quote(symbol) + "/multiplier?network="
               + urllib.parse.quote(network))
        current = None
        error = None
        try:
            status, body = http_json(url, timeout=20, attempts=2)
            if status == 200:
                current = _decimal(json.loads(body).get("currentMultiplier"))
            else:
                error = f"http_{status}"
        except Exception as exc:  # noqa: BLE001
            error = type(exc).__name__
        if current and current > 0:
            row = {"current": format(current, "f"), "at": now, "error": None}
        else:
            row = {**row, "error": error or "no_value"}
        out[(symbol, network)] = row
        cache[cache_key] = row
        changed = True
    if changed:
        tmp = MULT_CACHE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False))
        tmp.replace(MULT_CACHE)
    return out


GAS_PRICE_PAIRS = {
    "eth": "ETHUSD",
    "bsc": "BNBUSD",
    "arbitrum": "ETHUSD",
    "optimism": "ETHUSD",
    "monad": "MONUSD",
    "hyperevm": "HYPEUSD",
    "x-layer": "OKBUSD",
    "ink": "ETHUSD",
}


def gas_context() -> dict:
    pairs = sorted(set(GAS_PRICE_PAIRS.values()))
    native_prices: dict[str, Decimal | None] = {}
    try:
        ticker = kraken_public_ticker(pairs, asset_class="")
        for pair, row in ticker.items():
            native_prices[pair] = _decimal(((row.get("c") or [None])[0]))
    except Exception:  # noqa: BLE001
        pass
    out: dict[str, dict] = {}
    for slug, pair in GAS_PRICE_PAIRS.items():
        try:
            gas_price = int(v4.rpc(v4.CHAINS[slug]["rpc"], "eth_gasPrice", []), 16)
        except Exception:  # noqa: BLE001
            gas_price = None
        out[slug] = {"gas_price_wei": gas_price, "native_usd": native_prices.get(pair)}
    return out


def gas_usd_for(gas_estimate: int | None, gc: dict, slug: str) -> Decimal | None:
    row = gc.get(slug) or {}
    gas_price = row.get("gas_price_wei")
    native_usd = row.get("native_usd")
    if not gas_price or not native_usd:
        return None
    units = int(gas_estimate or GAS_FALLBACK_UNITS) + GAS_BUFFER_UNITS
    return Decimal(units * gas_price) / (Decimal(10) ** 18) * native_usd


def hook_code_info(slug: str, hook: str) -> tuple[list[str], dict[str, bool], int | None]:
    code = v4.rpc(v4.CHAINS[slug]["rpc"], "eth_getCode", [hook, "latest"])
    if not code or code == "0x":
        return ["hook 没有合约代码"], {}, 0
    flags: list[str] = []
    hay = code[2:].lower()
    quoters = {chain["quoter"][2:].lower() for chain in v4.CHAINS.values()}
    if any(item in hay for item in quoters):
        flags.append("字节码里写了 V4Quoter 地址，能分辨模拟")
    opcodes = scan_opcodes(code)
    if opcodes.get("ORIGIN"):
        flags.append("字节码使用 ORIGIN，能分辨真实交易和模拟")
    return flags, opcodes, len(code[2:]) // 2


def v4_pairs_for_chains(symbols: set[str] | None = None) -> tuple[list[dict], dict]:
    """Discover current Uniswap v4 pools on every supported xStocks chain."""
    if not FORCE_DISCOVERY and POOL_CACHE.exists():
        try:
            cached = json.loads(POOL_CACHE.read_text())
            age = time.time() - float(cached.get("generated_ms") or 0) / 1000
            if age < DISCOVERY_TTL_S and cached.get("rows"):
                rows = cached.get("rows") or []
                if symbols:
                    rows = [row for row in rows if row.get("symbol") in symbols]
                return rows, {}
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    conn = sqlite3.connect(f"file:{BOOK}?mode=ro", uri=True)
    try:
        universe = census.load_universe(conn, list(V4_DEPLOYMENTS))
    finally:
        conn.close()
    if symbols:
        universe = {
            key: [row for row in rows if row["symbol"] in symbols]
            for key, rows in universe.items()
        }

    out: list[dict] = []
    ds_addresses: list[str] = []
    ds_keys = {row["dexscreener"]: key for key, row in V4_DEPLOYMENTS.items()
               if row.get("dexscreener")}
    for key, rows in universe.items():
        if V4_DEPLOYMENTS[key].get("dexscreener"):
            ds_addresses.extend(_addr(row["address"]) for row in rows
                                if str(row["address"]).startswith("0x"))
    ds_pairs = census.ds_pairs(sorted(set(ds_addresses))) if ds_addresses else {}

    for key, rows in universe.items():
        cfg = V4_DEPLOYMENTS[key]
        if cfg.get("dexscreener"):
            for row in rows:
                for pair in ds_pairs.get(_addr(row["address"]), []):
                    dex = f"{pair.get('dexId') or ''} {' '.join(pair.get('labels') or [])}".lower()
                    if pair.get("chainId") != cfg["dexscreener"] or "v4" not in dex:
                        continue
                    out.append({
                        "slug": key,
                        "network": cfg["network"],
                        "symbol": row["symbol"],
                        "ticker": row["ticker"],
                        "form": row["form"],
                        "address": row["address"],
                        "pair_address": _addr(pair.get("pairAddress")),
                        "dex": pair.get("dexId") or "uniswap-v4",
                        "pool_name": (
                            f"{(pair.get('baseToken') or {}).get('symbol') or ''} / "
                            f"{(pair.get('quoteToken') or {}).get('symbol') or ''}"
                        ).strip(),
                        "base_address": _addr((pair.get("baseToken") or {}).get("address")),
                        "base_symbol": str((pair.get("baseToken") or {}).get("symbol") or ""),
                        "quote_address": _addr((pair.get("quoteToken") or {}).get("address")),
                        "quote_symbol": str((pair.get("quoteToken") or {}).get("symbol") or ""),
                        "depth_usd": _decimal((pair.get("liquidity") or {}).get("usd"), "0"),
                        "volume_h24": _decimal((pair.get("volume") or {}).get("h24"), "0"),
                        "pair_created_ms": pair.get("pairCreatedAt"),
                    })
            continue

        gecko = cfg.get("gecko")
        if not gecko:
            continue
        cache = {}
        try:
            cache = json.loads(v4.CACHE.read_text())
        except (OSError, json.JSONDecodeError):
            cache = {}
        refreshed = 0
        for row in rows:
            ident = f"{gecko}:{_addr(row['address'])}"
            if ident in cache:
                pools = cache.get(ident) or []
            elif refreshed < GECKO_REFRESH_PER_CHAIN:
                try:
                    pools = v4.gecko_pools(gecko, row["address"], cache)
                except Exception:  # noqa: BLE001
                    continue
                refreshed += 1
            else:
                continue
            for pool in pools:
                if "v4" not in str(pool.get("dex") or "").lower():
                    continue
                out.append({
                    "slug": key,
                    "network": cfg["network"],
                    "symbol": row["symbol"],
                    "ticker": row["ticker"],
                    "form": row["form"],
                    "address": row["address"],
                    "pair_address": _addr(pool.get("address")),
                    "dex": pool.get("dex") or "uniswap-v4",
                    "pool_name": pool.get("name") or "",
                    "base_address": _addr(pool.get("base")),
                    "base_symbol": pool.get("base_symbol") or "",
                    "quote_address": _addr(pool.get("quote")),
                    "quote_symbol": pool.get("quote_symbol") or "",
                    "depth_usd": _decimal(pool.get("reserve"), "0"),
                    "volume_h24": None,
                    "pair_created_ms": None,
                })
    out.sort(key=lambda row: (row["slug"], row["symbol"], -(row.get("depth_usd") or Decimal(0))))
    tmp = POOL_CACHE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "generated_ms": int(time.time() * 1000),
        "rows": [
            {**row, "depth_usd": None if row.get("depth_usd") is None else format(row["depth_usd"], "f"),
             "volume_h24": None if row.get("volume_h24") is None else format(row["volume_h24"], "f")}
            for row in out
        ],
    }, ensure_ascii=False))
    tmp.replace(POOL_CACHE)
    return out, universe


def context_build(pairs: list[dict], now: float) -> dict:
    symbols = sorted({row["ticker"] for row in pairs if row.get("ticker")})
    kraken_pairs = [f"{symbol}x/USD" for symbol in symbols]
    ticker = kraken_public_ticker(kraken_pairs)
    bids: dict[str, Decimal] = {}
    for key, row in ticker.items():
        symbol = str(key).split("/", 1)[0]
        value = _decimal((row.get("b") or [None])[0])
        if value and value > 0:
            bids[symbol] = value
    depths: dict[str, list] = {}
    for symbol in dict.fromkeys(row["symbol"] for row in pairs):
        url = ("https://api.kraken.com/0/public/Depth?pair="
               + urllib.parse.quote(f"{symbol}/USD")
               + "&asset_class=tokenized_asset&assetVersion=1&count=25")
        try:
            status, body = http_json(url, timeout=25, attempts=2)
            if status != 200:
                continue
            payload = json.loads(body)
            if payload.get("error"):
                continue
            depths[symbol] = next(iter((payload.get("result") or {}).values()), {}).get("bids") or []
        except Exception:  # noqa: BLE001
            continue
    exit_cache = load_exit_cache()
    matrix: dict = {}
    try:
        matrix = json.loads(ROUTE_MATRIX.read_text())
    except (OSError, json.JSONDecodeError):
        matrix = {}
    return {
        "now": now,
        "refs": load_refs(now),
        "kraken_bids": bids,
        "kraken_depths": depths,
        "exit_methods": exit_cache.get("deposits") or {},
        "exit_pairs": exit_cache.get("pairs") or {},
        "exit_cache_refreshed_ms": exit_cache.get("refreshed_ms"),
        "route_matrix": matrix,
        "route_rows": {
            (row.get("chain"), row.get("symbol")): row
            for row in matrix.get("routes") or []
        },
        "inventory": matrix.get("chains") or {},
        "address_book": matrix.get("address_book") or [],
        "multipliers": read_multipliers(
            [(row["symbol"], row["network"]) for row in pairs], now),
        "gas": gas_context(),
    }


def audit_pair(row: dict, context: dict) -> dict:
    result = {
        "symbol": row["symbol"],
        "ticker": row["ticker"],
        "network": row["network"],
        "chain": row["slug"],
        "form": row["form"],
        "token": row["address"],
        "pool": row["pool_name"],
        "pool_id": row["pair_address"],
        "dex": row["dex"],
        "depth_usd": _money(row.get("depth_usd")),
        "volume_h24": _money(row.get("volume_h24")),
        "tier": "red",
        "label": "未完成",
        "permissions": [],
        "flags": [],
        "quotes": [],
        "monitor_candidate": False,
        "execution_ready": False,
        "blocked_by": "",
    }
    if not row["pair_address"].startswith("0x"):
        result["blocked_by"] = "v4 pool id 不是 0x 地址"
        return result

    slug = row["slug"]
    key = v4.key_from_position_manager(slug, row["pair_address"])
    if key is None and LOG_SCAN_ENABLED:
        created = row.get("pair_created_ms")
        created_at = None
        if created:
            created_at = datetime.fromtimestamp(float(created) / 1000, tz=timezone.utc).isoformat()
        log = None
        try:
            log = v4.find_initialize(slug, row["pair_address"], created_at)
        except Exception:  # noqa: BLE001
            log = None
        if log:
            key = v4.decode_key(log)
    if key is None:
        result["blocked_by"] = "PositionManager 没有这口池的 PoolKey"
        return result

    state = v4.slot0(slug, row["pair_address"]) or {}
    hook = key["hooks"]
    flags, opcodes, code_size = (hook_code_info(slug, hook)
                                 if hook and _addr(hook) != ZERO else ([], {}, 0))
    classification = hook_classification(hook, key.get("fee"), flags, opcodes)
    result.update({
        "tier": classification["tier"],
        "label": classification["label"],
        "permissions": classification["permissions"],
        "flags": flags,
        "hook": hook,
        "hook_code_size": code_size,
        "hook_fee_capable": classification["hook_fee_capable"],
        "dynamic_fee": classification["dynamic_fee"],
        "simulation_guard": classification["simulation_guard"],
        "hook_reasons": classification["reasons"],
        "fee_key": key.get("fee"),
        "fee_key_pct": "动态" if key.get("fee") == 0x800000 else format(Decimal(key.get("fee") or 0) / Decimal(10000), "f"),
        "lp_fee_pct": None if state.get("lp_fee") is None else format(Decimal(state["lp_fee"]) / Decimal(10000), "f"),
        "tick_spacing": key.get("tick_spacing"),
        "protocol_fee_word": state.get("protocol_fee"),
    })

    our = _addr(row["address"])
    c0, c1 = _addr(key["currency0"]), _addr(key["currency1"])
    stable_addresses = {
        _addr(address): symbol
        for symbol, (address, _decimals) in (route_matrix.CHAINS.get(slug, {}).get("stables") or {}).items()
    }
    if our == c0:
        token_is_one = False
        stable = key["currency1"]
        stable_symbol = stable_addresses.get(_addr(stable), "")
    elif our == c1:
        token_is_one = True
        stable = key["currency0"]
        stable_symbol = stable_addresses.get(_addr(stable), "")
    else:
        result["blocked_by"] = "池子两侧都不是这个部署地址"
        return result
    result["stable"] = stable_symbol
    if stable_symbol.upper() not in STABLE_SYMBOLS and not stable_symbol.upper().startswith("USD"):
        result["blocked_by"] = f"对手方 {stable_symbol or stable[:10]} 不是稳定币"
        return result

    multiplier = _decimal(
        (context["multipliers"].get((row["symbol"], row["network"])) or {}).get("current"),
        "1")
    if not multiplier or multiplier <= 0:
        multiplier = Decimal(1)
    kraken_factor = kraken_units_per_balance_unit(row["symbol"], multiplier)
    dec_token = v4.decimals(slug, our)
    dec_stable = v4.decimals(slug, stable)
    zero_for_one = token_is_one
    if state.get("protocol_fee") is not None:
        result["protocol_fee_bps"] = format(
            decode_protocol_fee(state["protocol_fee"], zero_for_one) or Decimal(0), "f")

    if state.get("sqrt"):
        mid_token = v4.spot_price(
            state["sqrt"],
            v4.decimals(slug, key["currency0"]),
            v4.decimals(slug, key["currency1"]),
            token_is_one,
        )
        result["mid_share_usd"] = _money(mid_token / kraken_factor if mid_token else None)
    else:
        mid_token = None

    ref = context["refs"].get(row["ticker"]) or {}
    ref_price = ref.get("price")
    if ref_price:
        result["ref_usd"] = _money(ref_price)
        result["ref_kind"] = ref.get("kind")
        result["ref_age_s"] = ref.get("age_s")
        result["ref_stale"] = bool(ref.get("stale"))

    bid = context["kraken_bids"].get(row["symbol"])
    if bid:
        result["kraken_bid_usd"] = _money(bid)
    route = context["route_rows"].get((slug, row["symbol"])) or {}
    direct = bool(route.get("direct_deposit"))
    bridge = bool(route.get("bridge_to_solana_supported"))
    if not direct and not bridge:
        result["blocked_by"] = "没有出口：Kraken 不收这条链，桥也没有登记到 Solana"
        if result["tier"] != "red":
            result["tier"] = "red"
        return result
    deposit_min = _decimal(route.get("deposit_minimum")) if direct else None
    pair_info = context["exit_pairs"].get(row["symbol"]) or {}
    raw_order_min = _decimal(pair_info.get("ordermin"))
    order_min = kraken_order_minimum(row["symbol"], raw_order_min, multiplier)
    refreshed_ms = context.get("exit_cache_refreshed_ms")
    exit_cache_age_s = None
    if refreshed_ms:
        exit_cache_age_s = max(0.0, context["now"] - float(refreshed_ms) / 1000)
    result["exit_route"] = route.get("exit_route") or ("direct_deposit" if direct else "bridge_to_solana")
    result["direct_deposit"] = direct
    result["bridge_to_solana_supported"] = bridge
    result["bridge_verified"] = bool(route.get("bridge_verified"))
    result["withdraw_method"] = route.get("withdraw_method")
    result["withdraw_fee_units"] = route.get("withdraw_fee_units")
    result["withdraw_minimum"] = route.get("withdraw_minimum")
    result["address_book_ready"] = bool(route.get("address_book_ready"))
    result["required_address"] = route.get("required_address")
    funding = ((context.get("route_matrix") or {}).get("funding") or {}).get(slug) or {}
    result["funding"] = funding
    result["funding_ready"] = bool(funding.get("address_book_ready"))
    result["deposit_method"] = route.get("deposit_method")
    result["deposit_min"] = _money(deposit_min, 8)
    result["kraken_status"] = pair_info.get("status")
    result["kraken_ordermin"] = _money(order_min, 8)
    result["kraken_units_per_token"] = format(kraken_factor, "f")
    units_row = KRAKEN_UNITS.get(row["symbol"]) or {}
    deposit_factor = _decimal(units_row.get("deposit_credit_units_per_balance_unit"))
    result["deposit_credit_units_per_balance_unit"] = (
        format(deposit_factor, "f") if deposit_factor is not None else None)
    result["deposit_credit_verified"] = deposit_factor is not None
    result["exit_cache_age_s"] = exit_cache_age_s
    result["exit_cache_stale"] = exit_cache_age_s is None or exit_cache_age_s > 24 * HOUR

    quotes: list[dict] = []
    last_error = ""
    first_gas = None
    for size in SIZES:
        raw_in = int(size * (Decimal(10) ** dec_stable))
        raw, err = v4.call(
            v4.CHAINS[slug]["rpc"],
            v4.CHAINS[slug]["quoter"],
            v4.encode_quote(key, zero_for_one, raw_in),
        )
        if err:
            last_error = str(err)
            break
        amount_out, gas_estimate = decode_quote_result(raw)
        if amount_out is None or amount_out <= 0:
            last_error = "quote_empty_or_zero"
            break
        if first_gas is None:
            first_gas = gas_estimate
        tokens = Decimal(amount_out) / (Decimal(10) ** dec_token)
        shares = tokens * kraken_factor
        share_price = size / shares if shares > 0 else Decimal(0)
        vs_mid = None
        if mid_token and mid_token > 0:
            # Price per Kraken unit is the token price divided by the verified
            # Kraken-units-per-token factor.
            vs_mid = (share_price / (mid_token / kraken_factor) - 1) * Decimal(10000)
        quotes.append({
            "usd": size,
            "tokens": tokens,
            "shares": shares,
            "share": share_price,
            "quoted_vs_mid_bps": vs_mid,
            "gas_estimate": gas_estimate,
        })
        if share_price > Decimal(0) and ref_price and (
                share_price > ref_price * Decimal(20) or share_price * Decimal(20) < ref_price):
            last_error = "quote far from stock reference"
            break
    result["quotes"] = [
        {
            "usd": format(item["usd"], "f"),
            "tokens": format(item["tokens"], "f"),
            "shares": format(item["shares"], "f"),
            "buy_share_usd": _money(item["share"]),
            "quoted_vs_mid_bps": _money(item.get("quoted_vs_mid_bps"), 2),
            "gas_estimate": item.get("gas_estimate"),
        }
        for item in quotes
    ]
    if not quotes:
        result["blocked_by"] = classify_quote_error(last_error) if last_error else "V4Quoter 没有报价"
        return result

    gas_cost = gas_usd_for(first_gas, context["gas"], slug)
    result["gas_estimate"] = first_gas
    result["gas_usdt"] = _money(gas_cost)
    economics = pick_economics(
        quotes, bid, gas_cost or Decimal(0), deposit_min,
        kraken_depth=context["kraken_depths"].get(row["symbol"]),
        order_min=order_min,
    )
    result["economics"] = [
        {
            "usd": format(item["usd"], "f"),
            "buy_share_usd": _money(item.get("buy_share_usd")),
            "shares": format(item["shares"], "f"),
            "gross_usdt": _money(item.get("gross_usdt")),
            "kraken_fee_usdt": _money(item.get("kraken_fee_usdt")),
            "slippage_buffer_usdt": _money(item.get("slippage_buffer_usdt")),
            "gas_usdt": _money(item.get("gas_usdt")),
            "sell_vwap_usd": _money(item.get("sell_vwap_usd")),
            "net_usdt": _money(item.get("net_usdt")),
            "net_bps": _money(item.get("net_bps"), 2),
            "deposit_ok": item.get("deposit_ok"),
            "order_ok": item.get("order_ok"),
        }
        for item in economics["rows"]
    ]
    best = economics["best"]
    largest = economics["largest_profitable"]
    for key_name, item in (("best", best), ("largest_profitable", largest)):
        if item:
            result[f"{key_name}_size_usd"] = format(item["usd"], "f")
            result[f"{key_name}_net_usdt"] = _money(item.get("net_usdt"))
            result[f"{key_name}_net_bps"] = _money(item.get("net_bps"), 2)
    if best:
        result["best_buy_share_usd"] = _money(best.get("buy_share_usd"))
        result["best_vs_kraken_bps"] = (
            _money((best["buy_share_usd"] / bid - 1) * Decimal(10000), 2)
            if bid and best.get("buy_share_usd") else None
        )
        if best.get("quoted_vs_mid_bps") is not None and result.get("lp_fee_pct"):
            result["extra_over_lp_bps"] = _money(
                best["quoted_vs_mid_bps"]
                - Decimal(result["lp_fee_pct"]) * Decimal(100)
                - Decimal(result.get("protocol_fee_bps") or "0"),
                2)
    if ref_price and result.get("best_buy_share_usd"):
        result["best_vs_stock_bps"] = _money(
            (Decimal(result["best_buy_share_usd"]) / ref_price - 1) * Decimal(10000), 2)

    reasons: list[str] = []
    if not best or best.get("net_usdt") is None:
        reasons.append("没有 Kraken 卖价或无法算净利")
    elif best["net_usdt"] <= Decimal("0.1"):
        reasons.append("净利≤0.1 USDT")
    if best and best.get("deposit_ok") is False:
        reasons.append("数量低于 Kraken 直接充值最小值")
    if best and best.get("order_ok") is False:
        reasons.append("数量低于 Kraken 最小下单量")
    if classification["tier"] == "red":
        reasons.append(classification["label"])
    if result.get("ref_stale"):
        reasons.append("正股参考价陈旧")
    if result.get("exit_cache_stale"):
        reasons.append("Kraken 充值缓存超过 24 小时，执行前必须刷新")
    if result.get("exit_route") == "bridge_to_solana":
        reasons.append("需要桥到 Solana 再充 Kraken；链路尚未实盘验证")
    result["blocked_by"] = "、".join(dict.fromkeys(reasons))
    result["monitor_candidate"] = bool(
        largest
        and classification["tier"] != "red"
        and result.get("exit_route") in {"direct_deposit", "bridge_to_solana"}
    )
    result["execution_ready"] = False
    return result


def run_once() -> dict:
    pairs, _ = v4_pairs_for_chains()
    now = time.time()
    context = context_build(pairs, now)
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda row: audit_pair(row, context), pairs))
    rows.sort(key=lambda row: (
        row.get("tier") == "red",
        -float(row.get("best_net_bps") or "-999999"),
    ))
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "generated_ms": int(now * 1000),
        "chains": sorted({row["slug"] for row in pairs}),
        "pools_seen": len(rows),
        "quoted": sum(1 for row in rows if row.get("quotes")),
        "monitor_candidates": sum(1 for row in rows if row.get("monitor_candidate")),
        "tiers": {
            "green": sum(1 for row in rows if row.get("tier") == "green"),
            "amber": sum(1 for row in rows if row.get("tier") == "amber"),
            "red": sum(1 for row in rows if row.get("tier") == "red"),
        },
        "gas": {
            slug: {
                "gas_price_wei": row.get("gas_price_wei"),
                "native_usd": _money(row.get("native_usd"), 6),
            }
            for slug, row in (context["gas"] or {}).items()
        },
        "kraken_fee_bps": str(KRAKEN_FEE_BPS),
        "slippage_buffer_bps": str(SLIPPAGE_BUFFER_BPS),
        "kraken_units_config": {
            "path": str(KRAKEN_UNITS_CONFIG.relative_to(ROOT)),
            "entries": len(KRAKEN_UNITS),
            "deposit_verified": sorted(
                symbol for symbol, row in KRAKEN_UNITS.items()
                if row.get("deposit_credit_units_per_balance_unit")),
        },
        "exit_cache_refreshed_ms": context.get("exit_cache_refreshed_ms"),
        "funding": (context.get("route_matrix") or {}).get("funding") or {},
        "inventory": context.get("inventory") or {},
        "address_book": context.get("address_book") or [],
        "note": (
            "只读 v4 watch。报价走官方 V4Quoter，hook 回调会在 eth_call 中执行。"
            "没有接进执行链。tier=green 表示无 hook 或只有非 swap hook；amber 表示动态费率或 swap hook；"
            "red 表示 hook 可识别模拟、可返回 delta、报价失败或出口不成立。"
        ),
        "rows": rows,
    }


def write_once(out: Path = OUT) -> dict:
    payload = run_once()
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    tmp.replace(out)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one pass and exit")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S,
                        help="seconds between passes when resident")
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--deep-log-scan", action="store_true",
                        help="also search Initialize logs when PositionManager misses a PoolKey")
    parser.add_argument("--refresh-pools", action="store_true",
                        help="force a new pool discovery pass")
    args = parser.parse_args(argv)
    global LOG_SCAN_ENABLED, FORCE_DISCOVERY
    LOG_SCAN_ENABLED = bool(args.deep_log_scan)
    FORCE_DISCOVERY = bool(args.refresh_pools)
    out = Path(args.out)
    while True:
        try:
            if args.no_write:
                payload = run_once()
            else:
                payload = write_once(out)
            tiers = payload.get("tiers") or {}
            print(
                f"[{payload['generated_at']}] pools={payload['pools_seen']} "
                f"quoted={payload['quoted']} candidates={payload['monitor_candidates']} "
                f"tiers={tiers}",
                flush=True,
            )
            for row in payload["rows"]:
                if row.get("quotes"):
                    print(
                        f"  {row.get('chain'):>7} {row['symbol']:>7} {row.get('tier'):>5} "
                        f"{row.get('label','')} net={row.get('best_net_usdt') or '–'} "
                        f"bps={row.get('best_net_bps') or '–'} "
                        f"blocked={row.get('blocked_by') or '–'}",
                        flush=True,
                    )
        except Exception as exc:  # noqa: BLE001
            print(f"v4 watch failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(max(30.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
