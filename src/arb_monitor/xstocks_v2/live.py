"""Live gate, amount floor and cycle plan for chain -> Kraken.

Pure functions. The caller owns the wallet, the venue clients and the ledger;
this module answers only "may we", "how much at least", "probe or trade".

Three rules are encoded here rather than in prose:

* **Two keys plus an acknowledgement.** ``phase`` has to allow the family and
  ``auto_orders`` has to be ``live`` and ``live_ack`` has to be non-empty. One
  typo must never be the distance between a quiet scanner and a signed order.
* **The amount floor is the worst of every leg.** A transfer below any of the
  minimums is not a small trade, it is a lost one: below the bridge minimum the
  message fails, below the Kraken deposit minimum the coins never arrive, and
  below the effective order minimum the sell is rejected. The floor is the
  maximum of all of them, and an unknown minimum is not zero.
* **Probe once per route, then trade in full.** A probe is a real crossing of a
  route we have never seen arrive; it stays in our own wallet. Afterwards the
  whole amount goes in one message, because the bridge fee is per message
  (measured 2026-09-26: 0.000791242725185203 OKB per message, amount
  independent) and a second message doubles the fee and the wait.
"""
from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path

from arb_monitor.xstocks_v2.policy import V2Policy
from arb_monitor.xstocks_v2 import probe as probe_core


