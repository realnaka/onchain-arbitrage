"""Stage 0: read-only bilateral scan across every venue we can price.

The scan answers one question per path: if we bought here and sold there, what
would the spread be, and what stops us from doing it. It never signs anything.

Shape of a round (spec section 5.2):

* **Stage 1 — cheap and wide.** One Kraken ``Ticker`` call carries both the bid
  and the ask of every listed xStock, so the whole centralized side costs one
  request. Solana is ranked with batched ``price/v3`` references (50 mints per
  call). References may rank; they may not enter net profit.
* **Stage 2 — real and narrow.** Real two-way quotes for the candidates that
  landed within ``stage2_buffer_bps`` of the threshold, at most ``stage2_max``
  of them, plus every EVM pool (there are ~24 and they batch).

Two failure modes from the lessons library are structural here:

* A quote that is older than 15 s, or two sides more than 10 s apart, is
  untrusted. It does not get a net profit (lessons section C).
* ``t0`` spread is not the path's profit. Chain to Kraken takes about a minute
  on Solana and 22-27 minutes over CCIP, so the number a gate may act on is
  ``edge_after_requote_bps`` (spec section 5.4).
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path

from arb_monitor.xstocks_v2 import funnel as funnel_core
from arb_monitor.xstocks_v2 import costs
from arb_monitor.xstocks_v2 import dead_pools as dead_pools_core
from arb_monitor.xstocks_v2 import guards
from arb_monitor.xstocks_v2 import capability as capability_core
from arb_monitor.xstocks_v2 import live as live_core
from arb_monitor.xstocks_v2 import rotation as rotation_core
from arb_monitor.xstocks_v2 import units
from arb_monitor.xstocks_v2 import quotes as freshness
from arb_monitor.xstocks_v2 import routes as route_core
from arb_monitor.xstocks_v2 import bridge_fees as bridge_fees_core
from arb_monitor.xstocks_v2 import probe as probe_core
from arb_monitor.xstocks_v2 import gas_prices as gas_prices_core
from arb_monitor.xstocks_v2 import live_fees as live_fees_core
from arb_monitor.xstocks_v2.feeds import census as census_feed
from arb_monitor.xstocks_v2.feeds import evm, kraken
from arb_monitor.xstocks_v2.feeds import ton as ton_feed
from arb_monitor.xstocks_v2.feeds import xchange as xchange_feed
from arb_monitor.xstocks_v2.feeds.http import Http
from arb_monitor.xstocks_v2.feeds.jupiter import JupiterFeed
from arb_monitor.xstocks_v2.policy import V2Policy
from arb_monitor.xstocks_v2.spot_venue import (
    BUY, REAL, REFERENCE, SELL, Quote, edge_bps)
from arb_monitor.xstocks_v2.store import read_json, write_json

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA = ROOT / "data-v2"
SCAN_PATH = DEFAULT_DATA / "xstocks_v2_scan.json"
TIMING_PATH = DEFAULT_DATA / "xstocks_v2_full_scan_timing.json"
ROUTE_SAMPLES = DEFAULT_DATA / "xstocks_v2_route_samples.jsonl"
# The private API cannot register a withdrawal address (lessons 3.2), so the
# address book is a cached artifact the operator maintains in the UI.
ADDRESS_BOOK = ROOT / "data" / "xstocks_route_matrix.json"
WITHDRAW_METHODS = DEFAULT_DATA / "xstocks_v2_withdraw_methods.json"
DEAD_POOLS = DEFAULT_DATA / "xstocks_v2_dead_pools.json"
#: Which Solana rails have been looked at, so a few slots can go to the ones
#: that have waited the longest (see ``rotation``).
ROTATION = DEFAULT_DATA / "xstocks_v2_quote_rotation.json"
#: xChange long-tail rotation and the hot names promoted off it. Separate from
#: the Jupiter rotation: a different budget, a different venue.
XCHANGE_ROTATION = DEFAULT_DATA / "xstocks_v2_xchange_rotation.json"
EXIT_CACHE = ROOT / "data" / "kraken_exit_cache.json"
MULTIPLIERS = ROOT / "data" / "xstocks_multipliers.json"

SOL_DECIMALS = 8
USD_DECIMALS = 6
DEFAULT_SIZE_LADDER = (Decimal("25"), Decimal("5"))

# Chains this iteration may act on. Everything else is still scanned and shown
# with a reason, because monitoring is wider than execution (spec section 3).
PHASE_CHAINS = frozenset({"solana", "x-layer", "eth", "ton"})

#: The address we hold on each hub chain Kraken can send to. A hub without an
#: entry has **no** address, and that is what the board must print: until
#: 2026-10-09 every non-Solana hub was handed the EVM address, so a TON rail
#: looked ready to register with a 0x… address on The Open Network (the same
#: mistake the withdraw-address skill warns about). A missing address is a
#: missing input, not another chain's address.
HUB_ADDRESSES: dict[str, str] = {
    "solana": "AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev",
    "eth": "0xabababababababababababababababababababab",
    "x-layer": "0xabababababababababababababababababababab",
    "bsc": "0xabababababababababababababababababababab",
    "arbitrum": "0xabababababababababababababababababababab",
    "optimism": "0xabababababababababababababababababababab",
    # "ton": no wallet yet (todo C1). Absent on purpose -- see the comment above.
}

#: Why a hub has no address, in the words the board prints.
HUB_ADDRESS_MISSING: dict[str, str] = {
    "ton": "还没有 TON 钱包（C1）：OKX Agentic Wallet 不产 TON 地址；"
           "Binance Wallet 支持 TON，但创建它必须是有 KYC 的 Binance 用户",
}


def ton_address_file(path: Path | None = None) -> str:
    """The TON address line in ``config/ton.env``, or ``""``.

    Only ``TON_ADDRESS`` is read. The mnemonic on the next line stays in the
    file. A missing file is "no wallet yet", not an error.
    """
    env = path if path is not None else (
        Path(__file__).resolve().parents[3] / "config" / "ton.env")
    try:
        text = Path(env).read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith("TON_ADDRESS="):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


def hub_withdraw_address(hub: str, *, ton_env: Path | None = None) -> tuple[str, str]:
    """``(address, note)`` for a Kraken hub chain.

    An empty address with a note means "we hold no address on this chain" --
    the caller must print the note, never a different chain's address.
    TON comes from ``config/ton.env`` when that file already has an address.
    """
    chain = str(hub or "")
    if chain == "ton":
        address = ton_address_file(ton_env)
        if address:
            return address, ""
        return "", HUB_ADDRESS_MISSING.get(chain, "没有 ton 地址")
    address = HUB_ADDRESSES.get(chain, "")
    if address:
        return address, ""
    return "", HUB_ADDRESS_MISSING.get(chain, f"没有 {chain} 地址")

CHAIN_LABELS = {
    "solana": ["solana"],
    "ton": ["ton", "the open network"],
    "x-layer": ["xlayer", "x layer"],
    "eth": ["ethereum"],
    "bsc": ["bsc", "binance smart chain", "bnb"],
    "arbitrum": ["arbitrum"],
    "optimism": ["optimism"],
    "ink": ["ink"],
    "monad": ["monad"],
    "hyperevm": ["hyperevm", "hyperliquid"],
    "mantle": ["mantle"],
}


def _d(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def load_multipliers(path: Path | str = MULTIPLIERS) -> dict[str, Decimal]:
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    out: dict[str, Decimal] = {}
    for symbol, row in (raw or {}).items():
        value = _d((row or {}).get("current")) if isinstance(row, dict) else _d(row)
        if value is not None and value > 0:
            out[str(symbol)] = value
    return out


def load_deposits(path: Path | str = EXIT_CACHE) -> dict[str, list]:
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    deposits = raw.get("deposits") if isinstance(raw, dict) else None
    return deposits if isinstance(deposits, dict) else {}


def load_measured_bridges(path: Path | str = ROUTE_SAMPLES) -> frozenset:
    """``(source, dest)`` pairs whose probe actually arrived on the far side.

    The route ledger starts empty and stays empty until someone runs a probe.
    A source-chain simulation is not an arrival (lessons 4.1), so only a row
    with ``arrived: true`` opens an edge.
    """
    from arb_monitor.xstocks_v2.store import read_jsonl

    out = set()
    for row in read_jsonl(path):
        if row.get("arrived") is True:
            source, dest = row.get("source_chain"), row.get("dest_chain")
            if source and dest:
                out.add((str(source).lower(), str(dest).lower()))
    return frozenset(out)


def load_measured_symbols(path: Path | str = ROUTE_SAMPLES) -> frozenset:
    """``(source, dest, symbol)`` triples whose probe actually arrived.

    ``load_measured_bridges`` knows the chain pair only, so one SPYx sample
    "opens" x-layer -> solana for every symbol. That is not evidence: Solana's
    CCIP token config is per mint, so the walk that landed SPYx says nothing
    about NVDAx. This is the per-symbol view, and it is what makes
    ``bridge_unmeasured_symbol`` mean literally "this coin has no arrival
    sample" instead of "the pair has one, but not necessarily this one"
    (operator, 2026-10-07: a reason must not overstate what the ledger holds).
    """
    from arb_monitor.xstocks_v2.store import read_jsonl

    out = set()
    for row in read_jsonl(path):
        if row.get("arrived") is True:
            source, dest = row.get("source_chain"), row.get("dest_chain")
            symbol = row.get("symbol")
            if source and dest and symbol:
                out.add((str(source).lower(), str(dest).lower(), str(symbol)))
    return frozenset(out)


def route_sample(*, symbol: str, source_chain: str, dest_chain: str, arrived: bool,
                 amount_raw=None, dest_balance_after=None, source: str = "",
                 extra: dict | None = None) -> dict:
    """One line of the route ledger, in the schema the two loaders read.

    ``arrived`` is a required argument on purpose: only a confirmed arrival on
    the far side opens an edge, and a source-chain simulation is not one
    (spec 6.2/6.4, lessons 4.1). Making it optional is how "the message left"
    turns into "the coins are there".
    """
    row: dict = {
        "measured_ms": int(time.time() * 1000),
        "symbol": symbol,
        "source_chain": route_core.norm(source_chain),
        "dest_chain": route_core.norm(dest_chain),
        "arrived": bool(arrived),
        "source": source,
    }
    if amount_raw is not None:
        row["amount_raw"] = int(amount_raw)
    if dest_balance_after is not None:
        row["dest_balance_after"] = format(Decimal(str(dest_balance_after)), "f")
    row.update(extra or {})
    return row


def record_route_sample(row: dict, path: Path | str = ROUTE_SAMPLES) -> Path:
    """Append to the route ledger. Append-only, like every other v2 ledger."""
    from arb_monitor.xstocks_v2.store import append_jsonl

    return append_jsonl(path, row)


def method_networks(methods) -> dict[str, str]:
    """``method name -> network`` for one asset.

    Kraken names the Solana rail for an xStock just ``SPYx`` -- no network word
    in the name at all. Only ``WithdrawMethods`` carries the network, and the
    deposit and withdrawal tables use the same method names, so this map is what
    lets a deposit rail on Solana be recognised (found 2026-10-07).
    """
    out: dict[str, str] = {}
    for row in methods or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("method") or "").strip().lower()
        network = str(row.get("network") or "").strip().lower()
        if name and network:
            out.setdefault(name, network)
    return out


def deposit_rails(symbol: str, chain: str, deposits: dict, networks=None) -> list[dict]:
    """Kraken methods that can receive this symbol from this chain.

    The method name is per symbol: MRNAx once answered Solana only. A chain that
    is not named there cannot be deposited from, no matter what the bridge says.

    ``networks`` is the method->network map for this asset, needed because a
    Solana rail is named ``SPYx`` with no network in the name.
    """
    labels = CHAIN_LABELS.get(chain) or []
    networks = networks or {}
    out = []
    for row in deposits.get(symbol) or []:
        if not isinstance(row, dict):
            continue
        method = str(row.get("method") or "").lower()
        network = networks.get(method, "")
        if any(label in method or label in network for label in labels):
            out.append(row)
    return out


def merge_address_books(*sources) -> list[dict]:
    """Union of address books, keyed by ``(asset, key)``.

    The withdrawal-method cache and v2's own address cache are written by
    different scripts at different times. Taking whichever one is non-empty is
    how ``solanspyx1`` stayed invisible: the cache holds five stablecoin
    entries, so ``cache or load_address_book()`` never reached the file that
    knew about SPYx (found 2026-10-07).
    """
    out: list[dict] = []
    seen: set[tuple] = set()
    for rows in sources:
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            key = (str(row.get("asset")), str(row.get("key")))
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
    return out


def _hub_chain(chain: str) -> str:
    """``solana->x-layer`` has its Kraken rail on ``solana``, not on the whole arc."""
    return str(chain or "").split("->")[0].strip()


def _request_net(row: dict) -> tuple[Decimal | None, Decimal | None]:
    """``(net_usdt, net_bps)`` from whichever cost model actually ran.

    Two models write into one row: the Kraken-leg model (``indicative_net_usdt``
    / ``net_bps``) and the bridge model (``net_usdt``). Showing the gross spread
    beside a net test is what made "61.76 bps" sit next to "fails 20 bps".
    """
    net_usdt = _d(row.get("indicative_net_usdt"))
    if net_usdt is None:
        net_usdt = _d(row.get("net_usdt"))
    net_bps = _d(row.get("net_bps"))
    if net_bps is None and net_usdt is not None:
        notional = _d(row.get("notional_usdt"))
        if notional:
            net_bps = net_usdt / notional * Decimal(10000)
    return net_usdt, net_bps


def _request_clears(row: dict, policy: V2Policy, session: str) -> bool:
    net_usdt, net_bps = _request_net(row)
    if net_usdt is None or net_bps is None:
        return False
    return (net_bps >= policy.required_edge_bps(session)
            and net_usdt >= policy.min_net_profit_usdt)


def _request_why(row: dict) -> str:
    if _request_net(row)[0] is None:
        return ("这条路 Kraken 支持，但净利算不出来（缺一条腿的费用），"
                "所以不能判断值不值得做")
    return "Kraken → 链：这条路 Kraken 支持，只差你在网页登记一次提币地址"


def load_address_book(path: Path | str = ADDRESS_BOOK) -> list[dict]:
    """Kraken withdrawal address book, read from the route-matrix snapshot.

    The private API cannot add an address (lessons 3.2), so the only honest
    source is what the operator has already registered in the UI.

    v2's own cache (``data-v2/xstocks_v2_address_book.json``, written by
    ``scripts/xstocks_v2_addresses.py``) wins when present: the v1 matrix is a
    snapshot that does not know about addresses added later -- which is exactly
    how the real Kraken -> Solana -> X Layer route stayed invisible on the board
    after ``solanspyx1`` was registered (found 2026-10-07).
    """
    merged: list[dict] = []
    try:
        cached = json.loads(
            (DEFAULT_DATA / "xstocks_v2_address_book.json").read_text())
        merged.extend(row for row in (cached.get("address_book") or [])
                      if isinstance(row, dict))
    except (OSError, ValueError):
        pass
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return merged
    entries = raw.get("address_book") if isinstance(raw, dict) else None
    merged.extend(row for row in (entries or []) if isinstance(row, dict))
    return merged


def load_withdraw_methods(path: Path | str = WITHDRAW_METHODS) -> dict:
    """``{asset: [WithdrawMethods rows]}`` plus the cached address book."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def rail_coverage(rows: list[dict], *, symbols, withdraw_methods, address_book,
                  hubs: tuple[str, ...] = ("solana", "eth", "ton"),
                  limit: int = 30) -> dict:
    """Which (asset, hub) pairs Kraken offers a rail for, and which we quoted.

    "Not sampled" is not "no opportunity" (lessons section C): a rail we never
    quote is invisible on the board. Measured 2026-10-07 23:00: SOXLx/Solana has
    a rail (min 0.00270869) and no address in the book, yet it produced **no
    row at all** that round -- it did not win a slot in the Solana quote budget,
    so it appeared in neither the request list nor the deferred list. This list
    is the third place: it says "we did not look", with the reason.
    """
    quoted: set[tuple[str, str]] = set()
    for row in rows or []:
        symbol = str(row.get("symbol") or "")
        for part in str(row.get("chain") or "").split("->"):
            part = _hub_chain(part.strip())
            if symbol and part:
                quoted.add((symbol, part))
    missing: list[dict] = []
    rails = 0
    for symbol in sorted(symbols or ()):
        methods = (withdraw_methods or {}).get(symbol)
        for hub in hubs:
            if not has_withdraw_method(symbol, hub, methods):
                continue
            rails += 1
            if (symbol, hub) in quoted:
                continue
            entries = [row for row in (address_book or [])
                       if str(row.get("asset")) == symbol]
            missing.append({
                "symbol": symbol,
                "hub": hub,
                "network_label": {"eth": "Ethereum", "solana": "Solana",
                                  "ton": "The Open Network"}.get(hub, hub),
                "has_address": address_book_ready(symbol, hub, entries, methods),
            })
    missing.sort(key=lambda item: (not item["has_address"], item["symbol"]))
    return {"rails": rails, "quoted": len(quoted),
            "missing_count": len(missing), "missing": missing[:limit]}


def address_book_ready(symbol: str, chain: str, entries, methods=None) -> bool:
    """Is there a registered withdrawal address for this symbol on this chain?

    The book is keyed by ``(asset, method)``, so the same wallet address has to
    be added once per asset. An empty book is "not ready", never "probably
    fine".

    ``methods`` is the asset's ``WithdrawMethods`` rows. It is needed because
    for a stablecoin the method name is just the ticker (``USDC``) and the
    network lives only in that table: without the join, a registered USDC
    address on Ethereum reads as unregistered (found 2026-10-07).
    """
    labels = CHAIN_LABELS.get(chain) or []
    if methods:
        for row in methods:
            if not isinstance(row, dict):
                continue
            method = str(row.get("method") or "")
            network = str(row.get("network") or "")
            if not any(label in method.lower() or label in network.lower()
                       for label in labels):
                continue
            for entry in entries or []:
                if str(entry.get("asset") or "") != symbol:
                    continue
                if str(entry.get("method") or "").lower() == method.lower():
                    return True
        return False
    for row in entries or []:
        if str(row.get("asset") or "") != symbol:
            continue
        method = str(row.get("method") or "").lower()
        if any(label in method for label in labels):
            return True
    return False


def has_withdraw_method(symbol: str, chain: str, methods) -> bool:
    """Does Kraken even offer a withdrawal rail for this symbol on this chain?

    Distinct from the address book, and the distinction matters to the operator:
    a missing address is something they can fix in the UI in a minute, while a
    missing rail cannot be fixed at all. Measured 2026-10-07: SPYx, QQQx, AAPLx
    and STRCx all list Ethereum / Solana / TON and **no X Layer**, so the
    X Layer rows are unreachable however full the address book gets.
    """
    labels = CHAIN_LABELS.get(chain) or []
    for row in methods or []:
        if not isinstance(row, dict):
            continue
        method = str(row.get("method") or "").lower()
        network = str(row.get("network") or "").lower()
        if any(label in method or label in network for label in labels):
            return True
    return False


def withdraw_rail(symbol: str, chain: str, methods) -> dict | None:
    """The fee/minimum row for this symbol's rail on this chain."""
    labels = CHAIN_LABELS.get(chain) or []
    for row in methods or []:
        if not isinstance(row, dict):
            continue
        method = str(row.get("method") or "").lower()
        network = str(row.get("network") or "").lower()
        if any(label in method or label in network for label in labels):
            return row
    return None


def rail_fee_units(rail: dict | None) -> Decimal | None:
    """Kraken nests the fee as ``{"aclass":…, "asset":…, "fee": "0.0029"}``.

    Reading the outer dict as the number silently produced ``None``, which made
    the withdrawal fee drop out of the cost model and left a losing route
    looking like a candidate (found 2026-10-07). A bare number is accepted too,
    because ``DepositMethods`` uses that shape.
    """
    if not isinstance(rail, dict):
        return None
    fee = rail.get("fee")
    if isinstance(fee, dict):
        fee = fee.get("fee")
    return _d(fee)


