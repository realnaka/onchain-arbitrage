"""Hermetic checks for the live xStocks gap rule. No network."""
from __future__ import annotations

import importlib.util
import json
import unittest
from decimal import Decimal
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "xstocks_gap_live.py"
    spec = importlib.util.spec_from_file_location("xstocks_gap_live", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ClassifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_keeps_a_gap_wider_than_10_bps(self):
        gap = self.mod.classify(Decimal("99"), Decimal("100"))
        self.assertEqual(gap, Decimal("-100"))

    def test_drops_noise_inside_10_bps(self):
        self.assertIsNone(self.mod.classify(Decimal("99.95"), Decimal("100")))

    def test_drops_a_quote_far_from_the_reference(self):
        self.assertIsNone(self.mod.classify(Decimal("1"), Decimal("100")))

    def test_class_share_uses_a_hyphen(self):
        self.assertEqual(self.mod.yahoo_symbol("BRK.B"), "BRK-B")
        self.assertEqual(self.mod.yahoo_symbol("AAPL"), "AAPL")

    def test_free_fmp_price_stays_fresh_for_hours(self):
        now = 1_000_000.0
        self.assertTrue(self.mod.price_is_fresh(now - 3600, "FMP 正股", now, True))
        self.assertFalse(self.mod.price_is_fresh(now - 3600, "雅虎正股", now, True))

    def test_fmp_batch_maps_class_share_back(self):
        prices = self.mod.parse_fmp_batch(
            [{"symbol": "BRK-B", "price": 492.5}, {"symbol": "AAPL", "price": 0},
             {"Error Message": "bad"}],
            {"BRK-B": "BRK.B", "AAPL": "AAPL"},
        )
        self.assertEqual(prices, {"BRK.B": Decimal("492.5")})


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def clock(self):
        return self.now

    def wall(self):
        return 1_790_000_000.0 + self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class PacerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def _pacer(self, rps=2.0):
        fake = FakeClock()
        pacer = self.mod.Pacer(rps, headroom=1.0, clock=fake.clock, wall=fake.wall, sleep=fake.sleep)
        return pacer, fake

    def test_spaces_requests_to_the_budget(self):
        pacer, fake = self._pacer(2.0)
        for _ in range(5):
            pacer.wait()
        self.assertEqual(len(fake.slept), 4)
        self.assertTrue(all(abs(s - 0.5) < 1e-9 for s in fake.slept))

    def test_429_without_headers_pauses_a_full_window_and_halves_once(self):
        pacer, fake = self._pacer(2.0)
        pacer.wait()
        for _ in range(4):
            pacer.observe(429, {})
        self.assertEqual(pacer.rate, 1.0)
        self.assertEqual(pacer.limited, 4)
        pacer.wait()
        self.assertGreaterEqual(sum(fake.slept), self.mod.JUP_PAUSE_S)

    def test_429_waits_until_the_reset_header(self):
        pacer, fake = self._pacer(2.0)
        pacer.observe(429, {"x-ratelimit-reset": str(fake.wall() + 7)})
        pacer.wait()
        self.assertAlmostEqual(sum(fake.slept), 7.0)

    def test_empty_window_pauses_before_the_next_429(self):
        pacer, fake = self._pacer(2.0)
        pacer.observe(200, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(fake.wall() + 4)})
        pacer.wait()
        self.assertAlmostEqual(sum(fake.slept), 4.0)
        self.assertEqual(pacer.limited, 0)

    def test_rate_recovers_but_never_passes_the_ceiling(self):
        pacer, _fake = self._pacer(2.0)
        pacer.observe(429, {})
        for _ in range(1000):
            pacer.observe(200, {})
        self.assertEqual(pacer.rate, 2.0)


class LadderTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.row = {"symbol": "AAPLx", "ticker": "AAPL", "address": "mint", "form": "原生", "kraken": "可吃单"}

    def test_first_quote_rate_limited_is_not_a_failure(self):
        def limited(_mint, _raw):
            raise self.mod.RateLimited()
        self.mod.jupiter = limited
        status, row = self.mod.quote_solana(self.row, Decimal("100"), Decimal(1), "FMP 正股",
                                            Decimal("100"))
        self.assertEqual((status, row), ("limited", None))

    def test_rate_limit_mid_ladder_keeps_the_cheap_size_and_marks_next_unknown(self):
        calls = []

        def ladder(_mint, raw):
            calls.append(raw)
            if len(calls) > 2:
                raise self.mod.RateLimited()
            spent = Decimal(raw) / Decimal(10) ** 6
            got = spent / Decimal("99") * Decimal(10) ** 8
            return spent, Decimal(int(got)) / Decimal(10) ** 8, "Pool", Decimal("0.5")
        self.mod.jupiter = ladder
        status, row = self.mod.quote_solana(self.row, Decimal("100"), Decimal(1), "FMP 正股",
                                            Decimal("100"))
        self.assertEqual(status, "hit")
        self.assertEqual(row["spend_usd"], "5")
        self.assertEqual(row["breaks_usd"], "20")
        self.assertTrue(row["breaks_unknown"])

    def test_no_route_on_first_size_is_not_a_failure(self):
        def none(_mint, _raw):
            raise self.mod.NoRoute("http_400:NO_ROUTES_FOUND")
        self.mod.jupiter = none
        status, row = self.mod.quote_solana(self.row, Decimal("100"), Decimal(1), "FMP 正股",
                                            Decimal("100"))
        self.assertEqual((status, row), ("no_route", None))

    def test_no_route_on_a_larger_size_only_ends_the_ladder(self):
        calls = []

        def ladder(_mint, raw):
            calls.append(raw)
            if len(calls) > 1:
                raise self.mod.NoRoute("http_400:NO_ROUTES_FOUND")
            spent = Decimal(raw) / Decimal(10) ** 6
            return spent, spent / Decimal("99"), "Pool", Decimal("0.5")
        self.mod.jupiter = ladder
        status, row = self.mod.quote_solana(self.row, Decimal("100"), Decimal(1), "FMP 正股",
                                            Decimal("100"))
        self.assertEqual(status, "hit")
        self.assertEqual((row["spend_usd"], row["breaks_usd"], row["breaks_unknown"]), ("1", "5", False))

    def test_key_switches_to_the_documented_gateway(self):
        import os
        old = dict(os.environ)
        self.mod.JUP_ENV = Path("/nonexistent/jupiter.env")
        try:
            os.environ["JUPITER_API_KEY"] = "k"
            os.environ.pop("JUPITER_RPS", None)
            base, key, rps = self.mod.jupiter_config()
            self.assertEqual((base, key, rps), ("https://api.jup.ag", "k", self.mod.JUP_KEY_RPS))
            os.environ["JUPITER_RPS"] = "10"
            self.assertEqual(self.mod.jupiter_config()[2], 10.0)
        finally:
            os.environ.clear()
            os.environ.update(old)


class SellSideCostTests(unittest.TestCase):
    """净利只算买入这一侧，但卖出价用 Kraken 真实买盘（operator 2026-10-06）。"""

    def setUp(self):
        self.mod = _load()
        self.mod._sol_usd = Decimal("119.5")

    def test_net_subtracts_the_kraken_fee_and_the_chain_fees(self):
        size, tokens, sell = Decimal("20"), Decimal("0.14"), Decimal("184.93")
        gross, net = self.mod.edge_usdt(size, tokens, sell, symbol="TQQQx")
        proceeds = tokens * sell
        self.assertEqual(gross, proceeds - size)
        fees = (proceeds * Decimal("0.001")          # Kraken 10 bps = 0.1%
                + (self.mod.BUY_FEE_SOL + self.mod.TRANSFER_FEE_FIRST_SOL)
                * Decimal("119.5"))                 # 没有 seen 记录 → 按首次算
        self.assertEqual(net, gross - fees)

    def test_a_name_already_transferred_pays_the_repeat_fee(self):
        """`data/xstocks_transfer_seen.json` 里有的标的，转账费只用基础费。"""
        self.mod._transfer_seen_cache.update({"at": 9e9, "seen": {"TQQQx": "2026-10-06"}})
        self.assertEqual(self.mod.transfer_fee_sol("TQQQx"),
                         self.mod.TRANSFER_FEE_REPEAT_SOL)
        self.assertEqual(self.mod.transfer_fee_sol("NEWx"),
                         self.mod.TRANSFER_FEE_FIRST_SOL)

    def test_first_transfer_costs_more_than_the_repeat(self):
        self.assertGreater(self.mod.TRANSFER_FEE_FIRST_SOL,
                           self.mod.TRANSFER_FEE_REPEAT_SOL * 100)

    def test_selling_lower_on_kraken_lowers_the_net(self):
        """同一笔买入，Kraken 卖价低 1%，净利跟着掉——不再拿正股价顶替。"""
        size, tokens = Decimal("20"), Decimal("0.14")
        _g1, dear = self.mod.edge_usdt(size, tokens, Decimal("184.93"))
        _g2, cheap = self.mod.edge_usdt(size, tokens, Decimal("183.08"))
        self.assertGreater(dear - cheap, Decimal("0.2"))

    def test_anchor_is_the_kraken_bid_minus_our_fee(self):
        self.assertEqual(self.mod.sell_anchor(Decimal("100")), Decimal("99.9"))

    def test_gap_is_negative_when_the_buy_side_is_cheaper(self):
        self.assertEqual(self.mod.gap_vs(Decimal("99"), Decimal("100")), Decimal("-100"))

    def test_no_round_trip_term(self):
        """同一口池买回再卖出这件事不应出现在成本里。"""
        self.assertFalse(hasattr(self.mod, "jupiter_reverse"))
        self.assertFalse(hasattr(self.mod, "NETWORK_FEE_SOL"))

    def test_risk_follows_the_buy_side_cost(self):
        self.assertEqual(self.mod.risk_label(Decimal("0.5")), "低")
        self.assertEqual(self.mod.risk_label(Decimal("3")), "中")
        self.assertEqual(self.mod.risk_label(Decimal("11")), "高")
        self.assertEqual(self.mod.risk_label(None), "—")


