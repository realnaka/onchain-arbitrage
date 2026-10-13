"""Offline universe: which symbols have a quotable pool on which chain.

Two sources, both already on disk, so stage 1 costs no extra calls:

* ``data/xstocks_chain_census.json`` — the multi-chain pool census. Its
  candidates already passed the depth gate and the stable-quote gate.
* ``data/xstocks_book.sqlite`` — the issuer's deployment table. It answers
  "which address is this symbol on this chain", which is what Jupiter and the
  EVM quoter need.

The census is a candidate list, not a promise: a chain whose scan found nothing
shows up as zero pools rather than disappearing (spec section 5.2).
"""
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path

from arb_monitor.xstocks_v2.units import units_config_path

ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CENSUS = ROOT / "data" / "xstocks_chain_census.json"
DEFAULT_BOOK = ROOT / "data" / "xstocks_book.sqlite"
DEFAULT_V4 = ROOT / "data" / "xstocks_v4_watch.json"
DEFAULT_UNITS = units_config_path(ROOT)

# The census calls X Layer "XLayer"; the book calls it the same. Everything
# downstream keys on the lower-case slug.
NETWORK_TO_CHAIN = {
    "ethereum": "eth",
    "xlayer": "x-layer",
    "x-layer": "x-layer",
    "bnb smart chain": "bsc",
    "bsc": "bsc",
    "arbitrum": "arbitrum",
    "optimism": "optimism",
    "monad": "monad",
    "hyperevm": "hyperevm",
    "ink": "ink",
    "mantle": "mantle",
    "solana": "solana",
    "ton": "ton",
}


def chain_key(value: object) -> str:
    text = str(value or "").strip().lower()
    return NETWORK_TO_CHAIN.get(text, text)


def _decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def is_evm_address(value: object) -> bool:
    """``0x`` plus 40 hex digits. An empty or truncated string is not an address."""
    text = str(value or "")
    if len(text) != 42 or not text.startswith("0x"):
        return False
    try:
        int(text[2:], 16)
    except ValueError:
        return False
    return True
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def load_census(path: Path | str = DEFAULT_CENSUS) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def load_v4(path: Path | str = DEFAULT_V4) -> dict:
    """v4 watch rows, keyed by pool id. The watch already ran V4Quoter."""
    data = load_census(path)
    out = {"tiers": {}, "rows": []}
    for row in data.get("rows") or []:
        if not isinstance(row, dict):
            continue
        out["rows"].append(row)
        pool = str(row.get("pool_id") or row.get("pool") or "").lower()
        if pool:
            out["tiers"][pool] = str(row.get("tier") or "unknown")
    for pool, tier in (data.get("tiers") or {}).items():
        out["tiers"].setdefault(str(pool).lower(), str(tier))
    return out


#: Any of these keys marks a dict as one row of the ``[kraken_units]`` table.
UNIT_ROW_KEYS = frozenset({
    "status", "effective_min_units", "underlying_symbol",
    "market_units_per_balance_unit", "pair_ordermin",
    "underlying_per_market_unit", "deposit_credit_units_per_balance_unit",
    "deposit_credit_source", "deposit_credit_verified_at",
})


def flatten_units(table: dict, prefix: str = "") -> dict[str, dict]:
    """Undo the nesting a dotted symbol written bare as a TOML key creates.

    ``[kraken_units.BRK.Bx]`` parses as ``{"BRK": {"Bx": {<row>}}}``, so the
    reader sees a symbol called ``BRK`` and ``BRK.Bx`` disappears. The generator
    quotes its keys now; this keeps an older file readable instead of silently
    dropping one symbol (measured 2026-10-09).
    """
    out: dict[str, dict] = {}
    for key, value in table.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and not (UNIT_ROW_KEYS & set(value)):
            out.update(flatten_units(value, name))
        elif isinstance(value, dict):
            out[name] = value
    return out


def kraken_symbols(path: Path | str = DEFAULT_UNITS) -> dict[str, dict]:
    """``{symbol: {status, effective_min_units, underlying_symbol}}``."""
    import tomllib

    try:
        raw = tomllib.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    out: dict[str, dict] = {}
    for symbol, row in flatten_units(raw.get("kraken_units") or {}).items():
        out[str(symbol)] = {
            "status": str(row.get("status") or ""),
            "effective_min_units": _decimal(row.get("effective_min_units")),
            "underlying_symbol": str(row.get("underlying_symbol") or ""),
            "deposit_credit_units_per_balance_unit":
                _decimal(row.get("deposit_credit_units_per_balance_unit")),
        }
    return out


