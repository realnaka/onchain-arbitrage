#!/usr/bin/env python3
"""Resident xStocks discount board.

Quotes Kraken-listed Solana tokens through Jupiter and a few known EVM pools.
Writes data/xstocks_gap_live.json and serves a read-only page. No orders.

Each pass, in order:
  1. Stock reference: Jupiter Price V3 `stockData.price` for every Solana mint.
  2. Names it lacks fall back to FMP / Yahoo.
  3. A rotating few references are re-checked against Yahoo; > 1% is flagged.
  4. Buy quotes: Jupiter swap/v1 ladder 1-5-20-100-500 USDC per mint, then the
     EVM pools. Mints Jupiter cannot route are skipped until the 30-minute recheck.
  5. Keep rows still cheaper than the reference by more than 10 bps.
All Jupiter calls (steps 1 and 4) share one paced budget.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.venues.kraken_exit_gate import (  # noqa: E402
    book_exit, parse_asset_pairs, parse_bridge_symbols, reachable)
from arb_monitor.venues.kraken_spot import (  # noqa: E402
    KrakenSpotClient, load_credentials)

DB = ROOT / "data" / "xstocks_book.sqlite"
MULT = ROOT / "data" / "xstocks_multipliers.json"
OUT = ROOT / "data" / "xstocks_gap_live.json"
ETH_LEDGER = ROOT / "data" / "xstocks_eth_cycles.jsonl"
STOCK_CACHE = ROOT / "data" / "xstocks_stock_ref.json"
FMP_USAGE = ROOT / "data" / "xstocks_fmp_usage.json"
FMP_ENV = ROOT / "config" / "fmp.env"
JUP_ENV = ROOT / "config" / "jupiter.env"
PROXY = "http://127.0.0.1:4780"
# Every Jupiter call shares one budget. api.jup.ag publishes 0.5/s keyless,
# 1/s on a free key and 10/s on Developer, over a 60 s sliding window.
# lite-api.jup.ag publishes nothing and is being retired with shrinking limits;
# on 2026-10-03 it let about 130 calls through in 27 s, then answered 429 to
# everything while 4 workers kept retrying. A full pass paced at 1.35/s still
# drew 8 429s, so the keyless default sits below that.
JUP_LITE_RPS = 1.0
JUP_KEY_RPS = 1.0
JUP_HEADROOM = 0.9
JUP_PAUSE_S = 60.0
# Paid batch-quote can refresh the whole book every 6 minutes.
# The free plan rejects that endpoint (HTTP 402) and allows 250 calls/day,
# so free mode quotes one symbol at a time and keeps each price for 18 hours.
STOCK_TTL_S = 360
FMP_FREE_TTL_S = 18 * 3600
FMP_FREE_PER_CYCLE = 12
FMP_DAILY_STOP = 230
YAHOO_PER_CYCLE = 20
# Stock reference comes from Jupiter Price V3 `stockData.price` (xStocks feed,
# last trade including extended hours; 177/177 covered on 2026-10-03). FMP and
# Yahoo only fill names it lacks. Each pass re-checks a few names against
# Yahoo's own last trade (pre/post included, same basis) and flags gaps > 1%.
PRICE_V3_BATCH = 50
REF_KIND_JUP = "Jupiter 正股价"
REF_REUSE_S = 900
REF_CHECK_PER_CYCLE = 3
REF_ALARM = Decimal("0.01")
EVM_CACHE = ROOT / "data" / "xstocks_gap_evm_routes.json"
# 多链普查的产物（scripts/xstocks_chain_census.py）。看板只报价普查选出来的池子，
# 所以没有池子的链会以「已查、0 个池」出现在链覆盖表里，而不是凭空消失。
CENSUS = ROOT / "data" / "xstocks_chain_census.json"
SOL_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
SOL_DECIMALS = 8
WORKERS = 4
GAP_LIMIT = Decimal("-10")
SIZES = (Decimal(1), Decimal(5), Decimal(20), Decimal(100), Decimal(500))
# Execution settings. 2026-10-03 operator: slippage 10 bps, plus a Jito tip so
# the swap does not sit in the public path. A sandwich can only take what the
# slippage tolerance allows, so 50 bps on this book exposed ~$4.3 of a ~$10
# total edge. See docs/research/xstocks-execution-audit-2026-10-03.md.
SLIPPAGE_BPS = 10
JITO_TIP_LAMPORTS = 1_000_000
# Cost model for the "扣除所有费用能赚多少" column. 只算买入这一侧：
# 链上买入 → 充值 Kraken → 按正股价卖出。不做任何「卖回同一口池」的假设。
# Kraken xStocks taker 费率（实测 8.21–10 bps，取 10）。
EXIT_COST_BPS = Decimal("10")
BUY_FEE_SOL = Decimal("0.000105")      # 实测：两跳兑换 105k lamports
# 转账费按「这个标的以前有没有转过」分两档（docs/lessons §2.2 更正：
# 以前把 0.000015 当成全部转账费，错两个数量级）。
TRANSFER_FEE_FIRST_SOL = Decimal("0.00313")   # 首次：还要替 Kraken 建收币账户，不可退
TRANSFER_FEE_REPEAT_SOL = Decimal("0.000015")  # 账户已存在：只剩基础费
TOKEN_ACCOUNT_RENT_SOL = Decimal("0.00156")  # 我们自己的代币账户租金，关账户可退
TRANSFER_SEEN = ROOT / "data" / "xstocks_transfer_seen.json"
_transfer_seen_cache: dict = {"at": 0.0, "seen": {}}


def transfer_fee_sol(symbol: str) -> Decimal:
    """这个标的的充值转账费：首次含建账户租金，之后只剩基础费。

    `data/xstocks_transfer_seen.json` 由自动链维护；看板只读。文件不存在时
    一律按「首次」算（宁可高估成本，也不把亏损显示成盈利）。
    """
    now = time.time()
    if now - _transfer_seen_cache["at"] > 60:
        try:
            seen = json.loads(TRANSFER_SEEN.read_text())
        except Exception:  # noqa: BLE001
            seen = {}
        _transfer_seen_cache.update({"at": now, "seen": seen})
    seen = _transfer_seen_cache["seen"] or {}
    return (TRANSFER_FEE_REPEAT_SOL if symbol in seen else TRANSFER_FEE_FIRST_SOL)


def chain_fee_sol(symbol: str) -> Decimal:
    return BUY_FEE_SOL + transfer_fee_sol(symbol)
SOL_MINT = "So11111111111111111111111111111111111111112"
# Pools already checked by hand. Quoted every cycle; Gecko is not the price.
EVM_ROUTES = [
    {
        "symbol": "NBISx", "ticker": "NBIS", "network": "XLayer", "slug": "x-layer",
        "form": "wrapper", "token": "0x43a7b7acfae969b0682e56142b52e63e07d59b7d",
        "pool_name": "wNBISx / USDG 0.05%",
    },
    {
        "symbol": "SLVx", "ticker": "SLV", "network": "XLayer", "slug": "x-layer",
        "form": "wrapper", "token": "0xb842eacb35fd9c1beda53749072ef22823f2ca8c",
        "pool_name": "wSLVx / USDC 0.05%",
    },
    {
        "symbol": "CRWVx", "ticker": "CRWV", "network": "XLayer", "slug": "x-layer",
        "form": "wrapper", "token": "0xadf63a79c9813a8e0c4d1d334ebdfaeef04bdf98",
        "pool_name": "wCRWVx / USDG 0.05%",
    },
    {
        "symbol": "MSTRx", "ticker": "MSTR", "network": "BNB Smart Chain", "slug": "bsc",
        "form": "原生", "token": "0xae2f842ef90c0d5213259ab82639d5bbf649b08e",
        "pool_name": "mstrx / USDT 0.25%",
    },
]

_state_lock = threading.Lock()
# 多链普查的结果：链覆盖表写进每一轮快照，操作员因此能看见
# 「Monad 已查、0 个池子」这类状态，而不是看不见这条链。
_chains_summary: dict = {}
_census_at: str = ""
_evm_universe: int = 0
_census_total: int = 0
_census_window: list[str] = []
# 每轮询价的 EVM 路由上限。候选比这个多时按轮转切片，几轮内都会轮到，
# 单轮耗时因此有上限（Solana 那 177 个 mint 仍然是每轮全询）。
# 2026-10-03 门槛降到 20 USD 后候选变多（23 条），16 条在 Solana 那 ~190 秒里
# 并行跑得完，不会把一轮拖长；轮转仍然保留，用来兜住候选继续变多的情形。
EVM_BUDGET = 16
_state: dict = {
    "phase": "starting",
    "finished_at": None,
    "finished_ms": None,
    "cycle_s": None,
    "quoted": 0,
    "with_ref": 0,
    "without_ref": 0,
    "errors": 0,
    "hits": [],
    "note": "第一轮询价还没完成",
}


# ---------------------------------------------------------------------------
# Kraken 出口闸门（operator 2026-10-05）
#
# 只看 Solana / X Layer。买之前就必须知道「买进去能不能卖出去」，
# 出口不成立的名字不上榜——不是排在后面，是不显示。
#   1. Kraken 有没有这个交易对、状态能不能成交（每轮 1 次公开调用）
#   2. Kraken 收不收这条链（data/kraken_exit_cache.json，由
#      scripts/kraken_deposit_refresh.py 刷新）
#   3. 我们自己的数量会不会把 Kraken 盘口砸穿（每行 1 次公开 Depth）
# ---------------------------------------------------------------------------
KRAKEN_EXIT_CACHE = ROOT / "data" / "kraken_exit_cache.json"
# operator 2026-10-06：单笔 10 → 25 USDT（与 ops/xstocks_policy.toml 的
# [trade] per_trade_quote_usdt 对齐；实际每笔仍取 min(上限, 阶梯上仍便宜的最大档)）
TRADE_SIZE_USD = Decimal("25")
MAX_EXIT_SLIP_BPS = Decimal("50")
EXIT_PROBE_ROWS = 30
EXIT_PROBE_WORKERS = 6
PAIR_TTL_S = 300.0
# 卖出锚（operator 2026-10-06）：机会用「链上买入价 vs Kraken 实际买盘」判断，
# 正股价只作参考。Kraken 公开 Ticker 一轮一次拿回全部标的的买一。
KRAKEN_TICKER_TTL_S = 120.0
# 对 Kraken 有效卖价还差这么点（bps）以内的行，当作「近失」列出来让人看见，
# 但它们不是机会：不给净利、不进口闸门、不进可执行。
NEAR_MISS_BPS = Decimal("100")
NEAR_MISS_MAX = 6

_pair_cache: dict = {"at": 0.0, "pairs": {}}
_bridge_cache: dict = {"at": 0.0, "symbols": set()}
_ticker_cache: dict = {"at": 0.0, "bids": {}}
_exit_rejects: list[dict] = []
_exit_notes: dict = {}


def kraken_pairs_live(force: bool = False) -> dict:
    """Kraken 在册标的的实时状态。一次公开调用拿全表（约 350 行 / 177 个标的）。"""
    now = time.time()
    if not force and _pair_cache["pairs"] and now - _pair_cache["at"] < PAIR_TTL_S:
        return _pair_cache["pairs"]
    try:
        status, body = _get(
            "https://api.kraken.com/0/public/AssetPairs?aclass_base=tokenized_asset", timeout=30)
        if status == 200:
            payload = json.loads(body)
            if not payload.get("error"):
                pairs = parse_asset_pairs(payload.get("result") or {})
                if pairs:
                    _pair_cache.update({"at": now, "pairs": pairs})
                    return pairs
    except Exception:  # noqa: BLE001
        pass
    return _pair_cache["pairs"]


def kraken_deposits() -> dict:
    try:
        return json.loads(KRAKEN_EXIT_CACHE.read_text()).get("deposits") or {}
    except Exception:  # noqa: BLE001
        return {}


def kraken_bridge_symbols() -> set:
    """登记表里「有 Solana 端部署」的桥品种。X Layer 出发必须先过这一条。"""
    now = time.time()
    if _bridge_cache["symbols"] and now - _bridge_cache["at"] < 3600:
        return _bridge_cache["symbols"]
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        values = [row[0] for row in conn.execute(
            "SELECT products_json FROM bridges WHERE missing_since_ms IS NULL")]
        conn.close()
        symbols = parse_bridge_symbols(values)
    except Exception:  # noqa: BLE001
        symbols = set()
    _bridge_cache.update({"at": now, "symbols": symbols})
    return symbols


def kraken_bids(symbol: str) -> list:
    url = ("https://api.kraken.com/0/public/Depth?pair="
           + urllib.parse.quote(f"{symbol}/USD")
           + "&asset_class=tokenized_asset&assetVersion=1&count=25")
    try:
        status, body = _get(url, timeout=20)
        if status != 200:
            return []
        payload = json.loads(body)
        if payload.get("error"):
            return []
        rows = payload.get("result") or {}
        return list(rows.values())[0].get("bids") or []
    except Exception:  # noqa: BLE001
        return []


def kraken_best_bids(symbols: list[str]) -> dict[str, Decimal]:
    """Kraken 公开 Ticker：一次拿回全部 xStocks 的买一（USD/枚，公开调用、无密钥）。

    机会判断的卖出锚（operator 2026-10-06）：我们真的在 Kraken 卖出，
    所以「有没有套利」用链上买入价对 Kraken 买盘，正股价只作参考。
    进程内缓存，失败时沿用上一次的结果。
    """
    now = time.time()
    cached = _ticker_cache["bids"]
    if cached and now - _ticker_cache["at"] < KRAKEN_TICKER_TTL_S:
        return cached
    symbols = [symbol for symbol in dict.fromkeys(symbols) if symbol]
    if not symbols:
        return cached
    pair = ",".join(f"{symbol}/USD" for symbol in symbols)
    url = ("https://api.kraken.com/0/public/Ticker?pair=" + urllib.parse.quote(pair)
           + "&asset_class=tokenized_asset&assetVersion=1")
    try:
        status, body = _get(url, timeout=30)
        if status != 200:
            return cached
        payload = json.loads(body)
        if payload.get("error"):
            return cached
        bids: dict[str, Decimal] = {}
        for key, row in (payload.get("result") or {}).items():
            symbol = str(key).split("/", 1)[0]
            try:
                bid = Decimal(str((row.get("b") or [None])[0]))
            except Exception:  # noqa: BLE001
                continue
            if bid > 0:
                bids[symbol] = bid
        if bids:
            _ticker_cache.update({"at": now, "bids": bids})
        return _ticker_cache["bids"]
    except Exception:  # noqa: BLE001
        return cached


# ---------------------------------------------------------------------------
# 持仓展示（operator 2026-10-06）：钱包里有什么币、多少；Kraken 里有什么币、多少。
#
# 只读。三个来源各自刷新，一个失败不影响另外两个，失败原因照实写在页面上：
#   Solana 钱包   getBalance + getTokenAccountsByOwner（SPL + Token-2022）
#   X Layer 钱包  eth_getBalance + ERC20 balanceOf（在册代币 + 稳定币）
#   Kraken        私有 Balance，进程内缓存 5 分钟（不读私钥下单）
# 估值 = 数量 × 乘数 × 正股价，价格取本进程上一轮的正股对照；拿不到就留空（页面上「–」）。
# ---------------------------------------------------------------------------
SOLANA_WALLET = "AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev"
EVM_WALLET = "0xabababababababababababababababababababab"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
XLAYER_RPC = "https://rpc.xlayer.tech"
ETHEREUM_RPC = "https://ethereum-rpc.publicnode.com"
SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
HOLDINGS_TTL_S = 90.0          # 钱包余额刷新间隔
KRAKEN_BALANCE_TTL_S = 300.0   # 私有调用，5 分钟一次
STABLE_SYMBOLS = {"USD", "USDC", "USDT", "USDG", "USDT0", "USD1", "DAI", "PYUSD", "FDUSD", "USDE"}
# X Layer 上的现金腿。小数位写死 6；其它代币一律按 18 位。
XLAYER_STABLES = {
    "USDG": ("0x4ae46a509f6b1d9056937ba4500cb143933d2dc8", 6),
    "USDT0": ("0x779ded0c9e1022225f8e0630b35a9b54be713736", 6),
}
ETHEREUM_STABLES = {
    "USDC": ("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6),
    "USDT": ("0xdac17f958d2ee523a2206206994597c13d831ec7", 6),
}
HOLDINGS: dict = {"at_ms": None, "solana": None, "xlayer": None, "kraken": None}
_holdings_lock = threading.Lock()
_kraken_cache: dict = {"at": 0.0}
_ticker_of_symbol: dict[str, str] = {}
_sol_mint_symbols: dict[str, str] = {}


def _jsonrpc(url: str, calls: list[dict], timeout: int = 30) -> list[dict]:
    body = json.dumps(calls).encode()
    request = urllib.request.Request(url, data=body, headers={
        "User-Agent": "xstocks-gap-live/1",
        "content-type": "application/json",
        "Accept": "application/json"})
    with _opener().open(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def _jsonrpc_retry(url: str, calls: list[dict], tries: int = 3) -> list[dict]:
    """公共 RPC 偶发 429；重试几次，仍失败就把原因交给调用者。"""
    last: Exception | None = None
    for attempt in range(tries):
        try:
            return _jsonrpc(url, calls)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt + 1 < tries:
                time.sleep(1.5 * (attempt + 1))
    raise last if last is not None else RuntimeError("rpc failed")


def _rpc_rows(url: str, calls: list[dict], chunk: int = 10, pause: float = 0.3) -> list[dict]:
    """分批发 JSON-RPC。X Layer 的 RPC 拒绝大批次（too many RPC calls in batch request）。"""
    rows: list[dict] = []
    for start in range(0, len(calls), chunk):
        response = _jsonrpc_retry(url, calls[start:start + chunk])
        if isinstance(response, dict):
            raise RuntimeError(f"rpc error: {response.get('error') or response}")
        rows.extend(response)
        if start + chunk < len(calls):
            time.sleep(pause)
    return rows


def _symbol_tickers() -> dict[str, str]:
    global _ticker_of_symbol
    if not _ticker_of_symbol:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            _ticker_of_symbol = dict(conn.execute(
                "SELECT issuer_symbol, underlying_symbol FROM underlyings"))
        finally:
            conn.close()
    return _ticker_of_symbol


def _solana_mint_map() -> dict[str, str]:
    global _sol_mint_symbols
    if not _sol_mint_symbols:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            found = dict(conn.execute(
                "SELECT address, issuer_symbol FROM deployments "
                "WHERE network = 'Solana' AND form = 'native' AND missing_since_ms IS NULL"))
        finally:
            conn.close()
        # 买入/回款用的稳定币不在 xStock 登记表里，补上名字（否则页面只显示 mint 前缀）。
        found[SOL_USDC] = "USDC"
        found[SOL_USDT] = "USDT"
        _sol_mint_symbols = found
    return _sol_mint_symbols


def _gas_value(symbol: str, amount: Decimal) -> Decimal | None:
    if symbol != "SOL":
        return None
    sol_usd = Decimal(str(_state.get("sol_usd") or "0"))
    return amount * sol_usd if sol_usd > 0 else None


def _value_of(symbol: str, amount: Decimal, multipliers: dict[str, Decimal],
              scale_multiplier: bool = True) -> Decimal | None:
    """Value a holding. Solana raw units need × multiplier; EVM/Kraken already scaled."""
    if symbol in STABLE_SYMBOLS:
        return amount
    ticker = _symbol_tickers().get(symbol)
    raw = (_state.get("refs") or {}).get(ticker) if ticker else None
    if raw in (None, ""):
        return _gas_value(symbol, amount)
    try:
        factor = (multipliers.get(symbol) or Decimal(1)) if scale_multiplier else Decimal(1)
        return Decimal(str(raw)) * factor * amount
    except Exception:  # noqa: BLE001
        return None


def _hold_row(asset: str, amount: Decimal, note: str, value: Decimal | None) -> dict:
    text = f"{amount:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".") or "0"
    return {
        "asset": asset,
        "amount": text,
        "note": note,
        "value": None if value is None else f"{value:.2f}",
    }


def solana_holdings(multipliers: dict[str, Decimal]) -> dict:
    at = int(time.time() * 1000)
    result = _jsonrpc_retry(SOLANA_RPC, [{
        "jsonrpc": "2.0", "id": "sol", "method": "getBalance",
        "params": [SOLANA_WALLET]}])
    lamports = (result[0].get("result") or {}).get("value")
    if lamports is None:
        raise RuntimeError(f"getBalance: {result[0].get('error')}")
    sol = Decimal(int(lamports)) / Decimal(10) ** 9
    rows = [_hold_row("SOL", sol, "Gas", _gas_value("SOL", sol))]
    symbols = _solana_mint_map()
    empty = 0
    for program, label in ((SPL_TOKEN_PROGRAM, "SPL"), (TOKEN_2022_PROGRAM, "Token-2022")):
        time.sleep(0.6)          # 公共 RPC 对连发很敏感，隔开一点
        out = _jsonrpc_retry(SOLANA_RPC, [{
            "jsonrpc": "2.0", "id": "tokens", "method": "getTokenAccountsByOwner",
            "params": [SOLANA_WALLET, {"programId": program}, {"encoding": "jsonParsed"}]}])
        if out[0].get("error"):
            raise RuntimeError(f"getTokenAccountsByOwner({label}): {out[0]['error']}")
        for account in ((out[0].get("result") or {}).get("value")) or []:
            info = (((account.get("account") or {}).get("data") or {})
                    .get("parsed", {}).get("info") or {})
            mint = str(info.get("mint") or "")
            amount = Decimal(str(((info.get("tokenAmount") or {}).get("uiAmountString")) or "0"))
            if amount == 0:
                empty += 1
                continue
            symbol = symbols.get(mint) or (mint[:6] + "…")
            note = "稳定币" if symbol in STABLE_SYMBOLS else label
            rows.append(_hold_row(symbol, amount, note, _value_of(symbol, amount, multipliers)))
    return {"at_ms": at, "error": None, "wallet": SOLANA_WALLET, "empty": empty, "rows": rows}


def xlayer_watch() -> list[tuple[str, str, str, int]]:
    """（符号, 形式, 合约, 小数位）：稳定币 + 看板正在报价的 X Layer 代币。"""
    watch = [(symbol, "稳定币", address, decimals)
             for symbol, (address, decimals) in XLAYER_STABLES.items()]
    symbols = {route["symbol"] for route in EVM_ROUTES if route.get("network") == "XLayer"}
    try:
        symbols.update(route["symbol"] for route in load_census()[0]
                       if route.get("network") == "XLayer")
    except Exception:  # noqa: BLE001
        pass
    if symbols:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT issuer_symbol, form, address FROM deployments "
                "WHERE network = 'XLayer' AND missing_since_ms IS NULL "
                f"AND issuer_symbol IN ({','.join('?' * len(symbols))})",
                tuple(sorted(symbols))).fetchall()
        finally:
            conn.close()
        watch.extend((symbol, "包装" if form.startswith("wrapper") else "原生", address, 18)
                     for symbol, form, address in rows)
    return watch


def xlayer_holdings(multipliers: dict[str, Decimal]) -> dict:
    at = int(time.time() * 1000)
    watch = xlayer_watch()
    data = "0x70a08231" + "0" * 24 + EVM_WALLET[2:].lower()
    calls = [{"jsonrpc": "2.0", "id": "gas", "method": "eth_getBalance",
              "params": [EVM_WALLET, "latest"]}]
    for index, (_symbol, _form, address, _decimals) in enumerate(watch):
        calls.append({"jsonrpc": "2.0", "id": f"t{index}", "method": "eth_call",
                      "params": [{"to": address, "data": data}, "latest"]})
    results: dict[str, dict] = {}
    for row in _rpc_rows(XLAYER_RPC, calls, chunk=10, pause=0.4):
        results[str(row.get("id"))] = row
    native = (results.get("gas") or {}).get("result")
    if not native:
        raise RuntimeError(f"eth_getBalance: {(results.get('gas') or {}).get('error')}")
    okb = Decimal(int(native, 16)) / Decimal(10) ** 18
    rows = [_hold_row("OKB", okb, "Gas", _gas_value("OKB", okb))]
    for index, (symbol, form, _address, decimals) in enumerate(watch):
        raw = (results.get(f"t{index}") or {}).get("result")
        if not raw or raw == "0x":
            continue
        amount = Decimal(int(raw, 16)) / Decimal(10) ** decimals
        if amount == 0:
            continue
        rows.append(_hold_row(
            symbol, amount, form,
            _value_of(symbol, amount, multipliers, scale_multiplier=False)))
    return {"at_ms": at, "error": None, "wallet": EVM_WALLET, "empty": 0, "rows": rows}


def ethereum_watch() -> list[tuple[str, str, str, int]]:
    """稳定币 + 当前 v4/route 监控里出现的 Ethereum xStock。"""
    watch = [(symbol, "稳定币", address, decimals)
             for symbol, (address, decimals) in ETHEREUM_STABLES.items()]
    symbols: set[str] = set()
    try:
        watch_payload = json.loads((ROOT / "data" / "xstocks_v4_watch.json").read_text())
        symbols.update(
            str(row.get("symbol"))
            for row in watch_payload.get("rows") or []
            if row.get("chain") == "eth" and row.get("symbol"))
    except (OSError, json.JSONDecodeError):
        pass
    try:
        matrix = json.loads((ROOT / "data" / "xstocks_route_matrix.json").read_text())
        symbols.update(
            str(row.get("symbol"))
            for row in matrix.get("routes") or []
            if row.get("chain") == "eth" and row.get("symbol"))
    except (OSError, json.JSONDecodeError):
        pass
    if symbols:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT issuer_symbol, form, address FROM deployments "
                "WHERE network = 'Ethereum' AND missing_since_ms IS NULL "
                f"AND issuer_symbol IN ({','.join('?' * len(symbols))})",
                tuple(sorted(symbols))).fetchall()
        finally:
            conn.close()
        watch.extend((symbol, "包装" if form.startswith("wrapper") else "原生", address, 18)
                     for symbol, form, address in rows)
    return watch


def ethereum_holdings(multipliers: dict[str, Decimal]) -> dict:
    at = int(time.time() * 1000)
    watch = ethereum_watch()
    data = "0x70a08231" + "0" * 24 + EVM_WALLET[2:].lower()
    calls = [{"jsonrpc": "2.0", "id": "gas", "method": "eth_getBalance",
              "params": [EVM_WALLET, "latest"]}]
    for index, (_symbol, _form, address, _decimals) in enumerate(watch):
        calls.append({"jsonrpc": "2.0", "id": f"t{index}", "method": "eth_call",
                      "params": [{"to": address, "data": data}, "latest"]})
    results: dict[str, dict] = {}
    for row in _rpc_rows(ETHEREUM_RPC, calls, chunk=10, pause=0.4):
        results[str(row.get("id"))] = row
    native = (results.get("gas") or {}).get("result")
    if not native:
        raise RuntimeError(f"eth_getBalance: {(results.get('gas') or {}).get('error')}")
    eth = Decimal(int(native, 16)) / Decimal(10) ** 18
    rows = [_hold_row("ETH", eth, "Gas", None)]
    for index, (symbol, form, _address, decimals) in enumerate(watch):
        raw = (results.get(f"t{index}") or {}).get("result")
        if not raw or raw == "0x":
            continue
        amount = Decimal(int(raw, 16)) / Decimal(10) ** decimals
        if amount == 0:
            continue
        rows.append(_hold_row(
            symbol, amount, form,
            _value_of(symbol, amount, multipliers, scale_multiplier=False)))
    return {"at_ms": at, "error": None, "wallet": EVM_WALLET, "empty": 0, "rows": rows}


def kraken_asset(asset: str) -> tuple[str, str]:
    """Kraken 余额键 →（显示名, 说明）。``MRNAx.T`` 是代币化 xStock。"""
    if asset.endswith(".T"):
        return asset[:-2], "xStock"
    if asset.startswith("Z") and len(asset) == 4:
        return asset[1:], "法币"
    if asset == "XBT":
        return "BTC", "加密"
    if asset in STABLE_SYMBOLS:
        return asset, "稳定币"
    return asset, "加密"


def kraken_holdings(multipliers: dict[str, Decimal]) -> dict:
    key, secret = load_credentials(ROOT / "config" / "kraken.env")
    client = KrakenSpotClient(key, secret, proxy=PROXY, timeout=30)
    balance = client.balance()
    at = int(time.time() * 1000)
    rows: list[dict] = []
    empty = 0
    for asset, amount in balance.items():
        if amount == 0:
            empty += 1
            continue
        name, note = kraken_asset(asset)
        rows.append(_hold_row(
            name, amount, note,
            _value_of(name, amount, multipliers, scale_multiplier=False)))
    return {"at_ms": at, "error": None, "wallet": None, "empty": empty, "rows": rows}


def _part_total(part: dict) -> str | None:
    total = Decimal(0)
    seen = False
    for row in part.get("rows") or []:
        if row.get("value") is None:
            continue
        total += Decimal(str(row["value"]))
        seen = True
    return f"{total:.2f}" if seen else None


def _refresh_holdings(key: str, fetch) -> None:
    try:
        part = fetch()
        part["total"] = _part_total(part)
    except Exception as exc:  # noqa: BLE001
        with _holdings_lock:
            old = HOLDINGS.get(key)
            part = dict(old) if old else {"at_ms": int(time.time() * 1000), "rows": []}
        part["error"] = f"{type(exc).__name__}: {exc}"
    with _holdings_lock:
        HOLDINGS[key] = part


def holdings_loop() -> None:
    """钱包与 Kraken 持仓的独立刷新线程。不参与询价，也不改任何状态。"""
    while True:
        try:
            multipliers = load_multipliers()
        except Exception as exc:  # noqa: BLE001
            print("holdings: multipliers failed", exc, flush=True)
            multipliers = {}
        with _holdings_lock:
            HOLDINGS["at_ms"] = int(time.time() * 1000)
        _refresh_holdings("solana", lambda: solana_holdings(multipliers))
        _refresh_holdings("xlayer", lambda: xlayer_holdings(multipliers))
        _refresh_holdings("ethereum", lambda: ethereum_holdings(multipliers))
        if time.time() - _kraken_cache["at"] > KRAKEN_BALANCE_TTL_S:
            _kraken_cache["at"] = time.time()
            _refresh_holdings("kraken", lambda: kraken_holdings(multipliers))
        time.sleep(HOLDINGS_TTL_S)


# ---------------------------------------------------------------------------
# 自动链状态（operator 2026-10-06）：读自动链写的 state + 账本尾，只渲染。
# 看板保持只读：不驱动自动链，也不改它的任何文件。
# ---------------------------------------------------------------------------
AUTO_STATE = ROOT / "data" / "xstocks_auto.json"
AUTO_LEDGER = ROOT / "data" / "xstocks_cycles.jsonl"
AUTO_LEDGER_TAIL = 20
_auto_cache: dict = {"at": 0.0, "payload": None}


def eth_ledger_tail(limit: int = 20) -> list[dict]:
    try:
        lines = ETH_LEDGER.read_text().splitlines()[-limit:]
    except OSError:
        return []
    rows: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(rows))


def auto_snapshot(force: bool = False) -> dict:
    now = time.time()
    if not force and _auto_cache["payload"] is not None and now - _auto_cache["at"] < 5:
        return _auto_cache["payload"]
    try:
        state = json.loads(AUTO_STATE.read_text())
    except Exception:  # noqa: BLE001
        state = {}
    rows: list[dict] = []
    try:
        with AUTO_LEDGER.open() as handle:
            lines = handle.readlines()[-AUTO_LEDGER_TAIL:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
    except FileNotFoundError:
        pass
    payload = {
        "running": bool(state.get("running")),
        "phase": state.get("phase") or "unknown",
        "mode": state.get("mode") or "off",
        "updated_ms": state.get("updated_ms"),
        "pause_reason": state.get("pause_reason") or "",
        "note": state.get("note") or "",
        "trades_today": state.get("trades_today") or 0,
        "opened_quote_usdt": state.get("opened_quote_usdt") or "0",
        "loss_streak": state.get("loss_streak") or 0,
        "balances": state.get("balances") or {},
        "last_review": state.get("last_review"),
        "last_cycle": state.get("last_cycle"),
        "ledger": list(reversed(rows)),
    }
    _auto_cache.update({"at": now, "payload": payload})
    return payload


def apply_sell_side(hit: dict, bids, *, size: Decimal, share: Decimal) -> None:
    """把「按 ``size`` 买入、按 Kraken 买盘逐档卖出」的净利写进这一行。

    ``bids`` 是 Kraken 深度快照（``[price, volume, timestamp]``）。盘口吃不下
    这个尺寸就不给净利（留空），由出口列说明原因——不拿别的价格凑一个数。
    """
    tokens = size / share if share > 0 else Decimal(0)
    if tokens <= 0:
        hit["net_usdt"] = None
        return
    plan = book_exit(bids, tokens)
    filled = Decimal(str(plan.get("filled") or "0"))
    vwap = plan.get("vwap")
    hit["sell_vwap"] = vwap
    hit["sell_slippage_bps"] = plan.get("slippage_bps")
    hit["sell_in_range_usd"] = plan.get("in_range_usd")
    if not vwap or filled < tokens:
        hit["net_usdt"] = None
        hit["sell_short"] = True
        return
    gross, net = edge_usdt(size, tokens, Decimal(str(vwap)), symbol=str(hit.get("symbol") or ""))
    hit["gross_usdt"] = _money(gross)
    hit["net_usdt"] = _money(net)
    hit["net_positive"] = net > 0


def apply_exit_gate(hits: list[dict], route_by_symbol: dict | None = None) -> list[dict]:
    """Filter to rows whose exit can be shown to work, then probe their books."""
    global _exit_rejects
    pairs = kraken_pairs_live()
    deposits = kraken_deposits()
    bridges = kraken_bridge_symbols()
    rejects: list[dict] = []
    keep: list[dict] = []

    for hit in hits:
        symbol = str(hit.get("symbol") or "")
        source = str(hit.get("network") or "")
        row = pairs.get(symbol)
        if row is None:
            rejects.append({"symbol": symbol, "reason": "Kraken 没有这个交易对"})
            continue
        if row["status"] not in ("online", "post_only"):
            rejects.append({"symbol": symbol,
                            "reason": f"Kraken 状态 {row['status'] or '未知'}"})
            continue
        ok, reason = reachable(symbol, source_network=source,
                               deposits=deposits, bridge_symbols=bridges)
        if not ok:
            rejects.append({"symbol": symbol, "reason": reason})
            continue
        hit["kraken"] = "可吃单" if row["status"] == "online" else "只可挂单"
        hit["deposit_note"] = reason
        hit["ordermin"] = row["ordermin"]
        keep.append(hit)

    # 按「对 Kraken 买一便宜多少」排序后只探前 N 行。**并行**探测（operator 2026-10-05）：
    # 这些 Depth 调用与链上询价没有依赖，串行只是白等。
    keep.sort(key=lambda item: Decimal(item.get("gap_bps") or "0"))
    probe, rest = keep[:EXIT_PROBE_ROWS], keep[EXIT_PROBE_ROWS:]
    for hit in rest:
        hit["exit_text"] = "未探测（本轮额度）"
        hit["exit_ok"] = None

    def _probe_exit(hit: dict) -> None:
        symbol = str(hit["symbol"])
        bids = kraken_bids(symbol)
        share = Decimal(str(hit.get("share") or "0"))
        planned = Decimal(str(hit.get("size_planned") or "0"))
        spend = Decimal(str(hit.get("spend_usd") or "0"))
        # Kraken 收的枚数就是 size / 成交每股（= 链上到手数量 × 乘数）。
        tokens = planned / share if share > 0 else Decimal(0)
        plan = book_exit(bids, tokens, max_slip_bps=MAX_EXIT_SLIP_BPS)
        hit["exit_ok"] = plan["ok"]
        if plan["ok"]:
            hit["exit_text"] = (f"买一 {plan['best_bid']} · 均价 {plan['vwap']}"
                                f" · 滑点 {plan['slippage_bps']} bps"
                                f" · 50bps 内 ${plan['in_range_usd']}")
        else:
            hit["exit_text"] = f"✗ {plan['reason']}"
        # 净利：按板上尺寸买入 → 按 Kraken 买盘逐档卖出（真实卖价）。
        apply_sell_side(hit, bids, size=spend, share=share)
        if plan["ok"] and plan["vwap"] and planned > 0:
            _gross, net = edge_usdt(planned, tokens, Decimal(str(plan["vwap"])),
                                    symbol=str(hit.get("symbol") or ""))
            hit["net_planned"] = _money(net)

    if probe:
        with ThreadPoolExecutor(max_workers=EXIT_PROBE_WORKERS) as pool:
            list(pool.map(_probe_exit, probe))

    # 出口过不了的行直接不显示（operator 明确要求）。
    survivors = [hit for hit in probe if hit.get("exit_ok")] + rest
    for hit in probe:
        if not hit.get("exit_ok"):
            rejects.append({"symbol": hit["symbol"],
                            "reason": f"出口不可执行：{hit.get('exit_text')}"})

    with _state_lock:
        _exit_rejects = rejects[:60]
        _exit_notes.update({
            "kraken_pairs": len(pairs),
            "kraken_online": sum(1 for r in pairs.values() if r["status"] == "online"),
            "deposit_cached": len(deposits),
            "rejected": len(rejects),
        })
    return survivors


def plan_trade_size(hits: list[dict]) -> None:
    """定这一笔做多大，**不再单独询一次价**（operator 2026-10-05）。

    报价阶梯本来就是带着数量去问的：某一档还便宜，就说明这个尺寸吃得住；
    下一档不便宜，就说明吃不住。所以

        size = min(单笔上限 10 USDT, 阶梯上「仍便宜」的最大档)

    ——买 1 U 就推动价格的名字最多买 1 U；买 100 U 都不怎么动价的名字才买 10 U。
    成本用该档自己的成交价（``share``）算，不拿小档的价格去推大档。
    """
    for hit in hits:
        if hit.get("_fail"):
            continue
        try:
            spend = Decimal(str(hit.get("spend_usd") or "0"))
        except Exception:  # noqa: BLE001
            spend = Decimal(0)
        size = min(TRADE_SIZE_USD, spend) if spend > 0 else Decimal(0)
        hit["size_planned"] = _money(size)
        hit["size_verified"] = size > 0
        try:
            share = Decimal(str(hit.get("share")))
        except Exception:  # noqa: BLE001
            share = Decimal(0)
        # 净利要用 Kraken 真实买盘，等出口闸门那一步拿到 Depth 再算。
        hit["net_planned"] = None
        if share > 0:
            hit["tokens_planned"] = format(size / share, "f")
        hit["risk_planned"] = hit.get("risk")


def mark_executable(hits: list[dict]) -> None:
    """可执行 = 出口成立 + 按计划尺寸净利 > 0.1 + 风险低。"""
    for hit in hits:
        try:
            net = Decimal(str(hit.get("net_planned") or "0"))
        except Exception:  # noqa: BLE001
            net = Decimal(0)
        risk = hit.get("risk_planned") or hit.get("risk")
        reasons = []
        if hit.get("exit_ok") is not True:
            reasons.append("出口未验证")
        if not hit.get("size_verified"):
            reasons.append("尺寸未确认")
        if net <= Decimal("0.1"):
            reasons.append("净利≤0.1")
        if risk != "低":
            reasons.append(f"风险{risk or '未知'}")
        hit["executable"] = not reasons
        hit["blocked_by"] = "、".join(reasons)


def classify(share: Decimal, ref: Decimal) -> Decimal | None:
    """Return the gap in bps when a quoted share is still cheap enough to show.

    A quote more than 20x away from the reference is rejected. Gaps tighter
    than 10 bps are treated as noise and also rejected.
    """
    if share <= 0 or ref <= 0:
        return None
    if share * 20 < ref or share > ref * 20:
        return None
    gap = (share - ref) / ref * Decimal(10000)
    if gap > GAP_LIMIT:
        return None
    return gap


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))


def _get(url: str, timeout: int = 15) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url, headers={"User-Agent": "xstocks-gap-live/1", "Accept": "application/json"})
    try:
        with _opener().open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _fetch(url: str, extra: dict[str, str], timeout: int) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(url, headers={
        "User-Agent": "xstocks-gap-live/1", "Accept": "application/json", **extra})
    try:
        with _opener().open(request, timeout=timeout) as response:
            return response.status, response.read(), {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), {k.lower(): v for k, v in (exc.headers or {}).items()}


def _read_env(path: Path, name: str) -> str:
    if not path.exists():
        return ""
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            return value.strip().strip("'\"")
    return ""


def jupiter_config() -> tuple[str, str, float]:
    """Return base URL, API key and requests per second for Jupiter quotes."""
    key = os.environ.get("JUPITER_API_KEY", "").strip() or _read_env(JUP_ENV, "JUPITER_API_KEY")
    rps_text = os.environ.get("JUPITER_RPS", "").strip() or _read_env(JUP_ENV, "JUPITER_RPS")
    if key:
        base, rps = "https://api.jup.ag", JUP_KEY_RPS
    else:
        base, rps = "https://lite-api.jup.ag", JUP_LITE_RPS
    if rps_text:
        try:
            rps = float(rps_text)
        except ValueError:
            pass
    return base, key, max(0.1, rps)


class RateLimited(Exception):
    """Jupiter answered 429. The pacer has already paused every worker."""


# Jupiter answers these when the mint has no buyable route at all. They do not
# change from one pass to the next, so re-asking every pass only burns budget.
NO_ROUTE_CODES = {"NO_ROUTES_FOUND", "TOKEN_NOT_TRADABLE", "COULD_NOT_FIND_ANY_ROUTE"}
NO_ROUTE_RECHECK_S = 1800


class NoRoute(Exception):
    """Jupiter has no route for this mint."""


class Pacer:
    """One request budget shared by every quote thread.

    A 429 pauses all workers until the window frees and halves the rate once
    per incident. Retrying at full speed keeps the window full, which is how
    the board ended up with every quote failing.
    """

    def __init__(self, rps: float, headroom: float = JUP_HEADROOM,
                 clock=time.monotonic, wall=time.time, sleep=time.sleep) -> None:
        self.ceiling = rps * headroom
        self.rate = self.ceiling
        self.limited = 0
        self._next = 0.0
        self._paused_until = 0.0
        self._ok_streak = 0
        self._lock = threading.Lock()
        self._clock, self._wall, self._sleep = clock, wall, sleep

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            start = max(now, self._next, self._paused_until)
            self._next = start + 1 / self.rate
        if start > now:
            self._sleep(start - now)

    def _until_reset(self, headers: dict[str, str]) -> float:
        try:
            wait = float(headers["x-ratelimit-reset"]) - self._wall()
        except (KeyError, TypeError, ValueError):
            return JUP_PAUSE_S
        return min(max(wait, 1.0), JUP_PAUSE_S)

    def observe(self, status: int, headers: dict[str, str]) -> None:
        with self._lock:
            now = self._clock()
            if status == 429:
                self.limited += 1
                self._ok_streak = 0
                if now >= self._paused_until:
                    self.rate = max(self.ceiling / 8, self.rate / 2)
                self._paused_until = max(self._paused_until, now + self._until_reset(headers))
                return
            if status != 200:
                return
            try:
                remaining = int(headers.get("x-ratelimit-remaining", ""))
            except ValueError:
                remaining = None
            if remaining is not None and remaining <= 0:
                self._paused_until = max(self._paused_until, now + self._until_reset(headers))
            self._ok_streak += 1
            if self._ok_streak >= 30 and self.rate < self.ceiling:
                self.rate = min(self.ceiling, self.rate * 1.25)
                self._ok_streak = 0


_jup_base, _jup_key, _jup_rps = jupiter_config()
_pacer = Pacer(_jup_rps)
_fail_lock = threading.Lock()
_fail_reasons: dict[str, int] = {}
_no_route_until: dict[str, float] = {}


def _note_fail(reason: str) -> None:
    with _fail_lock:
        _fail_reasons[reason] = _fail_reasons.get(reason, 0) + 1


def _take_fail_reasons() -> dict[str, int]:
    with _fail_lock:
        out = dict(_fail_reasons)
        _fail_reasons.clear()
    return out


def _jupiter_error(status: int, body: bytes) -> str:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return f"http_{status}"
    if isinstance(payload, dict):
        code = payload.get("errorCode") or payload.get("error_code") or payload.get("code")
        if code:
            return f"http_{status}:{code}"
    return f"http_{status}"


def _money(value: Decimal) -> str:
    text = format(value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), "f")
    return text.rstrip("0").rstrip(".")


def _gap_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP), "f")


_sol_usd: Decimal = Decimal("0")


def gap_vs(share: Decimal, other: Decimal) -> Decimal:
    """相对某个价的价差（bps）。负数 = 比这个价便宜。"""
    return (share - other) / other * Decimal(10000)


def edge_usdt(size: Decimal, tokens: Decimal, sell_price: Decimal, *,
              symbol: str = "") -> tuple[Decimal, Decimal]:
    """毛套利与扣费净利（USDT）：链上买入价 vs Kraken 实际卖价。

    ``size``       链上花出去的 USDT（报价阶梯里这一档的金额）。
    ``tokens``     这一档到手、并且 Kraken 能收的枚数（= size / 成交每股）。
    ``sell_price`` Kraken 买盘逐档走出来的成交均价（USDT/枚）。

    只算买入这一侧：扣 Kraken 费率 ``EXIT_COST_BPS``、链上买入网络费、
    以及转到 Kraken 的转账费（首次含替 Kraken 建账户，按标的查
    ``data/xstocks_transfer_seen.json``）。我们自己的账户租金可退，不进这个数。
    """
    proceeds = tokens * sell_price
    gross = proceeds - size
    chain = chain_fee_sol(symbol) if symbol else (BUY_FEE_SOL + TRANSFER_FEE_FIRST_SOL)
    fees = proceeds * EXIT_COST_BPS / Decimal(10000) + chain * _sol_usd
    return gross, gross - fees


def risk_label(impact_pct: Decimal | None) -> str:
    """池子风险按「买入这侧高出中价的成本」分档。

    只关心买入：这个数越大，说明池子越薄、这笔买入本身就越贵，
    也说明同样的钱在更小的尺寸上才能成交。
    """
    if impact_pct is None:
        return "—"
    if impact_pct >= 5:
        return "高"
    if impact_pct >= 2:
        return "中"
    return "低"


def load_solana() -> list[dict]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT d.issuer_symbol AS symbol, d.address AS address,
               u.underlying_symbol AS ticker, s.status AS status
        FROM deployments d
        JOIN underlyings u ON u.issuer_symbol = d.issuer_symbol
        JOIN spot_exits s ON s.issuer_symbol = d.issuer_symbol
             AND s.missing_since_ms IS NULL
        WHERE d.missing_since_ms IS NULL
          AND d.network = 'Solana' AND d.form = 'native'
        """
    ).fetchall()
    conn.close()
    found: dict[str, dict] = {}
    for row in rows:
        symbol = row["symbol"]
        status = row["status"] or ""
        current = found.get(symbol)
        if current is None or status == "online":
            found[symbol] = {
                "symbol": symbol,
                "ticker": row["ticker"],
                "address": row["address"],
                "network": "Solana",
                "form": "原生",
                "kraken": "可吃单" if status == "online" else (
                    "只可挂单" if status == "post_only" else (status or "未知")),
                "via": "Jupiter",
            }
    return list(found.values())