def _d(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None

CHAIN_TO_KRAKEN = "chain_to_kraken"
# The family this file could not drive until 2026-10-07: buy on the pool chain,
# bridge to a hub Kraken credits, deposit, sell. Kraken lists xStocks on Solana /
# Ethereum / TON only, so a deep X Layer pool is reachable this way and no other.
CHAIN_VIA_BRIDGE_TO_KRAKEN = "chain_via_bridge_to_kraken"
# The reverse direction, and the one the executor now drives:
# Kraken buy -> withdraw to a chain -> bridge -> sell on the destination chain.
KRAKEN_VIA_BRIDGE_TO_CHAIN = "kraken_via_bridge_to_chain"
# Kraken -> chain with **no bridge**: when Kraken's own rail already lands on the
# chain holding the pool, family 6 pays a message fee and waits 2-27 minutes for
# nothing. Added 2026-10-07 with its executor
# (``scripts/xstocks_v2_kraken_to_chain.py``).
KRAKEN_TO_CHAIN = "kraken_to_chain"
# The reverse direction: buy on a chain, bridge to Solana, sell there. No Kraken
# money involved, which is why it can be practised at any balance.
XLAYER_VIA_BRIDGE_TO_SOLANA = "xlayer_via_bridge_to_solana"
# The fourth direction, and the one the scan shows as positive: buy on Solana,
# bridge to X Layer, sell there. Added 2026-10-07 after the operator noticed
# that the profitable row had no executor at all.
SOLANA_VIA_BRIDGE_TO_XLAYER = "solana_via_bridge_to_xlayer"
# Families this executor knows how to drive. Everything else stays monitoring.
EXECUTABLE_FAMILIES = frozenset({CHAIN_TO_KRAKEN, KRAKEN_VIA_BRIDGE_TO_CHAIN,
                                 XLAYER_VIA_BRIDGE_TO_SOLANA,
                                 CHAIN_VIA_BRIDGE_TO_KRAKEN,
                                 SOLANA_VIA_BRIDGE_TO_XLAYER, KRAKEN_TO_CHAIN})

#: Which chains each family's executor can actually act on today. Monitoring is
#: wider than execution on purpose (AGENTS.md 代码边界: 没有执行适配器的场所照常
#: 采集行情，但候选必须标注「不可执行」及原因). Without this map the scan called a
#: positive chain -> Kraken row on eth or ton "阻挡原因：无" while the runner
#: never looked at it: 2026-10-09 the board printed SPCXx/eth uniswap 164.6544
#: -> kraken 165.9100, +76.26 bps, no blocker, and the operator asked why it did
#: not run. The runner answered "not my chain"; the board answered "nothing
#: blocks it". That is the same class of lie as A46.
# One table, in ``capability.executor_chains``. chain -> Kraken is solana, eth,
# ton and x-layer (x-layer walks buy → bridge to Solana → deposit, because
# Kraken has no X Layer rail). Do not keep a second copy of this set here.
from arb_monitor.xstocks_v2.capability import executor_chains as _executor_chains

EXECUTOR_CHAINS: dict[str, frozenset] = _executor_chains()

#: The blocker a row carries when no executor reaches its chain.
NO_EXECUTOR_CHAIN = "no_executor_chain"


def executor_chain(family: str, chain: str) -> tuple[bool, str]:
    """Can the live runner act on this ``(family, chain)`` today?

    ``(True, "")`` when it can, ``(False, NO_EXECUTOR_CHAIN)`` when the family
    has a known executor but not on this chain. A family this map has no
    opinion about returns ``(True, "")``: its rows are parked by their own
    family gate, and inventing a blocker here would hide that reason.
    """
    chains = EXECUTOR_CHAINS.get(str(family))
    if chains is None:
        return True, ""
    if str(chain) in chains:
        return True, ""
    return False, NO_EXECUTOR_CHAIN


#: The Solana stablecoins the buy leg may spend. Both are 6-decimal and both are
#: priced in USD, so either one is usable money. Measured 2026-10-07: the wallet
#: held 8.481916 USDC and **44.5405 USDT**, and the executor read only USDC --
#: so a funded route was reported as unfunded.
SOLANA_QUOTES = {
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}

#: The Solana wallet every v2 leg spends from and receives into.
SOLANA_WALLET = "AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"


def spl_balance(http, mint: str, *, wallet: str = SOLANA_WALLET) -> Decimal | None:
    """SPL balance in whole tokens, or ``None`` when the wallet cannot be read.

    Solana wraps the answer: ``{"result": {"value": [...]}}``. Reading ``value``
    off the envelope returns no rows and silently reports a funded wallet as
    empty (found 2026-10-07). One implementation, so the paper runner and the
    executor cannot disagree about how much money the wallet holds.

    A42 (2026-10-08): ``None`` and ``Decimal("0")`` are different answers and
    must stay different -- "the RPC did not answer" is not "the wallet is
    empty", and a row that says ``funding_short`` for both hides which one it
    was. Callers that only need a number use ``or Decimal(0)``.
    """
    try:
        _status, payload = http.post_json(SOLANA_RPC, {
            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
            "params": [wallet, {"mint": mint}, {"encoding": "jsonParsed"}]})
    except Exception:  # noqa: BLE001
        return None
    result = (payload or {}).get("result")
    if not isinstance(result, dict) or not isinstance(result.get("value"), list):
        return None
    rows = result["value"]
    total = 0
    for row in rows:
        info = ((row.get("account") or {}).get("data") or {}).get("parsed", {})
        total += int((info.get("info") or {}).get("tokenAmount", {}).get("amount") or 0)
    return Decimal(total) / Decimal(10 ** 6)

#: Where the wallet records the mints it has already sent to Kraken once.
TRANSFER_SEEN_NAME = "xstocks_v2_transfer_seen.json"


def transfer_seen(path) -> dict:
    """Symbols this wallet has already deposited once. Unreadable = empty."""
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def first_transfer_for(symbol: str, path) -> bool:
    """Does the next transfer to ``symbol`` pay the account rent?

    The **first** SPL transfer to a mint pays the account rent (~0.00313 SOL,
    measured); every later one is the base fee (0.000015 SOL). Charging the
    first one forever is 145 bps on a 25 USDT ticket -- enough to hide a real
    row -- and charging nothing is enough to approve one (2026-10-08). Both
    the planner and the paper runner ask this, so they cannot disagree.
    """
    return symbol not in transfer_seen(path)


def quote_asset_choice(*, usdc, usdt, requested: str = "auto") -> str:
    """Which Solana stablecoin the buy leg spends.

    ``auto`` takes the larger balance: a smaller ticket pays a larger share of
    the fixed bridge fee, so the bigger wallet is the one with headroom
    (lesson 25). A requested asset with no balance returns ``""`` -- it is
    refused, not silently replaced by the other one.
    """
    want = str(requested or "auto").upper()
    usdc = _d0(usdc)
    usdt = _d0(usdt)
    if want in SOLANA_QUOTES:
        return want if (usdc if want == "USDC" else usdt) > 0 else ""
    if want != "AUTO":
        return ""
    if usdc <= 0 and usdt <= 0:
        return ""
    return "USDC" if usdc >= usdt else "USDT"


def _d0(value) -> Decimal:
    """A balance that could not be read is zero money, never a crash."""
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return Decimal("0")

#: How many times a decision-time conversion quote may re-ask on 429. Same
#: reason as ``scan.REQUOTE_TRIES``: the three v2 processes share one 1 rps key.
CONVERT_QUOTE_TRIES = 3


def funding_plan(*, usdc, usdt, need, slack=Decimal("0")) -> dict:
    """Which stablecoin pays for the buy leg, and whether one swap is needed.

    Operator 2026-10-08: 机会发现不看钱包（「不要按钱包真有的钱选币，先找机会，
    有机会可以把币兑换」）。So this is only ever asked at *execution* time:

    * enough USDC -> nothing to do;
    * USDT only -> swap the ticket (plus ``slack``);
    * **neither alone covers it but the two together do** -> swap only the gap
      (measured 22:5x: USDC 8.41 + USDT 19.54 could not fund 25 USDT "per
      asset", and a profitable row was refused for it).

    ``slack`` covers the swap's own slippage: swapping exactly the gap leaves
    ~5 bps less USDC than the ticket, which is a second refusal a minute later.
    """
    usdc = _d0(usdc)
    usdt = _d0(usdt)
    need = _d0(need)
    if need <= 0:
        return {"funding": "none", "convert_amount_usdt": Decimal("0")}
    target = need + max(_d0(slack), Decimal("0"))
    # The three numbers every caller needs to explain a refusal. Measured
    # 2026-10-09: ``funding_short`` was one word for 4,882 rows in a day -- it
    # said the wallet could not pay for the ticket, but not which chain, how
    # much the ticket was, or how far off the wallet was. ``short_usdt`` is the
    # answer to "how much more money unlocks this row".
    detail = {"need_usdt": need, "required_usdt": target,
              "have_usdc": usdc, "have_usdt": usdt}
    if usdc >= need:
        return {**detail, "funding": "usdc", "convert_amount_usdt": Decimal("0")}
    if usdt >= target:
        return {**detail, "funding": "convert_usdt_to_usdc",
                "convert_amount_usdt": target}
    if usdc + usdt >= target:
        return {**detail, "funding": "top_up_usdc_from_usdt",
                "convert_amount_usdt": target - usdc}
    return {**detail, "funding": "insufficient",
            "convert_amount_usdt": Decimal("0"),
            "short_usdt": target - (usdc + usdt)}


def funding_gap(funding: dict | None, *, wallet_chain: str = "solana") -> dict:
    """The shortfall in the shape the runners write and the board reads.

    Empty when nothing is missing: the board must not have to re-derive money
    (it is not a second opinion), and a row that *can* be funded has no gap to
    publish. ``need_usdt`` is the ticket, ``short_usdt`` is what has to arrive
    on that chain before the row can trade at all.

    ``wallet_chain`` names the wallet the two balances were read from. Both
    callers read the Solana wallet (the buy leg of this family spends Solana
    stablecoins), while the row's own ``chain`` names where the *token* lives --
    printing one next to the other without saying so would be a new silent
    number, which is the thing this function exists to remove.
    """
    if str((funding or {}).get("funding") or "") != "insufficient":
        return {}
    short = _d0((funding or {}).get("short_usdt"))
    if short <= 0:
        return {}
    return {"need_usdt": format(_d0(funding.get("need_usdt")), "f"),
            "wallet_chain": str(wallet_chain or ""),
            "have_usdc": format(_d0(funding.get("have_usdc")), "f"),
            "have_usdt": format(_d0(funding.get("have_usdt")), "f"),
            "short_usdt": format(short, "f")}


def conversion_cost_bps(*, feed, size_usdt, policy) -> Decimal | None:
    """Measured cost of swapping ``size_usdt`` USDT into USDC, in bps.

    One implementation for the live planner and the paper runner: two copies of
    "what does the swap cost" is how the board and the gate publish two nets for
    one trade. ``None`` means the quote failed -- unknown, which is not zero, so
    the caller refuses on ``cost_unknown`` instead of trading an unpriced leg.
    """
    size = _d0(size_usdt)
    raw = int(size * Decimal(10 ** 6))
    if raw <= 0:
        return Decimal("0")
    quote = feed.quote(SOLANA_QUOTES["USDT"], SOLANA_QUOTES["USDC"], raw,
                       slippage_bps=int(getattr(policy, "max_slippage_bps", 10)),
                       tries=CONVERT_QUOTE_TRIES)
    out = _d((quote or {}).get("out_amount"))
    if out is None or out <= 0:
        return None
    got = out / Decimal(10 ** 6)
    return (size - got) / size * Decimal(10000)


PROBE = "probe"
TRADE = "trade"

#: The rule itself lives in ``probe`` now, because the board asks the same
#: question. These names stay so a reader of this file still finds them.
PROBE_CLEARABLE = probe_core.PROBE_CLEARABLE
PROBE_INFORMATIONAL = probe_core.INFORMATIONAL
PROBE_TOLERATED = probe_core.TOLERATED


def live_gate(policy: V2Policy, *, halt: bool, lock: bool, kill: bool,
              auto_halt: bool, v1_busy: bool, family: str = CHAIN_TO_KRAKEN,
              chain: str = "solana", allowed_chains=frozenset({"solana", "eth"})) -> tuple[bool, str]:
    """May this process sign right now? Fail closed on every unknown."""
    allowed, reason = policy.live_broadcast_allowed()
    if not allowed:
        return False, reason
    if family not in EXECUTABLE_FAMILIES:
        return False, f"family_not_in_live:{family}"
    if chain not in allowed_chains:
        return False, f"chain_not_in_live:{chain}"
    if kill:
        return False, "kill"
    if auto_halt:
        return False, "auto_halt"
    if halt:
        return False, "halt"
    if lock:
        return False, "lock"
    if v1_busy:
        return False, "v1_live_busy"
    return True, "ok"


def amount_floor(*, bridge_min: Decimal | None, deposit_min: Decimal | None,
                 sell_min: Decimal | None, pool_min: Decimal | None = None,
                 extra: dict | None = None) -> tuple[Decimal | None, str]:
    """The smallest amount that survives every leg. ``(amount, reason)``.

    ``reason`` names the leg that set the floor, or the leg whose minimum is
    unknown. Unknown is not zero: skipping a minimum is how a transfer becomes
    a permanent loss.
    """
    legs = {
        "bridge_min": bridge_min,
        "deposit_min": deposit_min,
        "sell_min": sell_min,
        "pool_min": pool_min,
    }
    legs.update(extra or {})
    # A leg that does not exist on this route is not a constraint.
    present = {name: value for name, value in legs.items() if value is not None}
    if not present:
        return None, "no_minimum_known"
    floor = max(present.values())
    limiting = sorted(name for name, value in present.items() if value == floor)
    return floor, "+".join(limiting)


def unknown_minimums(*, bridge_min: Decimal | None, deposit_min: Decimal | None,
                     sell_min: Decimal | None, requires_bridge: bool = False,
                     requires_deposit: bool = True,
                     requires_sell: bool = True) -> list[str]:
    """Names of the minimums this route needs but does not have."""
    missing = []
    if requires_bridge and bridge_min is None:
        missing.append("bridge_min")
    if requires_deposit and deposit_min is None:
        missing.append("deposit_min")
    if requires_sell and sell_min is None:
        missing.append("sell_min")
    return missing


def route_stage(*, measured: bool) -> str:
    """A route nobody has seen arrive gets a probe; a proven one goes straight."""
    return TRADE if measured else PROBE


def probe_decision(row: dict, *, policy: V2Policy,
                   snapshot: dict | None = None,
                   require_spread: bool = True) -> dict:
    """Resolve a row by buying the minimum and crossing once?

    The operator's rule, 2026-10-07: do not pre-sample every coin on every
    bridge. Wait until a spread is worth having, then buy the smallest amount
    that survives every leg, walk the route, and let the **arrival** unlock the
    full size. Three questions, each a refusal when it cannot be answered:

    * does the spread clear this session's bar? A probe on a losing row spends
      money to unlock a trade nobody should make;
    * is the missing route sample the *only* thing in the way?
    * does the plan name every minimum the crossing needs? An unknown minimum is
      not zero, and below Kraken's credit minimum the coins are simply lost.

    Since 2026-10-09 the rule is **one** implementation (``probe.verdict``), and
    the board reads the same one. Before that the board checked only the plan and
    the spread while this gate also checked the blockers, so a row could print
    "能跑" on the board and be refused here.

    ``require_spread=False`` is the **sample** stage (operator 2026-10-09:
    任何桥/提币路线，只要缺「这个币在这条边到账过」的样本，触发条件就是用最小
    可行额走一次试探，不是拒绝理由). The spread question is dropped then, and
    the two estimates a crossing would replace are tolerated. The budget still
    applies -- the sample stage spends money.
    """
    return probe_core.verdict(row, policy=policy, snapshot=snapshot,
                              require_spread=require_spread)


def probe_size_usdt(row: dict) -> Decimal | None:
    """What a probe on this row would spend: the deposit minimum at the price."""
    plan = row.get("probe") or {}
    tokens = _d(plan.get("deposit_min_units"))
    price = _d(row.get("buy_price_usd"))
    if tokens is None or price is None or tokens <= 0 or price <= 0:
        return None
    return tokens * price


def probe_candidates(snapshot: dict, *, policy: V2Policy | None = None,
                     limit: int = 5, budget: dict | None = None,
                     now_ms: int | None = None) -> list[dict]:
    """Every row right now that is one crossing away from being tradable.

    Two stages, in this order (operator 2026-10-09):

    1. the row's spread clears the bar and only a sample is missing -- a probe
       here is the first leg of a trade we would make anyway;
    2. if there is no such row, the **sample** stage: the route has never been
       walked for this coin and nothing worse than an estimate
       (``below_threshold`` / ``below_net``) is in the way. 缺样本不是拒绝理由.

    Both stages go through the same budget: ``budget`` is the day's counter
    state and ``policy.probe_max_per_day`` / ``policy.probe_max_usdt`` are the
    caps. An item is annotated with ``budget_ok`` and ``budget_reason`` rather
    than dropped, so the board can say *why* nothing runs.
    """
    policy = policy or V2Policy.load()
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)

    def stage(require_spread: bool) -> list[dict]:
        found: list[dict] = []
        for row in snapshot.get("rows") or []:
            # No ``probe_eligible`` pre-filter: the verdict below *is* the rule
            # (blockers, plan, spread). The flag is a rendered copy of it, and a
            # copy that can go stale is what produced the board/gate mismatch on
            # 2026-10-09 (A46/A49). One implementation, asked per row.
            decision = probe_decision(row, policy=policy, snapshot=snapshot,
                                      require_spread=require_spread)
            if not decision.get("probe"):
                continue
            size = probe_size_usdt(row)
            found.append({**decision,
                          "stage": "trade" if require_spread else "sample",
                          "symbol": row.get("symbol"),
                          "chain": row.get("chain"), "family": row.get("family"),
                          "size_usdt": None if size is None else format(size, "f"),
                          "budget_ok": False, "budget_reason": "unchecked"})
        return found

    out = stage(True) or stage(False)
    if not out:
        return []
    for item in out:
        ok, why, state = probe_core.budget_decide(
            state=budget, now_ms=now_ms, size_usdt=item.get("size_usdt"),
            max_per_day=policy.probe_max_per_day,
            max_usdt_per_probe=policy.probe_max_usdt)
        item["budget_ok"] = bool(ok)
        item["budget_reason"] = why
        item["budget"] = state
    # A runnable probe outranks a blocked one; among equals, the wider spread.
    out.sort(key=lambda item: (bool(item.get("budget_ok")),
                               _d(item.get("spread_bps")) or Decimal("-1e9")),
             reverse=True)
    return out[:limit]