def kraken_to_chain_net(*, notional: Decimal, buy_price: Decimal,
                        sell_price: Decimal, fee_units: Decimal | None,
                        policy: V2Policy) -> tuple[Decimal | None, str]:
    """Indicative net for ``buy at Kraken, withdraw, sell on chain``.

    The withdrawal fee is charged in **units of the token**, and it is fixed
    rather than proportional. At a small ticket it dominates everything: STRCx
    charges 0.00291186 STRCx (~0.29 USDT), which is 116 bps of a 25 USDT trade
    against a 37 bps spread (measured 2026-10-07). Leaving it out made a losing
    route look like a candidate and sent the operator to register an address for
    it.
    """
    if fee_units is None:
        return None, "withdraw_fee_unknown"
    tokens = notional / buy_price if buy_price > 0 else None
    if tokens is None:
        return None, "no_buy_price"
    gross = tokens * sell_price - notional
    # One model, one place (``costs``): the Kraken taker and the slippage buffer
    # are a fraction of the *notional*, never of the gross profit -- applying
    # them to the profit made them a tenth of a basis point and hid the fact
    # that a spread can never clear the net gate (measured 2026-10-07).
    net = costs.net_after_costs(
        gross_usdt=gross, notional_usdt=notional,
        fixed_cost_usd=fee_units * sell_price, policy=policy,
        family="kraken_to_chain")
    return net, ""


# ---------------------------------------------------------------------------
# Real quotes
# ---------------------------------------------------------------------------
def solana_pair_quotes(feed: JupiterFeed, row: dict, notional: Decimal,
                       multiplier: Decimal, *, recv_ms: int,
                       tries: int = 1) -> dict:
    """Real two-way Solana quotes for one mint.

    Buy is ``USDC -> mint``. Sell is ``mint -> USDC`` on the tokens the buy
    returned. Selling "the same book back" is not assumed (lessons 5.5: 12 of
    15 Solana round trips lost 32-594 bps).

    ``tries`` is for the decision-time caller: a 429 there is a collision with
    the scan, not a missing pool, and it must not be recorded as one.
    """
    mint = row["token_address"]
    quote_mint = row["quote_address"]
    raw_in = int(notional * (10 ** USD_DECIMALS))
    buy = feed.quote(quote_mint, mint, raw_in, tries=tries)
    out = {"symbol": row["symbol"], "chain": "solana", "errors": []}
    if not buy:
        out["errors"].append("no_buy_route")
        return out
    tokens = Decimal(buy["out_amount"]) / (10 ** SOL_DECIMALS)
    # One conversion, one place (``units``): a Solana raw amount scales by the
    # multiplier, an EVM amount does not, and a wrapper pool divides by it.
    credit = units.to_credit_units("solana", tokens, multiplier)
    if credit <= 0:
        out["errors"].append("zero_out")
        return out
    out["tokens"] = tokens
    out["buy"] = Quote(
        venue="jupiter", chain="solana", symbol=row["symbol"], side=BUY, kind=REAL,
        price_usd=notional / credit, recv_ms=recv_ms, qty_token=credit,
        qty_quote=notional, source="jupiter:swap", depth_usd=notional,
        pool_id=buy.get("route", ""), note=row.get("quote_asset", ""),
        extra={"impact_bps": buy.get("impact_bps")},
    )
    sell = feed.quote(mint, quote_mint, int(tokens * (10 ** SOL_DECIMALS)),
                      tries=tries)
    if not sell:
        out["errors"].append("no_sell_route")
        return out
    received = Decimal(sell["out_amount"]) / (10 ** USD_DECIMALS)
    out["sell"] = Quote(
        venue="jupiter", chain="solana", symbol=row["symbol"], side=SELL, kind=REAL,
        price_usd=received / credit, recv_ms=recv_ms, qty_token=credit,
        qty_quote=received, source="jupiter:swap", depth_usd=notional,
        pool_id=sell.get("route", ""), note=row.get("quote_asset", ""),
        extra={"impact_bps": sell.get("impact_bps")},
    )
    return out


#: Pool ``form`` values that mean the pool trades the issuer's *native* token.
#: The implementation lives in ``units`` (one conversion, one place); this name
#: stays for readers who already know it.
NATIVE_POOL_FORMS = units.NATIVE_POOL_FORMS


def pool_credit_factor(pool: dict, multipliers: dict | None = None) -> Decimal:
    """Native (credit) units per one pool token. 1 for native, the multiplier for wrapper.

    Measured 2026-10-07: the X Layer SPYx pool trades the wrapper ``wSPYx``
    (``convertToAssets(1) = 1.005714560286254`` native), while the CCIP bridge
    hands us the **native** token -- so the exit route forces a wrap first and
    the aggregator pays 774.97, not the pool's 779.40. Comparing the wrapper
    price with a Solana or Kraken price (both per credit unit) printed a fake
    +55 bps that was exactly the multiplier.

    One credit unit **is** one native unit (measured: Kraken withdrew 0.00953311
    SPYx and X Layer received 0.009533097916934181 native), so a pool price has
    to be divided by this factor before it is compared with anything else.

    An unrecognised form counts as a wrapper: dividing by the multiplier can
    hide an opportunity, it can never invent one.

    Delegates to ``units.pool_credit_factor`` -- the same arithmetic as
    ``units.to_credit_units`` for Solana/TON amounts, so the two paths cannot
    disagree about what a credit unit is.
    """
    return units.pool_credit_factor(form=pool.get("form"),
                                    symbol=pool.get("symbol"),
                                    multipliers=multipliers)


def evm_pair_quotes(http: Http, chain: evm.Chain, pools: list[dict],
                    notional: Decimal | Callable[[dict], Decimal], *, recv_ms: int,
                    multipliers: dict | None = None) -> list[dict]:
    """Real two-way quotes for EVM pools, batched into two round trips.

    Round one asks every pool what it would give us for the stablecoin. Round
    two sells exactly what round one returned, so the two sides describe one
    path rather than two unrelated quotes.

    Prices are per **credit unit**, the unit every other venue in the scan
    quotes in: Solana and TON already multiply their raw amounts by the
    multiplier, and a wrapper pool's price is divided by it here. Without this
    the X Layer rows printed the multiplier itself as profit.

    ``notional`` is one size for every pool, or a function of the pool
    (``scan.pool_ticket_usdt``): the scan sizes each pool to what that pool can
    actually take, and the two-way quote has to be asked at that size for the
    round-trip check to describe the trade we would make.
    """
    def size_of(pool: dict) -> Decimal:
        return Decimal(notional(pool)) if callable(notional) else Decimal(notional)

    order = [pool for pool in pools if pool.get("token_address")
             and pool.get("quote_address")]
    factors = [pool_credit_factor(pool, multipliers) for pool in order]
    calls: list[tuple[str, str] | None] = []
    for pool in order:
        amount = int(size_of(pool) * (10 ** int(pool.get("quote_decimals") or 18)))
        calls.append(_buy_call(chain, pool, amount))
    buys = evm.rpc_batch(http, chain, calls)
    out: list[dict] = []
    sells: list[tuple[str, str] | None] = []
    for pool, factor, result in zip(order, factors, buys):
        row = {"symbol": pool["symbol"], "chain": pool["chain"], "pool_id": pool["pool_id"],
               "dex": pool.get("dex", ""), "kind": pool.get("kind", "v3"),
               "depth_usd": pool.get("depth_usd"),
               "form": str(pool.get("form") or ""),
               "credit_factor": format(factor, "f"),
               "errors": []}
        if result.get("error"):
            row["errors"].append(f"buy:{result['error']}")
            out.append(row)
            sells.append(None)
            continue
        raw_out = evm.decode_uint(result.get("result"))
        if not raw_out:
            row["errors"].append("buy:zero_out")
            out.append(row)
            sells.append(None)
            continue
        decimals = int(pool.get("token_decimals") or 18)
        tokens = Decimal(raw_out) / (10 ** decimals)
        if tokens <= 0:
            row["errors"].append("buy:zero_out")
            out.append(row)
            sells.append(None)
            continue
        # ``credit`` is what Kraken would credit (native units); ``raw_tokens``
        # is the wrapper amount the pool actually moved. Everything quoted,
        # priced and compared in this scan uses credit units.
        credit = tokens * factor
        if credit <= 0:
            row["errors"].append("buy:zero_credit")
            out.append(row)
            sells.append(None)
            continue
        row["tokens"] = credit
        row["raw_tokens"] = tokens
        size = size_of(pool)
        row["buy"] = Quote(
            venue=pool.get("dex") or "uniswap", chain=pool["chain"],
            symbol=pool["symbol"], side=BUY, kind=REAL,
            price_usd=size / credit, recv_ms=recv_ms, qty_token=credit,
            qty_quote=size, source="evm:quoter", depth_usd=pool.get("depth_usd"),
            pool_id=pool["pool_id"], note=pool.get("quote_asset", ""),
            fee_bps=None if pool.get("fee") is None else pool["fee"] * Decimal(10000),
        )
        sells.append(_sell_call(chain, pool, raw_out))
        out.append(row)
    results = evm.rpc_batch(http, chain, sells)
    for row, result in zip(out, results):
        if result.get("error"):
            if row.get("buy") is not None:
                row["errors"].append(f"sell:{result['error']}")
            continue
        raw_out = evm.decode_uint(result.get("result"))
        if not raw_out or row.get("tokens") is None:
            row["errors"].append("sell:zero_out")
            continue
        received = Decimal(raw_out) / (10 ** int(_quote_decimals(row, order)))
        row["sell"] = Quote(
            venue=row["dex"] or "uniswap", chain=row["chain"], symbol=row["symbol"],
            side=SELL, kind=REAL, price_usd=received / row["tokens"],
            recv_ms=recv_ms, qty_token=row["tokens"], qty_quote=received,
            source="evm:quoter", pool_id=row["pool_id"],
        )
    return out


def ton_pair_quotes(feed: ton_feed.TonFeed, pools: list[dict],
                    notional: Decimal | Callable[[dict], Decimal],
                    multipliers: dict, *, recv_ms: int) -> list[dict]:
    """Real two-way TON quotes. ston.fi answers both directions.

    ``notional`` is one size, or a function of the pool (see
    ``evm_pair_quotes``).
    """
    out: list[dict] = []
    for pool in pools:
        symbol = pool["symbol"]
        row = {"symbol": symbol, "chain": "ton", "pool_id": pool["pool_id"],
               "dex": pool.get("dex", "stonfi-v2"), "kind": "ton",
               "depth_usd": pool.get("depth_usd"), "errors": []}
        quote_dec = int(pool.get("quote_decimals") or 6)
        token_dec = int(pool.get("token_decimals") or 8)
        size = Decimal(notional(pool)) if callable(notional) else Decimal(notional)
        raw_in = int(size * (10 ** quote_dec))
        bought = feed.quote(pool["quote_address"], pool["token_address"], raw_in)
        if bought is None:
            row["errors"].append("no_buy_route")
            out.append(row)
            continue
        tokens = Decimal(bought) / (10 ** token_dec)
        credit = units.to_credit_units(
            "ton", tokens, multipliers.get(symbol, Decimal(1)))
        if credit <= 0:
            row["errors"].append("zero_out")
            out.append(row)
            continue
        row["tokens"] = tokens
        row["buy"] = Quote(
            venue="stonfi-v2", chain="ton", symbol=symbol, side=BUY, kind=REAL,
            price_usd=size / credit, recv_ms=recv_ms, qty_token=credit,
            qty_quote=size, source="stonfi:simulate",
            depth_usd=pool.get("depth_usd"), pool_id=pool["pool_id"],
            note=pool.get("quote_asset", ""),
            fee_bps=None if pool.get("fee") is None else pool["fee"] * Decimal(10000))
        sold = feed.quote(pool["token_address"], pool["quote_address"], bought)
        if sold is None:
            row["errors"].append("no_sell_route")
            out.append(row)
            continue
        received = Decimal(sold) / (10 ** quote_dec)
        row["sell"] = Quote(
            venue="stonfi-v2", chain="ton", symbol=symbol, side=SELL, kind=REAL,
            price_usd=received / credit, recv_ms=recv_ms, qty_token=credit,
            qty_quote=received, source="stonfi:simulate",
            depth_usd=pool.get("depth_usd"), pool_id=pool["pool_id"])
        out.append(row)
    return out


def _quote_decimals(row: dict, order: list[dict]) -> int:
    for pool in order:
        if pool["pool_id"] == row.get("pool_id"):
            return int(pool.get("quote_decimals") or 18)
    return 18


def _buy_call(chain: evm.Chain, pool: dict, amount: int):
    if pool.get("kind") == "v4":
        key = _pool_key(pool)
        if key is None or not chain.v4_quoter:
            return None
        zero_for_one = key["currency0"].lower() == str(pool["quote_address"]).lower()
        return (chain.v4_quoter, evm.encode_v4_quote(key, zero_for_one, amount))
    quoter = pool.get("quoter") or chain.quoter
    if not quoter:
        return None
    return (quoter, evm.encode_v3_quote(pool["quote_address"], pool["token_address"],
                                        amount, _fee_units(pool)))


def _sell_call(chain: evm.Chain, pool: dict, amount_out: int):
    if pool.get("kind") == "v4":
        key = _pool_key(pool)
        if key is None or not chain.v4_quoter:
            return None
        zero_for_one = key["currency0"].lower() == str(pool["token_address"]).lower()
        return (chain.v4_quoter, evm.encode_v4_quote(key, zero_for_one, amount_out))
    quoter = pool.get("quoter") or chain.quoter
    if not quoter:
        return None
    return (quoter, evm.encode_v3_quote(pool["token_address"], pool["quote_address"],
                                        amount_out, _fee_units(pool)))


def _fee_units(pool: dict) -> Decimal:
    fee = pool.get("fee")
    return fee if fee is not None else Decimal("0.003")


def _pool_key(pool: dict) -> dict | None:
    """Rebuild the v4 PoolKey. The census stores sides, not the sorted key."""
    token = str(pool.get("token_address") or "").lower()
    stable = str(pool.get("quote_address") or "").lower()
    if not (census_feed.is_evm_address(token) and census_feed.is_evm_address(stable)):
        return None
    currency0, currency1 = sorted([token, stable])
    return {
        "currency0": currency0,
        "currency1": currency1,
        "fee": int(_d(pool.get("fee_key")) or 0),
        "tick_spacing": int(pool.get("tick_spacing") or 60),
        "hooks": str(pool.get("hooks") or "0x" + "0" * 40),
    }


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------
def _is_xchange(entry: dict) -> bool:
    """An xChange soft-quote entry, as opposed to a pool or a Jupiter route."""
    if str(entry.get("dex") or "") == "xchange":
        return True
    return str(entry.get("pool_id") or "").startswith("xchange:")


def dedupe_scan_rows(rows: list[dict]) -> list[dict]:
    """Keep the later row for each ``(symbol, family, chain, buy, sell)``.

    A symbol that has both a Solana quote and an EVM quote is passed through
    ``build_candidates`` twice. The later call has the newer Kraken ticker, so
    it replaces the earlier copy. Jupiter and xChange stay as two rows: the
    venues are part of the key.
    """
    kept: list[dict | None] = []
    index: dict[tuple, int] = {}
    for row in rows:
        key = (row.get("symbol"), row.get("family"), row.get("chain"),
               row.get("buy_venue"), row.get("sell_venue"))
        if key in index:
            kept[index[key]] = None
        index[key] = len(kept)
        kept.append(row)
    return [row for row in kept if row is not None]


def _xchange_unpriced(*, symbol: str, entry: dict, kraken_row: dict,
                      policy: V2Policy, phase: str, now_ms: int,
                      deposits: dict, v4_tiers: dict,
                      measured_bridges: frozenset,
                      measured_symbols: frozenset,
                      address_book, withdraw_methods) -> list[dict]:
    """A row with no xChange price, so the board can say why it was not asked.

    Closed, below the venue minimum, and a refused request all land here.
    Dropping the entry would look the same as "we never looked".
    """
    reasons = [str(item) for item in (entry.get("xchange_blockers") or [])]
    if not reasons:
        return []
    chain = str(entry.get("chain") or "")
    bid, ask = kraken_row.get("bid"), kraken_row.get("ask")
    out: list[dict] = []
    gate = dict(
        symbol=symbol, chain=chain, phase=phase, policy=policy,
        buy=None, sell=None, same_pool=False, deposits=deposits,
        v4_tiers=v4_tiers, entry=entry, fresh=True,
        measured_bridges=measured_bridges, measured_symbols=measured_symbols,
        address_book=address_book, withdraw_methods=withdraw_methods)
    ticket = pool_ticket_usdt(entry, policy=policy)
    if bid is not None:
        blockers = _blockers(**gate) + reasons
        can_run, no_executor = live_core.executor_chain(
            live_core.CHAIN_TO_KRAKEN, chain)
        if not can_run:
            blockers = blockers + [no_executor]
        out.append(_row(
            symbol=symbol, family="chain_to_kraken", chain=chain,
            buy_venue="xchange", sell_venue="kraken",
            buy_price=None, sell_price=bid, edge=None, blockers=blockers,
            notional=ticket, entry=entry, policy=policy))
    if ask is not None:
        out.append(_row(
            symbol=symbol, family="kraken_to_chain", chain=chain,
            buy_venue="kraken", sell_venue="xchange",
            buy_price=ask, sell_price=None, edge=None,
            blockers=_blockers(**gate) + reasons,
            notional=ticket, entry=entry, policy=policy))
    return out


