"""Hermetic tests for the xStocks buy module. No network, no signing."""
from __future__ import annotations

import unittest
from decimal import Decimal

from arb_monitor.execution import xstocks_buy as buy


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = buy.Policy.load()

    def test_policy_matches_operator_settings(self):
        # operator 2026-10-06：单笔上限 10 → 25 USDT。
        self.assertEqual(self.policy.per_trade_quote_usdt, Decimal("25"))
        self.assertEqual(self.policy.min_net_profit_usdt, Decimal("0.1"))
        self.assertEqual(self.policy.max_risk, "低")
        self.assertEqual(self.policy.withdraw_threshold_usdg, Decimal("100"))
        self.assertTrue(self.policy.maker_first)

    def test_size_is_capped_by_the_ladder_not_by_a_new_quote(self):
        # 买 1 U 就推动价格 -> 最多买 1 U
        self.assertEqual(self.policy.plan_size([Decimal("1")]), Decimal("1"))
        # 100 U 都不动价 -> 只买到单笔上限
        self.assertEqual(self.policy.plan_size([Decimal("1"), Decimal("5"), Decimal("20"),
                                                Decimal("100")]), Decimal("25"))
        self.assertEqual(self.policy.plan_size([]), Decimal("0"))


class RiskTests(unittest.TestCase):
    def test_buckets_match_the_board(self):
        self.assertEqual(buy.classify_risk(Decimal("1.9")), "低")
        self.assertEqual(buy.classify_risk(Decimal("2")), "中")
        self.assertEqual(buy.classify_risk(Decimal("5")), "高")


class DecideTests(unittest.TestCase):
    def setUp(self):
        self.policy = buy.Policy.load()

    def test_profitable_low_risk_passes(self):
        ok, reason = buy.decide(self.policy, size_usd=Decimal("10"),
                                share=Decimal("1.5561"), ref=Decimal("1.59"),
                                impact_pct=Decimal("0.73"))
        self.assertTrue(ok, reason)
        self.assertIn("net", reason)

    def test_high_risk_is_refused(self):
        ok, reason = buy.decide(self.policy, size_usd=Decimal("10"),
                                share=Decimal("1.5561"), ref=Decimal("1.59"),
                                impact_pct=Decimal("6"))
        self.assertFalse(ok)
        self.assertIn("风险", reason)

    def test_thin_edge_is_refused(self):
        ok, reason = buy.decide(self.policy, size_usd=Decimal("10"),
                                share=Decimal("1.5880"), ref=Decimal("1.59"),
                                impact_pct=Decimal("0.1"))
        self.assertFalse(ok)
        self.assertIn("净利", reason)

    def test_fixed_fees_can_kill_a_thin_edge(self):
        ok, _ = buy.decide(self.policy, size_usd=Decimal("10"),
                           share=Decimal("1.5561"), ref=Decimal("1.59"),
                           impact_pct=Decimal("0.73"),
                           fixed_fee_usd=Decimal("1.0"))
        self.assertFalse(ok)


class QuoteParsingTests(unittest.TestCase):
    def test_parse_quote_uses_decimals(self):
        payload = {"inAmount": "10000000", "outAmount": "6426322215",
                   "priceImpactPct": "0.007309",
                   "routePlan": [{"swapInfo": {"label": "BisonFi"}},
                                 {"swapInfo": {"label": "Raydium CLMM"}}]}
        spent, got, route, impact = buy.parse_quote(payload, swap_mint_decimals=6,
                                                   token_decimals=8)
        self.assertEqual(spent, Decimal("10"))
        self.assertEqual(got, Decimal("64.26322215"))
        self.assertEqual(route, "BisonFi → Raydium CLMM")
        self.assertEqual(impact, Decimal("0.7309"))

    def test_parse_quote_rejects_missing_fields(self):
        with self.assertRaises(ValueError):
            buy.parse_quote({}, swap_mint_decimals=6, token_decimals=8)

    def test_quote_url_carries_amount_and_slippage(self):
        url = buy.quote_url("USDCmint", "VIDAmint", 10_000_000, 10)
        self.assertIn("amount=10000000", url)
        self.assertIn("slippageBps=10", url)
        self.assertIn("outputMint=VIDAmint", url)


class SimulateTests(unittest.TestCase):
    def test_simulation_failure_is_not_ok(self):
        def transport(url, body):
            return {"result": {"value": {"err": {"InstructionError": [0, "Custom"]},
                                         "logs": []}}}
        out = buy.simulate("base64tx", transport=transport)
        self.assertFalse(out["ok"])
        self.assertIsNotNone(out["err"])

    def test_simulation_success(self):
        def transport(url, body):
            self.assertEqual(body["method"], "simulateTransaction")
            return {"result": {"value": {"err": None, "unitsConsumed": 12345,
                                         "logs": ["ok"]}}}
        out = buy.simulate("base64tx", transport=transport)
        self.assertTrue(out["ok"])
        self.assertEqual(out["units_consumed"], 12345)


if __name__ == "__main__":
    unittest.main()
