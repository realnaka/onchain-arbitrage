"""Same-chain, two-pool round trips on Uniswap **v4** (stage 2, Ethereum).

``atomic.py`` covers the v3 case on X Layer (``SwapRouter02`` +
``multicall([exactInput, sweepToken])``). Ethereum's xStock pools are v4, which
is a different machine: there is no per-pool router, one ``PoolManager`` holds
every pool, and a swap is a call from a contract that the manager calls back
into while an ``unlock`` is open. So the single-transaction ring is expressed as
**one** ``UniversalRouter.execute`` call carrying one ``V4_SWAP`` command whose
action list does both swaps, then settles and takes the result:

```text
execute(commands = 0x10, inputs = [abi.encode(actions, params)])
    actions = 0x06 SWAP_EXACT_IN_SINGLE   pool A: USDC  -> SPCXx
              0x06 SWAP_EXACT_IN_SINGLE   pool B: SPCXx -> USDC
              0x0c SETTLE_ALL             pay the net USDC the ring owes (capped)
              0x0f TAKE_ALL               take the net USDC the ring earned
```

Two swaps are *not* this trade, so the module never emits two calls (one
``execute`` or nothing -- the same rule as :mod:`atomic`). Flash accounting is
what makes the ring cheap: the SPCXx leg nets to zero inside the manager, so
only the USDC shortfall is ever pulled from the wallet.

Nothing here is guessed:

* the two pool ids below are recomputed from the derived ``PoolKey`` and
  compared with the ids the census recorded (``test_pool_ids_match_the_census``);
* the action bytes and the parameter layout are read from Uniswap's published
  source **and** then measured against the deployed router (``probe_actions``
  in ``scripts/xstocks_v2_atomic_v4_probe.py``), because the mainnet deployment
  predates later struct fields.
"""
from __future__ import annotations

from decimal import Decimal

from arb_monitor.core.evm import keccak256
from arb_monitor.xstocks_v2 import atomic, costs

# ---------------------------------------------------------------------------
# Addresses, verified with ``eth_getCode`` on 2026-10-08 (chain id 1)
# ---------------------------------------------------------------------------
#: v4 ``PoolManager`` (24 009 B of code), the canonical mainnet deployment.
POOL_MANAGER = {"eth": "0x000000000004444c5dc75cb358380d2e3de08a90"}
#: ``UniversalRouter`` (19 499 B). It runs the v4 actions for us, so no
#: contract of ours has to be deployed for this family.
UNIVERSAL_ROUTER = {"eth": "0x66a9893cc07d91d95644aedd05d03f95e1dba8af"}
#: ``Permit2`` (9 152 B). v4 settles pull through it: ``Currency.transferFrom``
#: routes through Permit2 whenever the recipient is not the caller, and the
#: recipient here is the PoolManager.
PERMIT2 = {"eth": "0x000000000022d473030f116ddee9f6b43ac78ba3"}
#: Official v4 Quoter (already used by the scan for these pools).
V4_QUOTER = {"eth": "0x52f0e24d1c21c8a0cb1e5a5dd6198556bd9e1203"}

# ---------------------------------------------------------------------------
# Command and action bytes
# ---------------------------------------------------------------------------
#: ``Commands.V4_SWAP`` -- UniversalRouter hands the input to the v4 module.
COMMAND_V4_SWAP = 0x10

#: ``Actions`` (v4-periphery). Read from the published source on 2026-10-08;
#: ``probe_actions`` re-measures them against the deployed router, because the
#: mainnet deployment is older than the tree this list was read from.
A_SWAP_EXACT_IN_SINGLE = 0x06
A_SWAP_EXACT_IN = 0x07
A_SETTLE = 0x0B
A_SETTLE_ALL = 0x0C
A_TAKE = 0x0E
A_TAKE_ALL = 0x0F
A_SWEEP = 0x14

