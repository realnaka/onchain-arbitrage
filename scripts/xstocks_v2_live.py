#!/usr/bin/env python3
"""Stage-1 live executor for v2: chain -> Kraken, reusing the v1 legs.

    scripts/xstocks_v2_live.py status
    scripts/xstocks_v2_live.py once              # plan only, never signs
    scripts/xstocks_v2_live.py once --confirm-live

This is the only v2 file that can move money, and it moves it only when every
one of these is true:

* ``ops/xstocks_v2_policy.toml``: ``phase`` yields the family, ``auto_orders``
  is ``live``, ``live_ack`` is non-empty (all three are operator-only);
* v1 is not holding its Kraken key (no lock file, no auto chain process);
* no ``data-v2/xstocks_v2_HALT``, no ``data-v2/KILL_SWITCH``;
* ``--confirm-live`` is on the command line;
* at least one candidate passes the v2 gate on a fresh two-sided re-quote.

The three legs are the v1 ones, not copies: Jupiter quote + OnchainOS wallet
swap, ``xstocks_deposit`` transfer into the Kraken deposit address, and
``xstocks_sell`` against the Kraken book. Writing a second signing path is how
two processes end up with the same nonce.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from arb_monitor.execution import xstocks_buy as buy        # noqa: E402
from arb_monitor.execution import xstocks_deposit as dep    # noqa: E402
from arb_monitor.execution import xstocks_sell as sell      # noqa: E402
from arb_monitor.venues.kraken_spot import (                # noqa: E402
    KrakenError, KrakenSpotClient, load_credentials)
from arb_monitor.xstocks_v2 import costs                     # noqa: E402
from arb_monitor.xstocks_v2 import cycle as cycle_core      # noqa: E402
from arb_monitor.xstocks_v2 import (incident, ledger, live, paper,  # noqa: E402
                                    probe as probe_core)
from arb_monitor.xstocks_v2 import scan  # noqa: E402
from arb_monitor.xstocks_v2.feeds import census as census_feed  # noqa: E402
from arb_monitor.xstocks_v2.feeds import kraken as kraken_feed  # noqa: E402
from arb_monitor.xstocks_v2.feeds.http import Http          # noqa: E402
from arb_monitor.xstocks_v2.feeds.jupiter import (  # noqa: E402
    JupiterFeed, SwapClient, build_swap_retry)
from arb_monitor.xstocks_v2.policy import V2Policy          # noqa: E402
from arb_monitor.xstocks_v2.store import read_json, write_json  # noqa: E402
from arb_monitor.xstocks_v2.units import SCALED_RAW_CHAINS  # noqa: E402

DATA = scan.DEFAULT_DATA
SCAN = DATA / "xstocks_v2_scan.json"
STATE = DATA / "xstocks_v2_live.json"
CYCLES = DATA / "xstocks_v2_cycles.jsonl"
ROUTE_SAMPLES = DATA / "xstocks_v2_route_samples.jsonl"
TRANSFER_SEEN = DATA / live.TRANSFER_SEEN_NAME
SIZES = (Decimal("25"), Decimal("10"), Decimal("5"), Decimal("2"))
#: The chain -> Kraken *planner* below is Solana only: it re-quotes Jupiter,
#: reads SPL balances and sizes in Solana units. Ethereum has its own executor
#: (``scripts/xstocks_v2_eth_roundtrip.py``) and its own selector.
#: ``ALLOWED_CHAINS`` is executor coverage -- what the *gate* may sign for --
#: and comes from the same table the scan reads.
PLAN_CHAINS = frozenset({"solana"})
ALLOWED_CHAINS = live.EXECUTOR_CHAINS[live.CHAIN_TO_KRAKEN]
ETH_CHAIN = "eth"
ETH_SCRIPT = "xstocks_v2_eth_roundtrip.py"
TON_CHAIN = "ton"
TON_SCRIPT = "xstocks_v2_ton_roundtrip.py"
XLAYER_CHAIN = "x-layer"
SOL_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOLANA_WALLET = "AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev"
EVM_WALLET = "0xabababababababababababababababababababab"
SOL_USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE_DECIMALS = 6
TOKEN_DECIMALS = 8
#: A rate-limited buy-side re-quote is retried; a dead pool is not. See
#: ``requote_buy_leg``.
REQUOTE_TRIES = 4
REQUOTE_BACKOFF_S = 1.0
REQUOTE_BACKOFF_MAX_S = 5.0


def _d(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def row_notional(row: dict, policy: V2Policy) -> Decimal:
    """The size this row was **priced** at -- the scan's ticket, not the ceiling.

    Quoting and trading must be the same size, or the quote describes a trade we
    never make. Measured 2026-10-08: Solana rows were priced at the 200 USDT
    ceiling (300-9999 bps of impact, so nothing ever cleared), while the executor
    sized with the wallet. The scan now prices a Solana route at 25 USDT and the
    executor follows the row.
    """
    value = _d(row.get("notional_usdt"))
    return value if value is not None and value > 0 else policy.per_trade_quote_usdt


# ---------------------------------------------------------------------------
# Funding: 机会发现不看钱包，执行时缺什么就换什么 (operator 2026-10-08)
# ---------------------------------------------------------------------------
#: Funding lives in the module (``live.funding_plan`` / ``live.conversion_cost_bps``)
#: so the paper runner prices the same swap the same way. These names stay here
#: because the tests and ``plan_one`` patch/inspect them.
CONVERT_QUOTE_TRIES = live.CONVERT_QUOTE_TRIES
funding_plan = live.funding_plan
conversion_cost_bps = live.conversion_cost_bps


def convert_to_usdc(box, amount_usdt: Decimal, *, policy: V2Policy) -> dict:
    """One real swap: the stablecoin we hold -> the one the pool quotes in.

    The amount is the ticket itself, so the money stays a stablecoin whatever
    happens next: if the buy leg fails after this, we hold USDC instead of USDT
    -- a different dollar, not a naked position.
    """
    raw = int(amount_usdt * Decimal(10 ** QUOTE_DECIMALS))
    quote, note = requote_buy_leg(box.jupiter, input_mint=SOL_USDT,
                                  mint=SOL_USDC, raw_amount=raw,
                                  slippage_bps=policy.max_slippage_bps)
    if quote is None:
        return {"ok": False, "reason": f"convert_quote:{note}"}
    swap, note = build_swap_retry(box.jupiter, quote)
    if not swap.get("swapTransaction"):
        return {"ok": False, "reason": f"convert_build:{note}"}
    sim = box.solana.simulate(swap["swapTransaction"])
    if not sim["ok"]:
        return {"ok": False, "reason": f"convert_simulate:{sim['err']}"}
    before = box.solana.raw_balance(SOL_USDC)
    res = box.wallet.swap(swap["swapTransaction"])
    if not res["ok"]:
        return {"ok": False, "reason": f"convert_broadcast:{res['error']}",
                "unknown": True}
    held = _wait_balance(box.solana, SOL_USDC, before)
    if held is None:
        return {"ok": False, "reason": "convert_unconfirmed", "unknown": True}
    return {"ok": True, "tx": res["tx"], "usdc_raw": held}


def _wait_for_at_least(solana, mint: str, target: int, *, floor: int = 0,
                       tries: int = 6, pause: float = 3.0) -> int:
    """Wait for a balance to reach ``target``, else return the last reading."""
    best = floor
    for _ in range(max(1, int(tries))):
        now = solana.raw_balance(mint)
        if now >= target:
            return now
        best = max(best, now)
        time.sleep(pause)
    return best


def gate(policy: V2Policy, data_dir: Path, *, family: str = live.CHAIN_TO_KRAKEN,
         chain: str = "solana") -> tuple[bool, str]:
    paths = cycle_core.halt_paths(data_dir)
    busy, why = cycle_core.v1_busy(ROOT)
    allowed, reason = live.live_gate(
        policy, halt=paths["halt"].exists(), lock=paths["lock"].exists(),
        kill=paths["kill"].exists(), auto_halt=paths["auto_halt"].exists(),
        v1_busy=busy, family=family, chain=chain, allowed_chains=ALLOWED_CHAINS)
    if not allowed and reason == "v1_live_busy":
        return False, f"v1_live_busy: {why}"
    return allowed, reason


def candidates(snapshot: dict, *, chains=PLAN_CHAINS, limit: int = 5) -> list[dict]:
    """Rows with nothing in the gate left except the informational marks."""
    # ``below_threshold`` is the scan's t0 verdict. ``plan_one`` re-quotes both
    # legs and ``paper.decide`` re-derives it at the executable size, so treating
    # it as final here would replace a measurement with a snapshot -- and it
    # would silently stop re-quoting every chain -> Kraken row outside the
    # regular session, which is where the only solana rows live.
    informational = {"phase_scan", "chain_out_of_phase", "below_threshold"}
    out = []
    for row in snapshot.get("rows") or []:
        if row.get("family") != live.CHAIN_TO_KRAKEN:
            continue
        if row.get("chain") not in chains:
            continue
        if set(row.get("blockers") or []) - informational:
            continue
        edge = _d(row.get("edge_t0_bps"))
        if edge is None:
            continue
        out.append(row)
    out.sort(key=lambda row: _d(row["edge_t0_bps"]), reverse=True)
    return out[:limit]


def eth_trade_ready(row: dict, *, snapshot: dict, units: dict,
                    policy: V2Policy) -> bool:
    """Everything a mainnet ticket needs, including a measured credit ratio.

    The net has to clear **after** the gas line the scan charges an eth row, and
    Kraken's credit ratio for this symbol has to have been measured -- without
    it the sell cannot be sized (``xstocks_v2_eth_roundtrip.credit_guard``).
    A row that fails only the second question belongs to the sample stage.
    """
    unit = (units or {}).get(str(row.get("symbol"))) or {}
    if unit.get("deposit_credit_units_per_balance_unit") is None:
        return False
    edge, net = _d(row.get("edge_t0_bps")), _d(row.get("net_usdt"))
    if edge is None or net is None:
        return False
    threshold = _d(snapshot.get("threshold_bps")) or policy.min_net_edge_bps
    return costs.net_clears(edge_bps=edge, net_usdt=net, min_bps=threshold,
                            min_usdt=policy.min_net_profit_usdt)


def eth_candidates(snapshot: dict, *, units: dict | None = None,
                   limit: int = 3) -> list[dict]:
    """chain -> Kraken rows on Ethereum that are worth the gas.

    Same question as ``candidates``, one chain over: nothing left in the gate
    but the informational marks, and a net that clears **after** the mainnet
    gas line the scan charges an eth row (441,831 gas). The 25 USDT ticket is
    the hard case here: at 0.11 gwei the gas is ~0.12 USDT, so most 80 bps
    spreads do not pay, and the row's own ``below_net`` is what says so.

    A symbol whose Kraken credit ratio was never measured is **not** a trade
    row: the sell cannot be sized. It appears under ``eth_probe_candidates``
    instead, and the executor refuses it with a message that says so (the
    refusal has to be a row, not a crash inside the run loop).
    """
    informational = {"phase_scan", "chain_out_of_phase", "below_threshold"}
    policy = V2Policy.load()
    units = units if units is not None else {}
    out = []
    for row in snapshot.get("rows") or []:
        if row.get("family") != live.CHAIN_TO_KRAKEN:
            continue
        if str(row.get("chain")) != ETH_CHAIN:
            continue
        if set(row.get("blockers") or []) - informational:
            continue
        edge = _d(row.get("edge_t0_bps"))
        if edge is None:
            continue
        if not eth_trade_ready(row, snapshot=snapshot, units=units, policy=policy):
            continue
        out.append(row)
    out.sort(key=lambda row: _d(row["edge_t0_bps"]), reverse=True)
    return out[:limit]


def eth_probe_candidates(snapshot: dict, *, units: dict | None = None,
                         limit: int = 3) -> list[dict]:
    """Eth chain -> Kraken rows whose Kraken credit ratio was never measured.

    Kraken credits a deposit in *its* units and the ratio is per (symbol,
    network); the repo has measured it for Solana, and for Ethereum only for
    STRCx. 缺样本不是拒绝理由 (operator 2026-10-09): the way to learn it is to
    buy the deposit minimum, deposit it and watch the balance -- so these rows
    are exactly the sample stage, whatever their estimated net says.
    """
    units = units if units is not None else {}
    policy = V2Policy.load()
    out = []
    for row in snapshot.get("rows") or []:
        if row.get("family") != live.CHAIN_TO_KRAKEN:
            continue
        if str(row.get("chain")) != ETH_CHAIN:
            continue
        blockers = set(row.get("blockers") or [])
        # ``unmeasured_route`` / ``no_deposit_method`` mean Kraken has **no**
        # Ethereum rail for this symbol: the path is a bridge, which is another
        # family's executor. Buying the minimum here would pay for a sample this
        # executor cannot take (measured 2026-10-09: SLVx sat in this list with
        # ``unmeasured_route``, and its plan refuses at the rail lookup).
        if blockers & {"stale_or_skew", "price_outlier", "no_executor_chain",
                       "hook_not_green", "no_deposit_method",
                       "unmeasured_route", "no_address_book"}:
            continue
        edge = _d(row.get("edge_t0_bps"))
        if edge is None:
            continue
        unit = units.get(str(row.get("symbol"))) or {}
        if unit.get("deposit_credit_units_per_balance_unit") is not None:
            continue          # measured: the trade path can size it for real
        if eth_trade_ready(row, snapshot=snapshot, units=units, policy=policy):
            continue          # the trade path takes it; no sample needed first
        out.append(row)
    out.sort(key=lambda row: _d(row["edge_t0_bps"]) or Decimal("-1e9"), reverse=True)
    return out[:limit]


def _direct_ready(row: dict, *, chain: str, snapshot: dict, policy: V2Policy) -> bool:
    """A chain -> Kraken row this chain's own executor may sign.

    ``net_at_ceiling_usdt`` is the scan's "the 25 USDT quote loses a fixed
    burn, the ladder ceiling pays" mark. The executor re-quotes that size
    before it signs; this only decides the row is worth that re-quote.
    """
    if row.get("family") != live.CHAIN_TO_KRAKEN or str(row.get("chain")) != chain:
        return False
    informational = {"phase_scan", "chain_out_of_phase", "below_threshold"}
    if set(row.get("blockers") or []) - informational:
        return False
    edge = _d(row.get("edge_t0_bps"))
    net = _d(row.get("net_at_ceiling_usdt")) or _d(row.get("net_usdt"))
    if edge is None or net is None:
        return False
    threshold = _d(snapshot.get("threshold_bps")) or policy.min_net_edge_bps
    return costs.net_clears(edge_bps=edge, net_usdt=net, min_bps=threshold,
                            min_usdt=policy.min_net_profit_usdt)


def ton_candidates(snapshot: dict, *, limit: int = 3) -> list[dict]:
    """chain -> Kraken rows on TON whose net clears after the ston.fi burn."""
    policy = V2Policy.load()
    out = [row for row in (snapshot.get("rows") or [])
           if _direct_ready(row, chain=TON_CHAIN, snapshot=snapshot, policy=policy)]
    out.sort(key=lambda row: _d(row["edge_t0_bps"]), reverse=True)
    return out[:limit]


def xlayer_candidates(snapshot: dict, *, limit: int = 3) -> list[dict]:
    """chain -> Kraken rows on X Layer.

    Kraken has no X Layer deposit. The executor buys on the pool, bridges to
    Solana and deposits there. The scan has already put that bridge fee in
    ``hub_fixed_usd``, so a row that only clears before the fee never arrives
    here.
    """
    policy = V2Policy.load()
    out = [row for row in (snapshot.get("rows") or [])
           if _direct_ready(row, chain=XLAYER_CHAIN, snapshot=snapshot, policy=policy)]
    out.sort(key=lambda row: _d(row["edge_t0_bps"]), reverse=True)
    return out[:limit]


# The second executable family: Kraken buy -> withdraw to a rail Kraken has ->
# bridge -> sell on the destination chain. It is driven by the roundtrip cycle,
# which recomputes the path itself, so all this needs from the scan is a symbol
# whose new-family row has nothing left but informational marks.
BRIDGE_FAMILY = live.KRAKEN_VIA_BRIDGE_TO_CHAIN
# Measured lower bound: 0.00947893 SPYx went through on 2026-10-07. Unknown is
# not zero, so this is supplied explicitly rather than assumed.
BRIDGE_MIN = os.environ.get("XSTOCKS_V2_BRIDGE_MIN", "0.0095")


def bridge_net_ok(row: dict, *, snapshot: dict, policy: V2Policy | None = None,
                  available_usdt=None, family: str | None = None) -> bool:
    """May a bridge family fire on this row?

    Two things this must not do, both measured on 2026-10-07:

    * fire on a row whose net was never computed (the scan writes ``net_usdt``
      only when it can name the bridge fee, so a null net is a refusal);
    * judge the trade at the *policy* notional when the wallet can only afford a
      fraction of it. A fixed bridge fee of ~0.10 USD on an 8 USD ticket is
      125 bps, so a row that clears the bar at 200 USDT can lose money at the
      size we can actually trade. ``available_usdt`` caps the notional that the
      net is recomputed at.

    The cost model itself lives in ``costs.net_after_costs`` and is driven by
    the *family*: only the families that trade on Kraken pay its taker fee.
    Until 2026-10-07 this function charged that fee for every bridged family
    while the scan did not, so the board and the gate published two nets for
    the same row, 10 bps apart.
    """
    policy = policy or V2Policy.load()
    edge = _d(row.get("edge_after_requote_bps")) or _d(row.get("edge_t0_bps"))
    fee = _d(row.get("bridge_fee_usd"))
    notional = _d(row.get("notional_usdt"))
    if edge is None or notional is None:
        return False
    available = None if available_usdt is None else _d(available_usdt)
    if available_usdt is not None and available is None:
        return False
    size = costs.judged_notional(notional, available)
    if size is None or size <= 0:
        return False
    if fee is None:
        # No measured fee means no net at any size -- the old scan-level
        # ``net_usdt`` is not a substitute; it was computed at the wrong size.
        return False
    net_at_size = costs.net_after_costs(
        gross_usdt=size * edge / Decimal(10000), notional_usdt=size,
        fixed_cost_usd=fee, policy=policy,
        family=family if family is not None else row.get("family"))
    threshold = _d(snapshot.get("threshold_bps")) or policy.min_net_edge_bps
    return costs.net_clears(edge_bps=edge, net_usdt=net_at_size,
                            min_bps=threshold,
                            min_usdt=policy.min_net_profit_usdt)


def bridge_candidates(snapshot: dict, *, limit: int = 5) -> list[dict]:
    informational = {"phase_scan", "chain_out_of_phase"}
    out = []
    for row in snapshot.get("rows") or []:
        if row.get("family") != BRIDGE_FAMILY:
            continue
        # This slot signs on Solana and sells on X Layer. A row from any other
        # pair has no slot here, however good its numbers look.
        if str(row.get("chain")) != SLOT_PAIR[BRIDGE_FAMILY]:
            continue
        if set(row.get("blockers") or []) - informational:
            continue
        if not bridge_net_ok(row, snapshot=snapshot, family=BRIDGE_FAMILY):
            continue
        out.append(row)
    out.sort(key=lambda row: _d(row["edge_t0_bps"]), reverse=True)
    return out[:limit]


def run_bridge_cycle(symbol: str, *, notional=None, price=None,
                     data_dir: Path | str = DATA) -> dict:
    """Drive the Kraken -> hub -> bridge -> chain family once.

    Imported from the roundtrip script on purpose: the resident process and a
    manual ``once`` run the *same* state machine, or they will drift.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "v2roundtrip", ROOT / "scripts" / "xstocks_v2_xlayer_roundtrip.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    code = module.cycle_once(
        symbol=symbol, bridge_min=BRIDGE_MIN, confirm_live=True,
        notional=str(notional if notional is not None
                     else V2Policy.load().per_trade_quote_usdt),
        price=str(price or "0") or "780")
    return {"symbol": symbol, "exit_code": code,
            "state": "sold" if code == 0 else "see_ledger"}


