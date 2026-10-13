"""EVM pool quoting: v3 QuoterV2 and v4 Quoter, both directions.

Encoding is separated from transport so the calldata can be asserted in
hermetic tests, and a JSON-RPC batch keeps one round trip for many pools.

Two measured constraints are pinned here:

* **X Layer rejects batches above 10 calls** with ``-32014``. The chunk size is
  per chain, not a global constant.
* **A failing pool must not drop the chain.** ``tryAggregate`` and the batch
  decoder both keep per-call errors next to their call, so one bad pool is
  reported and skipped (lessons 6.7: a Gate contract row once removed an
  entire venue from monitoring without any visible error).
"""
from __future__ import annotations

import json
from decimal import Decimal

from arb_monitor.xstocks_v2.feeds.http import Http

# Uniswap QuoterV2 is the same CREATE2 address on most EVM chains. The census
# carries a per-pool quoter when a chain has more than one (Pancake on BSC).
V3_QUOTE_SELECTOR = "c6a5026a"      # quoteExactInputSingle(...)
V4_QUOTE_SELECTOR = "aa9d21cb"      # quoteExactInputSingle(PoolKey,bool,uint128,bytes)
TRY_AGGREGATE_SELECTOR = "bce38bd7"  # tryAggregate(bool,(address,bytes)[])
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"


class Chain:
    def __init__(self, key: str, rpc: str, *, batch_max: int = 50,
                 quoter: str | None = None, v4_quoter: str | None = None):
        self.key = key
        self.rpc = rpc
        self.batch_max = batch_max
        self.quoter = quoter
        self.v4_quoter = v4_quoter


CHAINS: dict[str, Chain] = {
    "eth": Chain("eth", "https://ethereum-rpc.publicnode.com", batch_max=20,
                 quoter="0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
                 v4_quoter="0x52f0e24d1c21c8a0cb1e5a5dd6198556bd9e1203"),
    "x-layer": Chain("x-layer", "https://rpc.xlayer.tech", batch_max=10,
                     quoter="0xd1b797d92d87b688193a2b976efc8d577d204343"),
    "bsc": Chain("bsc", "https://bsc-dataseed.binance.org", batch_max=20,
                 quoter="0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997"),
    "arbitrum": Chain("arbitrum", "https://arb1.arbitrum.io/rpc", batch_max=20,
                      quoter="0x61fFE014bA17989E743c5F6cB21bF9697530B21e"),
    "optimism": Chain("optimism", "https://optimism-rpc.publicnode.com", batch_max=20,
                      quoter="0x61fFE014bA17989E743c5F6cB21bF9697530B21e"),
    "ink": Chain("ink", "https://rpc-gel.inkonchain.com", batch_max=20),
    "monad": Chain("monad", "https://rpc.monad.xyz", batch_max=20),
    "hyperevm": Chain("hyperevm", "https://rpc.hyperliquid.xyz/evm", batch_max=20),
    "mantle": Chain("mantle", "https://rpc.mantle.xyz", batch_max=20),
}


def word(value: int) -> str:
    """A 32-byte word. Negative values wrap, as ABI encoding requires."""
    return format(int(value) & (2 ** 256 - 1), "064x")


def addr_word(address: str) -> str:
    if not address.startswith("0x") or len(address) != 42:
        raise ValueError(f"不是 EVM 地址：{address}")
    return address[2:].lower().rjust(64, "0")


def decode_uint(result: object) -> int | None:
    """First word of an ``eth_call`` result, as an integer."""
    if not isinstance(result, str) or not result.startswith("0x") or len(result) < 66:
        return None
    try:
        return int(result[2:66], 16)
    except ValueError:
        return None


def encode_v3_quote(token_in: str, token_out: str, amount_in: int, fee: Decimal,
                    *, sqrt_limit: int = 0) -> str:
    """``quoteExactInputSingle((tokenIn,tokenOut,amountIn,fee,sqrtPriceLimitX96))``."""
    fee_units = int(Decimal(fee) * Decimal(1_000_000))
    return ("0x" + V3_QUOTE_SELECTOR + addr_word(token_in) + addr_word(token_out)
            + word(amount_in) + word(fee_units) + word(sqrt_limit))


def encode_v4_quote(pool_key: dict, zero_for_one: bool, amount_in: int) -> str:
    """``quoteExactInputSingle((PoolKey,bool,uint128,bytes))``.

    The PoolKey is an inline struct behind one offset word; hookData is empty
    but still needs its own offset and length word. This layout is what the
    X Layer probe used successfully on 2026-10-07.
    """
    parts = [
        word(0x20),
        addr_word(pool_key["currency0"]),
        addr_word(pool_key["currency1"]),
        word(int(pool_key["fee"])),
        word(int(pool_key["tick_spacing"])),
        addr_word(pool_key["hooks"]),
        word(1 if zero_for_one else 0),
        word(amount_in),
        word(0x100),
        word(0),
    ]
    return "0x" + V4_QUOTE_SELECTOR + "".join(parts)


