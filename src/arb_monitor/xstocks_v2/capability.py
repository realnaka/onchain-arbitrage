"""One verdict per any-to-any edge. Pure. Does not sign or broadcast.

Every ordered chain pair lands in exactly one of:

* ``ready`` — the constructor can build the call, and a wallet we hold can sign it
* ``no_lane`` — the issuer bridge has no such edge (including every TON pair)
* ``no_selector`` — the edge is registered, but no chain selector has been read
* ``no_adapter`` — the constructor exists, and no wallet we hold can sign the source
* ``no_rail`` — Kraken has no deposit or withdrawal on that chain

``no_lane`` is not a missing script. Building a send for it would lose the coins.
"""
from __future__ import annotations

from decimal import Decimal

from arb_monitor.xstocks_v2 import routes
from arb_monitor.xstocks_v2.ccip_evm import (
    DEST_SELECTORS, SOURCE_SELECTORS, SOURCE_SLOT0)
from arb_monitor.xstocks_v2.ccip_solana import solana_dest_selector
from arb_monitor.xstocks_v2.paths import ALL_CHAINS

READY = "ready"
NO_LANE = "no_lane"
NO_SELECTOR = "no_selector"
NO_ADAPTER = "no_adapter"
NO_RAIL = "no_rail"
STATUSES = frozenset({READY, NO_LANE, NO_SELECTOR, NO_ADAPTER, NO_RAIL})

#: Chains whose DEX buy and sell we can sign today.
DEX_ADAPTERS = frozenset({"solana", "x-layer", "eth", "ton"})
#: EVM chains the wallet can sign. HyperEVM's wallet name is ``evm_hype``
#: (``scripts/xstocks_v2_edge_send.py``). A chain that is not here keeps its
#: constructor and still cannot broadcast.
EVM_BROADCAST = frozenset({
    "x-layer", "eth", "bsc", "arbitrum", "optimism", "ink", "hyperevm",
})
#: Kraken deposit / withdraw scripts that exist. X Layer is not among them.
KRAKEN_DEPOSIT = frozenset({"solana", "eth", "ton"})
KRAKEN_WITHDRAW = frozenset({"solana", "eth", "ton"})

#: Pairs the older roundtrip scripts already drive. The generic route must not
#: take their slot.
COVERED_PAIRS = frozenset({("x-layer", "solana"), ("solana", "x-layer")})

#: One proof send stays under this. Above it, the edge is recorded and not sent.
PROOF_CAP_USDT = Decimal("2")

CHAIN_TO_KRAKEN = "chain_to_kraken"
KRAKEN_VIA_BRIDGE_TO_CHAIN = "kraken_via_bridge_to_chain"
XLAYER_VIA_BRIDGE_TO_SOLANA = "xlayer_via_bridge_to_solana"
SOLANA_VIA_BRIDGE_TO_XLAYER = "solana_via_bridge_to_xlayer"
CHAIN_VIA_BRIDGE_TO_KRAKEN = "chain_via_bridge_to_kraken"
KRAKEN_TO_CHAIN = "kraken_to_chain"


def executor_chains() -> dict[str, frozenset]:
    """The chains each named family may sign. Includes the four chain → Kraken hubs."""
    return {
        CHAIN_TO_KRAKEN: frozenset({"solana", "eth", "ton", "x-layer"}),
        KRAKEN_VIA_BRIDGE_TO_CHAIN: frozenset({"solana"}),
        XLAYER_VIA_BRIDGE_TO_SOLANA: frozenset({"x-layer"}),
        SOLANA_VIA_BRIDGE_TO_XLAYER: frozenset({"solana"}),
        CHAIN_VIA_BRIDGE_TO_KRAKEN: frozenset({"x-layer"}),
        KRAKEN_TO_CHAIN: frozenset({"solana"}),
    }


def _norm(chain: str) -> str:
    return routes.norm(chain)