# The third executable family: buy on X Layer, bridge to Solana, sell there.
# Unlike the Kraken-hub family this one needs no Kraken money at all, which is
# why it is the only family that can be practised at the current balance. The
# scan already emits the row (family ``chain_to_chain``, ``x-layer->solana``);
# this only picks it up, through the same one-cycle slot as the others.
SOLANA_FAMILY = live.XLAYER_VIA_BRIDGE_TO_SOLANA
SOLANA_SOURCE, SOLANA_DEST = "x-layer", "solana"
# The bridge fee is per message and is paid in the source chain's gas token.
# Measured twice on 2026-10-07 (0.000791242725185203 OKB). Unknown is not zero,
# so it is passed explicitly and never assumed.
SOLANA_BRIDGE_FEE_WEI = os.environ.get("XSTOCKS_V2_SOLANA_FEE_WEI",
                                       "791242725185203")


def solana_candidates(snapshot: dict, *, limit: int = 5) -> list[dict]:
    informational = {"phase_scan", "chain_out_of_phase"}
    out = []
    for row in snapshot.get("rows") or []:
        if row.get("family") != "chain_to_chain":
            continue
        if (row.get("source_chain") != SOLANA_SOURCE
                or row.get("dest_chain") != SOLANA_DEST):
            continue
        if set(row.get("blockers") or []) - informational:
            continue
        if not bridge_net_ok(row, snapshot=snapshot, family=SOLANA_FAMILY):
            continue
        out.append(row)
    out.sort(key=lambda row: _d(row["edge_t0_bps"]), reverse=True)
    return out[:limit]


