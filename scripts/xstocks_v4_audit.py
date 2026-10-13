#!/usr/bin/env python3
"""Quote every Uniswap v4 pool on the cheap-token list, hook included.

The pool fee in the name is not the cost of a buy. This script reads the
pool key (tokens, fee, tick spacing, hook) from the Initialize event, reads
the live lpFee, then calls the official V4Quoter so beforeSwap runs. A pool
is cheap only when that quoted price is still below the stock reference and
the hook bytecode does not special-case the quoter.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from Crypto.Hash import keccak

ROOT = Path(__file__).resolve().parents[1]
CHEAP = ROOT / "data" / "xstocks_cheap_depth.json"
CACHE = ROOT / "data" / "xstocks_v4_pool_cache.json"
OUT = ROOT / "data" / "xstocks_v4_audit.json"
PROXY = "http://127.0.0.1:4780"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
def _topic(signature: bytes) -> str:
    digest = keccak.new(digest_bits=256)
    digest.update(signature)
    return "0x" + digest.hexdigest()


TOPIC0 = _topic(b"Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")
QUOTE_SELECTOR = "aa9d21cb"
SIZES = [Decimal(1), Decimal(5), Decimal(20), Decimal(100), Decimal(500)]
STABLES = {"USDC", "USDT", "USDG", "USD1", "USDT0", "DAI", "USDE", "PYUSD", "FDUSD", "USDS"}
ZERO = "0x" + "0" * 40

# Official Uniswap v4 addresses:
# https://developers.uniswap.org/docs/protocols/v4/deployments
CHAINS = {
    "bsc": {
        "rpc": "https://bsc.publicnode.com",
        "manager": "0x28e2ea090877bf75740558f6bfb36a5ffee9e9df",
        "state": "0xd13dd3d6e93f276fafc9db9e6bb47c1180aee0c4",
        "quoter": "0x9f75dd27d6664c475b90e105573e550ff69437b0",
        "position": "0x7a4a5c919ae2541aed11041a1aeee68f1287f95b",
        "block_time": Decimal("0.75"),
    },
    "eth": {
        "rpc": "https://ethereum.publicnode.com",
        "manager": "0x000000000004444c5dc75cb358380d2e3de08a90",
        "state": "0x7ffe42c4a5deea5b0fec41c94c136cf115597227",
        "quoter": "0x52f0e24d1c21c8a0cb1e5a5dd6198556bd9e1203",
        "position": "0xbd216513d74c8cf14cf4747e6aaa6420ff64ee9e",
        "block_time": Decimal(12),
    },
    "arbitrum": {
        "rpc": "https://arbitrum-one.publicnode.com",
        "manager": "0x360e68faccca8ca495c1b759fd9eee466db9fb32",
        "state": "0x76fd297e2d437cd7f76d50f01afe6160f86e9990",
        "quoter": "0x3972c00f7ed4885e145823eb7c655375d275a1c5",
        "position": "0xd88f38f930b7952f2db2432cb002e7abbf3dd869",
        "block_time": Decimal("0.25"),
    },
    "x-layer": {
        "rpc": "https://rpc.xlayer.tech",
        "manager": "0x360e68faccca8ca495c1b759fd9eee466db9fb32",
        "state": "0x76fd297e2d437cd7f76d50f01afe6160f86e9990",
        "quoter": "0x8928074ca1b241d8ec02815881c1af11e8bc5219",
        "position": "0xcf1eafc6928dc385a342e7c6491d371d2871458b",
        "block_time": Decimal("0.5"),
    },
    "ink": {
        "rpc": "https://rpc-gel.inkonchain.com",
        "manager": "0x360e68faccca8ca495c1b759fd9eee466db9fb32",
        "state": "0x76fd297e2d437cd7f76d50f01afe6160f86e9990",
        "quoter": "0x3972c00f7ed4885e145823eb7c655375d275a1c5",
        "position": "0x1b35d13a2e2528f192637f14b05f0dc0e7deb566",
        "block_time": Decimal(1),
    },
}
GECKO = {
    "ethereum": "eth",
    "solana": "solana",
    "arbitrum": "arbitrum",
    "bnbsmartchain": "bsc",
    "xlayer": "x-layer",
    "ink": "ink",
    "hyperevm": "hyperevm",
    "ton": "ton",
}
HEADS: dict[str, tuple[int, int]] = {}


def net_key(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def http_json(url: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 30):
    req = urllib.request.Request(url, data=data, headers=headers or {"User-Agent": "xstocks-book/0.1", "Accept": "application/json"})
    last = None
    for attempt in range(4):
        try:
            with OPENER.open(req, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()
            if exc.code in (403, 429, 502, 503):
                time.sleep(2 * (attempt + 1))
                last = exc
                continue
            return exc.code, body
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(str(last))


def rpc(url: str, method: str, params: list):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    status, raw = http_json(url, body, {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}, timeout=40)
    if status != 200:
        raise RuntimeError(f"{method} http {status}")
    payload = json.loads(raw)
    if payload.get("error"):
        raise RuntimeError(str(payload["error"])[:240])
    return payload["result"]


def call(url: str, to: str, data: str):
    try:
        return rpc(url, "eth_call", [{"to": to, "data": data}, "latest"]), None
    except RuntimeError as exc:
        return None, str(exc)


def head(slug: str) -> tuple[int, int]:
    if slug not in HEADS:
        chain = CHAINS[slug]
        number = int(rpc(chain["rpc"], "eth_blockNumber", []), 16)
        block = rpc(chain["rpc"], "eth_getBlockByNumber", [hex(number), False])
        HEADS[slug] = (number, int(block["timestamp"], 16))
    return HEADS[slug]


def key_from_position_manager(slug: str, pool_id: str) -> dict | None:
    """Official PositionManager stores the pool key, including the hook."""
    chain = CHAINS[slug]
    # bytes25 is the high 25 bytes of the pool id, left-aligned in the word.
    data = "0x86b6be7d" + pool_id.lower().replace("0x", "")[:50].ljust(64, "0")
    result, _err = call(chain["rpc"], chain["position"], data)
    if not result or len(result) < 2 + 64 * 5:
        return None
    words = [int(result[2 + 64 * i:2 + 64 * (i + 1)], 16) for i in range(5)]
    if words[0] == 0 and words[1] == 0:
        return None
    spacing = words[3]
    if spacing >= 2 ** 255:
        spacing -= 2 ** 256
    return {
        "currency0": "0x" + format(words[0], "064x")[-40:],
        "currency1": "0x" + format(words[1], "064x")[-40:],
        "fee": words[2],
        "tick_spacing": spacing,
        "hooks": "0x" + format(words[4], "064x")[-40:],
    }


def find_initialize(slug: str, pool_id: str, created_at: str | None) -> dict | None:
    chain = CHAINS[slug]
    latest, latest_ts = head(slug)
    if created_at:
        target = int(datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp())
        guess = latest - int((latest_ts - target) / chain["block_time"])
    else:
        guess = latest - 200_000
    guess = max(1, min(latest, guess))
    # Two hours, then twelve if Gecko's timestamp is late.
    for hours in (2, 12):
        span = int(Decimal(hours * 3600) / chain["block_time"])
        start = max(1, guess - span)
        stop = min(latest, guess + span)
        cursor = start
        while cursor <= stop:
            end = min(cursor + 3999, stop)
            logs = rpc(chain["rpc"], "eth_getLogs", [{
                "fromBlock": hex(cursor),
                "toBlock": hex(end),
                "address": chain["manager"],
                "topics": [TOPIC0, pool_id],
            }])
            if logs:
                return logs[0]
            cursor = end + 1
    return None


def decode_key(log: dict) -> dict:
    topics = log["topics"]
    raw = log["data"][2:]
    words = [int(raw[i:i + 64], 16) for i in range(0, len(raw), 64)]
    spacing = words[1]
    if spacing >= 2 ** 255:
        spacing -= 2 ** 256
    return {
        "currency0": "0x" + topics[2][-40:],
        "currency1": "0x" + topics[3][-40:],
        "fee": words[0],
        "tick_spacing": spacing,
        "hooks": "0x" + format(words[2], "064x")[-40:],
    }


def slot0(slug: str, pool_id: str) -> dict | None:
    chain = CHAINS[slug]
    result, err = call(chain["rpc"], chain["state"], "0xc815641c" + pool_id[2:].rjust(64, "0"))
    if not result or result == "0x" or len(result) < 2 + 64 * 4:
        return None
    words = [int(result[2 + 64 * i:2 + 64 * (i + 1)], 16) for i in range(4)]
    tick = words[1]
    if tick >= 2 ** 255:
        tick -= 2 ** 256
    return {"sqrt": words[0], "tick": tick, "protocol_fee": words[2], "lp_fee": words[3]}


def decimals(slug: str, token: str) -> int:
    if token == ZERO:
        return 18
    result, _ = call(CHAINS[slug]["rpc"], token, "0x313ce567")
    if not result:
        return 18
    return int(result, 16)


def spot_price(sqrt: int, dec0: int, dec1: int, token_is_one: bool) -> Decimal:
    ratio = (Decimal(sqrt) / (Decimal(2) ** 96)) ** 2  # raw token1 per raw token0
    if token_is_one:
        # human token0 paid for one human token1
        if ratio == 0:
            return Decimal(0)
        return (Decimal(10) ** (dec1 - dec0)) / ratio
    return ratio * (Decimal(10) ** (dec0 - dec1))


def encode_quote(key: dict, zero_for_one: bool, amount: int) -> str:
    def word(value: int) -> str:
        return format(value & (2 ** 256 - 1), "064x")

    def addr(value: str) -> str:
        return value[2:].lower().rjust(64, "0")

    parts = [
        word(0x20),
        addr(key["currency0"]),
        addr(key["currency1"]),
        word(key["fee"]),
        word(key["tick_spacing"]),
        addr(key["hooks"]),
        word(1 if zero_for_one else 0),
        word(amount),
        word(0x100),
        word(0),
    ]
    return "0x" + QUOTE_SELECTOR + "".join(parts)


def quote(slug: str, key: dict, zero_for_one: bool, amount: int) -> tuple[int | None, str | None]:
    chain = CHAINS[slug]
    result, err = call(chain["rpc"], chain["quoter"], encode_quote(key, zero_for_one, amount))
    if not result or result == "0x" or len(result) < 66:
        return None, err or "empty"
    return int(result[2:66], 16), None


def hook_flags(slug: str, hooks: str) -> list[str]:
    if hooks.lower() == ZERO:
        return []
    code = rpc(CHAINS[slug]["rpc"], "eth_getCode", [hooks, "latest"])
    if not code or code == "0x":
        return ["hook 没有合约代码"]
    raw = bytes.fromhex(code[2:])
    flags = []
    quoters = {chain["quoter"][2:].lower() for chain in CHAINS.values()}
    hay = code[2:].lower()
    if any(item in hay for item in quoters):
        flags.append("字节码里写了报价合约地址")
    # Walk opcodes so a 0x32 inside PUSH data is not treated as ORIGIN.
    i = 0
    origin = False
    while i < len(raw):
        op = raw[i]
        if op == 0x32:
            origin = True
            break
        if 0x60 <= op <= 0x7F:
            i += 1 + (op - 0x5F)
        else:
            i += 1
    if origin:
        flags.append("字节码使用 ORIGIN，能分辨真实交易和模拟")
    return flags


def is_stable(symbol: str) -> bool:
    text = (symbol or "").upper().replace("₮", "T")
    return text in STABLES or text.startswith("USD")


def fee_pct(fee: int) -> str:
    return format(Decimal(fee) / Decimal(10000), "f")


def gecko_pools(slug: str, address: str, cache: dict) -> list:
    key = f"{slug}:{address.lower()}"
    if key in cache:
        return cache[key]
    url = f"https://api.geckoterminal.com/api/v2/networks/{slug}/tokens/{address}/pools?include=base_token,quote_token"
    status, body = http_json(url)
    if status == 429:
        time.sleep(15)
        status, body = http_json(url)
    if status != 200:
        cache[key] = []
        return []
    payload = json.loads(body)
    included = {}
    for item in payload.get("included") or []:
        included[item.get("id")] = item
    pools = []
    for pool in payload.get("data") or []:
        attr = pool.get("attributes") or {}
        rel = pool.get("relationships") or {}
        dex = ((rel.get("dex") or {}).get("data") or {}).get("id") or ""
        base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
        quote_id = ((rel.get("quote_token") or {}).get("data") or {}).get("id") or ""
        base = included.get(base_id) or {}
        quote = included.get(quote_id) or {}
        pools.append({
            "dex": dex,
            "name": attr.get("name"),
            "address": attr.get("address"),
            "created": attr.get("pool_created_at"),
            "reserve": attr.get("reserve_in_usd"),
            "base_price": attr.get("base_token_price_usd"),
            "quote_price": attr.get("quote_token_price_usd"),
            "base": (base.get("attributes") or {}).get("address"),
            "quote": (quote.get("attributes") or {}).get("address"),
            "base_symbol": (base.get("attributes") or {}).get("symbol") or "",
            "quote_symbol": (quote.get("attributes") or {}).get("symbol") or "",
        })
    cache[key] = pools
    CACHE.write_text(json.dumps(cache))
    time.sleep(0.6)
    return pools


def audit_pool(row: dict, slug: str, pool: dict) -> dict:
    pool_id = str(pool.get("address") or "")
    if not pool_id.startswith("0x") or len(pool_id) != 66:
        pool_id = "0x" + pool_id.lower().replace("0x", "").rjust(64, "0")
    result = {
        "symbol": row["symbol"],
        "ticker": row["ticker"],
        "network": row["network"],
        "form": row["form"],
        "token": row["address"],
        "kraken": row["kraken_status"],
        "multiplier": row["multiplier"],
        "ref_usd": row["ref_usd"],
        "ref_kind": row["ref_kind"],
        "ref_label": row["ref_label"],
        "dex": pool.get("dex"),
        "pool": pool.get("name"),
        "pool_id": pool_id,
        "reserve": pool.get("reserve"),
        "verdict": "",
        "still_cheap": False,
    }
    state = slot0(slug, pool_id)
    if state:
        result["lp_fee"] = state["lp_fee"]
        result["lp_fee_pct"] = fee_pct(state["lp_fee"])
        result["protocol_fee_word"] = state["protocol_fee"]
    key = key_from_position_manager(slug, pool_id)
    if key is None:
        try:
            log = find_initialize(slug, pool_id, pool.get("created"))
        except Exception as exc:
            result["verdict"] = f"PositionManager 没有这口池，日志也查失败：{exc}"
            return result
        if not log:
            result["verdict"] = "官方头寸合约没有这口池的 hook，不能当成便宜"
            return result
        key = decode_key(log)
    result["hooks"] = key["hooks"]
    result["fee_key"] = key["fee"]
    result["fee_key_pct"] = "动态" if key["fee"] == 0x800000 else fee_pct(key["fee"])
    result["tick_spacing"] = key["tick_spacing"]
    result["flags"] = hook_flags(slug, key["hooks"])
    our = row["address"].lower()
    c0, c1 = key["currency0"].lower(), key["currency1"].lower()
    if our == c1:
        token_is_one = True
        stable = key["currency0"]
        stable_symbol = pool.get("base_symbol") if (pool.get("base") or "").lower() == c0 else pool.get("quote_symbol")
    elif our == c0:
        token_is_one = False
        stable = key["currency1"]
        stable_symbol = pool.get("base_symbol") if (pool.get("base") or "").lower() == c1 else pool.get("quote_symbol")
    else:
        result["verdict"] = "池子两边都不是这个代币"
        return result
    result["stable"] = stable_symbol
    if not is_stable(stable_symbol or ""):
        result["verdict"] = "对手方不是稳定币，没有按美元买入"
        return result
    dec_token = decimals(slug, row["address"])
    dec_stable = decimals(slug, stable)
    if state and state["sqrt"]:
        spot = spot_price(state["sqrt"], decimals(slug, key["currency0"]), decimals(slug, key["currency1"]), token_is_one)
        result["spot_token_usd"] = format(spot, "f")
    else:
        spot = None
    zero_for_one = token_is_one  # paying currency0 to receive currency1
    if not token_is_one:
        zero_for_one = False
    ref = Decimal(row["ref_usd"])
    mult = Decimal(row["multiplier"])
    quotes = []
    best = None
    breaks = None
    for size in SIZES:
        raw_in = int(size * (Decimal(10) ** dec_stable))
        out, err = quote(slug, key, zero_for_one, raw_in)
        if not out:
            breaks = format(size, "f")
            result["quote_error"] = err
            break
        got = Decimal(out) / (Decimal(10) ** dec_token)
        if got <= 0:
            breaks = format(size, "f")
            break
        share = (size / got) / mult
        implied = None
        if spot and spot > 0:
            implied = (size / got) / spot - 1
        item = {
            "usd": format(size, "f"),
            "share": format(share, "f"),
            "implied_fee": None if implied is None else format(implied, "f"),
        }
        quotes.append(item)
        if share >= ref or share > ref * 20 or share * 20 < ref:
            breaks = format(size, "f")
            break
        best = item
    result["quotes"] = quotes
    result["breaks_at_usd"] = breaks or ""
    if result["flags"]:
        result["verdict"] = "hook 会识别模拟，询价不能当作成交价"
        return result
    if best is None:
        shown = quotes[-1]["share"] if quotes else "无成交"
        result["verdict"] = f"询价后不便宜（{shown}）"
        return result
    share = Decimal(best["share"])
    gap = (share - ref) / ref * Decimal(10000)
    result["quoted_spend_usd"] = best["usd"]
    result["quoted_share_usd"] = best["share"]
    result["gap_bps"] = format(gap, "f")
    result["still_cheap"] = True
    result["verdict"] = "询价含 hook 后仍便宜"
    return result


def main() -> None:
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    rows = json.loads(CHEAP.read_text())
    done = []
    seen = set()
    for index, row in enumerate(rows):
        slug = GECKO.get(net_key(row["network"]))
        if slug not in CHAINS:
            if slug:
                try:
                    pools = gecko_pools(slug, row["address"], cache)
                except Exception:
                    pools = []
                extra = [pool for pool in pools if "v4" in str(pool.get("dex") or "").lower()]
                for pool in extra:
                    done.append({
                        "symbol": row["symbol"],
                        "network": row["network"],
                        "pool": pool.get("name"),
                        "dex": pool.get("dex"),
                        "verdict": "这条链没有官方 Uniswap v4 报价器，不能当成便宜",
                        "still_cheap": False,
                    })
                    print("no quoter", row["symbol"], row["network"], pool.get("name"), flush=True)
            continue
        try:
            pools = gecko_pools(slug, row["address"], cache)
        except Exception as exc:
            print("gecko fail", row["symbol"], row["network"], exc, flush=True)
            continue
        v4 = [pool for pool in pools if "v4" in str(pool.get("dex") or "").lower()]
        for pool in v4:
            ident = f"{slug}:{pool.get('address')}"
            if ident in seen:
                continue
            seen.add(ident)
            try:
                item = audit_pool(row, slug, pool)
            except Exception as exc:
                item = {"symbol": row["symbol"], "network": row["network"], "pool": pool.get("name"), "verdict": f"异常：{exc}", "still_cheap": False}
            done.append(item)
            print(
                f"{item.get('symbol')} {item.get('network')} {item.get('pool')} "
                f"lp={item.get('lp_fee_pct')} hook={str(item.get('hooks') or '')[:10]} "
                f"{item.get('verdict')} flags={item.get('flags')}",
                flush=True,
            )
            OUT.write_text(json.dumps({"rows": done, "partial": True}, ensure_ascii=False))
        if (index + 1) % 10 == 0:
            print(f"progress {index + 1}/{len(rows)} v4 {len(done)}", flush=True)
    OUT.write_text(json.dumps({"rows": done, "partial": False, "v4_pools": len(done)}, ensure_ascii=False))
    cheap = [item for item in done if item.get("still_cheap")]
    print(f"wrote {OUT} pools {len(done)} still_cheap {len(cheap)}", flush=True)


if __name__ == "__main__":
    main()