def encode_try_aggregate(calls: list[tuple[str, str]]) -> str:
    """Multicall3 ``tryAggregate(false, calls)``. Failure stays per call."""
    elements: list[str] = []
    for target, data in calls:
        blob = data[2:] if data.startswith("0x") else data
        if len(blob) % 2:
            raise ValueError("calldata 的十六进制长度必须是偶数")
        pad = (-len(blob)) % 64
        # Each element is (address target, uint256 offset=0x40, uint256 len, bytes).
        elements.append(addr_word(target) + word(0x40) + word(len(blob) // 2)
                        + blob + "0" * pad)
    offsets, cursor = [], 32 * len(calls)
    for element in elements:
        offsets.append(cursor)
        cursor += len(element) // 2
    array = (word(len(calls)) + "".join(word(offset) for offset in offsets)
             + "".join(elements))
    return ("0x" + TRY_AGGREGATE_SELECTOR + word(0x40) + word(0) + array)


def decode_try_aggregate(result: object) -> list[dict] | None:
    """``(bool success, bytes returnData)[]`` -> one row per call, in order."""
    if not isinstance(result, str) or not result.startswith("0x") or len(result) < 130:
        return None
    raw = result[2:]
    try:
        # Word 0 is the byte offset of the array; the array itself starts with
        # its length, and each element offset is relative to just after that.
        array_at = int(raw[0:64], 16) * 2
        count = int(raw[array_at:array_at + 64], 16)
    except ValueError:
        return None
    if count > 1024:
        return None
    out: list[dict] = []
    for index in range(count):
        slot = array_at + 64 + index * 64
        try:
            item = array_at + 64 + int(raw[slot:slot + 64], 16) * 2
            success = int(raw[item:item + 64], 16) == 1
            # element[1] is the offset to its bytes (0x40), element[2] is length.
            length = int(raw[item + 128:item + 192], 16)
            data = raw[item + 192:item + 192 + length * 2]
        except ValueError:
            return None
        out.append({"success": success,
                    "result": "0x" + data if length else "",
                    "value": decode_uint("0x" + data) if length >= 32 else None})
    return out


def rpc_batch(http: Http, chain: Chain, calls: list[tuple[str, str] | None]) -> list[dict]:
    """``eth_call`` for many pools in as few round trips as the chain allows.

    ``calls`` may contain ``None`` for a slot that was already known bad; the
    returned list keeps the same length and index so callers can zip them.
    """
    out: list[dict] = [{"error": "not_sent"} if call is None else {}
                       for call in calls]
    pending = [(index, call) for index, call in enumerate(calls) if call is not None]
    for start in range(0, len(pending), chain.batch_max):
        group = pending[start:start + chain.batch_max]
        payload = [
            {"jsonrpc": "2.0", "id": index, "method": "eth_call",
             "params": [{"to": target, "data": data}, "latest"]}
            for index, (target, data) in group
        ]
        try:
            status, body = http.request(
                chain.rpc, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
        except Exception as exc:  # noqa: BLE001
            for index, _call in group:
                out[index] = {"error": f"transport:{type(exc).__name__}"}
            continue
        if status != 200:
            for index, _call in group:
                out[index] = {"error": f"http_{status}"}
            continue
        try:
            rows = json.loads(body.decode("utf-8", "replace"))
        except (ValueError, AttributeError):
            for index, _call in group:
                out[index] = {"error": "bad_payload"}
            continue
        if not isinstance(rows, list):
            for index, _call in group:
                out[index] = {"error": "not_a_batch"}
            continue
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            index = row.get("id")
            if index not in {item[0] for item in group}:
                continue
            seen.add(index)
            if "error" in row:
                out[index] = {"error": str((row.get("error") or {}).get("message")
                                           or row["error"])[:80]}
            else:
                out[index] = {"result": row.get("result")}
        for index, _call in group:
            if index not in seen and not out[index]:
                out[index] = {"error": "missing_from_batch"}
    return out


def gas_price_wei(http: Http, chain: Chain) -> int | None:
    """``eth_gasPrice``. Unknown is None; it is never assumed to be zero."""
    payload = [{"jsonrpc": "2.0", "id": 1, "method": "eth_gasPrice", "params": []}]
    try:
        status, body = http.request(chain.rpc, data=json.dumps(payload).encode(),
                                    headers={"Content-Type": "application/json"})
    except Exception:  # noqa: BLE001
        return None
    if status != 200:
        return None
    try:
        rows = json.loads(body.decode("utf-8", "replace"))
        return int(rows[0]["result"], 16)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None