def build_candidates(*, symbol: str, chain_quotes: list[dict], kraken_row: dict,
                     policy: V2Policy, multiplier: Decimal,
                     deposits: dict, v4_tiers: dict, now_ms: int,
                     phase: str, measured_bridges: frozenset = frozenset(),
                     measured_symbols: frozenset = frozenset(),
                     bridge_fees: dict | None = None,
                     address_book: list | None = None,
                     withdraw_methods: dict | None = None,
                     missing_rails: list | None = None,
                     gas_usdt_xlayer: Decimal | None = Decimal(0),
                     gas_note_xlayer: str = "not_supplied",
                     gas_usdt_eth: Decimal | None = Decimal(0),
                     gas_note_eth: str = "not_supplied",
                     gas_usdt_ton: Decimal | None = Decimal(0),
                     gas_note_ton: str = "not_supplied",
                     gas_usdt_ink: Decimal | None = Decimal(0),
                     gas_note_ink: str = "not_supplied",
                     session: str = "calendar_unknown") -> list[dict]:
    """One row per buy venue -> sell venue pair for a single symbol.

    ``gas_usdt_xlayer`` is the round's priced cost of one X Layer action (or
    ``None`` when it could not be priced -- unknown gas refuses the row). The
    default of 0 keeps a caller that has no gas price (a test, a tool) at the
    spread-minus-known-costs arithmetic it had before.

    ``gas_usdt_eth`` is the same number for one Ethereum mainnet action, which
    is what a chain -> Kraken row on eth pays. Until 2026-10-09 that row had no
    gas line at all (``net_note = no_transfer_line``), so its net was the spread
    minus Kraken's taker fee -- and a real 0.12 USDT of mainnet gas was invisible.
    """
    missing_rail = missing_rails if missing_rails is not None else []
    rows: list[dict] = []
    bid, ask = kraken_row.get("bid"), kraken_row.get("ask")
    kraken_recv = int(kraken_row.get("recv_ms") or 0)
    for entry in chain_quotes:
        buy, sell = entry.get("buy"), entry.get("sell")
        if buy is None and sell is None:
            if _is_xchange(entry):
                rows.extend(_xchange_unpriced(
                    symbol=symbol, entry=entry, kraken_row=kraken_row,
                    policy=policy, phase=phase, now_ms=now_ms,
                    deposits=deposits, v4_tiers=v4_tiers,
                    measured_bridges=measured_bridges,
                    measured_symbols=measured_symbols,
                    address_book=address_book,
                    withdraw_methods=withdraw_methods))
            continue
        chain = entry.get("chain") or ""
        # chain -> Kraken: we buy on chain, we sell into the Kraken bid.
        if buy is not None and bid is not None:
            blockers = _blockers(
                symbol=symbol, chain=chain, phase=phase, policy=policy,
                buy=buy, sell=None, same_pool=False,
                deposits=deposits, v4_tiers=v4_tiers, entry=entry,
                measured_bridges=measured_bridges,
                measured_symbols=measured_symbols, address_book=address_book,
                withdraw_methods=withdraw_methods,
                fresh=freshness.quotes_fresh(
                    now_ms=now_ms, recv_a_ms=int(buy.recv_ms),
                    recv_b_ms=kraken_recv, max_age_ms=policy.max_age_ms,
                    max_pair_skew_ms=policy.max_pair_skew_ms))
            # Nothing left in the gate is not the same as "somebody can run
            # this". The chain -> Kraken executor buys on Solana only, so an
            # eth/ton row that passes every data and price check still has no
            # way to be traded. 2026-10-09 the board printed SPCXx/eth
            # (uniswap 164.6544 -> kraken 165.9100, +76.26 bps) with 阻挡原因
            # 「无」 and the operator asked why it never ran.
            can_run, no_executor = live_core.executor_chain(
                live_core.CHAIN_TO_KRAKEN, chain)
            if not can_run:
                blockers = blockers + [no_executor]
            # A TON buy pays the ston.fi burn. Unknown is not free: the row
            # stays out of the executable set until the TON price is in hand.
            if chain == "ton" and gas_usdt_ton is None and "cost_unknown" not in blockers:
                blockers = blockers + ["cost_unknown"]
            if chain == "ink" and gas_usdt_ink is None and "cost_unknown" not in blockers:
                blockers = blockers + ["cost_unknown"]
            edge = edge_bps(buy.price_usd, bid)
            # The spread verdict belongs on the row. Without it the only thing
            # left to print for a dud row was its rail blocker, so the board
            # blamed the missing sample for a -20 bps spread (operator,
            # 2026-10-07). The bar is the session bar, the same one the paper
            # runner re-derives after its re-quote.
            required = policy.required_edge_bps(session)
            if edge is not None and edge < required:
                blockers = blockers + ["below_threshold"]
            row = _row(
                symbol=symbol, family="chain_to_kraken", chain=chain,
                buy_venue=buy.venue, sell_venue="kraken",
                buy_price=buy.price_usd, sell_price=bid,
                edge=edge, blockers=blockers,
                notional=pool_ticket_usdt(entry, policy=policy), entry=entry,
                policy=policy, pools=[(entry, "buy")])
            row["required_edge_bps"] = format(required, "f")
            row["session"] = session
            # The gas this row's own on-chain buy pays (0 on Solana, the priced
            # line on an EVM chain). The Kraken re-price pass reads this back
            # and puts it into ``net_usdt``, so the number the board prints and
            # the number the live gate judges are the same one.
            row["gas_usdt"] = (None if row_gas_usdt(
                row, gas_usdt_xlayer, gas_usdt_eth, gas_usdt_ton,
                ink_gas_usdt=gas_usdt_ink) is None
                               else format(row_gas_usdt(
                                   row, gas_usdt_xlayer, gas_usdt_eth,
                                   gas_usdt_ton, ink_gas_usdt=gas_usdt_ink), "f"))
            row["gas_note"] = row_gas_note(
                row, gas_usdt_xlayer, gas_note_xlayer,
                gas_usdt_eth, gas_note_eth, gas_usdt_ton, gas_note_ton,
                ink_gas_usdt=gas_usdt_ink, ink_note=gas_note_ink)
            # Kraken has no deposit rail on this chain (X Layer). The coins
            # reach Kraken by bridging to Solana first, so the row pays that
            # message. Without it an 8 bps print looks like a trade.
            if not deposit_rails(
                    symbol, chain, deposits,
                    method_networks((withdraw_methods or {}).get(symbol))):
                fee = (bridge_fees or {}).get((chain, "solana"))
                row["hub_fixed_usd"] = None if fee is None else format(fee, "f")
                row["hub_fixed_note"] = "bridge_to_solana"
            if probe_shaped(blockers):
                row["probe_eligible"] = probe_eligible(blockers)
                row["probe"] = probe_plan(
                    symbol=symbol, source_chain=chain, hub_chain="solana",
                    blockers=blockers, deposits=deposits,
                    withdraw_methods=withdraw_methods,
                    bridge_fees=bridge_fees, required_bps=required)
            rows.append(row)
        # Kraken -> chain: we buy at the Kraken ask, we sell into the pool. A
        # missing pool quote is the reason this direction is dead, so the row
        # is still emitted (with no sell price) instead of disappearing.
        sell_errors = [error for error in (entry.get("errors") or [])
                       if error.startswith("sell:") or error == "no_sell_route"]
        if ask is not None and (sell is not None or sell_errors):
            blockers = _blockers(
                symbol=symbol, chain=chain, phase=phase, policy=policy,
                buy=None, sell=sell, same_pool=False,
                deposits=deposits, v4_tiers=v4_tiers, entry=entry,
                measured_bridges=measured_bridges, address_book=address_book,
                withdraw_methods=withdraw_methods,
                fresh=freshness.quotes_fresh(
                    now_ms=now_ms,
                    recv_a_ms=int(sell.recv_ms) if sell is not None else kraken_recv,
                    recv_b_ms=kraken_recv, max_age_ms=policy.max_age_ms,
                    max_pair_skew_ms=policy.max_pair_skew_ms))
            if "no_withdraw_method" in blockers:
                # Kraken has no rail for this (asset, network). The arc does not
                # exist, so a "Kraken -> x-layer" row is a path nobody can walk:
                # the real route is Kraken -> a rail it does have -> bridge.
                # Showing it looked like a phantom venue pair (operator, found
                # 2026-10-07). It is counted, not silently dropped.
                missing_rail.append(f"{symbol}:{chain}")
                continue
            if sell is None:
                out_reason = ["no_dest_pool"]
                blockers = out_reason + blockers
            edge = edge_bps(ask, sell.price_usd) if sell is not None else None
            rail = withdraw_rail(symbol, chain,
                                 (withdraw_methods or {}).get(symbol))
            fee_units = rail_fee_units(rail)
            # The ticket this pool can take, not the global ceiling: the whole
            # net-profit arithmetic below has to be done at the size we would
            # actually trade (2026-10-08).
            ticket = pool_ticket_usdt(entry, policy=policy)
            indicative = None
            fee_reason = ""
            if sell is not None:
                indicative, fee_reason = kraken_to_chain_net(
                    notional=ticket, buy_price=ask,
                    sell_price=sell.price_usd, fee_units=fee_units, policy=policy)
                if indicative is not None:
                    # The gate is a NET bar, and edge_t0_bps is a GROSS spread.
                    # Comparing the two directly is how a losing row read as a
                    # candidate (found 2026-10-07).
                    net_bps = indicative / ticket * Decimal(10000)
                    if (net_bps < policy.required_edge_bps(session)
                            or indicative < policy.min_net_profit_usdt):
                        blockers = blockers + ["below_net"]
            rows.append(_row(
                symbol=symbol, family="kraken_to_chain", chain=chain,
                buy_venue="kraken",
                sell_venue=sell.venue if sell is not None else (entry.get("dex") or chain),
                buy_price=ask, sell_price=sell.price_usd if sell is not None else None,
                edge=edge, blockers=blockers,
                notional=ticket, entry=entry, policy=policy,
                pools=[(entry, "sell")]))
            rows[-1]["withdraw_fee_units"] = (None if fee_units is None
                                              else format(fee_units, "f"))
            rows[-1]["withdraw_fee_usdt"] = (
                None if fee_units is None or sell is None
                else format(fee_units * sell.price_usd, "f"))
            rows[-1]["indicative_net_usdt"] = (
                None if indicative is None else format(indicative, "f"))
            rows[-1]["net_bps"] = (
                None if indicative is None
                else format(indicative / ticket * Decimal(10000), "f"))
            if fee_reason:
                rows[-1]["fee_note"] = fee_reason
    # Same chain, two different pools. Only meaningful when both sides are real
    # quotes from different pool ids (a Solana pair would be one route twice).
    # xChange is not a second pool. Pairing it here would mark the row
    # ``atomic_route_unproven`` and hide the real reason: two transactions.
    pools = [entry for entry in chain_quotes
             if entry.get("buy") and entry.get("sell") and not _is_xchange(entry)]
    for first in pools:
        for second in pools:
            first_pool = first.get("pool_id") or first["buy"].pool_id
            second_pool = second.get("pool_id") or second["sell"].pool_id
            if first_pool == second_pool or not first_pool or not second_pool:
                continue
            if first.get("chain") != second.get("chain"):
                continue
            if first["buy"].price_usd >= second["sell"].price_usd:
                continue
            blockers = _blockers(
                symbol=symbol, chain=first.get("chain") or "", phase=phase,
                policy=policy, buy=first["buy"], sell=second["sell"], same_pool=False,
                deposits=deposits, v4_tiers=v4_tiers, entry=first,
                measured_bridges=measured_bridges, address_book=address_book,
                withdraw_methods=withdraw_methods,
                fresh=freshness.quotes_fresh(
                    now_ms=now_ms, recv_a_ms=int(first["buy"].recv_ms),
                    recv_b_ms=int(second["sell"].recv_ms),
                    max_age_ms=policy.max_age_ms,
                    max_pair_skew_ms=policy.max_pair_skew_ms))
            # This family names two pools, so both have to pass the v4 quality
            # rule. Checking only ``entry`` (the buy side) left an amber
            # dynamic-fee pool on the sell side unmarked (2026-10-08).
            if "hook_not_green" not in blockers and any(
                    v4_tier_blocker(side, v4_tiers)
                    for side in (first, second)):
                blockers = blockers + ["hook_not_green"]
            edge = edge_bps(first["buy"].price_usd, second["sell"].price_usd)
            # One trade through two pools: the smaller pool sets the size.
            ticket = min(pool_ticket_usdt(first, policy=policy),
                         pool_ticket_usdt(second, policy=policy))
            row = _row(
                symbol=symbol, family="dex_to_dex", chain=first.get("chain") or "",
                buy_venue=first["buy"].venue, sell_venue=second["sell"].venue,
                buy_price=first["buy"].price_usd, sell_price=second["sell"].price_usd,
                edge=edge,
                blockers=blockers, notional=ticket,
                entry={**first, "pool_id": first_pool},
                policy=policy, pools=[(first, "buy"), (second, "sell")])
            # Stage 2: same chain, two pools is one trade, and one trade means
            # ONE transaction. Until a router proves a single call that visits
            # both pools, the row is not executable -- executing it as two swaps
            # is a different trade with a naked position in the middle, which is
            # the state the multi-leg executor cannot repair.
            row["atomic_required"] = True
            row["buy_pool"] = first_pool
            row["sell_pool"] = second_pool
            row["blockers"] = row["blockers"] + ["atomic_route_unproven"]
            row["reason"] = row["reason"] or "atomic_route_unproven"
            row["executable"] = False
            rows.append(row)
    # Same chain, a pool against xChange. The two fills are different
    # transactions, so the position is naked in between. The row is monitoring
    # until one transaction does both, or a two-leg state machine owns it.
    xchange_entries = [entry for entry in chain_quotes
                       if _is_xchange(entry) and entry.get("buy") and entry.get("sell")]
    for amm in pools:
        for xc in xchange_entries:
            if amm.get("chain") != xc.get("chain"):
                continue
            for buy_entry, sell_entry in ((amm, xc), (xc, amm)):
                buy_quote, sell_quote = buy_entry.get("buy"), sell_entry.get("sell")
                if buy_quote is None or sell_quote is None:
                    continue
                if buy_quote.price_usd >= sell_quote.price_usd:
                    continue
                blockers = _blockers(
                    symbol=symbol, chain=str(amm.get("chain") or ""), phase=phase,
                    policy=policy, buy=buy_quote, sell=sell_quote, same_pool=False,
                    deposits=deposits, v4_tiers=v4_tiers, entry=buy_entry,
                    measured_bridges=measured_bridges, address_book=address_book,
                    withdraw_methods=withdraw_methods, kraken_rail=False,
                    fresh=freshness.quotes_fresh(
                        now_ms=now_ms, recv_a_ms=int(buy_quote.recv_ms),
                        recv_b_ms=int(sell_quote.recv_ms),
                        max_age_ms=policy.max_age_ms,
                        max_pair_skew_ms=policy.max_pair_skew_ms))
                blockers = blockers + ["two_tx_inventory"]
                edge = edge_bps(buy_quote.price_usd, sell_quote.price_usd)
                ticket = min(pool_ticket_usdt(buy_entry, policy=policy),
                             pool_ticket_usdt(sell_entry, policy=policy))
                row = _row(
                    symbol=symbol, family="xchange_vs_pool",
                    chain=str(amm.get("chain") or ""),
                    buy_venue=buy_quote.venue, sell_venue=sell_quote.venue,
                    buy_price=buy_quote.price_usd, sell_price=sell_quote.price_usd,
                    edge=edge, blockers=blockers, notional=ticket,
                    entry=buy_entry, policy=policy,
                    pools=[(buy_entry, "buy"), (sell_entry, "sell")])
                gas = row_gas_usdt(
                    row, gas_usdt_xlayer, gas_usdt_eth, gas_usdt_ton,
                    ink_gas_usdt=gas_usdt_ink)
                row["gas_usdt"] = None if gas is None else format(gas, "f")
                row["gas_note"] = row_gas_note(
                    row, gas_usdt_xlayer, gas_note_xlayer,
                    gas_usdt_eth, gas_note_eth, gas_usdt_ton, gas_note_ton,
                    ink_gas_usdt=gas_usdt_ink, ink_note=gas_note_ink)
                if gas is None and "cost_unknown" not in row["blockers"]:
                    row["blockers"] = row["blockers"] + ["cost_unknown"]
                    row["reason"] = headline_reason(row["blockers"])
                    row["executable"] = False
                rows.append(row)
    # Chain A -> chain B. This is the any-to-any case the upgrade exists for:
    # buy where it is cheap on one chain, sell where it is expensive on another.
    # The token has to physically cross, so the row is emitted for monitoring
    # and stays non-executable until that exact (source, dest, symbol) triple
    # has an arrival sample (spec sections 6.2, 6.4, 7).
    by_chain: dict[str, list[dict]] = {}
    for entry in chain_quotes:
        if entry.get("chain") and (entry.get("buy") or entry.get("sell")):
            by_chain.setdefault(str(entry["chain"]), []).append(entry)
    for source in sorted(by_chain):
        for dest in sorted(by_chain):
            if source == dest:
                continue
            if not route_core.has_bridge(source, dest):
                continue
            dialect = route_core.dialect(source, dest)
            for first in by_chain[source]:
                for second in by_chain[dest]:
                    if first.get("pool_id") and first.get("pool_id") == second.get("pool_id"):
                        continue
                    buy_quote, sell_quote = first.get("buy"), second.get("sell")
                    if buy_quote is None or sell_quote is None:
                        continue
                    blockers = _blockers(
                        symbol=symbol, chain=source, phase=phase, policy=policy,
                        buy=buy_quote, sell=sell_quote, same_pool=False,
                        deposits=deposits, v4_tiers=v4_tiers, entry=first,
                        measured_bridges=measured_bridges,
                        address_book=address_book,
                        withdraw_methods=withdraw_methods,
                        kraken_rail=False,
                        fresh=freshness.quotes_fresh(
                            now_ms=now_ms, recv_a_ms=int(buy_quote.recv_ms),
                            recv_b_ms=int(sell_quote.recv_ms),
                            max_age_ms=policy.max_age_ms,
                            max_pair_skew_ms=policy.max_pair_skew_ms))
                    # Both pools have to be believable, not just the one we buy
                    # from: the exit pool is the one that fakes a high sell price,
                    # and the side we cross there is its sell side.
                    blockers = blockers + pool_quality(second, policy=policy,
                                                       side="sell")
                    # A bridge with no arrival sample, and a fee that has never
                    # been measured, are both refusals rather than zeros. The fee
                    # is per message and paid in the source chain's gas token, so
                    # it can only be priced through the fee table.
                    if (source, dest) not in measured_bridges:
                        blockers = ["unmeasured_route"] + blockers
                    fee_usd = (bridge_fees or {}).get((source, dest))
                    if fee_usd is None:
                        blockers = blockers + ["cost_unknown"]
                    edge = edge_bps(buy_quote.price_usd, sell_quote.price_usd)
                    ticket = min(pool_ticket_usdt(first, policy=policy),
                                 pool_ticket_usdt(second, policy=policy))
                    row = _row(
                        symbol=symbol, family="chain_to_chain",
                        chain=f"{source}->{dest}",
                        buy_venue=buy_quote.venue, sell_venue=sell_quote.venue,
                        buy_price=buy_quote.price_usd, sell_price=sell_quote.price_usd,
                        edge=edge, blockers=blockers,
                        notional=ticket, entry=first,
                        policy=policy, pools=[(first, "buy"), (second, "sell")])
                    row["source_chain"] = source
                    row["dest_chain"] = dest
                    row["ccip_dialect"] = dialect
                    row["bridge_status"] = capability_core.bridge_status(source, dest)
                    if row["bridge_status"] != capability_core.READY:
                        row["blockers"] = list(row["blockers"]) + [row["bridge_status"]]
                        row["reason"] = row["reason"] or row["bridge_status"]
                        row["executable"] = False
                    row["bridge_fee_usd"] = (
                        format(fee_usd, "f") if fee_usd is not None else None)
                    net = bridge_net_usdt(
                        gross_usdt=row["gross_usdt"],
                        notional_usdt=row["notional_usdt"],
                        bridge_fee_usd=fee_usd, policy=policy,
                        family=row["family"],
                        gas_usdt=row_gas_usdt(row, gas_usdt_xlayer, gas_usdt_eth, ink_gas_usdt=gas_usdt_ink))
                    row["gas_usdt"] = (None if row_gas_usdt(
                        row, gas_usdt_xlayer, gas_usdt_eth, ink_gas_usdt=gas_usdt_ink) is None
                                       else format(row_gas_usdt(
                                           row, gas_usdt_xlayer, gas_usdt_eth, ink_gas_usdt=gas_usdt_ink), "f"))
                    row["gas_note"] = row_gas_note(
                        row, gas_usdt_xlayer, gas_note_xlayer, gas_usdt_eth,
                        gas_note_eth, ink_gas_usdt=gas_usdt_ink,
                        ink_note=gas_note_ink)
                    apply_net_blocker(
                        row, net, policy, fixed_cost_usd=fee_usd,
                        gas_usdt=row_gas_usdt(row, gas_usdt_xlayer, gas_usdt_eth, ink_gas_usdt=gas_usdt_ink),
                        family=row["family"])
                    rows.append(row)
    rows.extend(_bridge_hops(
        symbol=symbol, chain_quotes=chain_quotes, by_chain=by_chain,
        kraken_row=kraken_row, policy=policy, deposits=deposits,
        withdraw_methods=withdraw_methods, v4_tiers=v4_tiers,
        measured_bridges=measured_bridges, measured_symbols=measured_symbols,
        bridge_fees=bridge_fees,
        gas_usdt_xlayer=gas_usdt_xlayer, gas_note_xlayer=gas_note_xlayer,
        gas_usdt_eth=gas_usdt_eth, gas_note_eth=gas_note_eth,
        gas_usdt_ink=gas_usdt_ink, gas_note_ink=gas_note_ink,
        address_book=address_book, now_ms=now_ms, phase=phase, session=session))
    return rows


