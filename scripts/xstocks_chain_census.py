#!/usr/bin/env python3
"""Multi-chain xStock pool census.

Answers one question per chain: are there xStock pools with real depth, and
which of them can be quoted? The live board only quotes pools that this census
put on the candidate list, so a chain with no pool shows up as "0 pools"
instead of silently disappearing.

Sources, in this order:
  1. DexScreener /latest/dex/tokens -- up to 30 addresses per call, covers
     Solana / Ethereum / BSC / Optimism / Arbitrum / Monad in one pass.
  2. GeckoTerminal networks/<slug>/tokens/<address>/pools -- one call per
     token per chain, for chains DexScreener does not index (X Layer, Ink,
     Mantle, HyperEVM). Slow on purpose: the free tier is rate limited.

Gates (all must pass before a pool becomes a candidate):
  * the other side of the pair is a stablecoin we know on that chain,
  * measured depth >= --min-depth (default 100 USD),
  * not a v4 pool and no fee of 50% or more (the trap pools),
  * the pool fee and both decimals can be read on chain.

Read only. No wallet, no order, no transaction.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOK = ROOT / "data" / "xstocks_book.sqlite"
OUT = ROOT / "data" / "xstocks_chain_census.json"
DEPTH_WATCHLIST = ROOT / "data" / "depth_watchlist.json"
PROXY = "http://127.0.0.1:4780"
DS_API = "https://api.dexscreener.com/latest/dex/tokens"
GECKO_API = "https://api.geckoterminal.com/api/v2/networks"
DS_BATCH = 30
# 深度门槛（operator 2026-10-03 指定）：20 USD。
# 低于这个数的池子连 1 美元的成交都撑不住，记成"低于门槛"而不是候选。
DEFAULT_MIN_DEPTH = Decimal("20")
STABLES = {
    "USDC", "USDT", "USDG", "USD1", "USDT0", "DAI", "USDE", "PYUSD", "FDUSD",
    "USDS", "USDE.E", "USDC.E",
}
CHAINS: dict[str, dict] = {
    "x-layer": {
        "network": "XLayer",
        "gecko": "x-layer",
        "dexscreener": None,
        "rpc": "https://rpc.xlayer.tech",
        "default_quoter": "0xd1b797d92d87b688193a2b976efc8d577d204343",
        "quoters": {},
        "stables": {
            "USDC": "0xb6ceceab302e2e4948951ee7843fc24e92933061",
            "USDG": "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8",
        },
    },
    "bsc": {
        "network": "BNB Smart Chain",
        "gecko": "bsc",
        "dexscreener": "bsc",
        "rpc": "https://bsc-dataseed.binance.org",
        # 2026-10-09：BSC 原来**没有 factory**，所以链上工厂扫描（发现 Ink 那 7 口深池的
        # 同一条路）从来没在 BSC 上跑过 —— census 里那 3 口池只是 DexScreener 的索引结果。
        # 补上以后，对最活跃的 20 个标的扫出 **8 口**池子（原来 3 口）。
        # 结论没变、但现在有证据：真实深度 $20–30，其余是尘埃池（其中一口
        # `liquidity()==0`、价格是垃圾价，被 pool_depth_usd 误报成 $1.1B，已加零流动性护栏）。
        "factory": "0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865",   # Pancake v3
        # 2026-10-10：不再对每个部署问工厂。BSC 的价走 xChange 软报价；
        # DexScreener 仍是一批地址一次调用，不是逐个池子。
        "pool_source": "xchange",
        # 工厂路径原来把 dex 硬编码成 "reservoir-v3"（Ink 的名字），于是 BSC 的池子
        # 匹配不到 `quoters["pancake"]`，回落成 default_quoter = 主网 Uniswap 地址，
        # 结果「池子找到了但报不出价」→ 看板上没有 BSC（2026-10-09 实测）。
        "factory_dex": "pancakeswap-v3",
        "probe_fees": [100, 500, 2500, 10000],
        "default_quoter": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        "quoters": {"pancake": "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997"},
        "stables": {
            "USDC": "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
            "USDT": "0x55d398326f99059ff775485246999027b3197955",
        },
    },
    "optimism": {
        "network": "Optimism",
        "gecko": "optimism",
        "dexscreener": "optimism",
        "rpc": "https://optimism-rpc.publicnode.com",
        "default_quoter": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        "quoters": {},
        "stables": {
            "USDC": "0x0b2C639c533813f4Aa9D7837CaF62653d097Ff85",
            "USDT": "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
        },
    },
    "monad": {
        "network": "Monad",
        "gecko": None,
        "dexscreener": "monad",
        "rpc": "https://rpc.monad.xyz",
        "default_quoter": "0x661e93cca42afacb172121ef892830ca3b70f08d",
        "quoters": {},
        "stables": {"USDC": "0x754704bc059f8c67012fed69bc8a327a5aafb603"},
    },
    "eth": {
        "network": "Ethereum",
        "gecko": "eth",
        "dexscreener": "ethereum",
        # ethereum.publicnode.com 经本机代理大量 SSL 断流；这个域名实测可用。
        "rpc": "https://ethereum-rpc.publicnode.com",
        "default_quoter": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        "quoters": {},
        "stables": {
            "USDC": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
            "USDT": "0xdac17f958d2ee523a2206206994597c13d831ec7",
        },
    },
    "arbitrum": {
        "network": "Arbitrum",
        "gecko": "arbitrum",
        "dexscreener": "arbitrum",
        "rpc": "https://arb1.arbitrum.io/rpc",
        "default_quoter": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        "quoters": {},
        "stables": {
            "USDC": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
            "USDT": "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",
        },
    },
    "ink": {
        "network": "Ink",
        "gecko": "ink",
        "dexscreener": None,
        "rpc": "https://rpc-gel.inkonchain.com",
        # Ink 上的池子是 Reservoir v3，标准地址的 Uniswap QuoterV2 在 Ink 上没有实现
        # （代码长度 2110 字节，报价调用返回空）。改用 slot0 + liquidity 自己算价。
        "quote_via": "slot0",
        # 2026-10-10：Ink 不再逐个问工厂。价走 xChange 软报价。
        "pool_source": "xchange",
        "factory": "0x640887a9ba3a9c53ed27d0f7e8246a4f933f3424",
        "factory_dex": "reservoir-v3",
        "probe_fees": [500, 3000],
        "default_quoter": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        "quoters": {},
        "stables": {
            "USDG": "0xe343167631d89b6ffc58b88d6b7fb0228795491d",
            "USDC": "0x2d270e6886d130d724215a266106e6832161eaed",
        },
    },
    "mantle": {
        "network": "Mantle",
        "gecko": "mantle",
        "dexscreener": None,
        "rpc": "https://rpc.mantle.xyz",
        "default_quoter": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        "quoters": {},
        "stables": {},
    },
    "hyperevm": {
        "network": "HyperEVM",
        "gecko": "hyperevm",
        "dexscreener": "hyperliquid",
        "rpc": "https://rpc.hyperliquid.xyz/evm",
        "default_quoter": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        "quoters": {},
        "stables": {},
    },
    "ton": {
        "network": "TON",
        "gecko": "ton",
        "dexscreener": None,
        # TON 不逐代币问 Gecko：ston.fi 一次就给全部池子和储备。
        "pool_source": "stonfi",
        # TON 不是 EVM：没有 eth_call，也不能用 Uniswap Quoter 报价。
        # 池子（ston.fi 等）靠 GeckoTerminal 发现，报价适配器还没实现。
        "kind": "ton",
        "rpc": None,
        "default_quoter": None,
        "quoters": {},
        "stables": {
            # 发行方登记表里 TON 的可兑稳定币是 USD₮；符号匹配本身也认 USD₮。
            "USDT": "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
        },
    },
}
CENSUS_CHAINS = list(CHAINS)


def _decimal(value) -> Decimal | None:
    try:
        if value in (None, ""):
            return None
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _addr(value: str) -> str:
    text = str(value or "").strip()
    return text.lower() if text.lower().startswith("0x") else text


def is_stable_symbol(symbol: str) -> bool:
    text = str(symbol or "").upper().replace("\u20ae", "T").strip()
    return text in STABLES or text.startswith("USD")


def fee_from_name(name: str) -> Decimal | None:
    """Pool names carry the fee, e.g. ``mstrx / USDT 0.25%``."""
    found = re.findall(r"(\d+(?:\.\d+)?)\s*%", name or "")
    if not found:
        return None
    return _decimal(found[-1]) / Decimal(100) if _decimal(found[-1]) is not None else None


def is_trap(pool: dict) -> str:
    """Return the reason a pool is a trap, or "" when it is not one.

    The 88% / 89% pools are hook fees that GeckoTerminal shows in the pool
    name. A $1 quote through them lands far above the stock price.
    """
    labels = " ".join(str(x) for x in (pool.get("labels") or []))
    dex = f"{pool.get('dex') or ''} {labels}".lower()
    if "v4" in dex:
        # Do not call every v4 pool a fee trap: many v4 pools have no hook at
        # all. They are out of scope for the v3 quote path and belong in the
        # separate read-only v4 watch.
        return "uniswap v4 池，交给独立 v4 watch；不进入 v3 主报价"
    fee = pool.get("fee_hint")
    if fee is not None and fee >= Decimal("0.5"):
        return f"池名标出的费率 {fee * 100:.0f}% 是陷阱"
    return ""


def normalize_dexscreener(pair: dict) -> dict:
    """DexScreener pair -> the shape the Gecko parser also returns."""
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    liquidity = pair.get("liquidity") or {}
    volume = pair.get("volume") or {}
    return {
        "pool_address": _addr(pair.get("pairAddress")),
        "dex": str(pair.get("dexId") or ""),
        "labels": list(pair.get("labels") or []),
        "pool_name": f"{base.get('symbol') or ''} / {quote.get('symbol') or ''}".strip(),
        "base_address": _addr(base.get("address")),
        "base_symbol": str(base.get("symbol") or ""),
        "quote_address": _addr(quote.get("address")),
        "quote_symbol": str(quote.get("symbol") or ""),
        "depth_usd": _decimal(liquidity.get("usd")),
        "volume_h24": _decimal(volume.get("h24")),
        "fee_hint": None,
        "source": "dexscreener",
    }


def normalize_gecko(pool: dict) -> dict:
    attr = pool.get("attributes") or {}
    rel = pool.get("relationships") or {}
    dex = ((rel.get("dex") or {}).get("data") or {}).get("id") or ""
    name = str(attr.get("name") or "")
    base_sym = quote_sym = ""
    if " / " in name:
        left, right = name.split(" / ", 1)
        base_sym = left.strip()
        quote_sym = right.split()[0].strip()

    def side_address(side: str) -> str:
        ident = ((rel.get(side) or {}).get("data") or {}).get("id") or ""
        # Gecko 的 id 形如 "bsc_0xabc" / "ton_EQ...". TON 地址本身含 "-" 和 "_"，
        # 所以只能按第一个下划线切，不能用 rsplit。
        return _addr(ident.split("_", 1)[1] if "_" in ident else ident)

    volume = attr.get("volume_usd") or {}
    return {
        "pool_address": _addr(attr.get("address")),
        "dex": str(dex),
        "labels": [str(dex).split("-")[-1]] if dex else [],
        "pool_name": name,
        "base_address": side_address("base_token"),
        "base_symbol": base_sym,
        "quote_address": side_address("quote_token"),
        "quote_symbol": quote_sym,
        "depth_usd": _decimal(attr.get("reserve_in_usd")),
        "volume_h24": _decimal(volume.get("h24")) if isinstance(volume, dict) else None,
        "fee_hint": fee_from_name(name),
        "source": "geckoterminal",
    }


def other_side(pool: dict, token: str) -> tuple[str, str] | None:
    ours = _addr(token)
    base, quote = _addr(pool["base_address"]), _addr(pool["quote_address"])
    if ours == base:
        return quote, pool["quote_symbol"]
    if ours == quote:
        return base, pool["base_symbol"]
    return None


def pool_rejection(pool: dict, token: str, chain: dict, min_depth: Decimal) -> str:
    """Why this pool cannot be a candidate, or "" when it can."""
    trap = is_trap(pool)
    if trap:
        return trap
    evm_chain = chain.get("kind") != "ton"
    if evm_chain and not pool["pool_address"].startswith("0x"):
        return "池地址不是 EVM 地址"
    side = other_side(pool, token)
    if side is None:
        return "池子两侧都不是这个代币"
    other_addr, other_sym = side
    if other_addr == _addr(token):
        return "池子两侧是同一个代币"
    if evm_chain and not other_addr.startswith("0x"):
        return "对手方不是 EVM 地址"
    known = {addr.lower(): sym for sym, addr in (chain.get("stables") or {}).items() if addr}
    if not is_stable_symbol(other_sym) and other_addr.lower() not in known:
        return f"对手方 {other_sym or other_addr[:10]} 不是已知稳定币"
    depth = pool.get("depth_usd")
    if depth is None:
        return "没有深度数据"
    if depth < min_depth:
        return f"深度 {depth:.2f} USD 低于门槛 {min_depth}"
    return ""


def pick_candidates(pools: list[dict], token: str, chain_key: str,
                    min_depth: Decimal | None = None) -> dict:
    """Split measured pools into candidates, traps and rejections."""
    chain = CHAINS[chain_key]
    floor = DEFAULT_MIN_DEPTH if min_depth is None else min_depth
    candidates: list[dict] = []
    traps: list[dict] = []
    rejected: list[dict] = []
    for pool in pools:
        trap = is_trap(pool)
        if trap:
            traps.append({**pool, "why": trap})
            continue
        why = pool_rejection(pool, token, chain, floor)
        if why:
            rejected.append({**pool, "why": why})
            continue
        candidates.append(pool)
    candidates.sort(key=lambda row: row.get("depth_usd") or Decimal(0), reverse=True)
    return {"candidates": candidates, "traps": traps, "rejected": rejected}


def quoter_for(dex: str, chain: dict) -> str:
    text = (dex or "").lower()
    for needle, address in (chain.get("quoters") or {}).items():
        if needle in text:
            return address
    return chain.get("default_quoter") or ""


DS_CHAIN_KEYS = {
    str(cfg["dexscreener"]): key
    for key, cfg in CHAINS.items() if cfg.get("dexscreener")
}


def gecko_targets(key: str, rows: list[dict], covered: set[str]) -> list[dict]:
    """Addresses a Gecko walk still has to ask.

    X Layer keeps the measured watchlist, so those addresses are not asked
    again. Every other address on that chain still is. The watchlist is a
    sample of pools already traded, not the list of tokens that exist.
    """
    if key != "x-layer":
        return list(rows)
    return [row for row in rows if _addr(row["address"]) not in covered]


def xlayer_watchlist_pools(path: Path = DEPTH_WATCHLIST) -> tuple[list[dict], str]:
    """X Layer pools already measured by the repo's own RPC depth census.

    Reusing that file is better than re-reading Gecko: the measurement is a
    pool-balance read on X Layer itself, and it covers the wrapper pools the
    basis monitor actually trades. Returns (pools, scanned_at).
    """
    if not Path(path).exists():
        return [], ""
    try:
        document = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return [], ""
    chain = CHAINS["x-layer"]
    pools: list[dict] = []
    for row in document.get("with_depth") or []:
        pool = _addr(row.get("pool"))
        token = _addr(row.get("base_address"))
        quote_symbol = str(row.get("quote_symbol") or "")
        stable = (chain.get("stables") or {}).get(quote_symbol.upper())
        if not pool.startswith("0x") or not token.startswith("0x"):
            continue
        pools.append({
            "pool_address": pool,
            "dex": "uniswap-v3-x-layer",
            "labels": ["v3"],
            "pool_name": f"{row.get('symbol') or ''} / {quote_symbol}".strip(),
            "base_address": token,
            "base_symbol": str(row.get("symbol") or ""),
            "quote_address": _addr(stable) if stable else "",
            "quote_symbol": quote_symbol,
            "depth_usd": _decimal(row.get("depth_usd")),
            "volume_h24": None,
            "fee_hint": None,
            "source": "xlayer_depth_watchlist",
        })
    scanned = document.get("scanned_at_ms")
    stamp = ""
    if scanned:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(scanned) / 1000))
    return pools, stamp


STONFI_POOLS = "https://api.ston.fi/v1/pools"
SELECTOR_GET_POOL = "0x1698ee82"
SELECTOR_SLOT0 = "0x3850c7bd"
SELECTOR_LIQUIDITY = "0x1a686502"
SELECTOR_TOKEN0 = "0x0dfe1681"
SELECTOR_BALANCE_OF = "0x70a08231"


def _word(value: str) -> str:
    return value[2:].lower().rjust(64, "0")


def _uint(hex_result: str | None) -> int | None:
    if not hex_result or hex_result == "0x":
        return None
    try:
        return int(hex_result, 16)
    except ValueError:
        return None


def factory_pools(chain_key: str, tokens: list[dict], stables: dict[str, str],
                  fees: list[int]) -> tuple[dict[str, list[dict]], dict[str, int]]:
    """Discover v3-style pools by asking the factory for known pairs.

    Ink's Reservoir v3 is not on GeckoTerminal's good side (rate limits), but
    ``factory.getPool(token, stable, fee)`` answers in about a second and is
    exact. Depth is then measured from the pool's own balances, so a discovered
    pool still has to clear the depth gate.
    """
    chain = CHAINS[chain_key]
    factory = chain.get("factory") or ""
    found: dict[str, list[dict]] = {}
    stats = {"probes": 0, "errors": 0, "pools": 0}
    if not factory:
        return found, stats
    # Ask the factory in JSON-RPC batches. The sweep is O(tokens × stables ×
    # fees) -- ~20k calls for BSC alone -- and one round trip per call is what
    # made it look unrunnable, which is how BSC came to have no factory at all.
    plan: list[tuple[dict, str, str, int]] = []
    for row in tokens:
        token = row["address"]
        if not str(token).startswith("0x"):
            continue
        for stable_symbol, stable in (stables or {}).items():
            if not str(stable).startswith("0x"):
                continue
            for fee in fees:
                plan.append((row, stable_symbol, stable, fee))
    stats["probes"] = len(plan)
    batch_max = max(1, int((CHAINS.get(chain_key) or {}).get("batch_max") or 20))
    results: list[str | None] = []
    for start in range(0, len(plan), batch_max):
        chunk = plan[start:start + batch_max]
        results += rpc_batch(chain_key, [
            (factory, SELECTOR_GET_POOL + _word(row["address"])
             + _word(stable) + _word(hex(fee)))
            for row, _stable_symbol, stable, fee in chunk])
    for (row, stable_symbol, stable, fee), result in zip(plan, results):
        token = row["address"]
        if result is None:
            stats["errors"] += 1
            continue
        if int(result, 16) == 0:
            continue
        pool_address = "0x" + result[-40:]
        depth = pool_depth_usd(chain_key, pool_address, token, stable,
                               read_decimals(chain_key, token),
                               read_decimals(chain_key, stable))
        if depth is None:
            stats["errors"] += 1
            continue
        if depth <= 0:
            # Zero active liquidity: a pool that cannot execute a swap is not a
            # pool for our purposes (found on BSC 2026-10-09).
            continue
        stats["pools"] += 1
        found.setdefault(token, []).append({
            "pool_address": pool_address,
            # The dex label decides which quoter the scan uses (``quoter_for``
            # matches on a substring of ``chain["quoters"]``), so it may not be
            # hardcoded: BSC's pools are Pancake, Ink's are Reservoir.
            "dex": str((CHAINS.get(chain_key) or {}).get("factory_dex")
                       or "reservoir-v3"),
            "labels": ["v3"],
            "pool_name": f"{row.get('symbol') or ''} / {stable_symbol} {fee / 10000:g}%",
            "base_address": token,
            "base_symbol": str(row.get("symbol") or ""),
            "quote_address": stable,
            "quote_symbol": stable_symbol,
            "depth_usd": depth,
            "volume_h24": None,
            "fee_hint": Decimal(fee) / Decimal(1_000_000),
            "source": "factory",
        })
    return found, stats


def pool_depth_usd(chain_key: str, pool_address: str, token: str, stable: str,
                   dec_token: int, dec_stable: int) -> Decimal | None:
    """Measured depth of a v3 pool: stable balance plus token balance at spot.

    A pool whose **active liquidity is zero** cannot execute a swap at any size:
    its ``slot0`` price is whatever the last (possibly dust) trade left, so
    ``token_balance × that price`` is a made-up number. Measured 2026-10-09 on
    BSC: the SPCXx/USDT 0.25% pool held 0.00066 SPCXx + 0.09 USDT with
    ``liquidity() == 0`` and a garbage price, and this function called it
    **$1,120,621,080** deep. Zero liquidity is a fact about tradability, so it is
    answered with 0 here rather than with a price artifact.
    """
    liquidity = _uint(rpc(chain_key, pool_address, SELECTOR_LIQUIDITY))
    if liquidity == 0:
        return Decimal(0)
    slot0 = rpc(chain_key, pool_address, SELECTOR_SLOT0)
    token0 = rpc(chain_key, pool_address, SELECTOR_TOKEN0)
    if not slot0 or not token0 or len(slot0) < 66:
        return None
    sqrt_price = _uint(slot0[2:66])
    if not sqrt_price:
        return None
    bal_token = _uint(rpc(chain_key, token, SELECTOR_BALANCE_OF + _word(pool_address)))
    bal_stable = _uint(rpc(chain_key, stable, SELECTOR_BALANCE_OF + _word(pool_address)))
    if bal_token is None or bal_stable is None:
        return None
    t0 = "0x" + token0[-40:]
    base_is_token0 = t0.lower() == token.lower()
    with_dp = Decimal(10) ** (dec_token - dec_stable)
    price = (Decimal(sqrt_price) / (Decimal(2) ** 96)) ** 2
    price_token_in_stable = price * with_dp if base_is_token0 else with_dp / price
    token_side = (Decimal(bal_token) / Decimal(10) ** dec_token) * price_token_in_stable
    stable_side = Decimal(bal_stable) / Decimal(10) ** dec_stable
    return token_side + stable_side


def stonfi_pools_usd(addresses: set[str]) -> tuple[dict[str, list[dict]], str]:
    """TON pools straight from ston.fi, keyed by the xStock jetton address.

    One call returns every pool (48k+) with its reserves, so there is no need
    to ask GeckoTerminal once per token. USD depth is ``lp_total_supply_usd``.
    """
    status, body = get(STONFI_POOLS, timeout=90, attempts=2)
    if status != 200:
        return {}, f"stonfi_http_{status}"
    try:
        rows = json.loads(body).get("pool_list") or []
    except json.JSONDecodeError:
        return {}, "stonfi_bad_json"
    out: dict[str, list[dict]] = {}
    for pool in rows:
        t0 = str(pool.get("token0_address") or "")
        t1 = str(pool.get("token1_address") or "")
        ours, other = ("", "")
        if t0 in addresses:
            ours, other = t0, t1
        elif t1 in addresses:
            ours, other = t1, t0
        else:
            continue
        fee_bps = _decimal(pool.get("lp_fee")) or Decimal(0)
        out.setdefault(ours, []).append({
            "pool_address": str(pool.get("address") or ""),
            "dex": "stonfi-v2",
            "labels": [],
            "pool_name": f"{ours[:6]}… / {other[:6]}…",
            "base_address": ours,
            "base_symbol": "",
            "quote_address": other,
            "quote_symbol": "",
            "depth_usd": _decimal(pool.get("lp_total_supply_usd")),
            "volume_h24": None,
            # ston.fi 的 lp_fee 是万分之几（20 = 0.2%）。
            "fee_hint": fee_bps / Decimal(10000),
            "source": "stonfi",
        })
    return out, ""


def get(url: str, timeout: int = 25, attempts: int = 3) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url, headers={"User-Agent": "arb-monitor/0.1", "Accept": "application/json"})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"unreachable {url}: {last}")


def rpc(chain_key: str, to: str, data: str) -> str | None:
    url = (CHAINS.get(chain_key) or {}).get("rpc")
    if not url:
        return None
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": to, "data": data}, "latest"]}).encode()
    request = urllib.request.Request(
        url, data=body,
        # 部分 RPC（以太坊 publicnode）对没有 UA 的请求直接断流。
        headers={"Content-Type": "application/json", "User-Agent": "arb-monitor/0.1"})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    try:
        payload = json.loads(opener.open(request, timeout=25).read())
    except Exception:
        return None
    if payload.get("error"):
        return None
    return payload.get("result")


def rpc_batch(chain_key: str, calls: list[tuple[str, str]]) -> list[str | None]:
    """``eth_call`` in one JSON-RPC batch. ``None`` per call that did not answer.

    The factory sweep is O(tokens × stables × fees): BSC alone is ~1,271 names ×
    2 forms × 2 stables × 4 fee tiers ≈ 20k calls, and one HTTP round trip each
    is what made the on-chain sweep look "too slow to run" (which is how BSC
    ended up with no factory configured at all). A batch is 2 orders of
    magnitude fewer round trips; the result order is matched by ``id``, not by
    position, because RPCs are free to reorder.
    """
    url = (CHAINS.get(chain_key) or {}).get("rpc")
    if not url or not calls:
        return []
    out: list[str | None] = [None] * len(calls)
    body = json.dumps([
        {"jsonrpc": "2.0", "id": index, "method": "eth_call",
         "params": [{"to": to, "data": data}, "latest"]}
        for index, (to, data) in enumerate(calls)]).encode()
    request = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "arb-monitor/0.1"})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    try:
        payload = json.loads(opener.open(request, timeout=60).read())
    except Exception:  # noqa: BLE001 - a batch that does not answer is unknown
        return out
    items = payload if isinstance(payload, list) else [payload]
    for item in items:
        if not isinstance(item, dict) or item.get("error"):
            continue
        index = item.get("id")
        if isinstance(index, int) and 0 <= index < len(out):
            result = item.get("result")
            out[index] = result if isinstance(result, str) else None
    return out


def read_fee(chain_key: str, pool_address: str) -> Decimal | None:
    # 代理偶发 SSL 断流：一次失败不等于"读不到费率"，重试两次再放弃。
    for attempt in range(3):
        result = rpc(chain_key, pool_address, "0xddca3f43")
        if result and result != "0x":
            try:
                return Decimal(int(result, 16)) / Decimal(1_000_000)
            except (ValueError, InvalidOperation):
                return None
        if attempt < 2:
            time.sleep(1.5)
    return None


_decimals_cache: dict[tuple[str, str], int] = {}
_stonfi_decimals_cache: dict[str, int | None] = {}


def stonfi_decimals(address: str, timeout: int = 20) -> int | None:
    """Jetton decimals from the ston.fi asset endpoint (TON only)."""
    if address in _stonfi_decimals_cache:
        return _stonfi_decimals_cache[address]
    status, body = get(f"https://api.ston.fi/v1/assets/{address}", timeout=timeout, attempts=2)
    value: int | None = None
    if status == 200:
        try:
            raw = (json.loads(body).get("asset") or {}).get("decimals")
            value = int(raw) if raw is not None else None
        except (json.JSONDecodeError, TypeError, ValueError):
            value = None
    _stonfi_decimals_cache[address] = value
    return value


def read_decimals(chain_key: str, token: str) -> int:
    key = (chain_key, _addr(token))
    if key in _decimals_cache:
        return _decimals_cache[key]
    result = rpc(chain_key, token, "0x313ce567") if token.startswith("0x") else None
    try:
        value = int(result, 16) if result and result != "0x" else 18
    except ValueError:
        value = 18
    if not 0 <= value <= 36:
        value = 18
    _decimals_cache[key] = value
    return value


def ds_pairs(addresses: list[str], pause: float = 0.3) -> dict[str, list[dict]]:
    """All DexScreener pairs, keyed by the lowercase token address queried."""
    found: dict[str, list[dict]] = {}
    for start in range(0, len(addresses), DS_BATCH):
        batch = addresses[start:start + DS_BATCH]
        url = DS_API + "/" + ",".join(urllib.parse.quote(a, safe="") for a in batch)
        status, body = get(url)
        if status == 429:
            time.sleep(20)
            status, body = get(url)
        if status != 200:
            print(f"dexscreener http_{status} for {len(batch)} addresses", flush=True)
            continue
        for pair in (json.loads(body).get("pairs") or []):
            for side in ("baseToken", "quoteToken"):
                address = _addr((pair.get(side) or {}).get("address"))
                if address in batch:
                    found.setdefault(address, []).append(pair)
        time.sleep(pause)
    return found


def gecko_pools(slug: str, address: str) -> tuple[list[dict], str]:
    url = (f"{GECKO_API}/{slug}/tokens/{urllib.parse.quote(address, safe='')}/pools"
           "?include=base_token,quote_token")
    status, body = get(url)
    for _ in range(2):
        if status != 429:
            break
        # 免费档是 30 次/分钟。429 必须当成「没测到」，不能当成「没有池子」——
        # 2026-10-03 之前的看板就是因为把 429 当空池，才在整轮里丢掉全部池子。
        time.sleep(30)
        status, body = get(url)
    if status == 404:
        return [], "gecko_404"
    if status == 429:
        return [], "gecko_429"
    if status != 200:
        return [], f"gecko_http_{status}"
    try:
        return (json.loads(body).get("data") or []), ""
    except json.JSONDecodeError:
        return [], "gecko_bad_json"


def load_universe(conn: sqlite3.Connection, chains: list[str],
                  symbols: list[str] | None = None) -> dict[str, list[dict]]:
    """Issuer symbols -> their deployments on the census chains.

    Default scope is every issuer symbol still in the catalog. A name with no
    Kraken pair is still a ``dex_to_dex`` or ``chain_to_chain`` candidate.
    Limiting the walk to ``spot_exits`` was the old exit-through-Kraken rule,
    and it left about a thousand issued tokens unmeasured.

    ``symbols`` still narrows one run to a named list.
    """
    if symbols:
        wanted_symbols = sorted({str(item) for item in symbols if str(item)})
    else:
        wanted_symbols = [row[0] for row in conn.execute(
            "select issuer_symbol from underlyings "
            "where missing_since_ms is null and issuer_symbol is not null "
            "and issuer_symbol != ''")]
    # Deliberately *not* named ``symbols``-in-place: the caller passes the scope
    # through this function and a same-named local used to shadow it.
    universe_symbols = wanted_symbols
    info = {
        row[0]: {"ticker": row[1], "listing_country": row[2]}
        for row in conn.execute(
            "select issuer_symbol, underlying_symbol, listing_country from underlyings")
    }
    wanted = {CHAINS[key]["network"]: key for key in chains}
    out: dict[str, list[dict]] = {key: [] for key in chains}
    seen: set[tuple[str, str]] = set()
    marks = ",".join("?" * len(universe_symbols))
    rows = conn.execute(
        f"select issuer_symbol, network, address, form from deployments "
        f"where issuer_symbol in ({marks}) and missing_since_ms is null",
        universe_symbols)
    for symbol, network, address, form in rows:
        key = wanted.get(network)
        if key is None or not address:
            continue
        if (key, _addr(address)) in seen:
            continue
        seen.add((key, _addr(address)))
        out[key].append({
            "symbol": symbol,
            "ticker": (info.get(symbol) or {}).get("ticker"),
            "form": "wrapper" if str(form).startswith("wrapper") else "原生",
            "address": address,
        })
    for key in out:
        out[key].sort(key=lambda row: (row["symbol"], row["form"]))
    return out


def resolve_route(chain_key: str, token_row: dict, pool: dict) -> tuple[dict | None, str]:
    """Turn a measured pool into a route the board can quote."""
    chain = CHAINS[chain_key]
    side = other_side(pool, token_row["address"])
    if side is None:
        return None, "池子两侧都不是这个代币"
    stable, stable_symbol = side
    if chain.get("kind") == "ton":
        # TON 的询价走 ston.fi REST（POST /v1/swap/simulate），不经 eth_call。
        dec_token = stonfi_decimals(token_row["address"])
        dec_stable = stonfi_decimals(stable)
        if dec_token is None or dec_stable is None:
            return None, "ston.fi 查不到精度"
        return {
            "symbol": token_row["symbol"],
            "ticker": token_row["ticker"],
            "network": chain["network"],
            "slug": chain_key,
            "gecko": chain.get("gecko"),
            "kind": "ton",
            "venue": pool.get("dex") or "ston.fi",
            "form": token_row["form"],
            "token": token_row["address"],
            "stable": stable,
            "stable_symbol": stable_symbol,
            "pool_name": pool["pool_name"] or f"{token_row['symbol']} / {stable_symbol}",
            "pool_address": pool["pool_address"],
            "fee": format(pool.get("fee_hint") or Decimal("0"), "f"),
            "v2": True,
            "dec_stable": dec_stable,
            "dec_token": dec_token,
            "quoter": "",
            "dex": pool.get("dex"),
            "depth_usd": format(pool["depth_usd"], "f"),
            "volume_h24": None if pool.get("volume_h24") is None
            else format(pool["volume_h24"], "f"),
            "source": pool["source"],
            "explorer_url": (
                f"https://www.geckoterminal.com/{chain['gecko']}/pools/"
                f"{pool['pool_address']}" if chain.get("gecko") else ""
            ),
        }, ""
    if pool["source"] == "dexscreener":
        fee = read_fee(chain_key, pool["pool_address"])
        if fee is None:
            return None, "读不到链上费率"
    else:
        fee = pool.get("fee_hint")
        if fee is None:
            fee = read_fee(chain_key, pool["pool_address"])
        if fee is None:
            return None, "读不到链上费率，池名里也没有"
    dex_text = str(pool.get("dex") or "").lower()
    v2 = "v2" in dex_text and "v3" not in dex_text
    if v2:
        fee = pool.get("fee_hint") or Decimal("0.003")
    dec_token = read_decimals(chain_key, token_row["address"])
    dec_stable = read_decimals(chain_key, stable)
    gecko = chain.get("gecko")
    ds_id = chain.get("dexscreener")
    if ds_id:
        explorer = f"https://dexscreener.com/{ds_id}/{pool['pool_address']}"
    elif gecko:
        explorer = f"https://www.geckoterminal.com/{gecko}/pools/{pool['pool_address']}"
    else:
        explorer = ""
    return {
        "symbol": token_row["symbol"],
        "ticker": token_row["ticker"],
        "network": chain["network"],
        "slug": chain_key,
        "gecko": gecko,
        "form": token_row["form"],
        "token": token_row["address"],
        "pool_name": pool["pool_name"] or f"{token_row['symbol']} / {stable_symbol}",
        "pool_address": pool["pool_address"],
        "fee": format(fee, "f"),
        "stable": stable,
        "stable_symbol": stable_symbol,
        "v2": v2,
        "dec_stable": dec_stable,
        "dec_token": dec_token,
        "quoter": quoter_for(pool.get("dex", ""), chain),
        "quote_via": chain.get("quote_via") or "quoter",
        "dex": pool.get("dex"),
        "depth_usd": format(pool["depth_usd"], "f"),
        "volume_h24": None if pool.get("volume_h24") is None else format(pool["volume_h24"], "f"),
        "source": pool["source"],
        "explorer_url": explorer,
    }, ""


def census(chains: list[str], *, min_depth: Decimal = DEFAULT_MIN_DEPTH,
           limit_per_chain: int | None = None, top_per_chain: int = 0,
           gecko_cross_check: bool = False, pause_gecko: float = 2.1,
           db: Path = BOOK, out: Path = OUT, now: str | None = None,
           merge: bool = True, forms: str = "both",
           symbols: list[str] | None = None) -> dict:
    conn = sqlite3.connect(db)
    try:
        universe = load_universe(conn, chains, symbols)
    finally:
        conn.close()

    scoped: dict[str, list[dict]] = {}
    for key in chains:
        rows = universe.get(key) or []
        if forms == "wrapper":
            rows = [row for row in rows if row["form"] == "wrapper"]
        elif forms == "native":
            rows = [row for row in rows if row["form"] != "wrapper"]
        scoped[key] = rows[:limit_per_chain] if limit_per_chain else rows

    pools_by_chain: dict[str, dict[str, list[dict]]] = {key: {} for key in chains}
    sources: dict[str, str] = {}

    ds_chains = [key for key in chains if CHAINS[key].get("dexscreener")]
    ds_wanted: dict[str, set[str]] = {}
    for key in ds_chains:
        for row in scoped[key]:
            ds_wanted.setdefault(_addr(row["address"]), set()).add(key)
    if ds_wanted:
        print(f"dexscreener {len(ds_wanted)} addresses in "
              f"{(len(ds_wanted) + DS_BATCH - 1) // DS_BATCH} calls", flush=True)
        pairs = ds_pairs(sorted(ds_wanted))
        for address, chain_keys in ds_wanted.items():
            for pair in pairs.get(address, []):
                key = DS_CHAIN_KEYS.get(str(pair.get("chainId") or ""))
                if key in chain_keys:
                    pools_by_chain[key].setdefault(address, []).append(
                        normalize_dexscreener(pair))
    for key in ds_chains:
        sources[key] = "dexscreener"

    watchlist_stamp = ""
    if "x-layer" in chains:
        pools, watchlist_stamp = xlayer_watchlist_pools()
        if pools:
            for pool in pools:
                pools_by_chain["x-layer"].setdefault(pool["base_address"], []).append(pool)
            sources["x-layer"] = "xlayer_depth_watchlist"
            print(f"xlayer depth watchlist {len(pools)} pools "
                  f"(measured {watchlist_stamp or 'unknown'})", flush=True)

    if "ton" in chains:
        # Named for what it is: this used to be called ``symbols`` and shadowed
        # the ``symbols`` scope argument of ``census()``, so every run took the
        # targeted-merge branch (2026-10-09).
        ton_by_address = {_addr(row["address"]): row for row in scoped["ton"]}
        ton_pools, why = stonfi_pools_usd(set(ton_by_address))
        if why:
            print(f"ston.fi pools failed: {why}", flush=True)
        stable_symbols = {_addr(addr): sym
                          for sym, addr in (CHAINS["ton"].get("stables") or {}).items() if addr}
        for address, rows in ton_pools.items():
            for pool in rows:
                pool["base_symbol"] = ((ton_by_address.get(address) or {}).get("symbol")
                                       or pool["base_symbol"])
                pool["quote_symbol"] = stable_symbols.get(pool["quote_address"], "")
                if pool["base_symbol"] and pool["quote_symbol"]:
                    pool["pool_name"] = f"{pool['base_symbol']} / {pool['quote_symbol']}"
                pools_by_chain["ton"].setdefault(address, []).append(pool)
        if ton_pools:
            sources["ton"] = "stonfi"
            print(f"ston.fi {sum(len(v) for v in ton_pools.values())} pools for "
                  f"{len(ton_pools)} jettons", flush=True)

    factory_stats: dict[str, dict] = {}
    for key in chains:
        if CHAINS[key].get("pool_source") != "factory":
            continue
        rows = scoped[key]
        print(f"factory probe {key} {len(rows)} tokens", flush=True)
        found, stats = factory_pools(key, rows, CHAINS[key].get("stables") or {},
                                     CHAINS[key].get("probe_fees") or [500])
        for address, pools in found.items():
            pools_by_chain[key].setdefault(address, []).extend(pools)
        factory_stats[key] = stats
        sources[key] = "factory"
        print(f"factory {key} probes={stats['probes']} pools={stats['pools']} "
              f"errors={stats['errors']}", flush=True)

    gecko_chains = [
        key for key in chains
        if CHAINS[key].get("gecko")
        and (CHAINS[key].get("dexscreener") is None or gecko_cross_check)
        and CHAINS[key].get("pool_source") not in ("stonfi", "factory", "xchange")
    ]
    gecko_failures: dict[str, dict[str, int]] = {}
    for key in gecko_chains:
        slug = CHAINS[key]["gecko"]
        targets = gecko_targets(key, scoped[key], set(pools_by_chain.get(key) or {}))
        if not targets:
            continue
        print(f"geckoterminal {key} {len(targets)} tokens", flush=True)
        failures = {"unmeasured": 0, "unindexed": 0}
        for index, row in enumerate(targets, start=1):
            pools, why = gecko_pools(slug, row["address"])
            if why == "gecko_404":
                failures["unindexed"] += 1
            elif why:
                failures["unmeasured"] += 1
                print(f"  {row['symbol']} {why}", flush=True)
                continue
            for pool in pools:
                pools_by_chain[key].setdefault(_addr(row["address"]), []).append(
                    normalize_gecko(pool))
            if index % 25 == 0:
                print(f"  {index}/{len(scoped[key])}", flush=True)
            time.sleep(pause_gecko)
        gecko_failures[key] = failures
        sources[key] = (sources.get(key, "") + "+geckoterminal").lstrip("+")

    summary: dict[str, dict] = {}
    candidates: list[dict] = []
    traps: list[dict] = []
    rejected: list[dict] = []
    unresolved: list[dict] = []
    for key in chains:
        rows = scoped[key]
        per_chain: list[dict] = []
        pools_seen = traps_n = 0
        deepest = Decimal(0)
        deepest_candidate = Decimal(0)
        below_gate = 0
        other_rejected = 0
        for row in rows:
            pools = pools_by_chain[key].get(_addr(row["address"])) or []
            pools_seen += len(pools)
            for pool in pools:
                depth = pool.get("depth_usd")
                if depth is not None and not is_trap(pool):
                    deepest = max(deepest, depth)
            picked = pick_candidates(pools, row["address"], key, min_depth)
            traps_n += len(picked["traps"])
            for trap in picked["traps"]:
                traps.append({"chain": key, "symbol": row["symbol"], "form": row["form"],
                              "pool_name": trap["pool_name"],
                              "dex": trap.get("dex"),
                              "pool_address": trap.get("pool_address"),
                              "quote_symbol": trap.get("quote_symbol"),
                              "volume_h24": None if trap.get("volume_h24") is None
                              else format(trap["volume_h24"], "f"),
                              "depth_usd":
                              None if trap.get("depth_usd") is None else format(trap["depth_usd"], "f"),
                              "why": trap["why"]})
            if not picked["candidates"]:
                for row_rejected in picked["rejected"]:
                    if "低于门槛" in row_rejected["why"]:
                        below_gate += 1
                    else:
                        other_rejected += 1
                    if len(rejected) < 60:
                        rejected.append({
                            "chain": key, "symbol": row["symbol"], "form": row["form"],
                            "pool_name": row_rejected["pool_name"],
                            "depth_usd": None if row_rejected.get("depth_usd") is None
                            else format(row_rejected["depth_usd"], "f"),
                            "why": row_rejected["why"],
                        })
                continue
            best = picked["candidates"][0]
            depth = best.get("depth_usd") or Decimal(0)
            deepest_candidate = max(deepest_candidate, depth)
            route, why = resolve_route(key, row, best)
            if route is None:
                unresolved.append({"chain": key, "symbol": row["symbol"],
                                   "pool_name": best["pool_name"], "why": why})
                continue
            per_chain.append(route)
        per_chain.sort(key=lambda route: Decimal(route["depth_usd"]), reverse=True)
        # ``0`` (the default since 2026-10-10) means "keep every pool that
        # passed the gate". It used to be 3, which quietly capped the *scan*:
        # ``census_pools()`` reads ``candidates`` and nothing else, so X Layer's
        # 38 pools with depth reached the board as **3** -- and the fewer the
        # pools, the fewer the chances, for no reason anyone had written down
        # (operator 2026-10-10: 「为什么每轮监控的数量这么少呢？被哪里拦住了」).
        # A cap is a sampling device, so it stays available by flag; it is no
        # longer the default, because it silently became a coverage limit.
        kept = keep_per_chain(per_chain, top_per_chain)
        candidates.extend(kept)
        summary[key] = {
            "network": CHAINS[key]["network"],
            "source": sources.get(key, ""),
            "source_scanned_at": watchlist_stamp if key == "x-layer" and pools_by_chain[key] else "",
            "kraken_deployments_in_scope": len(rows),
            # The names actually looked at, not just how many. Without this a
            # symbol that was scanned and had no pool is indistinguishable from
            # one that was never in scope (2026-10-09, FLWSx).
            "symbols_in_scope": sorted({row["symbol"] for row in rows}),
            "probed": len(rows),
            "unmeasured": (gecko_failures.get(key) or {}).get("unmeasured", 0),
            "unindexed": (gecko_failures.get(key) or {}).get("unindexed", 0),
            "pools_seen": pools_seen,
            "pools_with_depth": len(per_chain),
            "traps": traps_n,
            "below_gate": below_gate,
            "other_rejected": other_rejected,
            "deepest_measured_usd": format(deepest, "f"),
            "deepest_candidate_usd": format(deepest_candidate, "f"),
            "candidates": [route["symbol"] for route in kept],
        }
        top = kept[0] if kept else None
        note = gecko_failures.get(key) or {}
        print(f"{key:10s} deployed={len(rows):4d} pools={pools_seen:3d} "
              f"deep={len(per_chain):3d} traps={traps_n:2d} below_gate={below_gate:3d} "
              f"other_rejected={other_rejected:3d} "
              f"deepest_measured=${deepest:>10.2f} "
              f"unmeasured={note.get('unmeasured', 0)} unindexed={note.get('unindexed', 0)} "
              f"best={'' if top is None else top['symbol']}", flush=True)

    payload = {
        "generated_at": now or time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "min_depth_usd": format(min_depth, "f"),
        "chains": summary,
        "candidates": candidates,
        "traps": traps,
        "rejected": rejected,
        "unresolved": unresolved,
        "note": (
            "默认扫发行方目录里的全部名字，不要求有 Kraken 交易对；--symbols 只扫指定名字"
            "（两者都写在同一份文件里，定向扫描只替换它扫过的那几个符号）。"
            "候选池必须对手方是稳定币、"
            f"实测深度 ≥ {min_depth} USD、不是 v4 池、费率 < 50%，"
            "并且链上能读到费率和精度。表上没有的链不是没查，是没有合格池子。"
        ),
    }
    if merge and out and Path(out).exists():
        # A86: say what this run is entitled to overwrite *before* it does it.
        # One line, on stdout, so the runner's tail keeps it as evidence.
        report = merge_report(payload, Path(out), chains, symbols)
        print("merge: " + json.dumps(report, ensure_ascii=False), flush=True)
        payload["merge_report"] = report
        payload = merge_payload(payload, Path(out), chains, symbols)
    if out:
        tmp = Path(str(out) + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
        tmp.replace(out)
    return payload


def _scoped_rows(old_rows, fresh_rows, scope: set[str]) -> list[dict]:
    """Replace the rows of the names we looked at; keep every other row."""
    kept = [row for row in (old_rows or [])
            if str((row or {}).get("symbol") or "") not in scope]
    return kept + list(fresh_rows or [])


#: The four row lists a census payload carries. A merge replaces rows in these,
#: and nothing else, so they are what the report below counts.
ROW_LISTS = ("candidates", "traps", "rejected", "unresolved")


def keep_per_chain(routes: list[dict], top_per_chain) -> list[dict]:
    """How many of one chain's pools reach the scan.

    ``0`` (the default since 2026-10-10) keeps every pool that passed the
    depth/route gate. The old default of 3 was an undocumented sampling cap
    that became a silent coverage limit: the scan quotes ``candidates`` only,
    so X Layer's 38 pools with depth arrived as 3.
    """
    if int(top_per_chain or 0) <= 0:
        return list(routes)
    return list(routes[:int(top_per_chain)])


#: Which field names the owner of a row in a **full** merge. ``candidates`` is
#: keyed by chain slug, the other three by chain -- the same asymmetry
#: ``merge_payload`` already has, spelled out so the report cannot drift from
#: the thing it describes.
FULL_SCOPE_KEY = {"candidates": "slug", "traps": "chain",
                  "rejected": "chain", "unresolved": "chain"}


def merge_report(fresh: dict, path: Path, chains: list[str],
                 symbols: list[str] | None = None) -> dict:
    """What the merge is about to **replace**, counted before it happens.

    Measured 2026-10-09 (A86): the first targeted run (``--symbols FLWSx``)
    named all nine chains, the chain-level ownership rule treated every chain
    as touched, and 24 candidate pools measured that afternoon were overwritten
    by a run that had looked at one token. ``data/`` is gitignored, so there
    was no version to roll back to -- the recovery was re-scanning every chain.

    The check that would have caught it is one line long: how many rows this
    run is entitled to drop. A targeted run may only drop the names it named,
    so ``kept_total`` stays large and ``replaced`` is small; a full sweep
    replaces a whole chain's rows on purpose.
    """
    try:
        old = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {"mode": "fresh", "replaced": {}, "replaced_total": 0,
                "kept_total": 0}
    if symbols:
        mode, scope = "targeted", {str(item) for item in symbols}
        keys = {name: "symbol" for name in ROW_LISTS}
        owned_by = scope
    else:
        mode, keys = "full", dict(FULL_SCOPE_KEY)
        owned_by = {str(item) for item in chains}
    replaced = {name: sum(1 for row in (old.get(name) or [])
                          if str((row or {}).get(keys[name]) or "") in owned_by)
                for name in ROW_LISTS}
    kept = {name: sum(1 for row in (old.get(name) or [])
                      if str((row or {}).get(keys[name]) or "") not in owned_by)
            for name in ROW_LISTS}
    return {"mode": mode,
            "symbols": sorted(scope) if symbols else None,
            "chains": None if symbols else sorted(owned_by),
            "replaced": replaced,
            "replaced_total": sum(replaced.values()),
            "kept_total": sum(kept.values())}


def merge_payload(fresh: dict, path: Path, chains: list[str],
                  symbols: list[str] | None = None) -> dict:
    """Keep what this run did not measure.

    Two kinds of run, two rules:

    * a **full** run owns the chains it names, so a chain it did not scan keeps
      its old numbers (a sweep is slow: GeckoTerminal is rate limited);
    * a **targeted** run (``--symbols``) owns only those names, so every other
      symbol's rows are kept and only the targeted ones are replaced.

    The second rule exists because the first targeted run (2026-10-09, FLWSx)
    named all nine chains: the chain-level rule treated every chain as owned by
    a scan that had looked at one token, and wiped the 24 candidates measured
    that afternoon.
    """
    try:
        old = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return fresh
    merged = {
        **old,
        **fresh,
        "chains": {**(old.get("chains") or {}), **(fresh.get("chains") or {})},
    }
    if symbols:
        scope = {str(item) for item in symbols}
        for name in ("candidates", "traps", "rejected", "unresolved"):
            merged[name] = _scoped_rows(old.get(name), fresh.get(name), scope)
        for key, fresh_chain in (fresh.get("chains") or {}).items():
            old_chain = (old.get("chains") or {}).get(key) or {}
            # Counts describe the wider scan; only the name list grows.
            summary = {**fresh_chain, **old_chain}
            summary["symbols_in_scope"] = sorted(
                set(old_chain.get("symbols_in_scope") or [])
                | set(fresh_chain.get("symbols_in_scope") or []))
            summary["partial"] = True
            merged["chains"][key] = summary
        return merged
    touched = set(chains)
    merged = {
        **merged,
        "candidates": [row for row in (old.get("candidates") or [])
                       if row.get("slug") not in touched]
                      + list(fresh.get("candidates") or []),
        "traps": [row for row in (old.get("traps") or []) if row.get("chain") not in touched]
                 + list(fresh.get("traps") or []),
        "rejected": [row for row in (old.get("rejected") or []) if row.get("chain") not in touched]
                    + list(fresh.get("rejected") or []),
        "unresolved": [row for row in (old.get("unresolved") or []) if row.get("chain") not in touched]
                      + list(fresh.get("unresolved") or []),
    }
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chains", default=",".join(CENSUS_CHAINS),
                        help="comma separated: " + ", ".join(CENSUS_CHAINS))
    parser.add_argument("--min-depth", default=str(DEFAULT_MIN_DEPTH))
    parser.add_argument("--limit-per-chain", type=int, default=None)
    parser.add_argument("--top-per-chain", type=int, default=0,
                        help="keep at most this many pools per chain "
                             "(0 = keep every pool that passed the gate; the "
                             "scan only quotes what this writes down)")
    parser.add_argument("--gecko-cross-check", action="store_true",
                        help="also read GeckoTerminal for chains DexScreener covers")
    parser.add_argument("--pause-gecko", type=float, default=2.1)
    parser.add_argument("--forms", choices=("both", "native", "wrapper"), default="both",
                        help="只扫某一类部署（Ink/X Layer 的池子基本都在 wrapper 上）")
    parser.add_argument("--db", default=str(BOOK))
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--no-write", action="store_true",
                        help="print the table only, keep the JSON file untouched")
    parser.add_argument("--no-merge", action="store_true",
                        help="overwrite the JSON instead of keeping the chains this run skipped")
    parser.add_argument("--symbols", default=None,
                        help="comma separated: scan only these issuer symbols "
                             "(a new listing costs a few calls, not a chain sweep)")
    args = parser.parse_args(argv)
    chains = [item.strip() for item in args.chains.split(",") if item.strip()]
    unknown = [item for item in chains if item not in CHAINS]
    if unknown:
        parser.error("unknown chains: " + ", ".join(unknown))
    census(
        chains,
        min_depth=Decimal(args.min_depth),
        limit_per_chain=args.limit_per_chain,
        top_per_chain=args.top_per_chain,
        gecko_cross_check=args.gecko_cross_check,
        pause_gecko=args.pause_gecko,
        db=Path(args.db),
        out=None if args.no_write else Path(args.out),
        merge=not args.no_merge,
        forms=args.forms,
        symbols=[item.strip() for item in (args.symbols or "").split(",") if item.strip()]
        or None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
