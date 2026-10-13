"""Pure two-leg screening: tokenized-stock spot versus perpetual futures.

A basis pair is always long one leg and short the other. Two directions exist
and they are NOT symmetric:

* ``long_spot_short_perp`` - buy the token on-chain (pays the on-chain ask)
  and short the perp (sells at the perp bid). This is the only direction this
  project could ever automate, because shorting the X Layer token is not
  available for these instruments.
* ``short_spot_long_perp`` - sell the token (needs inventory or a borrow) and
  long the perp (pays the perp ask). Implemented only so the arithmetic for the
  mirror side is testable; it is deliberately NOT offered in the monitor or the
  dashboard, because X Layer has no borrow market for these tokens, so the
  short-spot leg cannot actually be opened.

Both legs must use the side the trade would actually take. Comparing a spot
bid with a perp bid is not a trade, it is a quote.

Every number here is a screening estimate. Nothing in this module is an
exact-in quote, a fill guarantee or a locked profit.
"""
from __future__ import annotations

from decimal import Decimal, localcontext

ZERO = Decimal(0)
BPS = Decimal(10000)


def pool_side_prices(pool: dict, size_quote_usd: Decimal) -> dict:
    """Depth-aware buy/sell price for one notional in a Uniswap-V3-style pool.

    Uses the active-tick liquidity only. Offsetting ticks into other liquidity
    ranges would need the tick bitmap, so the result carries
    ``within_active_liquidity`` and callers must not present it as a deep-book
    walk. The 0.05% pool fee is charged on the input side of each swap.
    """
    liquidity = Decimal(pool.get("liquidity") or 0)
    sqrt_price_x96 = Decimal(pool.get("sqrt_price_x96") or 0)
    base_decimals = int(pool["base_decimals"])
    quote_decimals = int(pool["quote_decimals"])
    base_is_token0 = pool["base_address"].lower() == pool["token0"].lower()
    fee = Decimal(pool["fee"]) / Decimal(1_000_000)  # uint24 hundredths of a bip
    result = {"fee_bps": fee * BPS, "size_quote_usd": size_quote_usd}
    if liquidity <= 0 or sqrt_price_x96 <= 0 or size_quote_usd <= 0:
        return {**result, "ask_usd": None, "bid_usd": None,
                "within_active_liquidity": False, "error": "empty_or_uninitialized_pool"}
    with localcontext() as ctx:
        ctx.prec = 60
        sqrt_p = sqrt_price_x96 / (Decimal(2) ** 96)
        # Human price of one base token in quote units, and its inverse scale.
        decimals = Decimal(10) ** (base_decimals - quote_decimals)
        price_human = (sqrt_p ** 2) * decimals if base_is_token0 else decimals / (sqrt_p ** 2)
        quote_per_base = price_human
        base_scale = Decimal(10) ** base_decimals
        quote_scale = Decimal(10) ** quote_decimals
        # Uniswap V3 within one tick range, with sqrtP = sqrt(token1/token0):
        #   delta_token0 = L * (1/sqrtA - 1/sqrtB)
        #   delta_token1 = L * (sqrtB - sqrtA)
        # Adding token0 pushes the price down, adding token1 pushes it up. So a
        # buy of token0 with token1 makes token0 scarcer and raises sqrtP.
        def base_out_for_quote_in(quote_in: Decimal) -> Decimal:
            amount = quote_in * (Decimal(1) - fee)
            if base_is_token0:
                # Quote is token1 in: sqrtP rises, token0 leaves the pool.
                new_sqrt = sqrt_p + (amount * quote_scale) / liquidity
                out_raw = liquidity * ((Decimal(1) / sqrt_p) - (Decimal(1) / new_sqrt))
            else:
                # Quote is token0 in: sqrtP falls, token1 leaves the pool.
                new_sqrt = Decimal(1) / ((Decimal(1) / sqrt_p)
                                         + (amount * quote_scale) / liquidity)
                out_raw = liquidity * (sqrt_p - new_sqrt)
            return out_raw / base_scale

        def quote_out_for_base_in(base_in: Decimal) -> Decimal:
            amount = base_in * base_scale * (Decimal(1) - fee)
            if base_is_token0:
                # Base is token0 in: sqrtP falls, token1 leaves the pool.
                new_sqrt = Decimal(1) / ((Decimal(1) / sqrt_p) + amount / liquidity)
                out_raw = liquidity * (sqrt_p - new_sqrt)
            else:
                # Base is token1 in: sqrtP rises, token0 leaves the pool.
                new_sqrt = sqrt_p + amount / liquidity
                out_raw = liquidity * ((Decimal(1) / sqrt_p) - (Decimal(1) / new_sqrt))
            return out_raw / quote_scale

        base_out = base_out_for_quote_in(size_quote_usd)
        ask = size_quote_usd / base_out if base_out > 0 else None
        natural_base_in = size_quote_usd / quote_per_base
        quote_out = quote_out_for_base_in(natural_base_in)
        bid = quote_out / natural_base_in if natural_base_in > 0 else None
    impact_bps = None
    if ask is not None and quote_per_base > 0:
        impact_bps = (ask / quote_per_base - Decimal(1)) * BPS
    # Both impacts are positive "worse than mid" magnitudes: the ask sits above
    # mid, the bid below it.
    return {
        **result, "mid_usd": quote_per_base, "ask_usd": ask, "bid_usd": bid,
        "ask_impact_bps": impact_bps,
        "bid_impact_bps": (Decimal(1) - bid / quote_per_base) * BPS if bid else None,
        "base_quantity": natural_base_in,
        # A five-percent move would certainly cross into other tick ranges that
        # this single-range estimate cannot see.
        "within_active_liquidity": impact_bps is not None and impact_bps < Decimal(500),
    }


