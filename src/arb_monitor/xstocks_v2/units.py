"""Kraken credit units. Every conversion in the repo comes here.

One credit unit is one unit Kraken books: the issuer's **native** token. Three
shapes have to be turned into it, and until 2026-10-07 each had its own copy of
the arithmetic (``scan.solana_pair_quotes``, ``scan.ton_pair_quotes``,
``scan.pool_credit_factor``) -- which is exactly how a wrapper pool's price got
compared with a Solana price and printed the multiplier itself as +55 bps of
profit. Two rules keep that from coming back:

* ``to_credit_units`` for an amount that is already in the venue's own decimals;
* ``pool_credit_factor`` for an amount that is one *pool token* (a wrapper is
  ``multiplier`` credits, a native token is one).
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

UNITS_NAME = "kraken_xstocks_units.toml"
#: Where the table the scan reads lives. ``scripts/kraken_xstocks_units.py``
#: rewrites it from Kraken's own ``AddOrder validate=true`` answers, and that
#: makes the file **runtime state**, so it is written under the gitignored
#: ``data-v2/``. Writing it into ``config/`` (as it did until 2026-10-10) left
#: the runner's checkout dirty on every nightly refresh, and
#: ``scripts/deploy-from-github.sh`` refuses to pull a dirty tree -- the runner
#: stopped taking new code seven minutes after it was cloned.
UNITS_LIVE = ROOT / "data-v2" / UNITS_NAME
#: The committed seed: what a fresh clone, CI and the tests read.
UNITS_SEED = ROOT / "config" / UNITS_NAME


def units_config_path(root: Path | str | None = None) -> Path:
    """The Kraken unit table to read: what this machine measured, else the seed."""
    base = Path(root) if root is not None else ROOT
    live = base / "data-v2" / UNITS_NAME
    return live if live.exists() else base / "config" / UNITS_NAME


def units_config_sources(root: Path | str | None = None) -> list[Path]:
    """Every file that may carry evidence, weakest first, the live one last.

    The committed seed is not just a fallback: a deposit ratio measured on
    another machine and merged through a pull request lands there, and the
    runner's own file does not have it yet. Reading both keeps that evidence
    instead of shadowing it.
    """
    base = Path(root) if root is not None else ROOT
    return [p for p in (base / "config" / UNITS_NAME,
                        base / "data-v2" / UNITS_NAME) if p.exists()]

EVM_CHAINS = frozenset({
    "eth", "ethereum", "xlayer", "x-layer", "bsc", "bnb",
    "arbitrum", "optimism", "monad", "hyperevm", "ink", "mantle",
})
SCALED_RAW_CHAINS = frozenset({"solana", "ton"})

# EVM ``balanceOf()`` is scaled by the *token's own* decimals, and the two
# tokens on X Layer do not share them: xStocks are 18, USDG is 6. Reading USDG
# as an 18-decimal token turns 63.4 into 6.3e-11 (measured 2026-10-07), which
# silently breaks every comparison that is not "did it move at all".
XSTOCK_DECIMALS = 18
USDG_DECIMALS = 6


def scale_raw(raw: int, decimals: int) -> Decimal:
    """A raw ``uint256`` balance as a decimal amount, at the token's decimals."""
    if decimals < 0:
        raise ValueError("decimals 不能为负")
    return Decimal(raw) / (Decimal(10) ** decimals)


# STRCx effective minimum from config/kraken_xstocks_units.toml on 2026-10-07.
STRCX_EFFECTIVE_MIN = Decimal("0.05459063574")

#: Pool ``form`` values that mean the pool trades the issuer's *native* token,
#: whose on-chain unit is already the credit unit. The census writes ``wrapper``
#: (English) and ``原生`` (Chinese) today, and both pool readers default a
#: missing form to ``native``.
NATIVE_POOL_FORMS = frozenset({"native", "原生"})


def to_credit_units(chain: str, raw_ui: Decimal, multiplier: Decimal) -> Decimal:
    """Convert a wallet amount into Kraken credit units.

    EVM ``balanceOf()`` is already scaled. Multiplying again recreates the
    STRCx 1.0918 false edge. Solana and TON still scale raw amounts.
    """
    key = chain.lower()
    if key in SCALED_RAW_CHAINS:
        return raw_ui * multiplier
    if key in EVM_CHAINS:
        return raw_ui
    raise ValueError(f"未知链 {chain}")


def pool_credit_factor(*, form: str | None, symbol: str | None,
                       multipliers: dict | None = None) -> Decimal:
    """Credit units per one *pool* token: 1 for native, the multiplier for a wrapper.

    Measured 2026-10-07: the X Layer SPYx pool trades ``wSPYx``
    (``convertToAssets(1) = 1.005714560286254`` native) while the CCIP bridge
    hands us the native token, so a wrapper price has to be divided by this
    factor before it is compared with a Solana, TON or Kraken price.

    An unrecognised form counts as a wrapper: dividing by a multiplier can hide
    an opportunity, it can never invent one. A missing or non-positive
    multiplier stays 1, so the failure mode is "no conversion" rather than a
    division by zero.
    """
    if str(form or "").strip().lower() in NATIVE_POOL_FORMS:
        return Decimal(1)
    factor = (multipliers or {}).get(str(symbol or ""))
    if factor is None:
        return Decimal(1)
    try:
        value = Decimal(str(factor))
    except (ArithmeticError, ValueError):
        return Decimal(1)
    return value if value > 0 else Decimal(1)


def clears_min_units(quantity: Decimal, minimum: Decimal) -> bool:
    return quantity >= minimum


def clip_notional(*, balance: Decimal | None, depth: Decimal, per_trade: Decimal,
                  min_notional: Decimal, verified: bool) -> tuple[Decimal | None, str]:
    """Size a chain-to-Kraken trade.

    Unverified deposit credit still trades, but only at the minimum size.
    A balance that cannot cover that minimum is ``below_min_size``.
    """
    if balance is None:
        return None, "balance_unknown"
    room = min(per_trade, balance * Decimal("0.999"), depth)
    if room < min_notional:
        return None, "below_min_size"
    if not verified:
        return min_notional, "probe_only"
    return room, "full"


def credit_status(expected: Decimal, actual: Decimal, mismatch_ratio: Decimal) -> str:
    """Compare a real Kraken credit with the chain rule.

    Within the ratio: verified. Beyond it: stop that symbol only.
    """
    if expected <= 0:
        return "symbol_halt"
    gap = abs(actual - expected) / expected
    if gap > mismatch_ratio:
        return "symbol_halt"
    return "verified"
