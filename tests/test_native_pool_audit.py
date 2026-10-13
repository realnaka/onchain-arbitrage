"""Hermetic tests for the native-vs-wrapper pool audit (no network, no chain)."""
from __future__ import annotations

from decimal import Decimal
import unittest

from arb_monitor.ops.native_pool_audit import (
    BOTH, NATIVE_ONLY, NEITHER, NO_WRAPPER, READ_FAILED, WRAPPER_ONLY,
    audit_row, build_rows, csv_rows, issuer_rows, probe_side, render_table,
    summarize, verdict,
)

FACTORY = "0x" + "aa" * 20
USDG = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"
NATIVE = "0x" + "11" * 20
WRAPPER = "0x" + "22" * 20
NATIVE_POOL = "0x" + "33" * 20
WRAPPER_POOL = "0x" + "44" * 20
EMPTY_POOL = "0x" + "55" * 20

GET_POOL = "0x1698ee82"
SELECTORS = {"token0": "0x0dfe1681", "token1": "0xd21220a7",
             "liquidity": "0x1a686502", "slot0": "0x3850c7bd",
             "balanceOf": "0x70a08231"}


def word(value: int) -> str:
    return "0x" + f"{value:064x}"


def sqrt_price_x96_for(price: str = "1") -> int:
    """slot0 encoding for ``price`` USD per whole base token (base 18dp, quote 6dp)."""
    ratio = Decimal(price) * Decimal(10) ** (6 - 18)
    return int((ratio.sqrt() * (Decimal(2) ** 96)).to_integral_value())


def slot0(sqrt_price_x96: int, tick: int = 0) -> str:
    return "0x" + f"{sqrt_price_x96:064x}" + f"{tick:064x}" + "0" * (64 * 5)


def pool_state(token0: str, token1: str, *, base_raw: int, quote_raw: int,
               liquidity: int = 10 ** 15, price: str = "1") -> dict:
    return {
        "token0": token0.lower(), "token1": token1.lower(), "liquidity": liquidity,
        "sqrt_price_x96": sqrt_price_x96_for(price), "tick": 0,
        "balances": {token0.lower(): base_raw, token1.lower(): quote_raw},
    }


class FakeRPC:
    """Answers exactly the read-only calls the audit makes, and records them."""

    def __init__(self, pools: dict | None = None, states: dict | None = None):
        self.pools = {tuple(k): v for k, v in (pools or {}).items()}
        self.states = {k.lower(): v for k, v in (states or {}).items()}
        self.methods: list[str] = []

    def single(self, method: str, params: list):
        self.methods.append(method)
        if method == "eth_blockNumber":
            return hex(1000)
        if method == "eth_getBlockByNumber":
            return {"number": params[0], "timestamp": hex(1_700_000_000)}
        if method == "eth_call":
            return self._call(params[0]["to"], params[0]["data"])
        raise RuntimeError(f"unexpected method {method}")

    def batch(self, calls: list) -> list:
        return [self.single(method, params) for method, params in calls]

    def block_number(self) -> int:
        return int(self.single("eth_blockNumber", []), 16)

    def block(self, number: int) -> dict:
        return self.single("eth_getBlockByNumber", [hex(number), False])

    def call(self, to: str, data: str, block: str = "latest") -> str:
        return self.single("eth_call", [{"to": to, "data": data}, block])

    def _call(self, to: str, data: str) -> str:
        to = to.lower()
        if to == FACTORY and data.startswith(GET_POOL):
            base = "0x" + data[10 + 24:10 + 64]
            stable = "0x" + data[74 + 24:74 + 64]
            fee = int(data[138:202], 16)
            pool = self.pools.get((base.lower(), stable.lower(), fee))
            return word(int(pool, 16)) if pool else word(0)
        if data.startswith(SELECTORS["balanceOf"]):
            # balanceOf(token, pool): answer for the pool named in the call data.
            owner = "0x" + data[10 + 24:10 + 64]
            state = self.states.get(owner)
            if state is None or to not in state["balances"]:
                return word(0)
            return word(state["balances"][to])
        state = self.states.get(to)
        if state is None:
            raise RuntimeError("execution reverted")
        selector = data[:10]
        if selector == SELECTORS["token0"]:
            return word(int(state["token0"], 16))
        if selector == SELECTORS["token1"]:
            return word(int(state["token1"], 16))
        if selector == SELECTORS["liquidity"]:
            return word(state["liquidity"])
        if selector == SELECTORS["slot0"]:
            return slot0(state["sqrt_price_x96"], state["tick"])
        raise RuntimeError(f"unexpected selector {selector}")


