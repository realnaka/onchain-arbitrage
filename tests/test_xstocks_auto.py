"""Hermetic checks for the xStocks auto chain core. No network, no wallet."""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from arb_monitor.execution import xstocks_auto as auto


def policy() -> auto.AutoPolicy:
    return auto.AutoPolicy.load()


def hit(**over) -> dict:
    row = {
        "symbol": "TQQQx", "ticker": "TQQQ", "network": "Solana",
        "share": "82.95", "sell": "83.09", "ref": "83.23",
        "gap_bps": "-17.0", "spend_usd": "25", "risk": "低",
        "net_usdt": "0.18", "executable": True, "near_miss": False,
    }
    row.update(over)
    return row


class PolicyTests(unittest.TestCase):
    def test_real_policy_file_loads_with_every_field(self):
        p = policy()
        self.assertGreater(p.per_trade_quote_usdt, 0)
        self.assertGreater(p.solana_target_usdt, p.solana_floor_usdt)
        self.assertGreater(p.daily_open_quote_usdt, p.per_trade_quote_usdt)
        self.assertGreater(p.daily_trades_max, 0)
        self.assertGreater(p.transfer_first_sol, p.transfer_repeat_sol)
        self.assertIsInstance(p.snapshot_max_age_s, int)

    def test_live_needs_all_three_keys(self):
        p = policy()
        self.assertFalse(replace(p, auto_orders="off").live_ok()[0])
        self.assertFalse(replace(p, auto_orders="live", mode="paper").live_ok()[0])
        self.assertFalse(replace(p, auto_orders="live", mode="live", live_ack="").live_ok()[0])
        ok, why = replace(p, auto_orders="live", mode="live", live_ack="op-1").live_ok()
        self.assertTrue(ok, why)


class CostTests(unittest.TestCase):
    def test_first_transfer_costs_more_than_repeat(self):
        p = policy()
        kw = dict(size_usd=Decimal("25"), tokens=Decimal("0.30"),
                  sell_price=Decimal("83.09"), sol_usd=Decimal("119"))
        first = auto.estimate_net(p, first_transfer=True, **kw)
        repeat = auto.estimate_net(p, first_transfer=False, **kw)
        self.assertGreater(first["net_usdt"], Decimal("-9"))
        self.assertLess(first["net_usdt"], repeat["net_usdt"])
        delta = repeat["net_usdt"] - first["net_usdt"]
        self.assertAlmostEqual(float(delta), float((p.transfer_first_sol - p.transfer_repeat_sol) * Decimal("119")), places=6)

    def test_rent_is_reported_not_deducted_twice(self):
        p = policy()
        stats = auto.estimate_net(p, size_usd=Decimal("25"), tokens=Decimal("0.3"),
                                  sell_price=Decimal("83"), sol_usd=Decimal("119"),
                                  first_transfer=True)
        self.assertGreater(stats["rent_refundable_usdt"], 0)
        self.assertEqual(
            stats["net_usdt"],
            stats["gross_usdt"] - stats["sell_fee_usdt"] - stats["chain_fee_usdt"])


class CandidateTests(unittest.TestCase):
    def test_only_solana_low_risk_real_hits_with_edge(self):
        snap = {"hits": [
            hit(),
            hit(symbol="Vx", net_usdt="0.5", risk="高"),
            hit(symbol="Wx", net_usdt="0.5", network="XLayer"),
            hit(symbol="Ny", net_usdt="0.5", near_miss=True),
            hit(symbol="Zz", net_usdt="0.01"),
        ]}
        rows = auto.order_candidates(snap, policy())
        self.assertEqual([r["symbol"] for r in rows], ["TQQQx"])

    def test_candidates_sorted_by_net_and_capped(self):
        snap = {"hits": [hit(symbol=f"S{i}", net_usdt=str(i / 10)) for i in range(1, 6)]}
        rows = auto.order_candidates(snap, policy(), limit=3)
        self.assertEqual([r["symbol"] for r in rows], ["S5", "S4", "S3"])

    def test_snapshot_age(self):
        self.assertEqual(auto.snapshot_age_s({"finished_ms": 1_000}, now_ms=61_000), 60.0)
        self.assertIsNone(auto.snapshot_age_s({}))