def _bridge_hops(*, symbol: str, chain_quotes: list[dict], by_chain: dict,
                 kraken_row: dict, policy: V2Policy, deposits: dict,
                 withdraw_methods: dict, v4_tiers: dict,
                 measured_bridges: frozenset, address_book, now_ms: int,
                 phase: str, bridge_fees: dict | None = None,
                 measured_symbols: frozenset = frozenset(),
                 gas_usdt_xlayer: Decimal | None = Decimal(0),
                 gas_note_xlayer: str = "not_supplied",
                 gas_usdt_eth: Decimal | None = Decimal(0),
                 gas_note_eth: str = "not_supplied",
                 gas_usdt_ink: Decimal | None = Decimal(0),
                 gas_note_ink: str = "not_supplied",
                 session: str = "calendar_unknown") -> list[dict]:
    """Two-hop paths around the fact that Kraken only lists a few chains.

    Kraken can only receive or send an xStock on Solana, Ethereum, TON (and a
    couple of long-tail chains). X Layer and Ink pools are deep and mispriced,
    and they are unreachable in one hop. So the path is:

    * **to Kraken**: buy on the pool chain -> bridge to a hub Kraken accepts ->
      deposit -> sell into the Kraken bid.
    * **from Kraken**: buy at the Kraken ask -> withdraw to a hub -> bridge to
      the pool chain -> sell into the pool.

    Both are emitted as catalogue rows. They stay non-executable until that
    exact bridge has an arrival sample and its fee has been measured, because a
    bridge fee guessed at zero is how a "+1200 bps" path becomes a loss.
    """
    rows: list[dict] = []
    bid, ask = kraken_row.get("bid"), kraken_row.get("ask")
    kraken_recv = int(kraken_row.get("recv_ms") or 0)
    for source, entries in by_chain.items():
        for hub in sorted(by_chain):
            if hub == source or not route_core.has_bridge(source, hub):
                continue
            for entry in entries:
                buy_quote, sell_quote = entry.get("buy"), entry.get("sell")
                # pool -> hub -> Kraken
                if buy_quote is not None and bid is not None and deposit_rails(
                        symbol, hub, deposits,
                        method_networks((withdraw_methods or {}).get(symbol))):
                    blockers = _hop_blockers(
                        policy=policy, symbol=symbol, entry=entry, entries=(entry,),
                        pair=(int(buy_quote.recv_ms), kraken_recv), now_ms=now_ms,
                        phase=phase, source=source, dest=hub,
                        measured_bridges=measured_bridges,
                        measured_symbols=measured_symbols,
                        v4_tiers=v4_tiers,
                        bridge_fee_usd=(bridge_fees or {}).get((source, hub)),
                        extra=pool_quality(entry, policy=policy, side="buy"))
                    edge = edge_bps(buy_quote.price_usd, bid)
                    required = policy.required_edge_bps(session)
                    if edge is not None and edge < required:
                        blockers = blockers + ["below_threshold"]
                    row = _row(
                        symbol=symbol, family="chain_via_bridge_to_kraken",
                        chain=f"{source}->{hub}",
                        buy_venue=buy_quote.venue, sell_venue="kraken",
                        buy_price=buy_quote.price_usd, sell_price=bid, edge=edge,
                        blockers=blockers,
                        notional=pool_ticket_usdt(entry, policy=policy),
                        entry=entry, policy=policy, pools=[(entry, "buy")])
                    row["source_chain"] = source
                    row["dest_chain"] = hub
                    row["ccip_dialect"] = route_core.dialect(source, hub)
                    row["hub_rail"] = f"Kraken deposit on {hub}"
                    row["required_edge_bps"] = format(required, "f")
                    row["session"] = session
                    row["bridge_fee_usd"] = (
                        format((bridge_fees or {}).get((source, hub)), "f")
                        if (bridge_fees or {}).get((source, hub)) is not None else None)
                    net = bridge_net_usdt(
                        gross_usdt=row["gross_usdt"],
                        notional_usdt=row["notional_usdt"],
                        bridge_fee_usd=(bridge_fees or {}).get((source, hub)),
                        policy=policy, family=row["family"],
                        gas_usdt=row_gas_usdt(row, gas_usdt_xlayer, gas_usdt_eth, ink_gas_usdt=gas_usdt_ink))
                    row["gas_usdt"] = (None if row_gas_usdt(
                        row, gas_usdt_xlayer, gas_usdt_eth, ink_gas_usdt=gas_usdt_ink) is None
                                       else format(row_gas_usdt(
                                           row, gas_usdt_xlayer, gas_usdt_eth, ink_gas_usdt=gas_usdt_ink), "f"))
                    row["gas_note"] = row_gas_note(
                        row, gas_usdt_xlayer, gas_note_xlayer, gas_usdt_eth,
                        gas_note_eth, ink_gas_usdt=gas_usdt_ink,
                        ink_note=gas_note_ink)
                    apply_net_blocker(
                        row, net, policy,
                        fixed_cost_usd=(bridge_fees or {}).get((source, hub)),
                        gas_usdt=row_gas_usdt(row, gas_usdt_xlayer, gas_usdt_eth, ink_gas_usdt=gas_usdt_ink),
                        family=row["family"])
                    if probe_shaped(row["blockers"]):
                        row["probe_eligible"] = probe_eligible(row["blockers"])
                        row["probe"] = probe_plan(
                            symbol=symbol, source_chain=source, hub_chain=hub,
                            blockers=row["blockers"], deposits=deposits,
                            withdraw_methods=withdraw_methods,
                            bridge_fees=bridge_fees, required_bps=required)
                    rows.append(row)
                # Kraken -> hub -> pool
                if sell_quote is not None and ask is not None and has_withdraw_method(
                        symbol, hub, (withdraw_methods or {}).get(symbol)):
                    blockers = _hop_blockers(
                        policy=policy, symbol=symbol, entry=entry, entries=(entry,),
                        pair=(kraken_recv, int(sell_quote.recv_ms)), now_ms=now_ms,
                        phase=phase, source=hub, dest=source,
                        measured_bridges=measured_bridges,
                        measured_symbols=measured_symbols,
                        v4_tiers=v4_tiers,
                        bridge_fee_usd=(bridge_fees or {}).get((hub, source)),
                        extra=pool_quality(entry, policy=policy, side="sell"))
                    entry_rows = [row for row in (address_book or [])
                                  if str(row.get("asset")) == symbol]
                    if not address_book_ready(symbol, hub, entry_rows,
                                              (withdraw_methods or {}).get(symbol)):
                        blockers = blockers + ["no_address_book"]
                    edge = edge_bps(ask, sell_quote.price_usd)
                    row = _row(
                        symbol=symbol, family="kraken_via_bridge_to_chain",
                        chain=f"{hub}->{source}",
                        buy_venue="kraken", sell_venue=sell_quote.venue,
                        buy_price=ask, sell_price=sell_quote.price_usd, edge=edge,
                        blockers=blockers,
                        notional=pool_ticket_usdt(entry, policy=policy),
                        entry=entry, policy=policy, pools=[(entry, "sell")])
                    row["source_chain"] = hub
                    row["dest_chain"] = source
                    row["ccip_dialect"] = route_core.dialect(hub, source)
                    row["hub_rail"] = f"Kraken withdrawal on {hub}"
                    row["bridge_fee_usd"] = (
                        format((bridge_fees or {}).get((hub, source)), "f")
                        if (bridge_fees or {}).get((hub, source)) is not None else None)
                    net = bridge_net_usdt(
                        gross_usdt=row["gross_usdt"],
                        notional_usdt=row["notional_usdt"],
                        bridge_fee_usd=(bridge_fees or {}).get((hub, source)),
                        policy=policy, family=row["family"],
                        gas_usdt=row_gas_usdt(row, gas_usdt_xlayer, gas_usdt_eth, ink_gas_usdt=gas_usdt_ink))
                    row["gas_usdt"] = (None if row_gas_usdt(
                        row, gas_usdt_xlayer, gas_usdt_eth, ink_gas_usdt=gas_usdt_ink) is None
                                       else format(row_gas_usdt(
                                           row, gas_usdt_xlayer, gas_usdt_eth, ink_gas_usdt=gas_usdt_ink), "f"))
                    row["gas_note"] = row_gas_note(
                        row, gas_usdt_xlayer, gas_note_xlayer, gas_usdt_eth,
                        gas_note_eth, ink_gas_usdt=gas_usdt_ink,
                        ink_note=gas_note_ink)
                    apply_net_blocker(
                        row, net, policy,
                        fixed_cost_usd=(bridge_fees or {}).get((hub, source)),
                        gas_usdt=row_gas_usdt(row, gas_usdt_xlayer, gas_usdt_eth, ink_gas_usdt=gas_usdt_ink),
                        family=row["family"])
                    rows.append(row)
    return rows


def _hop_blockers(*, policy: V2Policy, symbol: str, entry: dict, entries: tuple,
                  pair: tuple, now_ms: int, phase: str, source: str, dest: str,
                  measured_bridges: frozenset, v4_tiers: dict,
                  measured_symbols: frozenset = frozenset(),
                  extra: list[str], bridge_fee_usd=None) -> list[str]:
    """Blockers common to both bridge hops."""
    out: list[str] = []
    if not freshness.quotes_fresh(
            now_ms=now_ms, recv_a_ms=pair[0], recv_b_ms=pair[1],
            max_age_ms=policy.max_age_ms, max_pair_skew_ms=policy.max_pair_skew_ms):
        out.append("stale_or_skew")
    if phase == "scan":
        out.append("phase_scan")
    if source not in PHASE_CHAINS or dest not in PHASE_CHAINS:
        out.append("chain_out_of_phase")
    for item in entries:
        if item.get("kind") == "v4" and (
                v4_tiers.get(str(item.get("pool_id") or "").lower()) != "green"):
            out.append("hook_not_green")
    # Same two statements as the one-hop check: the pair, then the coin. A
    # per-mint token config makes the pair's sample a weaker fact than it looks.
    if (source, dest) not in measured_bridges:
        out.append("unmeasured_route")
    elif (source, dest, symbol) not in measured_symbols:
        out.append("bridge_unmeasured_symbol")
    # The bridge fee is only known once that edge has been measured for real.
    # A measured fee with a stale gas price is still unknown, not zero.
    if bridge_fee_usd is None:
        out.append("cost_unknown")
    out.extend(extra)
    return out


def v4_tier_blocker(entry: dict, v4_tiers: dict) -> str | None:
    """``hook_not_green`` when this v4 pool is not a plain pool, else ``None``.

    One implementation, asked per **pool**. A v4 pool with a hook (above all a
    dynamic-fee one) prices a swap its hook can still change, so the quote is
    not a promise. Measured 2026-10-08: SPCXx/eth had a green buy pool
    (``0x9b8f0f0d…``) and an amber hooked sell pool (``0x527e4a83…``, hook
    ``0xbf982845…``), and the row listed only ``atomic_route_unproven`` --
    because the family checked the tier of ``entry``, which is the buy side.
    A row trades every pool it names, so every pool has to pass.
    """
    if entry.get("kind") != "v4":
        return None
    if v4_tiers.get(str(entry.get("pool_id") or "").lower()) != "green":
        return "hook_not_green"
    return None


def _blockers(*, symbol: str, chain: str, phase: str, policy: V2Policy,
              buy, sell, same_pool: bool, deposits: dict,
              v4_tiers: dict, entry: dict, fresh: bool,
              measured_bridges: frozenset = frozenset(),
              measured_symbols: frozenset = frozenset(),
              address_book: list | None = None,
              withdraw_methods: dict | None = None,
              kraken_rail: bool = True) -> list[str]:
    out: list[str] = []
    if same_pool:
        out.append("same_pool")
    if not fresh:
        out.append("stale_or_skew")
    if phase == "scan":
        out.append("phase_scan")
    if chain not in PHASE_CHAINS:
        out.append("chain_out_of_phase")
    if v4_tier_blocker(entry, v4_tiers):
        out.append("hook_not_green")
    out.extend(pool_quality(entry, policy=policy,
                            side=pool_side(entry, buy, sell)))
    # The two Kraken-rail checks below only make sense when Kraken is one end of
    # the path. A chain -> chain row does not touch Kraken at all, and applying
    # them there put ``bridge_unmeasured_symbol`` on every reverse-family row,
    # which would keep a profitable one non-executable forever (found 2026-10-07
    # when the fee table first made those rows visible).
    if kraken_rail and buy is not None and chain:
        # The chain is the source, so the tokens have to reach Kraken from it.
        # No direct rail means a bridge, and a bridge with no arrival sample is
        # not executable (spec sections 6.2, 6.4).
        if not deposit_rails(symbol, chain, deposits,
                             method_networks((withdraw_methods or {}).get(symbol))):
            # Two different statements, kept apart on purpose. ``unmeasured_route``
            # means nobody has ever walked this chain pair. ``bridge_unmeasured_symbol``
            # means the pair has been walked, but not by *this* coin -- and a
            # per-mint token config is not transferable evidence. A caller that
            # has no per-symbol ledger gets the second one, which is the honest
            # reading of "the pair is known, the coin is not".
            if (chain, "solana") not in measured_bridges:
                out.append("unmeasured_route")
            elif (chain, "solana", symbol) not in measured_symbols:
                out.append("bridge_unmeasured_symbol")
    if kraken_rail and buy is None and sell is not None and chain:
        # Kraken -> chain needs a withdrawal rail, and the address book entry is
        # per asset. Without it the token cannot leave Kraken at all.
        rows = (withdraw_methods or {}).get(symbol)
        if not has_withdraw_method(symbol, chain, rows):
            out.append("no_withdraw_method")
        elif address_book is not None and not address_book_ready(
                symbol, chain, address_book, rows):
            out.append("no_address_book")
    return out


def pool_ticket_usdt(entry: dict, *, policy: V2Policy) -> Decimal:
    """The size this round asks the pool for: **the smallest ticket we will do**.

    Round 1 quotes one size per route and round 2 walks the ladder up
    (``live.ladder_sizes`` 25 -> 50 -> 100 -> 200) while the trade still pays.
    So the base size is the floor, not ``depth × share``.

    Operator 2026-10-08 (AGENTS.md 「第一原则：赚钱优先」): 「先看套利利润，
    以利润为准，如果有利润，即使我们的交易占池子超过 20% 也是可以的」。
    ``max_depth_share`` therefore stopped being a gate -- it is now an *evidence*
    number (``pool_quality_evidence``) that says how much of the pool our size
    is, and the profit test decides. A pool whose depth is a fraction of our
    ticket gets a real quote at the ticket size instead of a refusal: the quote
    already carries the impact, and the profit test reads it.
    """
    return policy.min_trade_quote_usdt


def pool_share(entry: dict, *, policy: V2Policy) -> Decimal | None:
    """Our base ticket as a fraction of the pool's depth (evidence, not a gate)."""
    depth = _d(entry.get("depth_usd"))
    if depth is None or depth <= 0:
        return None
    return pool_ticket_usdt(entry, policy=policy) / depth


def solana_quote_usdt(policy: V2Policy) -> Decimal:
    """The size to quote a Solana route at (see ``pool_ticket_usdt``)."""
    return policy.min_trade_quote_usdt


#: How many times a decision-time Solana re-quote may re-ask on 429. The scan,
#: the paper runner and the live runner each pace their own Jupiter limiter but
#: share one 1 rps free key, so this collision is structural, not a bad pool.
REQUOTE_TRIES = 3


def pool_quality_evidence(entry: dict, *, policy: V2Policy,
                          side: str | None = None) -> dict:
    """Pool-quality **evidence**, with the number behind it. Nothing here blocks.

    * ``thin_pool`` -- our ticket is more than ``max_depth_share`` of the pool.
    * ``impact_over`` -- the venue's own impact figure **in the direction we
      trade** is over ``max_price_impact_bps``.

    Both used to be ``below_depth`` blockers. Operator 2026-10-08 retired them as
    gates: 「以利润为准…即使我们的交易占池子超过 20% 也是可以的」. The impact
    figure is *inside* the price the venue returns for our size, so a quote that
    still shows a net profit after costs is a real opportunity however thin the
    pool is. What is left here is the number, so the board can print
    "占池子 300%、impact 800 bps" next to a profitable row instead of hiding it.

    The one pool-side gate that survives is **data quality**, and it is not in
    this function: ``price_outlier`` (a price a whole multiple away from the
    reference) means the quote is wrong, not cheap.

    ``side`` is ``"buy"`` when the row buys in this pool and ``"sell"`` when it
    sells into it (``None`` = the caller does not know, so both sides count).

    The same-pool round trip is measured and **reported, never enforced**
    (operator, 2026-10-08). It says "this pool has no liquidity on one side",
    which is a fact about the pool, not about our trade: we cross this pool
    once. Measured that night on 10 pools and 24 Solana mints:

    * HOODx/x-layer quoted a clean, smooth buy curve (109.96 -> 111.12 USDT over
      50 -> 200) while its reverse side was empty -- the round trip said 5242 bps
      and hid a usable pool;
    * the genuinely broken ones (INTCx, MRVLx, SLVx, CRCLx) had a buy price that
      did not move with size (constant output) or was 7,000-100,000 bps off the
      reference -- both visible without the reverse quote;
    * a healthy pool's round trip is ~2x its fee (TSLAx: 10.1 bps), so a 150 bps
      round-trip gate is a 75 bps one-way gate: twice as strict as intended.

    A pool our size cannot move through quotes a fake edge in BOTH directions:
    the buy side is pushed up and the sell side is pushed down. This has to be
    applied to **every** pool a path touches, not just the entry one (measured
    2026-10-07: TSLAx on Ethereum quoted a 12% cross-chain edge that was really
    a 250 USD pool -- see the open question in ``docs/xstocks-v2-todo.md``).
    """
    depth = _d(entry.get("depth_usd"))
    ticket = pool_ticket_usdt(entry, policy=policy)
    share = pool_share(entry, policy=policy)
    thin = share is not None and share > policy.max_depth_share
    detail = "pool_too_thin" if thin else ""
    buy, sell = entry.get("buy"), entry.get("sell")
    round_trip = None
    if buy is not None and sell is not None:
        trip = edge_bps(buy.price_usd, sell.price_usd)
        if trip is not None:
            round_trip = -trip
    # The venue's own impact figure. It is the only quality signal left when the
    # reverse quote failed, which is exactly when a thin pool looks tradable --
    # and it is directional: what matters is the side this row would cross.
    impact = None
    quotes = (buy, sell) if side is None else ((buy,) if side == "buy" else (sell,))
    for quote in quotes:
        value = _d((quote.extra or {}).get("impact_bps")) if quote is not None else None
        if value is None:
            continue
        if impact is None or abs(value) > abs(impact):
            impact = value
    impact_over = (impact is not None
                   and abs(impact) > policy.max_price_impact_bps)
    if impact_over:
        detail = detail or "price_impact"
    return {
        # Nothing blocks here any more (see the docstring): the key stays so the
        # callers keep one shape, and it is empty by construction.
        "blockers": [],
        "detail": detail,
        "depth_usd": depth,
        "ticket_usdt": ticket,
        "pool_share": share,
        "thin_pool": thin,
        "impact_over": impact_over,
        # True when there is something worth printing on the row.
        "flagged": thin or impact_over,
        "round_trip_bps": round_trip,
        # Reported, not enforced: "this pool has no liquidity on one side".
        "round_trip_wide": (None if round_trip is None
                            else round_trip > policy.venue_round_trip_max_bps),
        "impact_bps": impact,
    }


def pool_quality(entry: dict, *, policy: V2Policy,
                 side: str | None = None) -> list[str]:
    """The blocker names from ``pool_quality_evidence`` (see it for the rules)."""
    return pool_quality_evidence(entry, policy=policy, side=side)["blockers"]


def pool_identity(entry: dict, *, chain: str, symbol: str) -> dict:
    """The pool's **own** identity: venue + pool id, never the row's Kraken end.

    Measured 2026-10-08: the dead-pool list keyed off the row's ``buy_venue``,
    so one pool was recorded up to three times (``uniswap:<pool>`` and
    ``kraken:<pool>`` and ``jupiter:solana:<mint>``) and the count said 82 for
    46 real pools.
    """
    quote = entry.get("buy") or entry.get("sell")
    venue = str(getattr(quote, "venue", "") or entry.get("dex") or "?")
    return {
        "venue": venue,
        "pool_id": str(entry.get("pool_id") or ""),
        "chain": str(entry.get("chain") or chain or "?"),
        "symbol": str(entry.get("symbol") or symbol or "?"),
    }


def pool_side(entry: dict, buy, sell) -> str | None:
    """Which side of this pool a row crosses: ``"buy"``, ``"sell"``, or unknown.

    Decided by identity, not by guessing: the row's two prices are compared with
    the pool's own two quotes. Getting this wrong is how a directional rule
    (``price_impact``) would judge us by the *other* side of the book.
    """
    if buy is not None and buy is entry.get("buy"):
        return "buy"
    if sell is not None and sell is entry.get("sell"):
        return "sell"
    return None


