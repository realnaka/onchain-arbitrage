#!/usr/bin/env python3
"""Read-only probe for stage 2: can one X Layer transaction visit two pools?

Usage:
    scripts/xstocks_v2_atomic_probe.py discover
    scripts/xstocks_v2_atomic_probe.py pools --tokens USDG,wSPYx,USDC,USDT0,WOKB
    scripts/xstocks_v2_atomic_probe.py simulate

Both modes are read-only: they call ``eth_call`` and never sign, never approve
and never broadcast. ``discover`` verifies the Uniswap v3 X Layer deployment
table against the chain instead of trusting it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.core.evm import keccak256                       # noqa: E402
from arb_monitor.xstocks_v2 import atomic                        # noqa: E402
from arb_monitor.xstocks_v2.feeds.http import Http               # noqa: E402

RPC = "https://rpc.xlayer.tech"

FACTORY = "0x4B2ab38DBF28D31D467aA8993f6c2585981D6804"
QUOTER_V2 = "0xd1b797d92d87b688193a2b976efc8d577d204343"
SWAP_ROUTER_02 = "0x4f0c28f5926afda16bf2506d5d9e57ea190f9bca"
MULTICALL = "0xe2023f3fa515cf070e07fd9d51c1d236e07843f4"
POSITION_MANAGER = "0x315e413A11AB0df498eF83873012430ca36638Ae"
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"

WOKB = "0xe538905cf8410324e03a5a23c1c177a474d59b2b"
USDG = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"
WSPYX = "0xe7e553cd128f0011777323a0b44a7b96ea1cb540"
SPYX_USDG_POOL = "0x07c40850d14064d20eb0afdef9574675392f2c11"
OUR_WALLET = "0xabababababababababababababababababababab"

TOKENS = {
    "USDG": USDG,
    "wSPYx": WSPYX,
    "WOKB": WOKB,
    # Both from the issuer's own X Layer deployment row
    # (data/xstocks_book.sqlite `deployments`.stables_json), not guessed.
    "USDC": "0xb6ceceab302e2e4948951ee7843fc24e92933061",
}
FEE_TIERS = (100, 500, 3000, 10000)


def sel(signature: str) -> str:
    """Same selector table the executor uses -- one implementation, not two."""
    return atomic.selector(signature).removeprefix("0x")


def rpc(method: str, params: list, *, http=None) -> object:
    http = http or Http()
    _status, payload = http.post_json(
        RPC, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if not isinstance(payload, dict):
        raise RuntimeError(f"rpc {method}: bad payload")
    if "error" in payload:
        raise RuntimeError(f"rpc {method}: {payload['error']}")
    return payload.get("result")


def call(to: str, data: str, *, http=None, sender: str | None = None) -> str:
    tx: dict = {"to": to, "data": data}
    if sender:
        tx["from"] = sender
    return rpc("eth_call", [tx, "latest"], http=http) or "0x"


def code_size(address: str, *, http=None) -> int:
    code = rpc("eth_getCode", [address, "latest"], http=http) or "0x"
    return max(0, (len(code) - 2) // 2)


def word(value: str) -> str:
    return atomic.word(value)


def enc_bytes(hexdata: str) -> str:
    return atomic.enc_bytes(hexdata)


def enc_bytes_array(items: list[str]) -> str:
    return atomic.enc_bytes_array(items)


def as_address(result: str) -> str:
    if not result or len(result) < 42:
        return ""
    return "0x" + result[-40:]


def as_uint(result: str) -> int:
    if not result or result == "0x":
        return 0
    return int(result, 16)


def discover(*, http=None) -> dict:
    http = http or Http()
    out: dict = {"chain_id": rpc("eth_chainId", [], http=http), "contracts": {}}
    for name, address in (("factory", FACTORY), ("quoter_v2", QUOTER_V2),
                          ("swap_router_02", SWAP_ROUTER_02),
                          ("multicall", MULTICALL),
                          ("position_manager", POSITION_MANAGER),
                          ("permit2", PERMIT2)):
        out["contracts"][name] = {"address": address,
                                  "code_bytes": code_size(address, http=http)}
    router_factory = as_address(call(SWAP_ROUTER_02, "0x" + sel("factory()"), http=http))
    router_weth = as_address(call(SWAP_ROUTER_02, "0x" + sel("WETH9()"), http=http))
    out["router"] = {
        "factory": router_factory,
        "factory_matches": router_factory.lower() == FACTORY.lower(),
        "weth9": router_weth,
        "weth9_matches_wokb": router_weth.lower() == WOKB.lower(),
    }
    pool_factory = as_address(call(SPYX_USDG_POOL, "0x" + sel("factory()"), http=http))
    out["spyx_pool"] = {
        "address": SPYX_USDG_POOL,
        "factory": pool_factory,
        "factory_matches": pool_factory.lower() == FACTORY.lower(),
        "token0": as_address(call(SPYX_USDG_POOL, "0x" + sel("token0()"), http=http)),
        "token1": as_address(call(SPYX_USDG_POOL, "0x" + sel("token1()"), http=http)),
        "fee": as_uint(call(SPYX_USDG_POOL, "0x" + sel("fee()"), http=http)),
        "liquidity": as_uint(call(SPYX_USDG_POOL, "0x" + sel("liquidity()"), http=http)),
    }
    return out


def get_pool(a: str, b: str, fee: int, *, http=None) -> str:
    data = ("0x" + sel("getPool(address,address,uint24)") + word(a) + word(b)
            + hex(fee)[2:].rjust(64, "0"))
    return as_address(call(FACTORY, data, http=http))


def pool_table(token_names: list[str], *, http=None) -> list[dict]:
    rows: list[dict] = []
    for i, left in enumerate(token_names):
        for right in token_names[i + 1:]:
            for fee in FEE_TIERS:
                pool = get_pool(TOKENS[left], TOKENS[right], fee, http=http)
                if pool and int(pool, 16) != 0:
                    rows.append({"pair": f"{left}/{right}", "fee": fee, "pool": pool,
                                 "code_bytes": code_size(pool, http=http)})
    return rows


# --------------------------------------------------------------------------
# ring simulation: one exactInput over two real pools, in one transaction
# --------------------------------------------------------------------------
def encode_path(hops: list[tuple[str, int]]) -> str:
    """Delegates to ``atomic.encode_path``; the caller prepends the start token."""
    return atomic.encode_path("", hops)


def mapping_slot(owner: str, mapping: int) -> str:
    """``keccak256(abi.encode(owner, uint256(mapping)))``."""
    return "0x" + keccak256(
        bytes.fromhex(word(owner) + hex(mapping)[2:].rjust(64, "0"))).hex()


def allowance_slot(owner: str, spender: str, *, mapping_index: int) -> str:
    """Storage slot of ``allowance[owner][spender]`` for a nested mapping."""
    inner = mapping_slot(owner, mapping_index)
    outer = keccak256(bytes.fromhex(word(spender) + inner[2:]))
    return "0x" + outer.hex()


def find_allowance_slot(token: str, owner: str, spender: str, *, http=None) -> tuple[int, str]:
    """Which mapping index holds ``allowance[owner][spender]``?

    USDG on X Layer is not plain OpenZeppelin: its *balances* mapping sits at
    slot 1 and its *allowances* mapping at slot 3 (measured 2026-10-07 by
    overriding one slot and re-reading ``allowance()``). Guessing would make
    the simulation revert for the wrong reason, so the index is probed.
    """
    data = "0x" + sel("allowance(address,address)") + word(owner) + word(spender)
    for index in range(0, 8):
        slot = allowance_slot(owner, spender, mapping_index=index)
        override = {token: {"stateDiff": {slot: "0x" + "f" * 64}}}
        try:
            raw = rpc("eth_call", [{"to": token, "data": data}, "latest", override],
                      http=http)
        except RuntimeError:
            continue
        if raw and raw != "0x" and int(raw, 16) != 0:
            return index, slot
    raise RuntimeError("no allowance mapping slot accepted a state override")


def find_balance_slot(token: str, owner: str, *, http=None) -> tuple[int, str, int]:
    """Same probe for ``balanceOf[owner]``; also returns the real balance."""
    data = "0x" + sel("balanceOf(address)") + word(owner)
    real = int(rpc("eth_call", [{"to": token, "data": data}, "latest"], http=http) or "0x0", 16)
    for index in range(0, 8):
        slot = mapping_slot(owner, index)
        override = {token: {"stateDiff": {slot: "0x" + "f" * 64}}}
        try:
            raw = rpc("eth_call", [{"to": token, "data": data}, "latest", override],
                      http=http)
        except RuntimeError:
            continue
        if raw and raw != "0x" and int(raw, 16) == int("f" * 64, 16):
            return index, slot, real
    raise RuntimeError(f"no balance mapping slot for {token}")


def decode_uint(raw: str, index: int = 0) -> int:
    body = (raw or "0x")[2:]
    chunk = body[index * 64:(index + 1) * 64]
    return int(chunk, 16) if chunk else 0


def quote_exact_input(path: str, amount_in: int, *, http=None) -> int | None:
    """QuoterV2.quoteExactInput(bytes,uint256). It answers by reverting."""
    data = "0x" + sel("quoteExactInput(bytes,uint256)") \
        + hex(64)[2:].rjust(64, "0") + hex(amount_in)[2:].rjust(64, "0") \
        + hex((len(path) // 2))[2:].rjust(64, "0") + path
    try:
        out = call(QUOTER_V2, data, http=http)
    except RuntimeError as exc:
        text = str(exc)
        marker = "'data': '0x"
        if marker in text:
            payload = text.split(marker, 1)[1].split("'", 1)[0]
            return decode_uint(payload, 0)
        return None
    return decode_uint(out, 0)


def simulate_ring(*, ring: str, amount_in: int, min_out: int = 1,
                  sender: str = OUR_WALLET, http=None) -> dict:
    """eth_call ``multicall([exactInput, sweepToken])`` from our own wallet.

    A real approve has not happened, so the simulation adds a state override
    that sets ``allowance[us][router]`` to max. The override is verified by
    re-reading the allowance through the same override before the swap runs.
    """
    http = http or Http()
    spec = RINGS[ring]
    start = spec["start"]
    path = start.lower().replace("0x", "") + encode_path(spec["hops"])
    expected_out = quote_exact_input(path, amount_in, http=http)
    # A ring starts and ends in the same token, so a plausible quote is at most
    # the input. Anything larger is a decoding artefact and is not used as a
    # floor -- an invented minimum would make the simulation fail for the wrong
    # reason (measured 2026-10-07: a bad decode produced 8.8e76).
    if expected_out is not None and not (0 < expected_out <= amount_in):
        expected_out = None
    if min_out <= 1 and expected_out:
        min_out = max(1, expected_out * (10000 - 10) // 10000)

    # The calldata comes from the executor's own module: the probe must prove
    # the *shipping* encoding, not a second copy of it.
    ring_call = atomic.ring_calldata(
        start=start, hops=spec["hops"], router=SWAP_ROUTER_02, owner=sender,
        amount_in=amount_in, amount_out_min=min_out)
    multicall = ring_call["data"]

    index, slot = find_allowance_slot(start, sender, SWAP_ROUTER_02, http=http)
    override = {start: {"stateDiff": {slot: "0x" + "f" * 64}}}
    before = None
    try:
        raw = rpc("eth_call", [{"to": start,
                                "data": "0x" + sel("allowance(address,address)")
                                + word(sender) + word(SWAP_ROUTER_02)},
                               "latest", override], http=http)
        before = int(raw, 16) if raw and raw != "0x" else None
    except RuntimeError as exc:
        before = f"override_error:{str(exc)[:80]}"

    result: dict = {
        "ring": ring, "path": path, "amount_in": amount_in,
        "amount_out_min": min_out, "quoter_amount_out": expected_out,
        "allowance_with_override": str(before),
        "allowance_slot": slot, "allowance_mapping_index": index,
        "call": {"to": SWAP_ROUTER_02, "from": sender,
                 "selector": "multicall(bytes[])"},
    }
    code = rpc("eth_getCode", [SWAP_ROUTER_02, "latest"], http=http) or ""
    for name, signature in (("exactInput", "exactInput((bytes,address,uint256,uint256))"),
                            ("multicall", "multicall(bytes[])"),
                            ("sweepToken", "sweepToken(address,uint256,address)")):
        result.setdefault("selector_check", {})[name] = \
            ("63" + sel(signature)) in code.lower()
    try:
        out = rpc("eth_call", [{"to": SWAP_ROUTER_02, "data": multicall,
                                "from": sender}, "latest", override], http=http)
        result["ok"] = True
        result["raw_return"] = out
        result["amount_out"] = decode_uint(out, 1) if out and len(out) > 130 else None
    except RuntimeError as exc:
        result["ok"] = False
        result["error"] = str(exc)[:400]
    return result


RINGS = {
    # Two real USDG/WOKB pools, different fee tiers: the minimal two-pool ring.
    "usdg-wokb": {
        "start": USDG,
        "hops": [(WOKB, 100), (USDG, 500)],
        "pools": ["0x01cd955cba093127a5f6f8c7ded4fb773e150761",
                  "0x5d7e3ad08b0c52e460787677b0632cd024df437c"],
        "note": "USDG -100-> WOKB -500-> USDG, two distinct pools, both liquid "
                "(4.8e10 and 6.65e16 measured 2026-10-07)",
    },
    # The shape the SPYx family actually needs: the deep wSPYx/USDG pool, the
    # wSPYx/USDC pool, and the USDG/USDC pool, all real and all on X Layer.
    "spyx": {
        "start": USDG,
        "hops": [(WSPYX, 500), ("0xb6ceceab302e2e4948951ee7843fc24e92933061", 500),
                 (USDG, 100)],
        "pools": ["0x07c40850d14064d20eb0afdef9574675392f2c11",
                  "0x0f74ee629ee186dc49fc9f78bb5d5856c0ca8231",
                  "0xbb9a35f790ea6ea9763b99e885f33bcf95860d40"],
        "note": "USDG -500-> wSPYx -500-> USDC -100-> USDG. Visits the SPYx pool "
                "but is blocked: the wSPYx/USDC pool has liquidity 0 "
                "(measured 2026-10-07), so this is not an executable route",
    },
    # Three real liquid pools: proves a multi-pool single call even though the
    # two SPYx pools we would want do not both have liquidity.
    "usdg-usdc-wokb": {
        "start": USDG,
        "hops": [("0xb6ceceab302e2e4948951ee7843fc24e92933061", 100),
                 (WOKB, 500), (USDG, 500)],
        "pools": ["0xbb9a35f790ea6ea9763b99e885f33bcf95860d40",
                  "0x9f73888fb725a3409548bc8d2e42365ac063e148",
                  "0x5d7e3ad08b0c52e460787677b0632cd024df437c"],
        "note": "USDG -100-> USDC -500-> WOKB -500-> USDG, three liquid pools",
    },
}


def _try(to: str, data: str, *, override, sender: str, http) -> dict:
    try:
        out = rpc("eth_call", [{"to": to, "data": data, "from": sender}, "latest",
                               override], http=http)
        return {"ok": True, "result": out}
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)[:240]}


def exact_input_single(token_in: str, token_out: str, fee: int, amount_in: int,
                       recipient: str) -> str:
    return atomic.exact_input_single_calldata(
        token_in, token_out, fee, recipient=recipient, amount_in=amount_in)


def exact_input_path(path: str, recipient: str, amount_in: int, min_out: int) -> str:
    return atomic.exact_input_calldata(path, recipient=recipient,
                                       amount_in=amount_in, amount_out_min=min_out)


def ladder(*, ring: str, amount_in: int, sender: str = OUR_WALLET, http=None) -> dict:
    """Run the ring one piece at a time so a failure names its own step."""
    http = http or Http()
    spec = RINGS[ring]
    start = spec["start"]
    _, slot = find_allowance_slot(start, sender, SWAP_ROUTER_02, http=http)
    override = {start: {"stateDiff": {slot: "0x" + "f" * 64}}}
    hops = spec["hops"]
    # Every hop on its own: a hop we cannot fund is funded by a state override
    # so that a missing balance never gets confused with a broken pool.
    override_all = dict(override)
    for token, _fee in hops[:-1]:
        try:
            _b_idx, b_slot, _real = find_balance_slot(token, sender, http=http)
            _a_idx, a_slot = find_allowance_slot(token, sender, SWAP_ROUTER_02, http=http)
        except RuntimeError:
            continue
        override_all.setdefault(token, {"stateDiff": {}})["stateDiff"][b_slot] = \
            hex(10 ** 24)[2:].rjust(64, "0")
        override_all[token]["stateDiff"][a_slot] = "0x" + "f" * 64
    full_path = start.lower().replace("0x", "") + encode_path(hops)
    first_path = (start.lower().replace("0x", "")
                  + encode_path([(hops[0][0], hops[0][1])]))
    two_path = (start.lower().replace("0x", "") + encode_path(hops[:2]))
    steps: dict = {}
    steps["multicall_ring"] = _try(
        SWAP_ROUTER_02,
        atomic.ring_calldata(start=start, hops=hops, router=SWAP_ROUTER_02,
                             owner=sender, amount_in=amount_in,
                             amount_out_min=0)["data"],
        override=override_all, sender=sender, http=http)
    for i, (token_in, fee) in enumerate(hops):
        token_out = hops[i + 1][0] if i + 1 < len(hops) else start
        steps[f"single_hop_{i + 1}"] = _try(
            SWAP_ROUTER_02,
            exact_input_single(token_in, token_out, fee, amount_in, sender),
            override=override_all, sender=sender, http=http)
    steps["single_hop_1"] = _try(
        SWAP_ROUTER_02,
        exact_input_single(start, hops[0][0], hops[0][1], amount_in, sender),
        override=override_all, sender=sender, http=http)
    steps["exactInput_1hop"] = _try(
        SWAP_ROUTER_02, exact_input_path(first_path, sender, amount_in, 0),
        override=override_all, sender=sender, http=http)
    if len(hops) >= 2:
        steps["exactInput_2hops"] = _try(
            SWAP_ROUTER_02, exact_input_path(two_path, sender, amount_in, 0),
            override=override_all, sender=sender, http=http)
    steps["exactInput_ring"] = _try(
        SWAP_ROUTER_02, exact_input_path(full_path, sender, amount_in, 0),
        override=override_all, sender=sender, http=http)
    steps["quoter_multi_hop"] = quote_exact_input(full_path, amount_in, http=http)
    for name, row in steps.items():
        if isinstance(row, dict) and row.get("ok"):
            steps[name] = {"ok": True, "amount_out": decode_uint(row.get("result"), 0)}
    return {"ring": ring, "amount_in": amount_in, "path": full_path, "steps": steps}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("discover")
    table = sub.add_parser("pools")
    table.add_argument("--tokens", default="USDG,wSPYx,USDC,WOKB")
    sim = sub.add_parser("simulate")
    sim.add_argument("--ring", default="spyx", choices=sorted(RINGS))
    sim.add_argument("--amount", default="1000000",
                     help="raw amount of the start token (USDG is 6 decimals)")
    sim.add_argument("--min-out", default="0")
    lad = sub.add_parser("ladder")
    lad.add_argument("--ring", default="spyx", choices=sorted(RINGS))
    lad.add_argument("--amount", default="1000000")
    args = parser.parse_args()
    if args.cmd == "discover":
        print(json.dumps(discover(), ensure_ascii=False, indent=1))
        return 0
    if args.cmd == "simulate":
        result = simulate_ring(ring=args.ring, amount_in=int(args.amount),
                               min_out=int(args.min_out or "0"))
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    if args.cmd == "ladder":
        print(json.dumps(ladder(ring=args.ring, amount_in=int(args.amount)),
                         ensure_ascii=False, indent=1))
        return 0
    rows = pool_table([t.strip() for t in args.tokens.split(",") if t.strip()])
    print(f"pools found: {len(rows)}")
    for row in rows:
        print(f"  {row['pair']:16s} fee={row['fee']:<6d} {row['pool']} "
              f"code={row['code_bytes']}B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
