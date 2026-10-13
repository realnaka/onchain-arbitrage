"""Gas-token USD prices, refreshed by the round that needs them.

``xstocks_v2_gas_prices.json`` used to be refreshed by hand
(``scripts/xstocks_v2_bridge_fees.py refresh``), and nothing in the repo called
that script on a timer. Measured 2026-10-09: the OKB/SOL prices were written
2026-10-08 13:08, both went past the three hour allowance, and every x-layer
bridge read ``gas_price_stale`` while five board rows printed
"等：bridge_fee_usd" for 19.7 hours. The cause was not the market and not the
balance; nobody had called the refresher.

So the refresher lives here and the scan calls it. Two rules from A7 still
hold:

* the sources cannot see our wallet (an ``eth_call`` to the pool for OKB, the
  public Jupiter price endpoint for SOL) -- a balance-gated quote is what made
  the price go stale in the first place;
* a price that cannot be read stays **unknown**. The old row is left alone
  (so its age keeps failing the allowance), the caller writes the failure into
  its counters, and nothing ever falls back to "the last number, quietly".
"""
from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path

from arb_monitor.xstocks_v2 import bridge_fees as BF
from arb_monitor.xstocks_v2.feeds import evm as evm_feed
from arb_monitor.xstocks_v2.store import read_json, write_json

# X Layer: the bridge fee is native OKB, and WOKB is 1:1 with it.
WOKB_XLAYER = "0xe538905cf8410324e03a5a23c1c177a474d59b2b"
USDG_XLAYER = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"
# Solana: the CCIP program charges in native SOL.
WSOL_SOLANA = "So11111111111111111111111111111111111111112"
USDC_SOLANA = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
#: X Layer's QuoterV2 (same address the atomic proof uses) and the USDG/WOKB
#: pools. ``pool["fee"]`` is a *fraction* (the encoder multiplies by 1e6), so the
#: 1 bps pool is 0.0001 and the 5 bps pool is 0.0005 -- passing the contract's own
#: integer (100) encodes 100_000_000 and reverts (found 2026-10-07).
QUOTER_V2_XLAYER = "0xd1b797d92d87b688193a2b976efc8d577d204343"
USDG_WOKB_FEES = (Decimal("0.0005"), Decimal("0.0001"))
#: Two sizes have to agree, or it is not a price. Deeper pool first.
QUOTE_SIZES = ("1", "10")
SIZE_TOLERANCE_BPS = Decimal(50)
JUPITER_PRICE = "https://lite-api.jup.ag/price/v3"


def _scan_quote_fn():
    """``scan.evm_pair_quotes``, imported late: the scan imports this module."""
    from arb_monitor.xstocks_v2 import scan
    return scan.evm_pair_quotes


def price_okb_from_pool(http, *, quote_fn=None) -> tuple[Decimal | None, str]:
    """OKB in USD from a read-only QuoterV2 call, with no wallet involved.

    The aggregator quote is **balance-gated**: it refuses to quote an input the
    wallet cannot pay (measured 2026-10-07 -- after a swap left 0.0003 WOKB,
    every ``WOKB -> USDG`` quote answered ``no_route``, the OKB price went stale,
    and every x-layer bridge cost silently became ``cost_unknown``). An
    ``eth_call`` to the pool has no such dependency: it answers whether or not we
    own anything.

    Two sizes must agree within 50 bps, or it is not a price. The deeper pool is
    tried first (measured 2026-10-07: USDG/WOKB has 6.65e16 at 5 bps against
    4.8e10 at 1 bps).
    """
    quote = quote_fn or _scan_quote_fn()
    chain = evm_feed.CHAINS.get("x-layer")
    if chain is None:
        return None, "no x-layer config"
    failures: list[str] = []
    for fee in USDG_WOKB_FEES:
        pool = {"chain": "x-layer", "symbol": "OKB", "pool_id": f"usdg-wokb-{fee}",
                "dex": "uniswap-v3-x-layer", "token_address": WOKB_XLAYER,
                "quote_address": USDG_XLAYER, "fee": fee,
                "quoter": QUOTER_V2_XLAYER,
                "quote_decimals": 6, "token_decimals": 18}
        prices: list[Decimal] = []
        for usdg in QUOTE_SIZES:
            rows = quote(http, chain, [pool], Decimal(usdg),
                         recv_ms=int(time.time() * 1000))
            row = rows[0] if rows else {}
            price = getattr(row.get("buy"), "price_usd", None)
            if price is None or price <= 0:
                failures.append(f"{fee}:{row.get('errors') or 'no_quote'}")
                break
            prices.append(Decimal(str(price)))
        if len(prices) != 2:
            continue
        small, big = sorted(prices)
        if (big - small) / small * Decimal(10000) > SIZE_TOLERANCE_BPS:
            failures.append(f"{fee}:sizes disagree {small} vs {big}")
            continue
        return prices[-1], f"evm-quoter:usdg-wokb-{fee}"
    return None, f"quoter:{';'.join(failures) or 'no_pool'}"