def run_solana_cycle(symbol: str, *, notional=None, price=None,
                     data_dir: Path | str = DATA) -> dict:
    """Drive the X Layer -> Solana reverse family once."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "v2solana_roundtrip", ROOT / "scripts" / "xstocks_v2_solana_roundtrip.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    code = module.cycle_once(
        symbol=symbol, bridge_min=BRIDGE_MIN, fee_wei=SOLANA_BRIDGE_FEE_WEI,
        confirm_live=True,
        notional=str(notional if notional is not None
                     else V2Policy.load().per_trade_quote_usdt),
        price=str(price or "0") or "780")
    return {"symbol": symbol, "exit_code": code,
            "state": "sold" if code == 0 else "see_ledger"}


# The fourth family: buy on Solana, bridge to X Layer, sell there. The scan has
# always shown this direction; until 2026-10-07 nothing could execute it, which
# meant the only consistently positive row on the board had no executor.
SOLANA_BRIDGE_FAMILY = live.SOLANA_VIA_BRIDGE_TO_XLAYER
SOLANA_BUY_CHAIN, SOLANA_SELL_CHAIN = "solana", "x-layer"
USDG_XLAYER = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"

# The fifth family, added 2026-10-07: buy on the pool chain, bridge to a hub
# Kraken credits, deposit, sell into the Kraken bid. That is the family the
# board has been showing since the bridge table went in, and nothing could walk
# it -- Kraken credits these xStocks on Solana only, so a deep X Layer pool is
# reachable this way and no other.
VIA_BRIDGE_TO_KRAKEN = live.CHAIN_VIA_BRIDGE_TO_KRAKEN
#: Chased with the same roundtrip script, Kraken ending instead of Jupiter.
VIA_BRIDGE_MIN = os.environ.get("XSTOCKS_V2_BRIDGE_MIN", "0.0095")

#: The pair each bridge slot can really drive. The scan prices every family on
#: every chain, but a slot is one script with **one** source chain: the pickers
#: used to filter on the family alone, so a row from another chain pair would be
#: handed to a script that signs somewhere else. Nothing fired that way yet,
#: because those rows carry blockers -- which is exactly why this has to be a
#: check and not a hope (operator 2026-10-10: 「runner 没有槽是什么意思」).
SLOT_PAIR = {
    BRIDGE_FAMILY: "solana->x-layer",          # Kraken -> Solana -> X Layer
    VIA_BRIDGE_TO_KRAKEN: "x-layer->solana",   # X Layer -> Solana -> Kraken
}


def _rpc(http, url: str, method: str, params: list) -> dict:
    try:
        # feeds.Http is urllib-based and always goes through the local proxy.
        _status, payload = http.post_json(
            url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def available_quote_usdt(family: str, *, http=None, client=None) -> Decimal:
    """The buy-side money this family can actually spend, in USD.

    A fixed bridge fee does not care how big the ticket is, so the gate has to
    be applied at the *executable* size. Returns ``0`` when the balance cannot
    be read: an unreadable wallet is a refusal, not a licence to assume the
    policy notional.
    """
    http = http or Http()
    if family == SOLANA_BRIDGE_FAMILY:
        # Both Solana stablecoins are money for this family. Reading only USDC
        # hid 44.5405 USDT and reported a funded route as unfunded
        # (found 2026-10-07 after the operator said to use the USDT).
        #
        # The answer is the **larger** balance, not the sum: one cycle spends
        # one stablecoin, so summing them would let the gate clear a trade the
        # executor cannot size (and the fixed bridge fee is per message).
        return max((_solana_spl_balance(http, mint) for mint in
                    live.SOLANA_QUOTES.values()), default=Decimal("0"))
    if family in (SOLANA_FAMILY, VIA_BRIDGE_TO_KRAKEN):
        # Both start by spending X Layer USDG, so the same balance caps both.
        data = "0x70a08231" + "0" * 24 + EVM_WALLET[2:].lower()
        out = _rpc(http, "https://rpc.xlayer.tech", "eth_call",
                   [{"to": USDG_XLAYER, "data": data}, "latest"]).get("result")
        if not out or out == "0x":
            return Decimal("0")
        return Decimal(int(out, 16)) / Decimal(10 ** 6)
    if family == BRIDGE_FAMILY:
        try:
            return (client or _kraken_client()).balance().get("ZUSD", Decimal("0"))
        except Exception:  # noqa: BLE001
            return Decimal("0")
    if family == KRAKEN_TO_CHAIN_FAMILY:
        # Same money as family 6: the Kraken USD we buy with.
        try:
            return (client or _kraken_client()).balance().get("ZUSD", Decimal("0"))
        except Exception:  # noqa: BLE001
            return Decimal("0")
    return Decimal("0")


def _load_script(name: str):
    """Load one of this directory's runner scripts the way they are run."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "v2" + name.replace(".py", "").replace("_", ""), ROOT / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Family 2: Kraken -> chain, no bridge.
# ---------------------------------------------------------------------------
KRAKEN_TO_CHAIN_FAMILY = live.KRAKEN_TO_CHAIN
KRAKEN_TO_CHAIN_SCRIPT = "xstocks_v2_kraken_to_chain.py"


def kraken_to_chain_net_ok(row: dict, *, snapshot: dict,
                           policy: V2Policy | None = None,
                           available_usdt=None) -> bool:
    """May the direct Kraken -> chain family fire on this row?

    Same two rules as every other family: the fee must be measured (the scan
    writes ``indicative_net_usdt`` only then) and the net is judged at the size
    the wallet can pay. The withdraw fee is per withdrawal and fixed in *units*,
    so its USD value stays whatever the scan measured.
    """
    policy = policy or V2Policy.load()
    edge = _d(row.get("edge_after_requote_bps")) or _d(row.get("edge_t0_bps"))
    fee_usdt = _d(row.get("withdraw_fee_usdt"))
    notional = _d(row.get("notional_usdt"))
    if edge is None or notional is None or fee_usdt is None:
        return False
    size = costs.judged_notional(notional, available_usdt)
    if size is None or size <= 0:
        return False
    net = costs.net_after_costs(
        gross_usdt=size * edge / Decimal(10000), notional_usdt=size,
        fixed_cost_usd=fee_usdt, policy=policy, family=KRAKEN_TO_CHAIN_FAMILY,
        gas_cost_usd=Decimal(0))      # the destination is Solana, not an EVM chain
    threshold = _d(snapshot.get("threshold_bps")) or policy.min_net_edge_bps
    return costs.net_clears(edge_bps=edge, net_usdt=net, min_bps=threshold,
                            min_usdt=policy.min_net_profit_usdt)


def kraken_to_chain_candidates(snapshot: dict, *, limit: int = 5) -> list[dict]:
    """Rows for the direct family: a Kraken rail already lands on the pool chain."""
    informational = {"phase_scan", "chain_out_of_phase", "stale_or_skew"}
    out = []
    for row in snapshot.get("rows") or []:
        if row.get("family") != KRAKEN_TO_CHAIN_FAMILY:
            continue
        if str(row.get("chain")) != "solana":      # the only sell venue we run
            continue
        if set(row.get("blockers") or []) - informational:
            continue
        if not kraken_to_chain_net_ok(row, snapshot=snapshot):
            continue
        out.append(row)
    out.sort(key=lambda row: _d(row["edge_t0_bps"]), reverse=True)
    return out[:limit]


def run_kraken_to_chain_cycle(symbol: str, *, notional=None, price=None,
                              data_dir: Path | str = DATA) -> dict:
    """Drive the direct family once, through the same state machine as the CLI."""
    module = _load_script(KRAKEN_TO_CHAIN_SCRIPT)
    code = module.cycle_once(
        symbol=symbol, confirm_live=True,
        notional=str(notional if notional is not None
                     else V2Policy.load().per_trade_quote_usdt),
        price=str(price or "0") or "0")
    return {"symbol": symbol, "exit_code": code,
            "state": "sold" if code == 0 else "see_ledger"}


def _solana_spl_balance(http, mint: str) -> Decimal:
    """SPL balance in whole tokens. An unreadable wallet is 0, i.e. a refusal."""
    # One implementation, in the module: the paper runner reads the same wallet
    # and must not disagree about how much money is there (2026-10-08).
    return live.spl_balance(http, mint) or Decimal(0)


def solana_bridge_candidates(snapshot: dict, *, limit: int = 5) -> list[dict]:
    informational = {"phase_scan", "chain_out_of_phase"}
    out = []
    for row in snapshot.get("rows") or []:
        if row.get("family") != "chain_to_chain":
            continue
        if (row.get("source_chain") != SOLANA_BUY_CHAIN
                or row.get("dest_chain") != SOLANA_SELL_CHAIN):
            continue
        if set(row.get("blockers") or []) - informational:
            continue
        if not bridge_net_ok(row, snapshot=snapshot,
                             family=SOLANA_BRIDGE_FAMILY):
            continue
        out.append(row)
    out.sort(key=lambda row: _d(row["edge_t0_bps"]), reverse=True)
    return out[:limit]


def run_solana_bridge_cycle(symbol: str, *, notional=None, price=None,
                            quote: str = "auto",
                            data_dir: Path | str = DATA) -> dict:
    """Drive the Solana -> bridge -> X Layer family once.

    ``notional`` must be the buy-side money the gate used. It used to be left at
    the script default (8), so a row the gate cleared at 44 USDT was executed at
    8 -- where the fixed bridge fee makes the same row a loss.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "v2solanabridge",
        ROOT / "scripts" / "xstocks_v2_solana_bridge_roundtrip.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    code = module.cycle_once(
        symbol=symbol, bridge_min=BRIDGE_MIN, confirm_live=True, quote=quote,
        notional=str(notional if notional is not None
                     else V2Policy.load().per_trade_quote_usdt),
        price=str(price or "0") or "780")
    return {"symbol": symbol, "exit_code": code,
            "state": "sold" if code == 0 else "see_ledger"}


def via_bridge_candidates(snapshot: dict, *, limit: int = 5) -> list[dict]:
    """Chain -> hub -> Kraken rows that are ready to be walked.

    Ready means: nothing left in the gate but the informational marks, and a net
    that clears at the size the wallet can actually pay. A row whose route has
    no arrival sample yet stays out of here and is picked up by
    ``live.probe_candidates`` instead.
    """
    informational = {"phase_scan", "chain_out_of_phase"}
    out = []
    for row in snapshot.get("rows") or []:
        if row.get("family") != VIA_BRIDGE_TO_KRAKEN:
            continue
        # This slot buys on X Layer and deposits on Solana. An eth->solana row
        # is priced by the scan and has no runner here (yet); handing it to the
        # X Layer script would buy the wrong chain's token.
        if str(row.get("chain")) != SLOT_PAIR[VIA_BRIDGE_TO_KRAKEN]:
            continue
        if set(row.get("blockers") or []) - informational:
            continue
        if not bridge_net_ok(row, snapshot=snapshot,
                             family=VIA_BRIDGE_TO_KRAKEN):
            continue
        out.append(row)
    out.sort(key=lambda row: _d(row["edge_t0_bps"]), reverse=True)
    return out[:limit]


def run_via_bridge_cycle(symbol: str, *, price=None, ref_price=None,
                         notional: str | None = None,
                         data_dir: Path | str = DATA) -> dict:
    """Drive the pool chain -> hub -> Kraken family once.

    Same script as the Solana roundtrip; ``--exit kraken`` makes the last two
    legs a deposit and a taker sell instead of a Jupiter swap. The whole Solana
    balance is deposited, so an earlier probe slice is carried, not stranded.
    """
    import importlib.util

    policy = V2Policy.load()
    spec = importlib.util.spec_from_file_location(
        "v2solround", ROOT / "scripts" / "xstocks_v2_solana_roundtrip.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    code = module.cycle_once(
        symbol=symbol, notional=str(notional or policy.min_trade_quote_usdt),
        price=str(price or "0"), bridge_min=BRIDGE_MIN,
        fee_wei=SOLANA_BRIDGE_FEE_WEI, confirm_live=True,
        exit_kind=module.KRAKEN_EXIT, ref_price=None if ref_price is None else str(ref_price))
    return {"symbol": symbol, "exit_code": code,
            "state": "sold" if code == 0 else "see_ledger"}


def run_probe(symbol: str, *, price, ref_price=None, tokens,
              data_dir: Path | str = DATA) -> dict:
    """Buy the minimum, cross once, stop. The arrival unlocks the trade.

    Nothing is sold and nothing is deposited: the coins stay in our own wallet
    and the next cycle deposits them along with its own leg. ``--tokens`` is the
    whole point -- the floor that makes the coins usable at Kraken is a token
    amount, and ``spend / price`` cannot hit it exactly.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "v2solround", ROOT / "scripts" / "xstocks_v2_solana_roundtrip.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    code = module.cycle_once(
        symbol=symbol, notional=str(tokens), price=str(price),
        bridge_min=BRIDGE_MIN, fee_wei=SOLANA_BRIDGE_FEE_WEI, confirm_live=True,
        probe_only=True, tokens=str(tokens),
        ref_price=None if ref_price is None else str(ref_price))
    return {"symbol": symbol, "exit_code": code,
            "state": "probed" if code == 0 else "see_ledger"}


def probe_spent_money(summary: dict, risk: str) -> bool:
    """Did this probe really spend, whatever the cycle's final state says?

    The budget used to be charged only on ``probed``, i.e. only when the whole
    walk finished as designed. Measured 2026-10-09 12:28: a probe bought
    1.43124 USDG of QQQx on X Layer, the bridge dry-run reverted, the cycle
    ended ``stopped`` -- and the day's counter stayed at **0**. The cap exists
    to bound what a day can spend, so it has to be charged on what was spent,
    not on how the walk happened to end.

    ``risk`` is ``incident.risk()``: ``sent`` means something really left our
    hands, so the money is gone whether or not the last step succeeded.
    """
    state = str((summary or {}).get("state") or "")
    if state == "probed":
        return True
    if risk == "sent":
        return True
    return False


