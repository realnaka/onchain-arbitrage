"""Stage 2 executor: one transaction, two pools, or a refusal.

The rule is not "prefer atomic" -- it is "atomic or nothing", because two swaps
leave a naked position between them and the first one cannot be undone. The only
route that proves one call is a circular quote that visits both pools.
"""
from __future__ import annotations

import importlib.util
import unittest
from decimal import Decimal as D
from pathlib import Path

from arb_monitor.xstocks_v2 import atomic
from arb_monitor.xstocks_v2.policy import V2Policy

QUOTE = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"      # USDG
BUY = "0x07c40850f1b1f1e1d0f3f0b2e8b0a1c2d3e4f5a6"
SELL = "0x1111111111111111111111111111111111111111"
MID = "0xe538905cf8410324e03a5a23c1c177a474d59b2b"        # WOKB


def _script():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "v2atomicx", root / "scripts" / "xstocks_v2_atomic_execute.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(**over):
    row = {"symbol": "SPYx", "family": "dex_to_dex", "chain": "x-layer",
           "buy_pool": BUY, "sell_pool": SELL, "quote_address": QUOTE,
           "from_token": QUOTE, "to_token": QUOTE,
           "edge_t0_bps": "80", "notional_usdt": "200",
           "hops": [(MID, 500), (QUOTE, 500)]}
    row.update(over)
    return row


def _proof(*, buy=BUY, sell=SELL, tokens=(QUOTE, QUOTE)):
    # ``route_is_single_call`` reads ``dexRouterList`` or ``route``, and
    # ``_pool_ids`` only collects dicts whose key or value mentions "pool" --
    # a plain list of id strings carries no ids at all.
    return {"route": [{"pool": buy}, {"pool": sell}],
            "inputMint": tokens[0], "outputMint": tokens[1]}


class OneCallOrNothingTest(unittest.TestCase):
    def setUp(self):
        self.script = _script()
        self.policy = V2Policy.load()

    def test_without_a_route_proof_there_is_no_plan(self):
        plan = self.script.build_plan(_row(), policy=self.policy)
        self.assertFalse(plan["executable"])
        self.assertEqual(plan["one_transaction"], False)
        self.assertNotIn("calldata", plan)

    def test_a_two_leg_reading_is_never_accepted(self):
        """Two swaps are a different, more dangerous trade -- not a fallback."""
        plan = self.script.build_plan(
            _row(from_token=QUOTE, to_token=MID, route_proof=_proof()),
            policy=self.policy)
        self.assertFalse(plan["executable"])

    def test_a_proven_ring_produces_the_multicall_calldata(self):
        plan = self.script.build_plan(
            _row(route_proof=_proof()), policy=self.policy,
            available_usdt=D("200"), gas_usdt=D("0.0012"))
        self.assertTrue(plan["executable"], plan)
        calldata = plan["calldata"]
        self.assertEqual(calldata["pools_in_path"], 2)
        self.assertEqual(calldata["call_count"], 2)       # exactInput + sweepToken
        self.assertEqual(calldata["to"], atomic.SWAP_ROUTER_02["x-layer"])
        self.assertTrue(calldata["data"].startswith("0x"))
        # The size rule caps the notional at available x 0.999 before it is
        # encoded: 200 x 0.999 = 199.8 USDG.
        self.assertEqual(plan["amount_in_raw"], 199_800_000)

    def test_hops_are_required_even_with_a_proof(self):
        """A proof without the fee tiers cannot be encoded."""
        plan = self.script.build_plan(
            _row(hops=None, route_proof=_proof()), policy=self.policy,
            available_usdt=D("200"))
        self.assertFalse(plan["executable"])
        self.assertEqual(plan["reason"], "no_hops")

    def test_a_negative_edge_is_refused_by_the_shared_model(self):
        plan = self.script.build_plan(
            _row(edge_t0_bps="-5", route_proof=_proof()), policy=self.policy,
            available_usdt=D("200"))
        self.assertFalse(plan["executable"])
        self.assertIn("net_below_floor", plan["blockers"])

    def test_the_executor_never_builds_two_swap_calls(self):
        source = Path(self.script.__file__).read_text()
        # The ring is one multicall; a second `swap execute` here would be the
        # naked-position trade this family exists to forbid.
        self.assertNotIn('"swap", "execute"', source)
        self.assertIn("ring_calldata", source)


if __name__ == "__main__":
    unittest.main()
