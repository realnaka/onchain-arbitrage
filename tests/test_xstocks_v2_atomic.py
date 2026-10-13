"""Hermetic tests for stage 2: same-chain two-pool must be ONE transaction."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from arb_monitor.xstocks_v2 import atomic, scan
from arb_monitor.xstocks_v2.policy import V2Policy
from arb_monitor.xstocks_v2.spot_venue import BUY, REAL, SELL, Quote


def _board():
    """The read-only board, loaded the way it is run (it is a script)."""
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "v2board_atomic", root / "scripts" / "xstocks_v2_board.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SameChainTest(unittest.TestCase):
    def test_the_same_chain_two_pools_is_the_family(self):
        self.assertEqual(atomic.same_chain_two_pool(
            buy_chain="x-layer", sell_chain="x-layer",
            buy_pool="0xA", sell_pool="0xB"), (True, atomic.OK))

    def test_two_chains_are_a_bridge_trade_not_an_atomic_one(self):
        ok, reason = atomic.same_chain_two_pool(
            buy_chain="x-layer", sell_chain="solana",
            buy_pool="0xA", sell_pool="poolB")
        self.assertFalse(ok)
        self.assertEqual(reason, atomic.NOT_SAME_CHAIN)

    def test_one_pool_twice_is_not_a_spread(self):
        for buy_pool, sell_pool in (("0xA", "0xa"), ("0xA", ""), ("", "0xA")):
            ok, reason = atomic.same_chain_two_pool(
                buy_chain="x-layer", sell_chain="x-layer",
                buy_pool=buy_pool, sell_pool=sell_pool)
            self.assertFalse(ok, (buy_pool, sell_pool))
            self.assertEqual(reason, atomic.SAME_POOL)


class RouteProofTest(unittest.TestCase):
    POOLS = ("0xaaa", "0xbbb")

    def _route(self, pools):
        return {"dexRouterList": [{"pool": pool} for pool in pools]}

    def test_a_circular_two_pool_call_is_proven(self):
        ok, reason = atomic.route_is_single_call(
            self._route(self.POOLS), buy_pool=self.POOLS[0], sell_pool=self.POOLS[1],
            from_token="USDG", to_token="usdg")
        self.assertTrue(ok, reason)

    def test_a_directional_swap_is_not_a_round_trip(self):
        ok, reason = atomic.route_is_single_call(
            self._route(self.POOLS), buy_pool=self.POOLS[0], sell_pool=self.POOLS[1],
            from_token="USDG", to_token="SPYx")
        self.assertFalse(ok)
        self.assertEqual(reason, atomic.NOT_CIRCULAR)

    def test_a_route_that_skips_a_pool_is_not_a_proof(self):
        ok, reason = atomic.route_is_single_call(
            self._route((self.POOLS[0], "0xccc")), buy_pool=self.POOLS[0],
            sell_pool=self.POOLS[1], from_token="USDG", to_token="USDG")
        self.assertFalse(ok)
        self.assertTrue(reason.startswith(atomic.POOL_NOT_IN_ROUTE), reason)

    def test_no_route_at_all_is_a_refusal_not_a_two_leg_plan(self):
        for proof in (None, {}, {"dexRouterList": []}):
            ok, reason = atomic.route_is_single_call(
                proof, buy_pool=self.POOLS[0], sell_pool=self.POOLS[1],
                from_token="USDG", to_token="USDG")
            self.assertFalse(ok, proof)
            self.assertIn(reason, (atomic.NO_ROUTE,))


class AtomicNetTest(unittest.TestCase):
    def test_the_net_subtracts_gas_and_the_slippage_buffer(self):
        net = atomic.net_usdt(notional_usdt=D("200"), edge_bps=D("40"),
                             gas_usdt=D("0.02"), slippage_bps=D("10"))
        # 200 * 40 bps = 0.80, minus 0.20 slippage, minus 0.02 gas
        self.assertEqual(net, D("0.58"))

    def test_a_missing_term_is_unknown_not_cheaper(self):
        self.assertIsNone(atomic.net_usdt(notional_usdt=None, edge_bps=D("40"),
                                          gas_usdt=D("0.02"), slippage_bps=D("10")))
        self.assertIsNone(atomic.net_usdt(notional_usdt=D("200"), edge_bps=None,
                                          gas_usdt=D("0.02"), slippage_bps=D("10")))
        self.assertIsNone(atomic.net_usdt(notional_usdt=D("200"), edge_bps=D("40"),
                                          gas_usdt=None, slippage_bps=D("10")))

    def test_a_negative_edge_is_never_a_plan(self):
        net = atomic.net_usdt(notional_usdt=D("200"), edge_bps=D("-5"),
                              gas_usdt=D("0"), slippage_bps=D("0"))
        self.assertLess(net, 0)


class AtomicPlanTest(unittest.TestCase):
    def setUp(self):
        self.policy = V2Policy.load()

    def _plan(self, **kwargs):
        base = dict(symbol="SPYx", chain="x-layer", buy_pool="0xaaa",
                    sell_pool="0xbbb", edge_bps=D("40"), notional_usdt=D("200"),
                    gas_usdt=D("0.02"), policy=self.policy,
                    route_proof={"dexRouterList": [{"pool": "0xaaa"},
                                                   {"pool": "0xbbb"}]},
                    from_token="USDG", to_token="USDG")
        base.update(kwargs)
        return atomic.plan(**base)

    def test_a_proven_route_that_clears_the_floor_is_executable(self):
        plan = self._plan()
        self.assertTrue(plan["executable"], plan)
        self.assertTrue(plan["one_transaction"])
        self.assertEqual(plan["family"], "same_chain_atomic")
        self.assertFalse(plan["broadcast"])

    def test_without_a_route_proof_the_plan_is_refused(self):
        plan = self._plan(route_proof=None)
        self.assertFalse(plan["executable"])
        self.assertEqual(plan["blockers"], [atomic.NO_ROUTE])
        self.assertFalse(plan["one_transaction"])

    def test_a_thin_edge_does_not_clear_the_floor(self):
        plan = self._plan(edge_bps=D("11"))     # 0.22 gross - 0.20 slippage - gas
        self.assertFalse(plan["executable"])
        self.assertIn("net_below_floor", plan["blockers"])

    def test_an_unknown_cost_is_unknown(self):
        plan = self._plan(gas_usdt=None)
        self.assertFalse(plan["executable"])
        self.assertEqual(plan["blockers"], ["net_unknown"])


class SameChainScanMarkerTest(unittest.TestCase):
    """The scan must never hand a two-pool same-chain row out as executable."""

    def setUp(self):
        self.policy = V2Policy.load()

    def _pair_rows(self, *, tiers, chain="x-layer"):
        def quote(side, price, pool):
            return Quote(venue="uniswap", chain=chain, symbol="SPCXx",
                         side=side, kind=REAL, price_usd=D(price), recv_ms=1000,
                         pool_id=pool)
        entries = [
            {"symbol": "SPCXx", "chain": chain, "pool_id": "0xA", "kind": "v4",
             "errors": [], "buy": quote(BUY, "100", "0xA"),
             "sell": quote(SELL, "101", "0xA")},
            {"symbol": "SPCXx", "chain": chain, "pool_id": "0xB", "kind": "v4",
             "errors": [], "buy": quote(BUY, "102", "0xB"),
             "sell": quote(SELL, "110", "0xB")},
        ]
        rows = scan.build_candidates(
            symbol="SPCXx", chain_quotes=entries,
            kraken_row={"bid": D("99"), "ask": D("101"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"), deposits={},
            v4_tiers=tiers, now_ms=1000, phase="paper_kraken")
        return [row for row in rows if row["family"] == "dex_to_dex"]

    def test_a_same_chain_two_pool_row_carries_the_atomic_requirement(self):
        def quote(side, price, pool):
            return Quote(venue="uniswap-v3", chain="x-layer", symbol="SPYx",
                         side=side, kind=REAL, price_usd=D(price), recv_ms=1000,
                         pool_id=pool)
        entries = [
            {"symbol": "SPYx", "chain": "x-layer", "pool_id": "0xA", "errors": [],
             "buy": quote(BUY, "100", "0xA"), "sell": quote(SELL, "101", "0xA")},
            {"symbol": "SPYx", "chain": "x-layer", "pool_id": "0xB", "errors": [],
             "buy": quote(BUY, "102", "0xB"), "sell": quote(SELL, "110", "0xB")},
        ]
        rows = scan.build_candidates(
            symbol="SPYx", chain_quotes=entries,
            kraken_row={"bid": D("99"), "ask": D("101"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"), deposits={}, v4_tiers={},
            now_ms=1000, phase="scan")
        pair_rows = [row for row in rows if row["family"] == "dex_to_dex"]
        self.assertTrue(pair_rows)
        for row in pair_rows:
            self.assertTrue(row["atomic_required"])
            self.assertIn("atomic_route_unproven", row["blockers"])
            self.assertFalse(row["executable"])
            self.assertNotEqual(row["buy_pool"], row["sell_pool"])

    def test_both_pools_have_to_pass_the_v4_quality_rule(self):
        """One row, two pools: the tier rule is per pool, not per row.

        2026-10-08: SPCXx/eth listed a green buy pool + an amber hooked sell
        pool and the row still carried only ``atomic_route_unproven`` -- the
        family checked the tier of ``entry``, which is the buy side. A reader
        would then think the atomic proof was the only thing missing.
        """
        hooked = self._pair_rows(tiers={"0xa": "green", "0xb": "amber"})
        self.assertEqual(len(hooked), 1)
        self.assertIn("hook_not_green", hooked[0]["blockers"])
        self.assertIn("atomic_route_unproven", hooked[0]["blockers"])
        # The same row with two plain pools is not blocked on quality. Only the
        # family gate is left, which is the honest reading of this family today.
        plain = self._pair_rows(tiers={"0xa": "green", "0xb": "green"})
        self.assertEqual([name for name in plain[0]["blockers"]
                          if name != "unmeasured_route"],
                         ["atomic_route_unproven"])


class SameChainBoardTableTest(unittest.TestCase):
    """The board's stage-2 table must keep the family gate visible."""

    def _page(self, blockers):
        board = _board()
        snapshot = {"generated_ms": 1_800_000_000_000, "phase": "paper_kraken",
                    "session": "regular", "rows": [
                        {"symbol": "SPCXx", "family": "dex_to_dex", "chain": "eth",
                         "buy_venue": "uniswap", "sell_venue": "uniswap",
                         "buy_price_usd": "164.5374", "sell_price_usd": "164.6540",
                         "edge_t0_bps": "7.08", "gross_usdt": "0.0177",
                         "notional_usdt": "25", "blockers": list(blockers),
                         "reason": blockers[0], "executable": False,
                         "buy_pool": "0x9b8f0f0d", "sell_pool": "0x527e4a83"}],
                    "counters": {}, "universe": {}, "threshold_bps": "20",
                    "min_net_profit_usdt": "0.1"}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / board.SCAN.name).write_text(json.dumps(snapshot))
            data = board.collect(data_dir=Path(tmp), interval_s=180.0,
                                 now_ms=1_800_000_000_000)
            return board.render(data)

    def test_the_family_gate_is_not_hidden_by_a_second_blocker(self):
        page = self._page(["hook_not_green", "atomic_route_unproven"])
        self.assertIn("否：缺原子路线证明、v4 池不是 green", page)

    def test_a_row_with_only_the_family_gate_still_reads_as_the_family_gate(self):
        page = self._page(["atomic_route_unproven"])
        self.assertIn("否：缺原子路线证明", page)
        self.assertNotIn("否：缺原子路线证明、", page)