def perp_side(quotes: list[dict], side: str) -> dict | None:
    """Best executable perp side: short sells at the highest bid, long pays the
    lowest ask. The venue is always returned with the price."""
    usable = [q for q in quotes if q.get(side) is not None and q.get("fresh", False)]
    if not usable:
        usable = [q for q in quotes if q.get(side) is not None]
    if not usable:
        return None
    key = "bid" if side == "bid" else "ask"
    best = (max if side == "bid" else min)(usable, key=lambda q: q[key])
    return {
        "venue": best["venue"], "instrument": best["instrument"],
        "price": best[key], "funding_rate": best.get("funding_rate"),
        "book_age_ms": best.get("book_age_ms"), "fresh": best.get("fresh"),
        "payout_model": best.get("payout_model"),
    }


def entry_cost_bps(costs: dict, gas_share_usd: Decimal | None = None,
                   notional: Decimal | None = None) -> dict:
    """One-way opening cost. The exit side needs its own reviewed assumption."""
    spot_fee = Decimal(str(costs.get("spot_fee_bps", "0")))
    perp_fee = Decimal(str(costs.get("perp_fee_bps", "0")))
    slippage = Decimal(str(costs.get("slippage_bps", "0")))
    failure = Decimal(str(costs.get("failure_bps", "0")))
    gas_bps = ZERO
    if gas_share_usd is not None and notional and notional > 0:
        gas_bps = gas_share_usd / notional * BPS
    total = spot_fee + perp_fee + slippage + failure + gas_bps
    return {"spot_fee_bps": spot_fee, "perp_fee_bps": perp_fee,
            "slippage_bps": slippage, "failure_bps": failure,
            "gas_bps": gas_bps, "total_bps": total,
            "verified": bool(costs.get("verified", False))}


def roundtrip_cost_bps(*, spot_ask, spot_bid, perp_bid, perp_ask,
                       perp_fee_bps, gas_bps=ZERO, perp_style="taker",
                       perp_maker_fee_bps=None) -> dict:
    """What one open-and-close cycle costs before any basis is earned.

    The entry alert used to show only the *opening* cost, which is roughly half
    the story: the exit pays the other side of both spreads and another
    taker fee. Every component here is quoted on the side the trade would take
    (buy the ask, sell the bid), so the number is comparable to the gross basis
    instead of hiding the spread inside the prices.

    ``perp_style="maker"`` is the maker-first round trip (added 2026-09-16):
    both perpetual legs rest at the touch instead of crossing, so the spread
    that a taker pays twice over disappears, and the fee is the maker rate
    rather than the taker rate. The gross basis is still the taker-side number,
    so the bar drops only by the cost that genuinely stops being paid — the
    better fill price is not credited.
    """
    def spread(high, low) -> Decimal | None:
        if high is None or low is None:
            return None
        high, low = Decimal(str(high)), Decimal(str(low))
        if low <= 0:
            return None
        return (high - low) / low * BPS

    spot = spread(spot_ask, spot_bid)
    maker = str(perp_style or "taker").lower() == "maker"
    if maker:
        # Resting on the far side of the book earns the spread instead of
        # paying it. The conservative reading: charge nothing for it and credit
        # nothing either.
        perp = ZERO
        rate = Decimal(str(perp_maker_fee_bps if perp_maker_fee_bps is not None
                           else perp_fee_bps))
    else:
        perp = spread(perp_ask, perp_bid)
        rate = Decimal(str(perp_fee_bps))
    fees = rate * 2                            # both legs, whatever they are
    components = {"spot_spread_bps": spot, "perp_spread_bps": perp,
                  "perp_fee_bps": fees,
                  # An unconfigured gas budget stays unknown: treating it as 0
                  # would report a complete cost that nobody measured.
                  "gas_bps": None if gas_bps is None else Decimal(str(gas_bps))}
    known = [value for value in components.values() if value is not None]
    total = sum(known, ZERO) if len(known) == len(components) else None
    return {**components, "total_bps": total,
            "perp_style": "maker" if maker else "taker",
            "complete": len(known) == len(components)}