class GateTests(unittest.TestCase):
    def setUp(self):
        self.p = replace(policy(), auto_orders="live", mode="live", live_ack="op-1")
        self.state = auto.AutoState(trades_today=0, opened_quote_usdt="0")

    def gate(self, **over):
        kw = dict(snapshot_age=10.0, kill_switch=False, auto_halt=False,
                  onchain_usdt=Decimal("60"))
        kw.update(over)
        return auto.open_gate(self.p, self.state, **kw)

    def test_happy_path(self):
        self.assertTrue(self.gate()[0])

    def test_kill_switch_stops_everything(self):
        self.assertIn("KILL_SWITCH", self.gate(kill_switch=True)[1])

    def test_auto_halt_only_blocks_opening(self):
        ok, why = self.gate(auto_halt=True)
        self.assertFalse(ok)
        self.assertIn("只挡开仓", why)

    def test_soft_pause_blocks(self):
        self.state.pause_reason = "连续 2 笔净亏"
        self.assertFalse(self.gate()[0])

    def test_off_mode_never_opens(self):
        ok, why = auto.open_gate(replace(self.p, auto_orders="off"), self.state,
                                 snapshot_age=5.0, kill_switch=False, auto_halt=False)
        self.assertFalse(ok)
        self.assertIn("off", why)

    def test_stale_snapshot_blocks(self):
        self.assertIn("过期", self.gate(snapshot_age=10_000.0)[1])

    def test_daily_caps_block(self):
        self.state.trades_today = self.p.daily_trades_max
        self.assertIn("笔", self.gate()[1])
        self.state.trades_today = 0
        self.state.opened_quote_usdt = str(self.p.daily_open_quote_usdt)
        self.assertIn("累计开仓", self.gate()[1])

    def test_onchain_floor_blocks_buying(self):
        ok, why = self.gate(onchain_usdt=Decimal("10"))
        self.assertFalse(ok)
        self.assertIn("floor", why)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.p = replace(policy(), auto_orders="live", mode="live", live_ack="op-1")

    def review(self, **over):
        kw = dict(size_usd=Decimal("25"), tokens=Decimal("0.31"),
                  sell_price=Decimal("83.09"), sol_usd=Decimal("119"),
                  first_transfer=False, risk="低", book_ok=True, book_reason="通过",
                  deposit_ok=True, deposit_reason="可充")
        kw.update(over)
        return auto.review_ok(self.p, **kw)

    def test_good_quote_passes_with_stats(self):
        ok, why, stats = self.review()
        self.assertTrue(ok, why)
        self.assertGreater(stats["net_usdt"], self.p.min_net_profit_usdt)

    def test_thin_book_is_rejected(self):
        ok, why, _ = self.review(book_ok=False, book_reason="盘口吃不下")
        self.assertFalse(ok)
        self.assertIn("出口", why)

    def test_below_deposit_minimum_is_rejected(self):
        ok, why, _ = self.review(deposit_ok=False, deposit_reason="低于最小充值")
        self.assertFalse(ok)
        self.assertIn("充值", why)

    def test_low_net_is_rejected(self):
        ok, why, _ = self.review(tokens=Decimal("0.30"), sell_price=Decimal("82.96"))
        self.assertFalse(ok)
        self.assertIn("净利", why)

    def test_high_risk_is_rejected(self):
        ok, why, _ = self.review(risk="中")
        self.assertFalse(ok)


class TreasuryTests(unittest.TestCase):
    def setUp(self):
        self.p = replace(policy(), auto_orders="live", mode="live", live_ack="op-1")

    def test_keep_when_onchain_is_healthy(self):
        out = auto.treasury_decision(self.p, kraken_usd=Decimal("300"),
                                     onchain_usdt=Decimal("60"))
        self.assertEqual(out["action"], "keep")

    def test_withdraw_when_onchain_low_and_kraken_funded(self):
        out = auto.treasury_decision(self.p, kraken_usd=Decimal("120"),
                                     onchain_usdt=Decimal("3"))
        self.assertEqual(out["action"], "withdraw")
        self.assertEqual(out["amount"], self.p.solana_target_usdt)
        self.assertEqual(out["key"], self.p.withdraw_key)

    def test_wait_when_kraken_has_not_accumulated_enough(self):
        out = auto.treasury_decision(self.p, kraken_usd=Decimal("30"),
                                     onchain_usdt=Decimal("0"))
        self.assertEqual(out["action"], "wait")

    def test_withdraw_never_exceeds_what_kraken_holds_after_fee(self):
        out = auto.treasury_decision(self.p, kraken_usd=Decimal("45"),
                                     onchain_usdt=Decimal("0"))
        self.assertIn(out["action"], {"wait", "withdraw"})
        if out["action"] == "withdraw":
            self.assertLessEqual(out["amount"],
                                 Decimal("45") - self.p.withdraw_fee_usdt)


class FundingAlertTests(unittest.TestCase):
    def setUp(self):
        self.p = policy()

    def test_two_chain_shortage_and_gas_are_separate_alerts(self):
        alerts = auto.funding_alerts(self.p, solana_usdt=Decimal("5"),
                                     xlayer_usdg=Decimal("1"), sol_lamports=1_000_000)
        keys = {a["key"] for a in alerts}
        self.assertEqual(keys, {"solana_funding_low", "xlayer_funding_low", "sol_gas_low"})
        self.assertTrue(all(a["audience"] == "alert" for a in alerts))

    def test_healthy_wallets_raise_nothing(self):
        alerts = auto.funding_alerts(self.p, solana_usdt=Decimal("60"),
                                     xlayer_usdg=Decimal("50"), sol_lamports=20_000_000)
        self.assertEqual(alerts, [])

    def test_unknown_balance_is_not_treated_as_zero(self):
        alerts = auto.funding_alerts(self.p, solana_usdt=None, xlayer_usdg=None,
                                     sol_lamports=None)
        self.assertEqual(alerts, [])