#: ``ExactInputSingleParams`` has two shapes in the wild. The deployed router
#: answers exactly one of them. Measured 2026-10-08 against the deployed
#: ``UniversalRouter`` (Sourcify source + on-chain probe): the deployment wants
#: **v1**, and its decoder demands ``params.length >= 0x140`` (10 words).
#:   v1 = (PoolKey, bool, uint128 amountIn, uint128 amountOutMinimum, bytes hookData)
#:   v2 = same + uint256 minHopPriceX36 before hookData (added 2025)
SWAP_LAYOUT_V1 = "v1"
SWAP_LAYOUT_V2 = "v2"
DEFAULT_SWAP_LAYOUT = SWAP_LAYOUT_V1

#: Every swap blob is a **pointer** to the struct: the decoder does
#: ``swapParams := add(params.offset, calldataload(params.offset))``. Settle and
#: take blobs are read directly (``currency := calldataload(params.offset)``).
#: Two conventions in one action list -- measured, not guessed.
SWAP_PARAM_POINTER = 0x20

EXECUTE_SIGNATURE = "execute(bytes,bytes[])"
PERMIT2_APPROVE_SIGNATURE = "approve(address,address,uint160,uint48)"
PERMIT2_ALLOWANCE_SIGNATURE = "allowance(address,address,address)"
ERC20_APPROVE_SIGNATURE = "approve(address,uint256)"
ERC20_ALLOWANCE_SIGNATURE = "allowance(address,address)"

OK = atomic.OK
NOT_SAME_CHAIN = atomic.NOT_SAME_CHAIN
SAME_POOL = atomic.SAME_POOL


def selector(signature: str) -> str:
    return "0x" + keccak256(signature.encode())[:4].hex()


EXECUTE_SELECTOR = selector(EXECUTE_SIGNATURE)


# ---------------------------------------------------------------------------
# ABI helpers (strings of hex, same style as ``atomic.py``)
# ---------------------------------------------------------------------------
def word(value) -> str:
    """A 32-byte word from an int, a ``0x`` hex string or a decimal string."""
    if isinstance(value, str):
        text = value.lower().removeprefix("0x")
        if text and all(char in "0123456789abcdef" for char in text):
            return text.rjust(64, "0")
        value = int(value)
    return format(int(value) & ((1 << 256) - 1), "064x")


def addr_word(address: str) -> str:
    return str(address).lower().removeprefix("0x").rjust(64, "0")


