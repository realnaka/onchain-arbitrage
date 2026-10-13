"""Decide which venue to refill, from a venue that can spare the money.

A short wallet is not a reason to stop every other chain. This module names
the one move that refills the shortest floor. It does not sign. The runner in
``scripts/xstocks_v2_treasury.py`` signs, and only a rail whose minimum and
fee were read from Kraken on 2026-10-10.

Balances are ``{(venue, asset): Decimal}``. A missing key is an unread
balance, which is not zero: that venue is left alone.
"""
from __future__ import annotations

from decimal import Decimal

#: X Layer USDG. Kraken's own minimum is 2.50 and the fee taken out of it is
#: 1, so 1.50 arrives. The floor is that arrival: enough for a probe, and
#: not the 0.000001 that made the 09:07 buy a zero-amount refusal.
#: Key ``xlayertee1``, method ``USDG - Xlayer``, address is the EVM wallet.
XLAYER_USDG = {
    "rail": "kraken_withdraw",
    "source": ("kraken", "USDG"),
    "dest": ("x-layer", "USDG"),
    "key": "xlayertee1",
    "method": "USDG - Xlayer",
    "minimum": Decimal("2.50"),
    "fee": Decimal("1"),
    "dest_floor": Decimal("1"),
}

#: Kraken USDC/USD refuses an order below 5 (AssetPairs ``ordermin``,
#: 2026-10-10). The Ethereum deposit method ``USDC - Ethereum (Unified)``
#: refuses below 2.5. ZUSD of 2 is enough to buy the SPYx the TON withdrawal
#: was short, and it is not a sweep of the Ethereum USDC.
ZUSD_FLOOR = Decimal("2")
USDC_SELL_MIN = Decimal("5")
USDC_DEPOSIT_MIN = Decimal("2.5")
#: Leave this much USDC on Ethereum. The pocket is the reserve, not the ticket.
ETH_USDC_RESERVE = Decimal("20")


def _amount(balances: dict, key: tuple[str, str]):
    """``None`` when nobody read this balance. Zero is a real reading."""
    if key not in balances:
        return None
    value = balances[key]
    if value is None:
        return None
    return Decimal(str(value))


def _usdg_move(balances: dict) -> dict | None:
    dest_have = _amount(balances, XLAYER_USDG["dest"])
    source_have = _amount(balances, XLAYER_USDG["source"])
    if dest_have is None or source_have is None:
        return None
    if dest_have >= XLAYER_USDG["dest_floor"]:
        return None
    move = {
        "rail": XLAYER_USDG["rail"],
        "source": list(XLAYER_USDG["source"]),
        "dest": list(XLAYER_USDG["dest"]),
        "key": XLAYER_USDG["key"],
        "method": XLAYER_USDG["method"],
        "amount": format(XLAYER_USDG["minimum"], "f"),
        "fee": format(XLAYER_USDG["fee"], "f"),
        "arrives": format(XLAYER_USDG["minimum"] - XLAYER_USDG["fee"], "f"),
        "have_source": format(source_have, "f"),
        "have_dest": format(dest_have, "f"),
    }
    if source_have < XLAYER_USDG["minimum"]:
        move["status"] = "short_source"
        move["reason"] = (f"kraken USDG {source_have} < withdraw minimum "
                          f"{XLAYER_USDG['minimum']}")
        return move
    move["status"] = "ready"
    move["reason"] = (f"x-layer USDG {dest_have} < {XLAYER_USDG['dest_floor']}; "
                      f"withdraw {XLAYER_USDG['minimum']} from kraken")
    return move


def _zusd_move(balances: dict) -> dict | None:
    zusd = _amount(balances, ("kraken", "ZUSD"))
    usdc = _amount(balances, ("kraken", "USDC"))
    eth_usdc = _amount(balances, ("eth", "USDC"))
    if zusd is None:
        return None
    if zusd >= ZUSD_FLOOR:
        return None
    if usdc is not None and usdc >= USDC_SELL_MIN:
        return {
            "rail": "kraken_sell_usdc",
            "source": ["kraken", "USDC"],
            "dest": ["kraken", "ZUSD"],
            "amount": format(USDC_SELL_MIN, "f"),
            "status": "ready",
            "have_source": format(usdc, "f"),
            "have_dest": format(zusd, "f"),
            "reason": (f"ZUSD {zusd} < {ZUSD_FLOOR}; sell {USDC_SELL_MIN} USDC "
                       f"(pair minimum)"),
        }
    if usdc is None or eth_usdc is None:
        return None
    gap = max(USDC_SELL_MIN - usdc, USDC_DEPOSIT_MIN)
    spare = eth_usdc - ETH_USDC_RESERVE
    move = {
        "rail": "eth_usdc_deposit",
        "source": ["eth", "USDC"],
        "dest": ["kraken", "USDC"],
        "amount": format(gap, "f"),
        "have_source": format(eth_usdc, "f"),
        "have_dest": format(usdc, "f"),
        "zusd": format(zusd, "f"),
    }
    if spare < gap:
        move["status"] = "short_source"
        move["reason"] = (f"ethereum USDC spare {spare} < deposit {gap} "
                          f"(reserve {ETH_USDC_RESERVE})")
        return move
    move["status"] = "ready"
    move["reason"] = (f"ZUSD {zusd} < {ZUSD_FLOOR} and kraken USDC {usdc} "
                      f"< sell minimum {USDC_SELL_MIN}; deposit {gap} USDC")
    return move


def plan(balances: dict) -> list[dict]:
    """The moves that refill a floor, ready ones first.

    At most one move per rail. A venue that was not read is absent, not short.
    """
    moves = []
    for move in (_usdg_move(balances), _zusd_move(balances)):
        if move is not None:
            moves.append(move)
    moves.sort(key=lambda item: item["status"] != "ready")
    return moves