def load_multipliers() -> dict[str, Decimal]:
    raw = json.loads(MULT.read_text())
    out: dict[str, Decimal] = {}
    for symbol, row in raw.items():
        try:
            out[symbol] = Decimal(str((row or {}).get("current") or "1"))
        except Exception:
            out[symbol] = Decimal(1)
    return out


def yahoo_symbol(ticker: str) -> str:
    """Yahoo and FMP both use a hyphen for class shares such as BRK.B."""
    return ticker.replace(".", "-")


def fmp_key() -> str:
    env = os.environ.get("FMP_API_KEY", "").strip()
    if env:
        return env
    if not FMP_ENV.exists():
        return ""
    for line in FMP_ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "FMP_API_KEY":
            return value.strip().strip("'\"")
    return ""


def parse_fmp_batch(payload: object, alias: dict[str, str]) -> dict[str, Decimal]:
    if not isinstance(payload, list):
        return {}
    out: dict[str, Decimal] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        ticker = alias.get(str(row.get("symbol") or ""))
        if not ticker:
            continue
        try:
            price = Decimal(str(row.get("price")))
        except Exception:
            continue
        if price <= 0:
            continue
        out[ticker] = price
    return out


def price_is_fresh(fetched: float, kind: str, now: float, batch_blocked: bool) -> bool:
    ttl = FMP_FREE_TTL_S if batch_blocked and str(kind).startswith("FMP") else STOCK_TTL_S
    return now - fetched < ttl