def deployments(book: Path | str = DEFAULT_BOOK, *, network: str = "Solana") -> dict[str, dict]:
    """``{symbol: {address, form, stables}}`` for one network, read-only.

    Opened in ``mode=ro`` on purpose: v2 never writes the v1 book.
    """
    out: dict[str, dict] = {}
    try:
        conn = sqlite3.connect(f"file:{book}?mode=ro", uri=True)
    except sqlite3.Error:
        return out
    try:
        rows = conn.execute(
            "SELECT issuer_symbol, address, form, stables_json FROM deployments"
            " WHERE network = ? AND missing_since_ms IS NULL", (network,)).fetchall()
    except sqlite3.Error:
        return out
    finally:
        conn.close()
    for symbol, address, form, stables_json in rows:
        try:
            stables = json.loads(stables_json or "[]")
        except ValueError:
            stables = []
        previous = out.get(str(symbol))
        # Prefer the native form: a wrapper cannot be deposited into Kraken, so
        # a native deployment is the one worth quoting first (spec section 6.1).
        if previous and previous.get("form") == "native":
            continue
        out[str(symbol)] = {"address": str(address), "form": str(form),
                            "stables": stables}
    return out


def census_pools(census: dict, *, min_depth_usd: Decimal | None = None) -> list[dict]:
    """Normalise census candidates into pool rows the scan can quote."""
    out: list[dict] = []
    for row in census.get("candidates") or []:
        if not isinstance(row, dict):
            continue
        depth = _decimal(row.get("depth_usd"))
        if min_depth_usd is not None and depth is not None and depth < min_depth_usd:
            continue
        chain = chain_key(row.get("slug") or row.get("network"))
        token = str(row.get("token") or "")
        stable = str(row.get("stable") or "")
        if not (chain and is_evm_address(token) and is_evm_address(stable)):
            continue
        out.append({
            "chain": chain,
            "symbol": str(row.get("symbol") or ""),
            "pool_id": str(row.get("pool_address") or "").lower(),
            "dex": str(row.get("dex") or ""),
            "kind": "v4" if row.get("v4") else "v3",
            "token_address": token,
            "quote_asset": str(row.get("stable_symbol") or ""),
            "quote_address": stable,
            "token_decimals": int(row.get("dec_token") or 18),
            "quote_decimals": int(row.get("dec_stable") or 6),
            "fee": _decimal(row.get("fee")),
            "depth_usd": depth,
            "quoter": str(row.get("quoter") or ""),
            "form": str(row.get("form") or "native"),
            "source": "census",
        })
    return out


def solana_universe(census_symbols, *, book: Path | str = DEFAULT_BOOK) -> dict[str, dict]:
    """Symbols that have a real Solana mint and a Kraken listing.

    Jupiter routes across every Solana pool, so one mint is one venue: there is
    no per-pool quoting to do on Solana, and a "two Solana pools" candidate
    would be the same aggregated route on both sides (spec section 6.1).
    """
    dep = deployments(book, network="Solana")
    out: dict[str, dict] = {}
    for symbol in census_symbols:
        row = dep.get(symbol)
        if not row:
            continue
        stables = [stable for stable in row.get("stables") or []
                   if str(stable.get("symbol") or "").upper() in {"USDC", "USDT"}]
        if not stables:
            continue
        quote = sorted(stables, key=lambda item: 0 if str(item.get("symbol")).upper() == "USDC" else 1)[0]
        out[symbol] = {
            "chain": "solana",
            "symbol": symbol,
            "token_address": row["address"],
            "form": row["form"],
            "quote_asset": str(quote.get("symbol") or ""),
            "quote_address": str(quote.get("address") or ""),
            "quote_decimals": int(quote.get("decimals") or 6),
            "source": "deployments",
        }
    return out


# The book names networks the way the issuer does; the scan keys on the slug.
CHAIN_TO_NETWORK = {
    "eth": "Ethereum",
    "x-layer": "XLayer",
    "bsc": "BNB Smart Chain",
    "arbitrum": "Arbitrum",
    "optimism": "Optimism",
    "monad": "Monad",
    "hyperevm": "HyperEVM",
    "ink": "Ink",
    "mantle": "Mantle",
    "solana": "Solana",
    "ton": "TON",
}