def realized_net_usdt(*, proceeds, capital, conversion_usdt, gas_usd):
    """``(net, parts, missing)`` for one finished cycle.

    Every line the cycle really paid, and **missing is not zero**: a cycle whose
    gas price could not be read reports ``net_usdt = None`` and names the line it
    is missing. Measured 2026-10-08: the old formula (``cost - fee - plan size``)
    published +0.63337 for a cycle that really made +0.619 USDT.
    """
    parts = {"proceeds_usdt": proceeds, "capital_usdt": capital,
             "conversion_usdt": conversion_usdt, "gas_usd": gas_usd}
    missing = [name for name, value in parts.items() if value is None]
    if missing:
        return None, parts, missing
    return proceeds - capital - conversion_usdt - gas_usd, parts, []


def _last_family_row(data_dir: Path | str, symbol, *, since_ms: int) -> dict | None:
    """The row this family just wrote for ``symbol``, if it wrote one.

    Read back instead of guessed: ``held_at`` and ``reason`` belong to the
    ledger, not to the summary the runner returns. The window keeps a previous
    cycle of the same symbol from being read as this one's result.
    """
    for entry in reversed(ledger.recent(Path(data_dir) / CYCLES.name, limit=40)):
        if str(entry.get("symbol")) != str(symbol):
            continue
        if int(entry.get("ts_ms") or 0) >= since_ms:
            return entry
    return None


def guard_family(name: str, symbol, call, *, data_dir: Path | str) -> tuple[dict, bool]:
    """Run one family's cycle; a crash becomes a ledger row, not a silent gap.

    A39 (2026-10-08): the worker families are separate scripts that a bridge or
    a probe walks. An exception inside one of them used to escape to the run
    loop, which only prints it -- so a cycle could buy, deposit and then vanish
    from the record (and the next round could buy again). Returns
    ``(summary, crashed)``.

    A51 (2026-10-09): the catch is still total, but it no longer answers one
    question with one word. A crash *before* any money-moving step started is a
    refusal -- no HALT, no lock, no phantom position -- while a crash inside or
    after such a step stays ``unknown`` with a HALT. Measured that morning: a
    probe died reading a wallet balance and the machine stopped for two hours
    with QQQx listed as held, at two chains that both held zero.
    """
    data_dir = Path(data_dir)
    started_ms = int(time.time() * 1000)
    incident.begin(family=str(name), symbol=str(symbol or "?"), data_dir=data_dir)
    try:
        summary = call()
    except BaseException as exc:  # noqa: BLE001
        moved = incident.risk() != incident.NONE
        state = ledger.UNKNOWN if moved else ledger.REFUSED
        held_at = "unknown" if moved else ""
        reason = (f"{'crash' if moved else 'crash_before_money'}:"
                  f"{type(exc).__name__}:{exc}")[:200]
        incident.finish("halted" if moved else "refused", detail=reason,
                        data_dir=data_dir)
        record = ledger.row(
            symbol=str(symbol or "?"), chain="", family=str(name),
            state=state, mode="live", size_usdt=None, held_at=held_at,
            reason=reason,
            extra={"traceback": traceback.format_exc()[-1500:],
                   "incident": incident.snapshot(),
                   "note": ("family raised; the cycle stopped and HALT was "
                            "written" if moved else
                            "family raised before any money-moving step; "
                            "nothing left the wallet, no HALT")})
        ledger.record(record, data_dir / CYCLES.name)
        if moved:
            paths = cycle_core.halt_paths(data_dir)
            paths["halt"].write_text("halt\n")
        return ({"symbol": str(symbol or "?"), "state": state, "reason": reason,
                 "held_at": held_at, "crashed": True}, True)
    entry = _last_family_row(data_dir, symbol, since_ms=started_ms)
    incident.finish("finished",
                    detail=str((summary or {}).get("state") or ""),
                    data_dir=data_dir)
    return ({**summary,
             "held_at": ledger.effective_held_at(entry) if entry else "",
             "reason": str((entry or {}).get("reason") or ""),
             "crashed": False}, False)


def _kraken_client():
    key, secret = load_credentials(ROOT / "config" / "kraken.env")
    # Same host, same proxy as v1: the private API needs the local egress.
    return KrakenSpotClient(key, secret, proxy="http://127.0.0.1:4780")


def legs():
    """v1's proven venue objects, not copies.

    Importing the v1 auto chain as a module is deliberate (spec section 3.1:
    do not write a second ETH/Solana resident). The wallet CLI, the Jupiter
    client and the Kraken client are the same ones live v1 already runs.
    """
    import xstocks_auto_live as v1
    http = v1.Http()
    return SimpleNamespace(
        v1=v1, http=http,
        # The **keyed** v2 feed, not v1's client: measured 2026-10-08, v1's
        # hard-codes api.jup.ag without a key and answers 401, so every row that
        # cleared round 2 died at ``requote_failed`` (A23, operator: fix v2 only).
        jupiter=SwapClient(JupiterFeed(Http())), solana=v1.Solana(http),
        wallet=v1.Wallet(), kraken=v1.KrakenIO(_kraken_client()))


def _seen() -> dict:
    return live.transfer_seen(TRANSFER_SEEN)


def _mark_seen(symbol: str) -> None:
    seen = _seen()
    seen[symbol] = int(time.time() * 1000)
    TRANSFER_SEEN.parent.mkdir(parents=True, exist_ok=True)
    TRANSFER_SEEN.write_text(json.dumps(seen))


def _wait_balance(solana, mint: str, before: int, *, tries: int = 12, pause: float = 4.0):
    for _ in range(tries):
        time.sleep(pause)
        now = solana.raw_balance(mint)
        if now > before:
            return now - before
    return None


def _wait_credit(kraken, symbol: str, expected: Decimal, before: Decimal, *,
                 tries: int = 60, pause: float = 15.0):
    """Credit is a quantity increase, and dust does not count (lessons 3.4)."""
    want = max(expected, Decimal("0.0000001")) * Decimal("0.99")
    for _ in range(tries):
        time.sleep(pause)
        try:
            held = Decimal(str(kraken.balance().get(f"{symbol}.T") or 0))
        except KrakenError:
            continue
        if held >= before + want:
            return held - before
    return None


def _fill_detail(client, txid: str | None) -> dict:
    if not txid:
        return {}
    try:
        row = (client._post("/0/private/QueryOrders", {"txid": txid}) or {}).get(txid)
    except KrakenError:
        return {}
    if not isinstance(row, dict):
        return {}
    return {"cost": row.get("cost"), "fee": row.get("fee"),
            "avg": row.get("price"), "vol_exec": row.get("vol_exec")}


def _sell(kraken, client, policy, *, symbol: str, tokens: Decimal,
          ref: Decimal) -> dict:
    """Taker only. v2 never rests a maker sell (spec section 8.3, lessons 3.7/3.8)."""
    # Coerce once: the wait loop multiplies ``tokens``, so a caller that passed a
    # string (a manual completion did, 2026-10-08) got a TypeError *after* the
    # order was already placed.
    tokens = Decimal(str(tokens))
    ref = Decimal(str(ref))
    for attempt in range(2):
        book = kraken.depth(symbol)
        plan = sell.plan_sell(policy, symbol=symbol, tokens=str(tokens), ref=ref,
                              bids=book.get("bids") or [],
                              asks=book.get("asks") or [], pair_status="online")
        if not plan.ok:
            return {"ok": False, "reason": plan.reason}
        price = plan.best_bid
        try:
            before = Decimal(str(kraken.balance().get(f"{symbol}.T") or 0))
        except KrakenError:
            return {"ok": False, "reason": "balance_unreadable"}
        order = client.order(symbol, str(tokens), side="sell", ordertype="limit",
                             price=f"{price:.2f}", oflags="fciq", confirm="live")
        txid = (order.get("txid") or [None])[0]
        # The order is on the book: from here a crash in the fill-polling loop
        # below is "a position we may or may not still hold", so the evidence
        # has to say so (2026-10-09; before this, only a crash *before* the
        # order was reported, and everything was reported the same way).
        incident.sent("kraken_sell", f"{symbol} {tokens} @ {price} txid={txid}")
        for _ in range(24):                      # at most two minutes
            time.sleep(5)
            try:
                held = Decimal(str(kraken.balance().get(f"{symbol}.T") or 0))
            except KrakenError:
                continue
            if held <= before - tokens * Decimal("0.9"):
                return {"ok": True, "price": price, "txid": txid,
                        **_fill_detail(client, txid)}
        try:
            for open_order in client.open_orders():
                if str((open_order.get("descr") or {}).get("pair", "")).startswith(symbol):
                    client._post("/0/private/CancelOrder", {"txid": open_order.get("id")})
        except Exception:  # noqa: BLE001
            pass
    return {"ok": False, "reason": "两次挂出都没成交"}


def _status_quote(jupiter, input_mint: str, mint: str, raw_amount: int,
                  slippage_bps: int) -> tuple[int, dict | None]:
    """``(http_status, payload)`` from a swap client, old shape or new."""
    reader = getattr(jupiter, "quote_status", None)
    if reader is not None:
        status, payload = reader(input_mint, mint, raw_amount, slippage_bps)
        return int(status or 0), payload if isinstance(payload, dict) else None
    payload = jupiter.quote(input_mint, mint, raw_amount, slippage_bps)
    if isinstance(payload, dict) and "outAmount" in payload:
        return 200, payload
    return 0, None


def requote_buy_leg(jupiter, *, input_mint: str, mint: str, raw_amount: int,
                    slippage_bps: int, tries: int = REQUOTE_TRIES,
                    sleep=time.sleep) -> tuple[dict | None, str]:
    """Round 2's buy-side quote. Retry the transient causes, refuse the rest.

    Measured 2026-10-08 13:4x: the executor's re-quote of a row that had just
    cleared round 2 came back 429 ("[API Gateway] Too many requests") and the
    cycle died at ``requote_failed``. Nothing was wrong with the pool -- the
    very next call returned 200 with ``outAmount 7550159``. The cause is
    structural: the scan, the paper runner and the live runner each pace their
    own limiter, but all three share one 1 rps free key, so they collide.

    Retrying a **read** is safe and is not the retry the spec forbids: no
    broadcast has happened yet, and the no-retry-after-broadcast rule is
    untouched. Only 429 and a request that never arrived are retried, because
    only those mean "ask again", not "this pool is dead".
    """
    tries = max(1, int(tries))
    last = "unknown"
    for attempt in range(tries):
        status, payload = _status_quote(jupiter, input_mint, mint, raw_amount,
                                        slippage_bps)
        if status == 200 and isinstance(payload, dict) and "outAmount" in payload:
            return payload, ""
        last = f"http_{status}" if status else "transport"
        if status not in (0, 429) or attempt + 1 == tries:
            break
        sleep(min(REQUOTE_BACKOFF_S * (attempt + 1), REQUOTE_BACKOFF_MAX_S))
    return None, last