def rpc_with(*, native_depth: str | None, wrapper_depth: str | None) -> FakeRPC:
    """Native and wrapper each get one USDG/500 pool, or none."""
    pools, states = {}, {}
    if native_depth is not None:
        pools[(NATIVE, USDG, 500)] = NATIVE_POOL
        base_raw = int(Decimal(native_depth) * 10 ** 18)
        states[NATIVE_POOL] = pool_state(NATIVE, USDG, base_raw=base_raw, quote_raw=0)
    if wrapper_depth is not None:
        pools[(WRAPPER, USDG, 500)] = WRAPPER_POOL
        base_raw = int(Decimal(wrapper_depth) * 10 ** 18)
        states[WRAPPER_POOL] = pool_state(WRAPPER, USDG, base_raw=base_raw, quote_raw=0)
    return FakeRPC(pools, states)


ASSETS = [{
    "symbol": "MRNAx", "name": "Moderna xStock", "hk_code": "MRNA", "market": "US",
    "native_address": NATIVE, "wrapper_address": WRAPPER,
}]


class IssuerRowsTests(unittest.TestCase):
    def test_keeps_stock_name_and_both_addresses(self):
        rows = issuer_rows(ASSETS)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["name"], "Moderna")
        self.assertEqual(row["underlying"], "MRNA")
        self.assertEqual(row["market"], "US")
        self.assertEqual(row["native"], NATIVE)
        self.assertEqual(row["wrapper"], WRAPPER)

    def test_assets_without_any_xlayer_address_are_skipped(self):
        self.assertEqual(issuer_rows([{"symbol": "FOOx", "name": "Foo xStock"}]), [])


class ProbeSideTests(unittest.TestCase):
    def test_measures_depth_for_the_address_it_is_given(self):
        rpc = rpc_with(native_depth=None, wrapper_depth="1000")
        probe = probe_side(rpc, factory=FACTORY, base_address=WRAPPER)
        self.assertEqual(probe["pools_found"], 1)
        self.assertEqual(probe["pools_with_depth"], 1)
        self.assertEqual(probe["best_pool"], WRAPPER_POOL)
        # The fake encodes the price through sqrtPriceX96, so the measured depth
        # carries that encoding's rounding; the assertion is on the magnitude.
        self.assertAlmostEqual(float(probe["best_depth_usd"]), 1000.0, places=6)

    def test_empty_pool_is_reported_as_a_pool_without_depth(self):
        pools = {(NATIVE, USDG, 500): EMPTY_POOL}
        states = {EMPTY_POOL: pool_state(NATIVE, USDG, base_raw=0, quote_raw=0,
                                        liquidity=0)}
        probe = probe_side(FakeRPC(pools, states), factory=FACTORY, base_address=NATIVE)
        self.assertEqual(probe["pools_found"], 1)
        self.assertEqual(probe["pools_with_depth"], 0)
        self.assertEqual(Decimal(probe["pools"][0]["depth_usd"]), 0)
        # An empty pool is not a market: no tradable depth is reported.
        self.assertIsNone(probe["best_depth_usd"])
        self.assertEqual(probe["pools_not_in_range"], 1)

    def test_no_pool_at_all_is_not_the_same_as_an_empty_pool(self):
        probe = probe_side(FakeRPC(), factory=FACTORY, base_address=NATIVE)
        self.assertEqual(probe["pools_found"], 0)
        self.assertEqual(probe["pools"], [])

    def test_only_read_methods_are_ever_called(self):
        rpc = rpc_with(native_depth="100", wrapper_depth="100")
        probe_side(rpc, factory=FACTORY, base_address=NATIVE)
        self.assertTrue(set(rpc.methods) <= {"eth_call", "eth_blockNumber",
                                             "eth_getBlockByNumber"})

    def test_balances_without_in_range_liquidity_are_not_depth(self):
        """CRWDx, measured 2026-09-24: 344.7 tokens sitting in a pool whose
        ``liquidity()`` is 0.  Every QuoterV2 call reverted; the balances are
        out-of-range positions, not a market."""
        pools = {(NATIVE, USDG, 500): NATIVE_POOL}
        states = {NATIVE_POOL: pool_state(NATIVE, USDG, base_raw=344 * 10 ** 18,
                                          quote_raw=1, liquidity=0)}
        rpc = FakeRPC(pools, states)
        probe = probe_side(rpc, factory=FACTORY, base_address=NATIVE)
        self.assertIsNone(probe["best_depth_usd"])
        self.assertEqual(probe["pools_not_in_range"], 1)
        self.assertAlmostEqual(float(probe["untradable_best_depth_usd"]), 344.0, places=4)

        rows = build_rows(issuer_rows(ASSETS),
                          lambda row, side: probe_side(rpc, factory=FACTORY,
                                                       base_address=row[side]),
                          Decimal("50"))
        self.assertEqual(rows[0]["verdict"], NEITHER)
        self.assertIn("池不可成交", render_table(rows, Decimal("50")))