def _stable_lookup(book: Path | str) -> dict[tuple[str, str, str], dict]:
    """``(symbol, chain, STABLE) -> {address, decimals}`` from the book."""
    conn = sqlite3.connect(f"file:{book}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT issuer_symbol, network, stables_json FROM deployments"
            " WHERE missing_since_ms IS NULL").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out: dict[tuple[str, str, str], dict] = {}
    for symbol, network, stables_json in rows:
        chain = chain_key(network)
        try:
            stables = json.loads(stables_json or "[]")
        except ValueError:
            continue
        for stable in stables:
            if not isinstance(stable, dict):
                continue
            key = (str(symbol), chain, str(stable.get("symbol") or "").upper())
            out.setdefault(key, {
                "address": str(stable.get("address") or ""),
                "decimals": int(stable.get("decimals") or 6),
            })
    return out


def v4_pool_rows(v4_data: dict, *, book: Path | str = DEFAULT_BOOK) -> list[dict]:
    """Uniswap v4 pools from the v4 watch, with the PoolKey the quoter needs.

    The watch already recorded tier, hook, tick spacing and fee key, so the
    scan does not need to re-read the PositionManager. The stablecoin address
    comes from the issuer deployment table rather than being assumed.
    """
    stable_index = _stable_lookup(book)
    out: list[dict] = []
    for row in v4_data.get("rows") or []:
        if not isinstance(row, dict):
            continue
        chain = chain_key(row.get("chain") or row.get("network"))
        symbol = str(row.get("symbol") or "")
        token = str(row.get("token") or "")
        stable_symbol = str(row.get("stable") or "").upper()
        stable = stable_index.get((symbol, chain, stable_symbol)) or {}
        if not (chain and symbol and is_evm_address(token)
                and is_evm_address(stable.get("address"))):
            continue
        out.append({
            "chain": chain,
            "symbol": symbol,
            "pool_id": str(row.get("pool_id") or "").lower(),
            "dex": str(row.get("dex") or "uniswap-v4"),
            "kind": "v4",
            "token_address": token,
            "quote_asset": stable_symbol,
            "quote_address": str(stable["address"]),
            "token_decimals": 18,
            "quote_decimals": int(stable.get("decimals") or 6),
            # For a dynamic-fee pool this word is the flag the pool was
            # initialised with, and the quoter needs it verbatim.
            "fee": _decimal(row.get("fee_key")) / Decimal(1_000_000)
            if _decimal(row.get("fee_key")) is not None else None,
            "fee_key": _decimal(row.get("fee_key")),
            "tick_spacing": int(row.get("tick_spacing") or 60),
            "hooks": str(row.get("hook") or "0x" + "0" * 40),
            "tier": str(row.get("tier") or "unknown"),
            "depth_usd": _decimal(row.get("depth_usd")),
            "form": str(row.get("form") or "native"),
            "source": "v4_watch",
        })
    return out


def ton_pool_rows(census: dict, *, min_depth_usd: Decimal | None = None) -> list[dict]:
    """TON pools from the census.

    They were silently dropped: ``census_pools`` requires an ``0x`` token
    address, and a TON jetton is ``EQ...``. That made TON the only chain with a
    Kraken rail and a six-figure pool that v2 never looked at (measured
    2026-10-07: SPYx / USDT on ston.fi, 107,918 USD deep).
    """
    out: list[dict] = []
    for row in census.get("candidates") or []:
        if not isinstance(row, dict):
            continue
        if chain_key(row.get("slug") or row.get("network")) != "ton":
            continue
        depth = _decimal(row.get("depth_usd"))
        if min_depth_usd is not None and depth is not None and depth < min_depth_usd:
            continue
        token = str(row.get("token") or "")
        stable = str(row.get("stable") or "")
        if not token or not stable or token.startswith("0x"):
            continue
        out.append({
            "chain": "ton",
            "symbol": str(row.get("symbol") or ""),
            "pool_id": str(row.get("pool_address") or ""),
            "dex": str(row.get("venue") or row.get("dex") or "stonfi-v2"),
            "kind": "ton",
            "token_address": token,
            "quote_asset": str(row.get("stable_symbol") or ""),
            "quote_address": stable,
            "token_decimals": int(row.get("dec_token") or 8),
            "quote_decimals": int(row.get("dec_stable") or 6),
            "fee": _decimal(row.get("fee")),
            "depth_usd": depth,
            "form": str(row.get("form") or "native"),
            "source": "census_ton",
        })
    return out