def price_sol_from_jupiter(http, *, quote_fn=None) -> tuple[Decimal | None, str]:
    """SOL in USD from Jupiter's public price API (no wallet, no key)."""
    del quote_fn       # every source takes the same arguments; this one needs none
    try:
        status, payload, _headers = http.get_json(
            f"{JUPITER_PRICE}?ids={WSOL_SOLANA}")
    except Exception as exc:  # noqa: BLE001
        return None, f"jupiter:{type(exc).__name__}"
    if status != 200 or not isinstance(payload, dict):
        return None, f"jupiter:http_{status}"
    row = payload.get(WSOL_SOLANA) or {}
    for key in ("usdPrice", "price"):
        value = row.get(key) if isinstance(row, dict) else None
        if value:
            try:
                price = Decimal(str(value))
            except Exception:  # noqa: BLE001
                continue
            if price > 0:
                return price, "jupiter-price-v3"
    return None, "jupiter:no_price"


def price_ton_from_kraken(http, *, quote_fn=None) -> tuple[Decimal | None, str]:
    """TON in USD from Kraken's public ticker. Same rule as ETH: no wallet, no key."""
    del quote_fn
    from arb_monitor.xstocks_v2.feeds import kraken as kraken_feed

    try:
        prices = kraken_feed.native_usd(http, ["TON"])
    except Exception as exc:  # noqa: BLE001
        return None, f"kraken:{type(exc).__name__}"
    value = prices.get("TON")
    if value is None or value <= 0:
        return None, "kraken:no_price"
    return value, "kraken-ticker:TONUSD"


def _kraken_native(http, token: str) -> tuple[Decimal | None, str]:
    """One gas token from Kraken's public ticker. No wallet and no key."""
    from arb_monitor.xstocks_v2.feeds import kraken as kraken_feed

    try:
        prices = kraken_feed.native_usd(http, [token])
    except Exception as exc:  # noqa: BLE001
        return None, f"kraken:{type(exc).__name__}"
    value = prices.get(token)
    if value is None or value <= 0:
        return None, "kraken:no_price"
    return value, f"kraken-ticker:{token}USD"


def price_bnb_from_kraken(http, *, quote_fn=None) -> tuple[Decimal | None, str]:
    """BNB in USD. BSC bridge fees are paid in BNB."""
    del quote_fn
    return _kraken_native(http, "BNB")


def price_hype_from_kraken(http, *, quote_fn=None) -> tuple[Decimal | None, str]:
    """HYPE in USD. HyperEVM bridge fees are paid in HYPE."""
    del quote_fn
    return _kraken_native(http, "HYPE")


def price_eth_from_kraken(http, *, quote_fn=None) -> tuple[Decimal | None, str]:
    """ETH in USD from Kraken's public ticker (no wallet, no key).

    The Ethereum leg pays mainnet gas, and until 2026-10-09 nothing priced it:
    ``gas_prices.json`` held OKB and SOL only, so an eth row's ``net_usdt``
    counted gas as **0** (``net_note = no_transfer_line``) while a round trip
    on mainnet really costs ``evm_gas_units × gas price``. On 2026-10-09 that
    was 441,831 gas at 0.11 gwei ≈ 0.0000486 ETH ≈ 0.12 USDT -- five times the
    net profit the board printed for a 25 USDT ticket. Kraken's book is the
    same wallet-independent source the Solana rows already use for SOL.
    """
    del quote_fn       # every source takes the same arguments; this one needs none
    from arb_monitor.xstocks_v2.feeds import kraken as kraken_feed

    try:
        prices = kraken_feed.native_usd(http, ["ETH"])
    except Exception as exc:  # noqa: BLE001
        return None, f"kraken:{type(exc).__name__}"
    value = prices.get("ETH")
    if value is None or value <= 0:
        return None, "kraken:no_price"
    return value, "kraken-ticker:ETHUSD"


#: Which gas tokens we can price, and how.
SOURCES = {"OKB": price_okb_from_pool, "SOL": price_sol_from_jupiter,
           "ETH": price_eth_from_kraken, "TON": price_ton_from_kraken,
           "BNB": price_bnb_from_kraken, "HYPE": price_hype_from_kraken}

#: Every gas token the scan keeps fresh. BNB prices a BSC bridge fee and HYPE
#: prices a HyperEVM one. L2 fees are native ETH, so they use the ETH price.
GAS_TOKENS = ("OKB", "SOL", "ETH", "TON", "BNB", "HYPE")

