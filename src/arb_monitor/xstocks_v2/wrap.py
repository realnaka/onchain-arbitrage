"""ERC-4626 wrap and redeem calldata. Pure. Does not broadcast.

Solana and TON xStocks have no wrapper. Callers ask ``capability`` before
using these bytes. The selectors are the standard ERC-4626 ones:

* ``deposit(uint256 assets, address receiver)`` = ``0x6e553f65``
* ``redeem(uint256 shares, address receiver, address owner)`` = ``0xba087652``
"""
from __future__ import annotations

DEPOSIT_SELECTOR = "6e553f65"
REDEEM_SELECTOR = "ba087652"


def _word(value: int) -> str:
    if not 0 <= int(value) < 2 ** 256:
        raise ValueError("word out of range")
    return f"{int(value):064x}"


def _address_word(address: str) -> str:
    text = str(address or "").strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if len(text) != 40:
        raise ValueError(f"EVM address must be 20 bytes, got {len(text) // 2}")
    int(text, 16)
    return text.rjust(64, "0")


def deposit_calldata(*, assets: int, receiver: str) -> str:
    """Wrap ``assets`` of the native token into the wrapper, paid to ``receiver``."""
    if int(assets) <= 0:
        raise ValueError("assets must be positive")
    return "0x" + DEPOSIT_SELECTOR + _word(assets) + _address_word(receiver)


def redeem_calldata(*, shares: int, receiver: str, owner: str) -> str:
    """Redeem ``shares`` of the wrapper back to the native token."""
    if int(shares) <= 0:
        raise ValueError("shares must be positive")
    return ("0x" + REDEEM_SELECTOR + _word(shares)
            + _address_word(receiver) + _address_word(owner))
