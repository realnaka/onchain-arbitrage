"""Hermetic tests for the Kraken xStock sell module. No network, no orders."""
from __future__ import annotations

import unittest
from decimal import Decimal

from arb_monitor.execution import xstocks_sell as sell
from arb_monitor.execution.xstocks_buy import Policy


class FakeClient:
    """记录下单，并按脚本决定是否成交。"""

    def __init__(self, *, fill_after: int = 0, bid_after: str | None = None):
        self.orders: list[dict] = []
        self.cancels: list[dict] = []
        self.bids = [0]
        self.fill_after = fill_after
        self.bid_after = bid_after
        self.held = Decimal("6.44780479")

    def order(self, symbol, volume, **kw):
        self.orders.append({"symbol": symbol, "volume": volume, **kw})
        if len(self.orders) > self.fill_after:
            self.held = Decimal(0)
        return {"txid": [f"OID{len(self.orders)}"]}

    def balance(self):
        return {"VIDAx.T": self.held}

    def open_orders(self):
        return [{"id": "OID1", "descr": {"pair": "VIDAx/USD"}}] if self.held else []

    def _post(self, path, data):
        self.cancels.append(data)
        return {}

    def depth(self, symbol, **kw):
        return {"bids": [[self.bid_after or "1.58", "783.66", 0]],
                "asks": [["1.83", "100", 0]]}


def policy() -> Policy:
    return Policy.load()


class PriceTests(unittest.TestCase):
    def test_quantize_up_rounds_to_tick(self):
        self.assertEqual(sell.quantize_up(Decimal("1.58205"), Decimal("0.01")),
                         Decimal("1.59"))
        self.assertEqual(sell.quantize_up(Decimal("1.58"), Decimal("0.01")),
                         Decimal("1.58"))

    def test_maker_price_is_above_bid_and_never_crosses_the_ask(self):
        plan = sell.plan_sell(policy(), symbol="VIDAx", tokens="6.44780479",
                              ref=Decimal("1.59"),
                              bids=[["1.58", "783.66", 0]], asks=[["1.83", "100", 0]],
                              pair_status="post_only")
        self.assertTrue(plan.ok, plan.reason)
        self.assertEqual(plan.gate_price, Decimal("1.5821"))
        self.assertEqual(plan.maker_price, Decimal("1.59"))
        self.assertLess(plan.maker_price, plan.best_ask)
        self.assertTrue(plan.maker_price > plan.best_bid)

    def test_maker_price_is_none_when_ask_is_below_gate(self):
        plan = sell.plan_sell(policy(), symbol="VIDAx", tokens="6.44780479",
                              ref=Decimal("1.59"),
                              bids=[["1.583", "783.66", 0]], asks=[["1.584", "100", 0]],
                              pair_status="online")
        self.assertTrue(plan.ok, plan.reason)
        self.assertIsNone(plan.maker_price)
        self.assertTrue(plan.taker_allowed)


class GateTests(unittest.TestCase):
    def test_unknown_pair_status_is_refused(self):
        plan = sell.plan_sell(policy(), symbol="VIDAx", tokens="6.44", ref=Decimal("1.59"),
                              bids=[["1.58", "100", 0]], asks=[["1.83", "100", 0]],
                              pair_status="")
        self.assertFalse(plan.ok)
        self.assertIn("状态", plan.reason)

    def test_below_kraken_order_minimum_is_refused(self):
        plan = sell.plan_sell(policy(), symbol="VIDAx", tokens="1.0", ref=Decimal("1.59"),
                              bids=[["1.58", "700", 0]], asks=[["1.83", "100", 0]],
                              pair_status="online", order_min=Decimal("1.87"))
        self.assertFalse(plan.ok)
        self.assertIn("最小", plan.reason)

    def test_thin_book_is_refused(self):
        plan = sell.plan_sell(policy(), symbol="VIDAx", tokens="6.44", ref=Decimal("1.59"),
                              bids=[["1.58", "1", 0]], asks=[["1.83", "100", 0]],
                              pair_status="online")
        self.assertFalse(plan.ok)
        self.assertIn("吃不下", plan.reason)

    def test_low_bid_still_allows_posting_a_maker_order(self):
        # 买一 1.50 低于闸门，但我们可以挂 1.59（仍是最优卖价）
        plan = sell.plan_sell(policy(), symbol="VIDAx", tokens="3", ref=Decimal("1.59"),
                              bids=[["1.50", "700", 0]], asks=[["1.83", "100", 0]],
                              pair_status="online")
        self.assertTrue(plan.ok, plan.reason)
        self.assertEqual(plan.maker_price, Decimal("1.59"))
        self.assertFalse(plan.taker_allowed)

    def test_no_maker_and_taker_below_gate_is_refused(self):
        # 卖一 1.55 太低，挂 1.59 会变成吃单；买一 1.50 也低于闸门
        plan = sell.plan_sell(policy(), symbol="VIDAx", tokens="3", ref=Decimal("1.59"),
                              bids=[["1.50", "700", 0]], asks=[["1.55", "100", 0]],
                              pair_status="online")
        self.assertFalse(plan.ok)
        self.assertIn("挂不上", plan.reason)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.plan_kwargs = dict(symbol="VIDAx", tokens="6.44780479", ref=Decimal("1.59"),
                                bids=[["1.58", "783.66", 0]], asks=[["1.83", "100", 0]],
                                pair_status="post_only")

    def test_maker_fill(self):
        plan = sell.plan_sell(policy(), **self.plan_kwargs)
        client = FakeClient(fill_after=0)
        out = sell.run_sell(client, policy(), plan, confirm="live",
                            clock=lambda: 0, sleep=lambda s: None)
        self.assertTrue(out.fill)
        self.assertEqual(out.reason, "maker 成交")
        self.assertEqual(client.orders[0]["price"], "1.59")
        # 绝不能带 post_only（xStocks 不支持）
        self.assertNotIn("post_only", str(client.orders[0].get("oflags")))

    def test_maker_timeout_falls_back_to_taker_when_bid_still_passes(self):
        plan = sell.plan_sell(policy(), **self.plan_kwargs)
        client = FakeClient(fill_after=99, bid_after="1.59")
        clock = iter([0, 0, 0, 10**9]).__next__
        out = sell.run_sell(client, policy(), plan, confirm="live",
                            clock=clock, sleep=lambda s: None)
        self.assertEqual(len(client.orders), 2)
        self.assertEqual(client.orders[1]["price"], "1.59")
        self.assertIn("改吃买一", out.reason)
        self.assertTrue(client.cancels)

    def test_maker_timeout_stops_when_bid_dropped_below_gate(self):
        plan = sell.plan_sell(policy(), **self.plan_kwargs)
        client = FakeClient(fill_after=99, bid_after="1.50")
        clock = iter([0, 0, 0, 10**9]).__next__
        out = sell.run_sell(client, policy(), plan, confirm="live",
                            clock=clock, sleep=lambda s: None)
        self.assertFalse(out.ok)
        self.assertIn("不卖", out.reason)
        self.assertEqual(len(client.orders), 1)   # 没有再下一单


if __name__ == "__main__":
    unittest.main()