def probe_amount(*, bridge_min: Decimal | None, pool_min: Decimal | None = None,
                 wallet_balance: Decimal | None = None) -> tuple[Decimal | None, str]:
    """What to send across a route we have never seen land.

    Only the bridge and pool minimums apply: the probe never goes to Kraken, so
    the deposit minimum is not a constraint, and a probe below it must never be
    forwarded (below the deposit minimum the coins are lost, and a later top-up
    does not recover the first one).
    """
    floor, reason = amount_floor(bridge_min=bridge_min, deposit_min=None,
                                 sell_min=None, pool_min=pool_min)
    if floor is None:
        return None, reason
    if wallet_balance is not None and wallet_balance < floor:
        return None, "below_floor"
    return floor, reason


def ladder_sizes(*, base: Decimal, ceiling: Decimal,
                 factors=(1, 2, 4, 8)) -> list[Decimal]:
    """Ticket ladder for round 2: base first, then doubling, capped by the ceiling.

    Operator 2026-10-08: "25 起报，过了门槛的再往上加档". Round 1 quotes the
    smallest ticket so that *many* pools get a realistic price; round 2 walks
    this ladder and keeps the **largest** size that still clears the bar, which
    is how a good small opportunity becomes a good position instead of a 25 USDT
    trade.

    A ceiling at or below the base yields just the base: there is nothing to
    step up to.
    """
    base = Decimal(str(base))
    ceiling = Decimal(str(ceiling))
    if base <= 0:
        return []
    out = []
    for factor in factors:
        size = base * Decimal(factor)
        if size > ceiling:
            break
        if size not in out:
            out.append(size)
    if not out or out[-1] != min(base, ceiling):
        out.insert(0, min(base, ceiling))
    return sorted(set(out))