def _row(*, symbol: str, family: str, chain: str, buy_venue: str, sell_venue: str,
         buy_price, sell_price, edge, blockers: list[str],
         notional: Decimal, entry: dict,
         policy: V2Policy | None = None,
         pools: list[dict] | None = None) -> dict:
    """One scan row.

    ``pools`` are the pools this row quoted (usually just ``entry``; two for the
    same-chain and cross-chain pairs). When ``policy`` is given the row carries
    the verdict *and the numbers* behind every pool it rejected, so a dead-pool
    entry can be re-read later instead of being taken on faith -- and so a row
    can never be blamed on a pool it did not actually touch.
    """
    deduped = [item for index, item in enumerate(blockers) if item not in blockers[:index]]
    # Staleness is evidence, not a gate (operator, 2026-10-08): round 1 exists to
    # *find* candidates, and round 2 re-quotes both legs back to back and
    # re-derives the skew from those fresh timestamps. Measured that day: with a
    # 10 s pairing window and a ~40 s round, four of the five live families
    # dropped whole families of rows ("整族拒绝") on a snapshot that was 15 s old.
    stale = False
    if "stale_or_skew" in deduped:
        deduped.remove("stale_or_skew")
        stale = True
    # A price that is a whole multiple away from the reference is not an
    # opportunity, it is a broken quote. Measured 2026-10-08 09:16, right after
    # the same-pool round trip stopped being a veto: KLACx quoted 758 against a
    # Kraken bid of 194 (+26,164 bps) with **no other blocker**, and its impact
    # figure was small, so the directional impact rule could not see it.
    if (policy is not None and edge is not None
            and edge > policy.max_price_outlier_bps
            and "price_outlier" not in deduped):
        deduped.append("price_outlier")
    gross = (Decimal(0) if edge is None
             else notional * edge / Decimal(10000))
    row = {
        "symbol": symbol,
        "family": family,
        "chain": chain,
        "buy_venue": buy_venue,
        "sell_venue": sell_venue,
        "pool_id": entry.get("pool_id") or "",
        "buy_price_usd": None if buy_price is None else format(buy_price, "f"),
        "sell_price_usd": None if sell_price is None else format(sell_price, "f"),
        "edge_t0_bps": None if edge is None else format(edge, "f"),
        # The deep stage overwrites this. Until then the two are the same, and
        # the gate never reads it (phase=scan stays closed).
        "edge_after_requote_bps": None if edge is None else format(edge, "f"),
        "notional_usdt": format(notional, "f"),
        "gross_usdt": format(gross, "f"),
        "net_usdt": None,
        # Executable means "nothing in the gate blocks this path in this phase".
        # While phase=scan every row carries phase_scan, so nothing is. With the
        # paper phase open, a row with no other blocker becomes actionable for
        # paper -- it is still never a live order (that needs the operator keys).
        "executable": not deduped,
        "blockers": deduped,
        "reason": headline_reason(deduped),
        "errors": list(entry.get("errors") or []),
    }
    # Which price the Kraken leg carries. Round 1 reads one ticker call for every
    # name, so this starts as the best bid/ask; a row near the bar is re-priced
    # on the real book by ``reprice_kraken_legs``, which overwrites the price and
    # this label. The label exists so the board can never again print a 2 USD
    # best bid as if it were the price our ticket would get (MCDx, 2026-10-08).
    if sell_venue == "kraken":
        row["kraken_price_basis"] = "best_bid"
    elif buy_venue == "kraken":
        row["kraken_price_basis"] = "best_ask"
    if stale:
        row["stale_or_skew"] = True
    if policy is not None:
        quoted: list[str] = []
        evidence: list[dict] = []
        wide = False
        # ``pools`` is ``[(pool, side), ...]``; the side is what the row crosses
        # in that pool, and it decides which quote's impact counts.
        for item in (pools if pools is not None else [entry]):
            pool, side = item if isinstance(item, tuple) else (item, None)
            claim = pool_identity(pool, chain=chain, symbol=symbol)
            # xChange has no pool to retire. A failed soft quote stays on the
            # row's errors; it must not enter the dead-pool ledger.
            if claim["venue"] == "xchange":
                continue
            pool_key = dead_pools_core.key(**claim)
            if pool_key not in quoted:
                quoted.append(pool_key)
            signals = pool_quality_evidence(pool, policy=policy, side=side)
            if signals["round_trip_wide"]:
                # Kept on the row as a *note*: the pool is one-sided. It is not
                # a blocker (see ``pool_quality_evidence``).
                wide = True
            # Evidence, not a verdict: since 2026-10-08 nothing about pool depth
            # blocks a row (operator: 以利润为准). This list is what the board
            # prints next to the row and what the pool ledger remembers, so a
            # thin pool stays visible instead of silently disappearing.
            if not (signals["flagged"] or signals["round_trip_wide"]):
                continue
            evidence.append({
                "key": pool_key,
                **claim,
                "reason": "pool_evidence",
                "detail": signals["detail"] or "round_trip_wide",
                "depth_usd": signals["depth_usd"],
                "pool_share": signals["pool_share"],
                "impact_bps": signals["impact_bps"],
                "notional_usdt": signals["ticket_usdt"],
                "round_trip_bps": signals["round_trip_bps"],
            })
        row["quoted_pools"] = quoted
        if wide:
            row["round_trip_wide"] = True
        if evidence:
            row["dead_pools"] = evidence
    return row


# The order the board reads a row's story in. Economics and data quality come
# first, plumbing last. Measured 2026-10-07: the board printed "这个币在这条桥上
# 没有到账样本" as the headline for NVDAx (-20 bps) and SPCXx (-15 bps), because
# that blocker happened to be first in the list. It read as "one sample away",
# when the row had no spread at all (operator). A missing sample is a fact about
# the route; a negative spread is a fact about the trade.
REASON_ORDER = (
    "same_pool", "below_depth", "price_outlier", "below_threshold", "below_net",
    "stale_or_skew",
    "xchange_closed", "below_min:xchange", "above_max:xchange",
    "xchange_expired", "xchange_unauthorized", "xchange_rate_limited",
    "xchange_price_unavailable", "xchange_unconfigured", "two_tx_inventory",
    "hook_not_green", "no_executor_chain", "no_lane", "no_selector",
    "no_adapter", "no_rail", "false_t0", "atomic_route_unproven",
    "atomic_required",
    "no_dest_pool", "no_withdraw_method", "no_address_book", "cost_unknown",
    "unmeasured_route", "bridge_unmeasured_symbol", "phase_scan",
    "chain_out_of_phase",
)

# The sets come from ``probe`` (one implementation, shared with the live gate
# and the board). ``cost_unknown`` is tolerated for *planning* but not for
# running: a bridge fee that was never measured is not zero, and a probe cannot
# be budgeted without it. Keeping it here is what makes the board say which
# number is missing instead of hiding the row (operator, 2026-10-07: show the
# real blocker).
PROBE_CLEARABLE = probe_core.PROBE_CLEARABLE
NOT_A_VERDICT = PROBE_CLEARABLE | probe_core.INFORMATIONAL
PROBE_PLANNABLE = PROBE_CLEARABLE | probe_core.TOLERATED


def headline_reason(blockers) -> str:
    """The one reason the board leads with. See ``REASON_ORDER``."""
    names = [str(item) for item in (blockers or [])]
    for name in REASON_ORDER:
        if name in names:
            return name
    return names[0] if names else ""


def probe_eligible(blockers) -> bool:
    """Is the *only* thing between this row and a trade a route sample?

    True when the row's spread verdict is fine and every remaining blocker can
    be settled by walking the route once. This is the trigger the operator asked
    for on 2026-10-07: do not pre-sample every coin on every bridge -- buy the
    smallest amount that survives the route when there is something to buy for,
    and let the arrival unlock the trade.

    Since 2026-10-09 the flag also covers the **sample** stage: 缺样本不是拒绝
    理由, so a row whose only other objections are the two estimates a crossing
    replaces (``below_threshold`` / ``below_net``) is still worth walking. Which
    of the two stages actually runs is decided by ``probe.verdict`` (the live
    gate and the budget), not by this flag.
    """
    return probe_core.eligible(blockers) or probe_core.sample_eligible(blockers)


def probe_shaped(blockers) -> bool:
    """Is this row a probe with a missing input, rather than a different problem?

    Wider than ``probe_eligible`` by exactly one blocker: an unmeasured bridge
    fee. Such a row gets a plan so the board can name what it is waiting for,
    but it is not eligible to run until that number exists.
    """
    return probe_core.plannable(blockers) or probe_core.sample_plannable(blockers)


def probe_plan(*, symbol: str, source_chain: str, hub_chain: str, blockers,
               deposits: dict, withdraw_methods: dict | None,
               bridge_fees: dict | None, required_bps: Decimal) -> dict:
    """What the smallest crossing of this route costs, and what it is waiting for.

    The probe is the first slice of the trade, not a separate errand: it buys
    the smallest amount that survives every leg, sends it across, and **the
    arrival is what unlocks the full size**. Two inputs have to be known before
    it may run, and both are refusals rather than zeroes when they are missing:

    * ``deposit_min_units`` -- Kraken's credit minimum on the hub chain. Below
      it the coins are not credited, so the transfer is a loss, and a later
      top-up does not recover the first one (lessons 3.4, 3.10).
    * ``bridge_fee_usd`` -- charged per message in the source chain's gas token,
      and independent of the amount. An unmeasured fee is not zero.

    ``waiting_on`` non-empty means the plan is real but not runnable yet.
    """
    networks = method_networks((withdraw_methods or {}).get(symbol))
    hub_rails = deposit_rails(symbol, hub_chain, deposits or {}, networks)
    minimums = [_d(row.get("minimum")) for row in hub_rails]
    minimums = [value for value in minimums if value is not None and value > 0]
    deposit_min = max(minimums) if minimums else None
    fee = (bridge_fees or {}).get((source_chain, hub_chain))
    waiting_on = []
    if deposit_min is None:
        waiting_on.append("deposit_min_units")
    if fee is None:
        waiting_on.append("bridge_fee_usd")
    return {
        "symbol": symbol,
        "source_chain": source_chain,
        "hub_chain": hub_chain,
        "clears": sorted(set(str(item) for item in (blockers or []))
                         & PROBE_CLEARABLE),
        "deposit_min_units": None if deposit_min is None else format(deposit_min, "f"),
        "bridge_fee_usd": None if fee is None else format(fee, "f"),
        "required_edge_bps": format(required_bps, "f"),
        "waiting_on": waiting_on,
        "runnable": not waiting_on,
        "note": ("先买最小可行量过桥；到账即写样本，再按同一个价差补足剩余额度。"
                 "桥费按条收费，所以样本是这笔交易的第一段，不是额外一笔。"),
    }


#: EVM chains the cost model can price in USD today. X Layer and Ethereum both
#: have a wallet-independent gas price (``eth_gasPrice``) and a native-token
#: price the scan keeps fresh (``gas_prices.GAS_TOKENS``). Ethereum was added
#: 2026-10-09 with the chain -> Kraken executor: before that an eth row's gas
#: was silently 0.
PRICED_GAS_CHAINS = ("x-layer", "eth", "ton", "ink")


def row_chains(row: dict) -> list[str]:
    """Every chain this row names, in order, de-duplicated."""
    fields = [str(row.get("chain") or ""), str(row.get("source_chain") or ""),
              str(row.get("dest_chain") or "")]
    parts: list[str] = []
    for field in fields:
        for part in field.split("->"):
            part = part.strip()
            if part and part not in parts:
                parts.append(part)
    return parts


def row_gas_chains(row: dict) -> list[str]:
    """The priced chains whose gas this row pays, if any."""
    parts = row_chains(row)
    return [chain for chain in PRICED_GAS_CHAINS if chain in parts]


def row_gas_usdt(row: dict, xlayer_gas_usdt: Decimal | None = Decimal(0),
                 eth_gas_usdt: Decimal | None = Decimal(0),
                 ton_gas_usdt: Decimal | None = Decimal(0), *,
                 ink_gas_usdt: Decimal | None = Decimal(0)) -> Decimal | None:
    """The gas line to charge this row: 0, the measured sum, or None (unknown).

    A path that touches two priced chains pays both. Unknown is not zero:
    one unpriced chain makes the whole line ``None``, and ``net_after_costs``
    then refuses the row instead of treating a real cost as free. ``ton`` is
    the ston.fi burn (``legs_ton.BURN_TON`` times the TON price), not an EVM
    gas market.
    """
    lines = {"x-layer": xlayer_gas_usdt, "eth": eth_gas_usdt,
             "ton": ton_gas_usdt, "ink": ink_gas_usdt}
    charges = [lines[chain] for chain in row_gas_chains(row)]
    if not charges:
        return Decimal(0)
    total = Decimal(0)
    for value in charges:
        if value is None:
            return None
        total += Decimal(str(value))
    return total


def row_gas_note(row: dict, xlayer_gas_usdt: Decimal | None, note: str,
                 eth_gas_usdt: Decimal | None = Decimal(0),
                 eth_note: str = "not_supplied",
                 ton_gas_usdt: Decimal | None = Decimal(0),
                 ton_note: str = "not_supplied", *,
                 ink_gas_usdt: Decimal | None = Decimal(0),
                 ink_note: str = "not_supplied") -> str:
    values = {"x-layer": (xlayer_gas_usdt, note), "eth": (eth_gas_usdt, eth_note),
              "ton": (ton_gas_usdt, ton_note), "ink": (ink_gas_usdt, ink_note)}
    chains = row_gas_chains(row)
    if not chains:
        return "no_evm_leg"
    lines = []
    for chain in chains:
        value, why = values[chain]
        lines.append(why if value is not None else f"unknown:{why}")
    return "+".join(lines)


def bridge_net_usdt(*, gross_usdt, notional_usdt, bridge_fee_usd, policy,
                    family: str, gas_usdt: Decimal | None = Decimal(0)
                    ) -> Decimal | None:
    """``gross`` minus the costs we can actually name, or ``None``.

    The bridge fee is charged per message and is now measured, so it is
    subtracted. The slippage buffer is always charged; the Kraken taker fee only
    when a Kraken leg is in the path -- and ``family`` is what decides that, so
    the scan, the board and the live gate cannot drift apart again (measured
    2026-10-07: they differed by exactly 10 bps on ``chain_to_chain``). A
    missing fee or a missing notional makes the net unknown -- and an unknown
    net is not a free trade.
    """
    # ``gas_usdt`` is the path's own gas line, computed by the caller: a Decimal
    # (0 when the path has no EVM leg) or ``None`` when it could not be priced --
    # and unknown gas refuses the row rather than being free.
    return costs.net_after_costs(
        gross_usdt=gross_usdt, notional_usdt=notional_usdt,
        fixed_cost_usd=bridge_fee_usd, policy=policy, family=family,
        gas_cost_usd=gas_usdt)


def update_dead_pools(entries: dict, rows: list[dict], *, now_ms: int,
                      recheck: set[str] | None = None) -> dict:
    """Fold one round's pool verdicts into the dead-pool ledger.

    Two rules, both from the 2026-10-08 incident where a single wrong round hid
    a healthy pool for six hours:

    * a pool is marked from the **row's own evidence** (``_row`` records the
      pool's identity, depth, ticket and round trip), never from the row's
      Kraken end -- keying off the buy venue recorded one pool up to three times;
    * a pool that was on this round's recheck rotation and comes back *clean* is
      cleared. A pool that produced no rows at all stays as it was: "we did not
      see it" is not "it is healthy".

    Returns ``{"marked": n, "revived": n}`` for the round's counters.
    """
    recheck = set(recheck or ())
    quoted: set[str] = set()
    # One pool shows up in several rows of the same round (chain -> Kraken,
    # Kraken -> chain, both bridge hops), so the verdicts are merged and counted
    # **once per round**: ``hits`` is "how many rounds rejected it", which is what
    # ``dead_pool_confirm_hits`` compares against.
    by_key: dict[str, dict] = {}
    for row in rows or []:
        for pool_key in row.get("quoted_pools") or []:
            quoted.add(str(pool_key))
        for item in row.get("dead_pools") or []:
            pool_key = str(item.get("key") or "")
            if not pool_key:
                continue
            quoted.add(pool_key)
            merged = dict(by_key.get(pool_key) or {})
            merged.update({name: value for name, value in item.items()
                           if value not in (None, "")})
            merged.setdefault("detail", str(item.get("detail") or ""))
            by_key[pool_key] = merged
    dead_now: set[str] = set()
    for pool_key, item in sorted(by_key.items()):
        dead_now.add(pool_key)
        dead_pools_core.mark(
            entries, pool_key, now_ms=now_ms,
            reason=str(item.get("reason") or "below_depth"),
            detail=str(item.get("detail") or ""),
            depth_usd=item.get("depth_usd"),
            notional_usdt=item.get("notional_usdt"),
            round_trip_bps=item.get("round_trip_bps"))
    marked = len(dead_now)
    revived = 0
    for pool_key in sorted(recheck & quoted - dead_now):
        if dead_pools_core.clear(entries, pool_key) is not None:
            revived += 1
    return {"marked": marked, "revived": revived}


def apply_net_blocker(row: dict, net, policy: V2Policy, *,
                      fixed_cost_usd: Decimal | None = None,
                      gas_usdt: Decimal = Decimal(0),
                      family: str | None = None,
                      ceiling: Decimal | None = None) -> dict:
    """Write ``net_usdt`` and, when it is known and too small, block the row.

    ``executable`` used to mean only "no gate blocker", so a bridged row with a
    *negative* net still printed as executable on the board (measured
    2026-10-07: SPYx solana->x-layer at -64 bps, TSLAx at -8.6 bps). The live
    gate catches that, but the board did not, and a board that calls a losing
    row executable is worse than no board.
    """
    row["net_usdt"] = None if net is None else format(net, "f")
    if net is not None and net < policy.min_net_profit_usdt:
        # ``net`` was computed at the **base ticket** (25 USDT since 2026-10-08).
        # A cost that is charged per message -- the bridge fee, the first SPL
        # transfer's rent -- does not grow with the ticket, so the same spread
        # can pay at the ceiling while losing at the floor. The ladder in round 2
        # walks 25 -> 50 -> 100 -> 200 and trades the largest rung that clears
        # (operator 2026-10-08: 「25 起报，过了门槛的再往上加档」), so round 1
        # must not call such a row a loss -- that is how a real 200 USDT
        # opportunity gets hidden by the size we happened to ask about.
        #
        # The amortised figure keeps the **measured price** (it is an upper
        # bound: a bigger ticket usually gets a worse one) and is labelled as
        # such, so nothing here replaces the ladder's real re-quote.
        size = ceiling if ceiling is not None else policy.per_trade_quote_usdt
        edge = (_d(row.get("edge_after_requote_bps"))
                or _d(row.get("edge_t0_bps")))
        notional = _d(row.get("notional_usdt")) or policy.min_trade_quote_usdt
        amortised = None
        if (fixed_cost_usd is not None and edge is not None and size > notional):
            amortised = costs.net_after_costs(
                gross_usdt=size * edge / Decimal(10000), notional_usdt=size,
                fixed_cost_usd=fixed_cost_usd, policy=policy, family=family,
                gas_cost_usd=gas_usdt)
        if amortised is not None and amortised >= policy.min_net_profit_usdt:
            row["net_at_ceiling_usdt"] = format(amortised, "f")
            row["net_ceiling_size_usdt"] = format(size, "f")
            row["net_note"] = "fixed_cost_amortises_with_size"
        else:
            if "below_net" not in row["blockers"]:
                row["blockers"] = row["blockers"] + ["below_net"]
            row["executable"] = False
    # Recompute, do not "keep the first": appending below_net to a row whose
    # headline was a plumbing blocker is exactly how a losing row kept printing
    # "no arrival sample" as its reason (operator, 2026-10-07).
    row["reason"] = headline_reason(row["blockers"])
    refresh_probe_flag(row)
    return row


def refresh_probe_flag(row: dict) -> bool:
    """Re-derive ``probe_eligible`` from the row's **final** blockers (A49).

    The flag used to be computed before the net verdict was applied, so a row
    could carry ``probe_eligible: true`` and ``below_net`` at the same time.
    Measured 2026-10-09: TSLAx/x-layer did exactly that after
    ``apply_net_blocker`` (and, separately, after ``reprice_kraken_legs``
    rewrote ``blockers`` itself). Nothing spent money -- the gate reads the
    blockers through ``probe.verdict`` -- but a flag that disagrees with the row
    it describes is what the board reads, and that is how a losing row looks
    runnable.
    """
    if "probe_eligible" not in row:
        return False
    row["probe_eligible"] = probe_eligible(row["blockers"])
    return True


#: Kraken's ``Depth`` endpoint takes **one** pair per request (measured
#: 2026-10-08: two pairs answer ``EGeneral:Invalid arguments``, so
#: ``fetch_depth``'s chunking is useless for it). A depth pass is therefore one
#: call per symbol, and it needs a budget of its own.
KRAKEN_DEPTH_MAX = 25