def execute_cycle(row: dict, plan: dict, *, policy: V2Policy, box, data_dir: Path,
                  mode: str = "live", sol_usd=None) -> dict:
    """Buy on Solana, deposit into Kraken, sell. One cycle, one lock.

    Every step that broadcasts and cannot be confirmed ends in ``unknown``: the
    lock stays, ``data-v2/xstocks_v2_HALT`` is written, and a human reconciles.
    Nothing here retries a broadcast (spec section 8.3).
    """
    symbol = str(row["symbol"])
    universe = census_feed.solana_universe([symbol])
    mint = (universe.get(symbol) or {}).get("token_address") or row.get("token_address")
    if not mint:
        return ledger.row(symbol=symbol, chain="solana", family=live.CHAIN_TO_KRAKEN,
                          state=ledger.REFUSED, mode=mode, size_usdt=plan["size"],
                          reason="no_mint")
    paths = cycle_core.halt_paths(data_dir)
    entry = ledger.row(symbol=symbol, chain="solana", family=live.CHAIN_TO_KRAKEN,
                       state="planned", mode=mode, size_usdt=plan["size"],
                       tokens=plan["tokens"], held_at="", reason="plan")
    try:
        with cycle_core.CycleLock(paths["lock"], paths["halt"]) as lock:
            try:
                # The size the *decision* was made at: the ladder picks the largest
                # rung that still pays (operator 2026-10-08: 「25 起报，过了门槛的再
                # 往上加档」), so trading the base ticket instead would trade a trade
                # nobody judged. Falls back to the row's ticket when no rung chose.
                spend = _d(plan.get("chosen_usdt")) or row_notional(row, policy)
                # What the cycle pays in fees/gas, measured from the wallet:
                # the ledger's net has to agree with the balance change.
                sol_before = box.solana.lamports()
                conversion_cost = Decimal(0)
                # Funding, if the wallet holds the wrong stablecoin: one extra swap,
                # then the normal buy. Discovery already found the opportunity, so
                # refusing here over *which dollar we hold* is the wrong answer
                # (operator 2026-10-08: 有机会可以把币兑换).
                if plan.get("funding") in ("convert_usdt_to_usdc",
                                           "top_up_usdc_from_usdt"):
                    amount = _d(plan.get("convert_amount_usdt")) or spend
                    entry["extra"]["convert_amount_usdt"] = format(amount, "f")
                    usdt_before = (Decimal(box.solana.raw_balance(SOL_USDT))
                                   / Decimal(10 ** QUOTE_DECIMALS))
                    usdc_before = (Decimal(box.solana.raw_balance(SOL_USDC))
                                   / Decimal(10 ** QUOTE_DECIMALS))
                    conv = convert_to_usdc(box, amount, policy=policy)
                    if not conv["ok"]:
                        unknown = bool(conv.get("unknown"))
                        entry.update(
                            state=ledger.UNKNOWN if unknown else ledger.REFUSED,
                            reason=conv["reason"],
                            held_at="unknown" if unknown else "")
                        if unknown:
                            lock.hold = True
                        return entry
                    entry["extra"]["txid_convert"] = conv.get("tx")
                    entry["extra"]["usdc_after_convert"] = conv.get("usdc_raw")
                    # ``_wait_balance`` returns the first *changed* read, and a
                    # Solana RPC can serve that read from a partially updated
                    # index: measured 22:52:57 it reported 16.603536 USDC right
                    # after a swap that had really left 25.016777, and the cycle
                    # refused a ticket it could fund. Settle on the number we
                    # need (bounded) before judging.
                    funded_raw = _wait_for_at_least(
                        box.solana, SOL_USDC,
                        int(spend * 10 ** QUOTE_DECIMALS),
                        floor=int(conv.get("usdc_raw") or 0))
                    funded = Decimal(str(funded_raw)) / Decimal(10 ** QUOTE_DECIMALS)
                    entry["extra"]["usdc_settled"] = format(funded, "f")
                    usdt_after = (Decimal(box.solana.raw_balance(SOL_USDT))
                                  / Decimal(10 ** QUOTE_DECIMALS))
                    # Both legs are dollar stablecoins, so the cost is the
                    # difference between what left USDT and what arrived USDC.
                    conversion_cost = (usdt_before - usdt_after) - (funded - usdc_before)
                    if funded < spend:
                        # Say it here, plainly: the swap left less than the
                        # ticket. Letting the buy leg discover it reports the
                        # venue's ``Custom 6024`` instead of the funding fact
                        # (measured 22:47).
                        entry.update(
                            state=ledger.REFUSED,
                            reason=(f"funding_short_after_convert:"
                                    f"{format(funded, 'f')}<{format(spend, 'f')}"))
                        return entry
                    entry["extra"]["txid_convert"] = conv.get("tx")
                    entry["extra"]["usdc_after_convert"] = conv.get("usdc_raw")
                    funded = Decimal(str(conv.get("usdc_raw") or 0)) / Decimal(
                        10 ** QUOTE_DECIMALS)
                    if funded < spend:
                        # Say it here, plainly: the swap left less than the ticket.
                        # Letting the buy leg discover it reports the venue's
                        # ``Custom 6024`` instead of the funding fact (22:47).
                        entry.update(state=ledger.REFUSED,
                                     reason=f"funding_short_after_convert:"
                                            f"{format(funded, 'f')}"
                                            f"<{format(spend, 'f')}")
                        return entry
                quote, requote_note = requote_buy_leg(
                    box.jupiter, input_mint=SOL_USDC, mint=mint,
                    raw_amount=int(spend * 10 ** QUOTE_DECIMALS),
                    slippage_bps=policy.max_slippage_bps)
                if quote is None:
                    entry.update(state=ledger.REFUSED,
                                 reason=f"requote_failed:{requote_note}")
                    entry["extra"]["requote"] = requote_note
                    return entry
                spent, tokens, _route, impact = buy.parse_quote(
                    quote, swap_mint_decimals=QUOTE_DECIMALS, token_decimals=TOKEN_DECIMALS)
                entry["tokens"] = format(tokens, "f")
                entry["buy_price_usd"] = format(spent / tokens, "f")
                swap, build_note = build_swap_retry(box.jupiter, quote)
                if not swap.get("swapTransaction"):
                    # A38: the build used to be single-shot, so one 429 before the
                    # broadcast lost the whole cycle (measured 21:3x on the probe).
                    entry.update(state=ledger.REFUSED,
                                 reason=f"swap_build_failed:{build_note}")
                    entry["extra"]["build_note"] = build_note
                    return entry
                sim = box.solana.simulate(swap["swapTransaction"])
                if not sim["ok"]:
                    entry.update(state=ledger.REFUSED, reason=f"simulate:{sim['err']}")
                    return entry
                entry["extra"]["impact_bps"] = str(impact)

                before = box.solana.raw_balance(mint)
                res = box.wallet.swap(swap["swapTransaction"])
                if not res["ok"]:
                    entry.update(state=ledger.STOPPED, reason=f"buy_broadcast:{res['error']}",
                                 held_at="unknown")
                    lock.hold = True
                    return entry
                entry["extra"]["txid_buy"] = res["tx"]
                held = _wait_balance(box.solana, mint, before)
                if held is None:
                    entry.update(state=ledger.UNKNOWN, held_at="unknown",
                                 reason="bought_unconfirmed")
                    lock.hold = True
                    return entry
                tokens = Decimal(held) / Decimal(10 ** TOKEN_DECIMALS)
                entry["tokens"] = format(tokens, "f")
                entry["state"] = "bought"

                deposit = box.kraken.deposit_plan(symbol, mint, tokens)
                if not deposit.ok:
                    entry.update(state=ledger.STOPPED, held_at="wallet",
                                 reason=f"deposit_gate:{deposit.reason}")
                    lock.hold = True
                    return entry
                sent = box.wallet.send(mint, deposit.destination, deposit.raw_amount)
                if not sent["ok"]:
                    entry.update(state=ledger.STOPPED, held_at="wallet",
                                 reason=f"transfer:{sent['error']}")
                    lock.hold = True
                    return entry
                entry["extra"]["txid_deposit"] = sent["tx"]
                _mark_seen(symbol)
                try:
                    before_credit = Decimal(str(box.kraken.balance().get(f"{symbol}.T") or 0))
                except KrakenError:
                    entry.update(state=ledger.UNKNOWN, held_at="kraken",
                                 reason="credit_unreadable")
                    lock.hold = True
                    return entry
                multiplier = {
                    key: Decimal(str((value or {}).get("current") or "1"))
                    for key, value in (json.loads(
                        (ROOT / "data" / "xstocks_multipliers.json").read_text())).items()
                }.get(symbol, Decimal(1))
                credit = _wait_credit(box.kraken, symbol, tokens * multiplier, before_credit)
                if credit is None:
                    entry.update(state=ledger.UNKNOWN, held_at="kraken",
                                 reason="credit_unconfirmed")
                    lock.hold = True
                    return entry
                entry.update(state="credited", held_at="kraken",
                             extra={**entry["extra"], "credited_units": format(credit, "f")})

                sold = _sell(box.kraken, box.kraken.client, policy, symbol=symbol,
                             tokens=credit,
                             # Sell against the price the decision used (the book
                             # VWAP for our size), not against the round-1 best bid:
                             # 2026-10-08, the two were 2% apart on MCDx.
                             ref=Decimal(str(plan.get("sell_vwap")
                                             or row.get("sell_price_usd") or "0")))
                if not sold["ok"]:
                    entry.update(state=ledger.STOPPED, held_at="kraken",
                                 reason=f"sell:{sold['reason']}")
                    lock.hold = True
                    return entry
                entry.update(state=ledger.SOLD, held_at="",
                             sell_price=sold.get("avg") or sold.get("price"),
                             reason="两腿完成")
                entry["extra"]["txid_sell"] = sold.get("txid")
                if sold.get("cost") is not None:
                    cost = Decimal(str(sold["cost"]))
                    fee = Decimal(str(sold.get("fee") or "0"))
                    # Every line this cycle really paid: the capital we spent on
                    # the buy (**not** the plan's floor size), the measured cost
                    # of the stablecoin swap, and the chain's gas. Measured
                    # 2026-10-08: the old formula (cost - fee - plan size)
                    # published +0.63337 for a cycle that really made +0.619.
                    gas_lamports = sol_before - box.solana.lamports()
                    gas_usd = (None if sol_usd is None
                               else Decimal(gas_lamports) / Decimal(10 ** 9)
                               * Decimal(str(sol_usd)))
                    entry["extra"]["gas_lamports"] = gas_lamports
                    entry["extra"]["conversion_cost_usdt"] = (
                        None if conversion_cost is None
                        else format(conversion_cost, "f"))
                    net, parts, missing = realized_net_usdt(
                        proceeds=cost - fee, capital=spend,
                        conversion_usdt=conversion_cost, gas_usd=gas_usd)
                    entry["extra"]["realized"] = parts
                    if missing:
                        entry["extra"]["realized_missing"] = missing
                    entry["net_usdt"] = None if net is None else format(net, "f")
            except BaseException as exc:  # noqa: BLE001
                # A39 (2026-10-08): an exception used to leave *no* ledger row
                # and to release the cycle lock, so the next round could buy
                # again while the coins from this one sat unsold on Kraken.
                entry.update(state=ledger.UNKNOWN,
                             reason=f"crash:{type(exc).__name__}:{exc}"[:200],
                             held_at="unknown")
                entry["extra"]["stage"] = getattr(lock, "step", "")
                entry["extra"]["traceback"] = traceback.format_exc()[-1500:]
                lock.hold = True          # keep the lock + write HALT
                return entry
    except cycle_core.CycleBusy as exc:
        return ledger.row(symbol=symbol, chain="solana", family=live.CHAIN_TO_KRAKEN,
                          state=ledger.REFUSED, mode=mode, size_usdt=plan["size"],
                          reason=f"cycle_busy:{exc}")
    return entry