def size_for(*, notional: Decimal, balance_quote: Decimal | None, depth: Decimal | None,
             floor: Decimal, price: Decimal | None,
             probe: bool = False) -> tuple[Decimal | None, str]:
    """The executable token amount, in credit units.

    ``min(notional, balance, depth)`` in quote terms, then compared with the
    amount floor. A probe is capped at the floor on purpose: the first crossing
    of an unproven route is not the place to size up.
    """
    if price is None or price <= 0:
        return None, "no_price"
    if balance_quote is None:
        return None, "balance_unknown"
    if depth is None:
        return None, "depth_unknown"
    room_quote = min(notional, balance_quote * Decimal("0.999"), depth)
    room = room_quote / price
    if probe:
        room = floor
    if room < floor:
        return None, "below_floor"
    return room, "probe" if probe else "full"


def cycle_plan(*, symbol: str, chain: str, family: str, tokens: Decimal,
               buy_price: Decimal, expected_sell_price: Decimal,
               floor: Decimal, floor_reason: str, stage: str,
               net_usdt: Decimal, deposit_method: str) -> dict:
    """Everything the executor needs, and nothing it has to guess."""
    return {
        "symbol": symbol, "chain": chain, "family": family,
        "stage": stage, "tokens": tokens,
        "amount_floor": floor, "floor_reason": floor_reason,
        "buy_price_usd": buy_price, "expected_sell_price_usd": expected_sell_price,
        "expected_net_usdt": net_usdt, "deposit_method": deposit_method,
        "one_message": stage == TRADE,
        "broadcast": False,
    }
