"""Which venue keeps showing the arbitrage? A per-venue tally over recorded cycles.

Operator's request, 2026-09-16: "你持续监控这几个交易所的合约价格，我要统计哪个
交易所出现的套利机会最多，然后就先接那个交易所".

The monitor already records one ``basis_evaluation`` row per instrument per cycle,
including the ones that were rejected, so this tally is a read over data that
already exists: no part of the live loop changes and the answer can be recomputed
for any window after the fact.

Three things it deliberately does *not* do:

* **It does not rank by raw row counts.** Bybit and Bitget joined on 2026-09-16
  21:03 while Gate has rows back to 09-13, so a raw count ranks *monitoring age*,
  not opportunity. Ranking is by rate over the cycles in which the pair was
  actually evaluable, and the ``common_*`` figures restrict every venue to the
  cycles where all of them were sampled.
* **It does not treat "the pair was priced" as "there was an opportunity".**
  A row counts as an *opportunity* only when the gross entry basis in our
  direction (buy spot, short the perp) clears the threshold, and as a *candidate*
  only when it also clears the cost model — the monitor's own definition.
* **It does not promote anything the executor would refuse.** A venue with no
  execution adapter, or a pair with an unverified share mapping, is ranked and
  labelled; ``executable`` comes from the execution allowlist, never from this
  module's opinion.

Row shape read from the database (one per instrument per cycle)::

    {"run_id", "recv_ts_ms", "asset", "venue", "instrument",
     "gross_bps": Decimal | None, "rejections": [str], "phase": str,
     "evaluated_at_ms": int | None,
     "cost_quote": Decimal | None, "spot_open_quote": Decimal | None,
     "profit_quote": Decimal | None, "spot_quantity": Decimal | None,
     "contracts": Decimal | None, "status": str}

``gross_bps`` is the recorded ``entry_basis_bps``: ``(perp_open - spot_open) /
spot_open``, i.e. **positive means our direction pays**, which is the sign the
screen already uses (``xstocks_screen.screen_direction``). ``None`` means the pair
could not be evaluated at all that cycle (missing/stale book, non-simultaneous
quotes, unverified share mapping, quanto payout, ...), and the reasons are
collected under ``blocked`` instead of being silently dropped.

``evaluated_at_ms`` is when *this pair* was judged. It is written by the
streaming evaluator and by nothing else, so ``None`` marks a row from the older
regime that only ran once every probe of the cycle had returned; the tally
counts those separately and excludes them (see ``build_scoreboard``).

The four ``*_quote`` fields are the same row's **money**, not its bps, and they
are already in the database: the strategy writes ``roundtrip_cost_usdt`` (the
full four-leg cost at probe size), ``spot_open_usdt`` (the notional the probe
would spend), ``conditional_profit_usdt`` (entry basis minus the assumed exit
basis minus that cost) and ``spot_quantity``. Reading them back is what lets the
tally report 金额/利润/成本 as USDT instead of extrapolating everything from bps.
"""
from __future__ import annotations

import json
import sqlite3
from decimal import ROUND_DOWN, Decimal

DEFAULT_MONITOR_DB = "data/hk_monitor.db"
BPS = Decimal(10000)

# The monitor's own entry bar, in bps, and the reviewed cost model it is compared
# against (`config/hk_monitor.toml [costs]`: 5 perp + 5 slippage + 5 failure).
# Kept as arguments so a scenario can be recomputed without editing the code.
DEFAULT_MIN_GROSS_BPS = Decimal("20")
DEFAULT_COST_BPS = Decimal("15")
DEFAULT_MIN_NET_BPS = Decimal("20")
# A bps edge is not a trade. `config/hk_monitor.toml` also carries
# `min_net_profit_quote = 1` (USDT) and the gray-scale cap is 10 USDT per trade,
# so a 180 bps dislocation on a 10 USDT position is worth 0.18 USDT — real, but
# 5x short of the project's own minimum-profit bar. The tally therefore reports
# the notional a venue's opportunities would need to clear that bar, and how many
# of them fit inside the cap actually in force. Without this the ranking answers
# "where is the biggest number" instead of "where is something we could do".
DEFAULT_MIN_PROFIT_QUOTE = Decimal("1")
DEFAULT_TRADE_QUOTE_USDT = Decimal("10")

# The two per-leg bps constants below are a *fallback*, not the model. Every
# `basis_evaluation` row that got past the gas branch already carries the cost
# the monitor itself charged it (`roundtrip_cost_usdt`, from
# `RoundTripCosts.total`), so the tally reads that number when it is present and
# only falls back to `DEFAULT_COST_BPS` for rows that never reached the cost
# model.
#
# Why this matters (measured 2026-09-17, 8h window, 5,175 evaluable rows): the
# recorded cost is **50.1 bps** of spot_open on every venue, while
# `DEFAULT_COST_BPS = 15` was being subtracted instead. The 15 came from reading
# `[costs]` as "5 perp + 5 slippage + 5 failure, levied once on the position",
# but `RoundTripCosts.total` levies slippage+failure on **all four** legs'
# turnover and the perp fee on the perp pair, so a round trip costs ≈50 bps of
# the spot notional, not 15. Using 15 answered "how big is the number" with a
# cost nobody pays. See docs/venue-opportunity-scoreboard-2026-09-17.md §2.
DEFAULT_COST_MODE = "recorded"

# What one entry may actually deploy: min(gray-scale ceiling, wallet x 0.999),
# floored to whole shares. The wallet is usually the binding constraint (it was
# 10.92 USDG against a 100 USDT ceiling on 2026-09-17), and a tally that ignored
# it would report a size no trade could reach. `None` means "wallet unknown", in
# which case only the ceiling is applied and the report says so.
DEFAULT_WALLET_QUOTE_USDT: Decimal | None = None
# 0.999 = leave a hair of the balance behind so the exact-out route's rounding
# cannot ask for more USDG than the wallet holds. Same factor as
# `grey_scale.affordable_trade_quote` in `account-status`.
WALLET_SAFETY_FACTOR = Decimal("0.999")

# The exit assumption the profit estimate is built on. `config/hk_monitor.toml
# [scenario] target_exit_basis_bps` is the operator's number ("hold until the net
# basis converges back to <= target, then sell spot and buy back the short"), and
# `conditional_profit_usdt` in every recorded row was computed against it. A
# profit figure here is therefore a *scenario* for the same exit the monitor
# assumed, not a forecast that convergence happens.
DEFAULT_TARGET_EXIT_BPS = Decimal("3")

# Rejections that say "this observation is not trustworthy", as opposed to "the
# economics do not work". `hk_basis.evaluate()` records a basis even when it
# flags one of these, so a bps-only tally silently counts a stale-priced row as
# an opportunity — measured 2026-09-17, one cycle had 39 of 39 rows carrying
# `missing_or_stale_*` while still producing an `entry_basis_bps`. Counting them
# would answer "where is the biggest number" with numbers the monitor itself
# refuses to act on. `outside_strategy_window` is deliberately NOT here: it is
# the strategy's session policy, and excluding it would erase the session /
# off-hours split this report is built on.
DATA_QUALITY_REJECTIONS = frozenset({
    "missing_or_stale_perp_book",
    "missing_or_stale_spot_buy_quote",
    "missing_or_stale_spot_sell_quote",
    "non_simultaneous_quotes",
    "unverified_or_expired_full_roundtrip_costs",
    "spot_asset_direction_chain_mismatch",
})

