"""Hermetic checks for the Ethereum xStocks auto decision core."""
from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal

from arb_monitor.execution import xstocks_eth_auto as auto


def policy() -> auto.EthPolicy:
    return auto.EthPolicy.load()


def row(**over) -> dict:
    base = {
        "chain": "eth",
        "symbol": "STRCx",
        "tier": "green",
        "direct_deposit": True,
        "deposit_credit_verified": True,
        "ref_stale": False,
        "exit_cache_stale": False,
        "best_net_usdt": "1.0",
        "best_net_bps": "100",
        "best_size_usd": "20",
        "largest_profitable_size_usd": "20",
        "gas_usdt": "0.5",
    }
    base.update(over)
    return base


class PolicyTests(unittest.TestCase):
    def test_policy_loads_execution_controls(self):
        p = policy()
        self.assertIn(p.auto_orders, {"off", "paper", "live"})
        self.assertIn(p.mode, {"off", "paper", "live"})

    def test_live_needs_three_keys(self):
        p = policy()
        live = replace(p, auto_orders="live", mode="live", live_ack="op")
        self.assertTrue(live.live_ok()[0])
        self.assertFalse(replace(live, auto_orders="off").live_ok()[0])
        self.assertFalse(replace(live, live_ack="").live_ok()[0])


class CandidateTests(unittest.TestCase):
    def test_only_verified_direct_green_eth_rows(self):
        watch = {"rows": [
            row(),
            row(symbol="BAD1", tier="amber"),
            row(symbol="BAD2", direct_deposit=False),
            row(symbol="BAD3", deposit_credit_verified=False),
            row(symbol="BAD4", exit_cache_stale=True),
            row(symbol="ETH2", best_net_usdt="2.0"),
        ]}
        rows = auto.order_candidates(watch, policy())
        self.assertEqual([r["symbol"] for r in rows], ["ETH2", "STRCx"])
        self.assertTrue(all(r["_size_usd"] <= Decimal("25") for r in rows))

    def test_below_min_profit_is_dropped(self):
        rows = auto.order_candidates({"rows": [row(best_net_usdt="0.01")]}, policy())
        self.assertEqual(rows, [])


class GateTests(unittest.TestCase):
    def setUp(self):
        self.p = replace(policy(), auto_orders="live", mode="live", live_ack="op")
        self.state = auto.EthState()

    def gate(self, **over):
        kw = dict(snapshot_age=10.0, kill_switch=False, auto_halt=False,
                  eth_usdc=Decimal("25"), eth_gas=Decimal("0.002"))
        kw.update(over)
        return auto.open_gate(self.p, self.state, **kw)

    def test_happy_path(self):
        self.assertTrue(self.gate()[0])

    def test_kill_and_halt(self):
        self.assertIn("KILL_SWITCH", self.gate(kill_switch=True)[1])
        self.assertIn("AUTO_HALT", self.gate(auto_halt=True)[1])

    def test_floors_and_stale_snapshot(self):
        self.assertIn("USDC", self.gate(eth_usdc=Decimal("1"))[1])
        self.assertIn("gas", self.gate(eth_gas=Decimal("0.0001"))[1])
        self.assertIn("过期", self.gate(snapshot_age=9999)[1])


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.p = replace(policy(), auto_orders="live", mode="live", live_ack="op")

    def review(self, **over):
        kw = dict(tier="green", direct_deposit=True, deposit_unit_verified=True,
                  size_usd=Decimal("20"), kraken_units=Decimal("0.21"),
                  sell_price=Decimal("99.20"), order_min=Decimal("0.0546"),
                  book_ok=True, book_reason="通过", gas_usd=Decimal("0.5"))
        kw.update(over)
        return auto.review_ok(self.p, **kw)

    def test_good_review_passes(self):
        ok, why, stats = self.review()
        self.assertTrue(ok, why)
        self.assertGreater(stats["net_usdt"], self.p.min_net_profit_usdt)

    def test_unverified_unit_is_rejected(self):
        ok, why, _ = self.review(deposit_unit_verified=False)
        self.assertFalse(ok)
        self.assertIn("入账单位", why)

    def test_below_order_min_is_rejected(self):
        ok, why, _ = self.review(kraken_units=Decimal("0.01"))
        self.assertFalse(ok)
        self.assertIn("最小", why)

    def test_below_net_is_rejected(self):
        ok, why, _ = self.review(sell_price=Decimal("95.00"))
        self.assertFalse(ok)
        self.assertIn("净利", why)


class TreasuryTests(unittest.TestCase):
    def test_keep_with_enough_eth_usdc(self):
        out = auto.treasury_decision(policy(), kraken_usd=Decimal("100"),
                                     eth_usdc=Decimal("30"))
        self.assertEqual(out["action"], "keep")

    def test_wait_when_kraken_is_low(self):
        out = auto.treasury_decision(policy(), kraken_usd=Decimal("5"),
                                     eth_usdc=Decimal("1"))
        self.assertEqual(out["action"], "wait")

    def test_withdraw_when_eth_is_low_and_kraken_funded(self):
        out = auto.treasury_decision(policy(), kraken_usd=Decimal("100"),
                                     eth_usdc=Decimal("1"))
        self.assertEqual(out["action"], "withdraw")
        self.assertEqual(out["key"], "ethusdc1")


if __name__ == "__main__":
    unittest.main()