def plan_one(row: dict, *, policy: V2Policy, http: Http, client,
             units: dict, sol_usd: Decimal | None = None,
             multipliers: dict | None = None,
             wallets: dict | None = None,
             unreadable: list | None = None) -> dict:
    """Re-quote both legs, size the trade, and decide. Never signs.

    The cost lines are the same ones the paper runner charges, from the same
    implementations: a plan that ignores a transfer the executor will pay is
    the gate approving a trade that is not the one it priced (2026-10-08).
    """
    symbol = str(row["symbol"])
    feed = JupiterFeed(http)
    # Kraken quotes per credit unit: on Solana one on-chain token becomes
    # ``multiplier`` credits, so the sale is priced ``tokens × multiplier``.
    credit_per_token = (Decimal(1) if row.get("chain") not in SCALED_RAW_CHAINS
                        else (multipliers or {}).get(symbol, Decimal(1)))
    # The first transfer to a mint carries the account rent; later ones do not.
    first_for_mint = live.first_transfer_for(symbol, TRANSFER_SEEN)
    transfer_usdt, transfer_note = paper.transfer_cost_usdt(
        chain=str(row.get("chain")), first_for_mint=first_for_mint,
        sol_usd=sol_usd, policy=policy)
    if row.get("chain") not in SCALED_RAW_CHAINS:
        # The EVM chains pay gas instead, and that line lives in, and is
        # charged by, the family executor.
        transfer_usdt = Decimal(0)
    entry = scan.requote_buy(symbol=symbol, chain=row["chain"], policy=policy,
                             http=http, feed=feed) or {}
    depth = kraken_feed.fetch_depth(http, [symbol])
    depth_recv = int(time.time() * 1000)
    book = (depth["depth"] or {}).get(symbol) or {}
    kraken_bid = kraken_feed.book_side(book.get("bids"), side="sell")
    fresh_buy = entry.get("buy")
    buy_price = fresh_buy.price_usd if fresh_buy is not None else None
    tokens = entry.get("tokens")
    skew_ms = None if fresh_buy is None else abs(depth_recv - int(fresh_buy.recv_ms))
    # What the wallet actually holds, read once per round by the caller. It is
    # **not** a filter: the row is judged on price and net, and only the sizing
    # step asks whether one stablecoin swap has to be added (2026-10-08).
    wallets = wallets or {}
    usdc_balance = _d(wallets.get("USDC")) or Decimal(0)
    usdt_balance = _d(wallets.get("USDT")) or Decimal(0)
    conversion_bps = None
    if usdc_balance < policy.per_trade_quote_usdt and usdt_balance > 0:
        # One quote per plan, at the ceiling: the swap is priced proportionally,
        # so each rung scales this number.
        conversion_bps = conversion_cost_bps(
            feed=feed, size_usdt=policy.per_trade_quote_usdt, policy=policy)

    # Minimums, each from its own source. Unknown is not zero.
    unit = units.get(symbol) or {}
    sell_min = _d(unit.get("effective_min_units"))
    deposit_method = ""
    deposit_min = None
    try:
        methods = client.deposit_methods(symbol)
        found = dep.find_solana_method(methods, symbol)
        if found:
            deposit_method = str(found.get("method") or "")
            deposit_min = _d(found.get("minimum"))
    except KrakenError as exc:
        deposit_method = f"error:{exc.errors}"
    bridge_min = _d(entry.get("bridge_min"))  # only set once a route is measured
    floor, floor_reason = live.amount_floor(
        bridge_min=bridge_min, deposit_min=deposit_min, sell_min=sell_min)
    missing = live.unknown_minimums(bridge_min=bridge_min, deposit_min=deposit_min,
                                    sell_min=sell_min, requires_bridge=bool(bridge_min))
    # An unproven deposit goes at the floor, not at 25 USDT. Two independent
    # proofs count: v1 has actually transferred this symbol into Kraken, or the
    # credit ratio was verified on a real deposit (spec section 3.2, D3).
    proven = symbol in _seen() or unit.get(
        "deposit_credit_units_per_balance_unit") is not None
    stage = live.route_stage(measured=proven)
    size, size_reason = live.size_for(
        notional=row_notional(row, policy), balance_quote=row_notional(row, policy),
        depth=row_notional(row, policy),
        floor=floor if floor is not None else Decimal(0), price=buy_price,
        probe=stage == live.PROBE)

    # Round 2 does the sizing ladder (operator 2026-10-08): judge the base ticket
    # first, and only step up while the trade still clears. Quoting every pool at
    # 200 was how round 1 was blinded; up-sizing is what makes a 25 USDT winner
    # worth doing.
    base = row_notional(row, policy)
    ladder = live.ladder_sizes(base=base, ceiling=policy.per_trade_quote_usdt)
    ladder_notes: list[str] = []
    attempt = None
    chosen = None            # (size, tokens, buy_price, decision)
    for step in ladder:
        step_entry = entry if step == base else (scan.requote_buy(
            symbol=symbol, chain=row["chain"], policy=policy, http=http,
            feed=feed, notional=step) or {})
        step_tokens = step_entry.get("tokens")
        # The exit leg is priced on the real book for **this rung's size**, not
        # on the best bid (operator 2026-10-08: orderbook 只定价，利润说话).
        # Measured that day: a 2 USD best bid made MCDx print +203 bps on a
        # 25 USDT ticket and reach the executor.
        credits = (None if step_tokens is None
                   else step_tokens * credit_per_token)
        fill = (kraken_feed.book_fill(book.get("bids"), credits, side="sell")
                if credits is not None and credits > 0 else None)
        if fill is None or fill["vwap"] is None or not fill["ok"]:
            attempt = {"state": "refused", "reason": "kraken_depth_short",
                       "net_usdt": None, "costs": {}, "extra": {},
                       "sell_vwap": None}
            ladder_notes.append(f"{step}:refused:kraken_depth_short")
            break                      # a bigger rung cannot fill either
        sell_vwap = fill["vwap"]
        # The ticket's own slippage allowance is the slack the swap needs: a
        # top-up that produces 24.99 USDC cannot buy a 25 USDT ticket.
        slack = step * Decimal(policy.max_slippage_bps) / Decimal(10000)
        funding = funding_plan(usdc=usdc_balance, usdt=usdt_balance, need=step,
                               slack=slack)
        if funding["funding"] == "insufficient":
            # Funds, not price: the row keeps its edge on the board, and the
            # reason says which constraint stopped it (2026-10-08). A42: when a
            # wallet read failed, say *that* -- "we could not read it" and "it
            # is empty" are different answers.
            attempt = {"state": "refused",
                       "reason": ("wallet_unreadable" if unreadable
                                  else "funding_short"),
                       "net_usdt": None, "costs": {}, "extra": {},
                       "sell_vwap": format(sell_vwap, "f"),
                       "funding": funding["funding"],
                       "funding_have": {"usdc": funding.get("have_usdc"),
                                        "usdt": funding.get("have_usdt")},
                       # How much more money unlocks this row, in the shape the
                       # board prints (operator 2026-10-10: "哪条链缺多少钱").
                       "funding_gap": live.funding_gap(funding)}
            ladder_notes.append(
                f"{step}:refused:{attempt['reason']}")
            break
        conversion_usdt = Decimal(0)
        if funding["funding"] in ("convert_usdt_to_usdc", "top_up_usdc_from_usdt"):
            # Charge the swap on the amount actually swapped, not on the whole
            # ticket: a gap top-up is smaller than the ticket.
            conversion_usdt = (None if conversion_bps is None else
                               funding["convert_amount_usdt"] * conversion_bps
                               / Decimal(10000))
        attempt = paper.decide(
            row=row, policy=policy, kraken_bid=sell_vwap,
            size_usdt=step, tokens=step_tokens,
            transfer_usdt=transfer_usdt, gas_usdt=Decimal(0),
            phase_allows=True, slot_free=True, scan_fresh=True, pair_skew_ms=skew_ms,
            credit_per_token=credit_per_token,
            conversion_usdt=conversion_usdt,
            leg_note={"sell_basis": "kraken_book_vwap",
                      "kraken_best_bid": format(fill["best"], "f"),
                      "kraken_bid_at_size": format(sell_vwap, "f"),
                      "book_levels_used": fill["levels_used"],
                      "book_slip_bps": (None if fill["slip_bps"] is None
                                        else format(fill["slip_bps"], "f"))})
        attempt["sell_vwap"] = format(sell_vwap, "f")
        attempt["kraken_best_bid"] = format(fill["best"], "f")
        attempt["book_levels_used"] = fill["levels_used"]
        attempt["funding"] = funding["funding"]
        attempt["convert_amount_usdt"] = format(
            funding["convert_amount_usdt"], "f")
        attempt["conversion_usdt"] = (None if conversion_usdt is None
                                      else format(conversion_usdt, "f"))
        ladder_notes.append(f"{step}:{attempt['state']}:{attempt['reason']}")
        if attempt["state"] == "paper":
            step_buy = step_entry.get("buy")
            chosen = (step, step_tokens,
                      buy_price if step_buy is None else step_buy.price_usd, attempt)
            continue
        # A rung that fails only on the **fixed** part of the cost does not end
        # the ladder: a per-message transfer/bridge fee is amortised by size, so
        # the next rung can still pay. A rung whose *edge* is under the bar
        # cannot -- the bar is in bps, and the price only gets worse with size.
        if attempt.get("reason") == "below_net":
            continue
        break
    if chosen is not None:
        size, tokens, buy_price, decision = chosen
    elif attempt is not None:
        decision = attempt
    else:
        decision = {"state": "refused", "reason": "no_ticket",
                    "net_usdt": None, "costs": {}, "extra": {}}
    chosen_usdt = size
    size_reason = "ladder"
    funding_used = (decision.get("funding") if isinstance(decision, dict) else None)
    convert_amount = (Decimal(0) if funding_used not in
                      ("convert_usdt_to_usdc", "top_up_usdc_from_usdt")
                      else (_d(decision.get("convert_amount_usdt")) or Decimal(0)))
    return {
        "symbol": symbol, "chain": row["chain"],
        "buy_price_usd": None if buy_price is None else format(buy_price, "f"),
        "kraken_bid": None if kraken_bid is None else format(kraken_bid, "f"),
        "tokens": None if tokens is None else format(tokens, "f"),
        "pair_skew_ms": skew_ms,
        "sell_min": None if sell_min is None else format(sell_min, "f"),
        "deposit_min": None if deposit_min is None else format(deposit_min, "f"),
        "deposit_method": deposit_method,
        "amount_floor": None if floor is None else format(floor, "f"),
        "floor_reason": floor_reason,
        "missing_minimums": missing,
        "size": None if size is None else format(size, "f"),
        "size_reason": size_reason,
        "decision": decision["state"],
        "decision_reason": decision["reason"],
        "net_usdt": decision.get("net_usdt"),
        "ladder": ladder_notes,
        # The price the exit leg was judged at, so ``execute_cycle`` sells
        # against the same number the decision used (2026-10-08).
        "sell_vwap": (decision.get("sell_vwap") if isinstance(decision, dict)
                      else None),
        "kraken_best_bid": (decision.get("kraken_best_bid")
                            if isinstance(decision, dict) else None),
        "book_levels_used": (decision.get("book_levels_used")
                             if isinstance(decision, dict) else None),
        "chosen_usdt": None if chosen_usdt is None else format(chosen_usdt, "f"),
        # What the net was priced with, so the plan can be audited without
        # re-deriving it: the credit unit, the transfer line and which kind of
        # transfer the mint gets.
        "credit_per_token": format(credit_per_token, "f"),
        "first_for_mint": first_for_mint,
        "transfer_usdt": (None if transfer_usdt is None
                          else format(transfer_usdt, "f")),
        "transfer_note": transfer_note,
        # How this ticket gets funded, so the executor does not have to guess
        # and the board can say why a row that clears on price cannot fire
        # (2026-10-08: discovery never looks at the wallet).
        "funding": funding_used,
        # The shortfall itself, so an audited plan says how far off the wallet
        # was instead of only naming the state (operator 2026-10-10).
        "funding_gap": (decision.get("funding_gap")
                        if isinstance(decision, dict) else None) or {},
        "convert_amount_usdt": (None if convert_amount <= 0
                                else format(convert_amount, "f")),
        "conversion_usdt": (decision.get("conversion_usdt")
                            if isinstance(decision, dict) else None),
        "conversion_bps": (None if conversion_bps is None
                           else format(conversion_bps, "f")),
        "wallet_usdc": format(usdc_balance, "f"),
        "wallet_usdt": format(usdt_balance, "f"),
        "stage": stage,
        "proven": proven,
        "broadcast": False,
    }


