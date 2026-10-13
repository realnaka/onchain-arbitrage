"""Composition root for the existing data → strategy → gate → SQLite pipeline."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import fields
from datetime import date, datetime
from decimal import Decimal
import hashlib
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import random
import signal
import threading
import time
import tomllib
import uuid

from arb_monitor.core.hk import (
    DEFAULT_MAX_PEG_DEVIATION_BPS, DEFAULT_MAX_QUOTE_CONVERSION_AGE_MS, ZERO,
    Contract, PairMapping, QuoteConversion, RoundTripCosts, SpotQuote, decimal,
    dumps,
)
from arb_monitor.core.gas_watch import observe_gas
from arb_monitor.core.hk_calendar import HK, HKCalendar
from arb_monitor.core.cost_table import cost_table_from_tokens
from arb_monitor.core.liquidity_state import PoolObservation, classify_pool
from arb_monitor.core.markets import (
    HK as HK_MARKET, UNKNOWN as UNKNOWN_MARKET, describe_scope, normalize_market,
    validate_scan_countries,
)
from arb_monitor.core.math import quantize_down
from arb_monitor.core.sizing import largest_hedge_tokens, minimum_hedge_tokens
from arb_monitor.apps.account_dashboard import trade_universe
from arb_monitor.data.hk_cex import CexPublicAdapter, SOURCES, now_ms
from arb_monitor.data.lighter_stream import (
    LIGHTER_STREAM_URL, LighterOrderBookStream, lighter_market_ids,
)
from arb_monitor.data.hk_reference import PublicHTTP, ReferenceAdapter
from arb_monitor.data.onchainos import OnchainOSQuoteAdapter, QuoteChannelHealth
from arb_monitor.data.xlayer_onchain import (
    QUOTER_V2, XLayerOnchainAdapter, XLayerRPC, best_pool_reference, quote_single,
)
from arb_monitor.data.xstocks_registry import (
    discovered_token_entry, discover_pools, fetch_issuer_assets, match_cex_asset,
    fetch_okx_stock_perps, hk_shaped_code, listing_market, market_label, new_tokens,
    okx_hedge_ticker, probe_pools_cheap, render_discovered_toml,
)
from arb_monitor.ledger.store import EventStore
from arb_monitor.ops.dedup import SignalDeduplicator
from arb_monitor.ops.notify import AlertGate, FeishuNotifier, format_alert
from arb_monitor.risk.hk_basis import BasisLimits
from arb_monitor.strategy.hk_basis import HKBasisStrategy
from arb_monitor.strategy.token_perp_basis import TokenPerpBasisStrategy
from arb_monitor.strategy.hk_probe import AnchorReference, assess_probe
from arb_monitor.strategy.xstocks_screen import (
    breakeven_gross_bps, perp_side, pool_side_prices, roundtrip_cost_bps,
    screen_direction, size_plan,
)

# How long an unchanged `instrument_discovery` payload is still written as a
# heartbeat (see `_record_discovery`). A day is enough to prove the scan ran;
# anything shorter just re-stores the same megabyte.
DISCOVERY_HEARTBEAT_MS = 24 * 3600 * 1000


def _merge_signed_depth_mappings(config: dict, path: Path) -> None:
    """Operator-signed wrapper mappings that are not in the hand-written file.

    The file is produced by a mapping-verify pass. Loading it does not sign
    anything: ``mapping_verified`` is already in the file, and a name that
    failed a layer is simply not in it. Discovery still cannot write this file.
    """
    if not path.is_file():
        return
    with open(path, "rb") as handle:
        extra = tomllib.load(handle, parse_float=decimal).get("tokens") or []
    have = {t["asset"] for t in config["tokens"]}
    monitor = config["monitor"]
    for token in extra:
        if token.get("asset") in have:
            continue
        config["tokens"].append(token)
        have.add(token["asset"])
        if token["asset"] not in monitor["assets"]:
            monitor["assets"].append(token["asset"])


# A slot0 basis is not an execution quote. It only decides which allowlisted
# names spend this cycle's exact-in budget. 15 bps is below the ~26 bps
# round-trip bar, so a name that might clear after a real quote is not skipped,
# and a name sitting at a discount is not quoted.
HOT_DISPLAY_BASIS_BPS = Decimal("15")
HOT_EXACT_QUOTE_CAP = 2


def stablecoin_mid(bid: Decimal, ask: Decimal) -> tuple[Decimal, Decimal] | None:
    """Mid and absolute peg deviation in bps. A crossed or empty book is no rate."""
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / Decimal(2)
    if mid <= 0:
        return None
    return mid, abs(mid - Decimal(1)) * Decimal(10000)


def promoted_exact_assets(screen: dict, tokens: list[dict], allowlist: dict,
                          standing: set[str], *,
                          floor_bps: Decimal = HOT_DISPLAY_BASIS_BPS,
                          cap: int = HOT_EXACT_QUOTE_CAP) -> list[str]:
    """Allowlisted names whose display basis is high enough to spend a quote.

    Standing exact-in names are not repeated here. The cap is what keeps a
    40-name allowlist from turning into 40 OnchainOS probes.
    """
    instruments = {str(v) for v in allowlist.get("instruments") or []}
    assets = {str(v) for v in allowlist.get("assets") or []}
    verified = {str(t.get("asset")) for t in tokens if t.get("mapping_verified")}
    ranked: list[tuple[Decimal, str]] = []
    for row in (screen or {}).get("rows") or []:
        asset = str(row.get("asset") or "")
        if (not asset or asset in standing or asset not in assets
                or asset not in verified
                or str(row.get("instrument") or "") not in instruments):
            continue
        raw = row.get("display_basis_bps")
        if raw is None:
            continue
        try:
            basis = Decimal(str(raw))
        except Exception:
            continue
        if basis < floor_bps:
            continue
        ranked.append((basis, asset))
    ranked.sort(reverse=True)
    return [asset for _, asset in ranked[:max(0, cap)]]


def load_config(path: str) -> dict:
    with open(path, "rb") as handle:
        config = tomllib.load(handle, parse_float=decimal)
    monitor = config["monitor"]
    # Validate eagerly, before any network IO.
    limits_from_config(config)
    # Legacy grid only. The live probe size is each contract's minSz/lotSz,
    # converted to tokens. Fractional counts are legal there; this list is not
    # that floor. It stays whole so the old batch helper cannot ask for a size
    # the tests did not pin.
    shares = [decimal(x) for x in monitor["probe_shares"]]
    if not shares or len(shares) > 5 or any(x <= 0 or x > 5000 for x in shares):
        raise ValueError("use 1–5 probe share counts, each in (0, 5000]")
    if any(x != x.to_integral_value() for x in shares):
        raise ValueError("probe share counts must be whole shares")
    depth = [decimal(x) for x in monitor.get("depth_sizes", ["100"])]
    if not depth or len(depth) > 6 or any(x <= 0 or x > 5000 for x in depth):
        raise ValueError("use 1–6 depth sizes, each in (0, 5000]")
    if not 10 <= int(monitor["interval_seconds"]) <= 3600:
        raise ValueError("interval_seconds must be between 10 and 3600")
    if not 0 < int(monitor["quote_ttl_ms"]) <= int(monitor["max_age_ms"]):
        raise ValueError("quote TTL must be positive and no longer than max age")
    for name, low, high in (("market_batch_budget_seconds", 1, 300),
                            ("metadata_budget_seconds", 1, 300),
                            ("reference_budget_seconds", 1, 300),
                            ("quote_asset_deadline_seconds", 3, 60),
                            ("quote_workers_min", 1, 32),
                            ("pool_state_max_age_ms", 1000, 3_600_000)):
        if not low <= float(monitor.get(name, low)) <= high:
            raise ValueError(f"{name} out of range")
    if int(monitor.get("quote_workers_min", 1)) > int(monitor["quote_workers"]):
        raise ValueError("quote_workers_min cannot exceed quote_workers")
    if float(monitor.get("quote_asset_deadline_seconds", 12)) >= float(
            monitor["market_batch_budget_seconds"]):
        # One asset's budget has to fit inside the round's, or the per-asset
        # deadline could never fire before the batch one.
        raise ValueError("quote_asset_deadline_seconds must be inside "
                         "market_batch_budget_seconds")
    if (not monitor["venues"] or len(set(monitor["venues"])) != len(monitor["venues"])
            or not set(monitor["venues"]) <= set(SOURCES)):
        raise ValueError("venues must be unique gate/okx/binance")
    # A rotating slice with no width would silently stop quoting the market it
    # names — the failure mode would look exactly like "that market has nothing",
    # so it is refused at load time instead of discovered from a quiet board.
    if monitor.get("quote_slice_markets") and int(
            monitor.get("quote_slice_assets_per_cycle", 0) or 0) < 1:
        raise ValueError("quote_slice_markets needs quote_slice_assets_per_cycle "
                         ">= 1")
    _merge_signed_depth_mappings(config, Path(path).parent / "signed_depth_mappings.toml")
    assets = {t["asset"] for t in config["tokens"]}
    exact_assets = [str(a) for a in monitor.get("quote_exact_assets") or []]
    if exact_assets and not set(exact_assets) <= assets:
        raise ValueError("quote_exact_assets must name configured tokens")
    if not monitor["assets"] or not set(monitor["assets"]) <= assets:
        raise ValueError("enabled asset has no explicit token address")
    for token in [*config["tokens"], config["quote_token"]]:
        address = token["address"]
        if len(address) != 42 or not address.startswith("0x"):
            raise ValueError("invalid token address")
        int(address[2:], 16)
        if not 0 <= token["decimals"] <= 36:
            raise ValueError("invalid token decimals")
        for pool in token.get("pools", []):
            for field in ("address", "base_address", "quote_address"):
                value = pool.get(field, "")
                if len(value) != 42 or not value.startswith("0x"):
                    raise ValueError(f"invalid pool {field}")
                int(value[2:], 16)
            if pool["base_address"].lower() != token["address"].lower():
                raise ValueError("pool base token must be the configured token")
            for field in ("base_decimals", "quote_decimals"):
                if not 0 <= int(pool[field]) <= 36:
                    raise ValueError(f"invalid pool {field}")
    onchain = config.get("onchain", {})
    if onchain.get("enabled"):
        if str(onchain.get("chain_id", "")) != config["tokens"][0]["chain_id"]:
            raise ValueError("onchain chain_id must match the configured token chain")
        if not onchain.get("rpc_url", "").startswith("https://"):
            raise ValueError("onchain rpc_url must be https")
        for field, low, high in (("min_pool_depth_usd", 0, 10**9),
                                 ("min_trade_notional_usd", 0, 10**9),
                                 ("trade_lookback_blocks", 100, 100_000),
                                 ("max_prints_per_asset", 1, 100)):
            value = decimal(onchain[field])
            if not low <= value <= high:
                raise ValueError(f"onchain {field} out of range")
        # The scan scope decides which listings may be adopted, so an
        # unattributable market (UNKNOWN) or one with no mapping evidence is a
        # configuration error rather than something to silently drop.
        # Which markets discovery may scan, and which of those it may adopt.
        scan_scope(onchain)
    notify = config.get("notify", {})
    if notify.get("enabled"):
        if notify.get("channel") not in {"lark_cli", "webhook"}:
            raise ValueError("notify.channel must be lark_cli or webhook when enabled")
        if notify["channel"] == "lark_cli" and not (
            notify.get("lark_chat_id") or notify.get("lark_user_id")
        ):
            raise ValueError("notify needs lark_chat_id or lark_user_id")
        if not 0 <= int(notify.get("cooldown_seconds", 0)) <= 86400:
            raise ValueError("notify.cooldown_seconds out of range")
        if not 1 <= int(notify.get("max_alerts_per_cycle", 1)) <= 20:
            raise ValueError("notify.max_alerts_per_cycle out of range")
    # Machine-discovered listings are merged on top of the hand-written config.
    discovered_path = onchain.get("discovered_config")
    if discovered_path:
        path = Path(discovered_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[3] / discovered_path
        if path.exists():
            with open(path, "rb") as handle:
                extra = tomllib.load(handle, parse_float=decimal).get("tokens", [])
            have = {t["address"].lower() for t in config["tokens"]}
            for token in extra:
                if token["address"].lower() in have:
                    continue
                config["tokens"].append(token)
                have.add(token["address"].lower())
                if token["asset"] not in monitor["assets"]:
                    monitor["assets"].append(token["asset"])
    # The order style lives in the execution policy, not here. The alert has to
    # be priced the way the trade would actually be executed, and two answers to
    # "maker or taker" is how a threshold and its execution drift apart.
    policy_path = Path(config.get("execution_policy", "config/automation_policy.toml"))
    if not policy_path.is_absolute():
        policy_path = Path(__file__).resolve().parents[3] / policy_path
    if policy_path.exists():
        with open(policy_path, "rb") as handle:
            config["execution_policy"] = tomllib.load(handle, parse_float=decimal)
        config["execution_policy_path"] = str(policy_path)
    return config


def scan_scope(onchain: dict) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(scanned, adopted)`` markets, or a refusal.

    Two different questions, kept apart on purpose:

    * ``token_scan_countries`` — which markets discovery may *look at*. Looking
      is read-only: issuer asset list plus X Layer factory probes.
    * ``token_adopt_markets`` — which of those may be hot-added to price
      monitoring (and written to ``config/discovered_tokens.toml``).

    Adopting is the step that starts spending this machine's single proxy on a
    whole market, so a market with no reviewed mapping stays scanned-but-not-
    adopted until the operator moves it. Adopt must be a subset of scan: a
    market we never look at cannot be adopted, and saying otherwise would read
    as "nothing listed there".
    """
    try:
        scanned = validate_scan_countries(onchain.get("token_scan_countries", ["HK"]))
        adopted = validate_scan_countries(onchain.get("token_adopt_markets", ["HK"]))
    except ValueError as exc:
        raise ValueError(f"onchain scan scope: {exc}") from exc
    outside = [market for market in adopted if market not in scanned]
    if outside:
        raise ValueError("onchain.token_adopt_markets must be a subset of "
                         f"token_scan_countries, got {'/'.join(outside)}")
    # The trade-print scan is the expensive on-chain read, so it is named per
    # adopted market. Naming a market nothing adopts is a configuration mistake
    # worth refusing: it reads as "US trade prints are on" while the switch that
    # would act on it can never fire.
    scan_markets = onchain.get("token_adopt_trade_scan_markets")
    if scan_markets is not None:
        stray = [str(m).upper() for m in scan_markets
                 if str(m).upper() not in adopted]
        if stray:
            raise ValueError("onchain.token_adopt_trade_scan_markets must be a "
                             f"subset of token_adopt_markets, got {'/'.join(stray)}")
    return scanned, adopted