class RouterCalldataTest(unittest.TestCase):
    """The encoding for "one transaction, two pools". Pure strings, no RPC.

    The numbers this test pins were measured on X Layer on 2026-10-07: a real
    ``eth_call`` of the calldata below succeeded over two real, liquid pools
    (``USDG -100-> WOKB -500-> USDG``), so a self-built router is not required.
    """

    USDG = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"
    WOKB = "0xe538905cf8410324e03a5a23c1c177a474d59b2b"
    ROUTER = "0x4f0c28f5926afda16bf2506d5d9e57ea190f9bca"
    OWNER = "0xabababababababababababababababababababab"

    def test_selectors_match_the_deployed_swap_router_02(self):
        # Independently known Uniswap SwapRouter02 selectors.
        self.assertEqual(atomic.selector(atomic.EXACT_INPUT_SIGNATURE), "0xb858183f")
        self.assertEqual(atomic.selector(atomic.MULTICALL_SIGNATURE), "0xac9650d8")
        self.assertEqual(atomic.selector(atomic.SWEEP_TOKEN_SIGNATURE), "0xdf2ab5bb")
        self.assertEqual(atomic.selector(atomic.EXACT_INPUT_SINGLE_SIGNATURE),
                         "0x04e45aaf")

    def test_the_x_layer_router_is_the_one_verified_on_chain(self):
        self.assertEqual(atomic.SWAP_ROUTER_02["x-layer"],
                         "0x4f0c28f5926afda16bf2506d5d9e57ea190f9bca")
        self.assertEqual(atomic.UNISWAP_V3_FACTORY["x-layer"].lower(),
                         "0x4b2ab38dbf28d31d467aa8993f6c2585981d6804")
        self.assertEqual(atomic.QUOTER_V2["x-layer"].lower(),
                         "0xd1b797d92d87b688193a2b976efc8d577d204343")

    @staticmethod
    def _decode_path(path: str) -> list:
        hops = [("0x" + path[:40], None)]
        rest = path[40:]
        while rest:
            hops.append(("0x" + rest[6:46], int(rest[:6], 16)))
            rest = rest[46:]
        return hops

    def test_the_path_carries_token_fee_token_fee(self):
        path = atomic.encode_path(self.USDG, [(self.WOKB, 100), (self.USDG, 500)])
        self.assertEqual(len(path), 2 * 46 + 40)
        self.assertEqual(self._decode_path(path),
                         [(self.USDG, None), (self.WOKB, 100), (self.USDG, 500)])

    def test_exact_input_is_one_call_over_every_hop(self):
        path = atomic.encode_path(self.USDG, [(self.WOKB, 100), (self.USDG, 500)])
        data = atomic.exact_input_calldata(path, recipient=self.ROUTER,
                                           amount_in=1_000_000, amount_out_min=1778)
        body = data.removeprefix(atomic.selector(atomic.EXACT_INPUT_SIGNATURE))
        words = [body[i:i + 64] for i in range(0, len(body), 64)]
        self.assertEqual(int(words[0], 16), 32)              # tuple offset
        self.assertEqual(int(words[1], 16), 128)             # bytes offset in tuple
        self.assertEqual(words[2][-40:], self.ROUTER[2:])    # recipient = router
        self.assertEqual(int(words[3], 16), 1_000_000)
        self.assertEqual(int(words[4], 16), 1778)            # amountOutMinimum > 0
        self.assertEqual(int(words[5], 16), len(path) // 2)
        # the path is the last thing in the calldata, padded to a 32-byte word
        self.assertIn(path, body)
        self.assertTrue(body.endswith(path.ljust((len(path) + 63) // 64 * 64, "0")))

    def test_multicall_carries_both_calls_in_one_transaction(self):
        one = atomic.exact_input_single_calldata(self.USDG, self.WOKB, 100,
                                                recipient=self.OWNER, amount_in=10)
        two = atomic.sweep_token_calldata(self.USDG, amount_min=1,
                                          recipient=self.OWNER)
        data = atomic.multicall_calldata([one, two])
        body = data.removeprefix(atomic.selector(atomic.MULTICALL_SIGNATURE))
        words = [body[i:i + 64] for i in range(0, len(body), 64)]
        self.assertEqual(int(words[0], 16), 32)     # bytes[] offset
        self.assertEqual(int(words[1], 16), 2)      # two calls, one transaction
        self.assertEqual(int(words[2], 16), 64)     # first element offset
        self.assertIn(one[2:], body)
        self.assertIn(two[2:], body)

    def test_the_ring_is_one_transaction_and_the_sweep_moves_the_output(self):
        ring = atomic.ring_calldata(
            start=self.USDG, hops=[(self.WOKB, 100), (self.USDG, 500)],
            router=self.ROUTER, owner=self.OWNER, amount_in=1_000_000,
            amount_out_min=1778)
        self.assertEqual(ring["to"], self.ROUTER)
        self.assertTrue(ring["one_transaction"])
        self.assertEqual(ring["call_count"], 2)
        self.assertEqual(ring["pools_in_path"], 2)
        self.assertTrue(ring["data"].startswith(atomic.MULTICALL_SELECTOR))
        # both hops, with their own fee, inside one calldata
        self.assertIn(ring["path"], ring["data"])
        self.assertEqual([fee for _token, fee in self._decode_path(ring["path"])[1:]],
                         [100, 500])


if __name__ == "__main__":
    unittest.main()
