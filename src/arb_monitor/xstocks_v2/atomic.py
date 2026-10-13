"""Same-chain, two-pool round trips (stage 2): one transaction, or nothing.

The architecture rule (``docs/xstocks-v2-any-to-any-architecture-2026-10-07.md``
section 6) is that buying and selling the same asset on one chain must be a
**single** transaction. Two separate swaps are not that trade:

* between the two swaps the position is naked, and the price can move;
* either leg can fail alone, and the executor cannot repair it -- the first leg
  already moved the money, so the "repair" is a second market order;
* the whole point of the family is that both legs are one bet on one spread.

A single transaction needs a router that performs both swaps in one call. The
aggregator's quote says what it will do; this module reads that answer and
refuses anything it cannot prove is one call. **No proof is a refusal**, not a
translation into two swaps.

The router question is settled by measurement (2026-10-07, X Layer): the
aggregator refusing a circular *quote* is not the same as a router refusing a
circular *call*. Uniswap's existing ``SwapRouter02`` at
``0x4f0c28f5926afda16bf2506d5d9e57ea190f9bca`` ran
``multicall([exactInput(path), sweepToken(...)])`` successfully in a real
``eth_call`` over two real pools (``USDG ->100-> WOKB ->500-> USDG``) and again
over three. So **no contract of ours has to be deployed** for this family; what
is missing is a *second liquid pool* for the same token, which is a liquidity
fact and not a code fact.
"""
from __future__ import annotations

from decimal import Decimal

from arb_monitor.core.evm import keccak256
from arb_monitor.xstocks_v2 import costs

#: Uniswap v3 on X Layer, verified on-chain 2026-10-07 (chain id 0xc4 = 196):
#: the router answers ``factory()`` with the factory below and ``WETH9()`` with
#: WOKB, and the SPYx pool answers the same factory.
SWAP_ROUTER_02 = {"x-layer": "0x4f0c28f5926afda16bf2506d5d9e57ea190f9bca"}
UNISWAP_V3_FACTORY = {"x-layer": "0x4B2ab38DBF28D31D467aA8993f6c2585981D6804"}
QUOTER_V2 = {"x-layer": "0xd1b797d92d87b688193a2b976efc8d577d204343"}

EXACT_INPUT_SIGNATURE = "exactInput((bytes,address,uint256,uint256))"
EXACT_INPUT_SINGLE_SIGNATURE = (
    "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")
MULTICALL_SIGNATURE = "multicall(bytes[])"
SWEEP_TOKEN_SIGNATURE = "sweepToken(address,uint256,address)"


def selector(signature: str) -> str:
    """``0x`` + the first four bytes of keccak256(signature)."""
    return "0x" + keccak256(signature.encode())[:4].hex()


EXACT_INPUT_SELECTOR = selector(EXACT_INPUT_SIGNATURE)
EXACT_INPUT_SINGLE_SELECTOR = selector(EXACT_INPUT_SINGLE_SIGNATURE)
MULTICALL_SELECTOR = selector(MULTICALL_SIGNATURE)
SWEEP_TOKEN_SELECTOR = selector(SWEEP_TOKEN_SIGNATURE)

OK = "ok"
NOT_SAME_CHAIN = "not_same_chain"
SAME_POOL = "same_pool"
NO_ROUTE = "atomic_route_missing"
NOT_CIRCULAR = "atomic_route_not_circular"
POOL_NOT_IN_ROUTE = "atomic_route_missing_pool"


def same_chain_two_pool(*, buy_chain, sell_chain, buy_pool, sell_pool
                        ) -> tuple[bool, str]:
    """The family is only this family when it is one chain and two pools."""
    if str(buy_chain or "").lower() != str(sell_chain or "").lower():
        return False, NOT_SAME_CHAIN
    if not buy_pool or not sell_pool:
        return False, SAME_POOL
    if str(buy_pool).lower() == str(sell_pool).lower():
        return False, SAME_POOL
    return True, OK


