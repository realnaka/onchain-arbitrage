"""Ethereum leg of chain to Kraken. Builds calldata. Does not broadcast."""
from __future__ import annotations

from decimal import Decimal

from arb_monitor.xstocks_v2.policy import V2Policy
from arb_monitor.xstocks_v2.units import clip_notional


def approve_calldata(spender: str, amount_raw: int) -> str:
    """Exact allowance for this trade. Not an infinite approve."""
    if amount_raw <= 0:
        raise ValueError("approve 数量必须大于 0")
    return "0x095ea7b3" + "0" * 24 + spender[2:].lower() + format(amount_raw, "064x")


def transfer_calldata(to: str, amount_raw: int) -> str:
    """``transfer(address,uint256)`` -- the deposit leg's whole calldata.

    A Kraken deposit is a plain ERC-20 transfer to the address Kraken hands out
    per (asset, network); there is no contract call and no memo field. The
    Solana leg needs a Token-2022 ``transfer_checked`` with the mint and the
    decimals; the EVM leg needs none of that (2026-10-09, family 1 on eth).
    """
    if amount_raw <= 0:
        raise ValueError("transfer 数量必须大于 0")
    return "0xa9059cbb" + "0" * 24 + to[2:].lower() + format(amount_raw, "064x")


def ethereum_method(methods) -> dict | None:
    """Kraken's ``<symbol> - Ethereum`` deposit method, if it offers one.

    The method name carries the network; a symbol can be deposit-only on
    Solana and have no Ethereum rail at all, which is a refusal, not a zero.
    """
    for row in methods or []:
        if not isinstance(row, dict):
            continue
        if "ethereum" in str(row.get("method") or "").lower():
            return row
    return None


def deposit_fee_units(method) -> Decimal | None:
    """What Kraken charges to receive this token, in token units."""
    if not isinstance(method, dict):
        return None
    value = method.get("fee")
    if value in (None, ""):
        return None
    return Decimal(str(value))


def deposit_min_units(method) -> Decimal | None:
    """The smallest deposit Kraken credits. Below it the coins are lost."""
    if not isinstance(method, dict):
        return None
    value = method.get("minimum")
    if value in (None, ""):
        return None
    return Decimal(str(value))


def deposit_method(symbol: str) -> str:
    return f"{symbol} - Ethereum"


def paper_decision(row: dict, policy: V2Policy, *, balance: Decimal | None,
                   gas: Decimal | None, requote_edge_bps: Decimal | None,
                   session_label: str = "regular") -> dict:
    """Decide one Ethereum candidate. Paper never sets broadcast."""
    if row.get("tier") != "green":
        return _refuse("hook_not_green")
    if not row.get("direct_deposit"):
        return _refuse("no_deposit_method")
    if gas is None:
        return _refuse("cost_unknown")
    if gas < policy.eth_gas_floor_eth:
        return _refuse("below_floor")
    if balance is not None and balance < policy.eth_usdc_floor_usdt:
        return _refuse("below_floor")
    size, reason = clip_notional(
        balance=balance,
        depth=Decimal(str(row["depth_usdt"])),
        per_trade=policy.per_trade_quote_usdt,
        min_notional=Decimal(str(row["min_notional_usdt"])),
        verified=bool(row.get("deposit_credit_verified")),
    )
    if size is None:
        return _refuse(reason)
    edge = None if requote_edge_bps is None else requote_edge_bps
    net = None if row.get("net_usdt") is None else Decimal(str(row["net_usdt"]))
    if edge is None or net is None or edge < policy.required_edge_bps(session_label):
        return _refuse("false_t0")
    if net < policy.min_net_profit_usdt:
        return _refuse("below_threshold")
    return {
        "state": "paper",
        "reason": reason,
        "size_usdt": size,
        "broadcast": False,
        "probe_only": reason == "probe_only",
        "deposit_method": deposit_method(str(row["symbol"])),
        "quote_asset": row.get("quote_asset") or "USDC",
    }


def _refuse(reason: str) -> dict:
    return {
        "state": "refused",
        "reason": reason,
        "size_usdt": None,
        "broadcast": False,
        "probe_only": False,
    }