QUANTUM = Decimal("0.0001")


def refresh(data_dir: Path | str, *, now_ms: int | None = None, http=None,
            need=None, quote_fn=None, sources=None) -> dict:
    """Re-quote the named gas tokens and merge them into the price file.

    Returns ``{"refreshed": {asset: price_row}, "failed": {asset: reason}}``.
    A token that cannot be priced is **not** written and is not touched: its old
    row stays, keeps its old timestamp, and therefore keeps failing the
    allowance. The caller is expected to record ``failed`` where a human reads
    it.
    """
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    wanted = [str(asset).upper() for asset in (need or BF.feeds_needed())]
    http = http if http is not None else _http()
    table = dict(SOURCES)
    table.update(sources or {})
    refreshed: dict[str, dict] = {}
    failed: dict[str, str] = {}
    for asset in wanted:
        source = table.get(asset)
        if source is None:
            failed[asset] = "no_source"
            continue
        try:
            value, reason = source(http, quote_fn=quote_fn)
        except Exception as exc:  # noqa: BLE001 - a read that does not answer
            value, reason = None, f"{type(exc).__name__}"
        if value is None or value <= 0:
            failed[asset] = str(reason)
            continue
        refreshed[asset] = {
            "usd": format(Decimal(value).quantize(QUANTUM), "f"),
            "source": str(reason),
            "as_of_ms": now_ms,
        }
    if refreshed:
        write_prices(data_dir, refreshed, now_ms=now_ms)
    return {"refreshed": refreshed, "failed": failed}


def write_prices(data_dir: Path | str, prices: dict[str, dict], *,
                 now_ms: int) -> Path:
    """Merge ``{asset: {usd, source, as_of_ms}}`` into the price file."""
    path = Path(data_dir) / BF.PRICES_NAME
    previous = read_json(path, {}) or {}
    merged = dict(previous.get("prices") or {})
    merged.update(prices)
    return write_json(path, {
        "as_of_ms": now_ms,
        "method": ("扫描自己取价（A1）：OKB=X Layer USDG/WOKB 池的 QuoterV2 "
                   "eth_call，SOL=Jupiter price/v3；两个尺寸必须一致。"
                   "取不到就保持未知，绝不沿用旧价。"),
        "note": ("桥费付的是源链 gas 代币，所以 USD 价只影响『费用折美元』这一步，"
                 "不影响费用本身。价格超过 bridge_fees.MAX_PRICE_AGE_MS 就按未知处"
                 "理（cost_unknown 保留）。扫描每轮自己刷新（gas_prices."
                 "ensure_fresh）；手工刷新仍可 `scripts/xstocks_v2_bridge_fees.py "
                 "refresh`。"),
        "prices": merged,
    })


def _http():
    from arb_monitor.xstocks_v2.feeds.http import Http
    return Http()


def ensure_fresh(data_dir: Path | str, *, now_ms: int | None = None,
                 max_age_ms: int | None = None, http=None, need=None,
                 refresh_fn=None) -> dict:
    """Refresh only the prices that are missing or past their allowance.

    One call per asset per round at most, and none at all while the file is
    fresh -- the scan runs every ~2 minutes and must not turn into a quoter.
    """
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    max_age_ms = int(BF.MAX_PRICE_AGE_MS if max_age_ms is None else max_age_ms)
    report = BF.price_report(data_dir=data_dir, now_ms=now_ms,
                             max_price_age_ms=max_age_ms)
    wanted = [str(asset).upper() for asset in (need or BF.feeds_needed())]
    # "Missing" has two sources on purpose. ``report["missing"]`` only knows the
    # tokens the bridge fee table spends (``feeds_needed`` = OKB, SOL), so a
    # token that has **never** been priced -- ETH on 2026-10-09, the first day an
    # eth row carried a gas line -- was neither stale nor missing and never got
    # refreshed. A name the caller asks for that is absent from the file is
    # missing by definition.
    priced = report.get("prices") or {}
    stale = [asset for asset in wanted
             if asset in report["stale"] or asset in report["missing"]
             or asset not in priced]
    out = {"checked": wanted, "stale": stale, "refreshed": [], "failed": {},
           "skipped": []}
    if not stale:
        return out
    result = (refresh_fn or refresh)(data_dir, now_ms=now_ms, http=http,
                                     need=stale)
    out["refreshed"] = sorted(result.get("refreshed") or {})
    out["failed"] = dict(result.get("failed") or {})
    out["skipped"] = sorted(set(stale) - set(out["refreshed"])
                            - set(out["failed"]))
    return out