def _pool_ids(value) -> set[str]:
    out: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if "pool" in str(key).lower() or "pool" in str(item).lower():
                out.add(str(item).lower())
            out |= _pool_ids(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            out |= _pool_ids(item)
    return out


def route_is_single_call(quote, *, buy_pool: str, sell_pool: str,
                         from_token: str, to_token: str) -> tuple[bool, str]:
    """Does this quote describe one call that visits both pools?

    The only shape we can prove is a **circular** route: same input and output
    token, with both pool ids present in the router's own list. Anything else is
    a directional swap, and two of those are two transactions.
    """
    if not isinstance(quote, dict) or not quote:
        return False, NO_ROUTE
    if str(from_token or "").lower() != str(to_token or "").lower():
        return False, NOT_CIRCULAR
    route = quote.get("dexRouterList") or quote.get("route") or []
    pools = _pool_ids(route) if route else set()
    if not pools:
        return False, NO_ROUTE
    for wanted in (buy_pool, sell_pool):
        if str(wanted or "").lower() not in pools:
            return False, f"{POOL_NOT_IN_ROUTE}:{wanted}"
    return True, OK


def net_usdt(*, notional_usdt, edge_bps, gas_usdt, slippage_bps,
             pool_fee_bps: Decimal = Decimal("0")) -> Decimal | None:
    """Round-trip net. A missing term makes it unknown, never cheaper."""
    if notional_usdt is None or edge_bps is None or gas_usdt is None:
        return None
    notional = Decimal(str(notional_usdt))
    bps = Decimal(str(slippage_bps)) + Decimal(str(pool_fee_bps))
    return (notional * Decimal(str(edge_bps)) / Decimal(10000)
            - Decimal(str(gas_usdt))
            - notional * bps / Decimal(10000))


# --------------------------------------------------------------------------
# Calldata for "one transaction, two pools"
# --------------------------------------------------------------------------
# Measured 2026-10-07 with real ``eth_call`` on X Layer: the router below runs
# ``multicall([exactInput(path), sweepToken(...)])`` in one transaction and it
# **succeeds** on two real, liquid pools. So a self-built router contract is
# not needed for the same-chain family; see the module docstring and lesson 26.
def word(value) -> str:
    """Left-pad an address, a ``0x`` hex int or a small hex literal to 32 bytes."""
    text = str(value).lower().removeprefix("0x")
    return text.rjust(64, "0")


def enc_bytes(hexdata: str) -> str:
    """ABI ``bytes`` tail: a length word, then the data padded to 32 bytes."""
    body = hexdata or ""
    padded = body.ljust(64 * ((len(body) + 63) // 64), "0") if body else ""
    return word(hex(len(body) // 2)) + padded


def enc_bytes_array(items: list[str]) -> str:
    """ABI tail for ``bytes[]`` from already-encoded element payloads."""
    blobs = [enc_bytes(item) for item in items]
    offsets: list[str] = []
    cursor = 32 * len(items)
    for blob in blobs:
        offsets.append(word(hex(cursor)))
        cursor += len(blob) // 2
    return word(hex(len(items))) + "".join(offsets) + "".join(blobs)


def encode_path(start: str, hops: list[tuple[str, int]]) -> str:
    """Uniswap v3 path: ``tokenIn || fee || tokenOut || fee || ...`` (no 0x).

    ``hops`` is ``[(token_out, fee), ...]``; ``start`` is ``token_in``.
    Every pool the route visits is therefore visible in the calldata, which is
    what ``route_is_single_call`` later reads back.
    """
    out = str(start).lower().removeprefix("0x")
    for token, fee in hops:
        out += hex(int(fee))[2:].rjust(6, "0") + str(token).lower().removeprefix("0x")
    return out


def exact_input_calldata(path: str, *, recipient: str, amount_in: int,
                         amount_out_min: int) -> str:
    """``exactInput((bytes,address,uint256,uint256))`` -- one call, all hops."""
    head = (word("0x80") + word(recipient) + word(hex(int(amount_in)))
            + word(hex(int(amount_out_min))))
    return (selector(EXACT_INPUT_SIGNATURE) + word("0x20") + head + enc_bytes(path))


def exact_input_single_calldata(token_in: str, token_out: str, fee: int,
                                *, recipient: str, amount_in: int,
                                amount_out_min: int = 0) -> str:
    """``exactInputSingle(...)`` -- one pool, used by the probe's ladder."""
    return (selector(EXACT_INPUT_SINGLE_SIGNATURE) + word(token_in) + word(token_out)
            + word(hex(int(fee))) + word(recipient) + word(hex(int(amount_in)))
            + word(hex(int(amount_out_min))) + word("0x0"))


def sweep_token_calldata(token: str, *, amount_min: int, recipient: str) -> str:
    """``sweepToken(address,uint256,address)`` -- move the output to the owner."""
    return (selector(SWEEP_TOKEN_SIGNATURE) + word(token) + word(hex(int(amount_min)))
            + word(recipient))


def multicall_calldata(calls: list[str]) -> str:
    """``multicall(bytes[])`` -- the whole ring in one transaction."""
    payloads = [call.removeprefix("0x") for call in calls]
    return selector(MULTICALL_SIGNATURE) + word("0x20") + enc_bytes_array(payloads)


def ring_calldata(*, start: str, hops: list[tuple[str, int]], router: str,
                  owner: str, amount_in: int, amount_out_min: int) -> dict:
    """The complete single-transaction ring: ``multicall([exactInput, sweepToken])``.

    ``exactInput`` is sent with the router as recipient, so the intermediate and
    final tokens stay inside the router until ``sweepToken`` hands the output to
    ``owner``. Both calls are one transaction; two separate ``swap`` calls are
    not this trade (module docstring).
    """
    path = encode_path(start, hops)
    swap = exact_input_calldata(path, recipient=router, amount_in=amount_in,
                                amount_out_min=amount_out_min)
    sweep = sweep_token_calldata(start, amount_min=amount_out_min,
                                 recipient=owner)
    return {
        "to": router,
        "data": multicall_calldata([swap, sweep]),
        "path": path,
        "pools_in_path": len(hops),
        "one_transaction": True,
        "call_count": 2,
    }


def plan(*, symbol: str, chain: str, buy_pool: str, sell_pool: str,
         edge_bps, notional_usdt, gas_usdt, policy, route_proof=None,
         from_token: str = "", to_token: str = "") -> dict:
    """The one-trade plan, or a refusal that names what is missing.

    ``route_proof`` is the aggregator quote row. Without it -- or when it does
    not prove a circular, two-pool, single call -- this returns a refusal. It
    never returns a two-leg plan, because a two-leg plan is a different, more
    dangerous trade that this family exists to avoid.
    """
    same, reason = same_chain_two_pool(
        buy_chain=chain, sell_chain=chain, buy_pool=buy_pool, sell_pool=sell_pool)
    if not same:
        return {"executable": False, "blockers": [reason], "symbol": symbol}
    ok, why = route_is_single_call(route_proof, buy_pool=buy_pool,
                                   sell_pool=sell_pool, from_token=from_token,
                                   to_token=to_token)
    if not ok:
        return {"executable": False, "blockers": [why], "symbol": symbol,
                "chain": chain, "one_transaction": False}
    # The proportional term comes from the shared model (``costs``), which for
    # this family is the slippage buffer and no Kraken taker fee. Reading the
    # policy field directly here was a second reader of the same number.
    net = net_usdt(notional_usdt=notional_usdt, edge_bps=edge_bps,
                   gas_usdt=gas_usdt,
                   slippage_bps=costs.proportional_bps(policy, family="dex_to_dex"))
    blockers = []
    if net is None:
        blockers.append("net_unknown")
    elif net < policy.min_net_profit_usdt:
        blockers.append("net_below_floor")
    return {
        "symbol": symbol,
        "chain": chain,
        "family": "same_chain_atomic",
        "buy_pool": buy_pool,
        "sell_pool": sell_pool,
        "notional_usdt": None if notional_usdt is None else str(notional_usdt),
        "edge_bps": None if edge_bps is None else str(edge_bps),
        "gas_usdt": None if gas_usdt is None else str(gas_usdt),
        "net_usdt": None if net is None else format(net, "f"),
        "one_transaction": True,
        "executable": not blockers,
        "blockers": blockers,
        "reason": blockers[0] if blockers else "",
        "broadcast": False,
    }