class KrakenAnchorTests(unittest.TestCase):
    """机会判断的卖出锚 = Kraken 买盘（operator 2026-10-06）。不碰网络。"""

    def setUp(self):
        self.mod = _load()
        self.mod._sol_usd = Decimal("119.5")
        self.row = {"symbol": "AAPLx", "ticker": "AAPL", "address": "mint",
                    "form": "原生", "kraken": "可吃单"}

    def test_bulk_ticker_returns_the_best_bid_per_symbol(self):
        payload = {"error": [], "result": {
            "AAPLx/USD": {"b": ["334.35", "8", "8.000"]},
            "TQQQx/USD": {"b": ["82.41", "1", "1.000"]},
            "BADx/USD": {"b": []},
        }}
        calls = []

        def fake_get(url, timeout=15):
            calls.append(url)
            return 200, json.dumps(payload).encode()

        self.mod._get = fake_get
        self.mod._ticker_cache.update({"at": 0.0, "bids": {}})
        bids = self.mod.kraken_best_bids(["AAPLx", "TQQQx"])
        self.assertEqual(bids, {"AAPLx": Decimal("334.35"), "TQQQx": Decimal("82.41")})
        self.assertEqual(len(calls), 1)
        # 再问一次走缓存，不再发请求。
        self.mod.kraken_best_bids(["AAPLx"])
        self.assertEqual(len(calls), 1)

    def test_failed_ticker_keeps_the_previous_bids(self):
        self.mod._get = lambda url, timeout=15: (500, b"boom")
        self.mod._ticker_cache.update({"at": 0.0, "bids": {"AAPLx": Decimal("334.35")}})
        self.assertEqual(self.mod.kraken_best_bids(["AAPLx"])["AAPLx"], Decimal("334.35"))

    def test_sell_side_walks_the_kraken_book(self):
        hit = {"share": "100", "spend_usd": "20"}
        self.mod.apply_sell_side(hit, [["99", "0.1", 0], ["98", "1", 0]],
                                 size=Decimal("20"), share=Decimal("100"))
        # 0.2 枚：0.1 @99 + 0.1 @98 → 均价 98.5
        self.assertEqual(hit["sell_vwap"], "98.5000")
        expected_gross = Decimal("0.2") * Decimal("98.5000") - Decimal("20")
        self.assertEqual(hit["gross_usdt"], self.mod._money(expected_gross))
        self.assertFalse(hit["net_positive"])

    def test_book_too_thin_leaves_the_net_blank(self):
        hit = {"share": "100", "spend_usd": "20"}
        self.mod.apply_sell_side(hit, [["99", "0.05", 0]],
                                 size=Decimal("20"), share=Decimal("100"))
        self.assertIsNone(hit["net_usdt"])
        self.assertTrue(hit["sell_short"])

    def _quote(self, share: Decimal):
        def ladder(_mint, _raw):
            spent = Decimal(1)
            got = spent / share
            return spent, got, "Pool", Decimal("0.5")
        self.mod.jupiter = ladder
        return self.mod.quote_solana(self.row, Decimal("200"), Decimal(1), "FMP 正股",
                                     Decimal("100"))

    def test_a_quote_above_the_kraken_bid_but_close_becomes_a_near_miss(self):
        status, row = self._quote(Decimal("100.4"))     # 比有效卖价 99.9 贵 50 bps
        self.assertEqual(status, "near")
        self.assertTrue(row["near_miss"])
        self.assertIsNone(row["net_usdt"])
        self.assertIn("还贵", row["blocked_by"])

    def test_a_quote_far_above_the_kraken_bid_is_dropped(self):
        status, row = self._quote(Decimal("105"))       # 贵 510 bps
        self.assertEqual((status, row), ("miss", None))

    def test_a_quote_below_the_kraken_bid_is_a_hit_with_the_sell_price(self):
        status, row = self._quote(Decimal("95"))
        self.assertEqual(status, "hit")
        self.assertEqual(row["sell"], "100")
        self.assertTrue(row["gap_bps"].startswith("-500"))


