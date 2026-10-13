"""Paper decisions for the chain -> Kraken family.

Pure functions only. The caller fetches quotes; this module decides. Two rules
come straight from the specification:

* The threshold reads ``edge_after_requote_bps``, never the scan-time spread
  (section 5.3). A row that passed at t0 and failed on the re-quote is counted
  as ``false_t0``: it is evidence about the scan, not an opportunity.
* A cost that is unknown makes the path unpriceable. It is never treated as
  zero (section 10), because the missing term is usually the large one (a
  bridge fee or a withdrawal fee).
"""
from __future__ import annotations

from decimal import Decimal

from arb_monitor.xstocks_v2 import costs, guards
from arb_monitor.xstocks_v2.policy import V2Policy

CHAIN_TO_KRAKEN = "chain_to_kraken"


def transfer_cost_usdt(*, chain: str, first_for_mint: bool, sol_usd: Decimal | None,
                       policy: V2Policy) -> tuple[Decimal | None, str]:
    """The cost of moving the token from the chain into Kraken."""
    if chain == "solana":
        if sol_usd is None or sol_usd <= 0:
            return None, "sol_price_unknown"
        lamports = guards.sol_transfer_fee(first_for_mint=first_for_mint)
        return lamports * sol_usd, "measured"
    # EVM: approve + swap + deposit, at this round's gas price.
    return None, "gas_quote_required"


def gas_cost_usdt(*, chain: str, gas_price_wei: Decimal | None,
                  native_usd: Decimal | None, policy: V2Policy) -> tuple[Decimal | None, str]:
    """One implementation, in ``costs`` (the family's gas line lives there)."""
    return costs.gas_cost_usd(policy, chain=chain, gas_price_wei=gas_price_wei,
                              native_usd=native_usd)


def chain_to_kraken_net(*, size_usdt: Decimal, tokens: Decimal, buy_price_usd: Decimal,
                        kraken_bid: Decimal, policy: V2Policy,
                        transfer_usdt: Decimal | None, gas_usdt: Decimal | None,
                        quote_asset_usd: Decimal | None = Decimal(1),
                        credit_per_token: Decimal = Decimal(1),
                        conversion_usdt: Decimal | None = Decimal(0)) -> dict:
    """Net profit for ``buy on chain, deposit, sell into the Kraken bid``.

    ``conversion_usdt`` is the extra leg the executor may have to add when the
    wallet holds the *other* stablecoin (operator 2026-10-08: 机会发现不看钱包，
    执行时缺什么就换什么). It is a real cost, so the net charges it; ``None``
    (the quote failed) means unknown, and unknown is not zero.

    The notional is subtracted explicitly. ``buy_price_usd`` only sets how many
    tokens the notional buys; it is not itself a cost line, and leaving the
    capital out makes every candidate look profitable by its whole notional.
    Lines are what is really paid: transfer, gas, taker fee, slippage buffer.

    ``credit_per_token`` is how many **Kraken credit units** one on-chain token
    becomes: 1 on the EVM chains (``balanceOf`` is already the credited unit)
    and the mint's ``scaledUiAmount`` multiplier on Solana/TON. Kraken quotes
    per credit unit, so a sale priced as ``tokens × bid`` is in the wrong unit
    wherever the multiplier is not 1.

    Measured 2026-10-08: that mismatch understated every Solana row by
    ``multiplier − 1`` bps -- 139 bps on JPMx (1.0139175), 538 on PFEx, 901 on
    NFLXx (10.0). Two independent checks settled which side was wrong: the
    executor already waits for ``tokens × multiplier`` to be credited, and
    Kraken's price ÷ the real stock price is ≈ 1 for **every** online symbol,
    including the multiplier-10 ones (a per-raw quote would print 0.1 there).
    """
    parts: dict[str, Decimal | None] = {
        "transfer_usdt": transfer_usdt,
        "gas_usdt": gas_usdt,
        "conversion_usdt": conversion_usdt,
        "quote_asset_usd": quote_asset_usd,
    }
    if any(part is None for part in parts.values()):
        return {"net_usdt": None, "costs": {name: None for name in parts},
                "reason": "cost_unknown"}
    credits = tokens * credit_per_token
    proceeds = credits * kraken_bid
    # The taker fee and the slippage buffer come from the shared model, charged
    # on the *notional* like every other family (this used to charge them on the
    # proceeds here and on the notional in ``scan``: a second implementation of
    # the same two terms, differing by spread x fee -- negligible in USDT, but
    # exactly the drift the single-model rule exists to stop).
    fee, buffer = costs.proportional_parts(policy, notional=size_usdt,
                                           family=CHAIN_TO_KRAKEN)
    net = costs.net_after_costs(
        gross_usdt=proceeds - size_usdt, notional_usdt=size_usdt,
        fixed_cost_usd=transfer_usdt + gas_usdt + conversion_usdt, policy=policy,
        family=CHAIN_TO_KRAKEN)
    edge_after = (net / size_usdt) * Decimal(10000)
    return {
        "net_usdt": net,
        "edge_after_requote_bps": edge_after,
        "credit_per_token": credit_per_token,
        "credited_units": credits,
        "costs": {
            "proceeds_usdt": proceeds,
            "capital_usdt": size_usdt,
            "transfer_usdt": transfer_usdt,
            "gas_usdt": gas_usdt,
            "conversion_usdt": conversion_usdt,
            "kraken_fee_usdt": fee,
            "slippage_buffer_usdt": buffer,
        },
        "reason": "",
    }


