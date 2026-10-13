"""Size ladder for a sell leg. One quote is a point, not a curve.

Measured 2026-10-07: the sell leg was quoted once, at the full size, and the
executor treated that number as the market. At the 200 USDT ticket a single
point says nothing about whether the route still exists at 25% of the size, or
whether the last quarter of the order is filled at a materially worse price.

The ladder quotes the same route at a few fractions of the size and checks the
*shape*:

* every requested size must be quotable -- a route that cannot price the size
  is not a route (`ladder_incomplete`);
* the average price must not degrade by more than the allowance between the
  smallest and the largest size (`ladder_degradation`);
* a larger size may not get a *better* average price by more than tolerance --
  that is not a good fill, that is two quotes that do not describe one book
  (`ladder_non_monotonic`).

Pure functions, no network. The caller does the quoting; this module decides
whether the quotes describe a market we are willing to hit.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR

#: 25 / 50 / 75 / 100 percent. Four points is the smallest ladder that shows a
#: curve instead of a line, and it costs three extra quotes on a sell we are
#: about to make anyway.
DEFAULT_FRACTIONS = (Decimal("0.25"), Decimal("0.5"), Decimal("0.75"), Decimal("1"))

#: How much better a larger size may look before we call the ladder inconsistent.
NON_MONOTONIC_TOLERANCE_BPS = Decimal("5")

OK = "ok"
INCOMPLETE = "ladder_incomplete"
ORDER = "ladder_order"
DEGRADATION = "ladder_degradation"
NON_MONOTONIC = "ladder_non_monotonic"


def ladder_sizes(total_raw: int, fractions=DEFAULT_FRACTIONS) -> list[int]:
    """Integer sizes for the ladder, ascending, deduplicated, never empty.

    A size that rounds to zero is dropped: quoting a zero amount is not a
    smaller point, it is a failed call. A very small balance therefore gets a
    shorter ladder rather than a broken one.
    """
    total = int(total_raw)
    if total <= 0:
        return []
    sizes = []
    for fraction in fractions:
        size = int((Decimal(total) * Decimal(fraction)).to_integral_value(
            rounding=ROUND_FLOOR))
        if size > 0:
            sizes.append(size)
    if not sizes or sizes[-1] != total:
        sizes.append(total)
    return sorted(set(sizes))


def ladder_ok(points, *, max_bps: Decimal) -> tuple[bool, str]:
    """``points`` is ``[(size, unit_price), ...]``; unit must be consistent.

    Only the *relative* change matters, so the unit can be the raw out/in ratio
    of two tokens with different decimals: the whole ladder uses the same unit.
    """
    rows = [(int(size), Decimal(str(price))) for size, price in points]
    if len(rows) < 2:
        return False, INCOMPLETE
    sizes = [size for size, _ in rows]
    if sizes != sorted(sizes) or len(set(sizes)) != len(sizes):
        return False, ORDER
    if any(price <= 0 for _, price in rows):
        return False, f"{DEGRADATION}:non_positive_price"
    head = rows[0][1]
    tail = rows[-1][1]
    bps = (head - tail) / head * Decimal(10000)
    # 0.01 bps is fine enough to show a flat book (0.06) and coarse enough that
    # the message does not carry eighteen digits of noise.
    shown = bps.quantize(Decimal("0.01"))
    if bps > Decimal(max_bps):
        return False, f"{DEGRADATION}:{shown}bps"
    if bps < -NON_MONOTONIC_TOLERANCE_BPS:
        return False, f"{NON_MONOTONIC}:{shown}bps"
    return True, f"{OK}:{shown}bps"


def average_price(size_raw: int, out_raw: int) -> Decimal:
    """Out per in, in the two tokens' own smallest units."""
    if size_raw <= 0:
        raise ValueError("size 必须大于 0")
    return Decimal(out_raw) / Decimal(size_raw)


def confirm(points, *, max_bps: Decimal) -> tuple[bool, str, Decimal | None]:
    """``(ok, reason, worst_ratio)``.

    ``worst_ratio`` is the full-size ratio -- the number the reconcile gate and
    the slippage check should use, because it is the one we are about to trade
    at, and it was read before the swap. A ladder we refused returns ``None``:
    a caller must not be able to use a price from a shape we rejected.
    """
    ok, reason = ladder_ok(points, max_bps=max_bps)
    if not ok:
        return False, reason, None
    rows = [(int(size), Decimal(str(price))) for size, price in points]
    return True, reason, rows[-1][1]