class HoldingsTests(unittest.TestCase):
    """钱包 / Kraken 持仓展示。纯解析与算术，不碰网络。"""

    def setUp(self):
        self.mod = _load()
        self.mod._state.clear()
        self.mod._state["refs"] = {"MRNA": "190.56", "COIN": "188.645"}
        self.mod._state["sol_usd"] = "119.31"
        self.mod._ticker_of_symbol = {"MRNAx": "MRNA", "COINx": "COIN"}

    def test_kraken_balance_keys_are_named_for_display(self):
        self.assertEqual(self.mod.kraken_asset("MRNAx.T"), ("MRNAx", "xStock"))
        self.assertEqual(self.mod.kraken_asset("ZUSD"), ("USD", "法币"))
        self.assertEqual(self.mod.kraken_asset("USDG"), ("USDG", "稳定币"))
        self.assertEqual(self.mod.kraken_asset("XBT"), ("BTC", "加密"))

    def test_stablecoins_are_valued_at_one(self):
        self.assertEqual(self.mod._value_of("USDG", Decimal("39.72613"), {}), Decimal("39.72613"))

    def test_xstock_value_is_amount_times_multiplier_times_reference(self):
        value = self.mod._value_of("MRNAx", Decimal("0.54"), {"MRNAx": Decimal("1")})
        self.assertEqual(value, Decimal("0.54") * Decimal("190.56"))

    def test_name_without_a_reference_is_left_blank(self):
        self.assertIsNone(self.mod._value_of("NBISx", Decimal("1"), {"NBISx": Decimal("1")}))

    def test_gas_value_uses_the_sol_reference(self):
        self.assertEqual(self.mod._gas_value("SOL", Decimal("0.020097781")),
                         Decimal("0.020097781") * Decimal("119.31"))
        self.assertIsNone(self.mod._gas_value("OKB", Decimal("0.0017705")))

    def test_row_amount_keeps_precision_but_drops_padding(self):
        row = self.mod._hold_row("USDG", Decimal("39.72613000"), "稳定币", Decimal("39.73"))
        self.assertEqual(row["amount"], "39.72613")
        self.assertEqual(row["value"], "39.73")

    def test_row_without_a_price_shows_nothing(self):
        self.assertIsNone(self.mod._hold_row("OKB", Decimal("0.0017"), "Gas", None)["value"])

    def test_total_skips_rows_without_a_price(self):
        part = {"rows": [{"value": "1.00"}, {"value": None}, {"value": "2.5"}]}
        self.assertEqual(self.mod._part_total(part), "3.50")
        self.assertIsNone(self.mod._part_total({"rows": [{"value": None}]}))

    def test_xlayer_watch_lists_the_cash_leg_first(self):
        self.mod.EVM_ROUTES = []
        self.mod.load_census = lambda: ([], {}, "")
        watch = self.mod.xlayer_watch()
        self.assertEqual([row[0] for row in watch], ["USDG", "USDT0"])
        self.assertTrue(all(row[1] == "稳定币" for row in watch))


