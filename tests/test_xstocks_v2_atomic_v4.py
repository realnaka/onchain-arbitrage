"""Hermetic tests for the v4 ring (Ethereum, stage 2).

Every number pinned here was measured on chain on 2026-10-08, and the source of
each one is named, so the next reader can re-measure instead of guessing: the
pool ids come back out of ``keccak256(abi.encode(PoolKey))``, the action bytes
are the ones the deployed ``UniversalRouter`` answers to, and the parameter
layout is the deployment's (which differs from the current upstream tree in
three ways: no ``minHopPriceX36``, a leading pointer word on swap params, and
``SETTLE_ALL``/``TAKE_ALL`` that can never both fire).
"""
from __future__ import annotations

import unittest
from decimal import Decimal as D
from pathlib import Path

from arb_monitor.xstocks_v2 import atomic
from arb_monitor.xstocks_v2 import atomic_v4 as v4

SPCX = "0x68fa48b1c2fe52b3d776e1953e0e782b5044ce28"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
HOOK = "0xbf9828455cdc5f02771536e3ecb3c0f931eabec4"
#: The two pools from the 2026-10-08 board row (green buy, amber sell).
GREEN = v4.pool_key(token=SPCX, stable=USDC, fee=2500, tick_spacing=50)
AMBER = v4.pool_key(token=SPCX, stable=USDC, fee=8388608, tick_spacing=10,
                    hooks=HOOK)
NOTIONAL = 25_000_000                      # 25 USDC in raw units


def _words(hexbody: str) -> list[str]:
    body = hexbody.removeprefix("0x")
    return [body[index:index + 64] for index in range(0, len(body), 64)]


def _decode_execute(data: str) -> dict:
    """A small strict-ABI reader for ``execute(bytes,bytes[])`` calldata."""
    words = _words(data[10:])                 # drop the 4-byte selector
    assert words[0] == v4.word(0x40), "commands offset"
    commands_len = int(words[2], 16)
    commands = words[3][:commands_len * 2]
    inputs_at = int(words[1], 16) // 32
    count = int(words[inputs_at], 16)
    return {"selector": data[:10], "commands": commands, "count": count,
            "inputs_at": inputs_at, "words": words}