# The monitor records a session phase on every evaluation. Splitting the tally
# along it turned out to matter more than any other cut: the HK cash market being
# open or shut changes both the size and the *meaning* of a basis, so a venue that
# was only ever sampled after the close cannot be compared with one sampled
# through a session. Measured 2026-09-16: every Gate/Bybit/Bitget row in the first
# hours after they joined was `after_close`, where KUAISHOU sat at +90…+150 bps for
# hours, while OKX's opportunities sat in `continuous_am`/`continuous_pm`.
SESSION_PHASES = ("continuous_am", "continuous_pm", "lunch", "closing_auction")
OFF_HOURS_PHASES = ("preopen_0900_0930", "before_0900", "after_close")

PHASE_LABELS = {
    "session": "交易时段",
    "off_hours": "盘前/盘后",
    # Everything the calendar cannot place inside a trading day: weekends,
    # holidays, and (in principle) an unrecognised phase string. Both legs of
    # the trade quote 24/7, so this bucket is a first-class reading, not a
    # leftover — see AGENTS.md "全天候原则".
    "other": "其他/休市",
}


def phase_group(phase: str | None) -> str:
    if phase in SESSION_PHASES:
        return "session"
    if phase in OFF_HOURS_PHASES:
        return "off_hours"
    return "other"


