"""Hermetic tests for the cycle orchestrator. No network, no money."""
from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from arb_monitor.execution import xstocks_cycle as cyc


class Clock:
    def __init__(self, step=1.0):
        self.value = 1000.0
        self.step = step

    def __call__(self):
        self.value += self.step
        return self.value


def legs(*, buy=None, dep=None, sell=None):
    return (
        (lambda: buy or {"ok": True, "tokens": "6.44780479", "price": "1.5509",
                         "broadcast": True, "confirmed": True}),
        (lambda tokens: dep or {"ok": True, "sent": True, "credited": True}),
        (lambda tokens: sell or {"ok": True, "price": "1.59", "net": "0.24"}),
    )


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ledger = self.tmp / "cycles.jsonl"
        self.lock = cyc.LOCK_PATH
        self.lock.unlink(missing_ok=True)

    def tearDown(self):
        self.lock.unlink(missing_ok=True)

    def go(self, **kw):
        buy, dep, sell = legs(**kw)
        return cyc.run_cycle(symbol="VIDAx", size_usd=Decimal("10"),
                             buy_fn=buy, deposit_fn=dep, sell_fn=sell,
                             now=Clock(), ledger=self.ledger)

    def test_happy_path_records_segment_timings(self):
        out = self.go()
        self.assertEqual(out.state, cyc.SOLD)
        self.assertEqual(out.held_at, "nowhere")
        t = out.timings()
        for key in ("buy_s", "transfer_s", "kraken_credit_s", "sell_s", "total_s"):
            self.assertIsNotNone(t[key], key)
        self.assertEqual(len(self.ledger.read_text().strip().splitlines()), 1)

    def test_buy_gate_refusal_moves_no_money(self):
        out = self.go(buy={"ok": False, "reason": "风险 高"})
        self.assertEqual(out.state, cyc.REFUSED)
        self.assertEqual(out.held_at, "wallet")
        self.assertIn("风险", out.reason)

    def test_unconfirmed_buy_stops_and_never_retries(self):
        out = self.go(buy={"ok": True, "tokens": "6.44", "price": "1.55",
                            "broadcast": True, "confirmed": False})
        self.assertEqual(out.state, cyc.UNKNOWN)
        self.assertEqual(out.held_at, "unknown")
        self.assertIn("不重播", out.reason)

    def test_deposit_below_minimum_leaves_tokens_in_wallet(self):
        out = self.go(dep={"ok": False, "reason": "低于最小充值 0.378"})
        self.assertEqual(out.state, cyc.STOPPED)
        self.assertEqual(out.held_at, "wallet")

    def test_uncredited_deposit_stops_in_transit(self):
        out = self.go(dep={"ok": True, "sent": True, "credited": False})
        self.assertEqual(out.state, cyc.UNKNOWN)
        self.assertEqual(out.held_at, "in_transit")
        self.assertIn("不要重复转账", out.reason)

    def test_failed_sell_leaves_tokens_at_kraken(self):
        out = self.go(sell={"ok": False, "reason": "10 分钟不成交，买一低于闸门"})
        self.assertEqual(out.state, cyc.STOPPED)
        self.assertEqual(out.held_at, "kraken")

    def test_ledger_line_is_json_with_timings(self):
        self.go()
        row = json.loads(self.ledger.read_text().strip().splitlines()[0])
        self.assertEqual(row["symbol"], "VIDAx")
        self.assertEqual(row["size_usd"], "10")
        self.assertEqual(row["state"], "sold")
        self.assertIn("kraken_credit_s", row["timings"])


class LockTests(unittest.TestCase):
    def setUp(self):
        self.lock = Path(tempfile.mkdtemp()) / "lock"

    def test_second_cycle_is_refused_while_one_is_in_flight(self):
        with cyc.CycleLock(self.lock):
            with self.assertRaises(cyc.CycleBusy):
                with cyc.CycleLock(self.lock):
                    pass
        # 释放后可以再进
        with cyc.CycleLock(self.lock):
            pass

    def test_lock_file_is_removed_on_exit(self):
        with cyc.CycleLock(self.lock):
            self.assertTrue(self.lock.exists())
        self.assertFalse(self.lock.exists())


if __name__ == "__main__":
    unittest.main()