def run_once(*, policy: V2Policy | None = None, data_dir: Path | str = DATA,
             confirm_live: bool = False, http: Http | None = None) -> dict:
    policy = policy or V2Policy.load()
    data_dir = Path(data_dir)
    http = http or Http()
    allowed, reason = gate(policy, data_dir)
    snapshot = read_json(data_dir / SCAN.name, {}) or {}
    rows = candidates(snapshot, chains=PLAN_CHAINS)
    client = _kraken_client()
    units = census_feed.kraken_symbols()

    # The two inputs plan_one cannot invent: the gas token's USD price (the same
    # reading the paper runner uses) and the mint multipliers (the unit the
    # sale is priced in). Both are read once per round, not once per row.
    multipliers = scan.load_multipliers()
    sol_usd = (kraken_feed.native_usd(http, ["SOL"]) or {}).get("SOL") if rows else None
    # What the wallet holds, read once per round. It only feeds the *funding*
    # step -- never the candidate list, never the ranking (2026-10-08).
    # A42: "the wallet could not be read" is a different answer from "the wallet
    # is empty", and the board has to be able to tell them apart.
    raw_stables = ({name: live.spl_balance(http, mint) for name, mint
                    in live.SOLANA_QUOTES.items()} if rows else {})
    unreadable_stables = [name for name, value in raw_stables.items()
                          if value is None]
    stables = {name: (value or Decimal(0))
               for name, value in raw_stables.items()}
    plans = [plan_one(row, policy=policy, http=http, client=client, units=units,
                      sol_usd=sol_usd, multipliers=multipliers,
                      wallets=stables, unreadable=unreadable_stables)
             for row in rows]
    executed: list[dict] = []
    #: Why the sample stage did not run this round (budget cap, above cap, or an
    #: unreadable size). ``None`` means "not blocked by the budget".
    probe_skipped: dict | None = None
    # The buy-side money decides what a trade is actually worth: a fixed bridge
    # fee on a wallet-sized ticket is a different trade from the same row at the
    # 200 USDT policy notional. Read it once per round, per family.
    availability = {family: available_quote_usdt(family, http=http)
                    for family in (BRIDGE_FAMILY, SOLANA_FAMILY,
                                   SOLANA_BRIDGE_FAMILY,
                                   VIA_BRIDGE_TO_KRAKEN,
                                   KRAKEN_TO_CHAIN_FAMILY)}
    bridge_rows = [row for row in bridge_candidates(snapshot)
                   if bridge_net_ok(row, snapshot=snapshot,
                                    family=BRIDGE_FAMILY,
                                    available_usdt=availability[BRIDGE_FAMILY])]
    solana_rows = [row for row in solana_candidates(snapshot)
                   if bridge_net_ok(row, snapshot=snapshot,
                                    family=SOLANA_FAMILY,
                                    available_usdt=availability[SOLANA_FAMILY])]
    solana_bridge_rows = [row for row in solana_bridge_candidates(snapshot)
                          if bridge_net_ok(row, snapshot=snapshot,
                                           family=SOLANA_BRIDGE_FAMILY,
                                           available_usdt=availability[
                                               SOLANA_BRIDGE_FAMILY])]
    via_bridge_rows = [row for row in via_bridge_candidates(snapshot)
                       if bridge_net_ok(row, snapshot=snapshot,
                                        family=VIA_BRIDGE_TO_KRAKEN,
                                        available_usdt=availability[
                                            VIA_BRIDGE_TO_KRAKEN])]
    direct_rows = [row for row in kraken_to_chain_candidates(snapshot)
                   if kraken_to_chain_net_ok(
                       row, snapshot=snapshot,
                       available_usdt=availability[KRAKEN_TO_CHAIN_FAMILY])]
    # Rows that are one crossing away from being tradable: a spread that clears
    # the bar, and nothing in the way except a route sample nobody has taken.
    # Recorded every round so the board can show what would be probed and why
    # not. No broadcast: buying the minimum of a route is a money-moving step
    # and it goes through the same paper-then-live rule as any other leg
    # (AGENTS.md: 新增自动执行能力前，必须先通过纸面交易).
    # The sample stage spends money, so it reads the same day-counter the board
    # shows: 3 probes a day, 2 USDT each (operator 2026-10-09). An item that the
    # budget refuses is still listed, with the reason, so the board can say why
    # nothing ran instead of going quiet.
    budget_path = data_dir / probe_core.BUDGET_NAME
    probe_budget = probe_core.budget_load(budget_path)
    probes = live.probe_candidates(snapshot, policy=policy, budget=probe_budget)
    # Family 1 on Ethereum: a separate executor, and a separate sample. The
    # trade rows are read from the same snapshot; the gas line the scan charges
    # an eth row (2026-10-09) is what makes ``below_net`` mean something here.
    eth_rows = eth_candidates(snapshot, units=units)
    eth_probes = eth_probe_candidates(snapshot, units=units)
    ton_rows = ton_candidates(snapshot)
    xlayer_rows = xlayer_candidates(snapshot)
    if allowed and confirm_live:
        # One short venue is refilled from a venue that has spare. This is one
        # move, and it takes the round's only slot, the same way a trade does.
        # A failure here is a skipped refill, not a halt of every family.
        try:
            treasury = _load_script("xstocks_v2_treasury.py")
            move = treasury.maintain(client=client, http=http, data_dir=data_dir,
                                     confirm=True)
        except Exception as exc:  # noqa: BLE001
            move = {"sent": False,
                    "reason": f"treasury:{type(exc).__name__}:{exc}"[:160]}
        if move.get("sent"):
            summary = {"symbol": str(move.get("rail") or "treasury"),
                       "state": "sent",
                       "reason": str(move.get("reason") or ""),
                       "held_at": ""}
            executed.append({"symbol": summary["symbol"],
                             "family": "treasury",
                             "state": summary["state"],
                             "reason": summary.get("reason", ""),
                             "held_at": summary.get("held_at", "")})
        directory = cycle_core.halt_paths(data_dir)["symbols"]

        def tradeable(items):
            return [row for row in items
                    if not cycle_core.symbol_halted(
                        directory, str((row or {}).get("symbol") or ""))]

        paired = [(row, plan) for row, plan in zip(rows, plans)
                  if not cycle_core.symbol_halted(
                      directory, str(row.get("symbol") or ""))]
        eth_rows = tradeable(eth_rows)
        eth_probes = tradeable(eth_probes)
        ton_rows = tradeable(ton_rows)
        xlayer_rows = tradeable(xlayer_rows)
        bridge_rows = tradeable(bridge_rows)
        direct_rows = tradeable(direct_rows)
        solana_rows = tradeable(solana_rows)
        solana_bridge_rows = tradeable(solana_bridge_rows)
        via_bridge_rows = tradeable(via_bridge_rows)
        probes = tradeable(probes)
        box = legs()
        for row, plan in paired:
            if plan["decision"] != "paper" or plan["missing_minimums"]:
                continue
            record = execute_cycle(row, plan, policy=policy, box=box,
                                   data_dir=data_dir, sol_usd=sol_usd)
            ledger.record(record, data_dir / CYCLES.name)
            executed.append(record)
            break          # one cycle at a time, always
        # Ethereum chain -> Kraken. Its own script, borrowed the same way the
        # Solana roundtrips are: the resident process and a manual ``once`` run
        # the same state machine (one signing path per venue).
        if not executed and eth_rows:
            row = eth_rows[0]
            module = _load_script(ETH_SCRIPT)
            summary, _crashed = guard_family(
                live.CHAIN_TO_KRAKEN, str(row["symbol"]),
                lambda row=row: module.cycle_summary(
                    symbol=str(row["symbol"]),
                    # No size handed in: the eth executor picks the largest rung
                    # that still pays after mainnet gas (it re-quotes at each
                    # rung). Pinning the scan's 25 USDT ticket here would keep
                    # the gas unamortised, which is the very thing that made the
                    # 2026-10-09 row look dead.
                    notional=None,
                    price=str(row.get("sell_price_usd") or "0"),
                    confirm_live=True),
                data_dir=data_dir)
            executed.append({"symbol": summary["symbol"],
                             "family": live.CHAIN_TO_KRAKEN,
                             "state": summary["state"],
                             "reason": summary.get("reason", ""),
                             "held_at": summary.get("held_at", "")})
        # The Ethereum sample: buy the deposit minimum, deposit it, and let the
        # arrival settle Kraken's credit ratio for this symbol on this rail.
        # Same day-counter as the bridge samples (3/day, 2 USDT each).
        if not executed and eth_probes:
            row = eth_probes[0]
            module = _load_script(ETH_SCRIPT)
            try:
                ctx = module.plan(SimpleNamespace(
                    symbol=str(row["symbol"]), notional=None,
                    price=str(row.get("sell_price_usd") or "0"), tokens=None,
                    probe_only=True))
            except SystemExit as exc:
                probe_skipped = {"symbol": row["symbol"], "stage": "eth_sample",
                                 "reason": f"plan:{exc}", "size_usdt": None}
            else:
                ok, why, charged = probe_core.budget_decide(
                    state=probe_budget, now_ms=int(time.time() * 1000),
                    size_usdt=ctx["notional"],
                    max_per_day=policy.probe_max_per_day,
                    max_usdt_per_probe=policy.probe_max_usdt)
                if not ok:
                    probe_skipped = {"symbol": row["symbol"], "stage": "eth_sample",
                                     "reason": why,
                                     "size_usdt": format(ctx["notional"], "f")}
                else:
                    probe_skipped = None
                    # Same reservation rule as the bridge sample above (A81):
                    # the counter moves with the money, not with the ending.
                    probe_budget = probe_core.budget_reserve(
                        probe_budget, now_ms=int(time.time() * 1000),
                        size_usdt=ctx["notional"])
                    probe_core.budget_save(budget_path, probe_budget)
                    summary, _crashed = guard_family(
                        "eth_sample", str(row["symbol"]),
                        lambda: module.run_steps(ctx), data_dir=data_dir)
                    executed.append({"symbol": summary["symbol"],
                                     "family": live.CHAIN_TO_KRAKEN,
                                     "state": summary["state"],
                                     "reason": summary.get("reason", ""),
                                     "held_at": summary.get("held_at", "")})
                    if not probe_spent_money(summary, incident.risk()):
                        probe_budget = probe_core.budget_release(
                            probe_budget, size_usdt=ctx["notional"])
                        probe_core.budget_save(budget_path, probe_budget)
        # TON chain -> Kraken. Same shape as Ethereum: its own script, one
        # signing path. The size is the ceiling when the 25 USDT quote loses
        # the fixed burn and the ceiling still pays; the script re-quotes it.
        if not executed and ton_rows:
            row = ton_rows[0]
            size = row.get("net_ceiling_size_usdt") or row_notional(row, policy)
            module = _load_script(TON_SCRIPT)
            summary, _crashed = guard_family(
                live.CHAIN_TO_KRAKEN, str(row["symbol"]),
                lambda row=row, size=size: module.cycle_once(
                    symbol=str(row["symbol"]), notional=str(size),
                    price=str(row.get("sell_price_usd") or "0"),
                    confirm_live=True),
                data_dir=data_dir)
            executed.append({"symbol": summary["symbol"],
                             "family": live.CHAIN_TO_KRAKEN,
                             "state": summary["state"],
                             "reason": summary.get("reason", ""),
                             "held_at": summary.get("held_at", "")})
        # X Layer has no Kraken deposit rail. The row is still chain -> Kraken;
        # the walk is buy, bridge to Solana, deposit, sell.
        if not executed and xlayer_rows:
            row = xlayer_rows[0]
            size = row.get("net_ceiling_size_usdt") or row_notional(row, policy)
            summary, _crashed = guard_family(
                live.CHAIN_TO_KRAKEN, str(row["symbol"]),
                lambda row=row, size=size: run_via_bridge_cycle(
                    str(row["symbol"]),
                    notional=str(size),
                    price=row.get("buy_price_usd"),
                    ref_price=row.get("sell_price_usd"),
                    data_dir=data_dir),
                data_dir=data_dir)
            executed.append({"symbol": summary["symbol"],
                             "family": live.CHAIN_TO_KRAKEN,
                             "state": summary["state"],
                             "reason": summary.get("reason", ""),
                             "held_at": summary.get("held_at", "")})
        # chain → chain has no named roundtrip. It is paper until a requoted
        # cycle clears: ``paper_once`` quotes both DEX legs and the bridge fee
        # and does not sign. A live broadcast of this family is a separate step.
        if not executed:
            route = _load_script("xstocks_v2_route.py")
            route_rows = route.candidates(snapshot)
            if route_rows:
                row = route_rows[0]
                summary, _crashed = guard_family(
                    "chain_to_chain", str(row["symbol"]),
                    lambda row=row: route.paper_once(
                        symbol=str(row["symbol"]),
                        source=str(row.get("source_chain")),
                        dest=str(row.get("dest_chain")),
                        notional=str(row.get("net_ceiling_size_usdt")
                                     or row.get("notional_usdt") or "25"),
                        bridge_fee_usd=row.get("bridge_fee_usd"),
                        gas_usd=row.get("gas_usdt")),
                    data_dir=data_dir)
                # A refused requote (no quote token, balance short) does not
                # take the slot; the other families still get their turn.
                if summary.get("state") == "paper":
                    executed.append({"symbol": summary.get("symbol", row["symbol"]),
                                     "family": "chain_to_chain",
                                     "state": "paper",
                                     "reason": summary.get("reason", ""),
                                     "held_at": summary.get("held_at", "")})
        # One cycle at a time, including across families: only reach here when
        # the chain -> Kraken family did not take the slot.
        if not executed and bridge_rows:
            summary, _crashed = guard_family(
                BRIDGE_FAMILY, bridge_rows[0]["symbol"],
                lambda: run_bridge_cycle(
                    str(bridge_rows[0]["symbol"]),
                    notional=row_notional(bridge_rows[0], policy),
                    price=bridge_rows[0].get("buy_price_usd"), data_dir=data_dir),
                data_dir=data_dir)
            executed.append({"symbol": summary["symbol"],
                             "family": BRIDGE_FAMILY, "state": summary["state"],
                             "reason": summary.get("reason", ""),
                             "held_at": summary.get("held_at", "")})
        # The direct family (Kraken -> Solana, no bridge) goes first among the
        # Kraken-funded ones: when its rail already lands on the pool chain, the
        # bridge that family 6 pays for is pure cost.
        if not executed and direct_rows:
            summary, _crashed = guard_family(
                KRAKEN_TO_CHAIN_FAMILY, direct_rows[0]["symbol"],
                lambda: run_kraken_to_chain_cycle(
                    str(direct_rows[0]["symbol"]),
                    notional=row_notional(direct_rows[0], policy),
                    price=direct_rows[0].get("buy_price_usd"), data_dir=data_dir),
                data_dir=data_dir)
            executed.append({"symbol": summary["symbol"],
                             "family": KRAKEN_TO_CHAIN_FAMILY,
                             "state": summary["state"],
                             "reason": summary.get("reason", ""),
                             "held_at": summary.get("held_at", "")})
        # ... and only when neither of the other two families took the slot.
        if not executed and solana_rows:
            summary, _crashed = guard_family(
                SOLANA_FAMILY, solana_rows[0]["symbol"],
                lambda: run_solana_cycle(
                    str(solana_rows[0]["symbol"]),
                    notional=row_notional(solana_rows[0], policy),
                    price=solana_rows[0].get("buy_price_usd"), data_dir=data_dir),
                data_dir=data_dir)
            executed.append({"symbol": summary["symbol"],
                             "family": SOLANA_FAMILY, "state": summary["state"],
                             "reason": summary.get("reason", ""),
                             "held_at": summary.get("held_at", "")})
        if not executed and solana_bridge_rows:
            summary, _crashed = guard_family(
                SOLANA_BRIDGE_FAMILY, solana_bridge_rows[0]["symbol"],
                lambda: run_solana_bridge_cycle(
                    str(solana_bridge_rows[0]["symbol"]),
                    notional=row_notional(solana_bridge_rows[0], policy),
                    price=solana_bridge_rows[0].get("buy_price_usd"),
                    data_dir=data_dir),
                data_dir=data_dir)
            executed.append({"symbol": summary["symbol"],
                             "family": SOLANA_BRIDGE_FAMILY,
                             "state": summary["state"],
                             "reason": summary.get("reason", ""),
                             "held_at": summary.get("held_at", "")})
        # The fifth family. Same one-cycle slot as the rest.
        if not executed and via_bridge_rows:
            row = via_bridge_rows[0]
            summary, _crashed = guard_family(
                VIA_BRIDGE_TO_KRAKEN, row["symbol"],
                lambda: run_via_bridge_cycle(
                    str(row["symbol"]), price=row.get("buy_price_usd"),
                    ref_price=row.get("sell_price_usd"), data_dir=data_dir),
                data_dir=data_dir)
            executed.append({"symbol": summary["symbol"],
                             "family": VIA_BRIDGE_TO_KRAKEN,
                             "state": summary["state"],
                             "reason": summary.get("reason", ""),
                             "held_at": summary.get("held_at", "")})
        # Probes go last: a trade that is ready now outranks one that unlocks a
        # route for later. A probe is the only money-moving step here that ends
        # holding the token on purpose, so it runs alone, in its own cycle.
        if not executed and probes:
            probe = probes[0]
            plan = probe.get("plan") or {}
            # The budget is a hard gate on the sample stage: 3 probes a day,
            # 2 USDT each. A refusal is recorded in the state file so the board
            # can name the cap instead of showing an empty list.
            if not probe.get("budget_ok"):
                probe_skipped = {"symbol": probe.get("symbol"),
                                 "stage": probe.get("stage"),
                                 "reason": probe.get("budget_reason"),
                                 "size_usdt": probe.get("size_usdt")}
            else:
                probe_skipped = None
            row = next((item for item in snapshot.get("rows") or []
                        if item.get("symbol") == probe["symbol"]
                        and item.get("probe")), {})
            tokens = plan.get("deposit_min_units")
            price = row.get("buy_price_usd")
            if probe.get("budget_ok") and tokens and price:
                # A81: reserve before the walk. Charging after the cycle
                # returned let a killed process spend money the day never
                # counted (2026-10-09: 1.12 USDC of TSLAx, counter 4 vs 5).
                # A reservation that does not become a spend goes back below.
                probe_budget = probe_core.budget_reserve(
                    probe_budget, now_ms=int(time.time() * 1000),
                    size_usdt=probe.get("size_usdt"))
                probe_core.budget_save(budget_path, probe_budget)
                summary, _crashed = guard_family(
                    "probe", probe["symbol"],
                    lambda: run_probe(str(probe["symbol"]), price=price,
                                      ref_price=row.get("sell_price_usd"),
                                      tokens=tokens, data_dir=data_dir),
                    data_dir=data_dir)
                executed.append({"symbol": summary["symbol"],
                                 "family": "probe", "state": summary["state"],
                                 "reason": summary.get("reason", ""),
                                 "held_at": summary.get("held_at", "")})
                # "Did it spend?" is answered by the incident evidence, not by
                # the final state: a probe that bought and then stopped at the
                # bridge still spent (2026-10-09). Only a walk that never moved
                # money gets its reservation back.
                if not probe_spent_money(summary, incident.risk()):
                    probe_budget = probe_core.budget_release(
                        probe_budget, size_usdt=probe.get("size_usdt"))
                    probe_core.budget_save(budget_path, probe_budget)
    state = {
        "updated_ms": int(time.time() * 1000),
        "phase": policy.phase, "auto_orders": policy.auto_orders,
        "gate": {"allowed": allowed, "reason": reason},
        "confirm_live": confirm_live,
        "wallet_stables": {name: format(value, "f")
                           for name, value in stables.items()},
        "candidates": len(rows),
        "bridge_candidates": len(bridge_rows),
        "solana_candidates": len(solana_rows),
        "solana_bridge_candidates": len(solana_bridge_rows),
        "via_bridge_candidates": len(via_bridge_rows),
        "kraken_to_chain_candidates": len(direct_rows),
        "eth_candidates": [row.get("symbol") for row in eth_rows],
        "eth_probe_candidates": [row.get("symbol") for row in eth_probes],
        "ton_candidates": [row.get("symbol") for row in ton_rows],
        "xlayer_candidates": [row.get("symbol") for row in xlayer_rows],
        "probe_candidates": probes,
        "probe_budget": probe_budget,
        "probe_skipped": probe_skipped,
        "available_usdt": {family: format(value, "f")
                           for family, value in availability.items()},
        "plans": plans,
        "executed": [{"symbol": row.get("symbol"), "state": row.get("state"),
                      "reason": row.get("reason"), "held_at": row.get("held_at")}
                     for row in executed],
        "broadcast": bool(executed),
        "note": ("先报价、后判定、最后才谈广播。没有 --confirm-live 或三把钥匙不齐，"
                 "这里一步都不会签名。"),
    }
    write_json(data_dir / STATE.name, state)
    return state