class VerdictTests(unittest.TestCase):
    def test_native_only_is_the_shape_that_invalidates_the_old_assumption(self):
        rpc = rpc_with(native_depth="1000", wrapper_depth=None)
        rows = build_rows(issuer_rows(ASSETS),
                          lambda row, side: probe_side(rpc, factory=FACTORY,
                                                       base_address=row[side]),
                          Decimal("50"))
        self.assertEqual(rows[0]["verdict"], NATIVE_ONLY)
        self.assertEqual(rows[0]["native_pools_found"], 1)
        self.assertEqual(rows[0]["wrapper_pools_found"], 0)

    def test_wrapper_only_is_the_current_documented_shape(self):
        rpc = rpc_with(native_depth=None, wrapper_depth="1000")
        rows = build_rows(issuer_rows(ASSETS),
                          lambda row, side: probe_side(rpc, factory=FACTORY,
                                                       base_address=row[side]),
                          Decimal("50"))
        self.assertEqual(rows[0]["verdict"], WRAPPER_ONLY)

    def test_both_sides_deep_and_the_threshold_boundary(self):
        self.assertEqual(verdict(native_depth="50", wrapper_depth="50",
                                 has_wrapper=True, min_depth_usd=Decimal("50")), BOTH)
        self.assertEqual(verdict(native_depth="49.99", wrapper_depth="50",
                                 has_wrapper=True, min_depth_usd=Decimal("50")),
                         WRAPPER_ONLY)

    def test_missing_wrapper_is_its_own_verdict(self):
        self.assertEqual(verdict(native_depth="1000", wrapper_depth=None,
                                 has_wrapper=False, min_depth_usd=Decimal("50")),
                         NO_WRAPPER)

    def test_neither_side_has_depth(self):
        self.assertEqual(verdict(native_depth="10", wrapper_depth=None,
                                 has_wrapper=True, min_depth_usd=Decimal("50")),
                         NEITHER)


class OutputTests(unittest.TestCase):
    def test_table_lists_name_native_and_wrapper(self):
        rpc = rpc_with(native_depth=None, wrapper_depth="1000")
        rows = build_rows(issuer_rows(ASSETS),
                          lambda row, side: probe_side(rpc, factory=FACTORY,
                                                       base_address=row[side]),
                          Decimal("50"))
        table = render_table(rows, Decimal("50"))
        self.assertIn("Moderna", table)
        self.assertIn(NATIVE, table)
        self.assertIn(WRAPPER, table)
        self.assertIn("无池", table)
        # The price of the deep side comes from that pool's own slot0.
        self.assertIn("1.0000", table)

    def test_csv_rows_carry_both_addresses_prices_and_verdict(self):
        rpc = rpc_with(native_depth=None, wrapper_depth="1000")
        rows = build_rows(issuer_rows(ASSETS),
                          lambda row, side: probe_side(rpc, factory=FACTORY,
                                                       base_address=row[side]),
                          Decimal("50"))
        flat = csv_rows(rows)[0]
        self.assertEqual(flat["name"], "Moderna")
        self.assertEqual(flat["native"], NATIVE)
        self.assertEqual(flat["wrapper"], WRAPPER)
        self.assertEqual(flat["wrapper_pool"], WRAPPER_POOL)
        self.assertEqual(flat["native_price_usd"], "")
        self.assertAlmostEqual(float(flat["wrapper_price_usd"]), 1.0, places=6)
        self.assertEqual(flat["verdict"], WRAPPER_ONLY)

    def test_summary_counts_every_verdict_once(self):
        rows = [
            audit_row({"symbol": "A", "wrapper": WRAPPER}, {}, Decimal("50")),
            audit_row({"symbol": "B", "wrapper": None}, {}, Decimal("50")),
        ]
        summary = summarize(rows)
        self.assertEqual(summary["assets"], 2)
        self.assertEqual(summary["counts"][NEITHER], 1)
        self.assertEqual(summary["counts"][NO_WRAPPER], 1)
        self.assertEqual(summary["lists"][NO_WRAPPER], ["B"])

    def test_a_failing_probe_does_not_drop_the_stock(self):
        def probe_for(row, side):
            if side == "native":
                raise RuntimeError("rpc down")
            return {"address": row["wrapper"], "pools_found": 0, "pools_with_depth": 0,
                    "best_depth_usd": None, "pools": [], "errors": []}

        rows = build_rows(issuer_rows(ASSETS), probe_for, Decimal("50"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], NEITHER)
        self.assertTrue(rows[0]["native_errors"][0]["error"].startswith("RuntimeError"))

    def test_a_dead_rpc_is_not_reported_as_no_depth(self):
        def probe_for(row, side):
            raise RuntimeError("rpc down")

        rows = build_rows(issuer_rows(ASSETS), probe_for, Decimal("50"))
        self.assertEqual(rows[0]["verdict"], READ_FAILED)
        self.assertIn("读取失败", render_table(rows, Decimal("50")))


if __name__ == "__main__":
    unittest.main()