def reprice_kraken_legs(rows: list[dict], *, http, policy: V2Policy,
                        session: str, counters: dict,
                        limit: int = KRAKEN_DEPTH_MAX,
                        data_dir: Path | str | None = None) -> int:
    """Price the Kraken leg on the **real book, for our own size**.

    Operator 2026-10-08 (AGENTS.md 「第一原则：赚钱优先」): 「查询在池子那边套利
    允许的金额是多少，然后看在 orderbook 上把这个金额交易完，会以什么平均价格
    成交完，成交的话我们是否有套利的利润」。The orderbook is a pricer here, not a
    gate: no slippage cap and no book-share cap is applied -- the profit test is
    the only judge.

    Measured that afternoon: Kraken MCDx's best bid was **235.00 for 0.008511
    shares ≈ 2 USD**, with the next level at 230.52. A 25 USDT ticket therefore
    printed +203 bps with an empty blocker list, and the resident live runner
    picked the row up. Walking the book for the ticket turns it into the losing
    trade it really is (VWAP ≈ 230.60 -> net −0.36 USDT).

    Only rows whose gross edge is within ``stage2_buffer_bps`` of the bar are
    re-priced: one depth call per *candidate*, not per row. A book that cannot
    fill the ticket gets ``kraken_depth_short`` -- that is "this trade cannot
    happen", a fact, not a price opinion.
    """
    required = policy.required_edge_bps(session)
    floor = required - policy.stage2_buffer_bps
    targets = [row for row in rows
               if (edge := _d(row.get("edge_t0_bps"))) is not None
               and edge >= floor
               and (row.get("sell_venue") == "kraken"
                    or row.get("buy_venue") == "kraken")]
    if not targets:
        return 0
    targets.sort(key=lambda row: _d(row.get("edge_t0_bps")) or Decimal(0),
                 reverse=True)
    symbols: list[str] = []
    for row in targets:
        symbol = str(row.get("symbol") or "")
        if symbol and symbol not in symbols:
            symbols.append(symbol)
        if len(symbols) >= limit:
            break
    books: dict[str, dict] = {}
    for symbol in symbols:
        got = kraken.fetch_depth(http, [symbol], count=25)
        counters["kraken_depth_calls"] = counters.get("kraken_depth_calls", 0) + 1
        books[symbol] = (got.get("depth") or {}).get(symbol) or {}
    # A chain -> Kraken row has a transfer line the round-1 snapshot never
    # charged, which is why its ``net_usdt`` used to be null for every Solana
    # row. One SOL price for the whole pass, and the same "has this mint been
    # transferred before" rule the executor uses (A27).
    sol_usd = None
    if any(str(row.get("chain")) == "solana" for row in targets):
        sol_usd = (kraken.native_usd(http, ["SOL"]) or {}).get("SOL")
        counters["kraken_calls"] = counters.get("kraken_calls", 0) + 1
    done = 0
    for row in targets:
        book = books.get(str(row.get("symbol") or ""))
        if not book:
            continue
        sell_side = row.get("sell_venue") == "kraken"
        price = _d(row.get("sell_price_usd") if sell_side
                   else row.get("buy_price_usd"))
        ticket = _d(row.get("notional_usdt")) or policy.min_trade_quote_usdt
        if price is None or price <= 0:
            continue
        units = ticket / price
        fill = kraken.book_fill(
            book.get("bids" if sell_side else "asks"), units,
            side="sell" if sell_side else "buy")
        row["kraken_price_basis"] = "book_vwap"
        row["kraken_best_price"] = (None if fill["best"] is None
                                    else format(fill["best"], "f"))
        row["book_units"] = format(units, "f")
        row["book_levels_used"] = fill["levels_used"]
        row["book_slip_bps"] = (None if fill["slip_bps"] is None
                                else format(fill["slip_bps"], "f"))
        row["book_notional_usd"] = (None if fill["notional_usd"] is None
                                    else format(fill["notional_usd"], "f"))
        # The threshold/净利 verdicts are re-derived at the real price; every
        # other blocker (data quality, plumbing) is left exactly as it was.
        kept = [name for name in (row.get("blockers") or [])
                if name not in ("below_threshold", "below_net",
                                "kraken_depth_short")]
        if fill["vwap"] is None or not fill["ok"]:
            row["book_short"] = fill["short"]
            kept.append("kraken_depth_short")
            counters["kraken_depth_short"] = (
                counters.get("kraken_depth_short", 0) + 1)
        else:
            vwap = fill["vwap"]
            if sell_side:
                buy_price = _d(row.get("buy_price_usd"))
                row["sell_price_usd"] = format(vwap, "f")
                edge = edge_bps(buy_price, vwap) if buy_price else None
                row["edge_t0_bps"] = (None if edge is None
                                      else format(edge, "f"))
                row["gross_usdt"] = (None if edge is None else format(
                    ticket * edge / Decimal(10000), "f"))
                # Profit is the only judge, so round 1 prices the whole leg --
                # including the transfer it will really pay -- instead of
                # leaving ``net_usdt`` null and letting a negative trade print
                # as "no blocker" (MCDx, 2026-10-08).
                fixed, note = (Decimal(0), "no_transfer_line")
                if row.get("hub_fixed_note"):
                    raw_fee = row.get("hub_fixed_usd")
                    if raw_fee in (None, ""):
                        fixed, note = None, "bridge_fee_unknown"
                    else:
                        fixed, note = Decimal(str(raw_fee)), str(row["hub_fixed_note"])
                elif str(row.get("chain")) == "solana":
                    first = live_core.first_transfer_for(
                        str(row.get("symbol")),
                        Path(data_dir or DEFAULT_DATA)
                        / live_core.TRANSFER_SEEN_NAME)
                    if sol_usd is None or sol_usd <= 0:
                        fixed, note = None, "sol_price_unknown"
                    else:
                        fixed = (guards.sol_transfer_fee(first_for_mint=first)
                                 * sol_usd)
                        note = "measured" if not first else "first_transfer"
                gas = _d(row.get("gas_usdt")) if row.get("gas_usdt") else Decimal(0)
                net = None
                if edge is not None and fixed is not None:
                    net = costs.net_after_costs(
                        gross_usdt=ticket * edge / Decimal(10000),
                        notional_usdt=ticket, fixed_cost_usd=fixed,
                        policy=policy, family=row.get("family"),
                        gas_cost_usd=gas)
                loses = False
                if net is not None:
                    row["net_usdt"] = format(net, "f")
                    net_bps = net / ticket * Decimal(10000)
                    row["net_bps"] = format(net_bps, "f")
                    loses = (net < policy.min_net_profit_usdt or net_bps < required)
                # A per-message cost (bridge fee, TON burn, first SPL rent) can
                # lose at the 25 USDT quote and pay at the ladder ceiling. Round
                # 1 must not hide that row; round 2 still re-quotes the size.
                ceiling = policy.per_trade_quote_usdt
                fixed_part = ((fixed or Decimal(0)) + (gas or Decimal(0))
                              if fixed is not None and gas is not None else None)
                if (loses and fixed_part is not None and fixed_part > 0
                        and ceiling > ticket and edge is not None):
                    amortised = costs.net_after_costs(
                        gross_usdt=ceiling * edge / Decimal(10000),
                        notional_usdt=ceiling, fixed_cost_usd=fixed,
                        policy=policy, family=row.get("family"),
                        gas_cost_usd=gas)
                    amort_bps = amortised / ceiling * Decimal(10000)
                    if (amortised >= policy.min_net_profit_usdt
                            and amort_bps >= required):
                        row["net_at_ceiling_usdt"] = format(amortised, "f")
                        row["net_ceiling_size_usdt"] = format(ceiling, "f")
                        note = f"{note}+amortised"
                        loses = False
                if loses:
                    kept.append("below_net")
                row["net_note"] = note
            else:
                sell_price = _d(row.get("sell_price_usd"))
                row["buy_price_usd"] = format(vwap, "f")
                edge = edge_bps(vwap, sell_price) if sell_price else None
                row["edge_t0_bps"] = (None if edge is None
                                      else format(edge, "f"))
                row["gross_usdt"] = (None if edge is None else format(
                    ticket * edge / Decimal(10000), "f"))
                net, fee_reason = kraken_to_chain_net(
                    notional=ticket, buy_price=vwap, sell_price=sell_price,
                    fee_units=_d(row.get("withdraw_fee_units")), policy=policy)
                row["indicative_net_usdt"] = (None if net is None
                                              else format(net, "f"))
                row["net_bps"] = (None if net is None else format(
                    net / ticket * Decimal(10000), "f"))
                if fee_reason:
                    row["fee_note"] = fee_reason
                if net is not None and (net < policy.min_net_profit_usdt
                                        or net / ticket * Decimal(10000)
                                        < required):
                    kept.append("below_net")
            edge = _d(row.get("edge_t0_bps"))
            if edge is None or edge < required:
                kept.append("below_threshold")
        row["blockers"] = [item for index, item in enumerate(kept)
                           if item not in kept[:index]]
        row["executable"] = not row["blockers"]
        row["reason"] = headline_reason(row["blockers"])
        # This path rewrites the blockers itself, so it has to refresh the same
        # flag ``apply_net_blocker`` refreshes (A49).
        refresh_probe_flag(row)
        done += 1
    return done


def rank_stage2(rows: list[dict], *, threshold_bps: Decimal, buffer_bps: Decimal,
                limit: int) -> list[dict]:
    """Names close enough to the threshold to deserve a real quote."""
    floor = threshold_bps - buffer_bps
    picked = [(edge, row) for row in rows
              if (edge := _d(row.get("edge_t0_bps"))) is not None
              and edge >= floor
              and "same_pool" not in (row.get("blockers") or [])]
    picked.sort(key=lambda item: item[0], reverse=True)
    return [row for _edge, row in picked[:limit]]


def listed_symbols(units: dict) -> list[str]:
    """Every Kraken symbol we have a row for, whatever the pair status is.

    ``post_only`` is a Kraken order flag. The chain leg trades all day. Using
    ``status == "online"`` as the watch list dropped 165 names on 2026-10-10.
    """
    return [symbol for symbol, row in units.items() if isinstance(row, dict)]


