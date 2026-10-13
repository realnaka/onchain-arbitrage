"""Hermetic checks for the multi-chain census gates. No network."""
from __future__ import annotations

import importlib.util
import json
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "xstocks_chain_census.py"
    spec = importlib.util.spec_from_file_location("xstocks_chain_census", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pool(**over):
    pool = {
        "pool_address": "0xpool",
        "dex": "pancakeswap-v3-bsc",
        "labels": ["v3"],
        "pool_name": "mstrx / USDT 0.25%",
        "base_address": "0xtoken",
        "base_symbol": "mstrx",
        "quote_address": "0x55d398326f99059ff775485246999027b3197955",
        "quote_symbol": "USDT",
        "depth_usd": Decimal("398.26"),
        "volume_h24": Decimal("4.39"),
        "fee_hint": None,
        "source": "geckoterminal",
    }
    pool.update(over)
    return pool


class GateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_a_deep_stable_pool_is_a_candidate(self):
        picked = self.mod.pick_candidates([_pool()], "0xToken", "bsc", Decimal("100"))
        self.assertEqual(len(picked["candidates"]), 1)

    def test_thin_pool_is_rejected_with_the_measured_depth(self):
        picked = self.mod.pick_candidates(
            [_pool(depth_usd=Decimal("9.5"))], "0xToken", "bsc", Decimal("100"))
        self.assertEqual(picked["candidates"], [])
        self.assertIn("9.50", picked["rejected"][0]["why"])

    def test_v4_pool_is_a_trap_not_a_candidate(self):
        picked = self.mod.pick_candidates(
            [_pool(dex="uniswap-v4-bsc", labels=["v4"], depth_usd=Decimal("5000"))],
            "0xToken", "bsc", Decimal("100"))
        self.assertEqual(picked["candidates"], [])
        self.assertEqual(len(picked["traps"]), 1)

    def test_a_88_percent_fee_pool_is_a_trap(self):
        pool = _pool(pool_name="mstrx / USDC 88%", fee_hint=Decimal("0.88"))
        self.assertTrue(self.mod.is_trap(pool))
        self.assertEqual(self.mod.pick_candidates([pool], "0xToken", "bsc")["candidates"], [])

    def test_unknown_quote_token_is_not_a_candidate(self):
        picked = self.mod.pick_candidates(
            [_pool(quote_symbol="WIF", quote_address="0xwif")], "0xToken", "bsc")
        self.assertEqual(picked["candidates"], [])
        self.assertIn("不是已知稳定币", picked["rejected"][0]["why"])

    def test_the_token_must_be_one_side_of_the_pool(self):
        picked = self.mod.pick_candidates([_pool()], "0xOther", "bsc")
        self.assertEqual(picked["candidates"], [])
        self.assertIn("两侧都不是这个代币", picked["rejected"][0]["why"])

    def test_the_deepest_measured_pool_comes_first(self):
        deep = _pool(pool_address="0xdeep", depth_usd=Decimal("900"))
        shallow = _pool(pool_address="0xshallow", depth_usd=Decimal("150"))
        picked = self.mod.pick_candidates([shallow, deep], "0xToken", "bsc", Decimal("100"))
        self.assertEqual([row["pool_address"] for row in picked["candidates"]],
                         ["0xdeep", "0xshallow"])


class NormalizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_dexscreener_pair_is_normalized(self):
        pool = self.mod.normalize_dexscreener({
            "chainId": "monad",
            "dexId": "uniswap",
            "pairAddress": "0xAB",
            "labels": ["v3"],
            "baseToken": {"address": "0xTOKEN", "symbol": "AAPLx"},
            "quoteToken": {"address": "0xUSDC", "symbol": "USDC"},
            "liquidity": {"usd": 1234.5},
            "volume": {"h24": 10.0},
        })
        self.assertEqual(pool["pool_address"], "0xab")
        self.assertEqual(pool["base_address"], "0xtoken")
        self.assertEqual(pool["depth_usd"], Decimal("1234.5"))
        self.assertEqual(pool["source"], "dexscreener")

    def test_gecko_pool_name_carries_the_fee(self):
        self.assertEqual(self.mod.fee_from_name("wSLVx / USDC 0.05%"), Decimal("0.0005"))
        self.assertIsNone(self.mod.fee_from_name("mstrx / USDT"))

    def test_stable_symbols_cover_the_issuer_stables(self):
        for symbol in ("USDC", "USDG", "USD\u20ae", "USDT0"):
            self.assertTrue(self.mod.is_stable_symbol(symbol), symbol)
        self.assertFalse(self.mod.is_stable_symbol("WIF"))


class BscFactorySweepTest(unittest.TestCase):
    """2026-10-09: BSC had **no factory**, so the on-chain sweep never ran there.

    The census reported 3 BSC pools because they came from DexScreener's index.
    Asking Pancake v3's factory directly (the same call that found Ink's 7 deep
    pools) returns 8 for the 20 busiest symbols -- and one of them is dust:
    0.00066 SPCXx + 0.09 USDT with ``liquidity() == 0``, which the old
    ``pool_depth_usd`` valued at **$1,120,621,080**.
    """

    def setUp(self):
        self.mod = _load()

    def test_bsc_knows_its_factory_and_fee_tiers(self):
        bsc = self.mod.CHAINS["bsc"]
        self.assertEqual(bsc.get("factory"),
                         "0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865")
        self.assertEqual(bsc.get("pool_source"), "xchange")
        self.assertEqual(self.mod.CHAINS["ink"].get("pool_source"), "xchange")
        # 2500 (0.25%) is the tier every BSC xStock pool actually uses.
        self.assertIn(2500, bsc.get("probe_fees") or [])

    def test_a_zero_liquidity_pool_is_zero_depth_not_a_price_artifact(self):
        mod = self.mod
        seen: list[str] = []

        def fake_rpc(chain_key, to, data):
            seen.append(data[:10])
            if data.startswith(mod.SELECTOR_LIQUIDITY):
                return "0x" + "0" * 64          # zero active liquidity
            raise AssertionError(f"read past liquidity: {data[:10]}")

        original = mod.rpc
        mod.rpc = fake_rpc
        try:
            depth = mod.pool_depth_usd("bsc", "0xpool", "0xtoken", "0xstable", 18, 18)
        finally:
            mod.rpc = original
        self.assertEqual(depth, Decimal(0))
        self.assertEqual(seen, [mod.SELECTOR_LIQUIDITY])

    def test_rpc_batch_maps_results_by_id_not_by_position(self):
        mod = self.mod

        class _Response:
            def __init__(self, body):
                self._body = body

            def read(self):
                return self._body

        class _Opener:
            def __init__(self, body):
                self._body = body

            def open(self, request, timeout=None):
                return _Response(self._body)

        body = json.dumps([
            {"jsonrpc": "2.0", "id": 2, "result": "0xcc"},
            {"jsonrpc": "2.0", "id": 0, "result": "0xaa"},
            {"jsonrpc": "2.0", "id": 1, "error": {"code": 3}},
        ]).encode()
        with mock.patch.object(mod.urllib.request, "build_opener",
                               lambda *a, **k: _Opener(body)):
            out = mod.rpc_batch("bsc", [("0xto", "0x01"), ("0xto", "0x02"),
                                        ("0xto", "0x03")])
        self.assertEqual(out, ["0xaa", None, "0xcc"])

    def test_the_factory_sweep_asks_in_batches(self):
        """A one-call-per-pool sweep is ~20k round trips on BSC alone."""
        source = (Path(__file__).resolve().parents[1]
                  / "scripts" / "xstocks_chain_census.py").read_text()
        self.assertIn("rpc_batch(chain_key, [", source)
        self.assertNotIn("result = rpc(chain_key, factory", source)


class ChainMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_monad_is_a_known_chain_with_no_gecko_slug(self):
        chain = self.mod.CHAINS["monad"]
        self.assertIsNone(chain["gecko"])
        self.assertEqual(chain["dexscreener"], "monad")
        self.assertTrue(chain["default_quoter"].startswith("0x"))

    def test_pancake_pools_use_the_pancake_quoter(self):
        chain = self.mod.CHAINS["bsc"]
        self.assertEqual(self.mod.quoter_for("pancakeswap-v3-bsc", chain),
                         chain["quoters"]["pancake"])
        self.assertEqual(self.mod.quoter_for("uniswap-v3", chain),
                         chain["default_quoter"])

    def test_dexscreener_chain_ids_map_back_to_chain_keys(self):
        self.assertEqual(self.mod.DS_CHAIN_KEYS["monad"], "monad")
        self.assertEqual(self.mod.DS_CHAIN_KEYS["bsc"], "bsc")
        self.assertEqual(self.mod.DS_CHAIN_KEYS["ethereum"], "eth")

    def test_ton_is_a_known_chain_without_an_evm_quoter(self):
        chain = self.mod.CHAINS["ton"]
        self.assertEqual(chain["kind"], "ton")
        self.assertIsNone(chain["default_quoter"])
        self.assertIsNone(chain["rpc"])


class WatchlistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def _document(self):
        return {
            "scanned_at_ms": 1_790_000_000_000,
            "with_depth": [
                {"asset": "AAPL", "symbol": "wAAPLx",
                 "pool": "0xC44BD9C8589026D28D1632D7B86B2EFB6CDC8FD2",
                 "base_address": "0x943BF64D566C32A2BCD41AC92FB63C111CC9DE8F",
                 "quote_symbol": "USDG", "depth_usd": "412063.77"},
                {"asset": "ZZZ", "symbol": "wZZZx", "pool": "not-an-address",
                 "base_address": "0xabc", "quote_symbol": "USDG", "depth_usd": "5"},
            ],
        }

    def test_wrapper_pools_come_from_the_repo_measurement(self):
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "depth_watchlist.json"
            path.write_text(_json.dumps(self._document()))
            pools, stamp = self.mod.xlayer_watchlist_pools(path)
        self.assertEqual(len(pools), 1)
        pool = pools[0]
        self.assertEqual(pool["pool_address"], "0xc44bd9c8589026d28d1632d7b86b2efb6cdc8fd2")
        self.assertEqual(pool["base_address"], "0x943bf64d566c32a2bcd41ac92fb63c111cc9de8f")
        self.assertEqual(pool["quote_address"],
                         self.mod.CHAINS["x-layer"]["stables"]["USDG"])
        self.assertEqual(pool["source"], "xlayer_depth_watchlist")
        self.assertTrue(stamp)

    def test_a_missing_watchlist_is_not_an_error(self):
        pools, stamp = self.mod.xlayer_watchlist_pools(Path("/nonexistent/watchlist.json"))
        self.assertEqual(pools, [])
        self.assertEqual(stamp, "")


class MergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_chains_this_run_skipped_are_kept(self):
        import json as _json
        import tempfile
        old = {
            "chains": {"monad": {"pools_seen": 0}, "ton": {"pools_seen": 3}},
            "candidates": [{"slug": "ton", "symbol": "SPYx"},
                           {"slug": "monad", "symbol": "AAPLx"}],
            "traps": [{"chain": "ton"}], "rejected": [], "unresolved": [],
        }
        fresh = {
            "generated_at": "now", "chains": {"x-layer": {"pools_seen": 39}},
            "candidates": [{"slug": "x-layer", "symbol": "wSPYx"}],
            "traps": [], "rejected": [], "unresolved": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "census.json"
            path.write_text(_json.dumps(old))
            merged = self.mod.merge_payload(fresh, path, ["x-layer"])
        self.assertEqual(set(merged["chains"]), {"monad", "ton", "x-layer"})
        self.assertEqual([row["symbol"] for row in merged["candidates"]],
                         ["SPYx", "AAPLx", "wSPYx"])

    def test_a_rerun_of_the_same_chain_replaces_its_rows(self):
        import json as _json
        import tempfile
        old = {"chains": {"ton": {"pools_seen": 1}},
               "candidates": [{"slug": "ton", "symbol": "OLD"}],
               "traps": [], "rejected": [], "unresolved": []}
        fresh = {"generated_at": "now", "chains": {"ton": {"pools_seen": 2}},
                 "candidates": [{"slug": "ton", "symbol": "NEW"}],
                 "traps": [], "rejected": [], "unresolved": []}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "census.json"
            path.write_text(_json.dumps(old))
            merged = self.mod.merge_payload(fresh, path, ["ton"])
        self.assertEqual([row["symbol"] for row in merged["candidates"]], ["NEW"])
        self.assertEqual(merged["chains"]["ton"]["pools_seen"], 2)


if __name__ == "__main__":
    unittest.main()