def _utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def load_fmp_usage() -> dict:
    usage = {"day": _utc_day(), "calls": 0, "batch": True}
    if not FMP_USAGE.exists():
        return usage
    try:
        raw = json.loads(FMP_USAGE.read_text())
    except (OSError, json.JSONDecodeError):
        return usage
    if isinstance(raw, dict):
        usage.update({k: raw[k] for k in ("day", "calls", "batch") if k in raw})
    if usage.get("day") != _utc_day():
        usage["day"] = _utc_day()
        usage["calls"] = 0
    return usage


def save_fmp_usage(usage: dict) -> None:
    FMP_USAGE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FMP_USAGE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(usage))
    tmp.replace(FMP_USAGE)


def fmp_budget_ok(usage: dict) -> bool:
    """Count one call. False means today's free budget is already spent."""
    if usage.get("day") != _utc_day():
        usage["day"] = _utc_day()
        usage["calls"] = 0
    if int(usage.get("calls") or 0) >= FMP_DAILY_STOP:
        return False
    usage["calls"] = int(usage.get("calls") or 0) + 1
    save_fmp_usage(usage)
    return True


def fmp_quotes(tickers: list[str], key: str) -> dict[str, Decimal]:
    global _fmp_batch_blocked
    alias = {yahoo_symbol(ticker): ticker for ticker in tickers}
    symbols = ",".join(alias)
    url = (
        "https://financialmodelingprep.com/stable/batch-quote"
        f"?symbols={urllib.request.quote(symbols, safe=',')}&apikey={urllib.request.quote(key)}"
    )
    status, body = _get(url, timeout=20)
    if status == 402:
        _fmp_batch_blocked = True
        return {}
    if status != 200:
        return {}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return parse_fmp_batch(payload, alias)