def enc_bytes(hexdata: str) -> str:
    """ABI ``bytes``: a length word, then the payload padded to 32 bytes."""
    body = str(hexdata).removeprefix("0x")
    padded = body.ljust(64 * ((len(body) + 63) // 64), "0") if body else ""
    return word(len(body) // 2) + padded


def enc_bytes_array(items: list[str]) -> str:
    """ABI ``bytes[]`` from already-encoded element payloads (no ``0x``)."""
    blobs = [enc_bytes(item) for item in items]
    offsets, cursor = [], 32 * len(items)
    for blob in blobs:
        offsets.append(word(cursor))
        cursor += len(blob) // 2
    return word(len(items)) + "".join(offsets) + "".join(blobs)


# ---------------------------------------------------------------------------
# PoolKey: the id is recomputed, never trusted
# ---------------------------------------------------------------------------
def pool_key(*, token: str, stable: str, fee: int, tick_spacing: int,
             hooks: str = "0x" + "0" * 40) -> dict:
    """The sorted v4 ``PoolKey`` (currency0 < currency1, fee is raw)."""
    currency0, currency1 = sorted([str(token).lower(), str(stable).lower()])
    return {"currency0": currency0, "currency1": currency1, "fee": int(fee),
            "tick_spacing": int(tick_spacing),
            "hooks": (hooks or "0x" + "0" * 40).lower()}


def encode_pool_key(key: dict) -> str:
    """``abi.encode(PoolKey)`` -- the preimage of the pool id."""
    return (addr_word(key["currency0"]) + addr_word(key["currency1"])
            + word(key["fee"]) + word(key["tick_spacing"])
            + addr_word(key["hooks"]))


def pool_id(key: dict) -> str:
    """``keccak256(abi.encode(PoolKey))`` -- how v4 names a pool."""
    return "0x" + keccak256(bytes.fromhex(encode_pool_key(key))).hex()


# ---------------------------------------------------------------------------
# The ring: two v4 swaps in one unlock, settle and take the net
# ---------------------------------------------------------------------------
def swap_exact_in_single_params(key: dict, *, zero_for_one: bool, amount_in: int,
                                amount_out_min: int, hook_data: str = "",
                                layout: str = DEFAULT_SWAP_LAYOUT) -> str:
    """``ExactInputSingleParams`` for one pool (no ``0x``), pointer included."""
    if layout not in (SWAP_LAYOUT_V1, SWAP_LAYOUT_V2):
        raise ValueError(f"unknown swap layout {layout!r}")
    head = (encode_pool_key(key) + word(1 if zero_for_one else 0)
            + word(amount_in) + word(amount_out_min))
    if layout == SWAP_LAYOUT_V2:
        head += word(0)                       # minHopPriceX36 = "no per-hop bar"
    # ``hookData``'s offset is measured from the start of the struct, and the
    # head includes the offset word itself: 8 static words + 1 offset word.
    offset = len(head) // 2 + 0x20
    struct_blob = head + word(offset) + enc_bytes(hook_data)
    return word(SWAP_PARAM_POINTER) + struct_blob


def settle_all_params(currency: str, *, max_amount: int) -> str:
    """``SETTLE_ALL(Currency,uint256 maxAmount)`` -- pay the ring's net debt."""
    return addr_word(currency) + word(max_amount)


def settle_params(currency: str, *, amount: int, payer_is_user: bool = True) -> str:
    """``SETTLE(Currency,uint256 amount,bool payerIsUser)`` -- pay a stated amount."""
    return addr_word(currency) + word(amount) + word(1 if payer_is_user else 0)


def take_all_params(currency: str, *, min_amount: int) -> str:
    """``TAKE_ALL(Currency,uint256 minAmount)`` -- take the ring's net credit."""
    return addr_word(currency) + word(min_amount)


#: Two shapes for closing a **ring** (same currency in and out). Measured
#: 2026-10-08 on the deployed router: ``SETTLE_ALL`` reverts ``DeltaNotNegative``
#: when the delta is positive, and ``TAKE_ALL`` reverts ``DeltaNotPositive`` when
#: it is negative. So a single action list can only ever close one sign:
#:   ``profit`` = [swap, swap, TAKE_ALL]            -- the ring must end positive
#:   ``gross``  = [swap, swap, SETTLE, TAKE_ALL]    -- pay the ticket, take it
#:                back. Valid when the ring wins *or* loses, and both amounts
#:                show up in the receipt, which is what the ledger wants.
RING_MODE_PROFIT = "profit"
RING_MODE_GROSS = "gross"
DEFAULT_RING_MODE = RING_MODE_PROFIT


def v4_swap_input(actions: list[int], params: list[str]) -> str:
    """``abi.encode(bytes actions, bytes[] params)`` -- the ``V4_SWAP`` input."""
    if len(actions) != len(params):
        raise ValueError("every action needs exactly one params blob")
    action_bytes = "".join(format(int(action) & 0xFF, "02x") for action in actions)
    actions_blob = enc_bytes(action_bytes)
    # Strict ABI: offset to `actions` is 0x40, then `params` starts right after
    # the (padded) actions payload.
    params_offset = 0x40 + len(actions_blob) // 2
    return (word(0x40) + word(params_offset) + actions_blob
            + enc_bytes_array(params))


def execute_calldata(commands: bytes | str, inputs: list[str]) -> str:
    """``UniversalRouter.execute(bytes commands, bytes[] inputs)``."""
    command_hex = (commands.hex() if isinstance(commands, (bytes, bytearray))
                   else str(commands).removeprefix("0x"))
    commands_blob = enc_bytes(command_hex)
    inputs_offset = 0x40 + len(commands_blob) // 2
    body = (word(0x40) + word(inputs_offset) + commands_blob
            + enc_bytes_array([item.removeprefix("0x") for item in inputs]))
    return EXECUTE_SELECTOR + body


def ring_calldata(*, buy_key: dict, sell_key: dict, token_address: str,
                  quote_address: str,
                  amount_in: int, sell_amount_out_min: int,
                  profit_floor: int = 0, loss_cap: int = 0,
                  buy_amount_out_min: int = 0,
                  mode: str = DEFAULT_RING_MODE,
                  layout: str = DEFAULT_SWAP_LAYOUT, chain: str = "eth",
                  hook_data_buy: str = "", hook_data_sell: str = "") -> dict:
    """The whole ring as **one** ``execute`` call, or a refusal.

    ``amount_in`` is the USDC the buy leg spends. The ring is only worth sending
    when the cost model says so; that judgement is the caller's, not this
    function's.

    Four numbers guard the money, and they guard different things:

    * ``buy_amount_out_min`` -- tokens the buy leg must return (0 = no bar);
    * ``sell_amount_out_min`` -- USDC the **sell** leg must return. This is the
      price protection: the sell leg spends the whole buy output
      (``ActionConstants.OPEN_DELTA`` = 0 means "use the full credit"), so a
      spread that vanished makes this leg revert;
    * ``profit_floor`` -- ``TAKE_ALL`` minimum. In ``profit`` mode the credit is
      ``sell_out - amount_in``, so a positive value demands a profit; in
      ``gross`` mode the credit is the whole ``sell_out``, so the bar is the
      USDC the ring has to hand back;
    * ``loss_cap`` -- only ``gross`` mode can carry one (see ``RING_MODE_*``):
      it is refused loudly in ``profit`` mode instead of being ignored.

    ``mode``:

    * ``profit`` -- ``[swap, swap, TAKE_ALL]``. Cheapest, and the on-chain shape
      of "this spread is real": if the ring does not end positive, ``TAKE_ALL``
      reverts and nothing happens;
    * ``gross`` -- ``[swap, swap, SETTLE(ticket), TAKE_ALL]``. Pay ``amount_in``,
      take whatever the sell leg produced. It is the only shape that works when
      the ring loses, and both amounts land in the receipt, which is what the
      ledger and the reconciliation read.
    """
    same_key = (encode_pool_key(buy_key) == encode_pool_key(sell_key))
    if same_key:
        return {"executable": False, "blockers": [SAME_POOL],
                "reason": SAME_POOL}
    if not POOL_MANAGER.get(chain) or not UNIVERSAL_ROUTER.get(chain):
        return {"executable": False, "blockers": ["no_router"], "reason": "no_router"}
    quote = str(quote_address).lower()
    token = str(token_address).lower()
    # Direction comes from the currency each leg **spends**, never from a caller
    # flag: ``zeroForOne`` means "spend currency0". The buy leg spends the
    # quote, the sell leg spends the token. Getting this backwards is silent --
    # the router happily swaps the other way -- so both keys are checked to
    # hold the same pair first.
    for label, key in (("buy", buy_key), ("sell", sell_key)):
        if {key["currency0"].lower(), key["currency1"].lower()} != {token, quote}:
            return {"executable": False, "blockers": ["currency_mismatch"],
                    "reason": f"{label}_pool_is_not_token_quote"}
    buy_zero_for_one = quote == buy_key["currency0"].lower()
    sell_zero_for_one = token == sell_key["currency0"].lower()
    if buy_zero_for_one == sell_zero_for_one:
        return {"executable": False, "blockers": ["not_a_ring"],
                "reason": "not_a_ring"}
    if mode not in (RING_MODE_PROFIT, RING_MODE_GROSS):
        raise ValueError(f"unknown ring mode {mode!r}")
    if mode == RING_MODE_PROFIT and loss_cap:
        # TAKE_ALL cannot express "which loss is acceptable": a negative delta
        # reverts before any cap is read. Refuse instead of shipping a number
        # that has no effect.
        return {"executable": False, "blockers": ["loss_cap_not_expressible"],
                "reason": "profit_mode_cannot_express_a_loss_cap"}
    swap_buy = swap_exact_in_single_params(
        buy_key, zero_for_one=buy_zero_for_one, amount_in=amount_in,
        amount_out_min=buy_amount_out_min, hook_data=hook_data_buy, layout=layout)
    swap_sell = swap_exact_in_single_params(
        sell_key, zero_for_one=sell_zero_for_one, amount_in=0,
        amount_out_min=sell_amount_out_min, hook_data=hook_data_sell,
        layout=layout)
    if mode == RING_MODE_PROFIT:
        actions = [A_SWAP_EXACT_IN_SINGLE, A_SWAP_EXACT_IN_SINGLE, A_TAKE_ALL]
        params = [swap_buy, swap_sell,
                  take_all_params(quote, min_amount=profit_floor)]
    else:
        actions = [A_SWAP_EXACT_IN_SINGLE, A_SWAP_EXACT_IN_SINGLE, A_SETTLE,
                   A_TAKE_ALL]
        params = [swap_buy, swap_sell,
                  settle_params(quote, amount=amount_in, payer_is_user=True),
                  take_all_params(quote, min_amount=profit_floor)]
    swap_input = v4_swap_input(actions, params)
    data = execute_calldata(bytes([COMMAND_V4_SWAP]), [swap_input])
    return {
        "executable": True,
        "one_transaction": True,
        "to": UNIVERSAL_ROUTER[chain],
        "pool_manager": POOL_MANAGER[chain],
        "permit2": PERMIT2[chain],
        "data": data,
        "commands": ["v4_swap"],
        "actions": actions,
        "call_count": 1,
        "swap_count": 2,
        "layout": layout,
        "buy_pool_id": pool_id(buy_key),
        "sell_pool_id": pool_id(sell_key),
        "quote_address": quote,
        "token_address": token,
        "buy_zero_for_one": buy_zero_for_one,
        "sell_zero_for_one": sell_zero_for_one,
        "amount_in_raw": int(amount_in),
        "buy_amount_out_min_raw": int(buy_amount_out_min),
        "sell_amount_out_min_raw": int(sell_amount_out_min),
        "profit_floor_raw": int(profit_floor),
        "loss_cap_raw": int(loss_cap),
        "sell_leg_uses_full_buy_output": True,
    }


# ---------------------------------------------------------------------------
# Allowances: ERC20 -> Permit2, then Permit2 -> router
# ---------------------------------------------------------------------------
def erc20_approve_calldata(spender: str, amount: int) -> str:
    return selector(ERC20_APPROVE_SIGNATURE) + addr_word(spender) + word(amount)


def erc20_allowance_calldata(owner: str, spender: str) -> str:
    return selector(ERC20_ALLOWANCE_SIGNATURE) + addr_word(owner) + addr_word(spender)


def permit2_approve_calldata(token: str, spender: str, amount: int,
                             expiration: int) -> str:
    """``Permit2.approve(token, spender, amount, expiration)``."""
    return (selector(PERMIT2_APPROVE_SIGNATURE) + addr_word(token)
            + addr_word(spender) + word(amount) + word(expiration))


def permit2_allowance_calldata(owner: str, token: str, spender: str) -> str:
    return (selector(PERMIT2_ALLOWANCE_SIGNATURE) + addr_word(owner)
            + addr_word(token) + addr_word(spender))


def decode_permit2_allowance(result: str) -> dict:
    """``(uint160 amount, uint48 expiration, uint48 nonce)`` from a public read."""
    body = str(result or "").removeprefix("0x")
    if len(body) < 192:
        return {"amount": None, "expiration": None, "nonce": None}
    return {"amount": int(body[0:64], 16),
            "expiration": int(body[64:128], 16),
            "nonce": int(body[128:192], 16)}


def ring_net_usdt(*, notional_usdt, edge_bps, gas_usdt, policy) -> Decimal | None:
    """The same shared cost model every other family uses (no second formula)."""
    return atomic.net_usdt(
        notional_usdt=notional_usdt, edge_bps=edge_bps, gas_usdt=gas_usdt,
        slippage_bps=costs.proportional_bps(policy, family="dex_to_dex"))