class PageTests(unittest.TestCase):
    """页面上的两处展示口径（operator 2026-10-06）。"""

    def setUp(self):
        self.mod = _load()

    def test_net_column_shows_the_plain_number(self):
        """「净利 USDT」只印数字，不再拼成 0.001 @10U。"""
        self.assertIn("hit.net_usdt == null ? '–' : hit.net_usdt", self.mod.PAGE)
        self.assertNotIn("' @' + hit.size_planned", self.mod.PAGE)

    def test_holdings_blocks_are_separated_by_a_vertical_line(self):
        self.assertIn(".holdings > div { padding: 0 22px; border-left: 1px solid", self.mod.PAGE)

    def test_the_table_shows_the_kraken_sell_price_next_to_the_buy_price(self):
        self.assertIn("<th>Kraken 卖价<br>", self.mod.PAGE)
        self.assertIn("<th>正股参考<br>", self.mod.PAGE)
        self.assertIn("{v: hit.sell == null ? '–' : hit.sell", self.mod.PAGE)


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_stockdata_is_mapped_per_ticker_and_blanks_are_skipped(self):
        payload = {
            "m1": {"usdPrice": 80.3, "stockData": {"price": 81.01}},
            "m2": {"stockData": {"price": 0}},
            "m3": {"usdPrice": 1.0},
            "m4": {"stockData": {"price": 10}},
        }
        refs = self.mod.parse_stock_refs(payload, {"m1": "TQQQ", "m2": "X", "m3": "Y"})
        self.assertEqual(refs, {"TQQQ": Decimal("81.01")})

    def test_yahoo_last_uses_the_last_extended_hours_print(self):
        payload = {"chart": {"result": [{
            "meta": {"regularMarketPrice": 24.7},
            "indicators": {"quote": [{"close": [24.9, 25.16, None]}]},
        }]}}
        self.assertEqual(self.mod.parse_yahoo_last(payload), Decimal("25.16"))

    def test_yahoo_last_falls_back_to_the_regular_price(self):
        payload = {"chart": {"result": [{"meta": {"regularMarketPrice": 24.7},
                                         "indicators": {"quote": [{"close": [None]}]}}]}}
        self.assertEqual(self.mod.parse_yahoo_last(payload), Decimal("24.7"))

    def test_cross_check_flags_then_clears(self):
        self.mod._stock_refs.clear()
        self.mod._ref_alarms.clear()
        self.mod._stock_refs["GME"] = (Decimal("25.16"), 0.0)
        self.mod.cross_check_refs(["GME"], fetch=lambda _t: Decimal("24.70"))
        self.assertIn("GME", self.mod._ref_alarms)
        self.mod.cross_check_refs(["GME"], fetch=lambda _t: Decimal("25.15"))
        self.assertNotIn("GME", self.mod._ref_alarms)

    def test_cross_check_without_independent_price_does_not_judge(self):
        self.mod._stock_refs.clear()
        self.mod._ref_alarms.clear()
        self.mod._stock_refs["AAPL"] = (Decimal("333.6"), 0.0)
        self.mod.cross_check_refs(["AAPL"], fetch=lambda _t: None)
        self.assertEqual(self.mod._ref_alarms, {})
        self.assertEqual(self.mod._ref_checks[-1]["result"], "雅虎无数据，未判断")

    def test_evm_cache_round_trip_keeps_decimal_fee(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.mod.EVM_CACHE = Path(tmp) / "evm.json"
            route = {"symbol": "SLVx", "token": "0xb8", "pool_name": "wSLVx / USDC 0.05%",
                     "fee": Decimal("0.0005"), "dec_stable": 6, "v2": False}
            self.mod.save_evm_cache([route])
            cache = self.mod.load_evm_cache()
            self.assertEqual(cache["SLVx"]["fee"], Decimal("0.0005"))
            self.assertIsNotNone(self.mod.cached_route(
                {"symbol": "SLVx", "token": "0xb8", "pool_name": "wSLVx / USDC 0.05%"}, cache))
            self.assertIsNone(self.mod.cached_route(
                {"symbol": "SLVx", "token": "0xOTHER", "pool_name": "wSLVx / USDC 0.05%"}, cache))


if __name__ == "__main__":
    unittest.main()
