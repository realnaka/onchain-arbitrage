"""Every directed chain pair has one verdict, and no_lane never broadcasts."""
from __future__ import annotations

import unittest
from decimal import Decimal as D

from arb_monitor.xstocks_v2 import capability as cap
from arb_monitor.xstocks_v2 import live
from arb_monitor.xstocks_v2 import wrap as wrap_core
from arb_monitor.xstocks_v2.ccip_evm import SOURCE_SLOT0
from arb_monitor.xstocks_v2.ccip_solana import (
    LANE_CONFIG_DISCRIMINATOR, instruction_for_dest, parse_lane_config,
    solana_dest_selector)
from arb_monitor.xstocks_v2.paths import ALL_CHAINS


class CapabilityCoverageTest(unittest.TestCase):
    def test_every_ordered_pair_has_one_status(self):
        pairs = cap.all_bridge_pairs()
        self.assertEqual(len(pairs), len(ALL_CHAINS) * (len(ALL_CHAINS) - 1))
        for source, dest, status in pairs:
            self.assertIn(status, cap.STATUSES, f"{source}->{dest}")
            self.assertNotEqual(source, dest)

    def test_a_missing_edge_is_no_lane_and_ton_is_not_on_the_bridge(self):
        self.assertEqual(cap.bridge_status("solana", "mantle"), cap.NO_LANE)
        self.assertEqual(cap.bridge_status("ton", "eth"), cap.NO_LANE)
        self.assertEqual(cap.bridge_status("eth", "ton"), cap.NO_LANE)

    def test_chain_to_kraken_still_covers_the_four_hubs(self):
        hubs = cap.executor_chains()[cap.CHAIN_TO_KRAKEN]
        self.assertEqual(hubs, frozenset({"solana", "eth", "ton", "x-layer"}))
        self.assertEqual(live.EXECUTOR_CHAINS[live.CHAIN_TO_KRAKEN], hubs)

    def test_xlayer_to_solana_is_ready_and_solana_to_eth_is_ready(self):
        self.assertEqual(cap.bridge_status("x-layer", "solana"), cap.READY)
        self.assertEqual(cap.bridge_status("solana", "eth"), cap.READY)
        self.assertEqual(cap.bridge_status("solana", "monad"), cap.NO_SELECTOR)

    def test_bsc_can_sign_and_optimism_to_solana_cannot(self):
        self.assertEqual(cap.bridge_status("bsc", "eth"), cap.READY)
        self.assertEqual(cap.bridge_status("arbitrum", "eth"), cap.READY)
        self.assertEqual(cap.bridge_status("ink", "eth"), cap.READY)
        self.assertEqual(cap.bridge_status("hyperevm", "eth"), cap.READY)
        self.assertEqual(cap.bridge_status("optimism", "eth"), cap.READY)
        self.assertEqual(cap.bridge_status("optimism", "solana"), cap.NO_SELECTOR)
        self.assertNotIn("optimism", SOURCE_SLOT0)

    def test_no_lane_never_calls_broadcast_even_when_the_cost_is_free(self):
        called = []
        result = cap.attempt(status=cap.NO_LANE, cost_usdt=D("0"),
                             broadcast=lambda: called.append(1))
        self.assertFalse(result["sent"])
        self.assertEqual(called, [])

    def test_a_ready_edge_over_two_usdt_is_not_sent(self):
        called = []
        result = cap.attempt(status=cap.READY, cost_usdt=D("2.01"),
                             broadcast=lambda: called.append(1))
        self.assertEqual(result["reason"], "over_cap")
        self.assertEqual(called, [])

    def test_a_ready_edge_inside_the_cap_is_sent(self):
        called = []
        result = cap.attempt(status=cap.READY, cost_usdt=D("0.40"),
                             broadcast=lambda: called.append("yes"))
        self.assertTrue(result["sent"])
        self.assertEqual(called, ["yes"])

    def test_compose_names_buy_bridge_sell_and_does_not_broadcast(self):
        plan = cap.compose("eth", "x-layer")
        self.assertEqual(plan["steps"], ("buy", "bridge", "sell"))
        self.assertFalse(plan["broadcast"])
        blocked = cap.compose("solana", "mantle")
        self.assertEqual(blocked["steps"], ())
        self.assertFalse(blocked["broadcast"])


class ConstructorBytesTest(unittest.TestCase):
    def test_solana_destination_is_not_only_x_layer(self):
        self.assertIsNotNone(solana_dest_selector("eth"))
        data = instruction_for_dest(
            dest_chain="eth", recipient="0xabababababababababababababababababababab",
            product_id=1, amount=1)
        self.assertIn(solana_dest_selector("eth").to_bytes(8, "little"), data)

    def test_an_unknown_solana_destination_is_refused(self):
        with self.assertRaises(ValueError):
            instruction_for_dest(
                dest_chain="monad", recipient="0xabababababababababababababababababababab",
                product_id=1, amount=1)

    def test_lane_config_parses_the_selector(self):
        selector = solana_dest_selector("eth")
        raw = (LANE_CONFIG_DISCRIMINATOR
               + selector.to_bytes(8, "little")
               + b"\x00\x00"
               + (0).to_bytes(4, "little")
               + bytes.fromhex("abababababababababababababababababababab")
               + b"\x00" * 12)
        parsed, bridge = parse_lane_config(raw)
        self.assertEqual(parsed, selector)
        self.assertEqual(bridge, "0xabababababababababababababababababababab")

    def test_wrap_and_redeem_use_the_erc4626_selectors(self):
        deposit = wrap_core.deposit_calldata(
            assets=10, receiver="0xabababababababababababababababababababab")
        redeem = wrap_core.redeem_calldata(
            shares=10, receiver="0xabababababababababababababababababababab",
            owner="0xabababababababababababababababababababab")
        self.assertTrue(deposit.startswith("0x" + wrap_core.DEPOSIT_SELECTOR))
        self.assertTrue(redeem.startswith("0x" + wrap_core.REDEEM_SELECTOR))


if __name__ == "__main__":
    unittest.main()
