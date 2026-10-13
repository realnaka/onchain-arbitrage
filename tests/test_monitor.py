import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from arb_monitor.core.math import net_edge, walk_book
from arb_monitor.core.models import FeeModel, Level, OrderBook
from arb_monitor.ledger.store import EventStore
from arb_monitor.ops.dedup import SignalDeduplicator
from arb_monitor.strategy.spread import CrossVenueSpreadStrategy


class MonitorTests(unittest.TestCase):
    def test_walk_book_uses_depth(self):
        filled, notional = walk_book(
            [Level(Decimal("10"), Decimal("2")), Level(Decimal("11"), Decimal("3"))],
            Decimal("4"),
        )
        self.assertEqual(filled, Decimal("4"))
        self.assertEqual(notional, Decimal("42"))

    def test_cost_can_remove_apparent_edge(self):
        _, net, profit = net_edge(
            Decimal("100"), Decimal("100.05"),
            FeeModel(buy_bps=Decimal("3"), sell_bps=Decimal("3")),
            Decimal("1000"),
        )
        self.assertLess(net, Decimal("0"))
        self.assertLess(profit, Decimal("0"))

    def test_stale_book_is_rejected(self):
        now = 1_000_000
        books = [
            OrderBook("a", "BTC/USDT", (), (Level(Decimal("100"), Decimal("1")),), now - 5000, now),
            OrderBook("b", "BTC/USDT", (Level(Decimal("101"), Decimal("1")),), (), now, now),
        ]
        strategy = CrossVenueSpreadStrategy(
            "a", "b", FeeModel(), Decimal("100"), Decimal("1"), Decimal("0.01"),
            max_age_ms=1000,
        )
        self.assertEqual(list(strategy.on_snapshot(books, now)), [])

    def test_dedup_and_sqlite(self):
        now = 1_000_000
        books = [
            OrderBook("a", "BTC/USDT", (), (Level(Decimal("100"), Decimal("1")),), now, now),
            OrderBook("b", "BTC/USDT", (Level(Decimal("101"), Decimal("1")),), (), now, now),
        ]
        strategy = CrossVenueSpreadStrategy(
            "a", "b", FeeModel(), Decimal("100"), Decimal("1"), Decimal("0.01")
        )
        intent = list(strategy.on_snapshot(books, now))[0]
        dedup = SignalDeduplicator(3000)
        self.assertTrue(dedup.accept(intent))
        self.assertFalse(dedup.accept(intent))
        with TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "events.db")
            row_id = store.record_intent(intent)
            store.record_manual_execution(row_id, "放弃", "测试", now)
            store.close()


if __name__ == "__main__":
    unittest.main()