# ---------------------------------------------------------------------------
# Round
# ---------------------------------------------------------------------------
def run_scan(*, policy: V2Policy | None = None, http: Http | None = None,
             data_dir: Path | str = DEFAULT_DATA, book: Path | str = census_feed.DEFAULT_BOOK,
             census_path: Path | str = census_feed.DEFAULT_CENSUS,
             v4_path: Path | str = census_feed.DEFAULT_V4,
             units_path: Path | str = census_feed.DEFAULT_UNITS,
             deposits_path: Path | str = EXIT_CACHE,
             full_bilateral: bool = False,
             stage2_limit: int | None = None) -> dict:
    started = time.monotonic()
    policy = policy or V2Policy.load()
    http = http or Http()
    now_ms = int(time.time() * 1000)
    # The US session decides the bar (20 bps open, 4 bps closed) and it is
    # stamped on the snapshot so a later reader can tell which bar applied.
    session = _session_label(now_ms)
    # Symbols whose (symbol, chain) Kraken has no withdrawal rail for: the
    # Kraken->chain arc does not exist, so no row is emitted (counted instead).
    missing_rails: list[str] = []
    feed = JupiterFeed(http)
    counters: dict = {"kraken_calls": 0, "jupiter_calls": 0, "http_429": 0,
                      "symbols": 0, "real_quotes": 0, "full_bilateral": full_bilateral}
    ton = ton_feed.TonFeed(http)

    units = census_feed.kraken_symbols(units_path)
    listed = listed_symbols(units)
    deposits = load_deposits(deposits_path)
    multipliers = load_multipliers()

    ticker = kraken.fetch_ticker(http, listed, tokenized=True)
    counters["kraken_calls"] += 1
    kraken_rows = ticker["ticker"]
    kraken_errors = ticker["errors"]

    census = census_feed.load_census(census_path)
    pools = census_feed.census_pools(census)
    # TON is not EVM: census_pools() drops every EQ... jetton, so it is read
    # separately -- otherwise the one chain with a Kraken rail and a six-figure
    # pool stays invisible (measured 2026-10-07: SPYx / USDT, 107,918 USD).
    ton_pools = census_feed.ton_pool_rows(census)
    v4 = census_feed.load_v4(v4_path)
    # v4 pools live in the separate watch, not in the census candidate list.
    pools += census_feed.v4_pool_rows(v4, book=book)
    # A pool is a reason to quote the mint, Kraken listing or not. The chain
    # side of any-to-any does not wait for a CEX pair status.
    pool_symbols = ({pool["symbol"] for pool in pools}
                    | {pool["symbol"] for pool in ton_pools})
    solana = census_feed.solana_universe(
        list(dict.fromkeys([*listed, *sorted(pool_symbols)])), book=book)

    # Stage 1 ranking for Solana. Reference only: it sorts, it does not price.
    references: dict[str, Decimal] = {}
    if not full_bilateral and solana:
        references = feed.price_v3([row["token_address"] for row in solana.values()])
        counters["jupiter_calls"] = feed.calls

    measured_bridges = load_measured_bridges(Path(data_dir) / ROUTE_SAMPLES.name)
    # The per-symbol view of the same ledger. ``bridge_unmeasured_symbol`` is
    # only allowed to mean "this coin has no arrival sample" (see
    # ``load_measured_symbols``), and the probe plan is what clears it.
    measured_symbols = load_measured_symbols(Path(data_dir) / ROUTE_SAMPLES.name)
    # Gas-token prices are refreshed **here** (A1). Until 2026-10-09 the only
    # way they moved was a human running ``scripts/xstocks_v2_bridge_fees.py
    # refresh``; nothing called it on a timer, so both prices went 19.7 hours
    # stale and five board rows printed "等：bridge_fee_usd" all night. Only
    # missing-or-stale assets are quoted, at most once per round; a failure
    # leaves the old row alone (stale keeps failing the allowance) and is
    # written into the counters.
    price_refresh = gas_prices_core.ensure_fresh(
        data_dir, now_ms=now_ms, http=http,
        need=gas_prices_core.GAS_TOKENS)
    counters["gas_price_refresh"] = {
        "stale": price_refresh["stale"],
        "refreshed": price_refresh["refreshed"],
        "failed": price_refresh["failed"],
    }
    prices = bridge_fees_core.load_prices(
        Path(data_dir) / bridge_fees_core.PRICES_NAME)
    # Bridge fees are per message and paid in the source chain's gas token. The
    # chain is asked every round (A3): the measured file is the receipt and the
    # fallback, not the only source. An edge whose fee cannot be read *and* has
    # no receipt, or one whose gas price is missing or stale, stays
    # ``cost_unknown`` -- unknown is never 0.
    measured_fees = bridge_fees_core.load_fees(
        Path(data_dir) / bridge_fees_core.FEES_NAME)
    live_fees, live_fee_notes = live_fees_core.quote_edges(
        http=http, now_ms=now_ms)
    merged_fees = {**measured_fees, **live_fees}
    bridge_fees = bridge_fees_core.fee_table(
        fees=merged_fees, prices=prices, now_ms=now_ms)
    bridge_fee_unknown = bridge_fees_core.unknown_reasons(
        fees=merged_fees, prices=prices, now_ms=now_ms)
    counters["bridge_fees_known"] = sorted(
        f"{source}->{dest}" for source, dest in bridge_fees)
    counters["bridge_fees_unknown"] = bridge_fee_unknown
    counters["bridge_fee_live"] = live_fee_notes
    counters["bridge_fee_live_vs_evidence"] = live_fees_core.compare_with_evidence(
        live_fees, measured_fees)
    # Which gas price each bridge cost rides on, and whether it is still fresh.
    # A stale OKB price is why every x-layer bridge read `cost_unknown` on
    # 2026-10-07 -- the board has to be able to say that out loud.
    gas_price_report = bridge_fees_core.price_report(
        data_dir=data_dir, now_ms=now_ms)
    # One EVM action (approve + swap + deposit) costs gas, and the quoted price
    # of a swap does not contain it. X Layer is where we execute, and both inputs
    # are now available: the chain's gas price (one eth_gasPrice call) and OKB's
    # USD price (wallet-independent, see bridge_fees.price_report). A failed read
    # reuses the previous round's value for up to an hour instead of blanking
    # every bridge row on a hiccup.
    xlayer_cfg = evm.CHAINS.get("x-layer")
    prev_snapshot = read_json(Path(data_dir) / "xstocks_v2_scan.json") or {}
    cache = ((prev_snapshot.get("gas_lines") or {}).get("cache") or {})
    gas_wei: Decimal | None = None
    if xlayer_cfg is not None:
        try:
            gas_wei = evm.gas_price_wei(http, xlayer_cfg)
        except Exception:  # noqa: BLE001
            gas_wei = None
    gas_wei_source = "eth_gasPrice"
    cached = _d(cache.get("wei")) if isinstance(cache, dict) else None
    cache_age_s = (now_ms - int(cache.get("as_of_ms") or 0)) / 1000 \
        if isinstance(cache, dict) and cache.get("as_of_ms") else None
    if gas_wei is None and cached is not None and cache_age_s is not None \
            and cache_age_s < 3600:
        gas_wei, gas_wei_source = cached, "cached"
    # A2: the gas line carries the same age allowance as the bridge fee. Until
    # 2026-10-09 a 19.7 hour old OKB price still produced a confident
    # "one X Layer action = 0.00114 USDT" while the bridge fee on the same price
    # had already gone ``gas_price_stale``.
    okb_row = (gas_price_report.get("prices") or {}).get("OKB") or {}
    xlayer_gas_usdt, xlayer_gas_note = costs.gas_cost_usd(
        policy, chain="x-layer", gas_price_wei=gas_wei,
        native_usd=_d(okb_row.get("usd")),
        native_as_of_ms=okb_row.get("as_of_ms"), now_ms=now_ms,
        max_native_age_ms=bridge_fees_core.MAX_PRICE_AGE_MS)
    counters["xlayer_gas_usdt"] = (None if xlayer_gas_usdt is None
                                   else format(xlayer_gas_usdt, "f"))
    counters["xlayer_gas_price_wei"] = (None if gas_wei is None
                                        else format(gas_wei, "f"))
    counters["xlayer_gas_source"] = gas_wei_source
    counters["xlayer_gas_note"] = xlayer_gas_note

    # Ethereum mainnet, same shape as the X Layer line above. Added 2026-10-09
    # with the eth side of chain -> Kraken: before it, an eth row's gas was 0
    # and its net profit looked ~0.12 USDT better than it is at 25 USDT.
    eth_cfg = evm.CHAINS.get("eth")
    eth_wei: Decimal | None = None
    if eth_cfg is not None:
        try:
            eth_wei = evm.gas_price_wei(http, eth_cfg)
        except Exception:  # noqa: BLE001
            eth_wei = None
    eth_wei_source = "eth_gasPrice"
    cached_eth = _d(cache.get("eth_wei")) if isinstance(cache, dict) else None
    eth_cache_age_s = ((now_ms - int(cache.get("eth_as_of_ms") or 0)) / 1000
                       if isinstance(cache, dict) and cache.get("eth_as_of_ms")
                       else None)
    if (eth_wei is None and cached_eth is not None
            and eth_cache_age_s is not None and eth_cache_age_s < 3600):
        eth_wei, eth_wei_source = cached_eth, "cached"
    eth_row = (gas_price_report.get("prices") or {}).get("ETH") or {}
    eth_gas_usdt, eth_gas_note = costs.gas_cost_usd(
        policy, chain="eth", gas_price_wei=eth_wei,
        native_usd=_d(eth_row.get("usd")),
        native_as_of_ms=eth_row.get("as_of_ms"), now_ms=now_ms,
        max_native_age_ms=bridge_fees_core.MAX_PRICE_AGE_MS)
    counters["eth_gas_usdt"] = (None if eth_gas_usdt is None
                                else format(eth_gas_usdt, "f"))
    counters["eth_gas_price_wei"] = (None if eth_wei is None
                                     else format(eth_wei, "f"))
    counters["eth_gas_source"] = eth_wei_source
    counters["eth_gas_note"] = eth_gas_note

    # Ink pays ETH. One executeSwap is priced with the same unit count the
    # policy already uses for an EVM action; a failed read is unknown, not 0.
    ink_cfg = evm.CHAINS.get("ink")
    ink_wei: Decimal | None = None
    if ink_cfg is not None:
        try:
            raw_ink = evm.gas_price_wei(http, ink_cfg)
            ink_wei = None if raw_ink is None else Decimal(raw_ink)
        except Exception:  # noqa: BLE001
            ink_wei = None
    ink_wei_source = "eth_gasPrice"
    cached_ink = _d(cache.get("ink_wei")) if isinstance(cache, dict) else None
    ink_cache_age_s = ((now_ms - int(cache.get("ink_as_of_ms") or 0)) / 1000
                       if isinstance(cache, dict) and cache.get("ink_as_of_ms")
                       else None)
    if (ink_wei is None and cached_ink is not None
            and ink_cache_age_s is not None and ink_cache_age_s < 3600):
        ink_wei, ink_wei_source = cached_ink, "cached"
    ink_gas_usdt, ink_gas_note = costs.gas_cost_usd(
        policy, chain="ink", gas_price_wei=ink_wei,
        native_usd=_d(eth_row.get("usd")),
        native_as_of_ms=eth_row.get("as_of_ms"), now_ms=now_ms,
        max_native_age_ms=bridge_fees_core.MAX_PRICE_AGE_MS)
    counters["ink_gas_usdt"] = (None if ink_gas_usdt is None
                                else format(ink_gas_usdt, "f"))
    counters["ink_gas_price_wei"] = (None if ink_wei is None
                                     else format(ink_wei, "f"))
    counters["ink_gas_source"] = ink_wei_source
    counters["ink_gas_note"] = ink_gas_note

    # TON burn for a ston.fi buy plus the Kraken jetton transfer. The attached
    # 0.30 TON mostly comes back; ``legs_ton.BURN_TON`` is the part that does not,
    # until a receipt replaces the assumption. No TON price → the line is unknown.
    from arb_monitor.xstocks_v2 import legs_ton as legs_ton_core
    ton_price_row = (gas_price_report.get("prices") or {}).get("TON") or {}
    ton_usd = _d(ton_price_row.get("usd"))
    ton_as_of = ton_price_row.get("as_of_ms")
    ton_fresh = (ton_usd is not None and ton_usd > 0 and ton_as_of
                 and (now_ms - int(ton_as_of)) <= bridge_fees_core.MAX_PRICE_AGE_MS)
    if ton_fresh:
        ton_gas_usdt = legs_ton_core.BURN_TON * ton_usd
        ton_gas_note = "assumed_burn_0.08_TON"
    else:
        ton_gas_usdt, ton_gas_note = None, "ton_price_unknown"
    counters["ton_gas_usdt"] = (None if ton_gas_usdt is None
                                else format(ton_gas_usdt, "f"))
    counters["ton_gas_note"] = ton_gas_note

    ranked = _rank_solana(solana, kraken_rows, references, multipliers)
    budget = stage2_limit if stage2_limit is not None else policy.stage2_max

    if full_bilateral:
        wanted = ranked
    else:
        buffer_floor = policy.required_edge_bps(session) - policy.stage2_buffer_bps
        wanted = [entry for entry in ranked if _d(entry.get("edge_t0_bps")) is not None
                  and _d(entry["edge_t0_bps"]) >= buffer_floor][:budget]
        # The reverse family (EVM pool -> bridge -> Solana) is invisible to a
        # ranking by Kraken edge: an X Layer pool never beats the Kraken book on
        # t0, so those symbols would never get a Solana quote and no
        # chain_to_chain row could be built at all (measured 2026-10-07: SPYx had
        # ton and x-layer rows and no solana row). Add the symbols whose EVM pool
        # sits on a chain with a measured bridge to Solana. Bounded by the same
        # stage-2 budget, because every extra symbol is a real Jupiter quote on
        # the shared limiter.
        reverse_watch = reverse_watch_symbols(
            pools=pools, solana=solana, measured_bridges=measured_bridges,
            picked={entry.get("symbol") for entry in wanted}, limit=budget)
        counters["reverse_watch"] = reverse_watch
        wanted = wanted + [{"symbol": symbol} for symbol in reverse_watch]
    cached = load_withdraw_methods(Path(data_dir) / WITHDRAW_METHODS.name)
    # Pools measured dead by an earlier round. Without this every round re-quotes
    # the same 29 USD pool (measured 2026-10-07) and spends the 429 budget the
    # live names need.
    #
    # The verdict has to be able to be wrong, so three brakes are on it (all in
    # [watch], 2026-10-08): it takes `dead_pool_confirm_hits` rejections before a
    # pool is skipped at all, `dead_pool_ttl_s` expires the entry, and each round
    # re-quotes the `dead_pool_recheck_max` pools it has not looked at for the
    # longest -- an entry whose re-measurement comes back clean is dropped.
    dead_path = Path(data_dir) / DEAD_POOLS.name
    dead_entries = dead_pools_core.load(dead_path)
    # A verdict whose rule has been retired cannot be refreshed by any later
    # round, so it is dropped on sight (2026-10-08: the same-pool round trip).
    retired_entries = dead_pools_core.drop_retired(dead_entries)
    if retired_entries:
        counters["dead_pools_retired_verdict"] = len(retired_entries)
    dead_ttl_ms = max(1, int(policy.dead_pool_ttl_s)) * 1000
    dead_expired = dead_pools_core.sweep(dead_entries, now_ms=now_ms,
                                        ttl_ms=dead_ttl_ms)
    if dead_expired:
        counters["dead_pools_expired"] = len(dead_expired)
    recheck = set(dead_pools_core.recheck_keys(
        dead_entries, now_ms=now_ms, limit=policy.dead_pool_recheck_max,
        ttl_ms=dead_ttl_ms))
    counters["dead_pool_rechecks"] = len(recheck)
    # Prefer the cached (asset, method) table; fall back to the route-matrix
    # snapshot's address list when the cache has not been refreshed yet.
    withdraw_methods = cached.get("withdraw_methods") or {}
    # Union, never "one or the other": the cache carries the older stablecoin
    # entries and v2's own file carries the xStock entries the operator has
    # registered since (solanspyx1).
    address_book = merge_address_books(cached.get("address_book"),
                                       load_address_book())
    # Rotation, measured 2026-10-08 00:06: 300 Kraken withdrawal rails, 8 quoted
    # in a round. The 160 Solana rails that produced no row were not
    # unpriceable -- they lost the ranking to a larger edge of the moment. A
    # rail that never reaches the top ``stage2_max`` is never quoted, so the
    # board can never say whether registering its address would unblock a trade
    # (lessons C: "not sampled" is not "no opportunity"). A few slots therefore
    # go to the Solana rails that have waited the longest.
    rotation_path = Path(data_dir) / ROTATION.name
    rotation_entries = rotation_core.load(rotation_path)
    # ``withdraw_methods`` is the whole ``{asset: rows}`` table; the helper wants
    # the asset's own rows (same join ``rail_coverage`` does).
    rotation_candidates = [
        symbol for symbol in solana
        if has_withdraw_method(symbol, "solana", withdraw_methods.get(symbol))]
    rotation_entries = rotation_core.prune(rotation_entries,
                                           candidates=rotation_candidates)
    # Operator 2026-10-08, choosing between "keep the 6 h dead-pool exclusion"
    # and "quote everything, cover by rotation": **「全部报价、只按轮转补」**.
    # So a mint whose pool measured badly stays a normal rotation candidate --
    # the ledger keeps the measurement as evidence, but nothing is excluded from
    # being quoted. (The pool ledger and its recheck rotation now only decide
    # what the board *prints*, never what the scan is allowed to price.)
    rotation_dead: set[str] = set()
    rotation_slots = max(0, int(policy.rotation_slots))
    rotation_queue: list[dict] = []
    rotation_picks: list[dict] = []
    if not full_bilateral:
        # The queue is built even when nothing is reserved, so the board and the
        # ledger can still say how far behind the blind spot is.
        rotation_queue = rotation_core.waiting(
            rotation_entries, candidates=rotation_candidates,
            exclude={str(entry.get("symbol")) for entry in wanted} | rotation_dead,
            now_ms=now_ms)
        if rotation_slots:
            # The reserve comes out of the same budget, not on top of it (see
            # ``rotation.reserve_order``).
            wanted, rotation_picks = rotation_core.reserve_order(
                wanted, queue=rotation_queue, slots=rotation_slots, budget=budget)
    counters["rotation_slots"] = len(rotation_picks)
    counters["rotation_waiting"] = len(rotation_queue)
    chain_quotes: dict[str, list[dict]] = {}
    rows: list[dict] = []
    # Pairing is batched because the freshness gate compares two receive times.
    # Quoting twenty names on both sides takes about 45 s, so a single Kraken
    # snapshot would be stale against all but the last few rows (measured
    # 2026-10-07: 103 s round flagged every row stale_or_skew). Each batch is
    # quoted and then paired with its own refresh, which keeps both the age and
    # the skew inside the policy limits instead of relaxing them.
    pair_batch = max(1, int((policy.max_pair_skew_ms / 2000) * float(policy.jupiter_rps)))
    counters["pair_batch"] = pair_batch
    # Which rails this round actually spent a quote on -- the rotation ledger
    # follows the call, not the intention, so a dead-pool skip does not count as
    # "looked at".
    solana_quoted: list[str] = []
    # xChange is a second price on Solana / Ethereum / Ink. No key means the
    # scan still prices every other venue and says so in the counters.
    xchange = xchange_feed.XchangeFeed(http)
    xchange_catalog: dict = {}
    xchange_rails, xchange_hot = xchange_feed.load_state(
        Path(data_dir) / XCHANGE_ROTATION.name)
    xchange_quoted_symbols: set[str] = set()
    counters["xchange_unconfigured"] = 0 if xchange.configured else 1
    counters["xchange_quoted"] = 0
    counters["xchange_skipped"] = 0
    if xchange.configured:
        xchange_catalog, xchange_note = xchange.catalog()
        counters["xchange_catalog"] = xchange_note
        counters["xchange_networks_ignored"] = sorted(xchange.ignored_networks)
    else:
        counters["xchange_catalog"] = xchange_feed.UNCONFIGURED
    pool_names = pool_symbols | set(solana)
    counters["xchange_no_counterparty"] = len(xchange_feed.without_counterparty(
        catalog_symbols=xchange_catalog, kraken_online=listed,
        pool_symbols=pool_names))

    def attach_xchange(symbol: str, chain: str, cash: Decimal) -> None:
        asset = xchange_catalog.get(symbol)
        if asset is None:
            counters["xchange_skipped"] += 1
            return
        entry = xchange.pair_entry(
            asset, chain, cash, recv_ms=int(time.time() * 1000))
        if entry is None:
            counters["xchange_skipped"] += 1
            # Solana is the rotation chain. A name xChange does not offer there
            # will not become quotable by asking again later in the round.
            if chain == "solana":
                xchange_quoted_symbols.add(symbol)
            return
        chain_quotes.setdefault(symbol, []).append(entry)
        xchange_quoted_symbols.add(symbol)
        if entry.get("buy") or entry.get("sell"):
            counters["xchange_quoted"] += 1
            counters["real_quotes"] += 1
        else:
            counters["xchange_skipped"] += 1

    def close_batch(symbols: list[str]) -> None:
        for symbol in symbols:
            row = solana.get(symbol)
            if not row:
                continue
            # No dead-pool skip (operator 2026-10-08: 全部报价、只按轮转补). A
            # pool that measured badly is quoted and then judged on the price it
            # returns -- profit decides, not a verdict from an earlier round.
            result = solana_pair_quotes(
                feed, row, solana_quote_usdt(policy),
                multipliers.get(symbol, Decimal(1)), recv_ms=int(time.time() * 1000))
            chain_quotes.setdefault(symbol, []).append(result)
            counters["real_quotes"] += 1
            solana_quoted.append(symbol)
            if xchange.configured:
                attach_xchange(symbol, "solana", solana_quote_usdt(policy))
        if not symbols:
            return
        again = kraken.fetch_ticker(http, symbols, tokenized=True)
        counters["kraken_calls"] += 1
        fresh = again["ticker"]
        for symbol in symbols:
            if symbol not in chain_quotes:
                continue
            rows.extend(build_candidates(
                gas_usdt_xlayer=xlayer_gas_usdt,
                gas_note_xlayer=xlayer_gas_note,
                gas_usdt_eth=eth_gas_usdt,
                gas_note_eth=eth_gas_note,
                gas_usdt_ton=ton_gas_usdt,
                gas_note_ton=ton_gas_note,
                gas_usdt_ink=ink_gas_usdt,
                gas_note_ink=ink_gas_note,
                symbol=symbol, chain_quotes=chain_quotes[symbol],
                kraken_row=fresh.get(symbol) or {},
                policy=policy, multiplier=multipliers.get(symbol, Decimal(1)),
                deposits=deposits, v4_tiers=v4["tiers"],
                now_ms=int(time.time() * 1000), phase=policy.phase,
                measured_bridges=measured_bridges,
                measured_symbols=measured_symbols, bridge_fees=bridge_fees,
                address_book=address_book,
                withdraw_methods=withdraw_methods, session=session,
                missing_rails=missing_rails))

    pending: list[str] = []
    for entry in wanted:
        pending.append(entry["symbol"])
        if len(pending) >= pair_batch:
            close_batch(pending)
            pending = []
    close_batch(pending)

    # The ledger follows the calls (``solana_quoted``), not the intent, and it is
    # written in a full scan too: that round really did look at every rail.
    rotation_core.mark(rotation_entries, solana_quoted, now_ms=now_ms)
    counters["rotation_looked_at"] = len(set(solana_quoted))
    if solana_quoted or rotation_entries:
        rotation_core.save(rotation_path, rotation_entries, now_ms=now_ms)
    rotation_waiting = rotation_queue[len(rotation_picks):]

    # EVM pools are few and batch into a handful of round trips, so they are
    # always quoted for real and then paired in one more batch.
    evm_symbols: list[str] = []
    # Per-chain quote counters. Measured 2026-10-09: BSC pools were in the
    # census and quotable, yet the snapshot contained no BSC row and no trace of
    # why -- a chain whose quotes all fail looks exactly like a chain with no
    # pools. The counters make the difference visible in the snapshot.
    evm_quotes = counters.setdefault("evm_quotes", {})
    evm_errors = counters.setdefault("evm_quote_errors", {})
    for chain in sorted({pool["chain"] for pool in pools}):
        config = evm.CHAINS.get(chain)
        if config is None:
            continue
        group = [pool for pool in pools if pool["chain"] == chain]
        if not group:
            continue
        for entry in evm_pair_quotes(
                http, config, group,
                lambda pool: pool_ticket_usdt(pool, policy=policy),
                recv_ms=int(time.time() * 1000), multipliers=multipliers):
            entry["errors"] = entry.get("errors") or []
            evm_quotes[chain] = int(evm_quotes.get(chain) or 0) + 1
            if entry["errors"]:
                evm_errors[chain] = int(evm_errors.get(chain) or 0) + 1
            chain_quotes.setdefault(entry["symbol"], []).append(entry)
            counters["real_quotes"] += 1
            evm_symbols.append(entry["symbol"])
    if xchange.configured:
        seen_xchange: set[tuple[str, str]] = set()
        for chain in ("eth", "ink", "bsc"):
            symbols = [
                symbol for symbol, entries in chain_quotes.items()
                if any(item.get("chain") == chain and not _is_xchange(item)
                       for item in entries)]
            for symbol in symbols:
                key = (symbol, chain)
                if key in seen_xchange:
                    continue
                seen_xchange.add(key)
                attach_xchange(symbol, chain, solana_quote_usdt(policy))
    if evm_symbols:
        again = kraken.fetch_ticker(http, sorted(set(evm_symbols)), tokenized=True)
        counters["kraken_calls"] += 1
        fresh = {**kraken_rows, **again["ticker"]}
        for symbol in sorted(set(evm_symbols)):
            rows.extend(build_candidates(
                gas_usdt_xlayer=xlayer_gas_usdt,
                gas_note_xlayer=xlayer_gas_note,
                gas_usdt_eth=eth_gas_usdt,
                gas_note_eth=eth_gas_note,
                gas_usdt_ton=ton_gas_usdt,
                gas_note_ton=ton_gas_note,
                gas_usdt_ink=ink_gas_usdt,
                gas_note_ink=ink_gas_note,
                symbol=symbol, chain_quotes=chain_quotes[symbol],
                kraken_row=fresh.get(symbol) or {},
                policy=policy, multiplier=multipliers.get(symbol, Decimal(1)),
                deposits=deposits, v4_tiers=v4["tiers"],
                now_ms=int(time.time() * 1000), phase=policy.phase,
                measured_bridges=measured_bridges,
                measured_symbols=measured_symbols, bridge_fees=bridge_fees,
                address_book=address_book,
                withdraw_methods=withdraw_methods, session=session,
                missing_rails=missing_rails))

    # TON: one call per direction per pool, so it is quoted for real as well.
    ton_symbols: list[str] = []
    if ton_pools:
        for entry in ton_pair_quotes(
                ton, ton_pools,
                lambda pool: pool_ticket_usdt(pool, policy=policy),
                multipliers, recv_ms=int(time.time() * 1000)):
            entry["errors"] = entry.get("errors") or []
            chain_quotes.setdefault(entry["symbol"], []).append(entry)
            counters["real_quotes"] += 1
            ton_symbols.append(entry["symbol"])
        counters["ton_calls"] = ton.calls
        if ton.failures:
            counters["ton_failures"] = dict(sorted(ton.failures.items(),
                                                   key=lambda kv: -kv[1])[:5])
        again = kraken.fetch_ticker(http, sorted(set(ton_symbols)), tokenized=True)
        counters["kraken_calls"] += 1
        fresh = {**kraken_rows, **again["ticker"]}
        for symbol in sorted(set(ton_symbols)):
            rows.extend(build_candidates(
                gas_usdt_xlayer=xlayer_gas_usdt,
                gas_note_xlayer=xlayer_gas_note,
                gas_usdt_eth=eth_gas_usdt,
                gas_note_eth=eth_gas_note,
                gas_usdt_ton=ton_gas_usdt,
                gas_note_ton=ton_gas_note,
                gas_usdt_ink=ink_gas_usdt,
                gas_note_ink=ink_gas_note,
                symbol=symbol, chain_quotes=chain_quotes[symbol],
                kraken_row=fresh.get(symbol) or {},
                policy=policy, multiplier=multipliers.get(symbol, Decimal(1)),
                deposits=deposits, v4_tiers=v4["tiers"],
                now_ms=int(time.time() * 1000), phase=policy.phase,
                measured_bridges=measured_bridges,
                measured_symbols=measured_symbols, bridge_fees=bridge_fees,
                address_book=address_book,
                withdraw_methods=withdraw_methods, session=session,
                missing_rails=missing_rails))

    # Names Kraken lists and xChange lists, which this round's pool batches did
    # not already price on Solana. Eight rotate; names that cleared the buffer
    # last round are asked again (at most 12). Solana first: gas is ~0 and the
    # chain -> Kraken executor already exists.
    xchange_rotation_picks: list[str] = []
    if xchange.configured and xchange_catalog:
        hot_pick, xchange_rotation_picks, xchange_queue = xchange_feed.plan_extra_symbols(
            online=list(xchange_catalog), catalog_symbols=xchange_catalog,
            already=xchange_quoted_symbols, hot=xchange_hot, rails=xchange_rails,
            now_ms=now_ms)
        extra = hot_pick + xchange_rotation_picks
        counters["xchange_hot"] = hot_pick
        counters["xchange_rotation"] = xchange_rotation_picks
        counters["xchange_rotation_waiting"] = max(
            0, len(xchange_queue) - len(xchange_rotation_picks))
        chunk = 4
        for start in range(0, len(extra), chunk):
            group = extra[start:start + chunk]
            for symbol in group:
                for chain in ("solana", *xchange_feed.RFQ_INSTEAD_OF_FACTORY):
                    attach_xchange(symbol, chain, solana_quote_usdt(policy))
            again = kraken.fetch_ticker(http, group, tokenized=True)
            counters["kraken_calls"] += 1
            fresh = again["ticker"]
            for symbol in group:
                if symbol not in chain_quotes:
                    continue
                rows.extend(build_candidates(
                    gas_usdt_xlayer=xlayer_gas_usdt,
                    gas_note_xlayer=xlayer_gas_note,
                    gas_usdt_eth=eth_gas_usdt,
                    gas_note_eth=eth_gas_note,
                    gas_usdt_ton=ton_gas_usdt,
                    gas_note_ton=ton_gas_note,
                    gas_usdt_ink=ink_gas_usdt,
                    gas_note_ink=ink_gas_note,
                    symbol=symbol, chain_quotes=chain_quotes[symbol],
                    kraken_row=fresh.get(symbol) or {},
                    policy=policy, multiplier=multipliers.get(symbol, Decimal(1)),
                    deposits=deposits, v4_tiers=v4["tiers"],
                    now_ms=int(time.time() * 1000), phase=policy.phase,
                    measured_bridges=measured_bridges,
                    measured_symbols=measured_symbols, bridge_fees=bridge_fees,
                    address_book=address_book,
                    withdraw_methods=withdraw_methods, session=session,
                    missing_rails=missing_rails))
        xchange_rails = rotation_core.prune(
            xchange_rails, candidates=set(xchange_catalog) & set(listed))
        rotation_core.mark(xchange_rails, xchange_rotation_picks, now_ms=now_ms)
    counters["xchange_calls"] = xchange.calls
    counters["xchange_http_429"] = xchange.http_429
    counters["xchange_session_closed"] = xchange.session_closed

    counters["jupiter_calls"] = feed.calls
    counters["http_429"] = feed.http_429
    counters["symbols"] = len(kraken_rows)
    counters["kraken_edge_missing"] = sorted(set(missing_rails))
    rows = dedupe_scan_rows(rows)
    rows.sort(key=lambda row: _d(row.get("edge_t0_bps")) or Decimal(-10 ** 6), reverse=True)
    if xchange.configured and xchange_catalog:
        floor = policy.required_edge_bps(session) - policy.stage2_buffer_bps
        next_hot = xchange_feed.hot_from_rows(rows, floor_bps=floor)
        counters["xchange_hot_next"] = next_hot
        xchange_feed.save_state(
            Path(data_dir) / XCHANGE_ROTATION.name, xchange_rails, next_hot,
            now_ms=now_ms)

    # The address book has no write API (probed 2026-10-07: five REST spellings
    # 404, four WebSocket names 'Unsupported event'), so the operator registers
    # entries by hand. This is the shopping list: rows whose only remaining
    # blocker is that one missing entry.
    informational = {"phase_scan", "chain_out_of_phase"}
    # The registration list answers "which (asset, network) should I add in the
    # Kraken UI". That question does not depend on quote freshness, and it does
    # not depend on whether the row would pay either -- a row that needs an
    # address *and* would not earn anything is still worth showing, with its net
    # beside it. Dropping those two made the list empty whenever a round went
    # stale (measured 2026-10-07: four rows, then none).
    request_informational = informational | {"stale_or_skew", "below_net"}
    # Sorted by edge so the operator registers the useful ones first, and only
    # rows whose *only* remaining blocker is the missing entry.
    request_rows = [
        row for row in rows
        if "no_address_book" in (row.get("blockers") or [])
        and not (set(row["blockers"]) - request_informational - {"no_address_book"})
    ]
    request_rows.sort(key=lambda row: _d(row.get("edge_t0_bps")) or Decimal("-1e9"),
                      reverse=True)
    # Learn from this round: a pool rejected for depth is one the next round does
    # not need to quote. ``below_depth`` is exactly the "the book cannot take our
    # size / the same pool gives back less than the allowance" verdict -- not
    # staleness, not a missing rail. A rechecked pool that measures clean is
    # dropped again, so a wrong verdict heals instead of lasting a whole TTL.
    verdicts = update_dead_pools(dead_entries, rows, now_ms=now_ms, recheck=recheck)
    counters["dead_pool_marked"] = verdicts["marked"]
    if verdicts["revived"]:
        counters["dead_pool_revived"] = (
            counters.get("dead_pool_revived", 0) + verdicts["revived"])
    if dead_entries:
        dead_pools_core.save(dead_path, dead_entries, now_ms=now_ms)
    # ... and the rows the board will call "可信" get their Kraken leg priced on
    # the real book for their own ticket, not on the best bid of a 2 USD order
    # (operator 2026-10-08: orderbook 只定价，利润说话). This runs after the pool
    # ledger so a depth-short verdict is not folded back into it.
    counters["kraken_depth_priced"] = reprice_kraken_legs(
        rows, http=http, policy=policy, session=session, counters=counters)
    # Second list: rows that also want an address but are *not* one entry away
    # from tradable -- today that means a thin pool or a negative spread.
    # Deliberately kept apart from the shopping list: registering costs the
    # operator a web form and an e-mail confirmation, and a row whose only other
    # blocker is `below_depth` may never become tradable. They are listed so no
    # (asset, network) can hide, and they stay out of the main list so the
    # "register these" instruction keeps meaning "this unblocks a trade".
    # Measured 2026-10-07: SOXLx/Solana has a rail (min 0.00270869) and no entry
    # in the book, but its row is a 414,906 bps thin-pool fake -- invisible.
    request_row_keys = {(str(row.get("symbol")), str(row.get("chain")))
                        for row in request_rows}
    deferred_rows = [
        row for row in rows
        if "no_address_book" in (row.get("blockers") or [])
        and (str(row.get("symbol")), str(row.get("chain"))) not in request_row_keys
    ]
    deferred_rows.sort(key=lambda row: _d(row.get("edge_t0_bps")) or Decimal("-1e9"),
                      reverse=True)
    # One line per (asset, hub): the same request often has two rows (the row
    # that is stale and the one that is not), and the operator registers one
    # address, not two.
    seen_deferred: set[tuple[str, str]] = set()
    unique_deferred: list[dict] = []
    for row in deferred_rows:
        key = (str(row.get("symbol")), _hub_chain(str(row.get("chain"))))
        if key in seen_deferred:
            continue
        seen_deferred.add(key)
        unique_deferred.append(row)

    elapsed = time.monotonic() - started
    def _request_entry(row: dict) -> dict:
        hub = _hub_chain(str(row["chain"]))
        return {
            "symbol": str(row["symbol"]), "chain": str(row["chain"]),
            # The withdrawal happens on the HUB chain. For a two-hop row the
            # chain reads ``solana->x-layer``, and using the whole string made
            # the suggested key ``solana>xlayerspyx1`` (found 2026-10-07).
            "hub": hub,
            # Matches the naming the book already uses (xlayertee1, soltee1):
            # chain + asset, lower-case, no separators.
            "kraken_key_hint": f"{hub.replace('-', '')}"
                               f"{str(row['symbol']).lower()}1",
            "network_label": {"eth": "Ethereum", "solana": "Solana",
                              "ton": "The Open Network"}.get(hub, hub),
            # The address must be on the HUB chain: Kraken sends the token
            # there, not to the pool chain. A hub we hold no address on is
            # reported as missing -- never as another chain's address.
            "address": hub_withdraw_address(hub)[0],
            "address_ready": hub in HUB_ADDRESSES,
            "address_note": hub_withdraw_address(hub)[1],
            "edge_t0_bps": row.get("edge_t0_bps"),
            "net_bps": _request_net(row)[1],
            # The gate is a NET bar, so the column has to show the net. The
            # old version printed the gross spread next to a net test, which
            # read as "61.76 bps yet it fails 20 bps" (found 2026-10-07).
            "net_usdt": _request_net(row)[0],
            "clears_threshold": _request_clears(row, policy, session),
            "why": _request_why(row),
            "reason": row.get("reason"),
        }

    payload = {
        "generated_ms": now_ms,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "phase": policy.phase,
        "session": session,
        "threshold_bps": format(policy.required_edge_bps(session), "f"),
        "threshold_open_bps": format(policy.min_net_edge_bps_open, "f"),
        "threshold_closed_bps": format(policy.min_net_edge_bps_closed, "f"),
        "min_net_profit_usdt": format(policy.min_net_profit_usdt, "f"),
        "per_trade_quote_usdt": format(policy.per_trade_quote_usdt, "f"),
        "min_trade_quote_usdt": format(policy.min_trade_quote_usdt, "f"),
        "round_seconds": round(elapsed, 2),
        "counters": counters,
        "jupiter_failures": dict(sorted(feed.failures.items(), key=lambda kv: -kv[1])[:8]),
        "kraken_errors": kraken_errors,
        "universe": {
            "kraken_listed": len(listed),
            "kraken_online": sum(1 for symbol in listed
                                 if units[symbol].get("status") == "online"),
            "kraken_priced": len(kraken_rows),
            "solana_mints": len(solana),
            "evm_pools": len(pools),
            "v4_pools": sum(1 for pool in pools if pool.get("kind") == "v4"),
            "ton_pools": len(ton_pools),
            "stage2_wanted": len(wanted),
            # The Solana quote budget itself, so the board can say how many
            # slots a rail is competing for.
            "stage2_max": budget,
        },
        "venue_pairs": _venue_pairs(rows),
        "address_requests": [_request_entry(row) for row in request_rows],
        # Same shape, kept apart: they need an entry too, but an economic
        # blocker (thin pool, negative spread) stands in front of them.
        "address_requests_deferred": [
            _request_entry(row) for row in unique_deferred[:20]],
        # And the third list: rails nobody quoted this round, so nobody could
        # even ask whether they are worth registering.
        "rail_coverage": rail_coverage(
            rows, symbols=set(listed) | set(missing_rails),
            withdraw_methods=withdraw_methods, address_book=address_book),
        # And how that blind spot is worked down round by round: which rails
        # took a rotation slot, and how long the rest have been waiting.
        "rotation": {
            "slots": rotation_slots,
            "full_scan": bool(full_bilateral),
            "candidates": len(rotation_candidates),
            "picked": rotation_picks,
            "looked_at_this_round": len(set(solana_quoted)),
            "waiting_count": len(rotation_waiting),
            "never_quoted_count": sum(1 for item in rotation_waiting
                                      if item.get("never_quoted")),
            "oldest_waited_s": (rotation_waiting[0].get("waited_s")
                                if rotation_waiting else None),
            "waiting": rotation_waiting[:20],
        },
        # Pools on the list are not quoted (they carry their own rows, marked
        # ``dead_pool_skipped``). The count is here so a round that skipped a lot
        # can be read as "we saved calls", not as "the venue went quiet".
        "dead_pools": {
            "count": len(dead_entries),
            # Since 2026-10-08 nothing is skipped (operator: 全部报价、只按轮转补);
            # the list is evidence for the board. The old key stays so an older
            # reader sees zero rather than a missing number.
            "marked_this_round": counters.get("dead_pool_marked", 0),
            "skipped_this_round": 0,
            "expired_this_round": counters.get("dead_pools_expired", 0),
            "rechecked_this_round": counters.get("dead_pool_rechecks", 0),
            "revived_this_round": counters.get("dead_pool_revived", 0),
            "ttl_hours": round(dead_ttl_ms / 3600000, 2),
            "confirm_hits": policy.dead_pool_confirm_hits,
            "entries": {k: v for k, v in sorted(dead_entries.items())[:40]},
        },
        "gas_prices": gas_price_report,
        # A1/A3: the round refreshes the gas prices itself and reads each
        # bridge fee off the chain. Both are printed so a reader can tell "the
        # chain said so just now" from "the receipt from yesterday".
        "bridge_fees": {
            "live": counters.get("bridge_fee_live") or {},
            "live_vs_evidence": counters.get("bridge_fee_live_vs_evidence") or {},
            "price_refresh": counters.get("gas_price_refresh") or {},
            "unknown": bridge_fee_unknown,
        },
        # What the round used to price one X Layer action, and how it got it.
        "gas_lines": {
            "xlayer_usdt": counters.get("xlayer_gas_usdt"),
            "xlayer_gas_price_wei": counters.get("xlayer_gas_price_wei"),
            "source": counters.get("xlayer_gas_source"),
            "note": counters.get("xlayer_gas_note"),
            # The same line for Ethereum mainnet (2026-10-09): the eth chain ->
            # Kraken rows pay this one.
            "eth_usdt": counters.get("eth_gas_usdt"),
            "eth_gas_price_wei": counters.get("eth_gas_price_wei"),
            "eth_source": counters.get("eth_gas_source"),
            "eth_note": counters.get("eth_gas_note"),
            # For the next round: reuse this reading (up to an hour) if its own
            # eth_gasPrice call fails, instead of blanking every bridge row.
            "cache": {"wei": None if gas_wei is None else format(gas_wei, "f"),
                      "as_of_ms": (now_ms if gas_wei_source == "eth_gasPrice"
                                   else cache.get("as_of_ms")),
                      "eth_wei": None if eth_wei is None else format(eth_wei, "f"),
                      "eth_as_of_ms": (now_ms if eth_wei_source == "eth_gasPrice"
                                       else cache.get("eth_as_of_ms")),
                      "ink_wei": None if ink_wei is None else format(ink_wei, "f"),
                      "ink_as_of_ms": (now_ms if ink_wei_source == "eth_gasPrice"
                                       else cache.get("ink_as_of_ms"))},
            "ink_usdt": counters.get("ink_gas_usdt"),
            "ink_gas_price_wei": counters.get("ink_gas_price_wei"),
            "ink_source": counters.get("ink_gas_source"),
            "ink_note": counters.get("ink_gas_note"),
        },
        "rows": rows[:400],
        "preview": rows[:25],
        "note": ("只读扫描。价差一律读作「卖出得到的价格比买入付出的价格高多少 bps」。"
                 "参考价只用于排序，不进净利。executable 表示这条路径在当前 phase 下"
                 "没有被闸门挡住；它不代表会下单（下单还要 operator 的三把钥匙）。"),
    }
    write_json(Path(data_dir) / SCAN_PATH.name, payload)
    return payload