def status(*, policy: V2Policy | None = None, data_dir: Path | str = DATA) -> dict:
    policy = policy or V2Policy.load()
    data_dir = Path(data_dir)
    allowed, reason = gate(policy, data_dir)
    return {"phase": policy.phase, "auto_orders": policy.auto_orders,
            "live_gate": {"allowed": allowed, "reason": reason},
            "broadcast": False,
            "open_positions": [entry.get("symbol") for entry in
                               ledger.open_positions(data_dir / "xstocks_v2_cycles.jsonl")]}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    once = sub.add_parser("once")
    once.add_argument("--confirm-live", action="store_true")
    once.add_argument("--data-dir", default=str(DATA))
    run = sub.add_parser("run")
    run.add_argument("--confirm-live", action="store_true")
    run.add_argument("--interval", type=float, default=180.0)
    run.add_argument("--data-dir", default=str(DATA))
    args = parser.parse_args()
    if args.cmd == "status":
        print(json.dumps(status(data_dir=getattr(args, "data_dir", DATA)),
                         ensure_ascii=False, default=str))
        return 0
    if args.cmd == "run":
        # Resident. It re-reads the policy every round, so turning one key back
        # in ops/xstocks_v2_policy.toml stops the next round without a restart.
        while True:
            started = time.monotonic()
            try:
                state = run_once(data_dir=args.data_dir, confirm_live=args.confirm_live)
                print(json.dumps({
                    "gate": state["gate"], "candidates": state["candidates"],
                    "executed": state["executed"],
                    "stages": [plan.get("stage") for plan in state["plans"]],
                }, ensure_ascii=False), flush=True)
            except Exception as exc:  # noqa: BLE001
                print(json.dumps({"error": f"{type(exc).__name__}: {exc}"},
                                 ensure_ascii=False), flush=True)
                time.sleep(15)
                continue
            time.sleep(max(10.0, args.interval - (time.monotonic() - started)))
    state = run_once(data_dir=args.data_dir, confirm_live=args.confirm_live)
    print(json.dumps(state, ensure_ascii=False, default=str, indent=1)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