def bridge_status(source: str, dest: str) -> str:
    """Verdict for one directed bridge edge."""
    src, dst = _norm(source), _norm(dest)
    if not src or not dst or src == dst or src == "ton" or dst == "ton":
        return NO_LANE
    if not routes.has_bridge(src, dst):
        return NO_LANE
    kind = routes.dialect(src, dst)
    if kind in ("no_edge", "unknown"):
        return NO_LANE
    if kind == "evm_to_solana":
        if src not in SOURCE_SELECTORS or src not in SOURCE_SLOT0:
            # Slot 0 of the 11 accounts changes with the source chain. Optimism
            # has no execution to read it from, and a guessed account is how
            # coins get locked. Missing is the same refusal as a missing selector.
            return NO_SELECTOR
        if src not in EVM_BROADCAST:
            return NO_ADAPTER
        return READY
    if kind == "evm_to_evm":
        if src not in SOURCE_SELECTORS or DEST_SELECTORS.get(dst) is None:
            return NO_SELECTOR
        if src not in EVM_BROADCAST:
            return NO_ADAPTER
        return READY
    if kind == "solana_source":
        if solana_dest_selector(dst) is None:
            return NO_SELECTOR
        return READY
    return NO_LANE


def leg_status(kind: str, source: str, dest: str = "") -> str:
    """Verdict for one non-bridge leg. ``source`` is the chain the leg signs on."""
    src = _norm(source)
    name = str(kind or "")
    if name == "bridge":
        return bridge_status(source, dest)
    if name in ("buy", "sell"):
        return READY if src in DEX_ADAPTERS else NO_ADAPTER
    if name in ("wrap", "redeem"):
        if src in ("solana", "ton", ""):
            return NO_LANE
        if src not in routes.EVM:
            return NO_LANE
        return READY if src in EVM_BROADCAST else NO_ADAPTER
    if name == "deposit":
        return READY if src in KRAKEN_DEPOSIT else NO_RAIL
    if name == "withdraw":
        return READY if src in KRAKEN_WITHDRAW else NO_RAIL
    return NO_LANE


def all_bridge_pairs() -> list[tuple[str, str, str]]:
    """``(source, dest, status)`` for every ordered pair of ``ALL_CHAINS``."""
    out = []
    for source in ALL_CHAINS:
        for dest in ALL_CHAINS:
            if source == dest:
                continue
            out.append((source, dest, bridge_status(source, dest)))
    return out


def compose(source: str, dest: str) -> dict:
    """The step names a ready bridge path would sign. Never a broadcast.

    A path that is not ``ready`` has no steps and ``broadcast`` false, so a
    caller cannot turn ``no_lane`` into a transaction by forgetting a check.
    """
    status = bridge_status(source, dest)
    if status != READY:
        return {"status": status, "steps": (), "broadcast": False,
                "dialect": routes.dialect(source, dest)}
    steps = []
    if _norm(source) in DEX_ADAPTERS:
        steps.append("buy")
    steps.append("bridge")
    if _norm(dest) in DEX_ADAPTERS:
        steps.append("sell")
    return {"status": READY, "steps": tuple(steps), "broadcast": False,
            "dialect": routes.dialect(source, dest)}


def may_broadcast(cost_usdt) -> tuple[bool, str]:
    """A proof send is allowed only when the quoted cost is known and ≤ 2 USDT."""
    if cost_usdt in (None, ""):
        return False, "cost_unknown"
    try:
        cost = Decimal(str(cost_usdt))
    except (ArithmeticError, ValueError):
        return False, "cost_unknown"
    if cost > PROOF_CAP_USDT:
        return False, "over_cap"
    return True, ""


def attempt(*, status: str, cost_usdt, broadcast) -> dict:
    """Call ``broadcast`` only for a ready edge inside the cap.

    Every other verdict returns without calling it. Tests pass a function that
    records whether it ran.
    """
    if status != READY:
        return {"sent": False, "reason": status}
    allowed, why = may_broadcast(cost_usdt)
    if not allowed:
        return {"sent": False, "reason": why}
    broadcast()
    return {"sent": True, "reason": ""}