class AlertTrackerTests(unittest.TestCase):
    def test_same_alert_fires_once_per_day(self):
        tracker = auto.AlertTracker()
        self.assertTrue(tracker.should_send("solana_funding_low", "2026-10-06"))
        tracker.mark("solana_funding_low", "2026-10-06")
        self.assertFalse(tracker.should_send("solana_funding_low", "2026-10-06"))
        self.assertTrue(tracker.should_send("solana_funding_low", "2026-10-07"))


class LedgerTests(unittest.TestCase):
    def test_loss_streak_counts_the_tail_only(self):
        rows = [{"net_usdt": "0.2", "mode": "live"}, {"net_usdt": "-0.1", "mode": "live"},
                {"net_usdt": "-0.3", "mode": "live"}]
        self.assertEqual(auto.loss_streak(rows), 2)
        rows.append({"net_usdt": "0.05", "mode": "live"})
        self.assertEqual(auto.loss_streak(rows), 0)
        rows.append({"net_usdt": None, "mode": "live"})
        self.assertEqual(auto.loss_streak(rows), 0)

    def test_manual_rows_without_mode_do_not_count(self):
        """历史手工单不背到自动链头上（VIDAx 那笔旧亏损）。"""
        rows = [{"symbol": "VIDAx", "net_usdt": "-0.1169", "mode": None},
                {"symbol": "TQQQx", "net_usdt": "-0.05", "mode": "live"}]
        self.assertEqual(auto.loss_streak(rows), 1)
        self.assertEqual(auto.loss_streak(rows, only_auto=False), 2)

    def test_row_round_trip_and_day_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cycles.jsonl"
            row = auto.build_ledger_row(
                symbol="TQQQx", network="Solana", size_usd=Decimal("25"),
                state="sold", reason="完成", now=1_790_000_000,
                tokens="0.30", net_usdt="0.18", bought_at_ms=1)
            auto.append_ledger(row, path)
            auto.append_ledger({**row, "day": "1999-01-01", "size_usd": "999"},
                               path)
            rows = auto.read_ledger(path)
            self.assertEqual(len(rows), 2)
            today = auto.opened_rows(rows, row["day"])
            self.assertEqual(len(today), 1)
            self.assertEqual(sum(Decimal(r["size_usd"]) for r in today), Decimal("25"))


class StateTests(unittest.TestCase):
    def test_state_round_trip_and_day_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auto.json"
            state = auto.AutoState(mode="live", running=True, trades_today=7,
                                   opened_quote_usdt="175")
            auto.write_state(state, path)
            back = auto.read_state(path)
            self.assertEqual(back.mode, "live")
            self.assertTrue(back.running)
            self.assertEqual(back.opened_quote_usdt, "175")
            rows = [auto.build_ledger_row(symbol="Ax", network="Solana",
                                          size_usd=Decimal("25"), state="sold",
                                          reason="x", now=1_790_000_000,
                                          net_usdt="-0.2", bought_at_ms=1,
                                          mode="live"),
                    auto.build_ledger_row(symbol="Bx", network="Solana",
                                          size_usd=Decimal("25"), state="sold",
                                          reason="x", now=1_790_000_600,
                                          net_usdt="-0.1", bought_at_ms=2,
                                          mode="live")]
            back.refresh_day(rows[0]["day"], rows)
            self.assertEqual(back.trades_today, 2)
            self.assertEqual(back.opened_quote_usdt, "50")
            self.assertEqual(back.loss_streak, 2)

    def test_missing_state_file_is_an_empty_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = auto.read_state(Path(tmp) / "nope.json")
            self.assertEqual(state.phase, "idle")
            self.assertEqual(state.trades_today, 0)

    def test_transfer_seen_marks_first_transfer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen.json"
            seen = auto.load_transfer_seen(path)
            self.assertTrue(auto.transfer_is_first("TQQQx", seen))
            seen = auto.mark_transfer_seen("TQQQx", seen, path)
            self.assertFalse(auto.transfer_is_first("TQQQx", seen))
            self.assertFalse(auto.transfer_is_first("TQQQx", auto.load_transfer_seen(path)))

    def test_alert_files_are_written_for_the_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = auto.write_alert({"key": "solana_funding_low", "text": "x"},
                                    now=1_790_000_000, directory=tmp)
            self.assertTrue(path.exists())
            self.assertIn("solana_funding_low", path.name)
            self.assertEqual(json.loads(path.read_text())["text"], "x")


if __name__ == "__main__":
    unittest.main()