def fmp_quote_one(ticker: str, key: str) -> Decimal | None:
    symbol = yahoo_symbol(ticker)
    url = (
        "https://financialmodelingprep.com/stable/quote"
        f"?symbol={urllib.request.quote(symbol)}&apikey={urllib.request.quote(key)}"
    )
    status, body = _get(url, timeout=20)
    if status != 200:
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    return parse_fmp_batch(payload, {symbol: ticker}).get(ticker)


def load_stock_cache() -> dict[str, tuple[Decimal, float, str]]:
    if not STOCK_CACHE.exists():
        return {}
    try:
        raw = json.loads(STOCK_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, tuple[Decimal, float, str]] = {}
    if not isinstance(raw, dict):
        return out
    for ticker, row in raw.items():
        if not isinstance(row, dict):
            continue
        try:
            price = Decimal(str(row.get("price")))
            fetched = float(row.get("at") or 0)
        except Exception:
            continue
        if price <= 0:
            continue
        out[str(ticker)] = (price, fetched, str(row.get("kind") or "正股现价"))
    return out


def save_stock_cache(cache: dict[str, tuple[Decimal, float, str]]) -> None:
    payload = {
        ticker: {"price": format(price, "f"), "at": fetched, "kind": kind}
        for ticker, (price, fetched, kind) in cache.items()
    }
    STOCK_CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STOCK_CACHE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False))
    tmp.replace(STOCK_CACHE)


_fmp_not_before = 0.0
_fmp_batch_blocked = False


def refresh_stock_prices(tickers: list[str], cache: dict[str, tuple[Decimal, float, str]]) -> None:
    """Fill cache with real equity prices.

    Paid plans use one batch-quote call. The free plan returns HTTP 402 for
    that endpoint, so those keys use /stable/quote one symbol at a time.
    """
    global _fmp_not_before, _fmp_batch_blocked
    now = time.time()
    usage = load_fmp_usage()
    if usage.get("batch") is False:
        _fmp_batch_blocked = True
    unique = list(dict.fromkeys(tickers))
    # Names with no price yet come first, so a slow fill cannot starve the tail.
    missing = [ticker for ticker in unique if ticker not in cache]
    stale = [
        ticker for ticker in unique
        if ticker in cache and not price_is_fresh(cache[ticker][1], cache[ticker][2], now, _fmp_batch_blocked)
    ]
    due = missing + stale
    if not due:
        return
    missed = due
    key = fmp_key()
    if key and not _fmp_batch_blocked and now >= _fmp_not_before and fmp_budget_ok(usage):
        got = fmp_quotes(due, key)
        if _fmp_batch_blocked:
            usage["batch"] = False
            save_fmp_usage(usage)
        else:
            _fmp_not_before = now + STOCK_TTL_S
            for ticker, price in got.items():
                cache[ticker] = (price, now, "FMP 正股")
            missed = [ticker for ticker in due if ticker not in got]
    if key and _fmp_batch_blocked:
        failed: list[str] = []
        fetched = 0
        deferred: list[str] = []
        for ticker in missed:
            if fetched >= FMP_FREE_PER_CYCLE:
                deferred.append(ticker)
                continue
            if not fmp_budget_ok(usage):
                deferred.append(ticker)
                continue
            fetched += 1
            price = fmp_quote_one(ticker, key)
            if price is None:
                failed.append(ticker)
            else:
                cache[ticker] = (price, now, "FMP 正股")
        missed = failed
        # Deferred names stay due next cycle. Do not spend Yahoo on them.
        del deferred
    filled = 0
    for ticker in missed:
        if filled >= YAHOO_PER_CYCLE:
            break
        price = yahoo_price(ticker)
        filled += 1
        if price is not None:
            cache[ticker] = (price, now, "雅虎正股")
    save_stock_cache(cache)


def yahoo_price(ticker: str) -> Decimal | None:
    status, body = _get(
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.request.quote(yahoo_symbol(ticker)) + "?range=5d&interval=1d",
        timeout=15,
    )
    if status != 200:
        return None
    result = ((json.loads(body).get("chart") or {}).get("result") or [None])[0] or {}
    regular = (result.get("meta") or {}).get("regularMarketPrice")
    if regular in (None, 0, "0"):
        return None
    return Decimal(str(regular))


def parse_stock_refs(payload: object, mint_to_ticker: dict[str, str]) -> dict[str, Decimal]:
    """Map Price V3 rows to ticker -> stockData.price. Missing or zero is skipped."""
    out: dict[str, Decimal] = {}
    if not isinstance(payload, dict):
        return out
    for mint, row in payload.items():
        ticker = mint_to_ticker.get(mint)
        if not ticker or not isinstance(row, dict):
            continue
        try:
            price = Decimal(str((row.get("stockData") or {}).get("price")))
        except Exception:
            continue
        if price > 0:
            out[ticker] = price
    return out


def price_v3(mints: list[str]) -> dict:
    """Price V3 in batches of 50. A failed batch is left out, not retried."""
    out: dict = {}
    for i in range(0, len(mints), PRICE_V3_BATCH):
        url = f"{_jup_base}/price/v3?ids=" + ",".join(mints[i:i + PRICE_V3_BATCH])
        _pacer.wait()
        try:
            status, body, headers = _fetch(url, {"x-api-key": _jup_key} if _jup_key else {}, timeout=20)
        except Exception as exc:
            _note_fail("price_v3:" + type(exc).__name__)
            continue
        _pacer.observe(status, headers)
        if status != 200:
            _note_fail(f"price_v3:http_{status}")
            continue
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            _note_fail("price_v3:bad_payload")
            continue
        if isinstance(payload, dict):
            out.update(payload)
    return out


def parse_yahoo_last(payload: object) -> Decimal | None:
    """Last 1-minute close including pre/post market, else regularMarketPrice."""
    try:
        result = ((payload.get("chart") or {}).get("result") or [None])[0] or {}
    except AttributeError:
        return None
    closes = (((result.get("indicators") or {}).get("quote") or [{}])[0] or {}).get("close") or []
    for value in reversed(closes):
        if value not in (None, 0):
            return Decimal(str(value))
    regular = (result.get("meta") or {}).get("regularMarketPrice")
    if regular in (None, 0, "0"):
        return None
    return Decimal(str(regular))


def yahoo_last(ticker: str) -> Decimal | None:
    status, body = _get(
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.request.quote(yahoo_symbol(ticker)) + "?range=1d&interval=1m&includePrePost=true",
        timeout=15,
    )
    if status != 200:
        return None
    try:
        return parse_yahoo_last(json.loads(body))
    except json.JSONDecodeError:
        return None


def ref_deviation(ours: Decimal, other: Decimal) -> Decimal:
    return abs(ours / other - 1)


_stock_refs: dict[str, tuple[Decimal, float]] = {}
_ref_alarms: dict[str, dict] = {}
_ref_checks: list[dict] = []
_ref_cursor = 0


def cross_check_refs(tickers: list[str], fetch=yahoo_last) -> None:
    """Re-check a rotating few Jupiter references against an independent last trade."""
    global _ref_cursor
    names = sorted(set(tickers))
    if not names:
        return
    for _ in range(min(REF_CHECK_PER_CYCLE, len(names))):
        ticker = names[_ref_cursor % len(names)]
        _ref_cursor += 1
        ours = _stock_refs.get(ticker)
        if ours is None:
            continue
        try:
            other = fetch(ticker)
        except Exception:
            other = None
        entry = {"ticker": ticker, "jupiter": format(ours[0], "f"),
                 "at": datetime.now().astimezone().isoformat(timespec="seconds")}
        if other is None or other <= 0:
            entry["result"] = "雅虎无数据，未判断"
        else:
            dev = ref_deviation(ours[0], other)
            entry.update({"yahoo": format(other, "f"), "dev_pct": _money(dev * 100)})
            if dev > REF_ALARM:
                entry["result"] = "对照存疑"
                _ref_alarms[ticker] = entry
            else:
                entry["result"] = "一致"
                _ref_alarms.pop(ticker, None)
        _ref_checks.append(entry)
    del _ref_checks[:-30]