def _decimal(value) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _int_or_none(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def load_rows(db_path: str = DEFAULT_MONITOR_DB, *, since_ms: int | None = None,
              until_ms: int | None = None) -> list[dict]:
    """Every recorded basis evaluation in the window, evaluable or not.

    Opened read-only so a report can never become a writer of the live monitor
    database. The ``(kind, recv_ts_ms)`` index does the window filtering, which is
    what keeps a 4-day-old, multi-gigabyte database fast enough to re-read on
    demand.
    """
    sql = ["select run_id, recv_ts_ms, asset, venue, payload_json",
           "from monitor_events where kind = 'basis_evaluation'"]
    params: list = []
    if since_ms is not None:
        sql.append("and recv_ts_ms >= ?")
        params.append(int(since_ms))
    if until_ms is not None:
        sql.append("and recv_ts_ms < ?")
        params.append(int(until_ms))
    rows: list[dict] = []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for run_id, recv_ts_ms, asset, venue, payload in conn.execute(
                " ".join(sql), params):
            try:
                data = json.loads(payload)
            except (TypeError, ValueError):
                continue
            instrument = data.get("instrument") or f"{venue}:{asset}"
            rejections = data.get("rejections") or []
            rows.append({
                "run_id": run_id,
                "recv_ts_ms": int(recv_ts_ms),
                "asset": asset,
                "venue": venue,
                "instrument": instrument,
                "gross_bps": _decimal(data.get("entry_basis_bps")),
                "rejections": [str(tag) for tag in rejections],
                "phase": data.get("phase"),
                # The moment THIS pair was judged. Written by the streaming
                # evaluator only; `None` marks a row from the older
                # wait-for-the-whole-cycle regime, which the tally excludes.
                "evaluated_at_ms": _int_or_none(data.get("evaluated_at_ms")),
                # The money side of the same row. Absent on rows that never
                # reached the cost model (a rejected pair stops before the
                # gas/cost branch), which is exactly when the caller should fall
                # back to its bps constant.
                "cost_quote": _decimal(data.get("roundtrip_cost_usdt")),
                "spot_open_quote": _decimal(data.get("spot_open_usdt")),
                "profit_quote": _decimal(data.get("conditional_profit_usdt")),
                "spot_quantity": _decimal(data.get("spot_quantity")),
                "contracts": _decimal(data.get("contracts")),
                "status": data.get("status"),
            })
    finally:
        conn.close()
    return rows


def executable_pairs(venues, instruments) -> set[str]:
    """The ``venue:instrument`` pairs the unattended executor would accept.

    The execution gate checks the venue allowlist and the instrument allowlist
    separately, so the accepted set is their product — the same way
    ``auto_execute`` reasons about it.
    """
    return {f"{venue}:{instrument}"
            for venue in venues for instrument in instruments}


def _median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _deployable(budget: Decimal, price_per_share: Decimal) -> tuple[Decimal, Decimal]:
    """Whole shares a budget buys, and what they cost.

    Whole shares, not a fractional notional, because the exact-out spot quote is
    a share count and a fractional share count can never be hedged by an integer
    number of contracts (same reasoning as `[monitor] probe_shares`). Returning
    the count as well as the spend keeps the "shares x price" identity visible
    instead of silently reporting a notional no whole share count reaches.
    """
    if price_per_share <= 0 or budget <= 0:
        return Decimal(0), Decimal(0)
    shares = (budget / price_per_share).to_integral_value(rounding=ROUND_DOWN)
    return shares, shares * price_per_share


def _rate(numerator: int, denominator: int) -> Decimal | None:
    if not denominator:
        return None
    return (Decimal(numerator) / Decimal(denominator) * 100).quantize(Decimal("0.1"))


def _sum(values: list[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) if values else None


def _max(values: list[Decimal]) -> Decimal | None:
    return max(values) if values else None


def _q(value: Decimal | None, places: int) -> str | None:
    """A Decimal as a fixed-point string, or None when there is nothing to show."""
    if value is None:
        return None
    return str(Decimal(value).quantize(Decimal(1).scaleb(-places)))


def _trim(value: Decimal) -> str:
    """A Decimal without trailing zeros, and never in exponent form.

    `Decimal("10.000").normalize()` is `1E+1`, which is unreadable in a report
    that is meant to be pasted into a doc, so the exponent form is formatted
    back to plain fixed point.
    """
    return format(Decimal(value).normalize(), "f")


def build_scoreboard(rows: list[dict], *,
                     min_gross_bps: Decimal = DEFAULT_MIN_GROSS_BPS,
                     cost_bps: Decimal = DEFAULT_COST_BPS,
                     cost_mode: str = DEFAULT_COST_MODE,
                     min_net_bps: Decimal = DEFAULT_MIN_NET_BPS,
                     min_profit_quote: Decimal = DEFAULT_MIN_PROFIT_QUOTE,
                     trade_quote_usdt: Decimal = DEFAULT_TRADE_QUOTE_USDT,
                     wallet_quote_usdt: Decimal | None = DEFAULT_WALLET_QUOTE_USDT,
                     target_exit_basis_bps: Decimal = DEFAULT_TARGET_EXIT_BPS,
                     executable: set[str] | frozenset[str] = frozenset(),
                     include_legacy: bool = False,
                     now_ms: int | None = None) -> dict:
    """Aggregate recorded **evaluations** into a per-venue tally.

    The unit is one ``basis_evaluation`` row, not one monitoring cycle.

    Why the unit changed (operator, 2026-09-17 evening: "按新的不用等所有价格
    返回就判断是否机会成立的方式来统计"): a cycle stops being a moment as soon as
    each pair is judged on its own. Every row now carries its own
    ``evaluated_at_ms``, and within one cycle those timestamps span 4–13 seconds
    — so a "cycle" is a box around observations taken at different times, and
    the previous tally charged a venue for a cycle in which *any* of its rows
    was in time. Counting rows removes the box.

    Rows are also **filtered by regime**, and the filter is structural rather
    than a hardcoded timestamp: a row counts only when it carries its own
    ``evaluated_at_ms``. Rows written before the streaming evaluator landed
    (measured 2026-09-17: everything up to 16:16:51 HKT, 9,117 rows) have no
    such field — they were judged only after every probe of the cycle had
    returned, which is exactly the delayed-price regime that produced the wrong
    numbers. They are excluded but **counted** (``legacy_rows``), never silently
    dropped; ``include_legacy=True`` puts them back for an audit.

    Denominator choice still matters, and is the reason this is a module rather
    than a SQL one-liner: a venue is not charged for evaluations in which the
    pair could not be priced (that is a data-coverage question, reported
    separately as ``evaluable_rate``). Raw counts do favour a venue that lists
    more assets, so every count is shown next to its rate over that venue's own
    evaluable rows, and the ranking leads with the rate.

    Two cost bases are deliberately kept apart:

    * ``cost_mode="recorded"`` (default) charges each row **its own** recorded
      ``roundtrip_cost_usdt``, converted to bps of that row's ``spot_open_usdt``.
      This is the number the monitor itself used to decide.
    * ``cost_mode="flat"`` charges the ``cost_bps`` argument to every row, which
      is what this module did before 2026-09-17 and what ``--cost-bps`` still
      selects for a what-if.

    Rows with no recorded cost (they never reached the cost model) always fall
    back to ``cost_bps``, so a mixed window is still readable.
    """
    min_gross_bps = _decimal(min_gross_bps)
    cost_bps = _decimal(cost_bps)
    if cost_mode not in ("recorded", "flat"):
        raise ValueError("cost_mode must be 'recorded' or 'flat'")
    min_net_bps = _decimal(min_net_bps)
    min_profit_quote = _decimal(min_profit_quote)
    trade_quote_usdt = _decimal(trade_quote_usdt)
    target_exit_basis_bps = _decimal(target_exit_basis_bps)
    wallet_quote_usdt = (None if wallet_quote_usdt is None
                         else _decimal(wallet_quote_usdt))
    # The ceiling a trade is actually sized against. The wallet binds first
    # whenever it is smaller than the gray-scale cap.
    effective_cap = trade_quote_usdt
    if wallet_quote_usdt is not None:
        effective_cap = min(effective_cap,
                            wallet_quote_usdt * WALLET_SAFETY_FACTOR)
    executable = set(executable)

    venues: set[str] = set()
    per_venue: dict[str, dict] = {}

    cycles: set = set()
    legacy_rows = 0
    legacy_cycles: set = set()
    window_lo: int | None = None
    window_hi: int | None = None

    for row in rows:
        venue = row.get("venue") or "unknown"
        venues.add(venue)
        bucket = per_venue.setdefault(venue, {
            "rows": 0, "priced_rows": 0, "evaluable_rows": 0, "opportunity_rows": 0,
            "candidate_rows": 0, "executable_rows": 0,
            "legacy_rows": 0, "cycles": set(),
            "groups": {name: {"rows": 0, "evaluable_rows": 0,
                              "opportunity_rows": 0, "candidate_rows": 0}
                       for name in ("session", "off_hours", "other")},
            "pairs": set(), "opportunity_pairs": set(),
            "gross": [], "assets": {}, "blocked": {}, "pairs_blocked": {},
            "required_notional": [], "affordable_rows": 0,
            # The money side: one entry per opportunity row, all in USDT, all
            # already recorded by the strategy rather than re-derived from bps.
            "cost_bps_values": [], "cost_quote_values": [],
            "cost_fallback_rows": 0,
            "notional_quote_values": [], "deployable_notional_values": [],
            "deployable_shares_values": [],
            "profit_probe_values": [], "profit_deployable_values": [],
            "profitable_rows": 0,
            "stale_rows": 0,
            "first_ms": None, "last_ms": None,
        })
        run_id = row.get("run_id")
        ts = int(row.get("recv_ts_ms") or 0)
        # Regime filter. `evaluated_at_ms` is written by the streaming
        # evaluator and by nothing else, so its absence is a positive statement
        # that this row was judged after the whole cycle's probes returned.
        if row.get("evaluated_at_ms") is None and not include_legacy:
            bucket["legacy_rows"] += 1
            legacy_rows += 1
            legacy_cycles.add(run_id)
            continue
        cycles.add(run_id)
        bucket["cycles"].add(run_id)
        bucket["rows"] += 1
        if window_lo is None or ts < window_lo:
            window_lo = ts
        if window_hi is None or ts > window_hi:
            window_hi = ts
        if bucket["first_ms"] is None or ts < bucket["first_ms"]:
            bucket["first_ms"] = ts
        if bucket["last_ms"] is None or ts > bucket["last_ms"]:
            bucket["last_ms"] = ts

        gross = row.get("gross_bps")
        instrument = row.get("instrument") or f"{venue}:{row.get('asset')}"
        asset = row.get("asset") or "?"
        group = bucket["groups"][phase_group(row.get("phase"))]
        group["rows"] += 1
        if gross is None:
            # Not evaluable: record why, so "no opportunity" and "no data" never
            # look the same.
            for tag in (row.get("rejections") or ["unknown"]):
                bucket["blocked"][tag] = bucket["blocked"].get(tag, 0) + 1
                key = f"{asset}:{tag}"
                bucket["pairs_blocked"][key] = bucket["pairs_blocked"].get(key, 0) + 1
            continue

        bucket["pairs"].add(instrument)
        bucket["priced_rows"] += 1
        # A row can carry a basis and still be one the monitor refused to trust
        # (a stale book or a stale quote). Those are counted, then kept out of
        # every opportunity figure below — including `evaluable_rows`, so the
        # denominators in this report only ever describe rows whose prices the
        # monitor would act on.
        flagged = DATA_QUALITY_REJECTIONS.intersection(row.get("rejections") or ())
        if flagged:
            bucket["stale_rows"] += 1
            continue
        bucket["evaluable_rows"] += 1
        group["evaluable_rows"] += 1
        bucket["gross"].append(gross)
        # The cost this row was actually charged, in bps of its own notional.
        # Recorded beats modelled: see the constants at the top of the module.
        row_cost_bps = cost_bps
        cost_quote = row.get("cost_quote")
        spot_open_quote = row.get("spot_open_quote")
        used_recorded_cost = (cost_mode == "recorded" and cost_quote is not None
                              and spot_open_quote is not None
                              and spot_open_quote > 0)
        if used_recorded_cost:
            row_cost_bps = cost_quote / spot_open_quote * BPS
        if gross >= min_gross_bps:
            bucket["opportunity_rows"] += 1
            group["opportunity_rows"] += 1
            bucket["opportunity_pairs"].add(instrument)
            bucket["assets"][asset] = bucket["assets"].get(asset, 0) + 1
        if gross - row_cost_bps >= min_net_bps:
            bucket["candidate_rows"] += 1
            group["candidate_rows"] += 1
            if instrument in executable:
                bucket["executable_rows"] += 1
            # ---- 金额 / 利润估计 ------------------------------------------
            # Charged to the *candidate* rows, not to every above-threshold row:
            # "过门槛" is the bar the monitor itself acts on, so the money table
            # and the count table describe the same set of rows. A row that
            # clears the 20 bps entry basis but not the 20 bps *net* bar is a
            # priced observation, and averaging its (negative) profit into a
            # venue's total would answer "how much did we not lose" instead of
            # "how much is there".
            #
            # Two sizes per row:
            #   probe      = the recorded 1-share row (what the strategy saw)
            #   deployable = what min(ceiling, wallet x 0.999) actually buys
            # The deployable figures are a *linear extrapolation* of this row's
            # bps over a larger whole-share count. The strategy re-quotes at
            # execution time; this is a ranking aid, not a fill price. It also
            # does not check that the whole-share count maps onto an integer
            # contract count — that is re-derived at execution from the live
            # mapping (`shares_per_token / shares_per_perp_unit / multiplier`).
            # The cost reported is the cost *charged*, which is the recorded one
            # when the row has it and the flat fallback when it does not; the
            # fallback is counted so a report can never quietly present the
            # parameter as a measurement.
            bucket["cost_bps_values"].append(row_cost_bps)
            if used_recorded_cost:
                bucket["cost_quote_values"].append(cost_quote)
            else:
                bucket["cost_fallback_rows"] += 1
            if spot_open_quote is not None and spot_open_quote > 0:
                quantity = row.get("spot_quantity") or Decimal(1)
                if quantity <= 0:
                    quantity = Decimal(1)
                price_per_share = spot_open_quote / quantity
                shares, notional = _deployable(effective_cap, price_per_share)
                net_bps = gross - row_cost_bps
                bucket["notional_quote_values"].append(spot_open_quote)
                bucket["deployable_notional_values"].append(notional)
                bucket["deployable_shares_values"].append(shares)
                if row.get("profit_quote") is not None:
                    bucket["profit_probe_values"].append(row["profit_quote"])
                # Conditional profit at the deployable size, recomputed the same
                # way the strategy recomputes it:
                #   (gross basis - four-leg cost) x notional - assumed exit basis
                profit_deployable = (
                    notional * net_bps / BPS
                    - notional * target_exit_basis_bps / BPS
                )
                bucket["profit_deployable_values"].append(profit_deployable)
                if profit_deployable >= min_profit_quote:
                    bucket["profitable_rows"] += 1
        # Size gate: what notional this edge needs before it is worth the
        # project's own minimum profit. Derived from the recorded bps, so it is
        # an extrapolation from probe size (1 share) rather than a quote at that
        # size — labelled as such wherever it is shown.
        net_bps = gross - row_cost_bps
        if net_bps > 0:
            bucket["required_notional"].append(min_profit_quote / (net_bps / BPS))
            if min_profit_quote / (net_bps / BPS) <= effective_cap:
                bucket["affordable_rows"] += 1

    venue_list = sorted(venues)

    report: dict[str, dict] = {}
    for venue in venue_list:
        bucket = per_venue[venue]
        report[venue] = {
            # One evaluation = one judgement at its own `evaluated_at_ms`. This
            # row is the unit; `cycles` is informational only and must not be
            # used as a denominator (see the module docstring).
            "cycles": len(bucket["cycles"]),
            "rows": bucket["rows"],
            # Rows dropped by the regime filter: judged before the streaming
            # evaluator existed, i.e. after waiting for every probe in the cycle.
            "legacy_rows": bucket["legacy_rows"],
            # Rows that produced a basis at all, trustworthy or not.
            "priced_rows": bucket["priced_rows"],
            "evaluable_rows": bucket["evaluable_rows"],
            "opportunity_rows": bucket["opportunity_rows"],
            "candidate_rows": bucket["candidate_rows"],
            "executable_rows": bucket["executable_rows"],
            # Read these as: of the evaluations where this venue could be
            # priced, how often was there actually something to do. This is the
            # comparable number between venues of different listing breadth;
            # the raw counts above are not.
            "opportunity_rate": _rate(bucket["opportunity_rows"],
                                      bucket["evaluable_rows"]),
            "candidate_rate": _rate(bucket["candidate_rows"],
                                    bucket["evaluable_rows"]),
            # Coverage, kept separate on purpose: 0% here means "we could not
            # price this venue", which is a different problem from "no basis".
            "evaluable_rate": _rate(bucket["evaluable_rows"], bucket["rows"]),
            "max_gross_bps": str(max(bucket["gross"])) if bucket["gross"] else None,
            "median_gross_bps": (str(_median(bucket["gross"]))
                                 if bucket["gross"] else None),
            "best_net_bps": (str(max(bucket["gross"]) - cost_bps)
                             if bucket["gross"] else None),
            "top_assets": sorted(bucket["assets"].items(),
                                 key=lambda kv: (-kv[1], kv[0]))[:4],
            # How big a position this venue's edges need to be worth doing, and
            # how many of them fit inside the cap actually in force.
            "required_notional_usd_median": (
                str(_median(bucket["required_notional"]).quantize(Decimal("0.1")))
                if bucket["required_notional"] else None),
            "opportunity_rows_affordable": bucket["affordable_rows"],
            # ---- 成本 / 金额 / 利润估计 ----------------------------------
            # All of these are USDT from the recorded rows, never re-derived
            # from a bps constant. `cost_bps_median` is the number that should
            # be compared against the `[costs]` parameters, and it is ~50 on
            # this book because the model levies slippage+failure on all four
            # legs and the perp fee on two of them.
            "cost_bps_median": _q(_median(bucket["cost_bps_values"]), 1),
            "cost_quote_median": _q(_median(bucket["cost_quote_values"]), 4),
            # Candidate rows with no recorded cost: the median above is then a
            # mix of measurements and the `[costs]` fallback. Non-zero means
            # "read this column as an upper bound on what is measured".
            "cost_fallback_rows": bucket["cost_fallback_rows"],
            "notional_quote_median": _q(_median(bucket["notional_quote_values"]), 4),
            "notional_quote_sum": _q(_sum(bucket["notional_quote_values"]), 2),
            "deployable_notional_median": _q(
                _median(bucket["deployable_notional_values"]), 2),
            "deployable_notional_sum": _q(
                _sum(bucket["deployable_notional_values"]), 2),
            # Whole shares the ceiling buys at this row's price. 0 means the
            # share price alone is above the whole budget (measured 2026-09-17:
            # POPMART ≈19.4 USDG against a 10.91 USDG wallet), which is a
            # different blocker from "the basis is not there".
            "deployable_shares_median": _q(
                _median(bucket["deployable_shares_values"]), 0),
            "unaffordable_rows": sum(
                1 for value in bucket["deployable_shares_values"] if value == 0),
            "profit_probe_median": _q(_median(bucket["profit_probe_values"]), 4),
            "profit_probe_max": _q(_max(bucket["profit_probe_values"]), 4),
            "profit_deployable_median": _q(
                _median(bucket["profit_deployable_values"]), 4),
            "profit_deployable_max": _q(
                _max(bucket["profit_deployable_values"]), 4),
            "profit_deployable_sum": _q(
                _sum(bucket["profit_deployable_values"]), 2),
            # How many opportunities would actually clear the project's own
            # minimum profit *at the size the wallet allows*. This is the
            # column that decides whether a venue is worth wiring up now or
            # only after the wallet is funded.
            "profitable_rows": bucket["profitable_rows"],
            # Reported so a shrinking sample is visible as "we could not trust
            # the prices", not silently as "no opportunity".
            "stale_rows": bucket["stale_rows"],
            # Share of the rows that produced a basis at all but which the
            # monitor then refused to trust. `priced_rows` already includes the
            # stale ones.
            "stale_rate": _rate(bucket["stale_rows"], bucket["priced_rows"]),
            "blocked": sorted(bucket["blocked"].items(),
                              key=lambda kv: (-kv[1], kv[0]))[:6],
            # Which asset a rejection belongs to, because "this venue has no
            # opportunities" and "this venue's other five assets are unverified"
            # are different problems with different fixes.
            "pairs_blocked": sorted(bucket["pairs_blocked"].items(),
                                    key=lambda kv: (-kv[1], kv[0]))[:8],
            "pairs_seen": sorted(bucket["pairs"]),
            "opportunity_pairs": sorted(bucket["opportunity_pairs"]),
            "first_ms": bucket["first_ms"],
            "last_ms": bucket["last_ms"],
            "has_execution_adapter": any(
                pair.split(":", 1)[0] == venue for pair in executable),
            "phases": {
                name: {
                    "rows": group["rows"],
                    "evaluable_rows": group["evaluable_rows"],
                    "opportunity_rows": group["opportunity_rows"],
                    "candidate_rows": group["candidate_rows"],
                    "opportunity_rate": _rate(group["opportunity_rows"],
                                              group["evaluable_rows"]),
                    "candidate_rate": _rate(group["candidate_rows"],
                                            group["evaluable_rows"]),
                }
                for name, group in bucket["groups"].items()
            },
        }

    # Ranking rule, stated so it can be argued with rather than guessed at:
    # every recorded phase counts, because both legs of the trade quote around
    # the clock and the cash session is not the regime that decides anything
    # (AGENTS.md 全天候原则). Ranking on the cash session alone pushed the
    # venues whose edges live in the pre-open / after-close / weekend windows
    # down the list. *Rate before count*: a venue that merely lists more assets,
    # or gets sampled more often, would otherwise win with the same per-asset
    # behaviour. The count breaks ties, then the widest basis seen.
    ranking = sorted(
        venue_list,
        key=lambda venue: (
            report[venue]["candidate_rate"] is not None,
            report[venue]["candidate_rate"] or Decimal("-1"),
            report[venue]["candidate_rows"],
            report[venue]["max_gross_bps"] and Decimal(report[venue]["max_gross_bps"])
            or Decimal("-1"),
        ),
        reverse=True,
    )

    return {
        "window": {
            "since_ms": window_lo,
            "until_ms": window_hi,
            "now_ms": now_ms,
            # Cycles are reported for context only, never as a denominator.
            "cycles": len(cycles),
            "legacy_rows": legacy_rows,
            "legacy_cycles": len(legacy_cycles),
            "include_legacy": bool(include_legacy),
            "unit": "evaluation",
            "venues": venue_list,
        },
        "thresholds": {
            "min_gross_bps": str(min_gross_bps),
            "cost_bps": str(cost_bps),
            "cost_mode": cost_mode,
            "min_net_bps": str(min_net_bps),
            "min_profit_quote": str(min_profit_quote),
            "trade_quote_usdt": str(trade_quote_usdt),
            "wallet_quote_usdt": (None if wallet_quote_usdt is None
                                  else str(wallet_quote_usdt)),
            "effective_cap_usdt": _trim(effective_cap.quantize(Decimal("0.001"))),
            "target_exit_basis_bps": str(target_exit_basis_bps),
            "opportunity_means": (f"毛基差 >= {min_gross_bps} bps"
                                  "（买链上现货 / 空该所永续）"),
            "candidate_means": (
                f"毛基差 - 该行记录的成本（缺记录时用 {cost_bps} bps）"
                f" >= {min_net_bps} bps（监控自己的入场门槛）"
                if cost_mode == "recorded" else
                f"毛基差 - {cost_bps} bps 成本 >= {min_net_bps} bps"
                "（监控自己的入场门槛）"),
            "size_means": (f"达到 {min_profit_quote} USD 最小利润所需的名义金额"
                           f"（按 bps 外推，探针是 1 股；单笔上限 "
                           f"{trade_quote_usdt} USD）"),
            "unit_means": ("每一次「评估」（该场该合约自己的 evaluated_at_ms），"
                           "不是监控轮数"),
            "regime_means": (
                "只统计带 evaluated_at_ms 的行（= 两条腿各自到位就立刻判定）；"
                "整轮等齐才判定的旧行被排除并单独计数"),
            "money_means": (
                f"成本/金额/利润三列都读记录值（USDT）；可部署名义 = "
                f"min(单笔上限 {trade_quote_usdt}, 钱包 x {WALLET_SAFETY_FACTOR}) "
                f"按 {_trim(effective_cap.quantize(Decimal('0.001')))} USD 换算成整股"),
            "profit_means": (
                f"可部署净利 = 该行净基差 x 可部署名义 - 假定 {target_exit_basis_bps}"
                " bps 的退出基差（外推，不是该尺寸下的真实报价）"),
        },
        "venues": report,
        "ranking": ranking,
        "note": ("监控 != 可执行：没有执行适配器的场（bybit/bitget/gate/binance）"
                 "排得再前也只说明机会在那里，接它需要按 docs/execution.md 单独做适配器。"),
    }


def format_report(board: dict, *, places: int = 1) -> str:
    """The same numbers as text, for the CLI and for pasting into a doc."""
    window = board["window"]
    thresholds = board["thresholds"]
    lines = [
        "场馆机会统计（按「次评估」汇总：每条 basis_evaluation 在它自己的时刻判定）",
        "",
        f"  窗口        : {_stamp(window['since_ms'])} → {_stamp(window['until_ms'])}"
        f"（{window['cycles']} 个采样轮次，{len(window['venues'])} 场）",
        f"  统计单位    : {thresholds['unit_means']}",
        f"  口径        : {thresholds['regime_means']}",
        f"  机会定义    : {thresholds['opportunity_means']}",
        f"  候选定义    : {thresholds['candidate_means']}",
        f"  尺寸口径    : {thresholds['size_means']}",
        "",
        f"  {'场所':<10}{'交易时段 可评估':>16}{'有机会':>7}{'过门槛':>7}"
        f"{'候选率':>8}{'盘前盘后 可评估':>16}{'有机会':>7}{'过门槛':>7}"
        f"{'其他/休市 可评估':>18}{'有机会':>7}{'过门槛':>7}"
        f"{'最高毛(bps)':>12}{'够本金':>8}{'不可信':>8}",
    ]
    for venue in board["ranking"]:
        row = board["venues"][venue]
        session = row["phases"]["session"]
        off = row["phases"]["off_hours"]
        other = row["phases"]["other"]
        lines.append(
            f"  {venue:<10}{session['evaluable_rows']:>16}"
            f"{session['opportunity_rows']:>7}{session['candidate_rows']:>7}"
            f"{_pct(session['candidate_rate']):>8}"
            f"{off['evaluable_rows']:>16}{off['opportunity_rows']:>7}"
            f"{off['candidate_rows']:>7}"
            # Weekends/holidays: both legs quote, so these rows are read the
            # same way as the two buckets to the left (AGENTS.md 全天候原则).
            f"{other['evaluable_rows']:>18}{other['opportunity_rows']:>7}"
            f"{other['candidate_rows']:>7}"
            f"{_bps(row['max_gross_bps'], places):>12}"
            f"{row['opportunity_rows_affordable']:>8}{row['stale_rows']:>8}")
    lines.append("")
    # Second table, money instead of counts. Kept separate from the count table
    # because the two answer different questions ("how often" vs "how much"),
    # and because a 20-column terminal table reads as neither.
    lines += [
        f"  {'场所':<10}{'成本中位(bps)':>13}{'可部署名义中位':>15}{'利润中位@探针':>15}"
        f"{'利润中位@可部署':>16}{'利润合计':>11}{'够最小利润':>11}",
    ]
    for venue in board["ranking"]:
        row = board["venues"][venue]
        lines.append(
            f"  {venue:<10}{_num(row['cost_bps_median'], 1):>13}"
            f"{_num(row['deployable_notional_median'], 2):>15}"
            f"{_num(row['profit_probe_median'], 4):>15}"
            f"{_num(row['profit_deployable_median'], 4):>16}"
            f"{_num(row['profit_deployable_sum'], 2):>11}"
            f"{row['profitable_rows']:>11}")
    lines += [
        "",
        "  成本口径    : 读每行记录的 roundtrip_cost_usdt（该行 spot_open_usdt 的百分比），"
        "不再用 [costs] 参数的和。",
        "                模型把滑点+失败缓冲摊在四条腿的总流水上、永续手续费摊在两条永续腿上，",
        "                所以往返成本约为现货名义的 50 bps —— 15 bps 那个旧口径只是参数之和。",
        "                想回到旧口径：--cost-mode flat --cost-bps 15。",
        f"  金额/利润   : {thresholds['money_means']}",
        f"              {thresholds['profit_means']}",
        "                只对「过门槛」的行计价，和上面的计数表是同一批行；",
        "                没有过门槛的场这里显示 - ，不是 0。",
        "  单位        : 成本与利润为 USDT，名义为 USDT；@探针 = 记录的 1 股行，",
        f"                @可部署 = 换算到 min(单笔上限 {thresholds['trade_quote_usdt']}, "
        f"钱包 x {WALLET_SAFETY_FACTOR}"
        + (f" = {thresholds['effective_cap_usdt']} USD）后的线性外推"
           if thresholds["wallet_quote_usdt"]
           else "；钱包未知，只用了单笔上限）"),
        "",
    ]
    # A cost column that mixes measurements with the fallback parameter has to
    # say so, or the fallback gets read as something we observed.
    fallback = [venue for venue in board["ranking"]
                if board["venues"][venue]["cost_fallback_rows"]]
    if fallback:
        lines.append(
            "  成本缺失    : " + "、".join(
                f"{venue}×{board['venues'][venue]['cost_fallback_rows']}"
                for venue in fallback)
            + " 行没有记录成本，按参数回退计数；这些行的成本列不是实测值。")
        lines.append("")
    for venue in board["ranking"]:
        row = board["venues"][venue]
        assets = "、".join(f"{asset}×{count}" for asset, count in row["top_assets"])
        adapter = "有执行适配器" if row["has_execution_adapter"] else "无执行适配器"
        lines.append(f"  {venue}: {adapter}"
                     + (f"；过门槛集中在 {assets}" if assets else "；本窗口没有过门槛")
                     + (f"；最高净 {_bps(row['best_net_bps'], places)} bps"
                        if row["best_net_bps"] else ""))
        if row["required_notional_usd_median"]:
            lines.append(
                f"        按该场机会的中位净基差，要凑到 "
                f"{thresholds['min_profit_quote']} USD 最小利润需 ≈"
                f"{row['required_notional_usd_median']} USD 名义"
                f"（实际可部署 {thresholds['effective_cap_usdt']} USD = "
                f"min(单笔上限 {thresholds['trade_quote_usdt']}, 钱包 x "
                f"{WALLET_SAFETY_FACTOR})，"
                f"窗口内够本的次数 {row['opportunity_rows_affordable']}）")
        if not row["phases"]["session"]["evaluable_rows"]:
            lines.append("        交易时段没有任何可评估的次数 —— 它不是「没机会」，"
                         "而是这段时间没有这一档的样本")
        if row["blocked"] and not row["evaluable_rows"]:
            reasons = "、".join(f"{tag}×{count}" for tag, count in row["blocked"][:3])
            lines.append(f"        完全无法评估：{reasons}")
    # The regime filter has to be visible: an excluded majority is the whole
    # reason today's in-session numbers are absent, and a silent drop would
    # read as "there was nothing there".
    if window["legacy_rows"] and not window["include_legacy"]:
        lines += [
            "",
            f"  已排除旧口径: {window['legacy_rows']} 行（{window['legacy_cycles']} 个轮次）"
            "没有 evaluated_at_ms ——",
            "                它们是「等整轮探针都回来才判定」时期写的，价格年龄不可知。",
            "                要连着看（会重现旧数字）：--include-legacy。",
        ]
    lines += ["",
              "  读法：分母是「该场自己能定价的**次**数」—— 每条 basis_evaluation 在它自己的",
              "        evaluated_at_ms 上判定，不再把同一轮里时间不同的观测装进同一个盒子。",
              "        绝对次数偏好标的多、采样密的场，所以每个次数旁边都有它自己的**率**；",
              "        排序先看率，再看次数。",
              "  「不可信」= 该行虽然算出了基差，但带着陈旧/不同刻这类数据质量标记",
              "        （陈旧盘口、陈旧报价、非同时刻、成本过期），本表不计入机会与候选，",
              "        只在这里计数 —— 否则「机会最多」可能只是「行情最旧」的一种说法。"]
    # Only name the venues that genuinely have no session coverage: once they
    # do, the same sentence would be a lie about the very data on screen.
    no_session = [venue for venue in board["ranking"]
                  if not board["venues"][venue]["phases"]["session"]["evaluable_rows"]]
    if no_session:
        lines.append(f"        交易时段尚无数据的场：{'、'.join(no_session)} —— "
                     "它们不是「没机会」，是这段时间没被采样。")
    lines += ["", f"  {board['note']}"]
    return "\n".join(lines)


def _bps(value, places: int) -> str:
    if value in (None, ""):
        return "-"
    return f"{Decimal(str(value)):.{places}f}"


def _num(value, places: int) -> str:
    """A recorded money/bps figure for the table, or a dash when absent.

    A dash rather than 0: "this venue had no evaluable opportunity" and "this
    venue's cost was zero" are different statements, and the second one is never
    true on this book.
    """
    if value in (None, ""):
        return "-"
    return f"{Decimal(str(value)):.{places}f}"


def _pct(value) -> str:
    return "-" if value is None else f"{value}%"


def _stamp(ms) -> str:
    if not ms:
        return "-"
    import datetime as _dt
    hk = _dt.datetime.fromtimestamp(int(ms) / 1000, _dt.timezone(_dt.timedelta(hours=8)))
    return hk.strftime("%m-%d %H:%M")


def scoreboard_html(board: dict, *, places: int = 1) -> str:
    """A compact block for the market tab, appended after the existing document."""
    window = board["window"]
    rows = []
    money_rows = []
    # Only venues that genuinely have no session coverage get called out: once
    # they do, the same sentence would misdescribe the numbers right below it.
    no_session = [venue for venue in board["ranking"]
                  if not board["venues"][venue]["phases"]["session"]["evaluable_rows"]]
    session_note = (f"<br>{'、'.join(no_session)} 在交易时段还没有样本，"
                    "交易时段列显示「—」而不是 0：那是「没采到」，不是「没机会」。"
                    if no_session else "")
    legacy_note = (
        f"<br><b>已排除旧口径</b>：{window['legacy_rows']} 行"
        f"（{window['legacy_cycles']} 个轮次）没有 <code>evaluated_at_ms</code> —— "
        "它们是「等整轮探针都回来才判定」时期写的，价格年龄不可知，所以不计入。"
        "要看旧的（会重现旧数字）：<code>--include-legacy</code>。"
        if window["legacy_rows"] and not window["include_legacy"] else "")
    for venue in board["ranking"]:
        row = board["venues"][venue]
        session = row["phases"]["session"]
        off = row["phases"]["off_hours"]
        other = row["phases"]["other"]
        assets = "、".join(f"{asset}×{count}" for asset, count in row["top_assets"]) or "-"

        def pair(block: dict, key: str) -> str:
            # "n/m": what happened against what could be priced. A dash keeps an
            # unsampled phase from reading as a zero.
            if not block["evaluable_rows"]:
                return "—"
            return (f"{block[key]}<span style='color:#7d8f9c'>"
                    f"/{block['evaluable_rows']}</span>")

        def cell(text: str, *, strong: bool = False) -> str:
            style = "padding:4px 8px;text-align:right"
            if strong:
                style += ";font-weight:600"
            return f"<td style='{style}'>{text}</td>"

        rows.append(
            "<tr>"
            f"<td style='padding:4px 8px'><b>{venue}</b>"
            + ("" if row["has_execution_adapter"]
               else "<span style='color:#8b98a5;font-size:11px'> · 无适配器</span>")
            + "</td>"
            + cell(pair(session, "opportunity_rows"))
            + cell(pair(session, "candidate_rows"), strong=True)
            + cell(_pct(session["candidate_rate"]))
            + cell(pair(off, "opportunity_rows"))
            + cell(pair(off, "candidate_rows"), strong=True)
            + cell(pair(other, "opportunity_rows"))
            + cell(pair(other, "candidate_rows"), strong=True)
            + cell(_bps(row["max_gross_bps"], places))
            + cell(str(row["opportunity_rows_affordable"]))
            + cell(str(row["stale_rows"]))
            + f"<td style='padding:4px 8px'>{assets}</td>"
            + "</tr>")

        money_rows.append(
            "<tr>"
            f"<td style='padding:4px 8px'><b>{venue}</b></td>"
            + cell(_num(row["cost_bps_median"], 1))
            + cell(_num(row["deployable_notional_median"], 2))
            + cell(_num(row["profit_probe_median"], 4))
            + cell(_num(row["profit_deployable_median"], 4), strong=True)
            + cell(_num(row["profit_deployable_sum"], 2))
            + cell(str(row["profitable_rows"]), strong=True)
            + "</tr>")
    return f"""
<section style="margin:16px;padding:12px;border:1px solid #2b3b49;border-radius:10px">
  <h2 style="margin:0 0 6px;font-size:15px">场馆机会统计
    <span style="color:#7d8f9c;font-size:12px">
      {_stamp(window['since_ms'])} → {_stamp(window['until_ms'])} ·
      {window['cycles']} 个采样轮次 · {len(window['venues'])} 场 ·
      单位＝<b>次评估</b>（各自 evaluated_at_ms 判定）
    </span></h2>
  <div style="color:#7d8f9c;font-size:12px;margin-bottom:8px">
     「有机会」= 毛基差 ≥ {board['thresholds']['min_gross_bps']} bps；
     「过门槛」= 扣 {board['thresholds']['cost_bps']} bps 成本后 ≥
     {board['thresholds']['min_net_bps']} bps。
    分母是<b>该场自己能定价的次数</b>——每条 basis_evaluation 在它自己的
    <code>evaluated_at_ms</code> 上判定，不再把同一轮里时间不同的观测装进同一个盒子。
    绝对次数偏好标的多、采样密的场，所以每个次数旁边都有它自己的<b>率</b>；排序先看率。
    {legacy_note}{session_note}
     <br><b>「够本金」</b>：按该场机会的净基差，凑到
     {board['thresholds']['min_profit_quote']} USD 最小利润所需的名义金额 ≤
    单笔上限 {board['thresholds']['trade_quote_usdt']} USD 的次数（由 1 股探针的
    bps 外推，不是该尺寸下的真实报价）。
    <br><b>「不可信」</b>：算出了基差、但带着陈旧盘口/陈旧报价/非同时刻/成本过期
    这类数据质量标记的行数。它们不计入「有机会 / 过门槛」，否则「机会最多」可能
    只是「行情最旧」。
  </div>
  <table style="border-collapse:collapse;font-size:13px">
    <tr style="color:#7d8f9c;font-size:12px">
      <th style="text-align:left;padding:4px 8px" rowspan="2">场所</th>
      <th style="text-align:center;padding:4px 8px" colspan="3">交易时段</th>
      <th style="text-align:center;padding:4px 8px" colspan="2">盘前 / 盘后</th>
      <th style="text-align:center;padding:4px 8px" colspan="2">其他 / 休市</th>
      <th style="text-align:right;padding:4px 8px" rowspan="2">最高毛</th>
      <th style="text-align:right;padding:4px 8px" rowspan="2">够本金</th>
      <th style="text-align:right;padding:4px 8px" rowspan="2">不可信</th>
      <th style="text-align:left;padding:4px 8px" rowspan="2">集中在</th>
    </tr>
    <tr style="color:#7d8f9c;font-size:12px">
      <th style="text-align:right;padding:4px 8px">有机会</th>
      <th style="text-align:right;padding:4px 8px">过门槛</th>
      <th style="text-align:right;padding:4px 8px">候选率</th>
      <th style="text-align:right;padding:4px 8px">有机会</th>
      <th style="text-align:right;padding:4px 8px">过门槛</th>
      <th style="text-align:right;padding:4px 8px">有机会</th>
      <th style="text-align:right;padding:4px 8px">过门槛</th>
    </tr>
    {''.join(rows)}
  </table>
  <h3 style="margin:14px 0 6px;font-size:13px">金额 / 利润估计 / 成本估计
    <span style="color:#7d8f9c;font-size:11px">
      · 全部读记录值（USDT），不按 bps 常数重算
    </span></h3>
  <div style="color:#7d8f9c;font-size:12px;margin-bottom:8px">
     「可部署名义」= 按 <b>min(单笔上限 {board['thresholds']['trade_quote_usdt']},
     钱包 x {WALLET_SAFETY_FACTOR})</b>
     {('= ' + board['thresholds']['effective_cap_usdt'] + ' USD') if board['thresholds']['wallet_quote_usdt'] else ''}
     换算成整股后真正花掉的钱。<b>「利润中位@可部署」</b>是这一步的收益，
     已经扣掉该行记录的成本与 {board['thresholds']['target_exit_basis_bps']} bps
     的假定退出基差 —— <b>是外推，不是那个尺寸下的真实报价</b>（执行时会重算）。
     <b>「够最小利润」</b>= 在该尺寸下净利 ≥ {board['thresholds']['min_profit_quote']}
     USD 的次数。
     <br><b>「成本中位」</b>来自每行记录的 <code>roundtrip_cost_usdt</code>，
     不是 <code>[costs]</code> 里那几个参数的和 —— 模型把滑点与失败缓冲摊在
     <b>四条腿</b>的总流水上、永续手续费摊在两条永续腿上，所以往返成本约为现货
     名义的 50 bps。
  </div>
  <table style="border-collapse:collapse;font-size:13px">
    <tr style="color:#7d8f9c;font-size:12px">
      <th style="text-align:left;padding:4px 8px">场所</th>
      <th style="text-align:right;padding:4px 8px">成本中位 bps</th>
      <th style="text-align:right;padding:4px 8px">可部署名义中位 USDT</th>
      <th style="text-align:right;padding:4px 8px">利润中位@探针 USDT</th>
      <th style="text-align:right;padding:4px 8px">利润中位@可部署 USDT</th>
      <th style="text-align:right;padding:4px 8px">利润合计 USDT</th>
      <th style="text-align:right;padding:4px 8px">够最小利润次数</th>
    </tr>
    {''.join(money_rows)}
  </table>
  <div style="color:#7d8f9c;font-size:12px;margin-top:8px">{board['note']}</div>
</section>"""


def collect(*, db_path: str = DEFAULT_MONITOR_DB, hours: float | None = 24.0,
            since_ms: int | None = None, until_ms: int | None = None,
            min_gross_bps=DEFAULT_MIN_GROSS_BPS, cost_bps=DEFAULT_COST_BPS,
            cost_mode: str = DEFAULT_COST_MODE,
            min_net_bps=DEFAULT_MIN_NET_BPS,
            min_profit_quote=DEFAULT_MIN_PROFIT_QUOTE,
            trade_quote_usdt=DEFAULT_TRADE_QUOTE_USDT,
            wallet_quote_usdt: Decimal | None = DEFAULT_WALLET_QUOTE_USDT,
            target_exit_basis_bps: Decimal = DEFAULT_TARGET_EXIT_BPS,
            include_legacy: bool = False,
            executable: set[str] | frozenset[str] = frozenset(),
            now_ms: int | None = None) -> dict:
    """Window + read + aggregate in one call, for the CLI and the board."""
    import time
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if since_ms is None and hours is not None:
        since_ms = now_ms - int(hours * 3600 * 1000)
    rows = load_rows(db_path, since_ms=since_ms, until_ms=until_ms)
    return build_scoreboard(rows, min_gross_bps=min_gross_bps, cost_bps=cost_bps,
                            cost_mode=cost_mode,
                            min_net_bps=min_net_bps, min_profit_quote=min_profit_quote,
                            trade_quote_usdt=trade_quote_usdt,
                            wallet_quote_usdt=wallet_quote_usdt,
                            target_exit_basis_bps=target_exit_basis_bps,
                            include_legacy=include_legacy,
                            executable=executable, now_ms=now_ms)


def executable_from_policy(policy_path: str) -> set[str]:
    """``venue:instrument`` pairs from the execution allowlist, or an empty set.

    Read with ``tomllib`` and never cached: this is the same file the executor
    gates on, so a report that disagrees with it would be worse than no report.
    """
    import tomllib
    try:
        with open(policy_path, "rb") as handle:
            policy = tomllib.load(handle)
    except (OSError, ValueError):
        return set()
    allowlist = policy.get("allowlist") or {}
    return executable_pairs(allowlist.get("venues") or [],
                            allowlist.get("instruments") or [])


def limits_from_policy(policy_path: str) -> dict:
    """The gray-scale ceilings currently in force, or empty when unreadable.

    The report's "affordable" column is meaningless against a ceiling the
    operator has already changed, so it reads the live number instead of
    carrying its own default. An unreadable policy yields ``{}`` — the caller
    keeps its documented defaults rather than inventing a limit.
    """
    import tomllib
    try:
        with open(policy_path, "rb") as handle:
            policy = tomllib.load(handle)
    except (OSError, ValueError):
        return {}
    limits = policy.get("limits") or {}
    out = {}
    for key in ("per_trade_quote_usdt", "daily_quote_usdt"):
        if limits.get(key) is not None:
            out[key] = str(limits[key])
    return out


def scenario_from_monitor(config_path: str) -> dict:
    """The exit assumption the profit estimate is built on, read from the config.

    `[scenario] target_exit_basis_bps` is the operator's number and the monitor
    uses it for every recorded `conditional_profit_usdt`. Carrying a different
    default here would make the report's arithmetic disagree with the rows it is
    summarising, so it is read instead of duplicated.
    """
    import tomllib
    try:
        with open(config_path, "rb") as handle:
            config = tomllib.load(handle)
    except (OSError, ValueError):
        return {}
    scenario = config.get("scenario") or {}
    if scenario.get("target_exit_basis_bps") is None:
        return {}
    return {"target_exit_basis_bps": str(scenario["target_exit_basis_bps"])}




def add_arguments(sub) -> None:
    parser = sub.add_parser(
        "venue-scoreboard",
        help="per-venue arbitrage opportunity tally over recorded monitor cycles")
    parser.add_argument("--db", default=DEFAULT_MONITOR_DB)
    parser.add_argument("--hours", type=float, default=24.0,
                        help="window length; 0 means the whole recorded history")
    parser.add_argument("--min-gross-bps", default=str(DEFAULT_MIN_GROSS_BPS),
                        help="gross entry basis that counts as an opportunity")
    parser.add_argument("--cost-bps", default=str(DEFAULT_COST_BPS),
                        help="flat per-round-trip cost in bps, used only when "
                             "--cost-mode flat or when a row has no recorded cost")
    parser.add_argument("--cost-mode", choices=("recorded", "flat"),
                        default=DEFAULT_COST_MODE,
                        help="recorded: charge each row its own recorded "
                             "roundtrip_cost_usdt (default). flat: charge "
                             "--cost-bps to every row (the pre-2026-09-17 "
                             "behaviour, kept for what-if runs)")
    parser.add_argument("--min-net-bps", default=str(DEFAULT_MIN_NET_BPS))
    parser.add_argument("--min-profit-quote", default=str(DEFAULT_MIN_PROFIT_QUOTE),
                        help="the project's own minimum profit per trade, USDT ("
                             "`risk/hk_basis.py BasisLimits.min_profit`)")
    parser.add_argument("--trade-quote-usdt",
                        help="per-trade ceiling for the affordability column; "
                             "default: read from --automation-policy")
    parser.add_argument("--wallet-quote-usdt",
                        help="wallet balance that pays the spot leg, USDT. The "
                             "real per-trade size is min(ceiling, wallet x 0.999) "
                             "floored to whole shares, and the wallet is usually "
                             "the binding constraint. Omit when unknown: the "
                             "report then uses the ceiling alone and says so.")
    parser.add_argument("--target-exit-basis-bps",
                        help="assumed exit basis for the profit estimate; "
                             "default: read [scenario] from --monitor-config")
    parser.add_argument("--include-legacy", action="store_true",
                        help="also count rows written before the streaming "
                             "evaluator landed (no per-row evaluated_at_ms — "
                             "they were judged only after every probe of the "
                             "cycle returned). Reproduces the pre-2026-09-17 "
                             "16:19 numbers, which the delayed prices made wrong.")
    parser.add_argument("--monitor-config", default="config/hk_monitor.toml")
    parser.add_argument("--automation-policy", default="config/automation_policy.toml")
    parser.add_argument("--json", action="store_true")


def run(args) -> int:
    policy_limits = limits_from_policy(args.automation_policy)
    trade_quote = args.trade_quote_usdt or policy_limits.get("per_trade_quote_usdt")
    target_exit = (args.target_exit_basis_bps
                   or scenario_from_monitor(args.monitor_config).get(
                       "target_exit_basis_bps")
                   or str(DEFAULT_TARGET_EXIT_BPS))
    board = collect(db_path=args.db,
                    hours=(None if not args.hours else args.hours),
                    min_gross_bps=Decimal(str(args.min_gross_bps)),
                    cost_bps=Decimal(str(args.cost_bps)),
                    cost_mode=args.cost_mode,
                    min_net_bps=Decimal(str(args.min_net_bps)),
                    min_profit_quote=Decimal(str(args.min_profit_quote)),
                    trade_quote_usdt=(DEFAULT_TRADE_QUOTE_USDT if trade_quote is None
                                      else Decimal(str(trade_quote))),
                    wallet_quote_usdt=(None if not args.wallet_quote_usdt
                                       else Decimal(str(args.wallet_quote_usdt))),
                    target_exit_basis_bps=Decimal(str(target_exit)),
                    include_legacy=bool(args.include_legacy),
                    executable=executable_from_policy(args.automation_policy))
    if args.json:
        from arb_monitor.core.hk import dumps
        print(dumps(board))
    else:
        print(format_report(board))
    return 0
