#!/usr/bin/env python3
"""Re-score cheap xStock pools after swap fees and a real quote.

GeckoTerminal's token-level price is not used. A pool only survives if a
quoted buy, fee included, is still below the OKX perp or the stock close.
Uniswap v4 pools are left out: hook fees are not in the pool fee.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHEAP = ROOT / "data" / "xstocks_cheap_depth.json"
OUT = ROOT / "data" / "xstocks_requote.json"
PROXY = "http://127.0.0.1:4780"
GECKO = {
    "ethereum": "eth", "solana": "solana", "ton": "ton", "arbitrum": "arbitrum",
    "optimism": "optimism", "bnbsmartchain": "bsc", "mantle": "mantle",
    "xlayer": "x-layer", "hyperevm": "hyperevm", "ink": "ink", "tron": "tron",
    # GeckoTerminal 没有 Monad 网络；Monad 的池子来自 DexScreener 普查，
    # 这里只登记 RPC 与 Quoter，不登记 slug。
}
RPC = {
    # ethereum.publicnode.com 经本机代理大量 SSL 断流；这个域名实测可用。
    "eth": "https://ethereum-rpc.publicnode.com",
    "bsc": "https://bsc-dataseed.binance.org",
    "arbitrum": "https://arb1.arbitrum.io/rpc",
    # mainnet.optimism.io 经本机代理握手失败；publicnode 实测可用。
    "optimism": "https://optimism-rpc.publicnode.com",
    "x-layer": "https://rpc.xlayer.tech",
    "ink": "https://rpc-gel.inkonchain.com",
    "hyperevm": "https://rpc.hyperliquid.xyz/evm",
    "mantle": "https://rpc.mantle.xyz",
    "monad": "https://rpc.monad.xyz",
}
# Uniswap QuoterV2 is the same CREATE2 address on the main EVM chains.
# Pancake's quoter is separate. v4 is not quoted here.
QUOTER = {
    "eth": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
    "arbitrum": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
    "optimism": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
    "bsc": "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997",
    # X Layer QuoterV2 is not the Ethereum CREATE2 address.
    # https://developers.uniswap.org/docs/protocols/v3/deployments/v3-xlayer-deployments
    "x-layer": "0xd1b797d92d87b688193a2b976efc8d577d204343",
    "ink": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
    "mantle": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
    "hyperevm": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
    # Monad v3 不是 CREATE2 同址部署，用官方 Monad 部署表里的 QuoterV2。
    "monad": "0x661e93cca42afacb172121ef892830ca3b70f08d",
}
SOL_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
STABLES = {"USDC", "USDT", "USDG", "USD1", "USDT0", "DAI", "USDE", "PYUSD", "FDUSD", "USDS"}
SIZES = [Decimal(1), Decimal(5), Decimal(20), Decimal(100), Decimal(500)]


def net_key(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def gecko_slug(network: str) -> str | None:
    return GECKO.get(net_key(network))


def http_get(url: str, timeout: int = 25) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "xstocks-book/0.1", "Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    last: Exception | None = None
    for attempt in range(3):
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(str(last))


def rpc(slug: str, to: str, data: str) -> str | None:
    url = RPC.get(slug)
    if not url:
        return None
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": to, "data": data}, "latest"]}).encode()
    request = urllib.request.Request(
        url, data=body,
        # 部分 RPC 对没有 UA 的请求直接断流。
        headers={"Content-Type": "application/json", "User-Agent": "arb-monitor/0.1"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    try:
        payload = json.loads(opener.open(request, timeout=20).read())
    except Exception:
        return None
    if payload.get("error"):
        return None
    return payload.get("result")


def parse_fee(name: str, dex: str) -> Decimal | None:
    import re
    found = re.findall(r"(\d+(?:\.\d+)?)\s*%", name or "")
    if found:
        return Decimal(found[-1]) / Decimal(100)
    dex_l = (dex or "").lower()
    if "v4" in dex_l:
        return None
    if "v2" in dex_l and "pancake" in dex_l:
        return Decimal("0.0025")
    if "v2" in dex_l:
        return Decimal("0.003")
    return None


def is_stable(symbol: str) -> bool:
    text = (symbol or "").upper().replace("₮", "T")
    return text in STABLES or text.startswith("USD")


def addr_tail(value: str) -> str:
    text = value or ""
    if "_" in text:
        text = text.split("_")[-1]
    return text


def our_price(pool: dict, our: str) -> tuple[Decimal | None, str, str, str]:
    """Return price of our token, stable symbol, stable address, side."""
    rel = pool.get("relationships") or {}
    base = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
    quote = ((rel.get("quote_token") or {}).get("data") or {}).get("id") or ""
    attr = pool.get("attributes") or {}
    our_l = our.lower()
    base_l = addr_tail(base).lower()
    quote_l = addr_tail(quote).lower()
    name = str(attr.get("name") or "")
    base_sym, quote_sym = "", ""
    if " / " in name:
        left, right = name.split(" / ", 1)
        base_sym = left.strip()
        quote_sym = right.split()[0].strip()
    if base_l == our_l:
        raw = attr.get("base_token_price_usd") or attr.get("token_price_usd")
        side = "base"
        stable_sym, stable_addr = quote_sym, addr_tail(quote)
    elif quote_l == our_l:
        raw = attr.get("quote_token_price_usd") or attr.get("token_price_usd")
        side = "quote"
        stable_sym, stable_addr = base_sym, addr_tail(base)
    else:
        return None, "", "", ""
    try:
        price = Decimal(str(raw))
    except Exception:
        return None, "", "", ""
    if price <= 0:
        return None, "", "", ""
    return price, stable_sym, stable_addr, side


def quote_v3(slug: str, stable: str, token: str, fee: Decimal, amount_raw: int,
             quoter: str | None = None) -> int | None:
    # 多链普查会给每条路由带上自己的 Quoter（例如 BSC 上 Pancake 与 Uniswap
    # 的 QuoterV2 不是同一个合约）。
    quoter = quoter or QUOTER.get(slug)
    if not quoter or not stable.startswith("0x"):
        return None
    fee_units = int(fee * Decimal(1_000_000))
    data = (
        "0xc6a5026a"
        + stable[2:].lower().rjust(64, "0")
        + token[2:].lower().rjust(64, "0")
        + hex(amount_raw)[2:].rjust(64, "0")
        + hex(fee_units)[2:].rjust(64, "0")
        + hex(0)[2:].rjust(64, "0")
    )
    result = rpc(slug, quoter, data)
    if not result or result == "0x":
        return None
    word = result[2:66]
    try:
        return int(word, 16)
    except ValueError:
        return None


def decimals_of(slug: str, token: str) -> int:
    if not token.startswith("0x"):
        return 6 if slug == "solana" else 18
    result = rpc(slug, token, "0x313ce567")
    if not result:
        return 18
    return int(result, 16)


def v2_out(slug: str, pool: str, stable: str, token: str, fee: Decimal, amount_raw: int) -> int | None:
    reserves = rpc(slug, pool, "0x0902f1ac")
    token0 = rpc(slug, pool, "0x0dfe1681")
    if not reserves or not token0 or len(reserves) < 2 + 64 * 2:
        return None
    r0 = int(reserves[2:66], 16)
    r1 = int(reserves[66:130], 16)
    t0 = "0x" + token0[-40:]
    if t0.lower() == stable.lower():
        reserve_in, reserve_out = r0, r1
    elif t0.lower() == token.lower():
        reserve_in, reserve_out = r1, r0
    else:
        return None
    if reserve_in <= 0 or reserve_out <= 0:
        return None
    # fee is a fraction, e.g. 0.0025. Keep 1e6 precision.
    fee_ppm = int(fee * Decimal(1_000_000))
    amount_in_after = amount_raw * (1_000_000 - fee_ppm)
    out = amount_in_after * reserve_out // (reserve_in * 1_000_000 + amount_in_after)
    return out


def jupiter(output_mint: str, usdc_raw: int) -> tuple[int, int, str] | None:
    url = (
        "https://lite-api.jup.ag/swap/v1/quote"
        f"?inputMint={SOL_USDC}&outputMint={output_mint}&amount={usdc_raw}&slippageBps=50"
    )
    try:
        status, body = http_get(url, timeout=20)
    except Exception:
        return None
    if status != 200:
        return None
    payload = json.loads(body)
    labels = []
    for step in payload.get("routePlan") or []:
        info = step.get("swapInfo") or {}
        label = str(info.get("label") or "")
        if label and label not in labels:
            labels.append(label)
    try:
        return int(payload["inAmount"]), int(payload["outAmount"]), " → ".join(labels[:4])
    except (KeyError, TypeError, ValueError):
        return None


def sol_decimals(mint: str) -> int:
    # xStocks SPL tokens are 8 decimals in the issuer bridge table. Jupiter
    # amounts are raw, so a wrong decimal would make the price absurd; 8 is
    # the issuer's svmDecimals for these assets.
    return 8


def better(left: dict, right: dict) -> bool:
    """Prefer the pool that still clears more dollars, then the wider gap."""
    spend_l = Decimal(left["quoted_spend_usd"])
    spend_r = Decimal(right["quoted_spend_usd"])
    if spend_l != spend_r:
        return spend_l > spend_r
    return Decimal(left["gap_bps"]) < Decimal(right["gap_bps"])


def sane_share(share: Decimal, ref: Decimal) -> bool:
    return ref / Decimal(20) <= share <= ref * Decimal(20)


def main() -> None:
    dest = Path(os.environ.get("XSTOCKS_REQUOTE_OUT", str(OUT)))
    cheap = json.loads(CHEAP.read_text())
    kept = []
    skipped_v4 = 0
    checked_pools = 0
    for index, row in enumerate(cheap):
        slug = gecko_slug(row["network"]) or row.get("gecko")
        if not slug or slug == "solana":
            continue
        mult = Decimal(row["multiplier"])
        ref = Decimal(row["ref_usd"])
        best = None
        if slug == "solana":
            dec = sol_decimals(row["address"])
            route = ""
            winner = None
            breaks_at = None
            for size in SIZES:
                quoted = jupiter(row["address"], int(size * Decimal(10) ** 6))
                time.sleep(0.2)
                if not quoted:
                    breaks_at = size
                    break
                inn, out, route = quoted
                if out <= 0:
                    breaks_at = size
                    break
                spent = Decimal(inn) / Decimal(10) ** 6
                got = Decimal(out) / Decimal(10) ** dec
                if got <= 0:
                    breaks_at = size
                    break
                share = (spent / got) / mult
                if not sane_share(share, ref) or share >= ref:
                    breaks_at = size
                    break
                gap = (share - ref) / ref * Decimal(10000)
                winner = (spent, share, gap)
            if winner is not None and winner[0] >= Decimal(1):
                spent, share, gap = winner
                best = {
                    "symbol": row["symbol"],
                    "ticker": row["ticker"],
                    "network": row["network"],
                    "form": row["form"],
                    "address": row["address"],
                    "dex": "jupiter",
                    "pool": route or "Jupiter",
                    "fee": "路径内含",
                    "spot_token_usd": "",
                    "quoted_spend_usd": format(spent, "f"),
                    "breaks_at_usd": "" if breaks_at is None else format(breaks_at, "f"),
                    "quoted_share_usd": format(share, "f"),
                    "ref_usd": format(ref, "f"),
                    "ref_kind": row["ref_kind"],
                    "ref_label": row["ref_label"],
                    "gap_bps": format(gap, "f"),
                    "quote_via": "jupiter",
                    "kraken": row["kraken_status"],
                    "multiplier": row["multiplier"],
                    "stable": "USDC",
                }
        else:
            url = (
                f"https://api.geckoterminal.com/api/v2/networks/{slug}/tokens/{row['address']}/pools"
                "?include=base_token,quote_token"
            )
            try:
                status, body = http_get(url)
            except Exception as exc:
                print("fetch fail", row["symbol"], row["network"], exc, flush=True)
                continue
            if status == 429:
                time.sleep(15)
                status, body = http_get(url)
            if status != 200:
                continue
            pools = json.loads(body).get("data") or []
            for pool in pools:
                attr = pool.get("attributes") or {}
                rel = pool.get("relationships") or {}
                dex = ((rel.get("dex") or {}).get("data") or {}).get("id") or ""
                checked_pools += 1
                if "v4" in dex.lower():
                    skipped_v4 += 1
                    continue
                fee = parse_fee(str(attr.get("name") or ""), dex)
                if fee is None or fee >= Decimal("0.5"):
                    # 50%+ fee cannot be a cheap stock. 88% pools land here.
                    continue
                price, stable_sym, stable_addr, _side = our_price(pool, row["address"])
                if price is None or not is_stable(stable_sym):
                    continue
                infinitesimal = price * (Decimal(1) + fee) / mult
                if infinitesimal >= ref:
                    continue
                pool_addr = str(attr.get("address") or "")
                dec_stable = decimals_of(slug, stable_addr if stable_addr.startswith("0x") else stable_addr)
                dec_token = decimals_of(slug, row["address"])
                use_v2 = "v2" in dex.lower()
                winner = None
                breaks_at = None
                how_try = "v2_reserves" if use_v2 else "quoter"
                for size in SIZES:
                    raw_in = int(size * Decimal(10) ** dec_stable)
                    if use_v2:
                        out = v2_out(slug, pool_addr, stable_addr, row["address"], fee, raw_in)
                    else:
                        out = quote_v3(slug, stable_addr, row["address"], fee, raw_in)
                    if not out:
                        if size == SIZES[0]:
                            print(
                                f"quote_fail {row['symbol']} {slug} {dex} fee={fee} {attr.get('name')}",
                                flush=True,
                            )
                        breaks_at = size
                        break
                    got = Decimal(out) / Decimal(10) ** dec_token
                    if got <= 0:
                        breaks_at = size
                        break
                    share = (size / got) / mult
                    if not sane_share(share, ref) or share >= ref:
                        breaks_at = size
                        break
                    gap = (share - ref) / ref * Decimal(10000)
                    winner = (size, share, gap)
                if winner is None:
                    continue
                spent, share, gap = winner
                if spent < Decimal(1):
                    continue
                candidate = {
                    "symbol": row["symbol"],
                    "ticker": row["ticker"],
                    "network": row["network"],
                    "form": row["form"],
                    "address": row["address"],
                    "dex": dex,
                    "pool": attr.get("name"),
                    "fee": format(fee, "f"),
                    "spot_token_usd": format(price, "f"),
                    "quoted_spend_usd": format(spent, "f"),
                    "breaks_at_usd": "" if breaks_at is None else format(breaks_at, "f"),
                    "quoted_share_usd": format(share, "f"),
                    "ref_usd": format(ref, "f"),
                    "ref_kind": row["ref_kind"],
                    "ref_label": row["ref_label"],
                    "gap_bps": format(gap, "f"),
                    "quote_via": how_try,
                    "kraken": row["kraken_status"],
                    "multiplier": row["multiplier"],
                    "stable": stable_sym,
                }
                if best is None or better(candidate, best):
                    best = candidate
            time.sleep(0.7)
        if best:
            kept.append(best)
            print(
                f"keep {best['symbol']} {best['network']} {best['form']} "
                f"fee={best['fee']} spend={best['quoted_spend_usd']} "
                f"breaks={best['breaks_at_usd'] or '500+'} gap={best['gap_bps']}",
                flush=True,
            )
        if (index + 1) % 5 == 0:
            dest.write_text(json.dumps({"kept": kept, "partial": True, "done": index + 1}, ensure_ascii=False))
            print(f"progress {index+1}/{len(cheap)} kept {len(kept)}", flush=True)
    kept.sort(key=lambda item: Decimal(item["gap_bps"]))
    dest.write_text(json.dumps({
        "kept": kept,
        "checked_pools": checked_pools,
        "skipped_v4_pools": skipped_v4,
        "candidates": len(cheap),
    }, ensure_ascii=False))
    print(f"wrote {OUT} kept {len(kept)} v4_skipped {skipped_v4}", flush=True)


if __name__ == "__main__":
    main()