def jupiter(mint: str, usdc_raw: int) -> tuple[Decimal, Decimal, str, Decimal] | None:
    """买入腿报价。返回 (花的 USDC, 拿到的枚数, 路由, 价格冲击%)。

    只看买入这一侧：我们的路径是链上买入 → 充值 Kraken → 卖出，
    不存在买回同一口池的动作，所以不需要卖出腿报价。
    """
    url = (
        f"{_jup_base}/swap/v1/quote"
        f"?inputMint={SOL_USDC}&outputMint={mint}&amount={usdc_raw}"
        f"&slippageBps={SLIPPAGE_BPS}"
    )
    _pacer.wait()
    status, body, headers = _fetch(url, {"x-api-key": _jup_key} if _jup_key else {}, timeout=12)
    _pacer.observe(status, headers)
    if status == 429:
        raise RateLimited()
    if status != 200:
        reason = _jupiter_error(status, body)
        _note_fail(reason)
        if reason.split(":", 1)[-1] in NO_ROUTE_CODES:
            raise NoRoute(reason)
        return None
    payload = json.loads(body)
    try:
        spent = Decimal(payload["inAmount"]) / Decimal(10) ** 6
        got = Decimal(payload["outAmount"]) / Decimal(10) ** SOL_DECIMALS
    except (KeyError, TypeError, ValueError):
        _note_fail("bad_payload")
        return None
    if spent <= 0 or got <= 0:
        _note_fail("zero_amount")
        return None
    labels = []
    for step in payload.get("routePlan") or []:
        label = str((step.get("swapInfo") or {}).get("label") or "")
        if label and label not in labels:
            labels.append(label)
    try:
        impact = Decimal(str(payload.get("priceImpactPct") or "0")) * 100
    except Exception:  # noqa: BLE001
        impact = Decimal("0")
    return spent, got, " → ".join(labels[:3]) or "Jupiter", impact


def sell_anchor(sell: Decimal) -> Decimal:
    """把 Kraken 买一折成「有效卖价」：扣掉我们要付的 Kraken 费率。"""
    return sell * (Decimal(1) - EXIT_COST_BPS / Decimal(10000))


def _impact_of(raw) -> Decimal | None:
    try:
        return None if raw in (None, "—", "") else Decimal(str(raw))
    except Exception:  # noqa: BLE001
        return None


def _near_miss(row: dict, gap_eff: Decimal) -> dict:
    """标记成「近失」：比 Kraken 有效卖价还贵，但差得不多，列出来让人看见。"""
    row.update({
        "near_miss": True,
        "near_gap_bps": _gap_text(gap_eff),
        "net_usdt": None,
        "net_planned": None,
        "net_positive": False,
        "executable": False,
        "blocked_by": (
            f"对 Kraken 有效卖价还贵 {_gap_text(gap_eff)} bps" if gap_eff > 0
            else f"比 Kraken 有效卖价只便宜 {_gap_text(-gap_eff)} bps，不够 10 bps 缓冲"),
        "exit_ok": None,
        "exit_text": None,
        "deposit_note": "",
        "risk": risk_label(_impact_of(row.get("impact_pct"))),
    })
    return row


def quote_solana(row: dict, ref: Decimal, mult: Decimal, ref_kind: str,
                 sell: Decimal) -> tuple[str, dict | None]:
    """Return hit, near, miss, fail, limited or no_route.

    口径（operator 2026-10-06）：能不能做，用链上买入价对 Kraken 买盘 ``sell``
    ——我们是真的在 Kraken 卖出；正股价 ``ref`` 只填参考列，不参与判断。
    A failed first quote is not treated as expensive.
    """
    anchor = sell_anchor(sell)
    winner = None
    first = None
    breaks = ""
    breaks_unknown = False
    route = ""
    saw_quote = False
    for size in SIZES:
        try:
            quoted = jupiter(row["address"], int(size * Decimal(10) ** 6))
        except RateLimited:
            if winner is None:
                return "limited", None
            breaks = _money(size)
            breaks_unknown = True
            break
        except NoRoute:
            if not saw_quote:
                return "no_route", None
            quoted = None
        if quoted is None:
            if not saw_quote:
                return "fail", None
            breaks = _money(size)
            break
        saw_quote = True
        spent, got, route, impact = quoted
        share = (spent / got) / mult
        if first is None:
            first = (size, share, route, got, impact)
        gap = classify(share, anchor)
        if gap is None:
            breaks = _money(size)
            break
        winner = (size, share, gap, route, got, impact)
    if winner is None:
        if first is None:
            return "miss", None
        size, share, route, got, impact = first
        gap_eff = gap_vs(share, anchor)
        if gap_eff > NEAR_MISS_BPS:
            return "miss", None
        return "near", _near_miss({
            "symbol": row["symbol"], "ticker": row["ticker"], "network": "Solana",
            "form": row["form"], "address": row["address"],
            "share": _money(share), "ref": _money(ref), "ref_kind": ref_kind,
            "sell": _money(sell), "gap_bps": _gap_text(gap_vs(share, sell)),
            "spend_usd": _money(size), "tokens": format(got, "f"),
            "impact_pct": _money(impact),
            "breaks_usd": "", "breaks_unknown": False,
            "pool": route, "kraken": row["kraken"], "via": "Jupiter",
            "url": f"https://jup.ag/tokens/{row['address']}", "frozen": False,
        }, gap_eff)
    size, share, gap, route, got, impact = winner
    return "hit", {
        "symbol": row["symbol"], "ticker": row["ticker"], "network": "Solana",
        "form": row["form"], "address": row["address"],
        "share": _money(share), "ref": _money(ref), "ref_kind": ref_kind,
        "sell": _money(sell), "gap_bps": _gap_text(gap_vs(share, sell)),
        "spend_usd": _money(size),
        # 卖出腿要按「枚数」拿回来报价，不能从每股价反推（乘数会丢）。
        "tokens": format(got, "f"),
        "impact_pct": _money(impact),
        "breaks_usd": breaks, "breaks_unknown": breaks_unknown,
        "pool": route, "kraken": row["kraken"], "via": "Jupiter",
        # 路径列可点：Jupiter 的代币页给出价、标记价、折扣和流动性。
        "url": f"https://jup.ag/tokens/{row['address']}",
        "frozen": False,
    }


def _import_requote():
    import importlib.util
    spec = importlib.util.spec_from_file_location("rq", ROOT / "scripts" / "xstocks_requote.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve_evm(route: dict, rq) -> tuple[dict | None, str]:
    """Look the pool up on GeckoTerminal once. Returns (route, "") or (None, why)."""
    url = (
        f"https://api.geckoterminal.com/api/v2/networks/{route['slug']}/tokens/"
        f"{route['token']}/pools?include=base_token,quote_token"
    )
    try:
        status, body = rq.http_get(url)
    except Exception as exc:
        return None, "gecko " + type(exc).__name__
    if status != 200:
        return None, f"gecko http_{status}"
    wanted = route["pool_name"].lower()
    for pool in json.loads(body).get("data") or []:
        attr = pool.get("attributes") or {}
        name = str(attr.get("name") or "")
        if wanted not in name.lower():
            continue
        rel = pool.get("relationships") or {}
        dex = ((rel.get("dex") or {}).get("data") or {}).get("id") or ""
        if "v4" in dex.lower():
            continue
        fee = rq.parse_fee(name, dex)
        price, stable_sym, stable_addr, _side = rq.our_price(pool, route["token"])
        if fee is None or price is None or not stable_addr.startswith("0x"):
            continue
        return {
            **route,
            "pool_address": str(attr.get("address") or ""),
            "pool": name,
            "fee": fee,
            "stable": stable_addr,
            "stable_symbol": stable_sym,
            "v2": "v2" in dex.lower(),
            "dec_stable": rq.decimals_of(route["slug"], stable_addr),
            "dec_token": rq.decimals_of(route["slug"], route["token"]),
        }, ""
    return None, "gecko 里没有这口池"


def load_evm_cache() -> dict[str, dict]:
    """Resolved pool facts (address, fee, tokens, decimals) never change; keep them."""
    if not EVM_CACHE.exists():
        return {}
    try:
        raw = json.loads(EVM_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, dict] = {}
    for symbol, row in (raw.items() if isinstance(raw, dict) else []):
        try:
            out[symbol] = {**row, "fee": Decimal(str(row["fee"]))}
        except Exception:
            continue
    return out


def save_evm_cache(routes: list[dict]) -> None:
    payload = {route["symbol"]: {**route, "fee": format(route["fee"], "f")} for route in routes}
    tmp = EVM_CACHE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False))
    tmp.replace(EVM_CACHE)


def cached_route(route: dict, cache: dict[str, dict]) -> dict | None:
    hit = cache.get(route["symbol"])
    if hit and hit.get("token") == route["token"] and hit.get("pool_name") == route["pool_name"]:
        return hit
    return None


STONFI_QUOTE = "https://api.ston.fi/v1/swap/simulate"
SLOT0_SELECTOR = "0x3850c7bd"
LIQUIDITY_SELECTOR = "0x1a686502"
TOKEN0_SELECTOR = "0x0dfe1681"


