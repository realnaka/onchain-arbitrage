"""Pre-flight gas top-up: turn just enough stablecoin into the gas token (C).

The failure this exists for, measured 2026-10-07 and still true on 2026-10-09:
the wallet holds 0.0006056 OKB, one CCIP message costs 0.000802 OKB, so every
x-layer family is refused with ``gas_low`` **before the buy leg** (that refusal
is correct -- it is what stops "buy the coin, then discover it cannot move").
Nothing in v2 tops the gas up, so the whole chain stays idle until a human
sends OKB.

The proposal (``docs/xstocks-v2-gas-topup-plan-2026-10-09.md``) is: when the
precheck says ``gas_low``, swap *exactly the shortfall plus a small reserve* out
of the same chain's stablecoin, write a ``gas_topup`` ledger row, then re-run
the precheck. This module is the decision only -- no wallet, no swap, no ledger.

**It is off, and the two caps are unset.** A blank cap is a refusal
(``cap_not_set``), not "unlimited": the size decision belongs to the operator
(AGENTS.md: 额度/阈值/开关 decisions are theirs), and a top-up loop with no
ceiling is exactly the kind of thing that spends money unattended.

Solana is deliberately absent: its fee is a per-transfer lamport line with no
market to swap in, and its own shortfall check (``sol_shortfall``) already
measures it. Adding SOL here needs a decision about which mint pays and where
the 145 bps ATA rent goes.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

#: What the chain decides: the stablecoin we pay with, and the pool route.
#: The X Layer pair is the one the A7 price source already reads, so a top-up
#: there produces no new market risk.
STABLE_FOR = {
    "x-layer": {"stable": "USDG", "gas": "OKB"},
    "solana": {"stable": "USDC", "gas": "SOL"},
}


@dataclass(frozen=True)
class TopupPolicy:
    """Operator-owned. ``None`` means "not filled in yet", i.e. do not act."""

    enabled: bool = False
    #: Hard ceiling on **one** top-up, in USDT.
    max_usdt_per_topup: Decimal | None = None
    #: Hard ceiling per UTC day, in top-ups. ``None`` = not filled in.
    max_topups_per_day: int | None = None
    #: How many messages we want to be able to pay for *after* the top-up.
    reserve_messages: Decimal = Decimal("2")
    #: Ceiling on the swap's own slippage, on top of the venue's quote.
    max_slippage_bps: Decimal = Decimal("10")


def _d(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def decide(*, policy: TopupPolicy, chain: str, native_balance,
           fee_per_message, stable_balance, native_usd, tops_today: int = 0,
           safety: Decimal = Decimal("0.999")) -> dict:
    """``{"topup": bool, "reason": str, ...}`` for one chain.

    Every question is a refusal when it cannot be answered. Nothing here moves
    money; a ``True`` means "this is the size the policy allows", and the caller
    still has to quote, swap and verify.
    """
    route = STABLE_FOR.get(str(chain or "").strip().lower())
    if route is None:
        return {"topup": False, "reason": f"no_route:{chain}"}
    out = {"chain": chain, "stable": route["stable"], "gas": route["gas"]}
    if not policy.enabled:
        return {"topup": False, "reason": "disabled", **out}
    if policy.max_usdt_per_topup is None or policy.max_topups_per_day is None:
        # Deliberately a refusal rather than a default: an unattended size cap
        # is the operator's number to set.
        return {"topup": False, "reason": "cap_not_set", **out}
    balance = _d(native_balance)
    fee = _d(fee_per_message)
    stable = _d(stable_balance)
    price = _d(native_usd)
    if balance is None:
        return {"topup": False, "reason": "balance_unknown", **out}
    if fee is None or fee <= 0:
        return {"topup": False, "reason": "fee_unknown", **out}
    if price is None or price <= 0:
        return {"topup": False, "reason": "gas_price_unknown", **out}
    need = Decimal(str(policy.reserve_messages)) * fee
    out["need_native"] = format(need, "f")
    out["have_native"] = format(balance, "f")
    shortfall = need - balance
    if shortfall <= 0:
        return {"topup": False, "reason": "no_shortfall", **out}
    size_usdt = shortfall * price
    out["size_usdt"] = format(size_usdt, "f")
    if size_usdt > Decimal(str(policy.max_usdt_per_topup)):
        return {"topup": False, "reason": "over_cap", **out}
    if tops_today >= int(policy.max_topups_per_day):
        return {"topup": False, "reason": "daily_cap", **out}
    if stable is None or stable <= 0 or stable < size_usdt * safety:
        return {"topup": False, "reason": "stable_short", **out}
    return {"topup": True, "reason": "shortfall_within_cap",
            "expected_native": format(need, "f"),
            "slippage_bps": format(Decimal(str(policy.max_slippage_bps)), "f"),
            **out}


def ledger_row(*, chain: str, size_usdt, got_native, native_usd, tx: str,
               symbol: str | None = None, note: str = "") -> dict:
    """The one ledger row a top-up writes. Kept next to the decision so the
    bookkeeping cannot drift from what was decided."""
    route = STABLE_FOR.get(str(chain or "").strip().lower()) or {}
    return {"event": "gas_topup", "chain": chain, "stable": route.get("stable"),
            "gas": route.get("gas"), "symbol": symbol,
            "size_usdt": format(Decimal(str(size_usdt)), "f"),
            "got_native": format(Decimal(str(got_native)), "f"),
            "native_usd": format(Decimal(str(native_usd)), "f"),
            "tx": tx, "note": note}