def requote_buy(*, symbol: str, chain: str, policy: V2Policy, http: Http,
                feed: JupiterFeed | None = None, book: Path | str = census_feed.DEFAULT_BOOK,
                census_path: Path | str = census_feed.DEFAULT_CENSUS,
                v4_path: Path | str = census_feed.DEFAULT_V4,
                units_path: Path | str = census_feed.DEFAULT_UNITS,
                multiplier: Decimal | None = None,
                notional: Decimal | None = None,
                tries: int = REQUOTE_TRIES) -> dict | None:
    """Re-price the buy leg now.

    A gate may only act on ``edge_after_requote_bps``. Re-checking one side
    against a scan-time price on the other is not a re-quote: it produces a
    ``stale_or_skew`` verdict for every row, which is true but useless. Both
    sides are refreshed here.

    ``notional`` overrides the size: the ladder (operator 2026-10-08) starts at
    the smallest ticket and steps up while the trade still clears, so the caller
    asks the pool about the size it would actually trade.
    """
    feed = feed or JupiterFeed(http)
    multipliers = load_multipliers()
    if multiplier is None:
        multiplier = multipliers.get(symbol, Decimal(1))
    if chain == "solana":
        universe = census_feed.solana_universe([symbol], book=book)
        row = universe.get(symbol)
        if not row:
            return None
        size = solana_quote_usdt(policy) if notional is None else notional
        return solana_pair_quotes(feed, row, size, multiplier,
                                  recv_ms=int(time.time() * 1000), tries=tries)
    pools = census_feed.census_pools(census_feed.load_census(census_path))
    pools += census_feed.v4_pool_rows(census_feed.load_v4(v4_path), book=book)
    config = evm.CHAINS.get(chain)
    group = [pool for pool in pools if pool["chain"] == chain and pool["symbol"] == symbol]
    if not config or not group:
        return None
    rows = evm_pair_quotes(http, config, group,
                           (lambda pool: pool_ticket_usdt(pool, policy=policy))
                           if notional is None
                           else (lambda pool: min(notional,
                                                  pool_ticket_usdt(pool, policy=policy))),
                           recv_ms=int(time.time() * 1000),
                           multipliers=multipliers)
    if not rows:
        return None
    # Keep the deepest pool: the same choice the scan ranks by.
    return max(rows, key=lambda item: _d(item.get("depth_usd")) or Decimal(0))


def reverse_watch_symbols(*, pools, solana, measured_bridges, picked, limit):
    """Symbols whose Solana side must be quoted for the reverse family to exist.

    The reverse family is "buy on an EVM pool chain -> bridge -> sell on
    Solana". A ranking by Kraken edge never selects those symbols, so their
    Solana side was never quoted and the row could not be built (measured
    2026-10-07: SPYx had ton and x-layer rows and no solana row at all).

    Only chains with a *measured* bridge to Solana qualify: quoting the Solana
    side of a pair nobody can cross is bandwidth spent on nothing.
    """
    candidates = sorted({
        str(pool.get("symbol")) for pool in pools
        if (route_core.norm(pool.get("chain")), "solana") in measured_bridges})
    return [symbol for symbol in candidates
            if symbol in solana and symbol not in picked][:limit]


def _rank_solana(solana: dict, kraken_rows: dict, references: dict,
                 multipliers: dict) -> list[dict]:
    """Cheap ordering for the Solana stage-2 budget."""
    out: list[dict] = []
    for symbol, row in solana.items():
        kraken_row = kraken_rows.get(symbol)
        if not kraken_row:
            continue
        multiplier = multipliers.get(symbol, Decimal(1))
        reference = references.get(row["token_address"])
        if reference is None:
            out.append({"symbol": symbol, "edge_t0_bps": None,
                        "buy_price": None, "sell_price": kraken_row.get("bid")})
            continue
        # Price V3 is per token; store it per credit unit so it compares with
        # the Kraken book, which is quoted per credit unit.
        per_credit = reference / multiplier if multiplier else reference
        bid = kraken_row.get("bid")
        out.append({
            "symbol": symbol,
            "buy_price": per_credit, "sell_price": bid,
            "edge_t0_bps": edge_bps(per_credit, bid),
        })
    out.sort(key=lambda row: _d(row.get("edge_t0_bps")) or Decimal(-10 ** 6), reverse=True)
    return out


def _venue_pairs(rows: list[dict]) -> list[dict]:
    tally: dict[tuple, dict] = {}
    for row in rows:
        key = (row["family"], row["buy_venue"], row["sell_venue"])
        item = tally.setdefault(key, {"family": key[0], "buy_venue": key[1],
                                      "sell_venue": key[2], "rows": 0,
                                      "best_edge_bps": None, "reasons": {}})
        item["rows"] += 1
        edge = _d(row.get("edge_t0_bps"))
        if edge is not None and (item["best_edge_bps"] is None
                                 or edge > item["best_edge_bps"]):
            item["best_edge_bps"] = edge
        reason = row.get("reason") or "ok"
        item["reasons"][reason] = item["reasons"].get(reason, 0) + 1
    out = []
    for item in tally.values():
        item["best_edge_bps"] = (None if item["best_edge_bps"] is None
                                 else format(item["best_edge_bps"], "f"))
        out.append(item)
    out.sort(key=lambda item: _d(item["best_edge_bps"]) or Decimal(-10 ** 6), reverse=True)
    return out


def _session_label(now_ms: int) -> str:
    from datetime import datetime

    from arb_monitor.xstocks_v2.calendar import session_label

    return session_label(datetime.fromtimestamp(now_ms / 1000).astimezone())


def run_full_bilateral(*, policy: V2Policy | None = None, http: Http | None = None,
                       data_dir: Path | str = DEFAULT_DATA,
                       book: Path | str = census_feed.DEFAULT_BOOK,
                       census_path: Path | str = census_feed.DEFAULT_CENSUS,
                       v4_path: Path | str = census_feed.DEFAULT_V4,
                       units_path: Path | str = census_feed.DEFAULT_UNITS,
                       deposits_path: Path | str = EXIT_CACHE) -> dict:
    """One timed pass that really quotes every Solana mint on both sides.

    This exists to replace an estimate with a measurement: the free Jupiter tier
    paces at about 0.9 requests/s, so 177 mints on two sides is roughly six
    minutes. The resident loop ranks first and only deep-quotes the close ones.
    """
    payload = run_scan(policy=policy, http=http, data_dir=data_dir, book=book,
                       census_path=census_path, v4_path=v4_path,
                       units_path=units_path, deposits_path=deposits_path,
                       full_bilateral=True)
    timing = {
        "measured_ms": int(time.time() * 1000),
        "round_seconds": payload["round_seconds"],
        "counters": payload["counters"],
        "universe": payload["universe"],
        "jupiter_failures": payload["jupiter_failures"],
        "rows": len(payload["rows"]),
    }
    write_json(Path(data_dir) / TIMING_PATH.name, timing)
    return timing