def _import_screen():
    import importlib.util
    path = ROOT / "src" / "arb_monitor" / "strategy" / "xstocks_screen.py"
    spec = importlib.util.spec_from_file_location("xstocks_screen", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def slot0_pool_state(route: dict, rq) -> dict | None:
    """Read slot0 / liquidity / token0 once for a pool. No quoter needed.

    Ink 的 Reservoir v3 没有可用的 Uniswap QuoterV2，任何 v3 池都可以用
    slot0 与当前区间流动性算出成交均价。结果只覆盖当前 tick 区间。
    """
    pool_address = route["pool_address"]
    slot0 = rq.rpc(route["slug"], pool_address, SLOT0_SELECTOR)
    liquidity = rq.rpc(route["slug"], pool_address, LIQUIDITY_SELECTOR)
    token0 = rq.rpc(route["slug"], pool_address, TOKEN0_SELECTOR)
    if not slot0 or not liquidity or not token0 or len(slot0) < 66:
        return None
    try:
        sqrt_price = Decimal(int(slot0[2:66], 16))
        liq = Decimal(int(liquidity, 16))
    except (ValueError, InvalidOperation):
        return None
    t0 = "0x" + token0[-40:]
    return {
        "address": pool_address,
        "base_address": route["token"],
        "base_decimals": int(route["dec_token"]),
        "quote_address": route["stable"],
        "quote_decimals": int(route["dec_stable"]),
        "quote_symbol": route.get("stable_symbol") or "",
        "fee": int(Decimal(str(route["fee"])) * Decimal(1_000_000)),
        "token0": t0,
        "token1": route["stable"] if t0.lower() == route["token"].lower() else route["token"],
        "sqrt_price_x96": sqrt_price,
        "liquidity": liq,
    }


def slot0_share(pool: dict, size: Decimal, screen=None) -> Decimal | None:
    if screen is None:
        screen = _import_screen()
    out = screen.pool_side_prices(pool, size)
    if out.get("error") or not out.get("within_active_liquidity"):
        return None
    return out.get("ask_usd")


def quote_ton(route: dict, amount_raw: int, timeout: int = 20) -> int | None:
    """Buy quote on TON through the ston.fi REST API. Returns ask units."""
    # POST，但参数在查询串里（接口就是这么定义的：body 会被拒）。
    url = (
        f"{STONFI_QUOTE}?offer_address={route['stable']}&ask_address={route['token']}"
        f"&units={amount_raw}&slippage_tolerance=0.01"
    )
    request = urllib.request.Request(url, data=b"", method="POST",
                                     headers={"Accept": "application/json",
                                              # 不带 UA 会被 Cloudflare 挡成 403。
                                              "User-Agent": "arb-monitor/0.1"})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except Exception:
        return None
    try:
        return int(payload["ask_units"])
    except (KeyError, TypeError, ValueError):
        return None


def load_census() -> tuple[list[dict], dict, str]:
    """Read the multi-chain census: (routes, per-chain summary, timestamp).

    A census file that is missing or unreadable is not fatal: the board keeps
    quoting the hand-checked routes it has. The chain strip then says so.
    """
    if not CENSUS.exists():
        return [], {}, ""
    try:
        payload = json.loads(CENSUS.read_text())
    except (OSError, json.JSONDecodeError):
        return [], {}, ""
    routes: list[dict] = []
    for row in payload.get("candidates") or []:
        try:
            route = {
                **row,
                "pool": row.get("pool_name") or "",
                "fee": Decimal(str(row["fee"])),
                "dec_stable": int(row["dec_stable"]),
                "dec_token": int(row["dec_token"]),
                "v2": bool(row.get("v2")),
                "depth_usd": Decimal(str(row.get("depth_usd") or "0")),
            }
        except (KeyError, ValueError, TypeError, InvalidOperation):
            continue
        if not route.get("slug") or not route.get("token"):
            continue
        routes.append(route)
    return routes, payload.get("chains") or {}, str(payload.get("generated_at") or "")


def merge_routes(*groups: list[dict]) -> list[dict]:
    """Union route lists, first one wins, keyed by chain + pool + token."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for group in groups:
        for route in group:
            key = (route.get("slug"), (route.get("pool_address") or "").lower(),
                   (route.get("token") or "").lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(route)
    return out


def quote_evm(route: dict, ref: Decimal, ref_kind: str, rq,
              sell: Decimal) -> dict | None:
    """链上 EVM/TON 买入报价。机会判断对 Kraken 买盘 ``sell``，``ref`` 只作参考。"""
    anchor = sell_anchor(sell)
    winner = None
    first = None
    breaks = ""
    slot0_state = None
    screen = None
    if route.get("quote_via") == "slot0":
        # 池子状态一轮读一次，整条价格阶梯共用同一份状态。
        try:
            slot0_state = slot0_pool_state(route, rq)
            screen = _import_screen()
        except Exception:
            slot0_state = None
    for size in SIZES:
        raw_in = int(size * Decimal(10) ** route["dec_stable"])
        if route.get("kind") == "ton":
            out = quote_ton(route, raw_in)
        elif route.get("quote_via") == "slot0":
            # 没有 Quoter 的链（Ink）：用 slot0 + 流动性算出这一档的成交均价，
            # 直接就是每枚的美元价，不需要再除以得到数量。
            share = None if slot0_state is None else slot0_share(slot0_state, size, screen)
            if share is None:
                breaks = _money(size)
                break
            if first is None:
                first = (size, share, size / share if share > 0 else Decimal(0),
                         route["fee"] * 100)
            gap = classify(share, anchor)
            if gap is None:
                breaks = _money(size)
                break
            winner = (size, share, gap, size / share if share > 0 else Decimal(0),
                      route["fee"] * 100)
            continue
        elif route["v2"]:
            out = rq.v2_out(route["slug"], route["pool_address"], route["stable"], route["token"], route["fee"], raw_in)
        else:
            out = rq.quote_v3(route["slug"], route["stable"], route["token"], route["fee"],
                              raw_in, route.get("quoter") or None)
        if not out:
            breaks = _money(size)
            break
        got = Decimal(out) / Decimal(10) ** route["dec_token"]
        if got <= 0:
            breaks = _money(size)
            break
        share = size / got
        if first is None:
            first = (size, share, got, route["fee"] * 100)
        gap = classify(share, anchor)
        if gap is None:
            breaks = _money(size)
            break
        # EVM 池的费率写在池名里（例如 0.05%），缓存里也有。这些尺寸下曲线部分
        # 可以忽略，所以买入成本就用池子费率本身。
        impact = route["fee"] * 100
        winner = (size, share, gap, got, impact)
    if winner is None:
        if first is None:
            return None
        size, share, got, impact = first
        gap_eff = gap_vs(share, anchor)
        if gap_eff > NEAR_MISS_BPS:
            return None
        return _near_miss({
            "symbol": route["symbol"], "ticker": route["ticker"], "network": route["network"],
            "form": route["form"], "address": route["token"],
            "share": _money(share), "ref": _money(ref), "ref_kind": ref_kind,
            "sell": _money(sell), "gap_bps": _gap_text(gap_vs(share, sell)),
            "spend_usd": _money(size), "tokens": format(got, "f"),
            "impact_pct": "—" if impact is None else _money(impact),
            "breaks_usd": "", "pool": route["pool"], "kraken": "",
            "via": "ston.fi" if route.get("kind") == "ton" else "链上询价",
            "url": route.get("explorer_url") or (
                f"https://www.geckoterminal.com/{route.get('gecko') or route['slug']}/pools/"
                f"{route.get('pool_address', '')}"),
            "frozen": False,
        }, gap_eff)
    size, share, gap, got, impact = winner
    return {
        "symbol": route["symbol"], "ticker": route["ticker"], "network": route["network"],
        "form": route["form"], "address": route["token"],
        "share": _money(share), "ref": _money(ref), "ref_kind": ref_kind,
        "sell": _money(sell), "gap_bps": _gap_text(gap_vs(share, sell)),
        "spend_usd": _money(size),
        "tokens": format(got, "f"),
        "impact_pct": "—" if impact is None else _money(impact),
        "breaks_usd": breaks, "pool": route["pool"], "kraken": "",
        "via": "ston.fi" if route.get("kind") == "ton" else "链上询价",
        # 路径列优先用普查给的链接（Monad 没有 GeckoTerminal 页面，用 DexScreener）。
        "url": route.get("explorer_url") or (
            f"https://www.geckoterminal.com/{route.get('gecko') or route['slug']}/pools/"
            f"{route.get('pool_address', '')}"
        ),
        "frozen": False,
    }


def _mark_frozen(hits: list[dict], previous: dict[tuple, dict]) -> None:
    for hit in hits:
        if hit.get("_fail"):
            continue
        old = previous.get((hit["symbol"], hit["network"]))
        if old is not None and old.get("share") == hit["share"]:
            hit["frozen"] = True


def one_cycle(solana: list[dict], multipliers: dict[str, Decimal],
              stock_cache: dict[str, tuple[Decimal, float, str]], evm_ready: list[dict],
              previous: dict[tuple, dict], stale_routes: list[dict] | None = None) -> dict:
    global _sol_usd
    started = time.perf_counter()
    limited_before = _pacer.limited
    _take_fail_reasons()
    tickers = [row["ticker"] for row in solana]
    tickers.extend(route["ticker"] for route in evm_ready)

    # Step 1: stock reference. Price V3 stockData for every Solana mint (4 calls).
    # A batch that fails keeps the last value for up to REF_REUSE_S.
    now = time.time()
    pv = price_v3([row["address"] for row in solana] + [SOL_MINT])
    try:
        sol_px = Decimal(str((pv.get(SOL_MINT) or {}).get("usdPrice")))
        if sol_px > 0:
            _sol_usd = sol_px          # 网络费换算成 USDT 用
    except Exception:
        pass
    fresh = parse_stock_refs(pv, {row["address"]: row["ticker"] for row in solana})
    for ticker, price in fresh.items():
        _stock_refs[ticker] = (price, now)
    refs: dict[str, tuple[Decimal, str]] = {
        ticker: (price, REF_KIND_JUP)
        for ticker, (price, at) in _stock_refs.items() if now - at <= REF_REUSE_S
    }
    # Step 2: names Price V3 does not cover fall back to FMP / Yahoo.
    fallback = [ticker for ticker in dict.fromkeys(tickers) if ticker not in refs]
    if fallback:
        try:
            refresh_stock_prices(fallback, stock_cache)
        except Exception as exc:
            print("stock refresh failed", exc, flush=True)
        for ticker in fallback:
            if ticker in stock_cache:
                refs[ticker] = (stock_cache[ticker][0], stock_cache[ticker][2])
    # Step 3: independent spot check of a few Jupiter references.
    try:
        cross_check_refs([ticker for ticker in refs if refs[ticker][1] == REF_KIND_JUP])
    except Exception as exc:
        print("ref check failed", exc, flush=True)

    # Step 3b: 卖出锚。一轮一次公开 Ticker，拿回全部标的的 Kraken 买一。
    # 「有没有套利」用链上买入价对 Kraken 买盘判断（operator 2026-10-06）：
    # 我们是真的在 Kraken 卖出，正股价只填参考列。
    sell_bids = kraken_best_bids(
        [row["symbol"] for row in solana] + [route["symbol"] for route in evm_ready])

    rq = _import_requote() if evm_ready else None
    quoted = 0
    errors = 0
    hits: list[dict] = []
    near_rows: list[dict] = []
    no_sell = 0

    def run_solana(row: dict) -> dict | None:
        try:
            cached = refs.get(row["ticker"])
            if cached is None:
                return None
            sell = sell_bids.get(row["symbol"])
            if not sell:
                return {"_nosell": True, "symbol": row["symbol"], "network": "Solana"}
            ref, kind = cached
            status, gap_row = quote_solana(
                row, ref, multipliers.get(row["symbol"], Decimal(1)), kind, sell)
            if status in ("fail", "limited", "no_route"):
                return {"_" + status: True, "symbol": row["symbol"], "network": "Solana"}
            return gap_row
        except Exception as exc:
            reason = getattr(exc, "reason", None)
            _note_fail(type(exc).__name__ + (f":{type(reason).__name__}" if reason is not None else ""))
            return {"_fail": True, "symbol": row["symbol"], "network": "Solana"}

    def keep_previous(result: dict, why: str) -> None:
        kept = previous.get((result["symbol"], result["network"]))
        if kept:
            stale = dict(kept)
            # 从改动前那一轮继承下来的行没有 url，补一个，否则路径列不可点。
            if not stale.get("url") and stale.get("network") == "Solana":
                stale["url"] = f"https://jup.ag/tokens/{stale.get('address', '')}"
            stale["frozen"] = True
            kind = str(stale.get("ref_kind") or "").split(" · 本轮")[0]
            stale["ref_kind"] = kind + " · " + why
            hits.append(stale)

    def quote_evm_routes(routes: list[dict]) -> tuple[list[dict], list[dict], int, int]:
        """EVM/TON 询价。与 Solana 的 Jupiter 询价并行跑，不占它的时间。"""
        out: list[dict] = []
        near: list[dict] = []
        count = 0
        bad = 0
        for route in routes:
            cached = refs.get(route["ticker"])
            if cached is None or (rq is None and route.get("kind") != "ton"):
                continue
            sell = sell_bids.get(route["symbol"])
            if not sell:
                continue
            ref_price, kind = cached
            try:
                hit = quote_evm(route, ref_price, kind, rq, sell)
            except Exception:
                bad += 1
                continue
            count += 1
            if hit:
                status = next((row["kraken"] for row in solana
                               if row["symbol"] == route["symbol"]), "")
                hit["kraken"] = status or hit["kraken"] or "未知"
                (near if hit.get("near_miss") else out).append(hit)
        return out, near, count, bad

    # Step 4: executable buy quotes. Mints with no Jupiter route wait for the recheck.
    with_ref = [row for row in solana if row["ticker"] in refs]
    solvable = [row for row in with_ref if _no_route_until.get(row["symbol"], 0) <= now]
    no_route_skipped = len(with_ref) - len(solvable)
    # Names that were cheap last pass are re-quoted first.
    solvable.sort(key=lambda row: (row["symbol"], "Solana") not in previous)
    missing = [row["ticker"] for row in solana if row["ticker"] not in refs]
    failed = 0
    limited = 0
    no_route = 0
    sol_quoted = 0
    # EVM/TON 与 Solana 并行：Solana 被 Jupiter 限速拖着，EVM 的 RPC 正好藏在等待里。
    evm_pool = ThreadPoolExecutor(1)
    evm_future = evm_pool.submit(quote_evm_routes, list(evm_ready))
    try:
        with ThreadPoolExecutor(WORKERS) as pool:
            for result in pool.map(run_solana, solvable):
                quoted += 1
                sol_quoted += 1
                if not result:
                    continue
                if result.get("_no_route"):
                    no_route += 1
                    _no_route_until[result["symbol"]] = time.time() + NO_ROUTE_RECHECK_S
                    continue
                if result.get("_limited"):
                    limited += 1
                    keep_previous(result, "本轮限流未询到")
                    continue
                if result.get("_fail"):
                    failed += 1
                    keep_previous(result, "本轮询价失败")
                    continue
                if result.get("_nosell"):
                    no_sell += 1
                    continue
                if result.get("near_miss"):
                    near_rows.append(result)
                    continue
                hits.append(result)
        evm_hits, evm_near, evm_quoted, evm_errors = evm_future.result()
        hits.extend(evm_hits)
        near_rows.extend(evm_near)
        quoted += evm_quoted
        errors += evm_errors
    finally:
        evm_pool.shutdown(wait=False)

    # Step 5: rows whose reference failed the spot check stay visible but flagged.
    for hit in hits:
        alarm = _ref_alarms.get(hit.get("ticker", ""))
        hit["ref_alarm"] = f"雅虎 {alarm['yahoo']}，差 {alarm['dev_pct']}%" if alarm else ""

    # Step 5b: 这一轮没轮到询价的候选，如果上一轮还便宜就留着并写明原因，
    # 免得操作员以为那口池子突然消失了。
    for route in stale_routes or []:
        key = (route["symbol"], route["network"])
        if any((hit["symbol"], hit["network"]) == key for hit in hits if not hit.get("_fail")):
            continue
        kept = previous.get(key)
        if not kept:
            continue
        stale = dict(kept)
        stale["frozen"] = True
        kind = str(stale.get("ref_kind") or "").split(" · 本轮")[0]
        stale["ref_kind"] = kind + " · 本轮未询价"
        hits.append(stale)

    # Step 6: 风险分档（买入这侧高出中价多少）。净利不在这里算——它要用 Kraken
    # 的真实买盘，等出口闸门那一步拿到 Depth 再算（operator 2026-10-06）。
    for hit in hits:
        hit.pop("roundtrip_bps", None)   # 旧口径遗留字段，不再使用
        hit["risk"] = risk_label(_impact_of(hit.get("impact_pct")))

    # Step 6b: 定这一笔做多大。不重新询价——阶梯已经带着数量问过了，
    # 用「对 Kraken 仍便宜的最大档」封顶单笔上限（10 USDT）。
    plan_trade_size(hits)

    # Step 6c: 出口闸门 + 卖出侧净利。净利 = 链上买入价 vs Kraken 买盘逐档成交价。
    # Kraken 收不到、或者我们自己的数量会砸穿盘口的行，直接不上榜。
    hits = apply_exit_gate(hits)
    mark_executable(hits)

    # Step 6d: 近失的行（比 Kraken 有效卖价还贵一点）。不是机会：只给价差、
    # 买入价和 Kraken 卖价，净利留空，也不进口闸门。
    near_rows.sort(key=lambda item: Decimal(item.get("near_gap_bps") or "0"))
    near_rows = near_rows[:NEAR_MISS_MAX]

    _mark_frozen(hits, previous)
    # 净利优先；没有净利的行按价差排，仍然留在表上让人看见但不能执行。
    hits.sort(key=lambda item: -Decimal(item.get("net_usdt") or "-999999"))
    hits.extend(near_rows)
    missing_n = len(set(missing))
    jup_n = sum(1 for _price, kind in refs.values() if kind == REF_KIND_JUP)
    source = (
        f"对照是正股价格：{jup_n} 只用 Jupiter Price V3 的 stockData（含盘后最后成交），"
        f"{len(refs) - jup_n} 只退回 FMP/雅虎。每轮用雅虎含盘后的最后成交抽查 "
        f"{REF_CHECK_PER_CYCLE} 只，差超过 {_money(REF_ALARM * 100)}% 标「对照存疑」，"
        f"当前 {len(_ref_alarms)} 只。"
        f"EVM 池 {len(evm_ready)}/{_evm_universe or len(EVM_ROUTES)} 条可询，其余等解析。"
        f"本轮询价窗口 {len(_census_window)}/{_census_total} 条（轮转，没轮到的行会标记）。"
    )
    quoted_by_chain: dict[str, int] = {}
    hits_by_chain: dict[str, int] = {}
    for route in evm_ready:
        quoted_by_chain[route.get("network", "")] = quoted_by_chain.get(route.get("network", ""), 0) + 1
    for hit in hits:
        network = hit.get("network", "")
        hits_by_chain[network] = hits_by_chain.get(network, 0) + 1
    chain_rows = [{
        "network": "Solana",
        "key": "solana",
        "deployed": len(solana),
        "pools_seen": None,
        "pools_with_depth": None,
        "below_gate": None,
        "traps": None,
        "deepest_measured_usd": None,
        "quoted": sol_quoted,
        "hits": hits_by_chain.get("Solana", 0),
    }]
    for network, row in _chains_summary.items():
        chain_rows.append({
            "network": row.get("network") or network,
            "key": network,
            "deployed": row.get("kraken_deployments_in_scope"),
            "pools_seen": row.get("pools_seen"),
            "pools_with_depth": row.get("pools_with_depth"),
            "traps": row.get("traps"),
            "below_gate": row.get("below_gate"),
            "other_rejected": row.get("other_rejected"),
            "unmeasured": row.get("unmeasured"),
            "unindexed": row.get("unindexed"),
            "deepest_measured_usd": row.get("deepest_measured_usd"),
            "quoted": quoted_by_chain.get(row.get("network") or network, 0),
            "hits": hits_by_chain.get(row.get("network") or network, 0),
        })
    # 普查不覆盖、但看板确实在报价的链（今天就是 X Layer 的手挑池）也要列出来，
    # 否则表上会让人以为那条链没在看。
    known = {row["network"] for row in chain_rows}
    for network, count in quoted_by_chain.items():
        if network in known:
            continue
        chain_rows.append({
            "network": network,
            "key": network,
            "deployed": None,
            "pools_seen": None,
            "pools_with_depth": None,
            "below_gate": None,
            "other_rejected": None,
            "traps": None,
            "unmeasured": None,
            "unindexed": None,
            "deepest_measured_usd": None,
            "quoted": count,
            "hits": hits_by_chain.get(network, 0),
            "note": "手挑池，未走普查",
        })
    return {
        "phase": "idle",
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "finished_ms": int(time.time() * 1000),
        "cycle_s": round(time.perf_counter() - started, 2),
        "quoted": quoted,
        "with_ref": len(with_ref),
        "without_ref": missing_n,
        "errors": errors + failed,
        "no_route": no_route + no_route_skipped,
        "no_route_names": sorted(
            symbol for symbol, until in _no_route_until.items() if until > time.time()),
        "limited": limited,
        "ref_jupiter": jup_n,
        "ref_fallback": len(refs) - jup_n,
        "ref_alarms": list(_ref_alarms.values()),
        "ref_checks": _ref_checks[-REF_CHECK_PER_CYCLE:],
        "evm_ready": [route["symbol"] for route in evm_ready],
        "evm_pending": [route["symbol"] for route in EVM_ROUTES
                        if route["symbol"] not in {r["symbol"] for r in evm_ready}],
        "chains": chain_rows,
        "census_at": _census_at,
        "census_total": _census_total,
        "census_window": _census_window,
        "fail_reasons": _take_fail_reasons(),
        "jupiter_429": _pacer.limited - limited_before,
        "jupiter_host": _jup_base.split("//", 1)[-1],
        "jupiter_rps": round(_pacer.rate, 2),
        "slippage_bps": SLIPPAGE_BPS,
        "jito_tip_lamports": JITO_TIP_LAMPORTS,
        "exit_cost_bps": str(EXIT_COST_BPS),
        "sol_usd": str(_sol_usd),
        # 链上费：重复标的（账户已建）与首次标的（含替 Kraken 建账户）两档。
        "buy_fee_usdt": _money((BUY_FEE_SOL + TRANSFER_FEE_REPEAT_SOL) * _sol_usd),
        "chain_fee_first_usdt": _money((BUY_FEE_SOL + TRANSFER_FEE_FIRST_SOL) * _sol_usd),
        "transfer_seen": sorted((_transfer_seen_cache["seen"] or {}).keys()),
        "rent_sol": str(TOKEN_ACCOUNT_RENT_SOL),
        # 正股对照价（ticker → 价）。持仓估值用同一份价格，不另开数据源。
        "refs": {ticker: str(price) for ticker, (price, _kind) in refs.items()},
        "hits": hits,
        "near_miss": len(near_rows),
        "executable": sum(1 for hit in hits if hit.get("executable")),
        "exit_notes": dict(_exit_notes),
        "exit_rejects": list(_exit_rejects),
        "note": (
            f"只列出询价后仍比 Kraken 有效卖价便宜超过 10 bps 的标的（正股价只填参考列）。"
            f"路径就是链上买入 → 充值 Kraken → 卖出，所以成本只算买入这一侧，"
            f"不做任何「卖回同一口池」的假设。"
            f"「净利 USDT」= 按板上尺寸在链上买入、按 Kraken 买盘逐档（真实买一与均价）卖出，"
            f"扣 Kraken 费率 {EXIT_COST_BPS} bps"
            f"再扣链上买入网络费 {BUY_FEE_SOL} SOL + 充值转账费"
            f"（首次 {TRANSFER_FEE_FIRST_SOL} / 已建账户 {TRANSFER_FEE_REPEAT_SOL} SOL，"
            f"按 SOL {_money(_sol_usd)} 折合 {_money((BUY_FEE_SOL + TRANSFER_FEE_REPEAT_SOL) * _sol_usd)}–"
            f"{_money((BUY_FEE_SOL + TRANSFER_FEE_FIRST_SOL) * _sol_usd)} USDT）；"
            f"买入的池子费与冲击已经在「成交每股」里。盘口吃不下这个尺寸就不给净利。"
            f"表尾「近失」是比 Kraken 有效卖价还贵、但差在 {NEAR_MISS_BPS} bps 以内的行，"
            f"列出 {len(near_rows)} 条，不构成机会、不给净利。"
            f"每个新代币账户另有 {TOKEN_ACCOUNT_RENT_SOL} SOL 租金（关账户可退），没有计入净利。"
            f"「买入成本 %」是这笔买入相对池子中价多付的部分（池子费 + 曲线），"
            f"Solana 取自买单报价的价格冲击，EVM 用池子费率本身；"
            f"「风险」按它分档：<2% 低、2–5% 中、≥5% 高。"
            f"出口闸门（2026-10-05 起）：Kraken 没有这个交易对、不能收这条链、"
            f"或者我们 {_money(TRADE_SIZE_USD)} USDT 的数量会把盘口砸穿 {MAX_EXIT_SLIP_BPS} bps 的行，"
            f"直接不上榜。「可执行」= 出口成立 + 按计划尺寸"
            f"（min({_money(TRADE_SIZE_USD)} USDT, 阶梯上仍便宜的最大档)）净利 > 0.1 + 风险低；"
            f"我们不为了 10 美元再单独询一次价。"
            f"执行参数：滑点 {SLIPPAGE_BPS} bps + Jito 小费 {JITO_TIP_LAMPORTS} lamports。"
            f"{source}"
            f"Jupiter 走 {_jup_base.split('//', 1)[-1]}，"
            f"{'带 key' if _jup_key else '不带 key'}，限速每秒 {_pacer.rate:.2f} 次。"
            f"Jupiter 没有买入路由的 {no_route + no_route_skipped} 个"
            f"（{NO_ROUTE_RECHECK_S // 60} 分钟复查一次）。"
            f"本轮被限流未询到 {limited} 个，询价失败 {failed} 个，"
            f"其中上一轮还便宜的会暂时留在表上。"
            f"Kraken 没有买盘（卖不掉）的 {no_sell} 个。"
            f"还没有正股价的 {missing_n} 个。"
        ),
    }


def _publish(snapshot: dict) -> None:
    text = json.dumps(snapshot, ensure_ascii=False)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(text)
    tmp.replace(OUT)
    with _state_lock:
        _state.clear()
        _state.update(snapshot)


def loop() -> None:
    global _chains_summary, _census_at, _evm_universe, _census_total, _census_window
    if OUT.exists():
        try:
            saved = json.loads(OUT.read_text())
            saved["note"] = "上一轮结果还在。新的一轮正在询价。"
            _publish(saved)
        except Exception:
            pass
    solana = load_solana()
    multipliers = load_multipliers()
    stock_cache = load_stock_cache()
    previous: dict[tuple, dict] = {}
    if OUT.exists():
        try:
            for hit in json.loads(OUT.read_text()).get("hits") or []:
                if not hit.get("near_miss"):
                    previous[(hit["symbol"], hit["network"])] = hit
        except Exception:
            previous = {}
    # EVM pools: use saved facts; GeckoTerminal 429s often, so a pool that is not
    # saved yet is looked up one at a time, one per pass, until it resolves.
    evm_cache = load_evm_cache()
    evm_ready: list[dict] = []
    evm_pending: list[dict] = []
    for route in EVM_ROUTES:
        hit = cached_route(route, evm_cache)
        (evm_ready if hit else evm_pending).append(hit or route)
    census_routes, _chains_summary, _census_at = load_census()
    _evm_universe = len(EVM_ROUTES) + len(census_routes)
    _census_total = len(census_routes)
    print(f"universe solana={len(solana)} evm={len(evm_ready)} "
          f"census={len(census_routes)} evm_pending={len(evm_pending)}", flush=True)
    evm_cursor = 0
    while True:
        # 重新读普查结果：后台普查跑完，看板不用重启就能接上新池子。
        census_routes, _chains_summary, _census_at = load_census()
        _evm_universe = len(EVM_ROUTES) + len(census_routes)
        _census_total = len(census_routes)
        # 轮转窗口：每轮只询价 EVM_BUDGET 条，候选多时几轮内轮完每一次。
        ordered = (census_routes[evm_cursor:] + census_routes[:evm_cursor]
                   if census_routes else [])
        window = ordered[:EVM_BUDGET]
        stale = ordered[EVM_BUDGET:]
        evm_cursor = (evm_cursor + len(window)) % max(1, len(census_routes))
        _census_window = [route["symbol"] for route in window]
        universe = merge_routes(evm_ready, window)
        if evm_pending:
            route = evm_pending[0]
            try:
                resolved, why = resolve_evm(route, _import_requote())
            except Exception as exc:
                resolved, why = None, type(exc).__name__
            if resolved:
                evm_pending.pop(0)
                evm_ready.append(resolved)
                save_evm_cache(evm_ready)
                universe = merge_routes(evm_ready, census_routes)
                print(f"evm resolved {route['symbol']}", flush=True)
            else:
                evm_pending.append(evm_pending.pop(0))
                print(f"evm resolve {route['symbol']} failed: {why}; retry next pass", flush=True)
        try:
            snapshot = one_cycle(solana, multipliers, stock_cache, universe, previous, stale)
        except Exception as exc:
            print("cycle failed", exc, flush=True)
            time.sleep(5)
            continue
        # 近失的行不进 previous：它们不是机会，不该被下一轮当成「上轮还便宜」继承。
        previous = {(hit["symbol"], hit["network"]): hit
                    for hit in snapshot["hits"] if not hit.get("near_miss")}
        _publish(snapshot)
        print(
            f"cycle {snapshot['cycle_s']}s quoted={snapshot['quoted']} "
            f"hits={len(snapshot['hits'])} without_ref={snapshot['without_ref']} "
            f"limited={snapshot['limited']} failed={snapshot['errors']} no_route={snapshot['no_route']} "
            f"jup429={snapshot['jupiter_429']} rps={snapshot['jupiter_rps']} "
            f"reasons={json.dumps(snapshot['fail_reasons'], ensure_ascii=False)}",
            flush=True,
        )
        # The quote pass itself is the interval. A short pause keeps the proxy usable.
        time.sleep(2)


PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>链上价差</title>
<style>
  :root {
    --bg: #f4f6fa;
    --surface: #fff;
    --line: #e3e8f0;
    --line-soft: #eef1f6;
    --text: #17212b;
    --muted: #697586;
    --accent: #1552f0;
    --good: #077a45;
    --bad: #bf1d29;
    --warn-bg: #fff8e6;
    --hover: #f8faff;
    --shadow: 0 10px 30px rgba(23, 33, 43, .07);
  }
  * { box-sizing: border-box; }
  body {
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    margin: 0;
    padding: 24px;
    color: var(--text);
    background: radial-gradient(circle at top left, #eaf0ff 0%, transparent 38%), var(--bg);
  }
  .shell { max-width: 1680px; margin: 0 auto; display: grid; gap: 18px; }
  h1 {
    font-size: 26px;
    letter-spacing: -.02em;
    margin: 0;
  }
  .stats {
    display: grid;
    grid-template-columns: repeat(4, minmax(160px, 1fr));
    gap: 12px;
  }
  .stats div {
    min-height: 92px;
    padding: 16px 18px;
    border: 1px solid var(--line);
    border-radius: 12px;
    background: var(--surface);
    box-shadow: 0 2px 6px rgba(23, 33, 43, .03);
  }
  .stats b {
    display: block;
    font-size: 28px;
    line-height: 1.15;
    letter-spacing: -.02em;
    margin-bottom: 5px;
  }
  .muted { color: var(--muted); }
  .panel {
    padding: 18px;
    border: 1px solid var(--line);
    border-radius: 14px;
    background: var(--surface);
    box-shadow: var(--shadow);
  }
  .panel > h2 {
    font-size: 16px;
    margin: 0 0 12px;
  }
  .holdings {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
  }
  /* 三块之间画竖线，读的时候不容易串行（operator 2026-10-06） */
  .holdings > div { padding: 0 22px; border-left: 1px solid #e6e6e6; }
  .holdings > div:first-child { border-left: 0; padding-left: 0; }
  .holdings > div:last-child { padding-right: 0; }
  .holdings h3 {
    font-size: 14px;
    margin: 12px 0 2px;
  }
  .holdings table { width: auto; min-width: 240px; }
  .table-card { padding: 0; overflow: hidden; }
  .table-scroll { overflow: auto; max-height: 72vh; border-radius: inherit; }
  table { border-collapse: collapse; width: 100%; min-width: 1560px; }
  th, td {
    padding: 10px 12px;
    border-bottom: 1px solid var(--line-soft);
    text-align: left;
    vertical-align: top;
    white-space: nowrap;
  }
  thead th {
    position: sticky;
    top: 0;
    z-index: 1;
    font-size: 11px;
    font-weight: 650;
    letter-spacing: .04em;
    color: #4b5768;
    background: #f7f9fc;
    box-shadow: inset 0 -1px 0 var(--line);
  }
  tbody tr:nth-child(even) td { background: #fbfcfe; }
  tbody tr:hover td { background: var(--hover); }
  th .muted { font-weight: 400; text-transform: none; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  td.pos { color: var(--good); font-weight: 700; }
  td.negv { color: var(--bad); font-weight: 650; }
  tr.frozen td { background: var(--warn-bg) !important; }
  tr.neg td { color: #8b8f98; }
  tr.near td { color: #878d99; font-style: italic; }
  tr.neg td.negv { color: var(--bad); }
  code {
    display: inline-block;
    max-width: 180px;
    overflow: hidden;
    text-overflow: ellipsis;
    vertical-align: bottom;
    padding: 2px 5px;
    border-radius: 5px;
    font-size: 12px;
    background: #f1f4f8;
  }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  #exitGate { margin-bottom: 0; }
  @media (max-width: 760px) {
    body { padding: 14px; }
    .stats { grid-template-columns: repeat(2, 1fr); }
  }
</style>
</head>
<body>
<main class="shell">
<header>
  <h1>链上价差</h1>
</header>
<div class="stats">
  <div><b id="hits">–</b><span class="muted">对 Kraken 便宜 &gt;10 bps</span></div>
  <div><b id="cycle">–</b><span class="muted">上一轮耗时（秒）</span></div>
  <div><b id="age">–</b><span class="muted">距上一轮完成（秒）</span></div>
  <div><b id="quoted">–</b><span class="muted">本轮已询价</span></div>
</div>
<section class="panel">
  <h2>钱包与 Kraken 持仓</h2>
  <p class="muted" id="holdNote">正在读取钱包与 Kraken 余额</p>
  <div class="holdings" id="holdings"></div>
  <p class="muted" id="exitGate">正在读取出口闸门</p>
</section>
<section class="panel">
  <h2>自动链</h2>
  <p class="muted" id="autoNote">正在读取自动链状态</p>
  <div class="stats" id="autoStats"></div>
</section>
<section class="panel table-card">
<div class="table-scroll" style="max-height:32vh">
<table id="autoRows">
  <thead>
    <tr>
      <th>时间</th><th>标的</th><th>链</th><th>尺寸 USDT</th><th>状态</th>
      <th>买入价</th><th>卖出价</th><th>净利 USDT</th><th>入账等待 s</th>
      <th>txid（买）</th><th>说明</th>
    </tr>
  </thead>
  <tbody></tbody>
</table>
</div>
</section>
<section class="panel table-card">
<h2>Ethereum 手工实盘</h2>
<div class="table-scroll" style="max-height:28vh">
<table id="ethRows">
  <thead>
    <tr>
      <th>日期</th><th>标的</th><th>尺寸 USD</th><th>状态</th>
      <th>买入价</th><th>卖出价</th><th>净利 USDT</th><th>txid（买）</th><th>说明</th>
    </tr>
  </thead>
  <tbody></tbody>
</table>
</div>
</section>
<section class="panel table-card">
<div class="table-scroll">
<table>
  <thead>
    <tr>
      <th>价差 bps</th><th>净利 USDT</th><th>代币</th><th>链</th><th>仍便宜的金额</th>
      <th>下一档</th>
      <th>成交每股<br><span class="muted">链上买入</span></th>
      <th>Kraken 卖价<br><span class="muted">买一</span></th>
      <th>正股参考<br><span class="muted">仅参考</span></th>
      <th>参考来源</th><th>路径</th>
      <th>买入成本 %</th><th>风险</th><th>合约</th><th>Kraken</th>
      <th>出口（50 bps 内）</th><th>充值链路</th><th>可执行</th>
    </tr>
  </thead>
  <tbody id="rows"></tbody>
</table>
</div>
</section>
</main>
<script>
const HOLD_TITLES = {solana: 'Solana 钱包', xlayer: 'X Layer 钱包', ethereum: 'Ethereum 钱包', kraken: 'Kraken'};

function holdingsTable(part) {
  const table = document.createElement('table');
  const thead = document.createElement('thead');
  const head = document.createElement('tr');
  for (const label of ['币', '数量', '估值 USDT', '说明']) {
    const th = document.createElement('th');
    th.textContent = label;
    head.appendChild(th);
  }
  thead.appendChild(head);
  table.appendChild(thead);
  const body = document.createElement('tbody');
  for (const row of (part && part.rows) || []) {
    const tr = document.createElement('tr');
    const cells = [
      [row.asset || '', ''],
      [row.amount == null ? '–' : row.amount, 'num'],
      [row.value == null ? '–' : '≈' + row.value, 'num'],
      [row.note || '', ''],
    ];
    for (const [text, cls] of cells) {
      const td = document.createElement('td');
      if (cls) { td.className = cls; }
      td.textContent = text;
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
  if (!body.children.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 4;
    td.className = 'muted';
    td.textContent = (part && part.error) ? '没读到' : '空';
    tr.appendChild(td);
    body.appendChild(tr);
  }
  table.appendChild(body);
  return table;
}

function holdingsBlock(key, part) {
  const box = document.createElement('div');
  const title = document.createElement('h3');
  title.textContent = HOLD_TITLES[key] || key;
  box.appendChild(title);
  const meta = document.createElement('p');
  meta.className = 'muted';
  meta.style.margin = '0 0 4px';
  const bits = [];
  if (part) {
    if (part.wallet) { bits.push(part.wallet.slice(0, 6) + '…' + part.wallet.slice(-4)); }
    if (part.at_ms) {
      bits.push('更新 ' + new Date(part.at_ms).toLocaleTimeString('zh-CN', {hour12: false}));
    }
    if (part.total) { bits.push('合计 ≈' + part.total); }
    if (part.empty) { bits.push('另有 ' + part.empty + ' 个 0 余额'); }
    if (part.error) { bits.push('查询失败：' + part.error); }
  } else {
    bits.push('等第一轮读取');
  }
  meta.textContent = bits.join(' · ');
  box.appendChild(meta);
  box.appendChild(holdingsTable(part));
  return box;
}

const AUTO_MODE_LABEL = {off: 'off（只巡检）', paper: 'paper（不广播）', live: 'live（真钱）'};

function renderAuto(auto) {
  const note = document.getElementById('autoNote');
  if (!auto.updated_ms) {
    note.textContent = '自动链还没跑过（没有 data/xstocks_auto.json）';
  } else {
    const ageS = Math.round((Date.now() - auto.updated_ms) / 1000);
    let processText = auto.running ? '进程在跑' : '进程没在跑';
    if (auto.running && ageS > 180) {
      processText = '进程可能停了（' + Math.round(ageS / 60) + ' 分钟没更新）';
    }
    const bits = [processText,
                  '模式 ' + (AUTO_MODE_LABEL[auto.mode] || auto.mode || 'off'),
                  '阶段 ' + (auto.phase || '?'),
                  '更新 ' + new Date(auto.updated_ms).toLocaleTimeString('zh-CN', {hour12: false})
                  + '（' + ageS + 's 前）'];
    if (auto.pause_reason) { bits.push('⛔ 软暂停：' + auto.pause_reason); }
    if (auto.note) { bits.push(auto.note); }
    note.textContent = bits.join(' · ');
  }
  const box = document.getElementById('autoStats');
  box.replaceChildren();
  const b = auto.balances || {};
  const addStat = (value, label, cls) => {
    const wrap = document.createElement('div');
    const strong = document.createElement('b');
    strong.textContent = value;
    if (cls) { strong.className = cls; }
    const span = document.createElement('span');
    span.className = 'muted';
    span.textContent = label;
    wrap.appendChild(strong);
    wrap.appendChild(span);
    box.appendChild(wrap);
  };
  addStat(String(auto.opened_quote_usdt == null ? '0' : auto.opened_quote_usdt),
          '今日开仓 / 500 USDT');
  addStat(String(auto.trades_today || 0), '今日笔数 / 20');
  addStat(String(auto.loss_streak || 0), '连亏笔数',
          (auto.loss_streak || 0) > 0 ? 'negv' : '');
  addStat(b.solana_usdt == null ? '–' : String(b.solana_usdt), 'Solana USDT');
  addStat(b.solana_lamports == null ? '–' : (b.solana_lamports / 1e9).toFixed(4),
          'Solana SOL');
  addStat(b.kraken_usd == null ? '–' : String(b.kraken_usd), 'Kraken ZUSD');
  addStat(b.xlayer_usdg == null ? '–' : String(b.xlayer_usdg), 'X Layer USDG');
  const body = document.querySelector('#autoRows tbody');
  body.replaceChildren();
  for (const row of auto.ledger || []) {
    const tr = document.createElement('tr');
    const when = row.ts_ms
      ? new Date(row.ts_ms).toLocaleString('zh-CN', {hour12: false}).slice(5) : '–';
    const net = row.net_usdt == null ? '–' : row.net_usdt;
    const values = [
      when, row.symbol || '–', row.network || '–', row.size_usd || '–',
      row.state || '–', row.buy_price || '–', row.sell_price || '–', net,
      row.credit_wait_s == null ? '–' : row.credit_wait_s,
      (row.txids && row.txids.buy) || '–', row.reason || '',
    ];
    values.forEach((value, index) => {
      const td = document.createElement('td');
      if ([3, 5, 6, 7, 8].includes(index)) { td.className = 'num'; }
      if (index === 7 && row.net_usdt != null) {
        td.className = 'num ' + (Number(row.net_usdt) >= 0 ? 'pos' : 'negv');
      }
      if (index === 9) {
        td.innerHTML = '<code>' + value + '</code>';
      } else {
        td.textContent = value;
      }
      tr.appendChild(td);
    });
    body.appendChild(tr);
  }
}

function renderEthLedger(rows) {
  const body = document.querySelector('#ethRows tbody');
  body.replaceChildren();
  for (const row of rows || []) {
    const tr = document.createElement('tr');
    const txids = row.txids || {};
    const buyTx = Array.isArray(txids.buy) ? txids.buy.join(', ') : (txids.buy || '–');
    const net = row.net_usdt == null ? '–' : row.net_usdt;
    const values = [
      row.day || '–', row.symbol || '–', row.size_usd || '–', row.state || '–',
      row.buy_price || '–', row.sell_price || '–', net, buyTx,
      Array.isArray(row.notes) ? row.notes.join('；') : (row.reason || ''),
    ];
    values.forEach((value, index) => {
      const td = document.createElement('td');
      if ([2, 4, 5, 6].includes(index)) { td.className = 'num'; }
      if (index === 6 && row.net_usdt != null) {
        td.className = 'num ' + (Number(row.net_usdt) >= 0 ? 'pos' : 'negv');
      }
      if (index === 7) { td.innerHTML = '<code>' + value + '</code>'; }
      else { td.textContent = value; }
      tr.appendChild(td);
    });
    body.appendChild(tr);
  }
}

async function tick() {
  const response = await fetch('/api/live', {cache: 'no-store'});
  const data = await response.json();
  const holdings = data.holdings || {};
  const holdBox = document.getElementById('holdings');
  holdBox.replaceChildren();
  for (const key of ['solana', 'xlayer', 'ethereum', 'kraken']) {
    holdBox.appendChild(holdingsBlock(key, holdings[key]));
  }
  document.getElementById('holdNote').textContent = holdings.at_ms
    ? '只读。估值 = 数量 × 乘数 × 上一轮正股对照价，「≈」是估算，「–」是没有可信价格。'
    : '正在读取钱包与 Kraken 余额';
  renderAuto(data.auto || {});
  renderEthLedger(data.eth_ledger || []);
  const hits = data.hits || [];
  document.getElementById('hits').textContent =
    String(hits.length - (data.near_miss || 0));
  const exitNotes = data.exit_notes || {};
  const exitRejects = data.exit_rejects || [];
  const rejectText = exitRejects
    .map((row) => row.symbol + '（' + row.reason + '）')
    .slice(0, 12).join('；');
  document.getElementById('exitGate').textContent =
    '出口闸门：Kraken 在册 ' + (exitNotes.kraken_pairs || 0)
    + ' 个（online ' + (exitNotes.kraken_online || 0) + '）· 充值方式已核对 '
    + (exitNotes.deposit_cached || 0) + ' 个 · 本轮因出口被拦下 '
    + (exitNotes.rejected || 0) + ' 个 · 可执行 ' + (data.executable || 0) + ' 行'
    + (rejectText ? ('｜被拦：' + rejectText) : '');
  document.getElementById('cycle').textContent = data.cycle_s == null ? '–' : String(data.cycle_s);
  document.getElementById('quoted').textContent = String(data.quoted || 0);
  const age = data.finished_ms ? Math.max(0, Math.round((Date.now() - data.finished_ms) / 1000)) : null;
  document.getElementById('age').textContent = age == null ? '–' : String(age);
  const body = document.getElementById('rows');
  body.replaceChildren();
  for (const hit of hits) {
    const tr = document.createElement('tr');
    tr.className = [hit.frozen ? 'frozen' : '', hit.net_positive === false ? 'neg' : '',
                    hit.near_miss ? 'near' : ''].filter(Boolean).join(' ');
    const cells = [
      {v: hit.gap_bps, cls: 'num', title: '链上买入价相对 Kraken 买一便宜多少（负数=便宜）。'},
      {v: hit.net_usdt == null ? '–' : hit.net_usdt,
       cls: hit.net_positive ? 'num pos' : 'num negv',
       title: '按板上尺寸 ' + hit.spend_usd + ' 美元在链上买入、按 Kraken 买盘逐档卖出'
         + '（扣 Kraken ' + (data.exit_cost_bps || '10') + ' bps、链上网络费 '
         + (data.buy_fee_usdt || '?') + ' USDT）。'
         + (hit.net_planned != null
            ? ' 按计划尺寸 ' + hit.size_planned + ' USDT 算是 ' + hit.net_planned + ' USDT。' : '')},
      {v: hit.symbol + ' ' + hit.ticker},
      {v: hit.network},
      {v: hit.near_miss ? '—' : hit.spend_usd + ' 美元', cls: 'num'},
      {v: hit.near_miss ? '—' : (hit.breaks_usd
          ? (hit.breaks_usd + (hit.breaks_unknown ? ' 被限流未询到' : ' 已不便宜'))
          : '500 仍便宜')},
      {v: hit.share, cls: 'num', title: '链上买入价（Kraken 同一单位）'},
      {v: hit.sell == null ? '–' : hit.sell, cls: 'num', title: 'Kraken 买一（真实卖价）'},
      {v: hit.ref, cls: 'num', title: '正股价，只作参考，不参与机会判断'},
      {v: hit.ref_kind + (hit.ref_alarm ? ' · 对照存疑：' + hit.ref_alarm : '')
          + (hit.frozen ? ' · 报价未变' : '')},
      {v: hit.pool + ' · ' + hit.via, url: hit.url},
      {v: hit.impact_pct == null ? '–' : hit.impact_pct, cls: 'num'},
      {v: hit.risk || '–', cls: hit.risk === '高' ? 'negv' : ''},
      {v: hit.address, code: true},
      {v: hit.kraken},
      {v: hit.exit_text || '–'},
      {v: hit.deposit_note || '–'},
      {v: hit.executable ? '✓ 可执行' : ('✗ ' + (hit.blocked_by || '未核实'))}
    ];
    cells.forEach((cell) => {
      const value = cell.v;
      const td = document.createElement('td');
      if (cell.cls) { td.className = cell.cls; }
      if (cell.title) { td.title = cell.title; }
      if (cell.url) {
        const a = document.createElement('a');
        a.href = cell.url;
        a.target = '_blank';
        a.rel = 'noreferrer';
        a.textContent = value;
        td.appendChild(a);
      } else if (cell.code) {
        td.innerHTML = '<code>' + value + '</code>';
      } else {
        td.textContent = value;
      }
      tr.appendChild(td);
    });
    body.appendChild(tr);
  }
}
tick();
setInterval(tick, 2000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/live"):
            with _state_lock:
                base = dict(_state)
            # 持仓由独立线程刷新，挂在这里而不是写进询价快照，两者互不覆盖。
            with _holdings_lock:
                payload = json.dumps({**base, "holdings": HOLDINGS,
                                      "auto": auto_snapshot(),
                                      "eth_ledger": eth_ledger_tail()},
                                     ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path in {"/", "/index.html"}:
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:
        return


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    threading.Thread(target=loop, name="xstocks-gap", daemon=True).start()
    threading.Thread(target=holdings_loop, name="xstocks-holdings", daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"listening http://127.0.0.1:{args.port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