def _decode_v4_swap_input(blob: str) -> dict:
    """``abi.encode(bytes actions, bytes[] params)`` -- what the router decodes."""
    words = _words(blob)
    assert words[0] == v4.word(0x40), "actions offset must be 0x40 (strict ABI)"
    actions_len = int(words[2], 16)
    actions = words[3][:actions_len * 2]
    params_at = int(words[1], 16) // 32
    assert words[1] == v4.word(0x60 + ((actions_len + 31) // 32) * 32), \
        "params offset must follow the padded actions payload"
    count = int(words[params_at], 16)
    offsets = [int(words[params_at + 1 + index], 16) // 32
               for index in range(count)]
    elements = []
    for offset in offsets:
        # ABI: an element offset is measured from the element area, which starts
        # right after the array length word.
        start = params_at + 1 + offset
        length = int(words[start], 16)
        elements.append(words[start + 1:][:length // 32])
    return {"actions": actions, "count": count, "elements": elements}


def _swap_struct(words: list[str]) -> dict:
    """Read one swap packet the way the deployment's decoder does."""
    assert words[0] == v4.word(v4.SWAP_PARAM_POINTER), "leading pointer word"
    struct = words[1:]
    hook_offset = int(struct[8], 16) // 32
    return {"currency0": "0x" + struct[0][24:], "currency1": "0x" + struct[1][24:],
            "fee": int(struct[2], 16), "tick_spacing": int(struct[3], 16),
            "hooks": "0x" + struct[4][24:], "zero_for_one": int(struct[5], 16),
            "amount_in": int(struct[6], 16), "amount_out_min": int(struct[7], 16),
            "hook_offset_words": hook_offset,
            "hook_length": int(struct[hook_offset], 16),
            "packet_words": len(words)}


def _swap_actions(plan: dict) -> dict:
    """The ``V4_SWAP`` input sits right after ``execute``'s two head words."""
    words = _words(plan["data"][10:])
    inputs_at = int(words[1], 16) // 32
    offsets = [int(words[inputs_at + 1 + index], 16) // 32
               for index in range(int(words[inputs_at], 16))]
    blob_words = words[inputs_at + 1 + offsets[0]:]
    length = int(blob_words[0], 16)
    return _decode_v4_swap_input("0x" + "".join(blob_words[1:][:length // 32]))


class AddressAndKeyTest(unittest.TestCase):
    def test_pool_ids_match_the_census_ids(self):
        """A v4 pool id is ``keccak256(abi.encode(PoolKey))``, so it is checkable."""
        self.assertEqual(
            v4.pool_id(GREEN),
            "0x9b8f0f0d632b8f8cd1d682a8349cb4d9ec10a7497416580c325f7987f5f2d485")
        self.assertEqual(
            v4.pool_id(AMBER),
            "0x527e4a83d6c7e05f65d04c0dec46de20eec3a8f2c450ed7ec73ead33701e5ba7")

    def test_currency_zero_is_the_smaller_address(self):
        self.assertEqual(GREEN["currency0"], SPCX)
        self.assertEqual(GREEN["currency1"], USDC)
        flipped = v4.pool_key(token=USDC, stable=SPCX, fee=2500, tick_spacing=50)
        self.assertEqual(flipped, GREEN)

    def test_addresses_are_the_measured_mainnet_ones(self):
        self.assertEqual(v4.POOL_MANAGER["eth"],
                         "0x000000000004444c5dc75cb358380d2e3de08a90")
        self.assertEqual(v4.UNIVERSAL_ROUTER["eth"],
                         "0x66a9893cc07d91d95644aedd05d03f95e1dba8af")
        self.assertEqual(v4.PERMIT2["eth"],
                         "0x000000000022d473030f116ddee9f6b43ac78ba3")
        self.assertEqual(v4.V4_QUOTER["eth"],
                         "0x52f0e24d1c21c8a0cb1e5a5dd6198556bd9e1203")


class ActionBytesTest(unittest.TestCase):
    """The deployment's own numbering, not the current upstream tree."""

    def test_command_and_actions_are_the_deployed_ones(self):
        self.assertEqual(v4.COMMAND_V4_SWAP, 0x10)
        self.assertEqual(v4.A_SWAP_EXACT_IN_SINGLE, 0x06)
        self.assertEqual(v4.A_SETTLE, 0x0B)
        self.assertEqual(v4.A_SETTLE_ALL, 0x0C)
        self.assertEqual(v4.A_TAKE_ALL, 0x0F)

    def test_selectors_are_the_router_and_error_selectors(self):
        self.assertEqual(v4.EXECUTE_SELECTOR, "0x24856bc3")
        self.assertEqual(v4.selector("CurrencyNotSettled()"), "0x5212cba1")
        self.assertEqual(v4.selector("DeltaNotNegative(address)"), "0x3351b260")
        self.assertEqual(v4.selector("DeltaNotPositive(address)"), "0x4c085bf1")
        self.assertEqual(v4.selector("AllowanceExpired(uint256)"), "0xd81b2f2e")

    def test_the_deployed_swap_struct_has_no_min_hop_price_field(self):
        """The packet is pointer + 8 fields + offset + length; the decoder wants
        at least 0x140 (10 words), which is the same struct **without** the
        pointer -- so the pointer is an extra word on top of its own minimum."""
        params = v4.swap_exact_in_single_params(
            GREEN, zero_for_one=False, amount_in=1, amount_out_min=0,
            layout=v4.SWAP_LAYOUT_V1)
        self.assertEqual(len(params) // 64, 11)      # pointer + struct(9) + len
        self.assertEqual(v4.DEFAULT_SWAP_LAYOUT, v4.SWAP_LAYOUT_V1)
        self.assertGreaterEqual(len(params) // 64, 0x140 // 32)
        wider = v4.swap_exact_in_single_params(
            GREEN, zero_for_one=False, amount_in=1, amount_out_min=0,
            layout=v4.SWAP_LAYOUT_V2)
        self.assertEqual(len(wider) // 64, 12)

    def test_swap_params_are_pointed_at_and_settle_params_are_not(self):
        """Measured 2026-10-08: two conventions inside one action list."""
        params = v4.swap_exact_in_single_params(
            GREEN, zero_for_one=False, amount_in=NOTIONAL, amount_out_min=0)
        self.assertEqual(_words(params)[0], v4.word(0x20))
        self.assertEqual(v4.settle_all_params(USDC, max_amount=1),
                         v4.addr_word(USDC) + v4.word(1))
        self.assertEqual(len(_words(v4.settle_params(USDC, amount=1))), 3)


class SwapPacketTest(unittest.TestCase):
    def test_the_packet_is_the_layout_the_deployed_decoder_reads(self):
        params = v4.swap_exact_in_single_params(
            GREEN, zero_for_one=False, amount_in=NOTIONAL, amount_out_min=7,
            hook_data="aabb")
        struct = _swap_struct(_words(params))
        self.assertEqual(struct["currency0"], SPCX)
        self.assertEqual(struct["currency1"], USDC)
        self.assertEqual(struct["fee"], 2500)
        self.assertEqual(struct["tick_spacing"], 50)
        self.assertEqual(struct["zero_for_one"], 0)
        self.assertEqual(struct["amount_in"], NOTIONAL)
        self.assertEqual(struct["amount_out_min"], 7)
        # 8 static words + the offset word itself: the offset points at the tail.
        self.assertEqual(struct["hook_offset_words"], 9)
        self.assertEqual(struct["hook_length"], 2)

    def test_hook_data_offset_is_relative_to_the_struct_not_the_packet(self):
        plain = v4.swap_exact_in_single_params(
            AMBER, zero_for_one=True, amount_in=0, amount_out_min=1)
        self.assertEqual(_words(plain)[1:][8], v4.word(0x120))
        self.assertEqual(_swap_struct(_words(plain))["hook_length"], 0)


class RingShapeTest(unittest.TestCase):
    def _ring(self, **kwargs):
        base = dict(buy_key=GREEN, sell_key=AMBER, token_address=SPCX,
                    quote_address=USDC, amount_in=NOTIONAL,
                    sell_amount_out_min=NOTIONAL + 1)
        base.update(kwargs)
        return v4.ring_calldata(**base)

    def test_one_execute_call_with_two_swaps_and_one_take(self):
        plan = self._ring()
        self.assertTrue(plan["executable"], plan)
        self.assertEqual(plan["call_count"], 1)
        self.assertEqual(plan["swap_count"], 2)
        self.assertEqual(plan["to"], v4.UNIVERSAL_ROUTER["eth"])
        decoded = _decode_execute(plan["data"])
        self.assertEqual(decoded["selector"], "0x24856bc3")
        self.assertEqual(decoded["commands"], "10")          # V4_SWAP
        self.assertEqual(decoded["count"], 1)
        inner = _swap_actions(plan)
        self.assertEqual(inner["actions"], "06060f")          # 2 swaps + TAKE_ALL
        self.assertEqual(inner["count"], 3)

    def test_the_buy_leg_spends_the_quote_and_the_sell_leg_spends_the_token(self):
        plan = self._ring()
        self.assertFalse(plan["buy_zero_for_one"])   # USDC is currency1 -> oneForZero
        self.assertTrue(plan["sell_zero_for_one"])   # SPCXx is currency0
        inner = _swap_actions(plan)
        buy = _swap_struct(inner["elements"][0])
        sell = _swap_struct(inner["elements"][1])
        self.assertEqual(buy["amount_in"], NOTIONAL)
        self.assertEqual(buy["zero_for_one"], 0)
        # The sell leg spends the buy leg's whole output: ``OPEN_DELTA`` = 0.
        self.assertEqual(sell["amount_in"], 0)
        self.assertEqual(sell["zero_for_one"], 1)
        self.assertEqual(sell["amount_out_min"], NOTIONAL + 1)

    def test_gross_mode_pays_the_ticket_and_takes_the_proceeds(self):
        plan = self._ring(mode=v4.RING_MODE_GROSS, profit_floor=0)
        inner = _swap_actions(plan)
        self.assertEqual(inner["actions"], "06060b0f")        # + SETTLE
        self.assertEqual(len(inner["elements"]), 4)
        settle = inner["elements"][2]
        self.assertEqual(settle[0], v4.addr_word(USDC))
        self.assertEqual(int(settle[1], 16), NOTIONAL)
        self.assertEqual(int(settle[2], 16), 1)              # payerIsUser

    def test_a_profit_mode_plan_cannot_carry_a_loss_cap(self):
        """``TAKE_ALL`` reverts on a negative delta, so a cap there would be dead."""
        plan = self._ring(loss_cap=NOTIONAL // 100)
        self.assertFalse(plan["executable"])
        self.assertEqual(plan["reason"], "profit_mode_cannot_express_a_loss_cap")

    def test_the_same_pool_twice_is_not_a_ring(self):
        plan = self._ring(sell_key=GREEN)
        self.assertFalse(plan["executable"])
        self.assertEqual(plan["blockers"], [v4.SAME_POOL])

    def test_a_pool_that_does_not_hold_this_pair_is_refused(self):
        other = v4.pool_key(token="0x1aad217b8f78dba5e6693460e8470f8b1a3977f3",
                            stable=USDC, fee=10000, tick_spacing=200)
        plan = self._ring(sell_key=other)
        self.assertFalse(plan["executable"])
        self.assertEqual(plan["blockers"], ["currency_mismatch"])

    def test_the_ring_is_only_ever_one_call(self):
        """A source guard: two swaps must never become two ``execute`` calls."""
        source = Path(v4.__file__).read_text()
        self.assertEqual(source.count("def execute_calldata("), 1)
        self.assertEqual(source.count('"call_count"'), 1)
        probe = (Path(v4.__file__).resolve().parents[3]
                 / "scripts" / "xstocks_v2_atomic_v4_probe.py").read_text()
        self.assertEqual(probe.count("def broadcast("), 1)


class CostModelAndPermit2Test(unittest.TestCase):
    def test_the_ring_uses_the_shared_cost_model(self):
        from arb_monitor.xstocks_v2.policy import V2Policy
        policy = V2Policy.load()
        net = v4.ring_net_usdt(notional_usdt=D("25"), edge_bps=D("7.0806"),
                               gas_usdt=D("0"), policy=policy)
        # 25 x 7.0806 bps = 0.0177, minus the 10 bps buffer = -0.0073.
        self.assertLess(net, 0)
        self.assertEqual(net, atomic.net_usdt(
            notional_usdt=D("25"), edge_bps=D("7.0806"), gas_usdt=D("0"),
            slippage_bps=policy.slippage_buffer_bps))

    def test_approve_and_allowance_calldata_shapes(self):
        approve = v4.permit2_approve_calldata(USDC, v4.UNIVERSAL_ROUTER["eth"],
                                              1_000_000, 1_800_000_000)
        self.assertEqual(approve[:10],
                         v4.selector("approve(address,address,uint160,uint48)"))
        self.assertEqual(approve[10:74], v4.addr_word(USDC))
        self.assertEqual(approve[74:138],
                         v4.addr_word(v4.UNIVERSAL_ROUTER["eth"]))
        self.assertEqual(v4.erc20_approve_calldata(v4.PERMIT2["eth"], 1) [:10],
                         "0x095ea7b3")

    def test_permit2_allowance_decoding(self):
        amount = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF
        packed = v4.word(amount) + v4.word((1 << 48) - 1) + v4.word(3)
        got = v4.decode_permit2_allowance("0x" + packed)
        self.assertEqual(got["amount"], amount)
        self.assertEqual(got["expiration"], (1 << 48) - 1)
        self.assertEqual(got["nonce"], 3)


if __name__ == "__main__":
    unittest.main()