def decide(*, row: dict, policy: V2Policy, kraken_bid: Decimal | None,
           size_usdt: Decimal, tokens: Decimal | None,
           transfer_usdt: Decimal | None, gas_usdt: Decimal | None,
           phase_allows: bool, slot_free: bool, reason_if_busy: str = "",
           scan_fresh: bool = True, pair_skew_ms: int | None = None,
           session_label: str = "regular",
           credit_per_token: Decimal = Decimal(1),
           leg_note: dict | None = None,
           conversion_usdt: Decimal | None = Decimal(0)) -> dict:
    """Turn one scan row into a paper state.

    ``phase_allows`` and ``slot_free`` are inputs rather than internal checks so
    the gate stays in one place (``cycle.open_gate``) and can be tested alone.

    ``kraken_bid`` is the price the caller measured for **this size** -- since
    2026-10-08 that is the VWAP of walking the Kraken book for our amount
    (``feeds.kraken.book_fill``), not the best bid. ``leg_note`` carries that
    evidence into the ledger so a row can be audited without re-deriving it.
    """
    if row.get("family") != CHAIN_TO_KRAKEN:
        return _state("refused", "family_not_in_phase_1")
    # ``stale_or_skew`` from the scan describes the scan's own pairing. The
    # caller re-quotes both legs, so staleness is re-derived from the fresh
    # timestamps (``pair_skew_ms``) instead of inherited from the snapshot.
    # ``below_threshold`` is the scan's t0 verdict and is re-derived here at the
    # executable size; inheriting it would replace a measured net with a
    # scan-time guess, which is the opposite of what this module is for.
    inherited = set(row.get("blockers") or []) - {
        "phase_scan", "stale_or_skew", "below_threshold"}
    if inherited:
        # The scan already ranked these (economics before plumbing). Alphabetical
        # order is not a priority: it made ``bridge_unmeasured_symbol`` the
        # reported reason for rows whose spread was -20 bps, which is where the
        # operator read it (2026-10-07). The full list stays in the ledger row's
        # ``extra.blockers``.
        blocker = row.get("reason") or sorted(inherited)[0]
        return _state("refused", blocker)
    if not phase_allows:
        return _state("refused", "phase_not_executable")
    if not slot_free:
        return _state("refused", reason_if_busy or "slot_busy")
    # Freshness belongs to the pair we measured, not to the scan round that
    # suggested it. The caller re-quotes both legs back to back, so ``tokens``
    # (fresh buy quote) and ``kraken_bid`` (fresh book) *are* the measurement;
    # a stale snapshot only means the candidate list was written a while ago.
    # Measured 2026-10-07: a round takes ~40 s against ``max_age_ms = 15000``, so
    # "the scan is old" refused 13-15 rows every round and the chain -> Kraken
    # family could never be paper-tested (v2: 1016 refused, 0 fills). It also
    # contradicted the comment above -- staleness was re-derived from the fresh
    # timestamps, then refused on the snapshot anyway.
    fresh_legs = tokens is not None and tokens > 0 and kraken_bid is not None
    if not scan_fresh and not fresh_legs:
        # No fresh numbers at all: the stale snapshot must not stand in for them.
        # Acting on an old row is how "the latest row" becomes a cost basis that
        # is 6.5x off (lessons 2.1).
        return _state("refused", "scan_stale")
    if pair_skew_ms is not None and pair_skew_ms > policy.max_pair_skew_ms:
        return _state("refused", "stale_or_skew")
    if kraken_bid is None or tokens is None or tokens <= 0:
        return _state("refused", "no_book_at_size")
    buy_price = _decimal(row.get("buy_price_usd"))
    if buy_price is None or buy_price <= 0:
        return _state("refused", "no_buy_price")
    result = chain_to_kraken_net(
        size_usdt=size_usdt, tokens=tokens, buy_price_usd=buy_price,
        kraken_bid=kraken_bid, policy=policy, transfer_usdt=transfer_usdt,
        gas_usdt=gas_usdt, credit_per_token=credit_per_token,
        conversion_usdt=conversion_usdt)
    if result["net_usdt"] is None:
        return _state("refused", result["reason"])
    edge_after = result["edge_after_requote_bps"]
    edge_t0 = _decimal(row.get("edge_t0_bps"))
    # The bar moves with the session: 20 bps while the US market is open, 4 bps
    # when it is closed (operator 2026-10-07).
    required = policy.required_edge_bps(session_label)
    passed_t0 = edge_t0 is not None and edge_t0 >= required
    freshness = {"fresh_legs": fresh_legs,
                 "scan_used_for": ("fresh" if scan_fresh
                                   else "candidate_selection"),
                 # The unit the sale was priced in, on the decision itself: a
                 # row priced per credit and sold per raw is 139 bps apart on
                 # JPMx, and the difference has to be visible in the ledger.
                 "credit_per_token": format(credit_per_token, "f"),
                 **(leg_note or {})}
    if edge_after < required or result["net_usdt"] < policy.min_net_profit_usdt:
        state = "false_t0" if passed_t0 else "refused"
        return _state(state, "below_threshold", net=result["net_usdt"],
                      edge_after=edge_after, costs=result["costs"],
                      extra=freshness)
    return _state("paper", "", net=result["net_usdt"], edge_after=edge_after,
                  costs=result["costs"], extra=freshness)


def _state(state: str, reason: str, *, net: Decimal | None = None,
           edge_after: Decimal | None = None, costs: dict | None = None,
           extra: dict | None = None) -> dict:
    return {"state": state, "reason": reason, "net_usdt": net,
            "edge_after_requote_bps": edge_after, "broadcast": False,
            "costs": costs or {}, **(extra or {})}


def _decimal(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
