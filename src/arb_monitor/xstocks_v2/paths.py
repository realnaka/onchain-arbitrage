"""Any-to-any path catalogue and selection. Pure functions, no network.

The unit is a **place a position can be**, not a venue::

    node = (venue_kind, chain, form)

* ``venue_kind`` is ``kraken`` (the CEX) or a chain name (the DEX side).
* ``chain`` is ``None`` on Kraken: a Kraken balance does not live on a chain.
* ``form`` is ``native`` or ``wrapper``. **Only EVM chains have a wrapper.**
  A wrapper cannot be deposited into Kraken, and the deep X Layer pool quotes
  the wrapper while the bridge hands you the native token -- so the form is part
  of the node, not an attribute of the token.

Five leg kinds move between nodes::

    buy / sell      a spot trade on one node      (Kraken book, or a DEX pool)
    wrap / redeem   same chain, native <-> wrapper (ERC-4626, never crosses)
    bridge          chain A -> chain B (CCIP, form preserving)
    deposit         chain -> Kraken (needs a Kraken deposit rail for the network)
    withdraw        Kraken -> chain (needs an address-book key)

Enumerating ``buy_node -> sell_node`` over this graph is exhaustive by
construction: every path is a walk of these five leg kinds, and the bridge
edges come from ``routes`` (the same table the scanner uses), so a chain pair
that is missing from the CCIP registration cannot be walked here.

Selection rule (operator 2026-10-07): **cheapest first, fastest second**.
Cheaper means more headroom against the threshold, and the threshold is what
decides whether the trade is allowed at all, so cost is the primary key.
Duration is the secondary key because every extra second is a naked-leg window.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from arb_monitor.xstocks_v2 import routes as route_core

KRAKEN = "kraken"
CASH = "cash"
NATIVE = "native"
WRAPPER = "wrapper"

BUY = "buy"
SELL = "sell"
WRAP = "wrap"
REDEEM = "redeem"
BRIDGE = "bridge"
DEPOSIT = "deposit"
WITHDRAW = "withdraw"

LEG_KINDS = (BUY, SELL, WRAP, REDEEM, BRIDGE, DEPOSIT, WITHDRAW)

ALL_CHAINS = (
    "solana", "x-layer", "eth", "bsc", "arbitrum", "optimism",
    "monad", "hyperevm", "ink", "mantle", "ton",
)


def node(venue: str, chain: str | None = None, form: str = NATIVE) -> tuple:
    return (venue, route_core.norm(chain) if chain else None, form)


def kraken_node() -> tuple:
    return (KRAKEN, None, NATIVE)


def cash_node(venue: str, chain: str | None = None) -> tuple:
    """Where the *money* sits before a buy or after a sell.

    A path starts at ``cash_node(entry venue)`` and ends at
    ``cash_node(exit venue)``. Without the cash node a spot trade would be a
    self-loop on one venue and the walk could never leave it.
    """
    return (venue, route_core.norm(chain) if chain else None, CASH)


@dataclass(frozen=True)
class Leg:
    """One movement of value, with everything the ranking needs.

    ``duration_s is None`` means unknown, and unknown is **not** fast: an edge
    with no measured duration is not executable (spec 5.4).
    """

    kind: str
    source: tuple
    dest: tuple
    symbol: str
    fee_bps: Decimal = Decimal("0")
    fixed_usd: Decimal = Decimal("0")
    duration_s: Decimal | None = None
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None
    pool_id: str = ""
    measured: bool = False
    note: str = ""

    @property
    def key(self) -> tuple:
        return (self.kind, self.source, self.dest, self.symbol, self.pool_id)


@dataclass(frozen=True)
class RankedPath:
    legs: tuple[Leg, ...]
    cost_usd: Decimal
    cost_bps: Decimal
    duration_s: Decimal
    executable: bool
    blockers: tuple[str, ...]

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(leg.kind for leg in self.legs)

    @property
    def route(self) -> str:
        """A short human label: ``kraken -withdraw-> solana -bridge-> ...``."""
        if not self.legs:
            return ""
        parts = [_label(self.legs[0].source)]
        for leg in self.legs:
            parts.append(f"-{leg.kind}-> {_label(leg.dest)}")
        return " ".join(parts)


def _label(value: tuple) -> str:
    venue, chain, form = value
    if venue == KRAKEN:
        return "kraken" if form != CASH else "kraken$"
    if form == CASH:
        return f"{venue}$"
    suffix = "" if form == NATIVE else f"[{form}]"
    return f"{venue}{suffix}"


class PathGraph:
    def __init__(self, legs=()):
        self.legs: list[Leg] = list(legs)

    def add(self, leg: Leg) -> None:
        if leg.kind not in LEG_KINDS:
            raise ValueError(f"unknown leg kind {leg.kind}")
        self.legs.append(leg)

    def out_edges(self, source: tuple, symbol: str):
        return [leg for leg in self.legs if leg.source == source and leg.symbol == symbol]

    def enumerate(self, buy: tuple, sell: tuple, symbol: str, *,
                  max_legs: int = 5, allow_cex_hub: bool = False) -> list[tuple[Leg, ...]]:
        """Every walk from ``buy`` to ``sell``. Exhaustive, bounded depth.

        ``allow_cex_hub=False`` keeps the catalogue honest: routing through
        Kraken to cross chains is two withdrawals and two taker fees, and the
        spec says cross-chain goes over CCIP, not through the exchange.
        """
        found: list[tuple[Leg, ...]] = []
        seen: set[tuple] = set()
        visited = {buy}

        def walk(current: tuple, trail: tuple[Leg, ...]) -> None:
            if len(trail) > max_legs or trail in seen:
                return
            seen.add(trail)
            if current == sell and trail:
                found.append(trail)
                return
            if len(trail) == max_legs:
                return
            for leg in self.out_edges(current, symbol):
                # Kraken is not a cross-chain hub (spec 6): arriving from a
                # chain, it may only be *sold* (sell -> cash), never used to
                # withdraw onto another chain. The old rule pruned the whole
                # node, which also killed every chain -> Kraken path -- family 1
                # and family 5 were invisible to this enumerator (found
                # 2026-10-07: 0 paths ended at Kraken for any symbol).
                if (not allow_cex_hub and current[0] == KRAKEN
                        and buy[0] != KRAKEN and leg.kind == WITHDRAW):
                    continue
                if leg.dest in visited:
                    continue          # no revisit: a cycle cannot add value
                visited.add(leg.dest)
                try:
                    walk(leg.dest, trail + (leg,))
                finally:
                    visited.discard(leg.dest)

        walk(buy, ())
        return found

    def rank(self, paths, *, notional_usd: Decimal, tokens: Decimal,
             min_profit_bps: Decimal = Decimal("20"),
             max_total_duration_s: Decimal = Decimal("1800"),
             min_leg_amount: dict | None = None) -> list[RankedPath]:
        """Cheapest first, fastest second. Costed paths only."""
        ranked: list[RankedPath] = []
        for trail in paths:
            blockers: list[str] = []
            fee_bps = Decimal("0")
            fixed = Decimal("0")
            duration = Decimal("0")
            for leg in trail:
                fee_bps += leg.fee_bps
                fixed += leg.fixed_usd
                if leg.duration_s is None:
                    blockers.append(f"duration_unknown:{leg.kind}")
                else:
                    duration += leg.duration_s
                if leg.kind == BRIDGE and not leg.measured:
                    blockers.append("unmeasured_route")
                if leg.min_amount is not None and tokens < leg.min_amount:
                    blockers.append(f"below_min:{leg.kind}")
                if leg.max_amount is not None and tokens > leg.max_amount:
                    blockers.append(f"above_depth:{leg.kind}")
                if min_leg_amount and leg.kind in min_leg_amount:
                    needed = min_leg_amount[leg.kind]
                    if needed is None:
                        blockers.append(f"min_unknown:{leg.kind}")
                    elif tokens < needed:
                        blockers.append(f"below_min:{leg.kind}")
            if duration > max_total_duration_s:
                blockers.append(f"too_slow:{duration}")
            cost_usd = fixed + notional_usd * fee_bps / Decimal(10000)
            cost_bps = (cost_usd / notional_usd * Decimal(10000)) if notional_usd else Decimal("0")
            executable = not blockers and cost_bps < min_profit_bps
            ranked.append(RankedPath(
                legs=tuple(trail), cost_usd=cost_usd,
                cost_bps=cost_bps, duration_s=duration,
                executable=executable, blockers=tuple(blockers)))
        # cheapest first; on a tie the faster one; then the one with fewer legs
        ranked.sort(key=lambda row: (row.blockers != (), row.cost_bps,
                                     row.duration_s, len(row.legs)))
        return ranked


# --------------------------------------------------------------------------
# catalogue builders (data in, legs out -- keeps this file hermetic)
# --------------------------------------------------------------------------
def dex_nodes(chain: str, *, forms=(NATIVE,)) -> list[tuple]:
    chain = route_core.norm(chain)
    allowed = forms if chain in route_core.EVM else (NATIVE,)
    return [node(chain, chain, form) for form in allowed]


def bridge_legs(*, symbol: str, measured: frozenset = frozenset(),
                bridge_fixed_usd: Decimal = Decimal("0.101"),
                bridge_duration_s: Decimal | None = Decimal("180"),
                chains=None) -> list[Leg]:
    """One CCIP edge per ordered chain pair the bridge table actually has.

    ``routes.MISSING_EDGES`` is the single source of truth for the missing
    pairs, so ``solana -> mantle`` cannot appear here even by accident.
    """
    out: list[Leg] = []
    for source in (chains or ALL_CHAINS):
        src = route_core.norm(source)
        if src == "ton":
            continue                     # TON is not on this bridge
        for dest in (chains or ALL_CHAINS):
            dst = route_core.norm(dest)
            if dst == src or dst == "ton":
                continue
            if not route_core.has_bridge(src, dst):
                continue
            out.append(Leg(
                kind=BRIDGE, source=node(src, src, NATIVE),
                dest=node(dst, dst, NATIVE), symbol=symbol,
                fixed_usd=bridge_fixed_usd, duration_s=bridge_duration_s,
                measured=(src, dst) in measured,
                note=f"ccip:{route_core.dialect(src, dst)}"))
    return out


def wrap_legs(*, symbol: str, chain: str, duration_s: Decimal = Decimal("60"),
              gas_usd: Decimal = Decimal("0.01"), pool_fee_bps: Decimal = Decimal("0")) -> list[Leg]:
    """native <-> wrapper on one EVM chain. Wrappers do not exist off EVM."""
    if route_core.norm(chain) not in route_core.EVM:
        return []
    return [
        Leg(kind=WRAP, source=node(chain, chain, NATIVE),
            dest=node(chain, chain, WRAPPER), symbol=symbol,
            fixed_usd=gas_usd, duration_s=duration_s, measured=True),
        Leg(kind=REDEEM, source=node(chain, chain, WRAPPER),
            dest=node(chain, chain, NATIVE), symbol=symbol,
            fixed_usd=gas_usd, duration_s=duration_s, measured=True),
    ]


def kraken_legs(*, symbol: str, deposit_rail: dict | None,
                withdraw_rail: dict | None, taker_bps: Decimal = Decimal("10"),
                duration_buy_s: Decimal = Decimal("5"),
                duration_sell_s: Decimal = Decimal("60"),
                lot_step: Decimal | None = None) -> list[Leg]:
    """The two Kraken legs, per rail that actually exists for this symbol.

    ``deposit_rail`` / ``withdraw_rail`` are the rows the scan already reads
    (``deposit_rails`` / address book). A missing rail means no edge, not a
    zero-cost one.
    """
    out: list[Leg] = []
    kraken = kraken_node()
    kraken_cash = cash_node(KRAKEN)
    if deposit_rail:
        chain = route_core.norm(deposit_rail.get("chain"))
        out.append(Leg(
            kind=DEPOSIT, source=node(chain, chain, NATIVE), dest=kraken,
            symbol=symbol, fee_bps=Decimal("0"),
            duration_s=Decimal(str(deposit_rail.get("duration_s", 60))),
            min_amount=_dec(deposit_rail.get("minimum")), measured=True,
            note=f"kraken deposit rail: {deposit_rail.get('method')}"))
    out.append(Leg(
        kind=BUY, source=kraken_cash, dest=kraken, symbol=symbol,
        fee_bps=taker_bps, duration_s=duration_buy_s, measured=True,
        note="kraken taker"))
    out.append(Leg(
        kind=SELL, source=kraken, dest=kraken_cash, symbol=symbol,
        fee_bps=taker_bps, duration_s=duration_sell_s, measured=True,
        note="kraken taker"))
    if withdraw_rail:
        chain = route_core.norm(withdraw_rail.get("chain"))
        out.append(Leg(
            kind=WITHDRAW, source=kraken, dest=node(chain, chain, NATIVE),
            symbol=symbol, fixed_usd=_dec(withdraw_rail.get("fee_usd")) or Decimal("0"),
            duration_s=Decimal(str(withdraw_rail.get("duration_s", 120))),
            min_amount=_dec(withdraw_rail.get("minimum")), measured=True,
            note=f"kraken withdrawal: {withdraw_rail.get('key')}"))
    return out


def dex_trade_legs(*, symbol: str, chain: str, form: str = NATIVE,
                   buy_fee_bps: Decimal = Decimal("5"),
                   sell_fee_bps: Decimal = Decimal("5"),
                   buy_duration_s: Decimal = Decimal("3"),
                   sell_duration_s: Decimal = Decimal("3"),
                   pool_depth_tokens: Decimal | None = None,
                   max_depth_share: Decimal = Decimal("0.2"),
                   quote_symbol: str = "") -> list[Leg]:
    """Buy/sell legs at one chain venue, in one token form.

    The size cap is the pool depth times ``max_depth_share`` (spec 5.2): the
    deep X Layer pool is a MILLION dollars, so this is what keeps a 200 USDT
    order from becoming the market.
    """
    chain = route_core.norm(chain)
    venue = node(chain, chain, form)
    money = cash_node(chain, chain)
    cap = (pool_depth_tokens * max_depth_share) if pool_depth_tokens else None
    return [
        Leg(kind=BUY, source=money, dest=venue, symbol=symbol,
            fee_bps=buy_fee_bps, duration_s=buy_duration_s, max_amount=cap,
            measured=True, note=f"dex buy {quote_symbol}".strip()),
        Leg(kind=SELL, source=venue, dest=money, symbol=symbol,
            fee_bps=sell_fee_bps, duration_s=sell_duration_s, max_amount=cap,
            measured=True, note=f"dex sell {quote_symbol}".strip()),
    ]


def full_catalog(*, symbol: str, chains=None, measured: frozenset = frozenset(),
                 bridge_fixed_usd: Decimal = Decimal("0.101"),
                 bridge_duration_s: Decimal | None = Decimal("180"),
                 deposit_rail: dict | None = None,
                 withdraw_rail: dict | None = None,
                 kraken_taker_bps: Decimal = Decimal("10"),
                 native_chains=None) -> PathGraph:
    """The whole any-to-any catalogue for one symbol.

    Every chain gets native + wrapper nodes (EVM only), wrap/redeem on EVM, the
    CCIP edges the bridge registration actually has, and a Kraken buy/sell pair.
    Deposit and withdrawal rails are symbol specific and get added by the
    caller, because they need the live Kraken tables.
    """
    graph = PathGraph()
    # Chains whose NATIVE token has a quoteable pool. X Layer is not one of them
    # (measured 2026-10-07: census only has the wrapper pool), so a native sell
    # there is not a path the catalogue may offer.
    native = set(native_chains) if native_chains is not None else set(
        route_core.norm(c) for c in (chains or ALL_CHAINS))
    for chain in (chains or ALL_CHAINS):
        chain = route_core.norm(chain)
        forms = (NATIVE, WRAPPER) if route_core.norm(chain) in route_core.EVM else (NATIVE,)
        if chain in native:
            graph.add(Leg(kind=BUY, source=cash_node(chain, chain),
                          dest=node(chain, chain, NATIVE), symbol=symbol,
                          measured=True, duration_s=Decimal("3")))
            graph.add(Leg(kind=SELL, source=node(chain, chain, NATIVE),
                          dest=cash_node(chain, chain), symbol=symbol,
                          measured=True, duration_s=Decimal("3")))
        for form in forms:
            if form == NATIVE:
                continue
            graph.add(Leg(kind=BUY, source=cash_node(chain, chain),
                          dest=node(chain, chain, form), symbol=symbol,
                          measured=True, duration_s=Decimal("3")))
            graph.add(Leg(kind=SELL, source=node(chain, chain, form),
                          dest=cash_node(chain, chain), symbol=symbol,
                          measured=True, duration_s=Decimal("3")))
        for leg in wrap_legs(symbol=symbol, chain=chain):
            graph.add(leg)
    for leg in bridge_legs(symbol=symbol, measured=measured, chains=chains,
                           bridge_fixed_usd=bridge_fixed_usd,
                           bridge_duration_s=bridge_duration_s):
        graph.add(leg)
    for leg in kraken_legs(symbol=symbol, deposit_rail=deposit_rail,
                           withdraw_rail=withdraw_rail, taker_bps=kraken_taker_bps):
        graph.add(leg)
    return graph


def _dec(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def d(amount) -> Decimal:
    return Decimal(str(amount))