def breakeven_gross_bps(roundtrip: dict, *, buffer_bps, expected_profit_bps) -> Decimal | None:
    """Gross basis a trade must show before it is worth doing.

    Break-even is not the bar: the measured round-trip cost, plus an allowance
    for the parts that are not measured yet, plus the profit the trade has to
    offer for the risk of holding it until the basis converges.
    """
    if not roundtrip.get("complete"):
        return None
    return (Decimal(str(roundtrip["total_bps"]))
            + Decimal(str(buffer_bps)) + Decimal(str(expected_profit_bps)))


def size_plan(pool: dict, sizes, perp_bid: Decimal | None, cost_bps: Decimal,
              min_edge_bps: Decimal, max_impact_bps: Decimal = Decimal("20")) -> list[dict]:
    """What each notional would actually get, instead of a single mid-price bps.

    The CJ handbook's first hard rule is that a spread must be measured at the
    depth you can actually trade; its second is that liquidation slippage is
    where these trades die. So every configured size gets its own execution
    price, the extra slippage beyond the pool fee, and the net basis that
    survives the entry cost. ``depth_ok`` is deliberately conservative: a size
    only passes when its price impact stays inside ``max_impact_bps``.
    """
    rows = []
    for size in sizes:
        size = Decimal(str(size))
        sides = pool_side_prices(pool, size)
        fee_bps = sides.get("fee_bps")
        impact = sides.get("ask_impact_bps")
        row = {
            "size_usd": size,
            "spot_ask_usd": sides.get("ask_usd"),
            "spot_mid_usd": sides.get("mid_usd"),
            "pool_fee_bps": fee_bps,
            "impact_vs_mid_bps": impact,
            "extra_slippage_bps": (impact - fee_bps) if (impact is not None and fee_bps is not None)
            else None,
            "within_active_liquidity": sides.get("within_active_liquidity"),
            "error": sides.get("error"),
        }
        depth = pool.get("depth_usd")
        row["depth_share_pct"] = (size / depth * 100) if depth else None
        if row["spot_ask_usd"] and perp_bid:
            ask = row["spot_ask_usd"]
            gross = (Decimal(str(perp_bid)) - ask) / ask * BPS
            row["gross_entry_basis_bps"] = gross
            row["net_entry_basis_bps"] = gross - cost_bps
            row["token_quantity"] = size / ask
            row["gross_profit_quote"] = (Decimal(str(perp_bid)) - ask) * row["token_quantity"]
            row["is_candidate"] = row["net_entry_basis_bps"] >= min_edge_bps
        row["depth_ok"] = bool(
            impact is not None and impact <= max_impact_bps
            and row.get("within_active_liquidity", False))
        rows.append(row)
    return rows


def screen_direction(direction: str, spot: dict | None, perp: dict | None,
                     costs: dict, gas_share_usd: Decimal | None = None,
                     notional: Decimal | None = None,
                     min_edge_bps: Decimal | None = None) -> dict:
    """One direction of one asset. Missing sides stay missing.

    Judgement is deliberately asymmetric: a non-positive net entry basis is
    never a signal, it is simply not executable. A positive net basis still has
    to clear ``min_edge_bps`` before it is called a candidate, because a couple
    of basis points cannot survive exit costs, funding and execution error.
    """
    label = ("买现货 + 空永续" if direction == "long_spot_short_perp"
             else "卖现货 + 多永续")
    result = {"direction": direction, "label": label, "spot": spot, "perp": perp,
              "executable_pair": False, "reason": ""}
    if spot is None or spot.get("price") in (None, 0):
        result["reason"] = "missing_onchain_spot_side"
        return result
    if perp is None or perp.get("price") in (None, 0):
        result["reason"] = "missing_perp_side"
        return result
    spot_price = Decimal(spot["price"])
    perp_price = Decimal(perp["price"])
    if direction == "long_spot_short_perp":
        # Pay the token ask, sell the perp bid.
        gross = (perp_price - spot_price) / spot_price * BPS
    else:
        # Receive the token bid, pay the perp ask.
        gross = (spot_price - perp_price) / spot_price * BPS
    cost = entry_cost_bps(costs, gas_share_usd, notional)
    net = gross - cost["total_bps"]
    threshold = Decimal(str(min_edge_bps)) if min_edge_bps is not None else None
    if net <= 0:
        judgement, reason = "not_executable_negative_net", "net_entry_basis_not_positive"
    elif threshold is not None and net < threshold:
        judgement, reason = "below_threshold", "net_entry_basis_below_min_edge"
    else:
        judgement, reason = "candidate", "net_entry_basis_above_min_edge"
    result.update({
        "spot_price_usd": spot_price, "perp_price_usd": perp_price,
        "gross_entry_basis_bps": gross,
        "estimated_entry_cost_bps": cost["total_bps"],
        "cost_breakdown": cost,
        "estimated_net_entry_bps": net,
        "min_edge_bps": threshold,
        "judgement": judgement,
        "judgement_reason": reason,
        "is_candidate": judgement == "candidate",
        "executable_pair": True,
        "is_screening_estimate": True,
    })
    return result