def _proxy_environment(proxy: str | None) -> dict:
    """Environment for CLI/subprocess reads: the reviewed route, nothing else."""
    if not proxy:
        return {}
    return {"ARB_HTTP_PROXY": proxy, "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy}


def _is_rate_limit(error: object) -> bool:
    """Did this failed quote actually answer "you are asking too often"?

    The OnchainOS CLI answers a burst with "Rate limited — retry with backoff"
    and an unreachable proxy with "Network unavailable — check your
    connection". Both arrive as ``status="api_error"``, and until 2026-09-21
    both were counted as rate limits — which is how a proxy outage looked like
    a venue throttle. The two have different fixes, so they get different
    counters.
    """
    text = str(error or "").lower()
    return "rate limit" in text or "too many request" in text


def _error_signature(error: dict) -> tuple[str, str]:
    """Stable identity of one reported source error: (key, signature).

    The key is what the error is *about* (kind plus the asset/venue it names);
    the signature includes the message, so a failure that changes from a 429 to
    a timeout is a new event rather than an update nobody hears about.
    """
    kind = str(error.get("kind") or error.get("source") or "unknown")
    subject = str(error.get("asset") or error.get("instrument")
                  or error.get("venue") or "-")
    message = str(error.get("error") or "")
    return f"{kind}|{subject}", f"{kind}|{subject}|{message}"


def _as_mapping(value: object) -> dict:
    """A plain dict, or ``{}``.

    Diagnostics that come off a pluggable adapter must not be able to break a
    run summary (or its JSON) just because a test double — or a future adapter
    — does not model the read-only view yet. A missing view is empty, never a
    fabricated one.
    """
    return dict(value) if isinstance(value, dict) else {}


def _worker_exit_view(dispatch_record: dict, kind: str) -> dict:
    """Present a worker-mode exit task under the key the board already reads.

    `funding_exit` / `basis_exit` used to be the rules' own result records. In
    worker mode the rules run on the worker, so the cycle reports the newest
    finished task of that kind instead of inventing a second result — and says
    "queued" when the task has been handed over but has not answered yet.
    """
    entry = (dispatch_record or {}).get(kind)
    if entry:
        view = dict(entry.get("result") or {})
        view["worker_task"] = entry.get("task")
        return view
    submitted = any(row.get("kind") == kind
                    for row in (dispatch_record or {}).get("submitted") or [])
    return {"mode": dispatch_record.get("order_mode"),
            "reason": "queued" if submitted else "not_due",
            "worker": True, "kind": kind}


def candidate_roundtrip(*, row: dict, direction: dict, costs, target,
                        min_edge_bps, buffer_bps=None,
                        expected_profit_bps=None, perp_style="taker") -> dict:
    """Cost of one open-and-close for one candidate, on the sides it would take.

    The alert used to report only the entry cost, so a "+23 bps net entry basis"
    looked tradable even when getting back out cost 60 bps. Both spreads and the
    exit taker fee belong in the message, next to the basis they have to beat.
    The style matters: a maker-first round trip rests both perpetual legs, so it
    is priced with the maker fee and without a perpetual spread to cross.
    """
    perp = direction.get("perp") or {}
    quote_row = next(
        (q for q in (row.get("perp_quotes") or [])
         if q.get("venue") == perp.get("venue")
         and q.get("instrument") == perp.get("instrument")), {})
    gas = getattr(costs, "roundtrip_gas_budget_usdt", None)
    result = roundtrip_cost_bps(
        spot_ask=row.get("onchain_spot_ask_usd"),
        spot_bid=row.get("onchain_spot_bid_usd"),
        perp_bid=perp.get("price"), perp_ask=quote_row.get("ask"),
        perp_fee_bps=costs.perp_fee_bps,
        gas_bps=gas, perp_style=perp_style,
        perp_maker_fee_bps=getattr(costs, "perp_maker_fee_bps", None))
    # The bar is cost + buffer + expected profit. `min_edge_bps` stays in the
    # record as the older net-entry threshold, for continuity.
    result["breakeven_gross_bps"] = breakeven_gross_bps(
        result,
        buffer_bps=buffer_bps if buffer_bps is not None else ZERO,
        expected_profit_bps=(expected_profit_bps if expected_profit_bps is not None
                             else min_edge_bps))
    result["buffer_bps"] = str(buffer_bps) if buffer_bps is not None else None
    result["expected_profit_bps"] = (str(expected_profit_bps)
                                     if expected_profit_bps is not None else None)
    result["exit_assumption_bps"] = str(target) if target is not None else None
    result["assets"] = f'{row.get("asset")} {perp.get("venue")}:{perp.get("instrument")}'
    return result


def limits_from_config(config: dict) -> BasisLimits:
    c = config["monitor"]
    return BasisLimits(
        total_capital=decimal(c["total_capital_usdt"]),
        spot_allocation=decimal(c["spot_allocation_usdt"]),
        margin_allocation=decimal(c["margin_allocation_usdt"]),
        max_age_ms=int(c["max_age_ms"]), max_pair_skew_ms=int(c["max_pair_skew_ms"]),
        max_unhedged_bps=decimal(c["max_unhedged_bps"]),
        max_unhedged_quote_usdt=decimal(c.get("max_unhedged_quote_usdt", "0.5")),
        min_net_edge_bps=decimal(c["min_net_edge_bps"]),
        min_profit=decimal(c["min_net_profit_quote"]),
    )


def costs_from_config(config: dict) -> RoundTripCosts:
    raw = config["costs"]
    values = {}
    for f in fields(RoundTripCosts):
        if f.name not in raw:
            continue
        if f.name == "verified":
            values[f.name] = bool(raw[f.name])
        elif f.name == "valid_until_ms":
            values[f.name] = int(raw[f.name])
        else:
            values[f.name] = decimal(raw[f.name])
    return RoundTripCosts(**values)


def token_listing_market(token: dict) -> str:
    """Which market a *configured token block* belongs to.

    Discovery reads the issuer's ``underlying.listingCountry`` and never guesses
    (see ``data.xstocks_registry.listing_market``). A hand-written config block
    has no such field for the Hong Kong names that predate it, so those blocks
    are recognised by their reviewed ``hk_code``. Everything else without an
    explicit country stays ``UNKNOWN`` and is refused by the risk gate rather
    than being adopted into whichever market happens to be first.
    """
    explicit = normalize_market(token.get("listing_country") or token.get("market"))
    if explicit != UNKNOWN_MARKET:
        return explicit
    if str(token.get("hk_code") or "").strip():
        # Legacy HK block: `hk_code` is the reviewed HKEX code, and every block
        # carrying one was reviewed as a Hong Kong listing.
        return HK_MARKET
    return UNKNOWN_MARKET


def token_probe_key(asset: dict) -> str:
    """Stable identity of one issuer listing, for the probe-rotation ledger.

    The rotation has to survive a restart, and an *index* into this round's
    pending list does not. Restarts are frequent here (the monitor was restarted
    several times on 2026-09-22, each time landing a `token_universe_scan`), and
    every restart reset the index to 0 — so the same first 20 names were probed
    again and again while ``probes_pending_after_cap`` stayed at 158, and the
    "a 193-name sweep every ~5 hours" claim was quietly untrue. Market + issuer
    symbol is stable across restarts *and* across the composition change that
    shifts an index: a name adopted in an earlier round leaves the pending list,
    moving every later index by one.
    """
    return f"{listing_market(asset)}:{str(asset.get('symbol') or '').strip()}"


def quote_conversion_from_token(token: dict) -> QuoteConversion | None:
    """Optional ``[tokens.quote_conversion]`` observation for a token block.

    Absent for Hong Kong rows, whose reviewed ``quote_to_usdt`` lives inside the
    per-contract mapping instead. Present means "here is the stablecoin→USDT
    rate we read, when we read it and where from" — never an assumed 1.0.
    """
    raw = token.get("quote_conversion")
    if not isinstance(raw, dict) or not raw:
        return None
    def _int(value) -> int:
        return int(decimal(value)) if value is not None else 0
    return QuoteConversion(
        quote_symbol=str(raw.get("quote_symbol") or ""),
        quote_address=str(raw.get("quote_address") or "").lower(),
        quote_to_usdt=(decimal(raw["quote_to_usdt"])
                       if raw.get("quote_to_usdt") is not None else None),
        source=str(raw.get("source") or ""),
        observed_ts_ms=_int(raw.get("observed_ts_ms")),
        valid_until_ms=_int(raw.get("valid_until_ms")),
        peg_deviation_bps=(decimal(raw["peg_deviation_bps"])
                           if raw.get("peg_deviation_bps") is not None else None),
        verified=bool(raw.get("verified", False)),
    )


def mapping_from_token(token: dict, contract: Contract) -> PairMapping:
    # Per-venue/instrument overrides are mandatory; matching tickers do not
    # establish identical stock exposure. Expiry covers FX and wrapper ratios.
    spec = token.get("mappings", {}).get(contract.key, {})
    conversion = quote_conversion_from_token(token)
    # The rate used for the spot leg is the observed one when a conversion is
    # configured: a US pool quotes in USDG/USDT0/USDC, and substituting 1.0
    # there is the assumption this field exists to remove.
    if conversion is not None and conversion.quote_to_usdt is not None:
        quote_to_usdt = conversion.quote_to_usdt
    elif "quote_to_usdt" in spec:
        quote_to_usdt = decimal(spec["quote_to_usdt"])
    else:
        quote_to_usdt = None
    return PairMapping(
        token["asset"],
        decimal(spec["shares_per_token"]) if "shares_per_token" in spec else None,
        decimal(spec["shares_per_perp_unit"]) if "shares_per_perp_unit" in spec else None,
        quote_to_usdt,
        bool(token.get("mapping_verified", False)) and bool(spec.get("verified", False)),
        spec.get("evidence", ""), int(spec.get("valid_until_ms", 0)),
        listing_market=token_listing_market(token),
        underlying_symbol=str(token.get("stock_symbol") or token.get("hk_code") or ""),
        perp_contract_multiplier=(decimal(spec["perp_contract_multiplier"])
                                  if "perp_contract_multiplier" in spec else None),
        token_address=str(token.get("address") or "").lower(),
        wrapper_address=str(token.get("wrapper_address") or "").lower(),
        quote_conversion=conversion,
    )


def mapping_status(token: dict, timestamp_ms: int) -> dict:
    """Is the wrapper→share→contract ratio claimed for this token still valid?

    One contract's expiry must not vouch for another contract's ratio, so the
    answer is per contract and always carries the evidence string that was
    accepted, plus whether it has expired.
    """
    entries = []
    for contract_key, spec in (token.get("mappings") or {}).items():
        valid_until = int(spec.get("valid_until_ms") or 0)
        claimed = bool(token.get("mapping_verified")) and bool(spec.get("verified"))
        entries.append({
            "contract": contract_key,
            "shares_per_token": spec.get("shares_per_token"),
            "shares_per_perp_unit": spec.get("shares_per_perp_unit"),
            "quote_to_usdt": spec.get("quote_to_usdt"),
            "valid_until_ms": valid_until,
            "expired": valid_until <= timestamp_ms,
            "verified": claimed and valid_until > timestamp_ms,
            "evidence": spec.get("evidence"),
        })
    return {
        "verified": any(entry["verified"] for entry in entries),
        "entries": entries,
    }


def quote_from_record(record: dict) -> SpotQuote:
    data = dict(record)
    for key in ("requested_amount", "token_quantity", "quote_amount", "gas_quote", "price_impact_pct"):
        if data.get(key) is not None:
            data[key] = decimal(data[key])
    data["routes"] = tuple(data.get("routes", []))
    return SpotQuote(**data)


def load_anchor_references(path: str | None) -> dict[str, AnchorReference]:
    if not path:
        return {}
    with open(path, "rb") as handle:
        raw = tomllib.load(handle, parse_float=decimal)
    return {
        r["asset"]: AnchorReference(
            decimal(r["previous_close_per_token"]), decimal(r["independent_current_per_token"]),
            date.fromisoformat(r["previous_session"]), int(r["observed_ts_ms"]),
            int(r["valid_until_ms"]), bool(r["normalized_and_actions_checked"]),
            r["source"], r["asset"], r["quote_currency"],
        ) for r in raw.get("references", [])
    }


def format_discovery_message(additions: list[dict], scope: list[str] | None = None) -> str:
    """Feishu text for newly tradable listings.

    The listing venue is printed from the entry's `market` field, never from a
    hard-coded "HK": the 2026-09-15 batch announced SK hynix, Reddit and 32
    other US-listed names as "（HK …）" because this line assumed Hong Kong.
    """
    scope_text = "/".join(
        market_label(country, "").strip() or country for country in (scope or [])
    ) or "未限定"
    lines = [f"【套利监控】发现新的股票代币，已加入价格监控（范围：{scope_text}）"]
    for entry in additions:
        lines += [
            f"标的: {entry['name']}（{market_label(listing_market(entry), entry['hk_code'])}）"
            f"代币 {entry['symbol']}",
            f"链上池: {entry['pool']}  计价 {entry['pools'][0]['quote_symbol']}",
            f"发现时深度: {entry['depth_usd']} USD  池价: {entry['pool_price_usd']}",
            f"对应永续: {entry['cex_match'] or '未匹配到（只有现货腿）'}",
            "",
        ]
    lines.append("仅提醒：已开始采集价格；是否有基差仍需看板确认。")
    return "\n".join(lines)


class HKMonitor:
    def __init__(self, config: dict, proxy: str | None = None, db: str | None = None):
        self.config, self.settings = config, config["monitor"]
        self.proxy = proxy
        self.calendar = HKCalendar.from_config(config["calendar"])
        self.clients = {v: CexPublicAdapter(v, proxy) for v in self.settings["venues"]}
        # Lighter is the one venue whose REST book carries no venue clock, so the
        # monitor holds its order-book WebSocket instead (2026-09-22). The stream
        # can only be started once the venue's own catalogue has told us the
        # market ids — see `_sync_lighter_stream`.
        self._lighter_stream_config = dict((config.get("streams") or {}).get("lighter") or {})
        self.lighter_stream: LighterOrderBookStream | None = None
        self.store = EventStore(db or self.settings["db"])
        self._closing = False
        # Retention runs beside the sampler, not inside a cycle: the first pass
        # over a multi-gigabyte file holds the write lock a batch at a time.
        self._retention_thread: threading.Thread | None = None
        self._retention_last_ms = 0
        self.references = ReferenceAdapter(PublicHTTP(proxy))
        self.quotes = OnchainOSQuoteAdapter(proxy, int(self.settings["quote_ttl_ms"]))
        self.tokens = [t for t in config["tokens"] if t["asset"] in self.settings["assets"]]
        self.contracts: list[Contract] = []
        self.reference_cache: dict[str, dict] = {}
        self.pool_cache: dict[str, dict] = {}
        self.last_discovery, self.last_reference, self.last_onchain = 0, 0, 0
        # Reference refreshes run on their own thread from the second round on.
        # Until 2026-09-21 they ran inline, on a 20 s budget, in the middle of
        # the sampling loop; they are evidence, so they must not hold the loop.
        # The *first* round is still inline: `reference_cache` decides whether an
        # asset can be screened at all, and starting up blind for a whole cycle
        # would be a new gap rather than a fix.
        self._reference_future = None
        self._reference_primed = False
        self.reference_rounds = 0
        self._reference_deferred = 0
        self.reference_round_state: dict = {}
        self._reference_pool = ThreadPoolExecutor(max_workers=1,
                                                  thread_name_prefix="reference")
        # Which raw discovery payloads we already stored, and when. See
        # `_record_discovery`: identical payloads are skipped.
        self._discovery_digest: dict[str, str] = {}
        self._discovery_digest_ms: dict[str, int] = {}
        self.last_quote_batch_ms = 0
        self.quote_rate_limited = 0
        # One cycle's spot quote grid is bounded. Every probe is an OnchainOS CLI
        # round trip over the single local proxy the 60 s sampling loop also
        # uses, so a market adopted for screening does not get to multiply the
        # grid by its name count. Measured 2026-09-22 on the HK grid: 6 assets =
        # 12 probes ≈ 6-12 s per cycle. Issuing the 114 US names the same way
        # would be 228 probes ≈ 55 waves through 4 workers, i.e. minutes per
        # round — and the HK leg's own quotes are what would age out first.
        #
        # So the markets named in ``quote_slice_markets`` rotate: each cycle
        # quotes the next `quote_slice_assets_per_cycle` names from a cursor, so
        # the whole market is still covered, just across N cycles instead of in
        # one. An empty market list means the old behaviour (quote everything),
        # which is what the HK-only configuration has always been.
        self.quote_slice_markets = {str(m).upper() for m in
                                    (self.settings.get("quote_slice_markets") or [])}
        self.quote_slice_assets_per_cycle = max(
            0, int(self.settings.get("quote_slice_assets_per_cycle", 0) or 0))
        self.quote_slice_cursor = 0
        self.quote_slice_report: dict = {}
        # Empty means "the slice above is the only bound". A non-empty set is
        # the execution quote list: OnchainOS exact-in is not how the 100-name
        # screen is priced.
        self.quote_exact_assets = {str(a) for a in
                                   (self.settings.get("quote_exact_assets") or [])}
        self.price_screen: dict = {}
        self.depth_watchlist = None
        self.depth_excluded = None
        # Every `api_error` retry, whether or not it was a rate limit. The two
        # used to be the same counter, so a proxy answering "Network
        # unavailable" was recorded as "the venue rate-limited us" — measured
        # 2026-09-21, the two have completely different causes and fixes.
        self.quote_api_error_retries = 0
        # Stage-2: the quote channel's own health, shared by every probe in
        # every cycle, so one unreachable proxy does not cost 16 x 4 attempts.
        self.quote_channel = QuoteChannelHealth(
            failure_threshold=int(self.settings.get("quote_channel_failures", 3)),
            cooldown_ms=int(self.settings.get("quote_channel_cooldown_ms", 30_000)),
            cooldown_max_ms=int(
                self.settings.get("quote_channel_cooldown_max_ms", 300_000)))
        # Per-asset wall-clock budget for one buy+sell probe. The batch budget
        # bounds the round; this bounds one asset, so a stuck asset releases its
        # worker instead of holding it until the batch deadline.
        self.quote_asset_deadline_ms = max(
            3_000, int(float(self.settings.get("quote_asset_deadline_seconds", 12))
                       * 1000))
        # Adaptive quote concurrency: never above the configured grid width (the
        # operator-visible knob), never below the floor. See `_adapt_quote_workers`.
        self.quote_workers_ceiling = max(1, int(self.settings.get("quote_workers", 8)))
        self.quote_workers_floor = max(
            1, min(self.quote_workers_ceiling,
                   int(self.settings.get("quote_workers_min", 2))))
        self.quote_workers = self.quote_workers_ceiling
        self.quote_concurrency_log: list[dict] = []
        self._quote_adapt_cooldown = 0
        self._quote_rate_baseline = 0
        self._quote_round_history: list[dict] = []
        # Set while the market batch is running, so a reference round that comes
        # due in that window waits for the next cycle instead of competing for
        # the same proxy.
        self._quote_window = threading.Event()
        # Stage-0 instrumentation: where one cycle's wall clock went, and how
        # the quote grid behaved. Recorded only; nothing reads it back to decide
        # anything. Without it the 1 322 s sampling gap on 2026-09-21 could not
        # be attributed to any phase, because the summary only carried
        # `evaluated_at_ms`.
        self.cycle_started_ms = 0
        self.last_cycle_started_ms = 0
        self.cycle_phase_ms: dict[str, int] = {}
        self.quote_metrics: dict = {}
        # Signature of the last *persistent* source error we wrote an event for.
        # Reference and discovery failures are cached state that is re-reported
        # on every cycle until the next refresh, so writing a row per cycle made
        # the event table's error counts mostly duplicates (measured
        # 2026-09-21: 8 124 `source_error` rows in 6 h for a handful of distinct
        # failures). The run summary still carries the full list every cycle —
        # only the event rows are deduplicated, and a changed failure logs
        # again.
        self._source_error_state: dict[str, str] = {}
        # Signatures already written during the current cycle, so a failure
        # reported by the refreshing call and again by the cycle's own summary
        # pass produces one row, not two.
        self._source_error_written: dict[str, str] = {}
        # Alternating venues (operator, 2026-09-17: "现在就做隔轮采样"). Adding
        # Aster/Hyperliquid/Lighter took a cycle from 29 instruments to 39 and the
        # spot quote batch — which shares this machine's one proxy with every
        # perp-book fetch — from ~6s to 10-27s against a 15s freshness budget.
        # These venues are sampled every N-th cycle instead of every cycle, and
        # *not evaluated* on the cycles they are skipped: writing a row per
        # skipped cycle would record "we chose not to look" as if it were "no
        # book existed", which is the one thing this project never conflates.
        self.alternate_venues = set(config["monitor"].get("alternate_venues") or [])
        self.alternate_every = max(1, int(
            config["monitor"].get("alternate_every_cycles", 1)))
        self.cycle_index = 0
        # Perp reads are flattened into one bounded pool instead of one serial
        # thread per venue; the bound is what keeps this machine's single local
        # proxy from being swamped by 8 venues at once.
        self.perp_workers = max(1, int(config["monitor"].get("perp_workers", 8)))
        # Funding reprices every 8 hours; re-reading it every cycle was ~40% of
        # all perp calls for data nothing consumes at that rate.
        self.funding_refresh_cycles = max(1, int(
            config["monitor"].get("funding_refresh_cycles", 20)))
        self.funding_read_cycle: dict[str, int] = {}
        # X Layer gas sponsorship is an observation with a date, not a promise.
        self.last_gas_check = 0
        self.gas_observation: dict | None = None
        # The wallet's spendable balance, read on the gas watcher's own clock.
        # ``None`` means "not read", which the sizing preview reports as unknown
        # rather than as a pass.
        self.wallet_balance_observation: dict | None = None
        self.wallet_runner = None
        # Last exact-in spot quote actually observed for an asset this process
        # has seen, and the pool-state verdict built from it. Both are evidence
        # for the boards; neither is a price.
        self.last_exact_in_quote: dict[str, dict] = {}
        self.pool_state_by_asset: dict[str, dict] = {}
        # Remember what the last gas alert said, so a steady state is not a
        # stream of notifications.
        self.gas_alerted: str | None = None
        self.discovery_errors, self.reference_errors, self.onchain_errors = [], [], []
        self.onchain_settings = config.get("onchain", {})
        self.onchain_enabled = bool(self.onchain_settings.get("enabled"))
        # Cycle quotes are eth_calls to QuoterV2. OnchainOS is only the
        # signature and broadcast of an order that has already cleared.
        self.pool_quotes = str(self.onchain_settings.get("pool_quotes", "")).lower() in {
            "1", "true", "yes"}
        self.quoter_v2 = str(self.onchain_settings.get("quoter_v2") or QUOTER_V2).lower()
        self.onchain = (
            XLayerOnchainAdapter(
                self.onchain_settings, self.tokens,
                XLayerRPC(self.onchain_settings["rpc_url"], proxy),
            ) if self.onchain_enabled else None
        )
        self.onchain_pools: list[dict] = []
        self.onchain_pools_updated_ms = 0
        self.onchain_trades: dict[str, dict] = {}
        self.token_scan_seconds = int(self.onchain_settings.get("token_scan_seconds", 0))
        # Validated at construction time as well as in ``load_config``: a
        # caller that builds the monitor from a dict must not be able to widen
        # the scan scope to a market nobody has evidence for.
        scanned, adopted = scan_scope(self.onchain_settings)
        self.token_scan_countries = set(scanned)
        # Which of the scanned markets discovery may actually adopt into price
        # monitoring. Scanning a market that is not adopted is a read-only
        # report: no config file is written and no token is hot-added.
        self.token_adopt_markets = set(adopted)
        # Which adopted markets also get the `eth_getLogs` trade-print scan.
        # Absent key = every adopted market (the behaviour before 2026-09-22).
        # A market can be worth *screening* long before it is worth spending
        # 43-176 s of archive RPC per scan on its trade prints.
        _scan_markets = self.onchain_settings.get("token_adopt_trade_scan_markets")
        self.adopt_trade_scan_markets = (
            None if _scan_markets is None
            else {str(m).upper() for m in _scan_markets})
        # When each issuer listing was last probed, keyed by ``token_probe_key``.
        # This *is* the rotation cursor: the per-round probe cap always spends
        # its probes on the least recently probed names, so progress survives a
        # restart. An index into the pending list did not — it restarted at 0
        # every time and replayed the same head of the list for ever (measured
        # 2026-09-22: two scans 51 minutes and one restart apart both probed
        # exactly `HK 9 + US 11`, with `probes_pending_after_cap = 158`).
        # Restored from the last `token_universe_scan` event, see
        # `_restore_last_scan_report`. A key with no entry counts as 0 = "never
        # probed", which is why a brand-new listing is looked at on its first
        # round instead of waiting its turn behind the backlog.
        self.token_probe_seen_ms: dict[str, int] = {}
        self.token_scan_full_every = max(1, int(
            self.onchain_settings.get("token_scan_full_every", 24)))
        self.token_scan_round = 0
        # The last completed scan's full report. The scan runs every
        # `token_scan_seconds` (1800 s) while the cycle runs every 60 s, so
        # without this the discovery funnel would read as "—" on 29 cycles out
        # of 30 — the board would look like it had no idea how many US names
        # exist. Kept verbatim and re-reported with the scan's own timestamp,
        # never merged with the current cycle's numbers.
        self.last_token_scan_report: dict | None = None
        self.discovered_config = self.onchain_settings.get("discovered_config")
        self._load_depth_watchlist()
        # The trade-print scan is evidence, not a price, and it is slow (43-176 s
        # measured 2026-09-21). One worker, one scan in flight, so the 60 s
        # sampling cycle never waits for it.
        self._scan_pool = ThreadPoolExecutor(max_workers=1,
                                             thread_name_prefix="onchain-scan")
        self._pool_pool = ThreadPoolExecutor(max_workers=1,
                                             thread_name_prefix="pool-state")
        self._token_pool = ThreadPoolExecutor(max_workers=1,
                                              thread_name_prefix="token-universe")
        self._token_state_lock = threading.RLock()
        # Receipt/gas inspection is operational evidence, not a pricing input.
        # wallet-history and one receipt RPC can each take tens of seconds; it
        # must never hold the 60-second sampling loop.  Keep one worker only so
        # a slow provider cannot create an unbounded backlog.
        self._gas_pool = ThreadPoolExecutor(max_workers=1,
                                            thread_name_prefix="gas-watch")
        self._trade_scan_future = None
        # Pool state is the pricing input, but the public RPC can stall when a
        # batch is rejected and the client falls back to individual calls.  It
        # must never hold the sampling loop hostage: one background read at a
        # time, and a cycle that is still waiting records a gap instead of
        # waiting behind it.
        self._pool_state_future = None
        self._token_scan_future = None
        self._gas_watch_future = None
        # The machine-managed file is rewritten from this list on every
        # addition, so it must start from what is already on disk: otherwise
        # one new listing would drop every entry discovered by an earlier
        # process (that is how the HK names found on 09-13 disappeared when the
        # 09-15 scan wrote its own batch).
        self.discovered_tokens: list[dict] = [
            dict(token) for token in config["tokens"] if token.get("auto_discovered")
        ]
        self.known_token_addresses = {
            t["address"].lower() for t in self.tokens
        } | {p["address"].lower() for t in self.tokens for p in t.get("pools", [])}
        # "Monitored" drives the re-probe budget; "known" only detects newly
        # listed tokens. Keeping them separate means a name we already monitor
        # is never probed or added twice.
        self.monitored_addresses = set(self.known_token_addresses)
        # Start the discovery clock now, not at zero: the first full round walks
        # every unmonitored listing and takes minutes, and that must not delay
        # the first price sample after a restart.
        self.last_token_scan = now_ms()
        # When the funnel below was measured. Kept apart from
        # ``last_token_scan`` because a restored report is older than the
        # process: the page has to say "扫描数据 42 分钟前", not "0 分钟前".
        self.last_token_scan_report_ms: int | None = None
        # The funnel outlives a restart. `last_token_scan` is deliberately reset
        # to "now" so a restart does not fire a 20-probe burst at the shared
        # proxy, but that also means the first cycle after every restart has no
        # scan to report — and a restart is exactly when somebody is looking at
        # the board. Re-read the last one from the event store instead.
        self._restore_last_scan_report()
        self.token_scan_errors: list[dict] = []
        notify_settings = config.get("notify", {})
        self.notify_enabled = bool(notify_settings.get("enabled"))
        self.notifier = FeishuNotifier(notify_settings) if self.notify_enabled else None
        # The operational room, separate from the opportunity room: a stranded
        # leg or an automatic halt must not compete for attention with "there is
        # a basis". Falls back to the signal channel, loudly, when no alert
        # destination is configured.
        self.alert_notifier = (FeishuNotifier(notify_settings, audience="alert")
                               if self.notify_enabled else None)
        self.alert_gate = (
            AlertGate(self.settings["min_net_edge_bps"],
                      int(notify_settings.get("cooldown_seconds", 900)),
                      int(notify_settings.get("max_alerts_per_cycle", 3)),
                      int(notify_settings.get("unexecutable_cooldown_seconds",
                                              notify_settings.get("cooldown_seconds", 900))))
            if self.notify_enabled else None
        )
        self.alerts_hydrated = False
        self.dedup = SignalDeduplicator(int(self.settings["signal_cooldown_ms"]))
        # Unattended execution, if the policy preauthorises it. It is
        # constructed unconditionally so the summary always reports *why* it is
        # not running (mode, kill switch, halt file) instead of staying silent.
        from arb_monitor.execution.auto_execute import AutoExecutor
        policy = {key: (dict(value) if isinstance(value, dict) else value)
                  for key, value in (config.get("execution_policy") or {}).items()}
        policy.setdefault("execution", {})["policy_path"] = str(
            config.get("execution_policy_path") or "config/automation_policy.toml")
        execution = policy.get("execution") or {}
        # The account gate: the unattended path asks the same question
        # `account-status` answers, and refuses to start when `verified` is
        # false (OKX, wallet, gas or ledger unreadable/stale). Without it, an
        # outage showed up as a failed *step* rather than as a refusal.
        verifier = None
        if str(execution.get("require_verified_account", "")).lower() not in {"false", "0", "no"}:
            verifier = self._account_verifier
        self.auto_executor = AutoExecutor(
            policy=policy, monitor_config=config,
            ledger_path=str(config.get("execution_ledger", "data/execution.db")),
            notifier=self.notifier,
            alert_notifier=self.alert_notifier,
            stop_file=str(execution.get("auto_halt_file", "data/AUTO_HALT")),
            lock_file=str(execution.get("auto_lock_file", "data/auto_execute.lock")),
            max_per_cycle=int(execution.get("auto_max_per_cycle", 1)),
            account_verifier=verifier)

    def _account_verifier(self) -> dict:
        """`account-status`'s own snapshot, for the pre-execution gate.

        Deliberately the same function the CLI prints and exits non-zero on, so
        the machine and the operator cannot disagree about what "verified"
        means. Errors are returned as an unverified snapshot rather than raised:
        an unreadable account is a refusal, not a crash.
        """
        from arb_monitor.apps.account_dashboard import (build_snapshot, okx_read_env)
        from arb_monitor.execution.okx_account import OKXReadOnlyClient
        from arb_monitor.execution.wallet_cli import OnchainOSRunner
        try:
            client = OKXReadOnlyClient.from_env(okx_read_env(
                os.environ.get("ARB_OKX_ENV", "config/okx-demo.env")))
            return build_snapshot(
                client=client if client.configured else None,
                runner=OnchainOSRunner(),
                automation_path=str((self.config.get("execution_policy_path")
                                     or "config/automation_policy.toml")))
        except Exception as exc:  # noqa: BLE001 - unverified, not fatal
            return {"verified": False, "unverified_sources": ["snapshot"],
                    "error": f"{type(exc).__name__}:{str(exc)[:160]}"}

    def event(self, run: str, kind: str, payload: object, **kwargs) -> None:
        if self._closing:
            return
        self.store.record_market_event(run, kind, payload, kwargs.pop("recv_ts_ms", now_ms()), **kwargs)

    def _log_source_error(self, run: str, error: dict, **kwargs) -> bool:
        """Write one ``source_error`` row, at most once per cycle per signature.

        Reference and discovery failures are reported twice: once by the call
        that made the attempt (which knows the asset or venue) and once by the
        cycle's own summary pass (which knows the whole picture). Before this
        they were also reported again on every following cycle, because
        ``self.reference_errors`` stays populated until the next refresh — that
        is how 24 h produced 8 002 rows for a handful of distinct failures.

        The rule now: the first report in a cycle wins (so the asset/venue
        columns survive), a repeated cycle is silent unless the signature
        changed, and a failure that comes back after clearing logs again.
        """
        key, signature = _error_signature(error)
        if self._source_error_written.get(key) == signature:
            return False
        if self._source_error_state.get(key) == signature:
            return False
        self._source_error_written[key] = signature
        # Register immediately as well: a round that finishes on the reference
        # worker changes the persistent state between two cycles, and the cycle
        # that adopts it must not report the same change a second time.
        self._source_error_state[key] = signature
        self.event(run, "source_error", error, status="error", **kwargs)
        return True

    def _token_snapshot(self) -> list[dict]:
        """Copy the token universe before the optional discovery worker mutates it."""
        with self._token_state_lock:
            return [dict(token) for token in self.tokens]

    def _reference_host_health(self) -> dict:
        """Per-host reference state for the run summary; read-only.

        A stub or future adapter that does not model the view yields ``{}``
        rather than breaking the summary, because this is diagnostics: it must
        never be the reason a cycle cannot record what happened.
        """
        http = getattr(self.references, "http", None)
        if http is None:
            return {}
        try:
            return _as_mapping(http.host_health())
        except Exception:  # noqa: BLE001 - diagnostics never break a cycle
            return {}

    def _record_discovery(self, run: str, venue: str, raw: object, received: int) -> None:
        """Store a venue's raw metadata dump only when it actually changed.

        Measured 2026-09-17: this payload averages 1 MB, it is written on every
        metadata refresh, and **no code reads it back** — it is evidence, not
        input. It was 36% of a 4.36 GB database. An unchanged payload carries no
        new evidence, so it is skipped; a daily heartbeat keeps "the scan still
        ran" auditable even when nothing changed.

        Nothing downstream is affected: books, references and the summary are
        recorded separately and never read this row.
        """
        digest = hashlib.sha256(dumps(raw).encode()).hexdigest()
        last_ms = self._discovery_digest_ms.get(venue, 0)
        if (self._discovery_digest.get(venue) == digest
                and now_ms() - last_ms < DISCOVERY_HEARTBEAT_MS):
            return
        self._discovery_digest[venue] = digest
        self._discovery_digest_ms[venue] = now_ms()
        self.event(run, "instrument_discovery", raw, venue=venue,
                   source=SOURCES[venue], recv_ts_ms=received)

    def discover(self, run: str) -> list[dict]:
        errors = []
        contracts = []
        pool = ThreadPoolExecutor(max_workers=max(1, len(self.clients)),
                                  thread_name_prefix="metadata-refresh")
        jobs = {pool.submit(client.instruments): venue for venue, client in self.clients.items()}
        budget = float(self.settings.get("metadata_budget_seconds", 20))
        try:
            try:
                completed = as_completed(jobs, timeout=max(1.0, budget))
                for future in completed:
                    venue = jobs[future]
                    try:
                        items, raw, received = future.result()
                        contracts.extend(items)
                        self._record_discovery(run, venue, raw, received)
                        # Catalogue rows that could not become a contract are
                        # reported, never silent: skipping them keeps the rest
                        # of the venue alive (see `parse_contracts`), and a
                        # wholesale field-name change would show up here as
                        # "every row skipped" instead of an empty venue.
                        dropped = list(getattr(self.clients.get(venue),
                                               "skipped_specs", None) or [])
                        if dropped:
                            names = sorted({str(d.get("instrument")) for d in dropped})
                            self._log_source_error(
                                run,
                                {"venue": venue, "error": "contract_spec_skipped:"
                                 + ",".join(names)[:160],
                                 "skipped": len(dropped),
                                 "assets": sorted({str(d.get("asset")) for d in dropped})},
                                venue=venue, source=SOURCES[venue])
                    except Exception as exc:
                        error = {"venue": venue, "error": type(exc).__name__ + ":" + str(exc)[:180]}
                        errors.append(error)
                        self._log_source_error(run, error, venue=venue, source=SOURCES[venue])
            except TimeoutError:
                for future, venue in jobs.items():
                    if future.done():
                        continue
                    error = {"venue": venue,
                             "error": f"metadata_deadline_exceeded:{budget:g}s"}
                    errors.append(error)
                    self._log_source_error(run, error, venue=venue, source=SOURCES[venue])
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        # A metadata outage must not erase the last known instrument registry.
        # Books are still independently aged and never reused as fresh data.
        if contracts:
            self.contracts = sorted(contracts, key=lambda c: (c.asset, c.venue, c.instrument_id))
        self._sync_lighter_stream()
        self.last_discovery = now_ms()
        self.discovery_errors = errors
        self.event(run, "registry", self.contracts, source="native specifications via CCXT")
        return errors

    def _sync_lighter_stream(self) -> None:
        """Start/refresh Lighter's order-book stream from the venue's own markets.

        The market ids come from the catalogue we just read (Lighter names them
        201/203/204/230), never from a hard-coded list here — a market the venue
        lists under a new id must be subscribed to without a code change.
        """
        cfg = self._lighter_stream_config
        if not cfg or "lighter" not in self.settings["venues"]:
            return
        if str(cfg.get("enabled", "true")).lower() in {"false", "0", "no"}:
            return
        markets = lighter_market_ids(self.contracts, self.settings["assets"])
        if not markets:
            return
        if self.lighter_stream is None:
            self.lighter_stream = LighterOrderBookStream(
                markets=markets, url=str(cfg.get("url") or LIGHTER_STREAM_URL),
                proxy=self.proxy)
            self.clients["lighter"].stream = self.lighter_stream
            self.lighter_stream.start()
            return
        self.lighter_stream.set_markets(markets)

    def _reference_tasks(self) -> list[tuple[int, str, object, str]]:
        """Every reference read for one round, in tier order.

        The order *is* the priority: a bounded pool runs its jobs in the order
        they were submitted, so the tier that decides whether an asset can be
        screened at all gets the workers before the daily candles that only
        decorate the board. Nothing is dropped — a round that cannot afford the
        lowest tier records it as not sampled, exactly as before.

        Tiers (see `reference_tiers` in the run summary, and the audit note in
        HANDOFF 2026-09-21):

        * 0 `token_reference` — execution_required: the token identity and
          decimals that decide whether the asset appears at all;
        * 1 `pool_reference` — screening_required: a public pool price used only
          as a non-executable fallback;
        * 2 `stock_daily_reference` / `fx_daily_reference` — display_only:
          evidence for the board, consumed by nothing.
        """
        tasks: list[tuple[int, str, object, str]] = []
        for token in self._token_snapshot():
            tasks.append((0, "token_reference",
                          (lambda t=token: self.references.token(t)), token["asset"]))
            tasks.append((1, "pool_reference",
                          (lambda t=token: self.references.pool_spot(t)), token["asset"]))
            tasks.append((2, "stock_daily_reference",
                          (lambda t=token: self.references.stock_daily(t)), token["asset"]))
        tasks.append((2, "fx_daily_reference", self.references.hkd_daily, "HKD"))
        tasks.sort(key=lambda item: item[0])
        return tasks

    def refresh_references(self, run: str) -> dict:
        """Public reference data, on a hard wall-clock budget.

        These are *evidence*, not fill quotes: a token search result, a pool's
        reserve, a daily candle. The refresh still ran inside the sampling cycle
        until 2026-09-21, and that is what broke the cadence: measured from
        ``data/hk_monitor.db``, cycles that included a refresh spanned
        347 s / 604 s / 1 009 s (``stock_daily_reference`` alone reached 815 s
        in the worst one) while cycles without one spanned 8-150 s. The interval
        is 60 s, so one slow reference request pushed every later sample out by
        minutes.

        The budget is therefore explicit: whatever has not answered when it runs
        out is recorded as ``reference_deadline_exceeded`` and simply left out.
        A missing reference stays missing - a failed refresh is still never
        passed off as a fresh sample (``reference_cache`` is only filled by
        answers that actually arrived).

        Returns what the caller should adopt rather than mutating the monitor's
        own state, because this now also runs on the reference worker's thread.
        """
        errors = []
        cache = {}
        pool_cache = {}
        tasks = {}
        budget_seconds = float(self.settings.get("reference_budget_seconds", 20))
        deadline = time.monotonic() + max(1.0, budget_seconds)
        workers = max(1, int(self.settings.get("reference_workers", 4)))
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="reference")
        try:
            for _tier, kind, call, asset in self._reference_tasks():
                tasks[pool.submit(call)] = (asset, kind)
            try:
                for future in as_completed(
                        tasks, timeout=max(0.0, deadline - time.monotonic())):
                    asset, kind = tasks[future]
                    try:
                        data = future.result()
                        self.event(run, kind, data, asset=asset, source=data["source"],
                                   recv_ts_ms=data["recv_ts_ms"])
                        if kind == "token_reference":
                            cache[asset] = data
                        elif kind == "pool_reference":
                            pool_cache[asset] = data
                    except Exception as exc:
                        error = {"asset": asset, "kind": kind,
                                 "error": type(exc).__name__ + ":" + str(exc)[:180]}
                        errors.append(error)
            except TimeoutError:
                # Only answers that arrived are used; the rest are named, not
                # guessed at.
                for future, (asset, kind) in tasks.items():
                    if future.done():
                        continue
                    error = {"asset": asset, "kind": kind,
                             "error": "reference_deadline_exceeded:"
                                      f"{budget_seconds:g}s"}
                    errors.append(error)
        finally:
            # Never wait for a slow reference: the sampling loop is the point.
            pool.shutdown(wait=False, cancel_futures=True)
        return {"cache": cache, "pool_cache": pool_cache, "errors": errors,
                "finished_ms": now_ms(), "attempted": len(tasks)}

    def _reference_round(self, run: str) -> dict | None:
        """Start or pick up one reference round. Never waits once primed."""
        future = self._reference_future
        if future is not None and future.done():
            self._reference_future = None
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - a failed round is not a crash
                result = {"cache": {}, "pool_cache": {},
                          "errors": [{"kind": "reference_round",
                                      "error": f"{type(exc).__name__}:{str(exc)[:160]}"}],
                          "finished_ms": now_ms(), "attempted": 0}
            self._apply_reference_round(result)
            return result
        if future is None:
            if not self._reference_primed:
                # Startup round: inline on purpose. See `__init__`.
                self._reference_primed = True
                result = self.refresh_references(run)
                self._apply_reference_round(result)
                return result
            if self._quote_window.is_set():
                # A quote grid is in flight on the same proxy. References are
                # evidence and can wait one cycle; a stale quote cannot.
                return None
            # The quote grid and the reference fan-out share one proxy, and the
            # quote side is the one whose age decides whether a trade may be
            # judged at all. When the last grid was already over its per-asset
            # budget, this round is deferred — but only for a bounded number of
            # cycles, so a persistently slow grid cannot starve the evidence
            # completely.
            defer_limit = max(1, int(self.settings.get("reference_defer_max_cycles", 5)))
            if (self._reference_deferred < defer_limit
                    and self.last_quote_batch_ms > self.quote_asset_deadline_ms):
                self._reference_deferred += 1
                self.reference_round_state = {
                    **self.reference_round_state,
                    "deferred_cycles": self._reference_deferred,
                    "deferred_reason": (f"quote_batch_"
                                        f"{self.last_quote_batch_ms}ms_over_"
                                        f"{self.quote_asset_deadline_ms}ms")}
                return None
            self._reference_deferred = 0
            self._reference_future = self._reference_pool.submit(
                self.refresh_references, run)
            return None
        return None

    def _apply_reference_round(self, result: dict) -> None:
        """Adopt a finished round: new answers in, un-refreshed ones kept *and
        labelled*.

        A round that could not re-read an asset used to delete that asset from
        `reference_cache`, which took it out of the screening table for the
        whole refresh interval — a data gap presented as "no such asset". The
        last good answer is now kept, carrying `stale` and the age of its own
        observation, so the board can say "identity confirmed 11 min ago"
        instead of showing nothing.

        This does **not** make stale data executable. `execution_required`
        references are exactly that: `execution_blockers` refuses a candidate
        whose token reference is stale, and `AutoExecutor.trigger` refuses to
        sign it, which is the same outcome as the old "the asset is missing"
        but with a reason attached. The price a stale reference carries can
        never be a candidate on its own either — that path is forced to
        `independent=False`.
        """
        finished = int(result.get("finished_ms") or now_ms())
        adopted: dict[str, dict] = {}
        for asset, row in (result.get("cache") or {}).items():
            adopted[asset] = {**row, "stale": False}
        kept = 0
        for asset, row in self.reference_cache.items():
            if asset in adopted:
                continue
            observed = int(row.get("recv_ts_ms") or 0)
            adopted[asset] = {**row, "stale": True,
                              "cache_hit": True,
                              "cache_age_ms": max(0, finished - observed)}
            kept += 1
        self.reference_cache = adopted
        self.pool_cache = dict(result.get("pool_cache") or {})
        self.reference_errors = list(result.get("errors") or [])
        self.last_reference = finished
        self.reference_rounds += 1
        self.reference_round_state = {
            "rounds": self.reference_rounds,
            "last_round_ms": finished,
            "attempted": int(result.get("attempted") or 0),
            "answered": len(result.get("cache") or {}),
            "kept_from_previous_round": kept,
            "errors": len(self.reference_errors),
        }

    def _reference_worker_state(self) -> dict:
        """Read-only view of the reference worker for the run summary."""
        future = self._reference_future
        return {**self.reference_round_state,
                "rounds": self.reference_rounds,
                "primed": self._reference_primed,
                "in_flight": bool(future is not None and not future.done())}

    def _adapt_quote_workers(self) -> dict:
        """Choose the next cycle's quote concurrency from the last few grids.

        The operator's constraint is explicit: do **not** solve a slow grid by
        raising `quote_workers`. All these probes share one local proxy, so more
        of them in flight is often what produced the timeouts. This only ever
        moves within [floor, configured ceiling] — the configured value stays
        the widest the grid is allowed to be — and it moves at most one step per
        cooldown, on the aggregate of a window rather than on one bad round.

        The signals are the ones the instrumentation now records: how many
        assets missed their own deadline, how many probes were rate-limited, how
        many retries were paid, and the round's own elapsed time against the
        freshness budget. A degraded channel lowers it; a comfortable window
        raises it back.
        """
        metrics = self.quote_metrics or {}
        assets = metrics.get("quote_assets") or []
        durations = [int(row.get("buy_ms") or 0) + int(row.get("sell_ms") or 0)
                     for row in assets
                     if row.get("buy_ms") is not None and row.get("sell_ms") is not None]
        durations.sort()

        def percentile(value: float) -> int | None:
            if not durations:
                return None
            index = min(len(durations) - 1, int(round(value * (len(durations) - 1))))
            return durations[index]

        window = [*self._quote_round_history, {
            "p50_ms": percentile(0.5), "p95_ms": percentile(0.95),
            "elapsed_ms": int(metrics.get("quote_batch_elapsed_ms") or 0),
            "deadline_missed": int(metrics.get("quote_deadline_exceeded_count") or 0),
            "asset_deadline_missed": sum(
                1 for row in assets if row.get("deadline_exceeded")),
            "rate_limited": int(self.quote_rate_limited - self._quote_rate_baseline),
            "retries": int(sum(int(row.get("retry_count") or 0) for row in assets)),
            "requested": int(metrics.get("quote_probe_jobs") or 0),
        }][-12:]
        self._quote_round_history = window
        self._quote_rate_baseline = self.quote_rate_limited

        budget = max(1, int(self.settings.get("max_age_ms", 15000)))
        pressured = [
            row for row in window
            if (row["elapsed_ms"] > budget
                or row["deadline_missed"] > 0
                or row["asset_deadline_missed"] > 0
                or row["rate_limited"] > 0)
        ]
        calm = [
            row for row in window
            if row["elapsed_ms"] <= budget * 0.5
            and row["requested"] > 0
            and row["retries"] == 0
        ]
        changed = 0
        reason = "steady"
        if self.cycle_index >= self._quote_adapt_cooldown:
            if len(pressured) >= max(2, len(window) // 2):
                changed = -1
                reason = (f"{len(pressured)}/{len(window)} of the window was over "
                          f"budget, missed a deadline or was rate limited")
            elif len(calm) >= max(3, (len(window) * 3) // 4):
                changed = 1
                reason = f"{len(calm)}/{len(window)} of the window was well inside budget"
        if changed:
            updated = max(self.quote_workers_floor,
                          min(self.quote_workers_ceiling, self.quote_workers + changed))
            if updated != self.quote_workers:
                self.quote_concurrency_log.append(
                    {"at_ms": now_ms(), "cycle": self.cycle_index,
                     "from": self.quote_workers, "to": updated,
                     "reason": reason})
                self.quote_concurrency_log = self.quote_concurrency_log[-20:]
                self.quote_workers = updated
            self._quote_adapt_cooldown = self.cycle_index + int(
                self.settings.get("quote_adapt_cooldown_cycles", 5))
        return {"quote_workers": self.quote_workers,
                "floor": self.quote_workers_floor,
                "ceiling": self.quote_workers_ceiling,
                "window": len(window),
                "pressured": len(pressured),
                "calm": len(calm),
                "p50_ms": percentile(0.5), "p95_ms": percentile(0.95),
                "decision": ("lower" if changed < 0 else
                             "raise" if changed > 0 else "hold"),
                "reason": reason,
                "recent_changes": self.quote_concurrency_log[-5:]}

    def refresh_onchain(self, run: str) -> list[dict]:
        """Read the 24/7 leg: X Layer pool state and decoded trade prints.

        This is deliberately independent of the cash-market calendars. A
        closed HK/US session does not stop the token from trading, and a pool
        that holds nothing is reported as depth 0 rather than as a price.
        """
        if self.onchain is None:
            return []
        errors: list[dict] = []
        pools = self._pool_states()
        if pools is None:
            errors.append({"kind": "pool_state",
                           "error": "pool_state_in_flight_not_sampled"})
        try:
            trades = self._trade_scan()
        except Exception as exc:
            trades = {}
            errors.append({"kind": "trade_scan",
                           "error": type(exc).__name__ + ":" + str(exc)[:180]})
        if trades is None:
            # One scan is in flight; this cycle reports "not sampled" rather
            # than waiting. The rows below are skipped, which is the same
            # convention the alternating venues use: an absent row means "not
            # sampled", never "no trades happened".
            trades = {}
            errors.append({"kind": "trade_scan",
                           "error": "trade_scan_in_flight_not_sampled"})
        for pool in pools or []:
            self.event(run, "onchain_pool_state", pool, asset=pool.get("asset", ""),
                       source="x-layer rpc " + self.onchain_settings["rpc_url"],
                       recv_ts_ms=pool.get("recv_ts_ms"), venue_ts_ms=pool.get("block_ts_ms"),
                       sequence=pool.get("block_number"),
                       status="error" if pool.get("error") else "ok")
        for asset, report in trades.items():
            self.event(run, "onchain_trade_scan", report, asset=asset,
                       source="x-layer rpc " + self.onchain_settings["rpc_url"],
                       recv_ts_ms=report.get("recv_ts_ms"),
                       venue_ts_ms=report.get("block_ts_ms"),
                       sequence=report.get("head_block"),
                       status="error" if report.get("error") else "ok")
        if pools is not None:
            self.onchain_pools = pools
            self.onchain_pools_updated_ms = now_ms()
        if trades:
            self.onchain_trades = trades
        self.onchain_errors, self.last_onchain = errors, now_ms()
        return errors

    def _pool_states(self) -> list[dict] | None:
        """Start/pick up one pool-state read without blocking the cycle."""
        future = self._pool_state_future
        if future is not None and future.done():
            self._pool_state_future = None
            try:
                return future.result()
            except Exception:
                return []
        if future is None:
            self._pool_state_future = self._pool_pool.submit(self._read_watched_pools)
        return None

    def _load_depth_watchlist(self) -> None:
        """A fresh census drops names whose pools were measured empty.

        Missing or stale file means no filter: the monitor keeps its previous
        behaviour instead of treating "we have not looked today" as "no depth".
        """
        from arb_monitor.data.depth_census import excluded_assets, load_watchlist

        raw = self.onchain_settings.get("depth_watchlist")
        if not raw:
            return
        path = Path(raw)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[3] / raw
        self.depth_watchlist_path = path
        hours = float(self.onchain_settings.get("depth_watchlist_max_age_hours") or 26)
        document = load_watchlist(path)
        self.depth_watchlist = document
        self.depth_excluded = excluded_assets(
            document, now_ms(), int(hours * 3600 * 1000))

    def _read_watched_pools(self) -> list[dict]:
        original = self.onchain.tokens
        try:
            from arb_monitor.data.depth_census import tokens_for_pool_read
            self.onchain.tokens = tokens_for_pool_read(
                original, self.depth_excluded, self.quote_exact_assets)
            return self.onchain.pool_states()
        finally:
            self.onchain.tokens = original

    def _depth_watchlist_summary(self) -> dict:
        document = self.depth_watchlist or {}
        scanned = document.get("scanned_at_ms")
        return {
            "active": self.depth_excluded is not None,
            "scanned_at_ms": scanned,
            "age_ms": None if scanned is None else max(0, now_ms() - int(scanned)),
            "min_depth_usd": document.get("min_depth_usd"),
            "with_depth": len(document.get("with_depth") or []),
            "assets": sorted(str(row.get("asset")) for row in
                             (document.get("with_depth") or []) if row.get("asset")),
            "no_depth": len(document.get("no_depth") or []),
            "unchecked": len(document.get("unchecked") or []),
            "targets": document.get("targets"),
        }

    def _trade_scan(self) -> dict[str, dict] | None:
        """The trade-print scan, run in the background, one at a time.

        Measured 2026-09-21: this scan took 43-176 s per call (``eth_getLogs``
        is capped at 100 blocks on this RPC, so a 300-block lookback is three
        sequential calls per token), and it ran *inside* the 60 s sampling
        cycle. That is what was turning a 60 s cadence into 150-200 s. The scan
        is evidence - the pool state that prices the spot leg is a separate,
        fast call - so the cycle now starts it, walks on, and picks up the
        result on a later cycle. Only one scan is ever in flight, so a slow
        provider cannot pile cycles up behind it.
        """
        future = self._trade_scan_future
        if future is not None and future.done():
            self._trade_scan_future = None
            try:
                return future.result()
            except Exception:  # noqa: BLE001 - a failed scan is not a crash
                return {}
        if future is None:
            self._trade_scan_future = self._scan_pool.submit(
                self.onchain.trade_prints,
                int(self.onchain_settings["trade_lookback_blocks"]),
                int(self.onchain_settings["max_prints_per_asset"]))
        return None

    def _restore_last_scan_report(self) -> None:
        """Re-read the last discovery funnel from the event store.

        The scan's own clock is reset at startup on purpose (see
        ``self.last_token_scan``), so without this every restart blanks the
        funnel on the board for a full 30 minutes. The last report is carried
        over verbatim, including the per-market "probed, no pool" counts in
        ``layers.no_pool`` and the raw ``skipped`` rows they were counted from
        (which keeps the market each name belongs to, something the summary's
        flat ``skipped_no_depth`` list of symbols cannot do).

        Read from the dedicated ``token_universe_scan`` event rather than from
        the newest ``run_summary``: after a restart the newest summary is
        precisely the one that has no funnel in it.
        """
        try:
            previous = self.store.latest_event("token_universe_scan") or {}
        except Exception:  # noqa: BLE001 - an unreadable store is not a crash
            return
        # The probe-rotation ledger, carried by the same event. Restored before
        # the `layers` check below: the ledger is what keeps a restart from
        # replaying the head of the probe list, and it matters even for an
        # event whose funnel we would otherwise refuse to display.
        ledger = previous.get("probe_seen_ms")
        if isinstance(ledger, dict):
            self.token_probe_seen_ms = {
                str(key): int(value) for key, value in ledger.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)}
        if not previous.get("layers"):
            return
        report = {k: v for k, v in previous.items()
                  if not str(k).startswith("_")}
        report["scanned"] = True
        self.last_token_scan_report = report
        self.last_token_scan_report_ms = int(
            previous.get("scanned_at_ms") or previous.get("last_scan_ms") or 0) or None

    def _token_scan(self, run: str, now: int, due: bool = True) -> dict:
        """Run issuer discovery off the sampling thread.

        The issuer list is an enrichment, not a price input.  A full scan can
        page HTTP and probe many pools, so waiting for it in ``cycle`` turns a
        transient listing refresh into a missed market cadence.  Only one scan
        is allowed in flight; the completed result is applied on the next
        cycle, preserving the existing append/hot-add behaviour.

        ``due`` is what starts a *new* scan (the 30-minute clock), and it is
        deliberately separate from collecting a finished one.  Gating both on
        the clock meant a scan that finished a second after it was due sat
        unread until the interval reopened — the three discovery layers the
        board prints stayed ``null`` for a full interval after the work was
        already done.  Collect first, start second: the scan frequency is
        unchanged, and the result is visible one cycle after it lands.
        """
        future = self._token_scan_future
        if future is not None and future.done():
            self._token_scan_future = None
            try:
                report = future.result()
            except Exception as exc:  # noqa: BLE001 - discovery is optional
                error = {"error": type(exc).__name__ + ":" + str(exc)[:140]}
                self.token_scan_errors = [error]
                report = {"scanned": True, "new_listings": 0,
                          "errors": [error], "reason": "background_scan_failed"}
            # Keep the last completed scan verbatim. The scan runs on a
            # 30-minute clock and the cycle on a 60-second one, so 29 cycles out
            # of 30 have no fresh scan to report: without this the board's
            # discovery funnel would read "—" and "how many of the 114 are
            # real?" would be answerable only once every half hour.
            self.last_token_scan_report = report
            self.last_token_scan_report_ms = int(now)
            return report
        if future is not None:
            return {"scanned": False, "reason": "in_flight_not_sampled"}
        if not due:
            return {"scanned": False, "reason": "not_due"}
        # A census already answered "which pools have depth" for today. The
        # 30-minute probe exists to discover that list; repeating it would
        # spend the proxy on names the census just set aside.
        if getattr(self, "depth_excluded", None) is not None:
            return {"scanned": False, "reason": "depth_watchlist_fresh",
                    "no_depth": len(self.depth_excluded)}
        self._token_scan_future = self._token_pool.submit(
            self.refresh_token_universe, run, now)
        return {"scanned": False, "reason": "started_background_scan"}

    def _gas_watch(self, run: str, now: int) -> dict | None:
        """Pick up/start one gas observation without blocking sampling.

        Gas sponsorship changes the cost model and therefore remains visible,
        but it is not needed to price this cycle's quotes.  The old synchronous
        call could spend 30s in ``wallet_history`` and then another long tail
        in receipt RPC retries, making a nominal 60s cycle look hung.
        """
        settings = self.config.get("gas_watch") or {}
        if not settings.get("enabled") or self.onchain is None:
            return None
        future = self._gas_watch_future
        if future is not None and future.done():
            self._gas_watch_future = None
            try:
                return future.result()
            except Exception as exc:  # noqa: BLE001 - watcher is optional
                error = {"status": "unknown", "checked": 0, "paid_count": 0,
                         "note": f"background gas watch failed: {type(exc).__name__}",
                         "samples": []}
                self.gas_observation = error
                self.event(run, "gas_observation", error, source="xlayer receipts",
                           recv_ts_ms=now, status="error")
                return error
        if future is None:
            # The worker performs the existing bounded implementation, including
            # its own interval check and alert transition.  It may finish after
            # this cycle; the next cycle picks it up.
            self._gas_watch_future = self._gas_pool.submit(
                self.refresh_gas_watch, run, now)
            return self.gas_observation
        return self.gas_observation
    def _perp_jobs(self, venues: set, assets: set | None = None) -> list[tuple]:
        """Every (contract, kind) read this cycle should issue, flattened.

        Flattened on purpose. The previous shape was one thread per venue with
        depth-then-funding **serial inside it**, which measured 3.2s for gate's
        8 contracts (16 sequential HTTP calls) and 3.4s for bitget's 6 — and
        `evaluated_at` waits for the slowest venue, so those seconds age every
        spot quote fetched alongside them. Measured 2026-09-17: the last quote
        landed 8.7s before `evaluated_at`, pushing quotes to 10.6-13.8s against a
        15s freshness budget and flagging whole cycles as untrustworthy.

        Funding is only re-read every `funding_refresh_cycles` cycles: it reprices
        on an 8-hour clock, nothing in this process reads the events back
        (`funding_exit` queries OKX directly), and on the venues without an
        execution adapter it is display-only. A failed read is retried on the
        next cycle, because "we could not read it" must not be remembered as
        "we read it".
        """
        jobs = []
        for contract in self.contracts:
            if contract.venue not in venues:
                continue
            if contract.asset not in self.settings["assets"] or not contract.active:
                continue
            # Full depth is an execution input. With pool quotes the book set
            # is whatever this cycle is quoting. Otherwise it stays the small
            # OnchainOS list, so a hundred names do not become a hundred books.
            if assets is not None:
                if contract.asset not in assets:
                    continue
            elif self.quote_exact_assets and not self.pool_quotes:
                allowed = self.quote_exact_assets | set(self.promoted_quote_assets())
                if contract.asset not in allowed:
                    continue
            jobs.append((contract, "depth"))
            # Tracked per contract, not per venue: one contract's funding read
            # failing must not ride on a sibling's success and go unretried for
            # the whole cadence.
            last = self.funding_read_cycle.get(contract.key)
            if (last is None
                    or self.cycle_index - last >= self.funding_refresh_cycles):
                jobs.append((contract, "funding"))
        return jobs

    def _fetch_perp(self, contract, kind: str) -> tuple:
        """One depth or funding read. Never raises: a failure is a result."""
        try:
            if kind == "depth":
                return "depth", contract, self.clients[contract.venue].book(contract), None
            return ("funding", contract,
                    self.clients[contract.venue].funding(contract), None)
        except Exception as exc:
            return kind, contract, None, {
                "instrument": contract.key, "kind": kind,
                "error": type(exc).__name__ + ":" + str(exc)[:180]}

    def _cached_perp_bid(self, contract) -> Decimal | None:
        """Last screen bid, used only to pick a probe size. Not an order price."""
        screen = getattr(self, "price_screen", None) or {}
        for row in screen.get("rows") or []:
            if row.get("instrument") == contract.instrument_id or row.get("asset") == contract.asset:
                try:
                    price = decimal(row.get("okx_bid"))
                except Exception:
                    return None
                return price if price > ZERO else None
        return None

    def _hedge_kwargs(self, contract, token: dict) -> dict | None:
        """Share ratio and the venue's own size floor, or None when either is unknown."""
        spec = (token.get("mappings") or {}).get(contract.key) or {}
        if "shares_per_token" not in spec or "shares_per_perp_unit" not in spec:
            return None
        if min(contract.multiplier, contract.lot_size, contract.min_contracts) <= ZERO:
            return None
        try:
            shares_per_token = decimal(spec["shares_per_token"])
            shares_per_perp = decimal(spec["shares_per_perp_unit"])
        except Exception:
            return None
        if shares_per_token <= ZERO or shares_per_perp <= ZERO:
            return None
        return {
            "shares_per_token": shares_per_token,
            "shares_per_perp_unit": shares_per_perp,
            "contract_multiplier": contract.multiplier,
            "lot_size": contract.lot_size,
            "min_contracts": contract.min_contracts,
            "min_notional": contract.min_notional,
            "token_decimals": int(token.get("decimals") or 18),
        }

    def _probe_bounds(self, contract, token: dict) -> tuple[Decimal, Decimal | None] | None:
        """(size to quote, smaller fallback) from this contract's own minimum.

        The fallback is the venue minimum. The first size is the largest exact
        hedge that still fits in the per-trade cash ceiling, so a deep pool is
        judged at the size the order would actually use. A missing ratio or a
        non-positive ``minSz`` is not replaced with one share.
        """
        kwargs = self._hedge_kwargs(contract, token)
        if kwargs is None:
            return None
        price = self._cached_perp_bid(contract) or ZERO
        try:
            floor = minimum_hedge_tokens(**kwargs, perp_price=price)
        except ValueError:
            return None
        cap = decimal(((self.config.get("execution_policy") or {}).get("limits") or {})
                      .get("per_trade_quote_usdt") or "0")
        primary = floor
        if price > ZERO and cap > ZERO:
            try:
                primary = largest_hedge_tokens(
                    notional_cap=cap, perp_price=price, **kwargs)
            except ValueError:
                primary = floor
        if primary < floor:
            primary = floor
        fallback = floor if primary > floor else None
        return primary, fallback

    def _probe_plan(self, quotable: list, pairs: dict, token_map: dict):
        """Quote jobs and the size each contract is waiting on.

        With ``probe_from_contract_minimum`` the size is that contract's own
        floor (raised to the cash ceiling when the last bid is known). Otherwise
        the configured ``probe_shares`` grid is used unchanged, which is how the
        batch tests pin a size list.
        """
        if not self.settings.get("probe_from_contract_minimum"):
            sizes = [decimal(x) for x in self.settings["probe_shares"]]
            jobs = [(token, size, None) for token, size in self._quote_jobs(quotable, sizes)]
            by_contract = {key: list(sizes) for key in pairs}
            return jobs, by_contract
        jobs = []
        seen = set()
        by_contract: dict[str, list] = {}
        quoted = {str(token.get("asset")) for token in quotable}
        for key, contract in pairs.items():
            if contract.asset not in quoted:
                by_contract[key] = []
                continue
            token = token_map.get(contract.asset)
            bounds = self._probe_bounds(contract, token) if token else None
            if bounds is None:
                by_contract[key] = []
                continue
            primary, fallback = bounds
            by_contract[key] = [primary]
            marker = (contract.asset, str(primary))
            if marker not in seen:
                seen.add(marker)
                jobs.append((token, primary, fallback))
        return jobs, by_contract

    def _quote_jobs(self, quotable: list, sizes: list) -> list[tuple]:
        """Quote jobs, most valuable first.

        A bounded pool runs its jobs in submission order, so the order *is* the
        priority. Quoting every monitored asset costs the same as quoting the
        ones we can trade, but only the latter can become an order, and a slow
        display-only asset must not be the reason an executable quote arrives
        after the freshness budget. Nothing is dropped — the low tiers still run
        whenever there is room.
        """
        ranked = sorted(quotable, key=self._quote_priority)
        return [(token, size) for token in ranked for size in sizes]

    def _quote_priority(self, token: dict) -> int:
        """0 = may be traded by the machine, 1 = has a perp leg, 2 = display.

        Keyed on the *execution* allowlist, not the monitoring list. The point is
        not to hide the other venues, it is to fail in the right direction when
        the grid cannot afford everything.
        """
        allow = (self.config.get("execution_policy") or {}).get("allowlist") or {}
        venues = {str(v) for v in allow.get("venues") or []}
        instruments = {str(v) for v in allow.get("instruments") or []}
        assets = {str(v) for v in allow.get("assets") or []}
        asset = str(token.get("asset") or "")
        if asset in assets and any(
                contract.asset == asset and contract.venue in venues
                and contract.instrument_id in instruments
                for contract in self.contracts):
            return 0
        return 1 if any(contract.asset == asset for contract in self.contracts) else 2

    def _retry_smaller_probe(self, result: tuple, fallback) -> tuple:
        """One smaller exact-out when the cash-ceiling size does not fill.

        The ceiling is the per-trade cap. A pool that cannot deliver that many
        tokens is not a failed market: the contract minimum is still a legal
        order, and that is the size retried here. One retry, not a search.
        """
        token, size, buy, sell, _records, _metrics = result
        if fallback is None or buy is None or buy.status in {"ok", "auth_error"}:
            return result
        try:
            smaller = decimal(fallback)
            asked = decimal(size)
        except Exception:
            return result
        if smaller <= ZERO or smaller >= asked:
            return result
        return self._fetch_quote(token, smaller, None)

    def _fetch_quote(self, token: dict, size, fallback=None) -> tuple:
        """One asset's buy+sell probe, retried on transient errors.

        Extracted from the batch so it can finish — and be evaluated — without
        waiting for the other assets. The sell probe depends on the buy's
        quantity, so the two stay sequential *within* an asset; different assets
        do not wait for each other.

        Quotes are reads, so a retry is allowed here (writes never are). Two
        failures are transient: the venue's "Rate limited — retry with backoff",
        and the CLI's "Network unavailable" when several probes share one local
        proxy. `auth_error` is NOT retried — it trips the cycle's stop flag
        instead, because hammering a channel that is rejecting us is not a fix.

        Returns one extra element over the old shape: a small per-asset timing
        record (`buy_ms`, `sell_ms`, `retry_count`, `final_status`). The batch
        total alone cannot say whether a slow cycle was one asset or all of
        them, and that is the question the 15-second budget keeps asking.
        """
        quote_token = self.config["quote_token"]
        attempts = 1 + max(0, int(self.settings.get("quote_retries", 2)))
        base = float(self.settings.get("quote_backoff_ms", 600)) / 1000
        retries = {"count": 0}
        deadline = time.monotonic() + self.quote_asset_deadline_ms / 1000

        def retrying(call):
            record = None
            for attempt in range(attempts):
                record = call()
                ok = record.status != "api_error"
                self.quote_channel.note(
                    ok, reason=f"{record.status}:{getattr(record, 'error', '')}")
                if record.status != "api_error":
                    return record
                retries["count"] += 1
                with self._quote_lock:
                    self.quote_api_error_retries += 1
                    if _is_rate_limit(getattr(record, "error", "")):
                        self.quote_rate_limited += 1
                if attempt + 1 >= attempts:
                    break
                # Two reasons not to spend another attempt: the shared channel
                # says it is not there (all 16 probes would learn that
                # separately, at ~4 s each), or this asset has already used its
                # own budget and its worker is needed by the others.
                if not self.quote_channel.retry_allowed():
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(base * (2 ** attempt) * (0.5 + random.random()))
            return record

        asset = token.get("asset")
        size_text = str(size)
        metrics = {"asset": asset, "size": size_text, "buy_ms": None,
                   "sell_ms": None, "retry_count": 0, "deadline_exceeded": False,
                   "final_status": "not_started"}

        def timed(record, slot, started):
            metrics[slot] = now_ms() - started
            return record

        # QuoterV2 only when the chain reader exists. A process with on-chain
        # quotes turned off (the hermetic cycle tests) still uses the quote
        # adapter it was given.
        if self.pool_quotes and getattr(self.onchain, "rpc", None) is not None:
            result = self._fetch_pool_quote(token, size, metrics)
            return self._retry_smaller_probe(result, fallback)
        stop = self._quote_stop
        if stop is not None and stop.is_set():
            metrics["final_status"] = "not_sampled"
            return token, size, None, None, [], metrics
        refusal = self.quote_channel.admit()
        if refusal is not None:
            # Not sampled: the channel is known to be down, so this probe is not
            # sent, and no earlier quote is substituted for it.
            metrics.update(final_status="not_sampled", channel=refusal)
            return token, size, None, None, [], metrics
        buy_started = now_ms()
        buy = timed(retrying(
            lambda: self.quotes.quote_exact_out(token, quote_token, size)),
            "buy_ms", buy_started)
        if buy.status == "auth_error":
            stop.set()
            metrics.update(retry_count=retries["count"], final_status="auth_error")
            return token, size, buy, None, [buy], metrics
        quantity = buy.token_quantity
        if not quantity or stop.is_set():
            metrics.update(retry_count=retries["count"],
                           final_status=str(buy.status))
            return self._retry_smaller_probe(
                (token, size, buy, None, [buy], metrics), fallback)
        if time.monotonic() >= deadline:
            # The buy leg already used this asset's budget. The pair cannot be
            # judged without the sell side, so it is this asset's round that is
            # lost — not the whole grid's.
            metrics.update(retry_count=retries["count"],
                           final_status="asset_deadline_exceeded",
                           deadline_exceeded=True)
            return token, size, buy, None, [buy], metrics
        sell_started = now_ms()
        sell = timed(retrying(
            lambda: self.quotes.quote(token, quote_token, "sell", quantity)),
            "sell_ms", sell_started)
        if sell.status == "auth_error":
            stop.set()
        metrics.update(retry_count=retries["count"],
                       final_status=("ok" if buy.status == "ok" and sell.status == "ok"
                                     else str(sell.status if sell.status != "ok"
                                              else buy.status)))
        return token, size, buy, sell, [record for record in (buy, sell) if record], metrics

    def _fetch_pool_quote(self, token: dict, size, metrics: dict) -> tuple:
        """Buy and sell quotes from QuoterV2 against this token's own pool.

        The buy is exact-out for ``size`` wrapper tokens. The sell quotes that
        same quantity back. No OnchainOS call is made here.
        """
        pools = token.get("pools") or []
        rpc = getattr(self.onchain, "rpc", None)
        if not pools or rpc is None:
            metrics["final_status"] = "no_pool"
            return token, size, None, None, [], metrics
        pool = pools[0]
        base = str(pool.get("base_address") or token.get("address") or "")
        quote = str(pool.get("quote_address") or "")
        base_decimals = int(pool.get("base_decimals") or token.get("decimals") or 18)
        quote_decimals = int(pool.get("quote_decimals") or 6)
        fee = int(pool.get("fee") or 500)
        ttl = int(self.settings["quote_ttl_ms"])
        symbol = str(pool.get("quote_symbol") or "")

        def one(side: str, *, exact_out: bool, token_in: str, token_out: str,
                 amount_raw: int, token_quantity, requested) -> SpotQuote:
            started = now_ms()
            raw = quote_single(
                rpc, self.quoter_v2, token_in=token_in, token_out=token_out,
                amount_raw=amount_raw, fee=fee, exact_out=exact_out)
            received = now_ms()
            if raw is None:
                return SpotQuote(
                    token["asset"], side, str(token.get("chain_id") or "196"),
                    base, quote, symbol, requested, None, None, "api_error",
                    started, received, received + ttl, "xlayer:quoter_v2",
                    error="quoter_reverted")
            quote_amount = Decimal(raw) / (Decimal(10) ** quote_decimals)
            return SpotQuote(
                token["asset"], side, str(token.get("chain_id") or "196"),
                base, quote, symbol, requested, token_quantity, quote_amount, "ok",
                started, received, received + ttl, "xlayer:quoter_v2",
                routes=("uniswap_v3_pool",))

        buy_raw_dec = Decimal(size) * (Decimal(10) ** base_decimals)
        if buy_raw_dec != buy_raw_dec.to_integral_value() or buy_raw_dec <= 0:
            metrics["final_status"] = "size_not_representable"
            return token, size, None, None, [], metrics
        buy_raw = int(buy_raw_dec)
        buy_started = now_ms()
        buy = one("buy", exact_out=True, token_in=quote, token_out=base,
                  amount_raw=buy_raw, token_quantity=Decimal(size), requested=Decimal(size))
        metrics["buy_ms"] = now_ms() - buy_started
        if buy.status != "ok" or not buy.token_quantity:
            metrics["final_status"] = buy.status
            return token, size, buy, None, [buy], metrics
        sell_raw = int(buy.token_quantity * (Decimal(10) ** base_decimals))
        sell_started = now_ms()
        sell = one("sell", exact_out=False, token_in=base, token_out=quote,
                   amount_raw=sell_raw, token_quantity=buy.token_quantity,
                   requested=buy.token_quantity)
        metrics["sell_ms"] = now_ms() - sell_started
        metrics["final_status"] = "ok" if sell.status == "ok" else sell.status
        return token, size, buy, sell, [buy, sell], metrics

    def _evaluate_pair(self, run: str, contract, size, buy, sell, book,
                       evaluated_at: int, context: dict) -> dict:
        """One contract × one probe size, decided at its own moment.

        Streaming on purpose (operator, 2026-09-17): "每条配对在自己两条腿都到位时
        就立刻评估…这样一条慢探针只作废它自己那一个配对". Waiting for the slowest
        leg of the whole cycle meant one 20-second probe aged *every* book and
        quote past the 15s budget and voided all 39 rows at once. Here each pair
        is evaluated when its own two legs are in hand, so a slow probe costs
        exactly its own pair.
        """
        token = context["token_map"][contract.asset]
        mapping = mapping_from_token(token, contract)
        market = token_listing_market(token)
        # The cost of a round trip belongs to the asset and the venue, not to a
        # single number compiled into the repo: a mapping block may carry its
        # own reviewed cost table, and an asset without one keeps the global
        # default unchanged.
        costs = context["cost_table"].for_pair(contract.asset, contract.venue,
                                               contract.instrument_id)
        options = {
            "contract": contract, "mapping": mapping, "buy": buy, "sell": sell,
            "costs": costs, "limits": context["limits"],
            "target_exit_basis_bps": context["target"],
            "phase": self.calendar.phase(evaluated_at),
            "wallet_balance_usdt": context.get("wallet_balance_usdt"),
        }
        strategy = (HKBasisStrategy(**options) if market == HK_MARKET
                    else TokenPerpBasisStrategy(
                        listing_market=market,
                        underlying_symbol=mapping.underlying_symbol,
                        **options))
        evaluation = strategy.evaluate([book] if book is not None else [], evaluated_at)
        evaluation["probe_size"] = size
        evaluation["probe_unit"] = "shares"
        if buy is not None and buy.status == "ok" and buy.token_quantity:
            # An exact-in quote is the only on-chain thing that may be sized
            # against; the pool's slot0 price and a historical print are not.
            self.last_exact_in_quote[contract.asset] = {
                "observed_ts_ms": int(buy.request_ts_ms),
                "quote_usdt": buy.quote_amount,
                "token_quantity": buy.token_quantity,
                "quote_currency": buy.quote_currency,
                "quote_address": buy.quote_address,
                "source": buy.source,
            }
        # Its own timestamp, not the cycle's: that is the number the freshness
        # checks were made against, so it has to survive into the record.
        evaluation["evaluated_at_ms"] = evaluated_at
        self.event(run, "basis_evaluation", evaluation, asset=contract.asset,
                   venue=contract.venue)
        for intent in strategy.on_snapshot([book] if book is not None else [],
                                           evaluated_at):
            if self.dedup.accept(intent):
                self.store.record_intent(intent)
                evaluation["_intent"] = True
        return evaluation

    def _quote_batch(self, enabled: bool) -> tuple[dict, list]:
        """One buy probe and one sell probe per asset and size.

        Every probe is a CLI round trip, so they run concurrently: issued
        sequentially the full grid took ~70s, during which the order books that
        were fetched alongside them aged out of the pairing budget. The sell
        probe depends on the buy quantity, so it follows its own buy; different
        (asset, size) pairs do not wait for each other.

        An authorization failure still stops the whole grid: a quote channel
        that is rejecting us must not be hammered 48 more times.
        """
        quote_token = self.config["quote_token"]
        sizes = [decimal(x) for x in self.settings["probe_shares"]]
        # Only probe what could ever pair. Operator's instruction 2026-09-17:
        # "把没有永续腿的标的踢出报价网格 —— MIXUx / HKEXCx". A token with no
        # perpetual anywhere cannot form a long-spot/short-perp pair, so its two
        # CLI probes per cycle are pure cost: measured that day, 2 of the 8
        # quoted assets (MIXUx, HKEXCx) had no perp leg at all, i.e. 4 of the 16
        # probes — 25% of the waves the pairing budget has to survive. Their pool
        # prices are still read on-chain, so the "watch for a perp listing" line
        # on the board keeps working; only the OnchainOS quote probe is skipped.
        quotable = self.quotable_tokens()
        result: dict[tuple[str, str], tuple] = {}
        self.quote_rate_limited = 0
        started = now_ms()
        if not enabled or not quotable:
            for token in quotable:
                for size in sizes:
                    result[(token["asset"], str(size))] = (None, None)
            self.last_quote_batch_ms = 0
            return result, []
        stop = threading.Event()

        def probe_quote(token: dict, side: str, amount):
            """One read-only quote, with a bounded backoff on transient errors.

            Quotes are reads, so a retry is allowed here (writes never are).
            Two failures are transient and worth another attempt: the venue's
            "Rate limited — retry with backoff" answer to a burst, and the CLI's
            "Network unavailable" when several probes share one local proxy.
            `auth_error` is NOT retried — that stops the whole grid instead,
            because hammering a channel that is rejecting us is not a fix.
            """
            attempts = 1 + max(0, int(self.settings.get("quote_retries", 2)))
            base = float(self.settings.get("quote_backoff_ms", 600)) / 1000
            record = None
            for attempt in range(attempts):
                record = self.quotes.quote(token, quote_token, side, amount)
                if record.status != "api_error":
                    return record
                self.quote_api_error_retries += 1
                if _is_rate_limit(getattr(record, "error", "")):
                    self.quote_rate_limited += 1
                if attempt + 1 < attempts:
                    time.sleep(base * (2 ** attempt) * (0.5 + random.random()))
            return record

        def probe(token: dict, size) -> tuple:
            if stop.is_set():
                return token, size, None, None
            # Exact-out: the quantity that comes back is the number of shares
            # the hedge has to match, so it has to be whole.
            buy = probe_exact_out(token, size)
            if buy.status == "auth_error":
                stop.set()
                return token, size, buy, None
            quantity = buy.token_quantity
            if not quantity or stop.is_set():
                return token, size, buy, None
            sell = probe_quote(token, "sell", quantity)
            if sell.status == "auth_error":
                stop.set()
            return token, size, buy, sell

        def probe_exact_out(token: dict, shares):
            attempts = 1 + max(0, int(self.settings.get("quote_retries", 2)))
            base = float(self.settings.get("quote_backoff_ms", 600)) / 1000
            record = None
            for attempt in range(attempts):
                record = self.quotes.quote_exact_out(token, quote_token, shares)
                if record.status != "api_error":
                    return record
                self.quote_api_error_retries += 1
                if _is_rate_limit(getattr(record, "error", "")):
                    self.quote_rate_limited += 1
                if attempt + 1 < attempts:
                    time.sleep(base * (2 ** attempt) * (0.5 + random.random()))
            return record

        jobs = [(token, size) for token in quotable for size in sizes]
        with ThreadPoolExecutor(max_workers=self.quote_workers) as pool:
            for token, size, buy, sell in pool.map(lambda job: probe(*job), jobs):
                result[(token["asset"], str(size))] = (buy, sell)
        self.last_quote_batch_ms = now_ms() - started
        records = []
        for token in quotable:
            for size in sizes:
                buy, sell = result.get((token["asset"], str(size)), (None, None))
                records.extend(record for record in (buy, sell) if record is not None)
        return result, records

    def quotable_tokens(self) -> list[dict]:
        """Tokens worth a spot quote probe: those with a perpetual to pair.

        Deliberately keyed on "does any contract exist for this asset", not on
        "is the mapping verified": an unverified mapping is a paperwork gap that
        a quote probe may still help close, while a missing contract is a
        structural fact. The set is recomputed every cycle because discovery
        hot-adds venues and instruments while the monitor runs.

        If discovery produced nothing at all, every token is treated as quotable.
        An empty contract list means "we could not read any exchange catalogue",
        not "no perpetual exists" — and silently dropping every spot quote on a
        transient venue outage would turn a data gap into a monitoring outage.
        """
        assets = {contract.asset for contract in self.contracts}
        if not assets:
            return self._token_snapshot()
        return [token for token in self._token_snapshot() if token["asset"] in assets]

    def refresh_okx_price_screen(self) -> dict:
        """Price every tracked OKX stock perp from one public tickers call.

        Failure here is a missing screen, not a failed cycle: the exact-in
        grid and the execution path do not read this result.
        """
        client = self.clients.get("okx")
        if client is None:
            screen = {"error": "okx_not_in_venues", "rows": [],
                      "okx_contracts": 0, "okx_priced": 0,
                      "spot_priced": 0, "both_legs": 0}
            self.price_screen = screen
            return screen
        try:
            raw = client.exchange.publicGetMarketTickers({"instType": "SWAP"})
            received = now_ms()
            data = raw.get("data") if isinstance(raw, dict) else None
        except Exception as exc:  # noqa: BLE001 - a display read must not stop the cycle
            screen = {"error": type(exc).__name__ + ":" + str(exc)[:180],
                      "rows": [], "okx_contracts": 0, "okx_priced": 0,
                      "spot_priced": 0, "both_legs": 0}
            self.price_screen = screen
            return screen
        screen = okx_price_screen(
            self.contracts, data or [], self._screen_pool_states(), received)
        self.price_screen = screen
        return screen

    def _screen_pool_states(self) -> dict:
        """Pool rows for the board: spot ask from the pool we would buy.

        A name whose pool has not been read has no ask, and the price table
        leaves it out. The ask is the buy side of one depth-ladder notional,
        not the pool mid.
        """
        states = {str(asset): dict(row) for asset, row in self.pool_state_by_asset.items()}
        latest: dict[str, dict] = {}
        for pool in self.onchain_pools or []:
            asset = str(pool.get("asset") or "")
            if not asset or pool.get("price_usd") is None:
                continue
            prev = latest.get(asset)
            ts = int(pool.get("recv_ts_ms") or pool.get("block_ts_ms") or 0)
            if prev is None or ts >= int(prev.get("recv_ts_ms") or prev.get("block_ts_ms") or 0):
                latest[asset] = pool
        sizes = self.settings.get("depth_sizes") or ["10"]
        size = decimal(sizes[0])
        for asset, pool in latest.items():
            state = states.setdefault(asset, {})
            try:
                ask = pool_side_prices(pool, size).get("ask_usd")
            except Exception:
                ask = None
            if ask is not None:
                state["spot_ask_usd"] = str(ask)
            if state.get("slot0_price_usd") in (None, ""):
                state["slot0_price_usd"] = str(pool["price_usd"])
                state.setdefault("state", "pool_read_stale")
        return states

    def promoted_quote_assets(self) -> list[str]:
        """Names that earn an exact-in probe because the screen shows edge."""
        allow = (self.config.get("execution_policy") or {}).get("allowlist") or {}
        return promoted_exact_assets(
            self.price_screen, self._token_snapshot(), allow, self.quote_exact_assets)

    def refresh_stablecoin_pegs(self, now: int) -> dict:
        """Read OKX spot mids for the stablecoins non-HK pools pay in.

        A failed or wide read leaves that token without a conversion, which
        the existing gate treats as "do not size". Nothing here writes 1.0.
        """
        client = self.clients.get("okx")
        fetch = getattr(getattr(client, "exchange", None), "publicGetMarketTicker", None)
        observations: dict = {}
        if fetch is None:
            self.stablecoin_pegs = {"status": "no_ticker_api"}
            return self.stablecoin_pegs
        wanted: dict[str, str] = {}
        for token in self.tokens:
            if token_listing_market(token) == HK_MARKET:
                continue
            pools = token.get("pools") or []
            if not pools:
                continue
            symbol = str(pools[0].get("quote_symbol") or "").upper()
            address = str(pools[0].get("quote_address") or "").lower()
            if symbol and address:
                wanted.setdefault(symbol, address)
        for symbol, address in sorted(wanted.items()):
            try:
                raw = fetch({"instId": f"{symbol}-USDT"})
                row = ((raw or {}).get("data") or [None])[0] or {}
                bid = Decimal(str(row.get("bidPx") or "0"))
                ask = Decimal(str(row.get("askPx") or "0"))
            except Exception as exc:  # noqa: BLE001 - a missing peg is a refusal
                observations[symbol] = {"ok": False, "error": type(exc).__name__}
                continue
            peg = stablecoin_mid(bid, ask)
            if peg is None:
                observations[symbol] = {"ok": False, "error": "bad_book"}
                continue
            mid, deviation = peg
            observations[symbol] = {
                "ok": deviation <= DEFAULT_MAX_PEG_DEVIATION_BPS,
                "quote_to_usdt": str(mid),
                "peg_deviation_bps": str(deviation),
                "quote_address": address,
            }
            if deviation > DEFAULT_MAX_PEG_DEVIATION_BPS:
                observations[symbol]["error"] = "peg_deviation"
                continue
            for token in self.tokens:
                if token_listing_market(token) == HK_MARKET:
                    continue
                pools = token.get("pools") or []
                if not pools:
                    continue
                if str(pools[0].get("quote_symbol") or "").upper() != symbol:
                    continue
                if str(pools[0].get("quote_address") or "").lower() != address:
                    token.pop("quote_conversion", None)
                    continue
                token["quote_conversion"] = {
                    "quote_symbol": symbol,
                    "quote_address": address,
                    "quote_to_usdt": str(mid),
                    "source": f"okx {symbol}-USDT bid/ask mid",
                    "observed_ts_ms": now,
                    "valid_until_ms": now + DEFAULT_MAX_QUOTE_CONVERSION_AGE_MS,
                    "peg_deviation_bps": str(deviation),
                    "verified": True,
                }
        self.stablecoin_pegs = observations
        return observations

    def quote_grid_tokens(self) -> list[dict]:
        """The assets *this cycle's* spot quote grid is actually issued for.

        ``quotable_tokens`` answers "what could ever pair" and is what the board
        reports coverage against the universe of. This answers "what are we
        spending this round's proxy budget on", and the two differ by exactly one
        thing: the rotating slice.

        Every quotable asset is issued every cycle *except* those belonging to a
        market listed in ``quote_slice_markets``. Those rotate — the cursor
        advances by one slice per call, so a market of N names is fully covered
        every ``ceil(N / slice)`` cycles and never adds more than ``slice`` names
        to any single round.

        The cursor is only advanced for markets that actually have something to
        quote, so a cycle with no US candidates does not silently skip a slice.
        The decision is reported back in ``quote_slice_report`` for the run
        summary: "which US names were priced this round" has to be answerable
        from the record, not inferred from a missing row.
        """
        quotable = self.quotable_tokens()
        report: dict = {"markets": sorted(self.quote_slice_markets),
                        "assets_per_cycle": self.quote_slice_assets_per_cycle,
                        "cursor": self.quote_slice_cursor, "sliced": {},
                        "deferred": {}, "rotating_pool": {}}
        report["pool_quotes"] = bool(self.pool_quotes)
        if self.quote_exact_assets and not self.pool_quotes:
            # OnchainOS fallback only. Pool quotes price every reviewed name
            # through QuoterV2, so this cap does not apply then.
            promoted = self.promoted_quote_assets()
            report["exact_in_only"] = sorted(self.quote_exact_assets)
            report["promoted_exact"] = promoted
            wanted = self.quote_exact_assets | set(promoted)
            quotable = [token for token in quotable
                        if str(token["asset"]) in wanted]
        if not self.quote_slice_markets or self.quote_slice_assets_per_cycle <= 0:
            # No slice: every quotable asset is on this cycle's grid, and the
            # board has to be able to say so without re-deriving the rule.
            report["issued"] = sorted(str(t["asset"]) for t in quotable)
            self.quote_slice_report = report
            return quotable
        kept, rotating = [], {}
        for token in quotable:
            market = token_listing_market(token)
            # Only machine-discovered names rotate. A hand-written block is one
            # somebody reviewed and walked through `mapping-verify`, so it stays
            # on the every-cycle grid; the slice is for the hundred names that
            # arrived from the issuer list and have no reviewed evidence yet.
            if market in self.quote_slice_markets and token.get("auto_discovered"):
                rotating.setdefault(market, []).append(token)
            else:
                kept.append(token)
        cursor = self.quote_slice_cursor
        offset = 0
        for market, tokens in sorted(rotating.items()):
            # The slice advances by its own width, not by one: consecutive rounds
            # are disjoint, so N names are covered in ceil(N/slice) rounds with
            # no name quoted twice before every other one has been.
            size = min(self.quote_slice_assets_per_cycle, len(tokens))
            start = (cursor + offset) % len(tokens)
            chosen = [tokens[(start + i) % len(tokens)] for i in range(size)]
            kept.extend(chosen)
            offset += size
            report["sliced"][market] = sorted(str(t["asset"]) for t in chosen)
            report["deferred"][market] = max(0, len(tokens) - len(chosen))
            report["rotating_pool"][market] = len(tokens)
        for market in sorted(self.quote_slice_markets):
            # A rotating market with an empty rotating pool is *not* a market
            # with holes: every one of its names is hand-reviewed and therefore
            # on the grid every cycle. Recorded as 0 rather than omitted so the
            # board can tell "nothing to rotate" from "we forgot to report".
            report["rotating_pool"].setdefault(market, 0)
        self.quote_slice_cursor = cursor + offset
        report["cursor_next"] = self.quote_slice_cursor
        # What was actually put on the wire this cycle. `quote_universe_assets`
        # is everything that *could* be quoted, so it cannot answer "was this
        # name priced this round" — this list can.
        report["issued"] = sorted(str(t["asset"]) for t in kept)
        self.quote_slice_report = report
        return kept

    def cycle(self, *, quote_enabled: bool = True, reference_file: str | None = None) -> dict:
        run = uuid.uuid4().hex
        # Per-cycle error-dedupe state: see `_log_source_error`.
        self._source_error_written = {}
        # Stage-0 instrumentation. `_phase` stamps one span; the names are the
        # only way to attribute a long cycle to a *phase* instead of to "the
        # cycle". Measured 2026-09-21: one cycle's summary was 1 322 s after the
        # previous one with no attributable phase, because only the end time was
        # recorded.
        cycle_started = time.monotonic()
        self.last_cycle_started_ms = self.cycle_started_ms
        self.cycle_started_ms = now_ms()
        phases: dict[str, int] = {}

        def _phase(name: str, since: float) -> None:
            phases[name] = int((time.monotonic() - since) * 1000)

        _t = time.monotonic()
        if now_ms() - self.last_discovery >= int(self.settings["metadata_refresh_seconds"]) * 1000:
            self.discover(run)
        _phase("discover_ms", _t)
        _t = time.monotonic()
        if now_ms() - self.last_reference >= int(self.settings["reference_refresh_seconds"]) * 1000:
            # Starts or collects a round; only the first one after a start runs
            # on this thread (see `_reference_round`).
            self._reference_round(run)
        _phase("reference_ms", _t)
        _t = time.monotonic()
        # 24/7 leg: refreshed on its own clock, independent of cash sessions.
        if (self.onchain_enabled and now_ms() - self.last_onchain
                >= int(self.settings["onchain_refresh_seconds"]) * 1000):
            self.refresh_onchain(run)
        _phase("onchain_ms", _t)
        _t = time.monotonic()
        # New issuer listings are only useful once they have a market; scan on a
        # slower clock and hot-add anything that does. A finished background scan
        # is collected every cycle — the 30-minute clock only decides when a new
        # one starts, so the report appears one cycle after the scan lands rather
        # than one interval after the *next* due time.
        token_scan = {"scanned": False, "reason": "not_due"}
        if self.onchain_enabled and self.token_scan_seconds > 0:
            token_scan = self._token_scan(
                run, now_ms(),
                due=now_ms() - self.last_token_scan >= self.token_scan_seconds * 1000)
        _phase("token_scan_ms", _t)
        _t = time.monotonic()
        # Two different kinds of error share one list, and they must be written
        # to the event log differently:
        #
        #   * "persistent" — discovery and reference failures. These are cached
        #     state that `cycle` re-reports unchanged on every cycle until the
        #     next refresh, so a row per cycle is a duplicate, not an event.
        #   * "observed" — this cycle's own reads (perp books, the pool state
        #     and trade scans, the batch deadline, token-scan errors). Each one
        #     really did happen now, so each one is written every time.
        #
        # The summary carries both every cycle either way; only the event table
        # is deduplicated, and only for the persistent half.
        persistent_errors = [*self.discovery_errors, *self.reference_errors]
        persistent_count = len(persistent_errors)
        errors = [*persistent_errors, *self.onchain_errors]
        errors.extend(token_scan.get("errors") or [])
        books, rates, quote_records = [], {}, []
        # Gas sponsorship is re-checked on its own slow clock; the boards read
        # the last observation from the event log.
        self._gas_watch(run, now_ms())
        _phase("gas_watch_ms", _t)
        _t = time.monotonic()
        quote_pairs = {}
        # Which venues are sampled *this* cycle. The alternating venues are
        # dropped from both the fetch and the evaluation, so a skipped cycle
        # leaves no half-filled rows behind.
        sampled = set(self.clients)
        skipped: set[str] = set()
        if self.alternate_every > 1 and self.alternate_venues:
            if self.cycle_index % self.alternate_every:
                skipped = self.alternate_venues & sampled
                sampled -= skipped
        self.cycle_index += 1
        # Streaming fetch-and-evaluate. Every leg is its own job and every pair
        # is judged the moment its own book and its own buy/sell quotes are both
        # in hand — never at "the end of the cycle", which is simply the moment
        # the slowest probe of the whole grid happens to return.
        token_map = {t["asset"]: t for t in self._token_snapshot()}
        self.refresh_stablecoin_pegs(now_ms())
        limits, costs = limits_from_config(self.config), costs_from_config(self.config)
        # Per-asset/venue round-trip costs, with the reviewed global value as the
        # fallback: a mapping block that carries its own `cost` table wins.
        cost_table = cost_table_from_tokens(costs, list(token_map.values()))
        target = self.config.get("scenario", {}).get("target_exit_basis_bps")
        strategy_settings = self.config.get("strategy") or {}
        context = {"token_map": token_map, "limits": limits, "costs": costs,
                   "cost_table": cost_table,
                   "wallet_balance_usdt": self.wallet_balance_usdt(now_ms()),
                   "target": decimal(target) if target is not None else None}
        # `quote_grid_tokens` is the bounded, rotation-aware view of
        # `quotable_tokens`; the universe stays available for the report.
        # Books are fetched for that same set, so a quoted name can be judged
        # in the same cycle.
        quotable = self.quote_grid_tokens()
        perp_jobs = self._perp_jobs(
            sampled, {str(token["asset"]) for token in quotable} if self.pool_quotes else None)
        pairs_by_key = {c.key: c for c in self.contracts
                        if c.venue in sampled and c.asset in token_map}
        quote_jobs, probe_sizes = self._probe_plan(quotable, pairs_by_key, token_map)
        quoted_assets = {str(token.get("asset")) for token in quotable}
        for contract in pairs_by_key.values():
            if contract.asset not in quoted_assets or probe_sizes.get(contract.key):
                continue
            if not self.settings.get("probe_from_contract_minimum"):
                continue
            errors.append({
                "asset": contract.asset, "instrument": contract.key,
                "kind": "probe", "error": "contract_minimum_unknown",
            })
        self._quote_stop = threading.Event()
        self._quote_lock = threading.Lock()
        records = []
        quote_started = now_ms()
        quote_last_ms = quote_started
        quote_first_success_ms: int | None = None
        quote_last_success_ms: int | None = None
        quote_asset_metrics: list[dict] = []
        evaluations, signals = [], 0
        funding_venues: set = set()
        books_by_key: dict = {}
        # A pair waits for exactly two things: its book, and its asset's quotes.
        waiting: dict = {}
        for contract in pairs_by_key.values():
            for size in probe_sizes.get(contract.key) or []:
                # Explicit "arrived" flags, because `None` alone cannot tell "this
                # leg failed" from "this leg has not answered yet" — and a pair
                # whose book read failed must still be recorded as a resolved
                # observation, not silently dropped (a bug the first version of
                # this had, caught by tests/test_hk_monitor.py).
                waiting[(contract.key, str(size))] = {
                    "book": None, "book_done": False,
                    "quotes": None, "quotes_done": False,
                }
        done_books: set = set()
        done_quotes: set = set()

        def ready(key, size, now):
            # `signals += 1` assigns the name. Without nonlocal, the first
            # intent crashes the cycle with UnboundLocalError and the restart
            # loop never reaches the order. Seen live 2026-09-24 on CRWV.
            nonlocal signals
            slot = waiting.get((key, str(size)))
            if slot is None or not slot["book_done"] or not slot["quotes_done"]:
                return None
            del waiting[(key, str(size))]
            contract = pairs_by_key[key]
            book, (buy, sell) = slot["book"], slot["quotes"]
            # The slot key is the size we asked for. A reverted ceiling probe
            # comes back at the contract minimum, and that filled quantity is
            # the one the hedge has to match.
            probed = decimal(size)
            if buy is not None and buy.token_quantity:
                probed = decimal(buy.token_quantity)
            evaluation = self._evaluate_pair(run, contract, probed, buy, sell,
                                             book, now, context)
            if evaluation.pop("_intent", False):
                signals += 1
            evaluations.append(evaluation)
            return evaluation

        def evaluate_ready(key=None, asset=None, now=None):
            now = now or now_ms()
            for (pair_key, size) in list(waiting):
                contract = pairs_by_key[pair_key]
                if key is not None and pair_key != key:
                    continue
                if asset is not None and contract.asset != asset:
                    continue
                ready(pair_key, size, now)

        pool = ThreadPoolExecutor(max_workers=self.perp_workers + self.quote_workers,
                                  thread_name_prefix="market-cycle")
        # Quoting and reference-refreshing share one proxy, and a quote grid
        # that is waiting on bandwidth is a quote grid that goes stale. The
        # reference worker is told not to start a round inside this window; it
        # is evidence, so the next cycle is soon enough.
        self._quote_window.set()
        try:
            # Quote jobs go in first: they are the leg whose age decides whether
            # a pair is trustworthy, and the slowest single unit of work here.
            quote_futures = [pool.submit(self._fetch_quote, token, size, fallback)
                             for token, size, fallback in quote_jobs]
            perp_futures = [pool.submit(self._fetch_perp, contract, kind)
                            for contract, kind in perp_jobs]
            budget_seconds = float(self.settings.get("market_batch_budget_seconds", 30))
            try:
                completed = as_completed(quote_futures + perp_futures,
                                         timeout=max(1.0, budget_seconds))
                for future in completed:
                    if future in quote_futures:
                        token, size, buy, sell, recs, quote_metric = future.result()
                        records.extend(recs)
                        quote_asset_metrics.append(quote_metric)
                        quote_pairs[(token["asset"], str(size))] = (buy, sell)
                        done_quotes.add((token["asset"], str(size)))
                        now = now_ms()
                        quote_last_ms = now
                        # "First success" and "last success" are different
                        # questions: the first bounds how stale the *best* pair
                        # looked, the last is what the whole grid cost. Reporting
                        # only the batch total conflated the two.
                        if any(record.status == "ok" for record in recs):
                            if quote_first_success_ms is None:
                                quote_first_success_ms = now - quote_started
                            quote_last_success_ms = now - quote_started
                        for contract in pairs_by_key.values():
                            if contract.asset != token["asset"]:
                                continue
                            slot = waiting.get((contract.key, str(size)))
                            if slot is None:
                                # The cash-ceiling probe reverted and the quote
                                # that came back is the contract minimum. The
                                # slot is still keyed by the size we asked for.
                                for planned in probe_sizes.get(contract.key) or []:
                                    slot = waiting.get((contract.key, str(planned)))
                                    if slot is not None and not slot["quotes_done"]:
                                        break
                            if slot is not None:
                                slot["quotes"] = (buy, sell)
                                slot["quotes_done"] = True
                        evaluate_ready(asset=token["asset"], now=now)
                        continue
                    kind, contract, item, error = future.result()
                    if error is not None:
                        errors.append(error)
                        if kind == "depth":
                            # A failed read is still an answer: it resolves the pair
                            # as "no book", which is a different fact from "not
                            # sampled this cycle" and must be recorded as such.
                            books_by_key[contract.key] = None
                            done_books.add(contract.key)
                            for size in probe_sizes.get(contract.key) or []:
                                slot = waiting.get((contract.key, str(size)))
                                if slot is not None:
                                    slot["book"] = None
                                    slot["book_done"] = True
                            evaluate_ready(key=contract.key)
                        continue
                    if kind == "depth":
                        books.append(item)
                        books_by_key[contract.key] = item
                        done_books.add(contract.key)
                        for size in probe_sizes.get(contract.key) or []:
                            slot = waiting.get((contract.key, str(size)))
                            if slot is not None:
                                slot["book"] = item
                                slot["book_done"] = True
                        evaluate_ready(key=contract.key)
                    else:
                        rates[contract.key] = item
                        # Only a *successful* read moves the clock; a failure is
                        # retried next cycle rather than remembered as "read".
                        self.funding_read_cycle[contract.key] = self.cycle_index
                        funding_venues.add(contract.venue)
            except TimeoutError:
                errors.append({"kind": "market_batch",
                               "error": f"market_batch_deadline_exceeded:{budget_seconds:g}s"})
        finally:
            # Do not wait for a slow network worker.  It is bounded to this
            # cycle's read-only work and its result is discarded if it arrives
            # after the deadline.
            pool.shutdown(wait=False, cancel_futures=True)
            self._quote_window.clear()
        _phase("market_batch_ms", _t)
        _t = time.monotonic()
        quote_records = records
        # Span from the first quote job to the last one: in the streaming shape
        # there is no single "batch" any more, and this is the number that still
        # bounds how old the earliest quote was by the time its pair was judged.
        self.last_quote_batch_ms = max(0, quote_last_ms - quote_started)
        # Quote jobs that never answered inside the market-batch budget. Their
        # assets are recorded as not sampled — never back-filled from an earlier
        # cycle — so this is a coverage number, not an age.
        quote_deadline_exceeded = len(quote_jobs) - len(done_quotes)
        self.quote_metrics = {
            "quote_probe_jobs": len(quote_jobs),
            "quote_probe_done": len(done_quotes),
            "quote_deadline_exceeded_count": max(0, quote_deadline_exceeded),
            "quote_first_success_ms": quote_first_success_ms,
            "quote_last_success_ms": quote_last_success_ms,
            "quote_batch_elapsed_ms": self.last_quote_batch_ms,
            "quote_assets": sorted(quote_asset_metrics,
                                   key=lambda row: str(row.get("asset"))),
        }
        self.quote_concurrency = self._adapt_quote_workers()
        evaluated_at = now_ms()
        phase = self.calendar.phase(evaluated_at)
        for book in books:
            self.event(run, "perp_book", book, venue=book.venue, source=book.source,
                       recv_ts_ms=book.recv_ts_ms, venue_ts_ms=book.venue_ts_ms, sequence=book.sequence)
        for key, rate in rates.items():
            self.event(run, "funding", {"instrument": key, **rate}, source=rate["source"],
                       recv_ts_ms=rate["recv_ts_ms"], venue_ts_ms=rate["venue_ts_ms"])
        for quote in quote_records:
            self.event(run, "spot_quote", quote, asset=quote.asset, source=quote.source,
                       recv_ts_ms=quote.recv_ts_ms, status=quote.status,
                       venue_ts_ms=quote.venue_ts_ms, sequence=quote.sequence)
        # Persistent failures (discovery, reference) are state, not events: they
        # are re-reported unchanged every cycle until the next refresh. Write an
        # event when the state appears or changes, and stay quiet while it is the
        # same. Everything after `persistent_count` is this cycle's own
        # observation and is always written.
        seen: dict[str, str] = {}
        for error in errors[:persistent_count]:
            key, signature = _error_signature(error)
            seen[key] = signature
            # The asset/venue columns are carried from the error record itself:
            # reference failures are written here, by the cycle that adopts the
            # round, rather than by the worker thread that produced them.
            self._log_source_error(run, error,
                                   asset=str(error.get("asset") or ""),
                                   venue=str(error.get("venue") or ""))
        # Merge instead of replace, and forget keys that are no longer reported
        # — a failure that clears and returns has to be visible again.
        for key in [key for key in self._source_error_state if key not in seen]:
            self._source_error_state.pop(key, None)
        self._source_error_state.update(seen)
        for error in errors[persistent_count:]:
            self.event(run, "source_error", error, status="error")
        start_of_day = datetime.fromtimestamp(evaluated_at // 1000, HK).replace(
            hour=0, minute=0, second=0, microsecond=0)
        history = self.store.events("spot_quote", int(start_of_day.timestamp()) * 1000)
        grouped = {}
        for record in history:
            quote = quote_from_record(record)
            # Probe evidence must never mix token wrappers, quote contracts,
            # sources, or calendar dates.  This matters when US xStocks use a
            # wrapper route while HK names use a direct pool, and when a
            # long-running database contains records across a session change.
            key = (
                quote.asset, quote.side, str(quote.requested_amount),
                quote.quote_currency, quote.chain_id, quote.token_address,
                quote.quote_address, quote.source,
                datetime.fromtimestamp(
                    quote.request_ts_ms // 1000, HK
                ).date().isoformat(),
            )
            grouped.setdefault(key, []).append(quote)
        anchor_refs = load_anchor_references(reference_file)
        probes = []
        p = self.config["probe"]
        for key, samples in grouped.items():
            result = assess_probe(
                samples, self.calendar, set(p["special_route_ids"]), anchor_refs.get(key[0]),
                decimal(p["anchor_tolerance_bps"]), decimal(p["min_overnight_move_bps"]),
                int(p["min_observations"]),
            )
            probes.append({"asset": key[0], "side": key[1], "requested_amount": key[2], **result})
        for asset in token_map:
            if not any(p["asset"] == asset for p in probes):
                probes.append({
                    "asset": asset, "side": "buy/sell", "requested_amount": "not_sampled",
                    "channel": "unknown", "yesterday_close_anchor": "inconclusive",
                    "reason": "no_measurement_for_asset", "sample_count": 0,
                })
        instruments = []
        book_map = {(b.venue, b.symbol): b for b in books}
        for c in self.contracts:
            if c.asset not in token_map:
                continue
            b = book_map.get((c.venue, c.instrument_id))
            rate = rates.get(c.key, {})
            instruments.append({
                "asset": c.asset, "instrument": c.key, "active": c.active,
                "payout_model": c.payout_model, "multiplier": c.multiplier,
                "bid": b.bids[0].price if b and b.bids else None,
                "ask": b.asks[0].price if b and b.asks else None,
                "book_age_ms": b.age_at(evaluated_at) if b else None,
                "book_fresh": b.is_fresh(evaluated_at, limits.max_age_ms) if b else False,
                "funding_rate": rate.get("rate"), "next_funding_ts_ms": rate.get("next_funding_ts_ms"),
            })
        quote_status = (
            "disabled" if not quote_enabled else
            "auth_blocked" if any(q.status == "auth_error" for q in quote_records) else
            "quotes_observed" if any(q.status == "ok" for q in quote_records) else "no_successful_quotes"
        )
        token_references = self._token_reference_rows(instruments)
        # Only a positive net entry basis that clears the configured minimum is
        # a candidate. A negative net basis is not a weak signal - it is no
        # signal at all, because the two legs cannot be opened at a profit.
        candidates = []
        perp_style = ("maker" if str((self.config.get("execution_policy") or {})
                                     .get("orders", {}).get("style") or "")
                      == "maker_first" else "taker")
        for row in token_references:
            direction = (row.get("directions") or {}).get("long_spot_short_perp") or {}
            if not direction.get("is_candidate"):
                continue
            perp = direction.get("perp") or {}
            provenance = row.get("onchain_provenance") or {}
            roundtrip = candidate_roundtrip(
                row=row, direction=direction, costs=costs, target=target,
                min_edge_bps=decimal(self.settings["min_net_edge_bps"]),
                buffer_bps=decimal(strategy_settings.get("edge_buffer_bps", "0")),
                expected_profit_bps=decimal(strategy_settings.get("expected_profit_bps", "0")),
                perp_style=perp_style)
            candidates.append({
                "asset": row["asset"],
                "perp_venue": perp.get("venue"), "perp_instrument": perp.get("instrument"),
                "spot_ask_usd": row.get("onchain_spot_ask_usd"),
                "perp_bid_usd": direction.get("perp_price_usd"),
                "gross_entry_basis_bps": direction.get("gross_entry_basis_bps"),
                "estimated_entry_cost_bps": direction.get("estimated_entry_cost_bps"),
                "estimated_net_entry_bps": direction.get("estimated_net_entry_bps"),
                "spot_source": row.get("onchain_ask_source"),
                "pool_depth_usd": row.get("onchain_pool_depth_usd"),
                "pool_fee_bps": row.get("onchain_pool_fee_bps"),
                "size_plan": direction.get("size_plan") or [],
                "cost_verified": (direction.get("cost_breakdown") or {}).get("verified"),
                "cost_breakdown": direction.get("cost_breakdown") or {},
                "cost_evidence": self.config.get("costs", {}).get("evidence"),
                "mapping": row.get("mapping") or {},
                "mapping_verified": bool((row.get("mapping") or {}).get("verified")),
                "mapping_evidence": next(
                    (entry.get("evidence") for entry in
                     (row.get("mapping") or {}).get("entries") or [] if entry.get("verified")),
                    None),
                "roundtrip": roundtrip,
                # The on-chain leg is addressed by contract, never by ticker:
                # the alert has to name the exact token and quote token that
                # would be bought, plus the pool the price came from.
                "chain_id": provenance.get("chain_id"),
                "token_symbol": provenance.get("token_symbol"),
                "token_address": provenance.get("token_address"),
                "quote_token_symbol": row.get("onchain_pool_quote"),
                "quote_token_address": direction.get("spot_quote_address"),
                "pool_address": row.get("onchain_pool_id"),
                # Carried onto the candidate so the board and the unattended
                # executor judge the same fact: the token identity behind this
                # candidate was confirmed in this round, or it was not.
                "reference_stale": bool(row.get("reference_stale")),
                "reference_age_ms": int(row.get("reference_age_ms") or 0),
            })
        # The board row's spot ask is the slot0 ladder, and it is cleared when
        # that read is older than 120s. The exact quote that just cleared the
        # profit gate is a different number and is what an order may use.
        # Smallest spend first: the wallet, not the headline basis, is usually
        # what decides whether the open can finish.
        already = {row.get("asset") for row in candidates}
        cost_ok = bool(costs.verified)
        signaled = [row for row in evaluations
                    if row.get("status") == "conditional_signal"
                    and row.get("asset") not in already
                    and not row.get("wallet_limited")]
        signaled.sort(key=lambda row: decimal(row.get("spot_open_usdt") or "0"))
        for row in signaled:
            spec = row.get("contract_spec") or {}
            venue = str(spec.get("venue") or "")
            instrument = str(spec.get("instrument") or "")
            gross = row.get("entry_basis_bps")
            if not venue or not instrument or gross is None:
                continue
            candidates.append({
                "asset": row["asset"],
                "perp_venue": venue, "perp_instrument": instrument,
                "spot_ask_usd": None,
                "perp_bid_usd": None,
                "gross_entry_basis_bps": gross,
                "estimated_net_entry_bps": row.get("conditional_net_bps"),
                "spot_source": "quoter_v2_exact",
                "shares": row.get("probe_size"),
                "cost_verified": cost_ok,
                "mapping_verified": True,
                "reference_stale": False,
                "reference_age_ms": 0,
                "roundtrip": {
                    "complete": True,
                    "breakeven_gross_bps": row.get("break_even_exit_basis_bps") or "0",
                },
                "from_strategy_signal": True,
            })
        # Attach "why this one cannot be ordered" to every candidate, from the
        # same policy the risk gate reads. A candidate that clears the alert bar
        # but fails any of these is announced and *not* tradable, and the page
        # has to say which.
        policy = self.config.get("execution_policy") or {}
        for candidate in candidates:
            candidate["execution_blockers"] = execution_blockers(candidate, policy)
            candidate["executable"] = not candidate["execution_blockers"]
            # The alert gate adds this key when it builds a message; carrying it
            # on the candidate too means the page, the JSON and the message all
            # state the same threshold instead of one of them showing "—".
            candidate["min_net_edge_bps"] = str(
                decimal(self.settings["min_net_edge_bps"]))
        # Everything up to here is one cycle's read-and-evaluate work. The three
        # calls below are the ones that can wait on a *live order* (maker-first
        # opens rest up to 120 s, closes up to 600 s), which is why they get
        # their own phase: until now a long cycle could not be attributed to
        # "evaluating" or to "waiting for an order".
        _phase("postprocess_ms", _t)
        _t = time.monotonic()
        # Automated execution runs after the alert gate, on the same candidates
        # and the same bar: what is announced and what may be executed are
        # decided by one threshold, not two.
        if self.auto_executor.worker_enabled():
            # Stage 3: the cycle nominates and walks on. A maker-first order can
            # rest for 120 s (open) or 600 s (close) — that wait belongs to the
            # worker, not to the sampling loop, and it is the wait that used to
            # stop the summaries for minutes at a time.
            auto_execution = self.auto_executor.dispatch(
                candidates, evaluations, evaluated_at)
            execution_worker = auto_execution.pop("execution_worker", None) or {}
            funding_exit = _worker_exit_view(auto_execution, "funding_exit")
            basis_exit = _worker_exit_view(auto_execution, "basis_exit")
        else:
            execution_worker = {"alive": False, "dispatch": "inline",
                                "queue_depth": 0, "in_flight": None}
            auto_execution = self.auto_executor.maybe_execute(candidates, evaluated_at)
            # The funding-cost stop runs every cycle, independently of whether
            # there is a candidate: it protects a position that already exists,
            # and a quiet market is exactly when nobody would notice the funding
            # bill. It closes nothing unless the venue shows a short and the paid
            # funding has reached the operator's threshold.
            funding_exit = self.auto_executor.maybe_funding_exit(evaluated_at)
            # The operator's exit rule (2026-09-21): enter at ≥20 bps of basis,
            # close when it comes back to 3 bps. Evaluated on this cycle's own
            # evaluations, on the execution venue only, and inert unless
            # `[auto_close] enabled = true` in the execution policy.
            basis_exit = self.auto_executor.maybe_basis_exit(evaluations,
                                                             evaluated_at)
        _phase("execution_ms", _t)
        _t = time.monotonic()
        # The discovery funnel reported to the board: this cycle's scan when one
        # ran, otherwise the last completed one (carried verbatim, with its own
        # timestamp in `scan_age_ms`).
        scan_report = token_scan if token_scan.get("scanned") else (
            self.last_token_scan_report or {})
        # The time the funnel numbers were measured: this cycle's scan when one
        # ran, otherwise the older scan this report came from — never "now",
        # which would make a restored report look freshly measured. When there
        # is no report at all (a first run, or a store with no scan in it) the
        # age stays ``None``: "9 minutes" would be the age of the *process*,
        # and there is no measurement for it to describe.
        scan_report_at = (self.last_token_scan if token_scan.get("scanned")
                          else self.last_token_scan_report_ms)
        price_screen = self.refresh_okx_price_screen()
        summary = {
            "run_id": run, "evaluated_at_ms": evaluated_at,
            "hk_time": datetime.fromtimestamp(evaluated_at // 1000, HK).isoformat(),
            "direction": "long_spot_short_perp",
            "direction_note": ("买链上现货(Ask) + 空该交易所永续(Bid)；"
                               "净基差<0 一律不可执行，不计为信号"),
            "min_net_edge_bps": str(decimal(self.settings["min_net_edge_bps"])),
            "perp_style": perp_style,
            "candidate_count": len(candidates), "candidates": candidates,
            **self._dispatch_alerts(run, candidates, evaluated_at),
            "auto_execution": auto_execution,
            "funding_exit": funding_exit,
            "basis_exit": basis_exit,
            # Where the execution work is, without having to read the worker's
            # own snapshot out of `auto_execution`. In inline mode this simply
            # says so: there is no queue and no in-flight task to report.
            "execution_worker": execution_worker,
            "execution_in_flight": bool(execution_worker.get("in_flight")),
            "execution_queue_depth": int(execution_worker.get("queue_depth") or 0),
            "execution_last_result": execution_worker.get("last_result"),
            "execution_last_error": execution_worker.get("last_error"),
            # Which instruments the machine may act on, straight from the
            # execution policy — shown on the board so "monitored" and
            # "tradable" are never the same list by accident.
            "execution_universe": trade_universe(
                automation_path=str(self.config.get("execution_policy_path")
                                    or "config/automation_policy.toml")),
            "phase": phase,
            # This summary is the monitor's own mode, not the execution policy;
            # the latter is reported under auto_execution/execution_universe.
            # Keeping a truthful label matters now that live automation is
            # explicitly enabled: an old read-only string hid real orders.
            "mode": ("live_auto_execution" if self.auto_executor.order_mode == "live"
                      else "paper_auto_execution" if self.auto_executor.order_mode == "paper"
                      else "read_only"),
            "quote_status": quote_status, "actual_quote_requests": len(quote_records),
            # How long the concurrent quote grid took. It bounds the age of any
            # order book fetched alongside it, which is why a 15s pairing budget
            # cannot survive a 70s grid.
            "quote_batch_ms": self.last_quote_batch_ms,
            "quote_workers": self.quote_workers,
            "quote_rate_limited": self.quote_rate_limited,
            # Rate limits and unreachable-proxy retries used to share one
            # counter, which made a proxy outage look like a venue throttle.
            "quote_api_error_retries": self.quote_api_error_retries,
            **self.quote_metrics,
            # The per-asset budget, the priority order the grid was issued in,
            # the shared quote-channel health, and what the concurrency
            # controller decided — all four together explain a slow grid.
            "quote_asset_deadline_ms": self.quote_asset_deadline_ms,
            "quote_asset_deadlines_missed": sum(
                1 for row in (self.quote_metrics.get("quote_assets") or [])
                if row.get("deadline_exceeded")),
            "quote_priority_order": [
                {"asset": token["asset"], "priority": self._quote_priority(token)}
                for token in quotable],
            "quote_channel": self.quote_channel.snapshot(),
            "quote_concurrency": getattr(self, "quote_concurrency", None) or {
                "quote_workers": self.quote_workers,
                "floor": self.quote_workers_floor,
                "ceiling": self.quote_workers_ceiling,
                "decision": "hold", "reason": "not_evaluated_yet"},
            "quote_max_age_vs_budget_ms": [
                self.last_quote_batch_ms, limits.max_age_ms],
            "onchain_status": self._onchain_status(),
            "gas_sponsorship": self.gas_observation,
            "onchain_refresh_seconds": int(self.settings["onchain_refresh_seconds"]),
            "onchain_errors": self.onchain_errors,
            "onchain_pools_read": len(self.onchain_pools),
            "token_universe_scan": {
                "interval_seconds": self.token_scan_seconds,
                "last_scan_ms": self.last_token_scan or None,
                # The funnel below is the last *completed* scan, not "this
                # cycle's scan": the two clocks differ by 30x, and a monitor
                # that reported "—" for 29 cycles out of 30 would be hiding the
                # one number the operator asked for (how many of the 114 are
                # real). `scan_age_ms` says how old it is.
                "scan_age_ms": (None if not scan_report_at
                                else max(0, now_ms() - scan_report_at)),
                "scan_fresh": bool(token_scan.get("scanned")),
                "new_listings": scan_report.get("new_listings"),
                "added": [a["asset"] for a in scan_report.get("added") or []],
                "skipped_no_depth": [s["symbol"]
                                     for s in scan_report.get("skipped") or []],
                "discovered_total": len(self.discovered_tokens),
                # Which venues the scan may add, and what it skipped for that
                # reason, so "HK only" stays checkable from the summary.
                "scope_countries": scan_report.get("scope_countries")
                or sorted(self.token_scan_countries),
                # Which scanned markets may be adopted, and the three layers of
                # the discovery report. "Scanned but not adopted" is a
                # deliberate, visible state — not the same as "nothing there".
                "adopt_countries": scan_report.get("adopt_countries")
                or sorted(self.token_adopt_markets),
                # Two lists intersected before probing: how many OKX
                # tokenised-stock perps exist, how many scoped listings were
                # dropped for having none, and how many were left to probe.
                "perp_gate": scan_report.get("perp_gate"),
                "layers": scan_report.get("layers"),
                # What the last completed scan actually spent its probes on and
                # the rotation it will resume from. Without this, a rotation
                # stuck on the same 20 names after every restart is invisible in
                # the summary: `probes_pending_after_cap` reads 158 either way.
                "probed_assets": scan_report.get("probed_assets") or [],
                "probe_rotation": scan_report.get("probe_rotation"),
                "report_only": scan_report.get("report_only") or [],
                "report_only_count": len(scan_report.get("report_only") or []),
                "probes_pending_after_cap": scan_report.get("probes_pending_after_cap"),
                "skipped_out_of_scope": scan_report.get("skipped_out_of_scope"),
                "unlabeled_hk_shaped": scan_report.get("unlabeled_hk_shaped") or [],
            },
            "onchain_assets_with_prints": sorted(
                a for a, t in self.onchain_trades.items() if t.get("last_print")),
            "quote_errors": [
                {"asset": q.asset, "side": q.side, "status": q.status,
                 "error_code": q.error_code, "error": q.error}
                for q in quote_records if q.status != "ok"
            ],
            # The denominator used to count every monitored token, including the
            # ones with no perpetual to pair against (MIXUx, HKEXCx), so a grid
            # that probed every *quotable* asset perfectly still read "12/16".
            # Coverage is now stated against the grid that was actually issued;
            # the old number is kept alongside it so historical comparisons
            # stay honest instead of silently changing meaning.
            "quote_probe_coverage": (
                f"{len(quote_records)}/{len(quote_jobs) * 2}"),
            # The second denominator stays "every monitored token × the legacy
            # grid width × two legs". It is the historical series, not the
            # count of probes this cycle actually sent.
            "quote_probe_coverage_all_tokens": (
                f"{len(quote_records)}/"
                f"{len(self._token_snapshot()) * len(self.settings['probe_shares']) * 2}"),
            "quote_probe_expected": len(quote_jobs) * 2,
            "quote_probe_observed": len(quote_records),
            "quote_probe_ok": sum(1 for q in quote_records if q.status == "ok"),
            "quote_quotable_assets": sorted(str(t["asset"]) for t in quotable),
            # Which assets the *slice* put on the wire this cycle, and how many
            # of that market's names are waiting their turn. Without this a
            # rotating market would look like a market with holes in it.
            "quote_grid_slice": self.quote_slice_report,
            # One OKX tickers response plus the pool slot0 we already read.
            # This is the price of every OKX stock perp we track, including
            # names that are not on the OnchainOS exact-in grid. Slot0 is a
            # display price; it is not an execution quote.
            "price_screen": price_screen,
            "depth_watchlist": self._depth_watchlist_summary(),
            "quote_universe_assets": sorted(
                str(t["asset"]) for t in self.quotable_tokens()),
            "probe_shares": [str(size) for _token, size, _fallback in quote_jobs],
            "probe_from_contract_minimum": bool(
                self.settings.get("probe_from_contract_minimum")),
            "skipped_quote_reason": "auth_circuit_breaker" if quote_status == "auth_blocked" else None,
            "conditional_signals": signals, "instruments": instruments, "evaluations": evaluations,
            "probes": probes, "source_errors": errors,
            "token_references": token_references,
            # One verdict per asset: which of the five on-chain liquidity states
            # the X Layer side is in, and the reason. Slot0 and trade prints are
            # carried as evidence inside each row, never as an execution price.
            "onchain_pool_states": [
                self.pool_state_by_asset.get(row["asset"])
                for row in token_references
                if self.pool_state_by_asset.get(row["asset"])
            ],
            "reference_note": "X Layer pool reference vs USDT perp screening only; not an executable net spread",
            "poll_interval_seconds": self.settings["interval_seconds"],
            # Alternating-venue sampling is a deliberate gap in coverage, so the
            # summary says which venues were read this cycle instead of leaving
            # their absence to be inferred from missing rows.
            "sampled_venues": sorted(sampled),
            "skipped_venues": sorted(skipped),
            "alternate_every_cycles": self.alternate_every,
            # The perp fetch shape, reported so a slow cycle can be read back
            # without guessing which knob was in force.
            "perp_workers": self.perp_workers,
            "funding_refresh_cycles": self.funding_refresh_cycles,
            "perp_jobs": len(perp_jobs),
            "funding_venues_read": sorted(funding_venues),
            # Lighter's WebSocket: the venue clock that decides whether its books
            # are usable at all. Reported every cycle so "no lighter rows" can be
            # read back as "the socket is down" instead of "the venue is quiet".
            "lighter_stream": (self.lighter_stream.health()
                               if self.lighter_stream is not None
                               else {"enabled": False}),
            "max_age_ms": limits.max_age_ms,
            # Stage-0 instrumentation. `cycle_work_ms` is how long this cycle's
            # own work took; `cycle_gap_ms` is the wall-clock distance from the
            # previous cycle's start, which is the number the sampling cadence
            # actually means. `phase_ms` says which part of the work it was, so
            # a future 1 322 s gap is attributable instead of mysterious.
            "cycle_started_ms": self.cycle_started_ms,
            "cycle_work_ms": int((time.monotonic() - cycle_started) * 1000),
            "cycle_gap_ms": (self.cycle_started_ms - self.last_cycle_started_ms
                             if self.last_cycle_started_ms else None),
            "phase_ms": dict(phases),
            # Per-host reference health: state, last error, and when the next
            # attempt is allowed. Read-only evidence for the boards.
            "reference_hosts": self._reference_host_health(),
            "reference_tiers": {
                "execution_required": ["token_reference"],
                "screening_required": ["pool_reference"],
                "display_only": ["stock_daily_reference", "fx_daily_reference"],
            },
            # The reference worker's own record: whether a round is in flight,
            # what the last one managed, and how much of the table is being kept
            # from the previous round because this one could not re-read it.
            "reference_worker": self._reference_worker_state(),
        }
        self.event(run, "run_summary", summary, source="arb_monitor", recv_ts_ms=evaluated_at,
                   status="degraded" if quote_status != "quotes_observed" or errors else "observing")
        return summary

    def prune_if_due(self) -> None:
        """Start one retention pass if the last one is finished and due.

        The sampling loop calls this after a cycle. It never waits: a pass that
        is still deleting keeps the write lock in its own connection, and the
        next cycle only checks the thread. ``[retention] enabled = false``
        leaves the file alone.
        """
        section = self.config.get("retention") or {}
        if not section.get("enabled", True):
            return
        running = self._retention_thread
        if running is not None and running.is_alive():
            return
        now = now_ms()
        interval_ms = int(float(section.get("interval_seconds", 3600)) * 1000)
        if self._retention_last_ms and now - self._retention_last_ms < interval_ms:
            return
        self._retention_last_ms = now
        self._retention_thread = threading.Thread(
            target=self._retention_job, name="monitor-retention", daemon=True)
        self._retention_thread.start()

    def _retention_job(self) -> None:
        from arb_monitor.ops.retention import apply, connect, policy_from_document
        policy = policy_from_document(self.config)
        window = (f"{policy.raw_hours:g} 小时" if policy.raw_hours is not None
                  else "配置的保留期")
        print(f"retention: 开始裁剪早于 {window} 的原始行"
              f"（信号行留下）", flush=True)
        conn = connect(self.store.path)
        try:
            def _progress(entry: dict) -> None:
                deleted = entry["deleted"]["rows"]
                kept = (entry.get("signals_kept") or {}).get("rows") or 0
                if deleted or kept:
                    print(f"retention: {entry['kind']} 删 {deleted} 行"
                          f"（信号留下 {kept}）", flush=True)
            report = apply(conn, policy, progress=_progress)
        except Exception as exc:  # noqa: BLE001 - a failed prune must not kill sampling
            print(f"retention failed: {type(exc).__name__}: {exc}", flush=True)
            return
        finally:
            conn.close()
        rows = sum(entry["deleted"]["rows"] for entry in report)
        nbytes = sum(entry["deleted"]["bytes"] for entry in report)
        print(f"retention: 删除 {rows} 行 / {nbytes / 1048576:.1f} MB"
              f"（早于 {window} 且不是信号的原始行）", flush=True)

    def close(self) -> None:
        self._closing = True
        # The read-only WebSocket goes first: it holds no orders, and leaving it
        # running while the store closes would only produce error rows.
        if self.lighter_stream is not None:
            self.lighter_stream.close()
        # Stop the execution worker last: it owns the venue calls, and it must
        # not still be mid-order when the monitor tears down its store.
        self.auto_executor.shutdown()
        self._scan_pool.shutdown(wait=False, cancel_futures=True)
        self._pool_pool.shutdown(wait=False, cancel_futures=True)
        self._token_pool.shutdown(wait=False, cancel_futures=True)
        self._gas_pool.shutdown(wait=False, cancel_futures=True)
        self._reference_pool.shutdown(wait=False, cancel_futures=True)
        for client in self.clients.values():
            client.exchange.session.close()
        self.store.close()

    def _onchain_status(self) -> str:
        if not self.onchain_enabled:
            return "disabled"
        if self.onchain_errors and not self.onchain_pools and not self.onchain_trades:
            return "unavailable"
        if self.onchain_errors:
            return "degraded"
        if any(t.get("last_print") for t in self.onchain_trades.values()):
            return "pools_and_trade_prints"
        return "pools_only"

    def _read_wallet_balance(self, runner, now: int) -> dict:
        """Spendable balance of the configured payment token, or a reason why not.

        One CLI read per gas-watch interval on the existing single worker: the
        wallet is already being read there, so this adds no parallel traffic.
        Every failure path yields ``status="unknown"`` — an unread wallet must
        never look like an empty or a funded one.
        """
        quote = str((self.config.get("quote_token") or {}).get("address") or "").lower()
        if runner is None or not quote:
            return {"status": "unknown", "observed_ts_ms": now,
                    "reason": "no_wallet_runner" if runner is None else "no_quote_token"}
        try:
            from arb_monitor.execution.wallet_cli import wallet_balances
            rows = wallet_balances(runner, "xlayer")
        except Exception as exc:  # noqa: BLE001 - unread, not fatal
            return {"status": "unknown", "observed_ts_ms": now,
                    "reason": f"wallet_balance_unreadable:{type(exc).__name__}"}
        for row in rows or []:
            if str(row.get("token_address") or "").lower() == quote:
                return {"status": "ok",
                        "balance_usdt": str(decimal(row["balance"])),
                        "token_address": quote,
                        "symbol": row.get("symbol"),
                        "source": "onchainos wallet balances",
                        "observed_ts_ms": now}
        return {"status": "unknown", "observed_ts_ms": now,
                "reason": "quote_token_not_in_wallet", "token_address": quote}

    def wallet_balance_usdt(self, now: int) -> Decimal | None:
        """The last good balance, or ``None`` (unknown is not a pass)."""
        observation = self.wallet_balance_observation or {}
        if observation.get("status") != "ok" or observation.get("balance_usdt") is None:
            return None
        interval = int((self.config.get("gas_watch") or {}).get("check_seconds", 900))
        observed = int(observation.get("observed_ts_ms") or 0)
        # Two intervals: one missed round is tolerable, a stale balance is not.
        if observed and now - observed > 2 * max(60, interval) * 1000:
            return None
        return decimal(observation["balance_usdt"])

    def min_pool_depth_usd(self) -> Decimal:
        return decimal(str(self.onchain_settings.get("min_pool_depth_usd", "1000")))

    def _pool_max_age_ms(self) -> int:
        """How old a pool read may be before it stops counting for this cycle."""
        return int(self.onchain_settings.get(
            "pool_state_max_age_ms",
            max(120_000, int(self.onchain_settings.get("refresh_seconds", 60)) * 2_000)))

    def pool_state(self, asset: str, token: dict, pools: list[dict],
                   trades: dict, now: int,
                   unsampled: list[dict] | None = None) -> dict:
        """Which liquidity state the on-chain leg is in, and why.

        The five states and the single executable one live in
        ``core.liquidity_state``. This method only does the plumbing: pick the
        deepest configured pool *including* the ones below the depth floor (a
        dust pool is the reason there is no price), attach the exact-in quote
        this process last observed, and attach the quote-token conversion.

        Two things must never be read as an execution price, and both are
        recorded here as evidence rather than folded away: ``slot0`` (the price
        the pool was initialised at) and a historical trade print.

        ``unsampled`` is the "we have not looked this cycle" case, and it is
        deliberately *not* ``no_pool``. The on-chain refresh is asynchronous
        (measured 2026-09-22: a full read lands every ~3 minutes) while a pool
        read stops counting after ``pool_state_max_age_ms`` (120 s), so several
        cycles in a row have no fresh row for an asset that unambiguously has a
        pool. Reporting those as ``no_pool`` would say "there is nothing there"
        when the truth is "we have not sampled yet" — the exact conflation
        this project keeps having to undo. The caller passes the last known rows
        (possibly ``[]``) only when the token *does* have configured pools;
        ``None`` keeps every other path behaviourally identical.
        """
        def depth_of(row: dict) -> Decimal:
            return row.get("depth_usd") or ZERO

        if unsampled is not None and not pools:
            last = max((p for p in unsampled if isinstance(p, dict)),
                       key=lambda p: int(p.get("recv_ts_ms") or p.get("block_ts_ms") or 0),
                       default=None)
            age_ms = (now - int(last.get("recv_ts_ms") or last.get("block_ts_ms") or 0)
                      if last else None)
            return {
                "asset": asset, "state": POOL_READ_STALE, "executable": False,
                "reasons": ["pool_read_not_sampled_this_cycle"],
                "detail": (f"configured pools exist but no read inside "
                           f"pool_state_max_age_ms={self._pool_max_age_ms()}ms"
                           + (f"; last read {age_ms}ms ago" if age_ms is not None
                              else "; no read yet")),
                "pool_address": (last or {}).get("address"),
                "slot0_price_usd": None if last is None else last.get("price_usd"),
                "liquidity": None if last is None else last.get("liquidity"),
                "depth_usd": None, "min_depth_usd": str(self.min_pool_depth_usd()),
                "last_print_ms": None, "last_exact_in_quote_ms": None,
                "quote_conversion_ok": False, "quote_conversion_reason": "",
            }
        pool = max(pools, key=depth_of) if pools else None
        error = str((pool or {}).get("error") or "")
        liquidity = (pool or {}).get("liquidity")
        depth_usd = (pool or {}).get("depth_usd")
        if error == "pool_not_initialized":
            # Initialised but unfunded is "empty", not "unreadable": the pool
            # exists on chain, it just has nothing in it.
            error, liquidity, depth_usd = "", ZERO, ZERO
        market = token_listing_market(token)
        pool_quote = str((pool or {}).get("quote_address") or "").lower()
        if market == HK_MARKET:
            # Hong Kong rows carry their reviewed rate inside the per-contract
            # mapping, so the gate stays exactly where it was.
            specs = [s for s in (token.get("mappings") or {}).values()
                     if isinstance(s, dict)]
            conversion_ok = any("quote_to_usdt" in spec for spec in specs)
            conversion_reason = "" if conversion_ok else "no_reviewed_quote_to_usdt"
        else:
            conversion = quote_conversion_from_token(token)
            if conversion is None:
                conversion_ok, conversion_reason = False, "quote_conversion_missing"
            elif not conversion.quote_address or not pool_quote:
                # Either the observation does not say which stablecoin it was
                # read for, or the pool row does not say what it pays in. Both
                # mean the rate cannot be tied to *this* pool, and "assume they
                # are the same" is exactly the assumption this field exists to
                # remove. Refuse.
                conversion_ok, conversion_reason = False, "quote_conversion_wrong_token"
            elif conversion.quote_address != pool_quote:
                # The observation is for a different stablecoin than the pool
                # actually pays in, so it says nothing about this pool.
                conversion_ok, conversion_reason = False, "quote_conversion_wrong_token"
            else:
                conversion_ok, conversion_reason = conversion.usable(
                    now, max_age_ms=DEFAULT_MAX_QUOTE_CONVERSION_AGE_MS,
                    max_peg_deviation_bps=DEFAULT_MAX_PEG_DEVIATION_BPS)
        quote = self.last_exact_in_quote.get(asset) or {}
        print_row = trades.get("last_print") or {}
        print_ts = 0
        if print_row:
            print_ts = max(0, int(trades.get("block_ts_ms") or 0)
                           - int(print_row.get("age_ms_estimate") or 0))
        verdict = classify_pool(
            PoolObservation(
                asset=asset,
                pool_address=str((pool or {}).get("address") or ""),
                liquidity=None if liquidity is None else Decimal(str(liquidity)),
                depth_usd=None if depth_usd is None else Decimal(str(depth_usd)),
                slot0_price_usd=(pool or {}).get("price_usd"),
                last_print_ts_ms=print_ts,
                exact_in_quote_ts_ms=int(quote.get("observed_ts_ms") or 0),
                exact_in_quote_usdt=quote.get("quote_usdt"),
                quote_conversion_ok=conversion_ok,
                quote_conversion_reason=conversion_reason,
                observed_ts_ms=int((pool or {}).get("recv_ts_ms") or 0),
                error=error,
            ),
            now_ms=now, min_depth_usd=self.min_pool_depth_usd(),
            max_quote_age_ms=int(self.settings["quote_ttl_ms"]))
        return {
            "asset": asset, "state": verdict.state,
            "executable": verdict.executable, "reasons": list(verdict.reasons),
            "detail": verdict.detail,
            "pool_address": (pool or {}).get("address"),
            "slot0_price_usd": None if (pool or {}).get("price_usd") is None
            else str(pool["price_usd"]),
            "liquidity": None if liquidity is None else str(liquidity),
            "depth_usd": None if depth_usd is None else str(depth_usd),
            "min_depth_usd": str(self.min_pool_depth_usd()),
            "last_print_ms": print_ts or None,
            "last_exact_in_quote_ms": int(quote.get("observed_ts_ms") or 0) or None,
            "quote_conversion_ok": conversion_ok,
            "quote_conversion_reason": conversion_reason,
        }

    def refresh_gas_watch(self, run: str, now: int) -> dict | None:
        """Re-read our own receipts: is X Layer still paying the gas?

        The cost model carries ``roundtrip_gas_budget_usdt = "0"`` because real
        receipts said so. When that stops being true the model understates every
        trade, so this runs on its own clock and the boards turn red on the
        first transaction that pays. A failed check is recorded as unknown — it
        is never reported as "still sponsored".
        """
        settings = self.config.get("gas_watch") or {}
        if not settings.get("enabled") or self.onchain is None:
            return None
        interval = int(settings.get("check_seconds", 900)) * 1000
        if self.last_gas_check and now - self.last_gas_check < interval:
            return self.gas_observation
        self.last_gas_check = now
        runner = self.wallet_runner
        try:
            from arb_monitor.execution.wallet_cli import OnchainOSRunner, wallet_history
            runner = self.wallet_runner or OnchainOSRunner(environ=_proxy_environment(self.proxy))
            self.wallet_runner = runner
            orders = wallet_history(runner, "xlayer", limit=int(settings.get("history_limit", 10)))
            hashes = [str(order.get("txHash")) for order in orders if order.get("txHash")]
            swap_hashes = self._swap_tx_hashes()
        except Exception as exc:  # noqa: BLE001 - a read failure is "unknown"
            observation = {"status": "unknown", "checked": 0, "paid_count": 0,
                           "note": f"wallet history unavailable: {type(exc).__name__}",
                           "samples": []}
        else:
            result = observe_gas(self.onchain.rpc, hashes,
                                 limit=int(settings.get("history_limit", 10)),
                                 swap_path_hashes=swap_hashes)
            observation = result.as_dict()
        self.gas_observation = observation
        # Same wallet, same runner, same clock: the spendable balance is what
        # usually decides "can this trade actually be done", so it is read here
        # rather than in the 60 s sampling loop. A failure is recorded as
        # unread, never as a pass.
        self.wallet_balance_observation = self._read_wallet_balance(runner, now)
        self.event(run, "gas_observation", observation, source="xlayer receipts",
                   recv_ts_ms=now,
                   status="paid" if observation["status"] == "paid" else "ok")
        # "Tell me when sponsorship stops" is a push, not a page refresh. Only
        # the actionable case is pushed — our own swap/approval paying gas —
        # because a fee on some unrelated transfer would otherwise be a false
        # alarm that trains the operator to ignore the channel. That state is
        # still shown on the boards in amber.
        if observation["status"] == "paid" and self.gas_alerted != "paid":
            self._alert_gas(observation, run)
            self.gas_alerted = "paid"
        elif observation["status"] == "sponsored":
            self.gas_alerted = None
        return observation

    def _alert_gas(self, observation: dict, run: str) -> None:
        severity = "红色" if observation["status"] == "paid" else "琥珀"
        text = "\n".join([
            "【成本告警】X Layer gas 赞助状态变化",
            f"状态: {observation['status']}（{severity}）",
            f"{observation.get('note') or ''}",
            "",
            "影响: 成本模型里的 roundtrip_gas_budget_usdt = 0 若不再成立，",
            "      每笔交易的净基差都会被高估，开仓判断需要重新校准。",
            "",
            "实测样本（我们自己的交易）:",
            *[f"  · {s['tx_hash'][:20]}… gas_used={s['gas_used']} "
              f"paid={s['paid_native']} swap路径={'是' if s.get('swap_path') else '否'}"
              for s in (observation.get("samples") or [])[:5]],
            f"核对: {observation.get('checked')} 笔 · 付费 {observation.get('paid_count')} 笔",
        ])
        try:
            # Operational room, not the opportunity room: this one means "the
            # cost model may be wrong", which is a different kind of attention.
            delivery = (self.alert_notifier or self.notifier).send(text)
        except Exception as exc:  # noqa: BLE001 - a broken channel must not stop sampling
            delivery = {"ok": False, "detail": type(exc).__name__}
        self.event(run, "gas_alert", {"observation": observation, "delivery": delivery},
                   source="arb_monitor", recv_ts_ms=now_ms())

    def _swap_tx_hashes(self) -> list[str]:
        """Transactions this project sent, from its own execution ledger."""
        path = Path(self.config.get("execution_ledger", "data/execution.db"))
        if not path.exists():
            return []
        try:
            from arb_monitor.execution.ledger import ExecutionLedger
            ledger = ExecutionLedger(path)
            try:
                return ledger.swap_tx_hashes()
            finally:
                ledger.close()
        except Exception:  # noqa: BLE001 - the watcher degrades to "unknown"
            return []

    def _dispatch_alerts(self, run: str, candidates: list[dict],
                         evaluated_at: int) -> dict:
        """Send Feishu alerts for gated candidates. Never fails the cycle.

        The dashboard's sound alert is client-side and uses the same summary
        fields, so the page and Feishu always agree on what counted.
        """
        if not self.notify_enabled or self.alert_gate is None:
            return {"notify_channel": "disabled", "alerts_sent": 0, "alerts": []}
        if not self.alerts_hydrated:
            # A restart must not re-announce what was already sent.
            history = self.store.events("alert", max(0, evaluated_at - 7 * 86400 * 1000))
            self.alert_gate.hydrate([
                {"key": row.get("alert_key"), "recv_ts_ms": row.get("sent_at_ms")}
                for row in history
                if (row.get("delivery") or {}).get("ok")
            ])
            self.alerts_hydrated = True
        pending = self.alert_gate.evaluate(candidates, evaluated_at)
        sent = []
        for alert in pending:
            text = format_alert(alert, evaluated_at)
            delivery = (self.notifier.send(text) if self.notifier
                        else {"ok": False, "channel": "disabled", "detail": "notifier_off"})
            record = {**alert, "delivery": delivery, "text": text,
                      "sent_at_ms": evaluated_at}
            sent.append(record)
            self.event(run, "alert", record, asset=alert.get("asset", ""),
                       source=f'feishu:{delivery.get("channel")}',
                       recv_ts_ms=evaluated_at,
                       status="sent" if delivery.get("ok") else "error")
            if delivery.get("ok"):
                self.alert_gate.mark_sent(alert, evaluated_at)
        return {
            "notify_channel": (self.notifier.channel if self.notifier else "disabled"),
            "alerts_sent": sum(1 for row in sent if row["delivery"].get("ok")),
            "alerts_failed": sum(1 for row in sent if not row["delivery"].get("ok")),
            "alerts": sent,
        }

    def refresh_token_universe(self, run: str, now: int) -> dict:
        """Re-scan the issuer's listing universe and hot-add tradable newcomers.

        A listing only enters price monitoring once it has a pool with real
        depth: a deployed token with no market would add noise, not information.
        Successful additions are written to the machine-managed config file (so
        a restart keeps them) and announced on the same Feishu channel.

        Three layers, and discovery only ever moves a listing between the first
        two. *Issuer universe* is everything the issuer lists in the scanned
        markets; *pairable candidates* are those with a pool that has depth and
        (when a ticker matches) a perpetual to hedge with; *executable* is the
        execution allowlist, which this method never writes. A market that is
        scanned but not adopted is reported in full and hot-added to nothing, so
        turning US scanning on cannot quietly add hundreds of US names to price
        monitoring before the operator has reviewed a single US mapping.
        """
        if not self.onchain_enabled or self.token_scan_seconds <= 0:
            return {"scanned": False, "reason": "disabled"}
        additions, seen, errors, report_only = [], [], [], []
        try:
            assets = fetch_issuer_assets(self.proxy)
        except Exception as exc:
            error = {"error": type(exc).__name__ + ":" + str(exc)[:140]}
            self.event(run, "source_error", {"kind": "token_universe", **error},
                       status="error")
            self.token_scan_errors = [error]
            return {"scanned": True, "new_listings": 0, "errors": [error]}
        fresh = new_tokens(assets, self.known_token_addresses)
        # Only the venues named in `token_scan_countries` are probed. The
        # market comes from the issuer's `underlying.listingCountry`; a listing
        # whose country is absent stays UNKNOWN and is excluded rather than
        # guessed, so a US name can never be announced as a Hong Kong one.
        scoped = [a for a in assets if listing_market(a) in self.token_scan_countries]
        out_of_scope: dict[str, int] = {}
        unlabeled_hk_shaped = []
        issuer_universe: dict[str, int] = {}
        for asset in assets:
            market = listing_market(asset)
            issuer_universe[market] = issuer_universe.get(market, 0) + 1
        for asset in assets:
            market = listing_market(asset)
            if market in self.token_scan_countries:
                continue
            out_of_scope[market] = out_of_scope.get(market, 0) + 1
            if market == "UNKNOWN" and hk_shaped_code(asset):
                unlabeled_hk_shaped.append(asset["symbol"])
        # Probe gate: two lists, intersected before a single pool is probed.
        #
        # The issuer lists 754 US tokens (2026-09-22) and OKX quotes 181
        # tokenised-stock perpetuals; only the 114 names that appear in *both*
        # can become a two-legged position, and a full sweep of the other 640
        # costs ~19 h of the same local proxy the 60 s sampling loop uses, to
        # produce rows that are unhedgeable by construction. So for the markets
        # named in ``token_scan_perp_gate_markets`` (default US) a listing has to
        # carry an OKX stock perp before it is probed at all. HK is left ungated
        # on purpose: its hedgeable set spans Gate/Bybit/Bitget as well, and
        # monitoring is allowed to be wider than any one venue.
        gate_markets = {str(m).upper() for m in
                        (self.onchain_settings.get("token_scan_perp_gate_markets")
                         or ["US"])}
        # ``perp_gate["probed"]`` counts what is *eligible* to be probed: the
        # listings inside the intersection. It is a ceiling for the whole
        # market, not this round's work — the probe cap below walks it 20 names
        # at a time. The per-round count is ``layers.probed_this_round``, and
        # the board prints both so "114" is never read as "114 names were
        # measured a minute ago".
        perp_gate: dict[str, object] = {"markets": sorted(gate_markets),
                                       "okx_stock_perps": 0, "gated_out": {},
                                       "probed": {}, "error": None}
        gated_out: dict[str, int] = {}
        if gate_markets & {listing_market(a) for a in scoped}:
            try:
                perps = fetch_okx_stock_perps(self.proxy)
            except Exception as exc:
                # An unreadable gate is not a licence to probe everything: an
                # unknown hedgeable set means "don't spend the sweep on names we
                # cannot currently qualify", and the miss is reported instead.
                perp_gate["error"] = type(exc).__name__ + ":" + str(exc)[:140]
                self.event(run, "source_error",
                           {"kind": "okx_stock_perps", "error": perp_gate["error"]},
                           status="error")
                perps = {}
            perp_gate["okx_stock_perps"] = len(perps)
            kept = []
            for asset in scoped:
                market = listing_market(asset)
                if market in gate_markets and okx_hedge_ticker(asset, perps) is None:
                    gated_out[market] = gated_out.get(market, 0) + 1
                    continue
                kept.append(asset)
            scoped = kept
            perp_gate["gated_out"] = gated_out
        for asset in scoped:
            market = listing_market(asset)
            perp_gate["probed"][market] = perp_gate["probed"].get(market, 0) + 1
        self.token_scan_round += 1
        # Probes are expensive, so a normal scan only looks at genuinely new
        # listings; a full round re-checks scoped names that had no pool yet.
        full_round = (self.last_token_scan == 0
                      or self.token_scan_round % self.token_scan_full_every == 0)
        def addresses_of(asset: dict) -> set[str]:
            return {a for a in (asset["native_address"], asset["wrapper_address"]) if a}

        def already_monitored(asset: dict) -> bool:
            return bool(addresses_of(asset) & self.monitored_addresses)

        # Everything the issuer lists right now is "known"; only later arrivals
        # are new listings.
        self.known_token_addresses |= {a for asset in assets for a in addresses_of(asset)}
        if full_round:
            to_probe = [a for a in scoped if not already_monitored(a)]
        else:
            to_probe = [a for a in scoped if a in fresh and not already_monitored(a)]
        # A full round over a market the issuer lists hundreds of names in would
        # otherwise issue every probe at once, on the same single local proxy the
        # 60 s sampling loop uses. The cap walks the list across rounds instead.
        probe_cap = max(1, int(self.onchain_settings.get("token_scan_max_probes_per_round", 40)))
        pending = len(to_probe)
        truncated = pending > probe_cap
        if truncated:
            # Least recently probed first, *not* an index into this list. The
            # list is rebuilt from the issuer's response on every scan, so an
            # index means "position in this round's answer", which a restart
            # resets and which a single adoption shifts by one for every later
            # name. The ledger is the durable half of the pair and is carried in
            # the `token_universe_scan` event. Ties (every name never probed,
            # plus anything the tests pin to one clock) break on the key so the
            # batch is deterministic rather than dependent on issuer order.
            to_probe = sorted(
                to_probe,
                key=lambda a: (self.token_probe_seen_ms.get(token_probe_key(a), 0),
                               token_probe_key(a)))[:probe_cap]
        cex_assets = {c.asset for c in self.contracts}
        min_depth = decimal(str(self.onchain_settings.get("min_pool_depth_usd", "1000")))
        probed_this_round: dict[str, int] = {}
        for asset in to_probe:
            probed_market = listing_market(asset)
            # Stamped before the probe, not after: a probe that fails, times out
            # or finds nothing is still work spent on that name, and the next
            # round must move on to a different name instead of retrying this
            # one for ever. "The last time we looked, there was nothing there"
            # is exactly what the ledger is for.
            self.token_probe_seen_ms[token_probe_key(asset)] = now
            probed_this_round[probed_market] = \
                probed_this_round.get(probed_market, 0) + 1
            brand_new = asset in fresh
            candidate_bases = [(k, a) for k, a in
                               (("wrapper", asset["wrapper_address"]),
                                ("native", asset["native_address"])) if a]
            if not brand_new:
                candidate_bases = [c for c in candidate_bases if c[0] == "wrapper"] or candidate_bases
            best = None
            pools_seen = 0
            for kind, base in candidate_bases:
                try:
                    pools = (discover_pools(self.onchain.rpc, self.onchain_settings["factory"], base)
                             if brand_new else
                             probe_pools_cheap(self.onchain.rpc,
                                               self.onchain_settings["factory"], base))
                except Exception as exc:
                    errors.append({"asset": asset["symbol"],
                                   "error": type(exc).__name__ + ":" + str(exc)[:120]})
                    continue
                pools_seen += len(pools)
                for pool in pools:
                    token = {
                        "asset": asset["symbol"], "symbol": asset["symbol"],
                        "address": base, "decimals": 18, "trade_scan": False,
                        "pools": [{**pool, "base_address": base, "base_decimals": 18,
                                   "quote_decimals": 6}],
                    }
                    try:
                        states = XLayerOnchainAdapter({}, [token], self.onchain.rpc).pool_states()
                    except Exception as exc:
                        errors.append({"asset": asset["symbol"],
                                       "error": type(exc).__name__ + ":" + str(exc)[:120]})
                        continue
                    for state in states:
                        depth = state.get("depth_usd") or decimal(0)
                        if state.get("error") or depth < min_depth:
                            continue
                        if best is None or depth > best[0]:
                            best = (depth, kind, base, pool, state)
            if best is None:
                seen.append({"symbol": asset["symbol"], "name": asset["name"],
                             "hk_code": asset["hk_code"], "market": listing_market(asset),
                             "status": ("no_pool" if pools_seen == 0
                                        else "no_pool_with_depth"),
                             "pools_seen": pools_seen})
                continue
            depth, kind, base, pool, state = best
            cex_asset = match_cex_asset(asset, cex_assets)
            market = listing_market(asset)
            if market not in self.token_adopt_markets:
                # Scanned, measurable, and deliberately not adopted: adopting a
                # whole market before its mappings are reviewed is how hundreds
                # of unhedgeable rows end up looking monitored. The candidate is
                # reported with everything needed to review it; nothing on disk
                # changes.
                report_only.append({
                    "symbol": asset["symbol"], "name": asset["name"],
                    "hk_code": asset["hk_code"], "market": market,
                    "status": "market_scanned_not_adopted",
                    "depth_usd": str(depth), "pool": pool["address"],
                    "pool_price_usd": (None if state.get("price_usd") is None
                                       else str(state.get("price_usd"))),
                    "quote_symbol": pool["quote_symbol"],
                    "cex_match": cex_asset or "",
                    "has_perp": bool(cex_asset),
                    "reasons": [] if cex_asset else ["no_perp"]})
                continue
            entry = discovered_token_entry(
                asset, kind, base, pool, depth, cex_asset, now,
                trade_scan=(self.adopt_trade_scan_markets is None
                            or market in self.adopt_trade_scan_markets))
            with self._token_state_lock:
                self.tokens.append(entry)
                if entry["asset"] not in self.settings["assets"]:
                    self.settings["assets"].append(entry["asset"])
                # The adapter keeps its own filtered token list.  Updating only
                # ``self.tokens`` would make the new asset appear in CEX/quote
                # tables but never in X Layer pool pricing: a discovery that
                # silently cannot become a tradable observation.
                if self.onchain is not None:
                    self.onchain.tokens = [
                        token for token in self.tokens
                        if token.get("onchain_scan", True)
                    ]
            self.known_token_addresses |= {a for a in
                                           (asset["native_address"], asset["wrapper_address"]) if a}
            self.monitored_addresses |= addresses_of(asset) | {pool["address"].lower()}
            additions.append({**entry, "pool_price_usd": str(state.get("price_usd")),
                              "depth_usd": str(depth), "cex_match": cex_asset or ""})
            self.event(run, "token_discovered", additions[-1], asset=entry["asset"],
                       source="issuer asset list + X Layer factory", recv_ts_ms=now,
                       sequence=state.get("block_number"))
        if additions:
            self.discovered_tokens.extend(additions)
            self._persist_discovered()
            self._announce_discoveries(additions, now, run)
        self.last_token_scan = now
        self.token_scan_errors = errors
        pairable: dict[str, int] = {}
        for row in [*additions, *report_only]:
            market = listing_market(row)
            pairable[market] = pairable.get(market, 0) + 1
        executable: dict[str, int] = {}
        for row in self.execution_allowlist_assets():
            market = token_listing_market(row) if isinstance(row, dict) else ""
            executable[market] = executable.get(market, 0) + 1
        # Probed and found nothing to price yet, per market. This is the number
        # that keeps "114 candidates" honest: the intersection is a *ceiling*,
        # and a name with no pool at depth is not a monitored name. Counted from
        # this round's probes only, so it reads as "the last time we looked at
        # this name, there was still nothing there".
        no_pool: dict[str, int] = {}
        for row in seen:
            market = str(row.get("market") or "UNKNOWN")
            no_pool[market] = no_pool.get(market, 0) + 1
        # The rotation ledger travels *inside* this event, which is the same
        # event `_restore_last_scan_report` reads at startup — so the scan that
        # finishes 30 minutes after a restart resumes from the probes the
        # previous process already paid for. Pruned to this round's scoped
        # universe so months of delisted names cannot grow the payload without
        # bound; a name that comes back is simply treated as "never probed",
        # which errs toward looking at it, not toward skipping it.
        scoped_keys = {token_probe_key(a) for a in scoped}
        probe_seen_ms = {key: value for key, value in self.token_probe_seen_ms.items()
                         if key in scoped_keys}
        self.token_probe_seen_ms = probe_seen_ms
        never_probed = sum(1 for key in scoped_keys if key not in probe_seen_ms)
        report = {"scanned": True, "issuer_assets": len(assets), "new_listings": len(fresh),
                  "added": additions, "skipped": seen, "errors": errors,
                  "scope_countries": sorted(self.token_scan_countries),
                  "adopt_countries": sorted(self.token_adopt_markets),
                  "perp_gate": perp_gate,
                  "skipped_out_of_scope": out_of_scope,
                  "unlabeled_hk_shaped": unlabeled_hk_shaped,
                  "report_only": report_only,
                  "probes_pending_after_cap": max(0, pending - len(to_probe)),
                  # Which names this round actually spent a probe on, and the
                  # state the next round rotates from. `probes_pending_after_cap`
                  # alone cannot show a rotation that is stuck: a restart used to
                  # replay the same 20 names and still report pending 158.
                  "probed_assets": [str(a.get("symbol") or "") for a in to_probe],
                  "probe_rotation": {
                      "order": "least_recently_probed",
                      "cap": probe_cap,
                      "pending": pending,
                      "truncated": truncated,
                      "scoped": len(scoped_keys),
                      "never_probed": never_probed,
                      "ledger": len(probe_seen_ms),
                  },
                  # Durable half of the rotation. Restored at startup from this
                  # very event; not repeated in `run_summary` (60 s cadence, and
                  # the ledger is ~200 entries) — the summary carries
                  # `probed_assets` + `probe_rotation` instead.
                  "probe_seen_ms": probe_seen_ms,
                  # The three layers, as counts per market: what the issuer
                  # lists, what is really pairable, and what the execution
                  # allowlist already covers. Only the middle column is
                  # discovery's work.
                  "layers": {"issuer_universe": issuer_universe,
                             # Dropped before any probe: no OKX stock perp, so
                             # the name can never become a two-legged position.
                             "gated_out": dict(perp_gate.get("gated_out") or {}),
                             # In the intersection and probed this round.
                             "probed": dict(perp_gate.get("probed") or {}),
                             "pairable_candidates": pairable,
                             "no_pool": no_pool,
                             # What this round actually spent probes on, after
                             # the per-round cap rotated the list. `probed`
                             # above is the whole intersection (a ceiling);
                             # this is the work done in the last 30 min.
                             "probed_this_round": probed_this_round,
                             "executable_allowlist": executable}}
        # Persist the funnel under its own event kind, with the time it was
        # measured. The board reads `run_summary`, which is written every 60 s,
        # while this report is measured every 30 min -- so the first summary
        # after a restart is exactly the one that has no scan in it, and the
        # page showed "—" for the numbers the operator asked for (754 -> 114 ->
        # how many are real) until the next scan landed. A dedicated kind lets
        # `_restore_last_scan_report` put the last measurement back at startup
        # and keep `scan_age_ms` honest about how old it is.
        self.event(run, "token_universe_scan", {**report, "scanned_at_ms": now},
                   recv_ts_ms=now, status="ok")
        return report

    def execution_allowlist_assets(self) -> list[dict]:
        """Token blocks named in the execution allowlist — read-only.

        This is layer three of the discovery report. It is *read* from the
        execution policy so the number on the board is the number the executor
        would use; discovery never adds to it.
        """
        policy = self.config.get("execution_policy") or {}
        names = {str(asset) for asset in
                 ((policy.get("allowlist") or {}).get("assets") or [])}
        if not names:
            return []
        by_asset: dict[str, dict] = {}
        for token in self.config.get("tokens") or []:
            asset = str(token.get("asset") or "")
            if asset in names and asset not in by_asset:
                by_asset[asset] = token
        return [by_asset[name] for name in sorted(by_asset)]

    def _persist_discovered(self) -> None:
        if not self.discovered_config:
            return
        path = Path(self.discovered_config)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[3] / self.discovered_config
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_discovered_toml(self.discovered_tokens))

    def _announce_discoveries(self, additions: list[dict], now: int, run: str) -> None:
        if self.notifier is None:
            return
        text = format_discovery_message(additions, sorted(self.token_scan_countries))
        delivery = self.notifier.send(text)
        self.event(run, "alert", {"kind": "token_discovered", "delivery": delivery,
                                  "assets": [e["asset"] for e in additions],
                                  "markets": sorted({listing_market(e) for e in additions}),
                                  "sent_at_ms": now},
                   source=f'feishu:{delivery.get("channel")}', recv_ts_ms=now,
                   status="sent" if delivery.get("ok") else "error")

    def _token_reference_rows(self, instruments: list[dict]) -> list[dict]:
        """One row per asset: on-chain spot sides vs every perp quote.

        Each direction of the pair uses the side it would really trade on:
        buying the token pays the on-chain ask, shorting the perp sells at a
        named exchange's bid; the mirror direction sells the token at the
        on-chain bid and buys the perp at a named exchange's ask. A basis is
        only reported when both sides exist. Nothing here is executable: the
        perp venue is always named so the number can be checked.
        """
        min_depth = decimal(str(self.onchain_settings.get("min_pool_depth_usd", "1000")))
        # The depth ladder is pool-derived, so it is independent of how many
        # exact-in quote probes this cycle can afford.
        sizes = sorted(decimal(x) for x in self.settings.get(
            "depth_sizes") or ["100"])
        probe_size = sizes[0]
        min_edge_bps = decimal(self.settings["min_net_edge_bps"])
        costs = costs_from_config(self.config)
        gas_share = (costs.roundtrip_gas_budget_usdt / 2
                     if costs.roundtrip_gas_budget_usdt is not None else None)
        pools_by_asset: dict[str, list[dict]] = {}
        # Everything we have read for an asset, and the subset still inside the
        # freshness window. The split exists so a cycle that has not re-read yet
        # can be reported as "not sampled", not as "no pool": the on-chain
        # refresh is asynchronous and lands every few minutes, while the window
        # is two. Collapsing the two would print "无池" on a name that has one.
        pools_known: dict[str, list[dict]] = {}
        pool_cutoff = now_ms() - self._pool_max_age_ms()
        for pool in self.onchain_pools:
            asset_key = pool.get("asset", "")
            pools_known.setdefault(asset_key, []).append(pool)
            if int(pool.get("recv_ts_ms") or pool.get("block_ts_ms") or 0) < pool_cutoff:
                continue
            pools_by_asset.setdefault(asset_key, []).append(pool)
        token_map = {token["asset"]: token for token in self._token_snapshot()}
        rows = []
        for asset, reference in sorted(self.reference_cache.items()):
            token = token_map[asset]
            onchain_pools = pools_by_asset.get(asset, [])
            best_pool = best_pool_reference(onchain_pools, min_depth)
            trades = self.onchain_trades.get(asset, {})
            gt_pool = self.pool_cache.get(asset, {})
            gt_price = gt_pool.get("pool_price_usd")
            gt_reserve = gt_pool.get("pool_reserve_usd")
            if gt_price is not None and (gt_reserve is None or gt_reserve < min_depth):
                # Below the floor the "price" comes from a dust pool: keep the
                # number as evidence but never screen against it.
                gt_price = None
            # --- on-chain spot sides -------------------------------------
            spot_ask = spot_bid = None
            ask_source = bid_source = ""
            pool_sides = None
            known = pools_known.get(asset) or []
            shown = best_pool or (max(
                known, key=lambda pool: int(pool.get("recv_ts_ms")
                                            or pool.get("block_ts_ms") or 0))
                if known else None)
            display_sides = None
            if best_pool is not None:
                # slot0 prices the display ladder and the depth only. The ask
                # that can become a signal is the QuoterV2 fill below.
                pool_sides = pool_side_prices(best_pool, probe_size)
                display_sides = pool_sides
                quote = self.last_exact_in_quote.get(asset) or {}
                try:
                    qty = decimal(str(quote.get("token_quantity") or "0"))
                    paid = decimal(str(quote.get("quote_usdt") or "0"))
                    age = now_ms() - int(quote.get("observed_ts_ms") or 0)
                except Exception:
                    qty, paid, age = ZERO, ZERO, 10**12
                if qty > 0 and paid > 0 and 0 <= age <= self._pool_max_age_ms():
                    spot_ask = paid / qty
                    ask_source = "QuoterV2 精确买入价"
            elif shown is not None:
                # The last slot0 read is older than 120s. Keep it for the
                # depth column only. The price used as a signal is the
                # QuoterV2 fill from this cycle, when one exists.
                try:
                    display_sides = pool_side_prices(shown, probe_size)
                except Exception:
                    display_sides = None
                quote = self.last_exact_in_quote.get(asset) or {}
                try:
                    qty = decimal(str(quote.get("token_quantity") or "0"))
                    paid = decimal(str(quote.get("quote_usdt") or "0"))
                    age = now_ms() - int(quote.get("observed_ts_ms") or 0)
                except Exception:
                    qty, paid, age = ZERO, ZERO, 10**12
                if qty > 0 and paid > 0 and 0 <= age <= self._pool_max_age_ms():
                    spot_ask = paid / qty
                    ask_source = "QuoterV2 精确买入价"
            elif trades.get("onchain_bid_usd") or trades.get("onchain_ask_usd"):
                spot_ask, spot_bid = trades.get("onchain_ask_usd"), trades.get("onchain_bid_usd")
                window = int(trades.get("lookback_blocks") or 0)
                ask_source = bid_source = (
                    f"X Layer 真实成交打印（近 {window} 区块，"
                    f"{trades.get('print_count', 0)} 笔）")
            # Independent cross-check: real executed prints against the pool
            # price. Kept side by side rather than blended, so a divergence
            # between the pool state and what people actually paid stays
            # visible instead of being averaged away.
            print_mid = trades.get("vwap_usd") or trades.get("onchain_last_trade_price_usd")
            cross_check_bps = None
            if best_pool is not None and print_mid:
                cross_check_bps = ((Decimal(print_mid) - best_pool["price_usd"])
                                   / best_pool["price_usd"] * Decimal(10000))
            if best_pool is not None:
                screen_price, kind = best_pool["price_usd"], "xlayer_pool_depth"
            elif trades.get("last_print"):
                screen_price, kind = (trades["last_print"]["price_usd"], "xlayer_trade_print")
            elif gt_price is not None:
                screen_price, kind = gt_price, "geckoterminal_pool"
            else:
                screen_price, kind = reference["price_usd"], "okx_public_reference"
            independent = kind in {"xlayer_pool_depth", "xlayer_trade_print",
                                   "geckoterminal_pool"}
            if not independent:
                # Same-venue display price is not independent evidence.
                spot_ask = spot_bid = None
            # --- perp quotes, venue named ---------------------------------
            perp_quotes = []
            for instrument in instruments:
                if instrument["asset"] != asset or instrument["payout_model"] != "usd_linear":
                    continue
                venue, _, instrument_id = instrument["instrument"].partition(":")
                perp_quotes.append({
                    "venue": venue, "instrument": instrument_id,
                    "bid": instrument.get("bid"), "ask": instrument.get("ask"),
                    "funding_rate": instrument.get("funding_rate"),
                    "book_age_ms": instrument.get("book_age_ms"),
                    "fresh": bool(instrument.get("book_fresh")),
                    "payout_model": instrument["payout_model"],
                })
            # Only one direction is modelled: buy the token, short the perp.
            # The mirror trade would need to borrow or already hold the token,
            # and there is no borrow market for these instruments on X Layer,
            # so it is not a tradable pair and is not shown.
            perp_bid = perp_side(perp_quotes, "bid")
            direction = screen_direction(
                "long_spot_short_perp",
                None if spot_ask is None else {"price": spot_ask, "source": ask_source},
                perp_bid, costs.__dict__, gas_share, probe_size, min_edge_bps)
            direction["spot_side"] = "ask"
            direction["perp_side"] = "bid"
            direction["spot_bid_unused"] = spot_bid
            # Which contract actually pays for the token. Named explicitly so a
            # notification can be checked against the wallet before acting.
            direction["spot_quote_address"] = (
                best_pool.get("quote_address") if best_pool else trades.get("quote_address"))
            direction["spot_quote_symbol"] = (
                best_pool.get("quote_symbol") if best_pool else trades.get("quote_symbol"))
            # Per-size execution plan: the CJ handbook's rule is that a spread
            # must be quoted at the depth you can actually trade.
            if best_pool is not None:
                direction["size_plan"] = size_plan(
                    best_pool, sizes,
                    direction.get("perp_price_usd"), direction.get("estimated_entry_cost_bps") or ZERO,
                    min_edge_bps)
            if not independent:
                # A venue's own display reference is never independent evidence.
                direction.update({
                    "judgement": "display_price_only", "is_candidate": False,
                    "judgement_reason": "venue_display_price_not_independent",
                })
            directions = {"long_spot_short_perp": direction}
            onchain_state = self.pool_state(
                asset, token, onchain_pools, trades, now_ms(),
                # Configured pools exist but none is inside the freshness
                # window, so this cycle has nothing to judge. Hand the plumbing
                # what we do know (possibly an older read) so it can say
                # "not sampled" instead of the false "no pool".
                unsampled=(pools_known.get(asset)
                           if not onchain_pools and token.get("pools") else None))
            self.pool_state_by_asset[asset] = onchain_state
            row = {
                "asset": asset, "price_usd": reference["price_usd"],
                "recv_ts_ms": reference["recv_ts_ms"], "source": reference["source"],
                "kind": "reference_only_not_executable",
                # Which of the five on-chain liquidity states this row is in.
                # `kind` above already says the row is not executable; this says
                # *why* the on-chain side has or has not a price at all.
                "listing_market": token_listing_market(token),
                "underlying_symbol": str(token.get("stock_symbol")
                                         or token.get("hk_code") or ""),
                "token_address": str(token.get("address") or ""),
                "pool_state": onchain_state["state"],
                "pool_state_reasons": onchain_state["reasons"],
                "pool_state_detail": onchain_state["detail"],
                "pool_state_executable": onchain_state["executable"],
                # Whether this row rests on a token reference confirmed in this
                # round or on the last good one. `stale` is the execution_required
                # flag: `execution_blockers` refuses a candidate carrying it, and
                # the unattended executor refuses to sign one.
                "reference_stale": bool(reference.get("stale")),
                "reference_age_ms": int(reference.get("cache_age_ms") or 0),
                "min_pool_depth_usd": min_depth,
                "screen_price_usd": screen_price, "screen_price_kind": kind,
                "probe_size_usd": probe_size,
                "onchain_spot_ask_usd": spot_ask, "onchain_spot_bid_usd": spot_bid,
                "onchain_ask_source": ask_source, "onchain_bid_source": bid_source,
                "pool_sides": pool_sides,
                "onchain_pool_ask_usd": (display_sides or {}).get("ask_usd"),
                "onchain_pool_bid_usd": (display_sides or {}).get("bid_usd"),
                "onchain_pool_size_usd": probe_size,
                # Provenance of every on-chain number in this row.
                "onchain_provenance": {
                    "rpc_url": self.onchain_settings.get("rpc_url"),
                    "chain_id": self.onchain_settings.get("chain_id"),
                    "token_symbol": token["symbol"], "token_address": token["address"],
                    "token_decimals": token["decimals"],
                    "pool_address": shown["address"] if shown else None,
                    "pool_block_number": shown["block_number"] if shown else None,
                    "pool_block_ts_ms": shown["block_ts_ms"] if shown else None,
                    "pool_method": ("eth_call slot0/liquidity + balanceOf, "
                                    "V3 constant-liquidity swap math"
                                    if best_pool else None),
                    "print_method": ("eth_getLogs Transfer + eth_getTransactionReceipt, "
                                     "per-address net flow pairing"),
                    "trade_lookback_blocks": trades.get("lookback_blocks"),
                    "trade_head_block": trades.get("head_block"),
                    "gw_geckoterminal_pool_id": gt_pool.get("pool_id"),
                    "venue_display_reference": reference["source"],
                },
                "print_vs_pool_bps": cross_check_bps,
                "perp_quotes": perp_quotes,
                "mapping": mapping_status(token, now_ms()),
                "directions": directions,
                "geckoterminal_pool_price_usd": gt_price,
                "geckoterminal_pool_reserve_usd": gt_reserve,
                "geckoterminal_pool_id": gt_pool.get("pool_id"),
                "onchain_pool_count": len(onchain_pools),
                "onchain_pool_with_depth": best_pool is not None,
                "onchain_pool_price_usd": shown["price_usd"] if shown else None,
                "onchain_pool_depth_usd": shown["depth_usd"] if shown else None,
                "onchain_pool_id": shown["address"] if shown else None,
                "onchain_pool_quote": shown["quote_symbol"] if shown else None,
                "onchain_pool_fee_bps": (Decimal(shown["fee"]) / 100
                                         if shown else None),
                "onchain_pool_block": shown["block_number"] if shown else None,
                "onchain_pool_block_ts_ms": shown["block_ts_ms"] if shown else None,
                "onchain_max_pool_depth_usd": max(
                    (p.get("depth_usd") or Decimal(0) for p in onchain_pools), default=None),
                "onchain_transfers_seen": trades.get("transfers_seen"),
                "onchain_dust_prints_skipped": trades.get("dust_prints_skipped"),
                "onchain_lookback_blocks": trades.get("lookback_blocks"),
                "onchain_print_count": trades.get("print_count"),
                "onchain_trade_volume_usd": trades.get("volume_usd"),
                "onchain_bid_usd": trades.get("onchain_bid_usd"),
                "onchain_ask_usd": trades.get("onchain_ask_usd"),
                "onchain_spread_bps": trades.get("onchain_spread_bps"),
                "onchain_vwap_usd": trades.get("vwap_usd"),
                "onchain_last_trade_price_usd": (
                    trades["last_print"]["price_usd"] if trades.get("last_print") else None),
                "onchain_last_trade_age_ms": (
                    trades["last_print"].get("age_ms_estimate")
                    if trades.get("last_print") else None),
                "onchain_head_block": trades.get("head_block"),
            }
            bids = [x for x in instruments if x["asset"] == asset
                    and x["payout_model"] == "usd_linear" and x.get("bid") is not None]
            asks = [x for x in instruments if x["asset"] == asset
                    and x["payout_model"] == "usd_linear" and x.get("ask") is not None]
            row["best_perp_bid_usd"] = max((x["bid"] for x in bids), default=None)
            row["best_perp_ask_usd"] = min((x["ask"] for x in asks), default=None)
            long_short = direction
            row["screen_buy_spot_sell_perp_bps"] = long_short.get("gross_entry_basis_bps")
            row["screen_buy_spot_sell_perp_net_bps"] = long_short.get("estimated_net_entry_bps")
            if not independent:
                row["screen_bps_suppressed_reason"] = "venue_display_price_not_independent"
            rows.append(row)
        # A failed OKX token-search does not erase a pool we already read.
        # CRWV is the case: token() raised "identity absent/ambiguous", so it
        # never entered reference_cache, and the board showed OKX prices with
        # an empty spot. The row is display-only.
        seen = {row["asset"] for row in rows}
        for asset, known in sorted(pools_known.items()):
            if asset in seen or asset not in token_map:
                continue
            token = token_map[asset]
            shown = max(known, key=lambda pool: int(
                pool.get("recv_ts_ms") or pool.get("block_ts_ms") or 0))
            state = self.pool_state(
                asset, token, [], {}, now_ms(), unsampled=known)
            self.pool_state_by_asset[asset] = state
            try:
                sides = pool_side_prices(shown, probe_size)
            except Exception:
                sides = {}
            rows.append({
                "asset": asset,
                "listing_market": token_listing_market(token),
                "underlying_symbol": str(token.get("stock_symbol")
                                         or token.get("hk_code") or ""),
                "token_address": str(token.get("address") or ""),
                "pool_state": state["state"],
                "pool_state_reasons": state["reasons"],
                "pool_state_detail": state["detail"],
                "pool_state_executable": False,
                "onchain_pool_price_usd": shown.get("price_usd"),
                "onchain_pool_depth_usd": shown.get("depth_usd"),
                "onchain_pool_ask_usd": sides.get("ask_usd"),
                "onchain_pool_bid_usd": sides.get("bid_usd"),
                "onchain_pool_quote": shown.get("quote_symbol"),
                "onchain_pool_fee_bps": (Decimal(shown["fee"]) / 100
                                         if shown.get("fee") is not None else None),
                "onchain_provenance": {
                    "token_symbol": token.get("symbol"),
                    "token_address": token.get("address"),
                    "pool_address": shown.get("address"),
                    "pool_block_number": shown.get("block_number"),
                },
                "directions": {"long_spot_short_perp": {"is_candidate": False}},
                "mapping": mapping_status(token, now_ms()),
                "perp_quotes": [],
            })
        return rows


def age_summary(summary: dict, timestamp_ms: int) -> dict:
    """Re-age on EVERY read: a stored 'fresh' flag is not live freshness."""
    age = max(0, timestamp_ms - int(summary["evaluated_at_ms"]))
    rows = []
    for row in summary["instruments"]:
        capture_age = row.get("book_age_ms")
        current_age = int(capture_age) + age if capture_age is not None else None
        rows.append({
            **row, "book_fresh_at_capture": row["book_fresh"], "book_age_now_ms": current_age,
            "book_fresh": bool(row["book_fresh"] and current_age is not None
                               and current_age <= int(summary.get("max_age_ms", 15000))),
        })
    return {
        **summary, "snapshot_age_ms": age,
        "monitor_stale": age > int(summary["poll_interval_seconds"]) * 2000,
        "instruments": rows,
    }


def print_summary(summary: dict) -> None:
    summary = age_summary(summary, now_ms())
    live = "STALE_MONITOR" if summary["monitor_stale"] else "recent_snapshot"
    print(f'{summary["hk_time"]} | {summary["phase"]} | {live} | {summary["quote_status"]}')
    print("ASSET    INSTRUMENT                         BID       ASK       MODEL         FRESH")
    for row in summary["instruments"]:
        print(f'{row["asset"]:8} {row["instrument"]:34} {str(row["bid"]):9} {str(row["ask"]):9} '
              f'{row["payout_model"]:13} {row["book_fresh"]}')
    print(f'实际报价请求 {summary["quote_probe_coverage"]}; 条件信号 {summary["conditional_signals"]}; '
          f'数据源错误 {len(summary["source_errors"])}')
    print(f'链上（X Layer，24/7）：{summary.get("onchain_status")}；'
          f'读取池子 {summary.get("onchain_pools_read")} 个；'
          f'有成交打印的标 {",".join(summary.get("onchain_assets_with_prints") or []) or "无"}')
    print(f'双腿基差候选：{summary.get("candidate_count", 0)} 个'
          f'（仅净基差 ≥ {summary.get("min_net_edge_bps")} bps；净基差<0 一律不可执行）')
    print("现货多 + 永续空筛选（非执行净价差；永续腿标注交易所）:")
    for r in summary["token_references"]:
        direction = (r.get("directions") or {}).get("long_spot_short_perp", {})
        perp = direction.get("perp") or {}
        venue = f'{perp.get("venue")}:{perp.get("instrument")}' if perp else "无可用永续腿"
        verdict = {
            "candidate": "候选",
            "below_threshold": "未达阈值",
            "not_executable_negative_net": "不可执行(净<0)",
            "display_price_only": "仅展示价",
        }.get(direction.get("judgement"), "缺侧")
        print(f'  {r["asset"]}: 买现货 ask={r.get("onchain_spot_ask_usd")} '
              f'来源={r.get("screen_price_kind")} 深度={r.get("onchain_pool_depth_usd")} | '
              f'空 {venue} @ {direction.get("perp_price_usd")} '
              f'gross={_bps_text(direction.get("gross_entry_basis_bps"))}bps '
              f'cost={_bps_text(direction.get("estimated_entry_cost_bps"))}bps '
              f'net={_bps_text(direction.get("estimated_net_entry_bps"))}bps '
              f'→ {verdict}')
    for p in summary["probes"]:
        print(f'09:00 probe {p["asset"]}/{p["side"]}/{p["requested_amount"]}: '
              f'{p["channel"]}; anchor={p["yesterday_close_anchor"]}; {p["reason"]}')
    if not summary["probes"]:
        print("09:00 通道/昨收锚定：尚无测量证据。")


def in_preopen_capture_window(calendar: HKCalendar, timestamp_ms: int) -> bool:
    dt = datetime.fromtimestamp(timestamp_ms // 1000, HK)
    minute = dt.hour * 60 + dt.minute
    return calendar.is_session(dt.date()) is True and 535 <= minute < 576


def install_stop_signal() -> threading.Event:
    """Turn SIGTERM/SIGINT into a flag the sampling loop can see.

    Without this, `stop` killed the process outright and `close()` never ran, so
    nothing had a chance to record that an execution was in flight. The handler
    itself does no work: it only asks the loop to stop at the next boundary, and
    the normal `finally: monitor.close()` takes it from there. (The loop's own
    SIGINT-as-KeyboardInterrupt path is unchanged — this is the same idea with a
    handler that can also be reached while a cycle is sleeping.)
    """
    stopping = threading.Event()

    def handler(_signum, _frame):
        stopping.set()

    for name in ("SIGTERM", "SIGINT"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            signal.signal(number, handler)
        except (ValueError, OSError, RuntimeError):
            # Not the main thread, or a platform that will not allow it: the
            # old behaviour (KeyboardInterrupt / default termination) stands.
            pass
    return stopping


def add_arguments(subparsers) -> None:
    for name, help_text in (
        ("hk-list", "discover public HK-related stock perpetual candidates"),
        ("hk-discover", "re-scan the issuer listing universe and add tradable newcomers"),
        ("hk-monitor", "record read-only HK spot-long/perp-short observations"),
        ("hk-status", "show the latest stored snapshot and its age"),
        ("hk-dashboard", "serve the read-only local status board"),
        ("hk-depth-census", "one pass: which X Layer pools have depth, via RPC not OnchainOS"),
    ):
        parser = subparsers.add_parser(name, help=help_text)
        parser.add_argument("--config", default="config/hk_monitor.toml")
        parser.add_argument("--db")
        if name in {"hk-list", "hk-monitor", "hk-discover", "hk-depth-census"}:
            parser.add_argument("--proxy", default=os.environ.get("ARB_HTTP_PROXY"))
        if name in {"hk-list", "hk-status", "hk-discover"}:
            parser.add_argument("--json", action="store_true")
        if name == "hk-monitor":
            parser.add_argument("--cycles", type=int, default=1, help="0 = until Ctrl-C; default is one bounded sample")
            parser.add_argument("--interval", type=int)
            parser.add_argument("--no-quotes", action="store_true", help="explicitly disable OnchainOS probes")
            parser.add_argument("--anchor-reference", help="reviewed normalized close/overnight reference TOML")
            parser.add_argument("--preopen-window", action="store_true",
                                help="only sample known HK sessions from 08:55 until 09:36")
        if name == "hk-dashboard":
            parser.add_argument("--port", type=int, default=8765)


def run_command(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.command == "hk-depth-census":
        from arb_monitor.data.depth_census import run_depth_census_command
        return run_depth_census_command(config, proxy=getattr(args, "proxy", None))
    if args.command in {"hk-status", "hk-dashboard"}:
        path = args.db or config["monitor"]["db"]
        if not Path(path).exists():
            print("No observations yet. Run hk-monitor first.")
            return 1
        if args.command == "hk-dashboard":
            return serve_dashboard(path, args.port)
        store = EventStore(path)
        try:
            summary = store.latest_summary()
            if summary is None:
                print("No monitor snapshot yet.")
                return 1
            print(dumps(age_summary(summary, now_ms()))) if args.json else print_summary(summary)
            return 0
        finally:
            store.close()
    if args.command == "hk-monitor":
        if args.cycles < 0:
            raise ValueError("cycles must be >= 0")
        if args.interval is not None:
            if not 10 <= args.interval <= 3600:
                raise ValueError("interval must be 10–3600 seconds")
            config["monitor"]["interval_seconds"] = args.interval
        if args.preopen_window and not in_preopen_capture_window(
            HKCalendar.from_config(config["calendar"]), now_ms()
        ):
            print("Skipped: outside reviewed HK trading-day 08:55–09:36 capture window.")
            return 0
    monitor = HKMonitor(config, args.proxy, args.db)
    try:
        if args.command == "hk-list":
            errors = monitor.discover(uuid.uuid4().hex)
            if args.json:
                print(dumps({"candidates": monitor.contracts, "errors": errors}))
            else:
                print("Live public candidates (NOT a hedge-equivalence whitelist):")
                for c in monitor.contracts:
                    print(f"{c.asset:16} {c.key:36} {str(c.active):5} {c.payout_model:12} "
                          f"mult={c.multiplier} {c.category}")
                if errors:
                    print(dumps(errors))
            return 2 if errors else 0
        if args.command == "hk-discover":
            run = uuid.uuid4().hex
            monitor.discover(run)
            report = monitor.refresh_token_universe(run, now_ms())
            if args.json:
                print(dumps(report))
            else:
                print(f'发行方资产: {report.get("issuer_assets", "—")}；'
                      f'本次新增列表: {report.get("new_listings", "—")}；'
                      f'已加入价格监控: {len(report.get("added") or [])}')
                for entry in report.get("added") or []:
                    print(f'  + {entry["asset"]}（{entry["symbol"]}）池 {entry["pool"]} '
                          f'深度 {entry["depth_usd"]} USD 永续: {entry["cex_match"] or "未匹配"}')
                for skipped in report.get("skipped") or []:
                    print(f'  · {skipped["symbol"]}: {skipped["status"]}')
                for error in report.get("errors") or []:
                    print(f'  ! {error}')
            return 0
        count = 0
        # A stop signal ends the loop at a boundary, so the `finally` below can
        # run `close()` — which waits for an in-flight execution task and writes
        # it down. It never cancels an order.
        stopping = install_stop_signal()
        while (args.cycles == 0 or count < args.cycles) and not stopping.is_set():
            if args.preopen_window and not in_preopen_capture_window(monitor.calendar, now_ms()):
                break
            started = time.monotonic()
            summary = monitor.cycle(quote_enabled=not args.no_quotes, reference_file=args.anchor_reference)
            print_summary(summary)
            # After the cycle is stored, not during it. The first pass can
            # delete days of tape; it must not sit on the sampling thread.
            monitor.prune_if_due()
            count += 1
            if args.cycles and count >= args.cycles:
                break
            # Bounded, interruptible sleeps. Missed polls aren't invented.
            deadline = started + int(config["monitor"]["interval_seconds"])
            while time.monotonic() < deadline and not stopping.is_set():
                time.sleep(min(1, max(0, deadline - time.monotonic())))
        return 0
    except KeyboardInterrupt:
        return 130
    finally:
        monitor.close()


def _bps_text(value) -> str:
    if value is None:
        return "—"
    return f"{Decimal(value):+.1f}"


# Prices on the board carry five decimals: the operator's instruction on
# 2026-09-16 was "价格保留5位小数已经足够". Five is enough to see a tick on a
# ~3.35 share and on a ~4.8 contract, and it stops 18-digit Decimal dust from
# pushing the interesting digits off the table. Non-price columns keep their own
# precision on purpose: bps (2), funding rate (6, because it is a rate), depth and
# notionals (0-2), balances (6-9).
PRICE_PLACES = 5


def _fmt(value, places: int = PRICE_PLACES) -> str:
    if value is None:
        return "—"
    try:
        return f"{Decimal(value):.{places}f}"
    except Exception:
        return str(value)


# Every row says which market it is and which on-chain liquidity state it is in.
# Both are short labels because they sit in a dense table; the reason codes they
# are built from stay in the JSON (`pool_state_reasons`).
_MARKET_LABELS = {HK_MARKET: "港股", "US": "美股", UNKNOWN_MARKET: "上市地未知"}

# Not one of the five *pool* states in `core/liquidity_state.py`: this one is
# about our own sampling, not about the pool. It exists so "we have not re-read
# this cycle" can never be printed as "there is no pool" (see `pool_state`).
POOL_READ_STALE = "pool_read_stale"


def _market_short_label(market) -> str:
    return _MARKET_LABELS.get(normalize_market(market),
                              normalize_market(market) or "—")


_POOL_STATE_LABELS = {
    "no_pool": "无池",
    "empty_pool": "空池",
    "pool_without_liquidity": "深度不足",
    "prints_without_exact_in_quote": "只有成交记录",
    "quote_unusable": "报价不可用",
    "exact_in_quote_available": "可精确报价",
    POOL_READ_STALE: "本轮未采样",
}


def _pool_state_label(state, reasons=None) -> str:
    if not state:
        return "—"
    label = _POOL_STATE_LABELS.get(str(state), str(state))
    reason = next((str(r) for r in (reasons or []) if r), "")
    return f"{label}（{reason}）" if reason else label


def _short_address(address) -> str:
    """``0x076cf393…235682ae`` — enough to check the wallet, not a wall of hex."""
    text = str(address or "")
    return f"{text[:10]}…{text[-8:]}" if len(text) > 20 else text


# Polls /api/status and rings when a gated candidate exists. Sound has to be
# unlocked by one click (browser autoplay policy); until then the banner and the
# flashing title still fire. The poll never starts a data collection of its own:
# it only reads the latest snapshot the monitor already stored.
_ALERT_SCRIPT = """
(function () {
  var MIN_EDGE = %MIN_EDGE%;
  var unlocked = false;
  var alertedKeys = {};
  var lastCount = null;
  var ctx = null;

  function beep(times) {
    if (!unlocked) return;
    try {
      ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
      for (var i = 0; i < times; i++) {
        var t = ctx.currentTime + i * 0.42;
        var osc = ctx.createOscillator();
        var gain = ctx.createGain();
        osc.type = 'sine';
        osc.frequency.value = 880;
        gain.gain.setValueAtTime(0.0001, t);
        gain.gain.exponentialRampToValueAtTime(0.35, t + 0.03);
        gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.34);
        osc.connect(gain); gain.connect(ctx.destination);
        osc.start(t); osc.stop(t + 0.36);
      }
    } catch (e) { /* a blocked AudioContext must not break the page */ }
  }

  var btn = document.getElementById('soundBtn');
  if (btn) {
    if (window.localStorage.getItem('arbSound') === 'on') {
      btn.textContent = '提示音已启用（点击可重试）';
    }
    btn.addEventListener('click', function () {
      unlocked = true;
      window.localStorage.setItem('arbSound', 'on');
      btn.textContent = '提示音已启用';
      beep(1);
    });
  }

  function render(state) {
    var bar = document.getElementById('alertBar');
    var rows = (state.candidates || []).filter(function (c) {
      return c.estimated_net_entry_bps !== null &&
             c.estimated_net_entry_bps !== undefined &&
             Number(c.estimated_net_entry_bps) >= Number(state.min_net_edge_bps || MIN_EDGE);
    });
    document.title = (rows.length ? '⚠️ ' + rows.length + ' 个候选 · ' : '')
      + '代币化股票双腿基差看板';
    if (!rows.length) {
      if (bar) bar.style.display = 'none';
      lastCount = 0;
      return;
    }
    if (bar) {
      bar.style.display = 'block';
      bar.textContent = '候选 ' + rows.length + ' 个：' + rows.map(function (c) {
        // One decimal, like every other bps on the board and in the Feishu
        // message. The raw value is a 28-digit Decimal and belongs in the JSON,
        // not in a sentence a human reads.
        var text = c.asset + ' 空 ' + c.perp_venue + ':' + c.perp_instrument +
                   ' 净 ' + Number(c.estimated_net_entry_bps).toFixed(1) + ' bps';
        var blockers = c.execution_blockers || [];
        // "候选" and "能下单" are different questions; say which one this is.
        text += blockers.length
          ? '（不可执行：' + blockers.join('、') + '）'
          : '（已列入可执行）';
        return text;
      }).join(' ｜ ');
    }
    var newOne = rows.some(function (c) {
      var key = c.asset + '@' + c.perp_venue + ':' + c.perp_instrument +
                '@' + c.estimated_net_entry_bps;
      if (alertedKeys[key]) return false;
      alertedKeys[key] = true;
      return true;
    });
    if (newOne || lastCount === 0 || lastCount === null) { beep(rows.length > 1 ? 2 : 1); }
    lastCount = rows.length;
  }

  function poll() {
    fetch('/api/status', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function () { /* offline page reload is not our concern */ });
  }
  poll();
  setInterval(poll, 30000);
})();
"""


def execution_blockers(candidate: dict, policy: dict) -> list[str]:
    """Why this candidate cannot be turned into an order, in plain words.

    Operator's question on 2026-09-16 ("按这个数字，是不是出了交易信号了") is the
    reason this exists. A candidate can clear the *alert* bar and still be
    utterly untradable — KUAISHOU's perp is on Gate (no execution adapter) and
    its share mapping is unverified — and nothing on the page said so. The same
    facts the risk gate uses are listed here so the banner, the JSON and the
    gate cannot disagree.
    """
    allow = (policy or {}).get("allowlist") or {}
    reasons: list[str] = []
    venue = candidate.get("perp_venue")
    instrument = candidate.get("perp_instrument")
    asset = candidate.get("asset")
    if venue not in (allow.get("venues") or []):
        reasons.append(f"该交易所（{venue}）没有执行适配器")
    if instrument not in (allow.get("instruments") or []):
        reasons.append("合约不在执行白名单")
    if asset not in (allow.get("assets") or []):
        reasons.append("标的不在执行白名单")
    if not candidate.get("mapping_verified"):
        reasons.append("份额映射未核验")
    if not candidate.get("cost_verified"):
        reasons.append("成本未验证")
    if candidate.get("reference_stale"):
        # `token_reference` is the execution_required tier: it carries the token
        # identity and decimals. A row kept from an earlier round is visible for
        # screening, but it must not become an order.
        reasons.append(f'代币引用已过期（{int(candidate.get("reference_age_ms") or 0)} ms 前确认）')
    return reasons


def _universe_banner(universe: dict) -> str:
    """One compact line on the market board: what may be traded, not just read.

    The market board lists everything the sampler reads (Gate, OKX, Binance).
    Only a subset of that can actually be executed, and the operator asked for
    the two to be visible in the same place (2026-09-16).
    """
    if not universe:
        return ""
    escape = lambda value: html.escape("—" if value in (None, "") else str(value))
    executable = universe.get("auto_executable") or []
    items = "、".join(
        f'{escape(row.get("asset"))}@{escape(row.get("venue"))}:'
        f'{escape(row.get("instrument"))}' for row in executable) or "无"
    counts = universe.get("counts") or {}
    return (
        '<div class="notice"><b>可自动执行</b>（{n} 个）：{items}<br>'
        '监控中的其他合约<b>只用于对比与告警</b>：没有已接入的执行适配器、'
        '不在执行白名单，或份额映射未核验。'
        '本页不提供下单入口，自动执行只发生在监控进程里，且受单笔/日额度与停机开关约束。'
        '</div>').format(n=escape(counts.get("auto_executable")), items=items)


def okx_price_screen(contracts, tickers, pool_states, recv_ts_ms: int) -> dict:
    """Join one OKX ticker snapshot to the contracts we already track.

    ``tickers`` is the ``data`` list from ``/api/v5/market/tickers?instType=SWAP``.
    A name is listed only when the perp bid and the pool ask both exist.
    The basis is that pair: sell the contract, buy the token.
    """
    by_id = {}
    for row in tickers or []:
        inst = str((row or {}).get("instId") or "")
        if inst:
            by_id[inst] = row
    rows = []
    for contract in contracts or []:
        if getattr(contract, "venue", None) != "okx" or not getattr(contract, "active", False):
            continue
        ticker = by_id.get(contract.instrument_id) or {}
        bid = _screen_decimal(ticker.get("bidPx"))
        pool = (pool_states or {}).get(contract.asset) or {}
        spot_ask = _screen_decimal(pool.get("spot_ask_usd"))
        # Only the trade we do: sell the perp at its bid, buy the token at
        # the pool ask. A missing leg is not a row.
        if bid is None or spot_ask is None or spot_ask <= 0:
            continue
        basis = (bid - spot_ask) / spot_ask * Decimal(10000)
        rows.append({
            "asset": contract.asset,
            "instrument": contract.instrument_id,
            "okx_bid": str(bid),
            "spot_ask_usd": str(spot_ask),
            "display_basis_bps": str(basis),
            "spot_state": pool.get("state"),
            "executable": False,
        })
    rows.sort(key=lambda row: row["asset"])
    return {
        "recv_ts_ms": recv_ts_ms,
        "source": "okx_tickers_plus_pool_slot0",
        "note": "只列出能买现货、能空合约的名字。基差 = (合约 Bid − 现货 Ask) / 现货 Ask。这一张表不下单。",
        "okx_contracts": len(rows),
        "okx_priced": len(rows),
        "spot_priced": len(rows),
        "both_legs": len(rows),
        "rows": rows,
    }


def _depth_watch_note(summary: dict) -> str:
    watch = summary.get("depth_watchlist") or {}
    if not watch or watch.get("targets") is None:
        return ('<p class="muted">深度名单还没有。跑一次 '
                '<code>hk-depth-census</code> 之后，没有深度的名字当天不再读池子。</p>')
    escape = lambda value: html.escape("—" if value in (None, "") else str(value))
    state = "生效中，无深度的名字不再读池子" if watch.get("active") else "已过期，不过滤"
    return (f'<p class="muted">今日深度名单（门槛 {escape(watch.get("min_depth_usd"))} USD，'
            f'{state}）：有深度 {escape(watch.get("with_depth"))}，'
            f'无深度 {escape(watch.get("no_depth"))}，'
            f'没读完 {escape(watch.get("unchecked"))}。'
            '无深度的不进入持续询价。</p>')


def price_screen_html(summary: dict) -> str:
    """Every OKX name priced this cycle, not only the six exact-in names."""
    escape = lambda value: html.escape("—" if value in (None, "") else str(value))
    screen = summary.get("price_screen") or {}
    rows = screen.get("rows") or []
    if not rows and not screen.get("error"):
        return ""
    head = (
        f'展示 {escape(len(rows))} 个两边都有价的名字。'
        f'没有现货 Ask 的不在这张表里。'
    )
    if screen.get("error"):
        head += f' · 读取失败 {escape(screen.get("error"))}'
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f'<td>{escape(row.get("asset"))}</td>'
            f'<td>{escape(row.get("instrument"))}</td>'
            f'<td>{_screen_places(row.get("okx_bid"), 4)}</td>'
            f'<td>{_screen_places(row.get("spot_ask_usd"), 4)}</td>'
            f'<td>{_screen_places(row.get("display_basis_bps"), 4)}</td>'
            f'<td>{escape(row.get("spot_state"))}</td>'
            "</tr>")
    return (
        '<h2>买入现货 / 卖出合约 '
        '<span class="dim">现货 Ask 对合约 Bid。没有现货价的名字不列。</span></h2>'
        f'<p class="muted">{head}。{escape(screen.get("note"))}</p>'
        + _depth_watch_note(summary)
        + '<div class="scroll"><table><thead><tr>'
        '<th>标的</th><th>OKX 合约</th><th>合约 Bid</th><th>现货 Ask</th>'
        '<th>基差 bps</th><th>池子状态</th>'
        '</tr></thead><tbody>' + "".join(body) + "</tbody></table></div>")


def _screen_places(value, places: int) -> str:
    """Fixed decimal places for the price-screen cells. Empty stays a dash."""
    number = _screen_decimal(value)
    if number is None:
        return "—"
    return f"{number:.{places}f}"


def _screen_decimal(value):
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if not number.is_finite():
        return None
    return number


def market_coverage_html(summary: dict) -> str:
    """Per-market coverage: monitored vs priced-this-cycle vs candidate vs funnel.

    Operator, 2026-09-22: "监控结果也要展示在看板上，现在看板上只有港股相关
    的内容". The table below has always carried a 上市地 column, so a monitored US
    row *does* appear — but a market that is adopted and only partly sampled would
    read as a market full of holes. This strip states the three numbers that
    separate the two: how many of a market's names are monitored, how many of
    those the cycle actually put on the quote grid (including the rotating slice
    and how many names are still waiting their turn), and how many produced a
    candidate.

    "Priced this cycle" is read from ``quote_grid_slice.issued`` — the list the
    cycle actually issued — not from ``quote_universe_assets``, which is
    everything that could ever be quoted and therefore cannot answer the
    question. Measured 2026-09-22 without that distinction the board read "美股
    本周期有报价 0" while all seven reviewed US names were in fact being quoted
    every cycle, because the rotating market was the only one consulted for the
    count.

    The funnel underneath is the *last completed* discovery scan (its age is
    printed). The scan runs every 30 min and the cycle every 60 s, so reporting
    only this cycle's scan would leave the whole strip reading "—" 29 cycles out
    of 30.

    Nothing here is a claim about executability — that stays per row, where
    `份额映射` and the execution allowlist decide it.
    """
    escape = lambda value: html.escape("—" if value in (None, "") else str(value))
    rows = summary.get("token_references") or []
    slice_info = summary.get("quote_grid_slice") or {}
    universe = {str(asset) for asset in
                (summary.get("quote_universe_assets") or [])}
    issued = {str(asset) for asset in (slice_info.get("issued") or [])}
    has_issued = "issued" in slice_info
    slice_markets = {str(m).upper() for m in (slice_info.get("markets") or [])}
    rotating_pool = {str(m).upper(): count for m, count in
                     (slice_info.get("rotating_pool") or {}).items()}
    per_market: dict[str, dict] = {}
    for row in rows:
        market = normalize_market(row.get("listing_market")) or "—"
        seen = per_market.setdefault(market, {"monitored": 0, "candidate": 0,
                                              "priced": 0, "deferred": None})
        seen["monitored"] += 1
        direction = (row.get("directions") or {}).get("long_spot_short_perp") or {}
        if direction.get("judgement") == "candidate":
            seen["candidate"] += 1
        asset = str(row.get("asset"))
        if has_issued:
            priced = asset in issued
        else:
            # Older summaries (before `issued` existed) can only be read the old
            # way: a non-rotating market issued everything, a rotating one only
            # what its slice named.
            priced = asset in universe and market not in slice_markets
        if priced:
            seen["priced"] += 1
    for market, deferred in (slice_info.get("deferred") or {}).items():
        key = normalize_market(market) or market
        per_market.setdefault(key, {"monitored": 0, "candidate": 0,
                                    "priced": 0, "deferred": None})
        per_market[key]["deferred"] = deferred
    scan = summary.get("token_universe_scan") or {}
    if not per_market and not (scan.get("layers") or {}):
        # Nothing monitored and no scan behind it: an empty strip would be noise.
        # A market that is monitored but not yet scanned *does* render, because
        # "we have not looked yet" is a different fact from "nothing is there".
        return ""
    cells = []
    for market, seen in sorted(per_market.items()):
        slice_text = ""
        if market in slice_markets:
            pool = rotating_pool.get(market, 0)
            if pool:
                slice_text = (f' <span class="dim">（轮转报价：本轮 '
                              f'{escape(slice_info.get("assets_per_cycle"))} 个 / '
                              f'轮转池 {escape(pool)} 个 / '
                              f'待轮 {escape(seen["deferred"])} 个）</span>')
            else:
                # A rotating market with nothing to rotate is not a market with
                # holes: every monitored name in it is there every cycle.
                slice_text = (' <span class="dim">（无待轮标的：'
                              '该市场本轮全部报价）</span>')
        cells.append(f'<tr><td>{escape(_market_short_label(market))}</td>'
                     f'<td>{escape(seen["monitored"])}</td>'
                     f'<td>{escape(seen["priced"])}{slice_text}</td>'
                     f'<td>{escape(seen["candidate"])}</td></tr>')
    funnel_text = _discovery_funnel_text(scan)
    return (
        '<h2>市场覆盖 <span class="dim">监控 ≠ 可执行；同一张表下面按上市地平铺</span></h2>'
        '<div class="scroll"><table>'
        '<thead><tr><th>上市地</th><th>监控中标的</th><th>本周期有报价</th>'
        '<th>本轮候选</th></tr></thead><tbody>'
        + "".join(cells) + '</tbody></table></div>'
        f'<p class="muted">{funnel_text}</p>')


def _discovery_funnel_text(scan: dict) -> str:
    """The discovery funnel, market by market, as one sentence.

    Every stage is printed with the number it *has*, not with the number the
    next stage wants: "发行方 754 → 无 OKX 永续 640（未探针）→ 已探针 114 →
    有池/已采纳 1 → 执行白名单 0" is the whole story of why a market of 754
    tokens produces one monitored name, and each step is a count this program
    measured rather than a promise.
    """
    escape = lambda value: html.escape("—" if value in (None, "") else str(value))
    layers = (scan or {}).get("layers") or {}
    if not layers:
        return ("发现漏斗：本轮还没有跑过发行人列表扫描；扫描每 "
                f'{escape((scan or {}).get("interval_seconds"))} 秒一次，'
                "结果会按市场逐层列在这里。")
    age = (scan or {}).get("scan_age_ms")
    issuer = layers.get("issuer_universe") or {}
    gated_out = layers.get("gated_out") or {}
    probed = layers.get("probed") or {}
    probed_this_round = layers.get("probed_this_round") or {}
    pairable = layers.get("pairable_candidates") or {}
    no_pool = layers.get("no_pool") or {}
    executable = layers.get("executable_allowlist") or {}
    parts = []
    for market in sorted(set(issuer) | set(pairable) | set(probed) | set(gated_out)):
        bits = [f'发行方 {escape(issuer.get(market))}']
        if market in gated_out:
            bits.append(f'无 OKX 股票永续 {escape(gated_out.get(market))}（未探针）')
        if market in probed:
            # Two numbers, because they answer different questions: how many
            # names sit in the intersection at all (a ceiling that does not
            # move between rounds) versus how many this round actually spent a
            # probe on (rotates 20 at a time under the cap).
            this_round = probed_this_round.get(market)
            bits.append(f'交集内 {escape(probed.get(market))}'
                        + (f' · 本轮探针 {escape(this_round)}'
                           if this_round is not None else ''))
        # "本轮新增" is load-bearing: a name that was adopted in an earlier
        # round is skipped by `already_monitored()`, so it never appears here
        # again. Without the qualifier the column would read "有池候选 0" on a
        # market that has seven names under monitoring two lines above — the
        # kind of number that gets read as "there is nothing here".
        bits.append(f'本轮新增有池候选 {escape(pairable.get(market))}')
        if market in no_pool:
            bits.append(f'探针无池 {escape(no_pool.get(market))}')
        if market in executable:
            bits.append(f'执行白名单 {escape(executable.get(market))}')
        parts.append(f'{escape(_market_short_label(market))}：' + " · ".join(bits))
    age_text = ""
    if isinstance(age, (int, float)):
        age_text = f'（扫描数据 {max(0, int(age)) // 60000} 分钟前）'
    # The rotation has to be visible or it cannot be checked. `probes_pending_after_cap`
    # reads the same 158 whether the sweep is progressing or replaying the same
    # first 20 names after every restart, so the board prints the order it uses,
    # how much of the intersection has never been looked at, and this round's
    # actual batch — the numbers that told us a restart was resetting progress.
    rotation = (scan or {}).get("probe_rotation") or {}
    rotation_text = ""
    if rotation:
        rotation_text = (
            '探针轮转：<span class="dim">按「最久未探优先」排、位置存在事件里，重启不回到'
            f'开头（上限 {escape(rotation.get("cap"))} 个/轮，'
            f'未探过 {escape(rotation.get("never_probed"))} / '
            f'交集内 {escape(rotation.get("scoped"))} 个）</span>')
        probed_assets = (scan or {}).get("probed_assets") or []
        if probed_assets:
            rotation_text += ('；<span class="dim">本轮探针名单：'
                              + escape("、".join(str(a) for a in probed_assets))
                              + '</span>')
    return ('发现漏斗{age}：{parts}。{rotation}'
            '美股候选要同时满足「OKX 有股票永续」与「X Layer 池深度 ≥ 门槛」'
            '才会进入监控，而且尚未核验份额映射，因此只做展示与筛选；'
            '「交集内」是总量上限，「本轮探针」是这一轮真的量过的数量'
            '（每轮按上限轮转），「本轮新增有池候选」才刚进监控'
            '（早先几轮进的不会重复出现在这里）。').format(
                age=age_text, parts="／".join(parts) or "—",
                rotation=(rotation_text + "。" if rotation_text else ""))


def summary_for_board(summary: dict | None) -> dict:
    """Drop names the depth census kept below the floor.

    No fresh list means nothing is hidden. A name on the with-depth list stays,
    including when its pool read is stale this cycle.
    """
    summary = summary or {}
    watch = summary.get("depth_watchlist") or {}
    assets = watch.get("assets")
    if not watch.get("active") or not assets:
        return summary
    allowed = set(assets)
    shown = dict(summary)
    shown["token_references"] = [
        row for row in summary.get("token_references") or []
        if row.get("asset") in allowed]
    shown["instruments"] = [
        row for row in summary.get("instruments") or []
        if row.get("asset") in allowed]
    screen = dict(summary.get("price_screen") or {})
    screen["rows"] = [
        row for row in screen.get("rows") or [] if row.get("asset") in allowed]
    shown["price_screen"] = screen
    return shown


def _trade_now_status(item: dict | None, spot_state: str | None, *, halt: bool) -> str:
    """One plain sentence: can this row be ordered on this cycle."""
    if halt:
        return "已停机，不会下单"
    item = item or {}
    state = str(spot_state or item.get("pool_state") or "")
    reasons = " ".join(str(x) for x in (item.get("pool_state_reasons") or []))
    if item.get("reference_stale") or "stale" in state or "stale" in reasons:
        return "这轮价格过期，先不下单"
    if "quote_conversion" in reasons or "peg" in reasons:
        return "稳定币汇率没核过，不能下单"
    mapping = item.get("mapping") or {}
    if mapping and not mapping.get("verified"):
        return "映射没签，不能下单"
    direction = (item.get("directions") or {}).get("long_spot_short_perp") or {}
    judgement = direction.get("judgement")
    if (judgement == "candidate" and mapping.get("verified")
            and item.get("pool_state_executable")):
        return "可以马上下单"
    net = direction.get("estimated_net_entry_bps")
    gross = direction.get("gross_entry_basis_bps")
    try:
        if net is not None and Decimal(str(net)) <= 0:
            return "价差是反的，不做"
        if gross is not None and Decimal(str(gross)) <= 0:
            return "价差是反的，不做"
    except Exception:
        pass
    if judgement in {"below_threshold", "not_executable_negative_net", "candidate"}:
        return "价差不够，先看着"
    return "这轮还不能下单"


def _trade_lines(summary: dict) -> list[dict]:
    """One row per name that has both a spot ask and a perp bid."""
    refs = {row.get("asset"): row for row in summary.get("token_references") or []}
    halt = bool((summary.get("auto_execution") or {}).get("halt"))
    lines = []
    seen = set()

    def add(asset, instrument, ask, bid, basis, item, spot_state):
        if asset in seen or ask in (None, "") or bid in (None, ""):
            return
        seen.add(asset)
        status = _trade_now_status(item, spot_state, halt=halt)
        lines.append({
            "asset": asset, "instrument": instrument or "—",
            "ask": ask, "bid": bid, "basis": basis, "status": status,
        })

    for row in (summary.get("price_screen") or {}).get("rows") or []:
        add(row.get("asset"), row.get("instrument"), row.get("spot_ask_usd"),
            row.get("okx_bid"), row.get("display_basis_bps"),
            refs.get(row.get("asset")), row.get("spot_state"))
    for item in summary.get("token_references") or []:
        direction = (item.get("directions") or {}).get("long_spot_short_perp") or {}
        perp = direction.get("perp") or {}
        add(item.get("asset"), perp.get("instrument"),
            item.get("onchain_spot_ask_usd") or item.get("onchain_pool_ask_usd"),
            direction.get("perp_price_usd"),
            direction.get("gross_entry_basis_bps"), item, item.get("pool_state"))
    rank = {"可以马上下单": 0}
    def sort_key(line):
        try:
            basis = Decimal(str(line["basis"]))
        except Exception:
            basis = Decimal("-999999")
        return (rank.get(line["status"], 1), -basis)
    lines.sort(key=sort_key)
    return lines


def dashboard_html(summary: dict | None) -> str:
    escape = lambda value: html.escape(str(value) if value is not None else "—")
    s = summary_for_board(summary)
    auto = s.get("auto_execution") or {}
    order_mode = str(auto.get("order_mode") or "off")
    mode = str(s.get("mode") or "read_only")
    halt = bool(auto.get("halt") or auto.get("halt_file") or s.get("auto_halt")
                or str(auto.get("reason") or "").startswith("halted:"))
    source_errors = s.get("source_errors") or []
    quote_status = str(s.get("quote_status") or "unknown")
    quote_batch_ms = s.get("quote_batch_ms")
    max_age_ms = int(s.get("max_age_ms") or 15000)
    degraded = bool(source_errors) or s.get("onchain_status") in {"degraded", "unavailable"}
    if s.get("monitor_stale", True):
        health_text, health_class = "监控快照过期", "paid"
    elif halt:
        health_text, health_class = "自动执行已停机，需人工对账", "paid"
    elif degraded or (quote_batch_ms is not None and int(quote_batch_ms) > max_age_ms):
        health_text, health_class = "运行中 · 数据降级（本轮不一定可执行）", "unknown"
    else:
        health_text, health_class = "运行正常", "sponsored"
    if order_mode == "live":
        execution_text = "自动实盘已启用"
    elif order_mode == "paper":
        execution_text = "自动纸面交易"
    else:
        execution_text = "只读/自动执行关闭"
    # The subtitle follows the configured discovery scope instead of claiming
    # both venues once the scan is narrowed to Hong Kong.
    scope = (s.get("token_universe_scan") or {}).get("scope_countries") or []
    scope_text = "/".join(market_label(c, "").strip() or c for c in scope) or "未限定"
    perp_rows = []
    for item in s.get("instruments", []):
        row = [item.get("asset"), item.get("instrument"),
               _fmt(item.get("bid"), PRICE_PLACES),
               _fmt(item.get("ask"), PRICE_PLACES), item.get("multiplier"), item.get("payout_model"),
               _fmt(item.get("funding_rate"), 6), item.get("book_fresh")]
        perp_rows.append("<tr>" + "".join(f"<td>{escape(x)}</td>" for x in row) + "</tr>")
    probes = []
    for item in s.get("probes", []):
        row = [item.get(k) for k in ("asset", "side", "channel", "yesterday_close_anchor", "reason")]
        probes.append("<tr>" + "".join(f"<td>{escape(x)}</td>" for x in row) + "</tr>")

    direction_a = []
    for item in s.get("token_references", []):
        direction = (item.get("directions") or {}).get("long_spot_short_perp", {})
        spot_show = item.get("onchain_spot_ask_usd") or item.get("onchain_pool_ask_usd")
        perp = direction.get("perp") or {}
        if spot_show is None or not perp.get("instrument"):
            continue
        book_age = perp.get("book_age_ms")
        judgement = direction.get("judgement")
        verdict = {
            "candidate": "候选（净基差 ≥ 阈值）",
            "below_threshold": "未达阈值",
            "not_executable_negative_net": "不可执行（净基差<0）",
            "display_price_only": "仅展示价（同源，不给信号）",
        }.get(judgement, direction.get("reason") or "缺侧，不给信号")
        # The verdict column is a *screening* read of two prices; it is not an
        # authorisation. A row whose share mapping nobody signed (every US name
        # today, plus the unlabelled HKEXCx/MIXUx) must not be readable as
        # "go": the strategy path rejects it with
        # `unverified_or_expired_share_ratio_fx_mapping` before any size is
        # computed, so the row says the same thing in the same cell.
        if judgement in {"candidate", "below_threshold",
                         "not_executable_negative_net"} \
                and not (item.get("mapping") or {}).get("verified"):
            verdict += " · 份额映射未核验，不可执行"
        plan = direction.get("size_plan") or []
        plan_text = " / ".join(
            f'{Decimal(str(row["size_usd"])):.0f}→{_fmt(row.get("spot_ask_usd"))}'
            f'({_bps_text(row.get("extra_slippage_bps"))}bps)'
            for row in plan
        ) or "—"
        provenance = item.get("onchain_provenance") or {}
        # Which token actually sits behind the row: its symbol, the underlying
        # ticker it tracks, and the X Layer contract. A row that cannot answer
        # "which address did you buy" is not one anybody should hedge against.
        token_cell = " · ".join(part for part in (
            str(provenance.get("token_symbol") or ""),
            str(item.get("underlying_symbol") or ""),
            _short_address(provenance.get("token_address")),
        ) if part) or "—"
        row = [
            item.get("asset"),
            # Which market the row is, and which of the five on-chain liquidity
            # states it is in. "扫描到但没有采用" is a state too, not a gap.
            _market_short_label(item.get("listing_market")),
            token_cell,
            _pool_state_label(item.get("pool_state"),
                              item.get("pool_state_reasons")),
            _fmt(spot_show),
            item.get("onchain_ask_source") or "—",
            ("—" if item.get("onchain_pool_depth_usd") is None
             else f'{Decimal(item["onchain_pool_depth_usd"]):,.0f}'),
            (f'{perp.get("venue")}:{perp.get("instrument")}' if perp else "—"),
            _fmt(direction.get("perp_price_usd")),
            _bps_text(direction.get("gross_entry_basis_bps")),
            _bps_text(direction.get("estimated_entry_cost_bps")),
            _bps_text(direction.get("estimated_net_entry_bps")),
            plan_text,
            verdict,
            ("1:1 ✓" if (item.get("mapping") or {}).get("verified") else "未验证"),
            _fmt(perp.get("funding_rate"), 6),
            ("—" if book_age is None else f'{book_age / 1000:.1f}s'),
        ]
        css = "" if judgement == "candidate" else ' class="weak"'
        direction_a.append(f"<tr{css}>" + "".join(f"<td>{escape(x)}</td>" for x in row) + "</tr>")
    direction_a = "".join(direction_a)
    candidate_count = s.get("candidate_count") or 0
    min_edge = s.get("min_net_edge_bps") or "—"
    # The JS threshold must always be a literal number, or the injected script
    # would be a syntax error whenever a summary lacks the field.
    min_edge_js = s.get("min_net_edge_bps") or "20"
    venue_rows = []
    for item in s.get("token_references", []):
        for quote in item.get("perp_quotes") or []:
            row = [item.get("asset"), quote.get("venue"), quote.get("instrument"),
                   _fmt(quote.get("bid"), PRICE_PLACES),
                   _fmt(quote.get("ask"), PRICE_PLACES),
                   _fmt(quote.get("funding_rate"), 6),
                   (f'{quote["book_age_ms"] / 1000:.1f}s'
                    if quote.get("book_age_ms") is not None else "—"),
                   "是" if quote.get("fresh") else "否"]
            venue_rows.append("<tr>" + "".join(f"<td>{escape(x)}</td>" for x in row) + "</tr>")
    refs = []
    for item in s.get("token_references", []):
        if item.get("onchain_pool_price_usd") is None and item.get("onchain_pool_ask_usd") is None:
            continue
        depth = item.get("onchain_pool_depth_usd")
        provenance = item.get("onchain_provenance") or {}
        prints = item.get("onchain_print_count") or 0
        row = [
            item.get("asset"), provenance.get("token_symbol"),
            f'{(provenance.get("pool_address") or "—")[:14]}',
            f'{item.get("onchain_pool_quote") or "—"} {_fmt(item.get("onchain_pool_fee_bps"), 1)}bps',
            _fmt(item.get("onchain_pool_price_usd"), PRICE_PLACES),
            _fmt(item.get("onchain_pool_ask_usd") or item.get("onchain_ask_usd"),
                 PRICE_PLACES),
            _fmt(item.get("onchain_pool_bid_usd") or item.get("onchain_bid_usd"),
                 PRICE_PLACES),
            ("—" if depth is None else f"{Decimal(depth):,.0f}"),
            f'{prints} 笔 / {_fmt(item.get("onchain_trade_volume_usd"), 2)} USD',
            _bps_text(item.get("print_vs_pool_bps")),
            ("—" if provenance.get("pool_block_number") is None
             else provenance.get("pool_block_number")),
        ]
        refs.append("<tr>" + "".join(f"<td>{escape(x)}</td>" for x in row) + "</tr>")
    provenance = (s.get("token_references") or [{}])[0].get("onchain_provenance") or {}
    status = "监控快照已过期" if s.get("monitor_stale", True) else "最近一次采样"
    gas = s.get("gas_sponsorship") or {}
    gas_status = str(gas.get("status") or "unknown")
    gas_text = {
        "paid": f'⚠️ X Layer 已不再免费：{gas.get("note") or "检测到实际支付的 gas"}',
        "paid_other": f'⚠️ 有非交易类交易付了 gas：{gas.get("note") or ""}',
        "sponsored": f'X Layer gas 仍为 0（{gas.get("checked", 0)} 笔回执实测）',
        "unknown": f'gas 状态未知：{gas.get("note") or "尚无观测"}',
    }.get(gas_status, f'gas 状态未知：{gas_status}')
    gas_banner = (
        '<div class="gasbanner %s">%s</div>' % (
            {"paid": "paid", "paid_other": "other", "sponsored": "sponsored"}.get(
            gas_status, "unknown"),
            escape(gas_text))
    )
    diag = s.get("board_diagnostics") or {}
    error_groups = diag.get("source_error_groups") or {}
    error_text = "、".join(f"{escape(k)} {escape(v)}" for k, v in error_groups.items())
    if not error_text:
        error_text = "无"
    interval_text = "—"
    if diag.get("recent_interval_ms") is not None:
        interval_text = f'{float(diag["recent_interval_ms"]) / 1000:.1f}s'
    max10_text = "—"
    if diag.get("max_interval_10_ms") is not None:
        max10_text = f'{float(diag["max_interval_10_ms"]) / 1000:.1f}s'
    max30_text = "—"
    if diag.get("max_interval_30_ms") is not None:
        max30_text = f'{float(diag["max_interval_30_ms"]) / 1000:.1f}s'
    window_text = "—"
    if diag.get("recent_30m_count") is not None:
        window_text = (f'{diag.get("recent_30m_degraded_count", 0)}/'
                       f'{diag.get("recent_30m_count", 0)} 轮降级；'
                       f' {diag.get("recent_30m_pools_only_count", 0)} 轮仅池状态')
    batch_text = "—" if quote_batch_ms is None else f'{float(quote_batch_ms) / 1000:.1f}s'
    budget_text = f'{max_age_ms / 1000:.1f}s'
    over_text = "未超预算"
    if quote_batch_ms is not None and int(quote_batch_ms) > max_age_ms:
        over_text = f'超 {float(int(quote_batch_ms) - max_age_ms) / 1000:.1f}s'
    readiness = diag.get("readiness") or (
        "本轮报价链路可评估" if quote_status == "quotes_observed" and not degraded
        else "本轮不具备完整自动执行条件")
    blockers = "、".join(escape(x) for x in diag.get("blockers") or []) or "无"
    # Per-host reference health, one line per host. A single "5 errors" count
    # could not tell a host that is being deliberately backed off from one that
    # is broken, and those two call for opposite responses.
    host_rows = []
    for host, entry in sorted((diag.get("reference_hosts") or {}).items()):
        state = str(entry.get("state") or "unknown")
        state_text = {"healthy": "正常", "backoff": "退避中",
                      "open": "熔断中", "recovering": "恢复探测中"}.get(state, state)
        retry_in = int(entry.get("retry_in_ms") or 0)
        reason = str(entry.get("last_reason") or "—")
        host_rows.append(
            f'<div><b>{escape(host)}</b> · {escape(state_text)}'
            f'<br><span class="muted">'
            + (f'下次重试 {retry_in / 1000:.0f}s 后 · ' if retry_in else '可请求 · ')
            + f'连续失败 {int(entry.get("consecutive_failures") or 0)} · '
            f'退避跳过 {int(entry.get("suppressed") or 0)} 次<br>'
            f'{escape(reason[:90])}</span></div>')
    worker = diag.get("reference_worker") or {}
    if worker:
        host_rows.append(
            '<div><b>引用刷新</b> · '
            + ("进行中" if worker.get("in_flight") else "空闲")
            + f'<br><span class="muted">已完成 {int(worker.get("rounds") or 0)} 轮 · '
            f'上轮 {int(worker.get("attempted") or 0)} 次请求 / '
            f'{int(worker.get("answered") or 0)} 个身份确认；'
            f'沿用上轮 {int(worker.get("kept_from_previous_round") or 0)} 个'
            '</span></div>')
    reference_hosts_block = (
        '<div class="diaggrid" style="margin-top:8px">'
        + ("".join(host_rows) or '<div>引用 host 健康：暂无记录</div>')
        + '</div>')
    diagnostics_block = (
        '<div class="diaggrid">'
        f'<div><b>本轮自动执行条件</b><br>{escape(readiness)}<br>'
        f'<span class="muted">阻塞项：{blockers}</span></div>'
        f'<div><b>报价新鲜度</b><br>批次 {escape(batch_text)} / 预算 {escape(budget_text)}<br>'
        f'<span class="muted">{escape(over_text)} · 覆盖 {escape(s.get("quote_probe_coverage"))}</span></div>'
        f'<div><b>采样节拍</b><br>上一间隔 {escape(interval_text)}<br>'
        f'<span class="muted">最近 10 轮最大 {escape(max10_text)} · 30 轮最大 {escape(max30_text)}<br>{escape(window_text)}</span></div>'
        f'<div><b>数据源错误分类</b><br>{error_text}<br>'
        f'<span class="muted">后台未采样不会被当作旧数据</span></div>'
        '</div>')
    lines = _trade_lines(s)
    ready = sum(1 for line in lines if line["status"] == "可以马上下单")
    body = []
    for line in lines:
        css = ' class="ready"' if line["status"] == "可以马上下单" else ""
        body.append(
            f'<tr{css}><td>{escape(line["asset"])}</td>'
            f'<td>{escape(line["instrument"])}</td>'
            f'<td>{_screen_places(line["ask"], 4)}</td>'
            f'<td>{_screen_places(line["bid"], 4)}</td>'
            f'<td>{_screen_places(line["basis"], 4)}</td>'
            f'<td>{escape(line["status"])}</td></tr>')
    trade_table = (
        '<div class="scroll"><table><thead><tr>'
        '<th>标的</th><th>合约</th><th>现货 Ask</th><th>合约 Bid</th>'
        '<th>基差 bps</th><th>现在能不能下单</th>'
        '</tr></thead><tbody>' + "".join(body) + '</tbody></table></div>')
    return (
        '<!doctype html><html lang="zh"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>代币化股票双腿基差看板</title>'
        '<style>body{max-width:1280px;margin:32px auto;padding:0 20px;font:14px system-ui;'
        'background:#101820;color:#e6edf3}h1{font-size:26px}h2{font-size:18px;margin-top:32px}'
        '.badge{padding:10px 16px;margin:8px 8px 8px 0;display:inline-block;background:#243747;'
        'border-radius:8px}table{width:100%;border-collapse:collapse;background:#182632}'
        'td,th{padding:10px;text-align:left;border-bottom:1px solid #2b3b49}'
        'td{font-family:ui-monospace,monospace}th{color:#78dcca}small,.muted{color:#acbcc9}'
        '.scroll{overflow:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;padding:20px;'
        'background:#182632}.notice{border-left:3px solid #e9ae62;padding:12px;background:#24303a}'
        'tr.ready td{background:#16301f;color:#d6f5df;font-weight:650}'
        '.alertbar{border-left:3px solid #e5534b;padding:12px;margin:12px 0;background:#3a2624;'
        'font-weight:600}'
        '.gasbanner{padding:12px;margin:12px 0;border-radius:8px;font-weight:600}'
        '.gasbanner.paid{background:#5a1f1c;border-left:4px solid #ff4d4f;color:#ffe6e6}'
        '.gasbanner.other{background:#3a3320;border-left:4px solid #e9ae62;color:#f7e9c8}'
        '.gasbanner.sponsored{background:#16301f;border-left:4px solid #3fb950;color:#d6f5df}'
        '.gasbanner.unknown{background:#3a3320;border-left:4px solid #e9ae62;color:#f7e9c8}'
        '.healthbar{padding:14px;margin:12px 0;border-radius:8px;font-weight:700}'
        '.healthbar.paid{background:#5a1f1c;border-left:4px solid #ff4d4f;color:#ffe6e6}'
        '.healthbar.unknown{background:#3a3320;border-left:4px solid #e9ae62;color:#f7e9c8}'
        '.healthbar.sponsored{background:#16301f;border-left:4px solid #3fb950;color:#d6f5df}'
        '.diaggrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;'
        'margin:12px 0}.diaggrid>div{padding:11px;background:#182632;border:1px solid #2b3b49;'
        'border-radius:7px;line-height:1.55}'
        '</style><h1>代币化股票双腿基差看板</h1>'
        + gas_banner +
        f'<div class="healthbar {health_class}">系统状态：{escape(health_text)} · '
        f'{escape(execution_text)} · 当前模式 {escape(mode)} · '
        f'最近数据源错误 {escape(len(source_errors))} 条</div>'
        f'<p class="muted">{escape(status)} · {escape(s.get("hk_time"))} · '
        f'{escape(execution_text)}。下面每一行只做一件事：买入现货 Ask，卖出合约 Bid。</p>'
        f'<span class="badge">可以马上下单：{ready} 个</span>'
        '<div id="alertBar" class="alertbar" style="display:none"></div>'
        '<div id="soundBar" class="muted">提示音：<button id="soundBtn" type="button">'
        '启用提示音</button>'
        ' （浏览器要求先有一次点击，之后本页在出现可下单的名字时会响）</div>'
        + trade_table +
        '<p class="muted">页面每 30 秒看一次有没有新的可下单名字。表格是打开页面时的这一轮。</p>'
        f'<script>{_ALERT_SCRIPT.replace("%MIN_EDGE%", escape(min_edge_js))}</script></html>'
    )


def serve_dashboard(path: str, port: int) -> int:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if self.path not in ("/", "/api/status"):
                self.send_error(404)
                return
            store = EventStore(path)
            try:
                summary = store.latest_summary()
            finally:
                store.close()
            if summary:
                summary = age_summary(summary, now_ms())
            payload = dumps(summary)
            if self.path == "/api/status":
                content, mime = payload, "application/json"
            else:
                content = dashboard_html(summary)
                mime = "text/html"
            body = content.encode()
            self.send_response(200)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Read-only local dashboard: http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
