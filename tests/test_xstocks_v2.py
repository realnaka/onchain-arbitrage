"""Hermetic checks for the xStocks v2 decision core. No network."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from arb_monitor.xstocks_v2 import calendar, costs, cycle, funnel, legs_eth, quotes, routes, scoreboard, units
from arb_monitor.xstocks_v2.policy import V2Policy

NY = ZoneInfo("America/New_York")
XLAYER_SELECTOR = 3016212468291539606
SCAN_PATH = Path(__file__).resolve().parents[1] / "scripts" / "xstocks_v2_scan.py"


def policy() -> V2Policy:
    return V2Policy.load()


class UnitTests(unittest.TestCase):
    def test_evm_does_not_multiply(self):
        got = units.to_credit_units("eth", Decimal("1"), Decimal("1.0918"))
        self.assertEqual(got, Decimal("1"))

    def test_solana_multiplies_raw(self):
        got = units.to_credit_units("solana", Decimal("1"), Decimal("1.0918"))
        self.assertEqual(got, Decimal("1.0918"))

    def test_usdg_is_six_decimals_and_xstocks_are_eighteen(self):
        """USDG is 6-decimal, xStocks are 18. Reusing the 18-decimal read for
        USDG keeps "did it move" working and breaks every magnitude comparison
        (2026-10-07: 63.4 USDG read as 6.3e-11)."""
        self.assertEqual(units.USDG_DECIMALS, 6)
        self.assertEqual(units.XSTOCK_DECIMALS, 18)
        self.assertEqual(units.scale_raw(63_400_000, units.USDG_DECIMALS),
                         Decimal("63.4"))
        self.assertEqual(units.scale_raw(10 ** 18, units.XSTOCK_DECIMALS),
                         Decimal("1"))
        # The wrong scale is silently plausible, which is why it has to be
        # passed explicitly rather than defaulted to the xStock case.
        self.assertEqual(units.scale_raw(63_400_000, units.XSTOCK_DECIMALS),
                         Decimal("0.0000000000634"))

    def test_strcx_minimum_uses_effective_units(self):
        self.assertFalse(units.clears_min_units(Decimal("0.05"), units.STRCX_EFFECTIVE_MIN))
        self.assertTrue(units.clears_min_units(units.STRCX_EFFECTIVE_MIN, units.STRCX_EFFECTIVE_MIN))

    def test_unverified_size_is_the_minimum(self):
        size, reason = units.clip_notional(
            balance=Decimal("40"), depth=Decimal("100"), per_trade=Decimal("25"),
            min_notional=Decimal("1"), verified=False)
        self.assertEqual(size, Decimal("1"))
        self.assertEqual(reason, "probe_only")

    def test_verified_size_uses_the_cap(self):
        size, reason = units.clip_notional(
            balance=Decimal("40"), depth=Decimal("100"), per_trade=Decimal("25"),
            min_notional=Decimal("1"), verified=True)
        self.assertEqual(size, Decimal("25"))
        self.assertEqual(reason, "full")

    def test_small_usdc_cannot_buy_strcx(self):
        size, reason = units.clip_notional(
            balance=Decimal("0.24"), depth=Decimal("100"), per_trade=Decimal("25"),
            min_notional=Decimal("5.2"), verified=True)
        self.assertIsNone(size)
        self.assertEqual(reason, "below_min_size")

    def test_credit_mismatch_stops_the_symbol_only(self):
        self.assertEqual(
            units.credit_status(Decimal("1"), Decimal("1.02"), Decimal("0.01")),
            "symbol_halt")
        self.assertEqual(
            units.credit_status(Decimal("0.05184276"), Decimal("0.051843"), Decimal("0.01")),
            "verified")

    def test_empty_credit_sample_is_not_one(self):
        self.assertIsNone(units.clip_notional(
            balance=None, depth=Decimal("10"), per_trade=Decimal("25"),
            min_notional=Decimal("1"), verified=False)[0])


class QuoteTests(unittest.TestCase):
    def test_skew_and_age_drop_the_net(self):
        self.assertFalse(quotes.quotes_fresh(
            now_ms=20_000, recv_a_ms=0, recv_b_ms=0,
            max_age_ms=15_000, max_pair_skew_ms=10_000))
        self.assertFalse(quotes.quotes_fresh(
            now_ms=10_001, recv_a_ms=0, recv_b_ms=10_001,
            max_age_ms=15_000, max_pair_skew_ms=10_000))
        self.assertTrue(quotes.quotes_fresh(
            now_ms=10_000, recv_a_ms=5_000, recv_b_ms=9_000,
            max_age_ms=15_000, max_pair_skew_ms=10_000))

    def test_reference_price_is_not_a_net(self):
        self.assertIsNone(quotes.executable_prices("dexscreener", 1, 2))
        self.assertIsNone(quotes.executable_prices("mid", 1, 2))
        self.assertEqual(quotes.executable_prices("quoter", 1, 2), (1, 2))


class FunnelTests(unittest.TestCase):
    def test_kraken_bid_alone_cannot_open_the_other_direction(self):
        got = funnel.stage1(
            kind="kraken_to_chain", kraken_bid=Decimal("1"), kraken_ask=None,
            chain_buy=None, chain_sell=Decimal("1"), same_pool=False)
        self.assertFalse(got["ok"])
        self.assertEqual(got["reason"], "missing_side")

    def test_same_pool_is_rejected(self):
        got = funnel.stage1(
            kind="dex_to_dex", kraken_bid=None, kraken_ask=None,
            chain_buy=Decimal("1"), chain_sell=Decimal("2"), same_pool=True)
        self.assertEqual(got["reason"], "same_pool")

    def test_stage2_keeps_at_most_twenty(self):
        rows = [{"edge_bps": 100 - i, "reason": ""} for i in range(30)]
        rows.append({"edge_bps": 1000, "reason": "same_pool"})
        picked = funnel.stage2(
            rows, threshold_bps=Decimal("20"), buffer_bps=Decimal("30"), limit=20)
        self.assertEqual(len(picked), 20)
        self.assertTrue(all(row["reason"] != "same_pool" for row in picked))
        self.assertGreater(Decimal(str(picked[0]["edge_bps"])), Decimal(str(picked[-1]["edge_bps"])))


class RouteTests(unittest.TestCase):
    def test_missing_directed_edges(self):
        self.assertFalse(routes.has_bridge("solana", "mantle"))
        self.assertFalse(routes.has_bridge("xlayer", "hyperevm"))
        self.assertTrue(routes.has_bridge("ethereum", "solana"))

    def test_three_dialects(self):
        self.assertEqual(routes.dialect("ethereum", "solana"), "evm_to_solana")
        self.assertEqual(routes.dialect("ethereum", "xlayer"), "evm_to_evm")
        self.assertEqual(routes.dialect("solana", "ethereum"), "solana_source")

    def test_bitmap_772_is_only_for_solana_destination(self):
        ok, _ = routes.extra_args_ok(
            "evm_to_solana", bitmap=772, n_accounts=11,
            source_selector=XLAYER_SELECTOR, expected_selector=XLAYER_SELECTOR)
        self.assertTrue(ok)
        bad, reason = routes.extra_args_ok(
            "evm_to_solana", bitmap=772, n_accounts=11,
            source_selector=1, expected_selector=XLAYER_SELECTOR)
        self.assertFalse(bad)
        self.assertEqual(reason, "source_selector")
        rejected, why = routes.extra_args_ok("evm_to_evm", bitmap=772, n_accounts=11)
        self.assertFalse(rejected)
        self.assertEqual(why, "bitmap_772_on_evm")
        sol, sol_why = routes.extra_args_ok("solana_source", bitmap=772, n_accounts=11)
        self.assertFalse(sol)
        self.assertEqual(sol_why, "solana_source_rejects_evm_send")


class CalendarTests(unittest.TestCase):
    def test_dst_half_day_holiday_and_next_year(self):
        self.assertEqual(calendar.session_label(datetime(2026, 3, 9, 9, 30, tzinfo=NY)), "regular")
        self.assertEqual(calendar.session_label(datetime(2026, 11, 2, 9, 30, tzinfo=NY)), "regular")
        self.assertEqual(calendar.session_label(datetime(2026, 11, 27, 12, 59, tzinfo=NY)), "regular")
        self.assertEqual(calendar.session_label(datetime(2026, 11, 27, 13, 0, tzinfo=NY)), "after_hours")
        self.assertEqual(calendar.session_label(datetime(2026, 12, 24, 12, 59, tzinfo=NY)), "regular")
        self.assertEqual(calendar.session_label(datetime(2026, 1, 1, 12, 0, tzinfo=NY)), "closed")
        self.assertEqual(calendar.session_label(datetime(2026, 7, 3, 10, 0, tzinfo=NY)), "closed")
        self.assertEqual(calendar.session_label(datetime(2027, 1, 4, 10, 0, tzinfo=NY)), "calendar_unknown")

    def test_the_bar_moves_with_the_us_session(self):
        """Operator 2026-10-07: 20 bps while the US market is open, 4 bps closed.

        This replaces the earlier "closed is not looser" rule. Premarket and
        after-hours count as closed, and a date the calendar cannot place takes
        the stricter side because that is the answer we cannot check.
        """
        got = policy()
        self.assertTrue(got.session_split)
        self.assertEqual(got.required_edge_bps("regular"), Decimal("20"))
        for closed in ("closed", "premarket", "after_hours"):
            self.assertEqual(got.required_edge_bps(closed), Decimal("4"), closed)
        self.assertEqual(got.required_edge_bps("calendar_unknown"), Decimal("20"))
        # Turning the split off falls back to the single field, for rollback.
        from dataclasses import replace

        flat = replace(got, session_split=False, min_net_edge_bps=Decimal("20"))
        self.assertEqual(flat.required_edge_bps("closed"), Decimal("20"))


class CostTests(unittest.TestCase):
    def test_missing_cost_is_unknown(self):
        self.assertIsNone(costs.path_cost([Decimal("1"), None]))

    def test_both_gates_are_required(self):
        self.assertFalse(costs.net_clears(
            edge_bps=Decimal("20"), net_usdt=Decimal("0.05"),
            min_bps=Decimal("20"), min_usdt=Decimal("0.1")))
        self.assertFalse(costs.net_clears(
            edge_bps=Decimal("10"), net_usdt=Decimal("1"),
            min_bps=Decimal("20"), min_usdt=Decimal("0.1")))
        self.assertTrue(costs.net_clears(
            edge_bps=Decimal("20"), net_usdt=Decimal("0.1"),
            min_bps=Decimal("20"), min_usdt=Decimal("0.1")))


class CycleTests(unittest.TestCase):
    def test_unknown_keeps_the_lock_and_writes_halt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path, halt_path = root / "cycle.lock", root / "HALT"
            with cycle.CycleLock(lock_path, halt_path) as lock:
                cycle.finish_state(cycle.UNKNOWN, lock)
            self.assertTrue(lock_path.exists())
            self.assertTrue(halt_path.exists())
            ok, reason = cycle.open_gate(halt=halt_path, lock=lock_path, v1_busy=False)
            self.assertFalse(ok)
            self.assertEqual(reason, "halt")

    def test_clean_exit_removes_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path, halt_path = root / "cycle.lock", root / "HALT"
            with cycle.CycleLock(lock_path, halt_path) as lock:
                cycle.finish_state(cycle.SOLD, lock)
            self.assertFalse(lock_path.exists())
            self.assertFalse(halt_path.exists())

    def test_a_lock_is_not_a_halt(self):
        """A cycle in flight is minutes; a HALT file is a human.

        Measured 2026-10-07: a live SPYx run held the lock for a whole bridge
        crossing and the paper runner reported ``halt``, which reads as "someone
        has to reconcile this".
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path, halt_path = root / "cycle.lock", root / "HALT"
            lock_path.write_text("12345")
            ok, reason = cycle.open_gate(halt=halt_path, lock=lock_path,
                                         v1_busy=False)
            self.assertFalse(ok)
            self.assertEqual(reason, "lock")
            # The real stop file still wins, and still says what it is.
            halt_path.write_text("halt\n")
            self.assertEqual(
                cycle.open_gate(halt=halt_path, lock=lock_path, v1_busy=False)[1],
                "halt")

    def test_unknown_is_not_retried_and_approve_race_is(self):
        calls = []

        def send(attempt, mode):
            calls.append(attempt)
            return mode

        attempt = 0
        while True:
            attempt += 1
            mode = send(attempt, "unknown")
            if not cycle.send_again(broadcast_unknown=mode == "unknown",
                                     approve_race=False, attempts=attempt):
                break
        self.assertEqual(calls, [1])

        calls.clear()
        attempt = 0
        while True:
            attempt += 1
            mode = send(attempt, "approve_race" if attempt == 1 else "ok")
            if not cycle.send_again(broadcast_unknown=False,
                                     approve_race=mode == "approve_race",
                                     attempts=attempt):
                break
        self.assertEqual(calls, [1, 2])

    def test_v1_busy_does_not_broadcast(self):
        sent = []
        ok, reason = cycle.open_gate(
            halt=Path("/no/such/halt"), lock=Path("/no/such/lock"), v1_busy=True)
        if ok:
            sent.append("broadcast")
        self.assertFalse(ok)
        self.assertEqual(reason, "v1_live_busy")
        self.assertEqual(sent, [])

    def test_symbol_halt_is_not_global(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cycle.symbol_halt(root / "symbols", "AAPLx", "symbol_halt")
            self.assertTrue(cycle.symbol_halted(root / "symbols", "AAPLx"))
            self.assertFalse((root / "HALT").exists())


class EthLegTests(unittest.TestCase):
    def test_exact_approve_and_deposit_method(self):
        data = legs_eth.approve_calldata("0x" + "ab" * 20, 5)
        self.assertTrue(data.startswith("0x095ea7b3"))
        self.assertTrue(data.endswith(format(5, "064x")))
        self.assertNotIn("f" * 64, data)
        self.assertEqual(legs_eth.deposit_method("AAPLx"), "AAPLx - Ethereum")

    def test_small_balance_refuses_strcx_without_a_fake_fill(self):
        got = legs_eth.paper_decision(
            _row(min_notional="5.2"), policy(),
            balance=Decimal("0.24"), gas=Decimal("0.001"),
            requote_edge_bps=Decimal("30"))
        self.assertEqual(got["state"], "refused")
        self.assertEqual(got["reason"], "below_min_size")
        self.assertFalse(got["broadcast"])

    def test_unverified_coin_with_room_is_a_minimum_probe(self):
        got = legs_eth.paper_decision(
            _row(symbol="ABCDx", min_notional_usdt="0.10", verified=False), policy(),
            balance=Decimal("0.24"), gas=Decimal("0.001"),
            requote_edge_bps=Decimal("30"))
        self.assertEqual(got["state"], "paper")
        self.assertEqual(got["size_usdt"], Decimal("0.10"))
        self.assertTrue(got["probe_only"])
        self.assertFalse(got["broadcast"])

    def test_failed_requote_is_not_executable(self):
        # 3 bps is below even the loosest session bar (4 when closed).
        got = legs_eth.paper_decision(
            _row(), policy(), balance=Decimal("20"), gas=Decimal("0.001"),
            requote_edge_bps=Decimal("3"))
        self.assertEqual(got["reason"], "false_t0")
        self.assertFalse(got["broadcast"])

    def test_amber_and_missing_gas_are_refused(self):
        p = policy()
        self.assertEqual(
            legs_eth.paper_decision(_row(tier="amber"), p, balance=Decimal("20"),
                                    gas=Decimal("0.001"), requote_edge_bps=Decimal("30"))["reason"],
            "hook_not_green")
        self.assertEqual(
            legs_eth.paper_decision(_row(), p, balance=Decimal("20"),
                                    gas=None, requote_edge_bps=Decimal("30"))["reason"],
            "cost_unknown")


class ScoreboardTests(unittest.TestCase):
    def test_denominator_is_priced_rounds(self):
        self.assertEqual(scoreboard.opportunity_rate(priced_rounds=10, opportunity_rounds=2), 0.2)


class ScanTests(unittest.TestCase):
    def test_the_status_command_reports_the_policy_and_never_invents_a_broadcast(self):
        """The phase may move; the invariant is that only the three operator
        keys unlock a broadcast, and this CLI just reports them."""
        spec = importlib.util.spec_from_file_location("xstocks_v2_scan", SCAN_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        got = module.status()
        policy = V2Policy.load()
        self.assertEqual(got["phase"], policy.phase)
        self.assertEqual(got["auto_orders"], policy.auto_orders)
        self.assertEqual(got["broadcast"], policy.live_broadcast_allowed()[0])

    def test_paper_does_not_broadcast_even_with_the_phase_open(self):
        from dataclasses import replace

        opened = replace(V2Policy.load(), phase="paper_kraken", auto_orders="paper")
        allowed, reason = opened.live_broadcast_allowed()
        self.assertFalse(allowed)
        self.assertEqual(reason, "live 开关未开")

    def test_live_needs_all_three_keys(self):
        from dataclasses import replace

        base = V2Policy.load()
        armed = replace(base, phase="paper_kraken", mode="live", auto_orders="live",
                        live_ack="operator-test")
        self.assertEqual(armed.live_broadcast_allowed(), (True, "live 已授权"))
        self.assertFalse(replace(armed, live_ack="").live_broadcast_allowed()[0])
        self.assertFalse(replace(armed, auto_orders="paper").live_broadcast_allowed()[0])
        self.assertFalse(replace(armed, phase="scan").live_broadcast_allowed()[0])


def _row(**over) -> dict:
    base = {
        "symbol": "STRCx",
        "tier": "green",
        "direct_deposit": True,
        "deposit_credit_verified": True,
        "depth_usdt": "100",
        "min_notional_usdt": "1",
        "net_usdt": "0.4",
        "quote_asset": "USDC",
    }
    base.update(over)
    if "verified" in over:
        base["deposit_credit_verified"] = over["verified"]
    return base


if __name__ == "__main__":
    unittest.main()
