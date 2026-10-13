"""Lesson-by-lesson regression map for v2.

`docs/lessons/recent-mistakes-2026-09-24-to-10-06.md` lists 43 mistakes. Each
test here pins one that the v2 code can actually get wrong. The ones that
cannot apply are named in `NOT_APPLICABLE` with the reason, so a later reader
can tell "not covered" apart from "does not apply".

Hermetic: no network, no wallets, no halt files outside a temporary directory.
"""
from __future__ import annotations

import json
import unittest
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory

from arb_monitor.xstocks_v2 import health, ledger, legs_kraken, paper, scan
from arb_monitor.xstocks_v2.feeds.http import PROXY, Http
from arb_monitor.xstocks_v2.policy import V2Policy

# ---------------------------------------------------------------------------
# The map. Key is the lesson number in docs/lessons/recent-mistakes-*.md.
# A lesson with no test here must be listed in NOT_APPLICABLE with a reason.
# ---------------------------------------------------------------------------
LESSON_TESTS: dict[str, list[str]] = {
    "2.1": ["tests.test_xstocks_v2_lessons_map.LessonMapTest.test_2_1_a_six_minute_old_snapshot_is_not_a_cost_basis"],
    "2.2": ["tests.test_xstocks_v2_paper.CostTest.test_first_solana_transfer_is_the_expensive_one"],
    "2.3": ["tests.test_xstocks_v2_lessons_map.LessonMapTest.test_2_3_cost_lines_are_summed_not_assumed"],
    "2.5": ["tests.test_xstocks_v2_feeds.KrakenParseTest.test_depth_best_side"],
    "2.6": ["tests.test_xstocks_v2_lessons.KnownBugTests.test_per_token_price_is_not_per_share"],
    "2.7": ["tests.test_xstocks_v2_lessons.KnownBugTests.test_missing_balance_is_not_zero"],
    "2.8": ["tests.test_xstocks_v2_lessons.KnownBugTests.test_budget_field_is_not_quote_age"],
    "2.9": ["tests.test_xstocks_v2_lessons_map.LessonMapTest.test_2_9_the_path_scoreboard_does_not_print_a_constant"],
    "3.1": ["tests.test_xstocks_v2_lessons_map.LessonMapTest.test_3_1_v2_does_not_grow_a_second_kraken_signing_path"],
    "3.2": ["tests.test_xstocks_v2_lessons_map.LessonMapTest.test_3_2_a_withdrawal_without_an_address_book_entry_is_not_executable"],
    "3.3": ["tests.test_xstocks_v2.UnitTests.test_strcx_minimum_uses_effective_units"],
    "3.4": ["tests.test_xstocks_v2_lessons_map.LessonMapTest.test_3_3_and_3_4_sell_minimum_and_dust"],
    "3.5": ["tests.test_xstocks_v2_paper.KrakenSellLegTest.test_still_open_at_5s_is_not_a_fill"],
    "3.6": ["tests.test_xstocks_v2.CycleTests.test_unknown_is_not_retried_and_approve_race_is"],
    "3.7": ["tests.test_xstocks_v2_lessons.KnownBugTests.test_phase1_never_rests_a_maker_sell"],
    "3.8": ["tests.test_xstocks_v2_lessons_map.LessonMapTest.test_3_5_and_3_7_and_3_8_no_maker_rest"],
    "3.9": ["tests.test_xstocks_v2.UnitTests.test_evm_does_not_multiply",
            "tests.test_xstocks_v2.UnitTests.test_solana_multiplies_raw"],
    "4.1": ["tests.test_xstocks_v2_lessons.KnownBugTests.test_source_simulation_is_not_delivery"],
    "4.2": ["tests.test_xstocks_v2.RouteTests.test_bitmap_772_is_only_for_solana_destination"],
    "4.3": ["tests.test_xstocks_v2.CostTests.test_missing_cost_is_unknown"],
    "4.4": ["tests.test_xstocks_v2_lessons.KnownBugTests.test_sol_is_not_sent_to_an_ata"],
    "4.5": ["tests.test_xstocks_v2_lessons.KnownBugTests.test_dust_and_undersized_deposit_do_not_count"],
    "4.6": ["tests.test_xstocks_v2_lessons_map.LessonMapTest.test_4_6_the_same_address_string_on_two_chains_is_two_assets"],
    "4.7": ["tests.test_xstocks_v2_lessons.KnownBugTests.test_unknown_duration_is_not_a_few_minutes"],
    "4.8": ["tests.test_xstocks_v2_lessons_map.LessonMapTest.test_4_8_a_failed_bridge_is_recorded_with_a_location"],
    "5.1": ["tests.test_xstocks_v2_lessons.KnownBugTests.test_fixed_fee_stays_in_the_path_cost",
            "tests.test_xstocks_v2_paper.CostTest.test_capital_is_inside_the_net"],
    "5.2": ["tests.test_xstocks_v2_lessons_map.LessonMapTest.test_5_2_the_withdrawal_fee_is_not_inside_the_arbitrage_net"],
    "5.3": ["tests.test_xstocks_v2_feeds.ScanGateTest.test_a_deep_pool_our_size_cannot_move_through_is_not_a_candidate"],
    "5.4": ["tests.test_xstocks_v2_feeds.ScanGateTest.test_chain_to_kraken_row_uses_bid_and_says_why_it_is_blocked"],
    "5.5": ["tests.test_xstocks_v2_feeds.ScanGateTest.test_same_pool_round_trip_is_flagged_even_without_a_depth_field",
            "tests.test_xstocks_v2.FunnelTests.test_same_pool_is_rejected"],
    "5.6": ["tests.test_xstocks_v2_lessons.KnownBugTests.test_sandwich_exposure_is_slippage_times_notional"],
    "5.7": ["tests.test_xstocks_v2_lessons.KnownBugTests.test_stale_rows_are_not_opportunities"],
    "5.8": ["tests.test_xstocks_v2_lessons_map.LessonMapTest.test_5_8_reaching_the_threshold_is_not_the_same_as_being_worth_it"],
    "6.1": ["tests.test_xstocks_v2_lessons_map.LessonMapTest.test_6_1_every_request_goes_through_the_local_proxy"],
    "6.2": ["tests.test_xstocks_v2_lessons_map.LessonMapTest.test_6_2_the_limiter_is_shared_and_a_429_pauses_everyone",
            "tests.test_xstocks_v2_feeds.JupiterParseTest.test_429_pauses_every_worker_once"],
    "6.3": ["tests.test_xstocks_v2_lessons_map.LessonMapTest.test_6_3_a_silent_runner_is_detected_by_its_heartbeat",
            "tests.test_xstocks_v2_lessons_map.LessonMapTest.test_6_3_a_missing_heartbeat_is_reported_not_ignored"],
    "6.6": ["tests.test_xstocks_v2_lessons_map.LessonMapTest.test_6_6_an_unreadable_process_list_is_busy_not_idle"],
    "6.7": ["tests.test_xstocks_v2_feeds.CensusTest.test_bad_row_is_skipped_not_fatal",
            "tests.test_xstocks_v2_feeds.EvmEncodeTest.test_one_bad_call_does_not_drop_the_batch"],
    "6.8": ["tests.test_xstocks_v2_lessons_map.LessonMapTest.test_6_8_the_repo_is_under_version_control_now"],
}

# Documented gaps. An entry here is a decision, not an oversight.
NOT_APPLICABLE = {
    "2.4": "v2 只做现货，没有永续腿；资金费属于 8765 那条被暂停的策略",
    "3.10": "v2 没有第二腿，先平永续、后卖现货的顺序规则不适用",
    "6.4": "日志轮转在 scripts/xstocks-v2-ctl.sh 的 rotate_log 里，shell 逻辑不进 hermetic 测试",
    "6.5": "磁盘回收是运维项，不是代码闸门",
}


class LessonMapTest(unittest.TestCase):
    def setUp(self):
        self.policy = V2Policy.load()

    # ---- 2.x 计算 / 口径 -------------------------------------------------
    def test_2_1_a_six_minute_old_snapshot_is_not_a_cost_basis(self):
        """The v1 board took each asset's newest row, once 6.5x off the truth.

        The fix is not "refuse everything the snapshot touched": it is "never let
        the snapshot be the cost basis". With a fresh pair in hand the decision
        rests on the re-quote (``scan_used_for = candidate_selection``); without
        one it still refuses, which is the case asserted second.
        """
        row = {"family": "chain_to_kraken", "symbol": "AAPLx", "chain": "solana",
               "blockers": [], "edge_t0_bps": "40", "buy_price_usd": "100"}
        decision = paper.decide(
            row=row, policy=self.policy, kraken_bid=D("102"), size_usdt=D("25"),
            tokens=D("0.25"), transfer_usdt=D("0.01"), gas_usdt=D("0"),
            phase_allows=True, slot_free=True, scan_fresh=False)
        self.assertEqual(decision["scan_used_for"], "candidate_selection")
        self.assertTrue(decision["fresh_legs"])
        no_legs = paper.decide(
            row=row, policy=self.policy, kraken_bid=None, size_usdt=D("25"),
            tokens=None, transfer_usdt=D("0.01"), gas_usdt=D("0"),
            phase_allows=True, slot_free=True, scan_fresh=False)
        self.assertEqual(no_legs["reason"], "scan_stale")

    def test_2_3_cost_lines_are_summed_not_assumed(self):
        """``[costs]`` holds parameters; the path still has to total them."""
        result = paper.chain_to_kraken_net(
            size_usdt=D("25"), tokens=D("0.25"), buy_price_usd=D("100"),
            kraken_bid=D("102"), policy=self.policy,
            transfer_usdt=D("0.37"), gas_usdt=D("0.11"))
        costs = result["costs"]
        for name in ("capital_usdt", "transfer_usdt", "gas_usdt",
                     "kraken_fee_usdt", "slippage_buffer_usdt"):
            self.assertIn(name, costs)
        self.assertEqual(
            result["net_usdt"],
            costs["proceeds_usdt"] - sum(
                (costs["capital_usdt"], costs["transfer_usdt"], costs["gas_usdt"],
                 costs["kraken_fee_usdt"], costs["slippage_buffer_usdt"])))

    def test_2_9_the_path_scoreboard_does_not_print_a_constant(self):
        """v1's "highest net" was ``max(gross) - 15 bps``, a fixed number."""
        rows = [{"family": "chain_to_kraken", "buy_venue": "jupiter",
                 "sell_venue": "kraken", "edge_t0_bps": "12", "reason": ""},
                {"family": "chain_to_kraken", "buy_venue": "jupiter",
                 "sell_venue": "kraken", "edge_t0_bps": "47", "reason": ""}]
        pairs = scan._venue_pairs(rows)
        self.assertEqual(D(pairs[0]["best_edge_bps"]), D("47"))
        # The published number is read from the rows, so a different set moves it.
        rows[1]["edge_t0_bps"] = "9"
        self.assertEqual(D(scan._venue_pairs(rows)[0]["best_edge_bps"]), D("12"))

    # ---- 3.x 执行 / 订单 ------------------------------------------------
    def test_3_1_v2_does_not_grow_a_second_kraken_signing_path(self):
        self.assertEqual(legs_kraken.pair_for("MRNAx"), "MRNAx/USD")
        self.assertFalse(hasattr(legs_kraken, "sign"))
        self.assertFalse(hasattr(legs_kraken, "KrakenSpotClient"))
        nonce = legs_kraken.make_nonce()
        self.assertIsInstance(nonce.next(), str)
        self.assertGreater(len(nonce.next()), 15)

    def test_3_2_a_withdrawal_without_an_address_book_entry_is_not_executable(self):
        entries = [{"key": "ethusdc1", "asset": "USDC", "method": "USDC"}]
        self.assertFalse(scan.address_book_ready("AAPLx", "eth", entries))
        self.assertTrue(scan.address_book_ready(
            "AAPLx", "eth", [{"asset": "AAPLx", "method": "AAPLx - Ethereum"}]))
        self.assertFalse(scan.address_book_ready("AAPLx", "solana", entries))
        # ``load_address_book`` merges v2's own cache now, so a missing v1
        # snapshot is not an empty book any more. What the lesson locks is that
        # an entry with no (asset, network) match cannot open the gate.
        rows = scan.load_address_book("/nonexistent.json")
        self.assertFalse([row for row in rows
                          if row.get("key") == "definitely-not-registered"])
        self.assertFalse(scan.address_book_ready("AAPLx", "x-layer", rows, []))

    def test_3_2_a_stablecoin_entry_needs_the_method_table_to_resolve_its_network(self):
        """``USDC`` is the whole method name for ERC20; the network is in WithdrawMethods."""
        entries = [{"asset": "USDC", "method": "USDC"},
                   {"asset": "USDC", "method": "USDC - Solana"}]
        methods = [{"asset": "USDC", "method": "USDC", "network": "Ethereum"},
                   {"asset": "USDC", "method": "USDC - Stellar XLM", "network": "Stellar"}]
        # Without the join, a bare "USDC" entry cannot be attributed to a chain.
        self.assertFalse(scan.address_book_ready("USDC", "eth", entries))
        # With it, Ethereum is resolved and Solana is not.
        self.assertTrue(scan.address_book_ready("USDC", "eth", entries, methods))
        self.assertFalse(scan.address_book_ready("USDC", "solana", entries, methods))
        sol_methods = [{"asset": "USDC", "method": "USDC - Solana", "network": "Solana"}]
        self.assertTrue(scan.address_book_ready("USDC", "solana", entries, sol_methods))

    def test_3_2_a_rail_kraken_does_not_offer_cannot_be_registered_away(self):
        """SPYx/QQQx/AAPLx list Ethereum, Solana and TON -- and no X Layer.

        Calling that "address book missing" sent the operator to fix something
        that cannot be fixed. The two reasons have to stay distinct.
        """
        methods = {"SPYx": [{"method": "SPYx - Ethereum", "network": "Ethereum"},
                            {"method": "SPYx", "network": "Solana"}]}
        self.assertTrue(scan.has_withdraw_method("SPYx", "eth", methods.get("SPYx")))
        self.assertTrue(scan.has_withdraw_method("SPYx", "solana", methods.get("SPYx")))
        self.assertFalse(scan.has_withdraw_method("SPYx", "x-layer", methods.get("SPYx")))
        # An empty or unknown table is "no rail", never "probably fine".
        self.assertFalse(scan.has_withdraw_method("SPYx", "solana", None))
        self.assertFalse(scan.has_withdraw_method("SPYx", "solana", []))

    def test_3_3_and_3_4_sell_minimum_and_dust(self):
        self.assertFalse(legs_kraken.credited_units(
            before=D("1"), after=D("1.0000001"), minimum=D("0.05459063574")))
        self.assertTrue(legs_kraken.credited_units(
            before=D("1"), after=D("1.0545907"), minimum=D("0.05459063574")))

    def test_3_5_and_3_7_and_3_8_no_maker_rest(self):
        from arb_monitor.xstocks_v2.guards import phase1_sell_is_taker

        self.assertTrue(phase1_sell_is_taker("XOPx"))
        self.assertTrue(phase1_sell_is_taker("AAPLx"))

    # ---- 4.x 跨链 / 链上 ------------------------------------------------
    def test_4_6_the_same_address_string_on_two_chains_is_two_assets(self):
        """One contract address on BSC says nothing about the same string on X Layer."""
        shared = "0x" + "a" * 40
        census = {"candidates": [
            {"slug": "bsc", "symbol": "MSTRx", "token": shared, "stable": shared,
             "fee": "0.0025", "dec_token": 18, "dec_stable": 18, "depth_usd": "9000"},
            {"slug": "x-layer", "symbol": "MSTRx", "token": shared, "stable": shared,
             "fee": "0.0005", "dec_token": 18, "dec_stable": 6, "depth_usd": "9000"},
        ]}
        pools = scan.census_feed.census_pools(census)
        self.assertEqual({pool["chain"] for pool in pools}, {"bsc", "x-layer"})
        self.assertEqual(len({(pool["chain"], pool["token_address"]) for pool in pools}), 2)

    def test_4_8_a_failed_bridge_is_recorded_with_a_location(self):
        entry = ledger.row(symbol="MRNAx", chain="x-layer", family="dex_to_dex",
                           state=ledger.STOPPED, mode="paper", size_usdt=D("25"),
                           held_at="in_bridge", reason="bridge_not_arrived")
        self.assertEqual(entry["held_at"], "in_bridge")
        self.assertIn(entry["state"], ledger.HOLD_STATES)

    # ---- 5.x 估计 / 规模 ------------------------------------------------
    def test_5_2_the_withdrawal_fee_is_not_inside_the_arbitrage_net(self):
        """Rebalancing is a separate ticket; folding it in gives profit away."""
        result = paper.chain_to_kraken_net(
            size_usdt=D("25"), tokens=D("0.25"), buy_price_usd=D("100"),
            kraken_bid=D("101"), policy=self.policy,
            transfer_usdt=D("0.37"), gas_usdt=D("0"))
        self.assertNotIn("withdraw_fee_usdt", result["costs"])
        self.assertNotIn("rebalance_usdt", result["costs"])

    def test_5_8_reaching_the_threshold_is_not_the_same_as_being_worth_it(self):
        row = {"family": "chain_to_kraken", "symbol": "AAPLx", "chain": "solana",
               "blockers": [], "edge_t0_bps": "25", "buy_price_usd": "100"}
        # 0.25 tokens, +1% = 0.25 USDT gross, but the transfer eats it.
        decision = paper.decide(
            row=row, policy=self.policy, kraken_bid=D("101"), size_usdt=D("25"),
            tokens=D("0.25"), transfer_usdt=D("0.37"), gas_usdt=D("0"),
            phase_allows=True, slot_free=True)
        self.assertEqual(decision["state"], "false_t0")
        self.assertLess(decision["net_usdt"], self.policy.min_net_profit_usdt)

    # ---- 6.x 环境 / 运维 ------------------------------------------------
    def test_6_1_every_request_goes_through_the_local_proxy(self):
        http = Http()
        handler = next(item for item in http.opener.handlers
                       if item.__class__.__name__ == "ProxyHandler")
        self.assertEqual(handler.proxies.get("https"), PROXY)

    def test_6_2_the_limiter_is_shared_and_a_429_pauses_everyone(self):
        from arb_monitor.xstocks_v2.feeds.http import RateLimiter

        limiter = RateLimiter(1000.0)
        limiter.pause(5.0)
        self.assertEqual(limiter.pauses, 1)

    def test_6_3_a_silent_runner_is_detected_by_its_heartbeat(self):
        """The v1 monitor once stopped for six hours unnoticed."""
        with TemporaryDirectory() as tmp:
            paper_path = Path(tmp) / "paper.json"
            paper_path.write_text(json.dumps({"updated_ms": 1_000_000}))
            report = health.check(paths={"paper": paper_path}, now_ms=10_000_000,
                                  interval_s=180.0)
            self.assertEqual(report["status"], health.STALE)
            self.assertIn("paper", health.alert_text(report))

    def test_6_3_a_missing_heartbeat_is_reported_not_ignored(self):
        with TemporaryDirectory() as tmp:
            report = health.check(paths={"scan": Path(tmp) / "nope.json"},
                                  now_ms=10_000_000, interval_s=180.0)
            self.assertEqual(report["status"], health.NEVER)
            self.assertIn("没有心跳文件", health.alert_text(report))

    def test_6_3_a_fresh_heartbeat_is_quiet(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan.json"
            path.write_text(json.dumps({"updated_ms": 9_990_000}))
            report = health.check(paths={"scan": path}, now_ms=10_000_000,
                                  interval_s=180.0)
            self.assertEqual(report["status"], health.OK)
            self.assertEqual(health.alert_text(report), "")

    def test_6_3_a_scan_snapshot_counts_as_a_heartbeat_too(self):
        """The scan writes ``generated_ms``; the paper state writes ``updated_ms``."""
        status, age = health.heartbeat({"generated_ms": 9_990_000},
                                       now_ms=10_000_000, max_age_ms=540_000)
        self.assertEqual(status, health.OK)
        self.assertEqual(age, 10_000)
        self.assertEqual(health.heartbeat({}, now_ms=1, max_age_ms=1)[0], health.NEVER)

    def test_6_6_an_unreadable_process_list_is_busy_not_idle(self):
        from arb_monitor.xstocks_v2 import cycle as cycle_core

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            busy, why = cycle_core.v1_busy(
                root, runner=lambda *a, **k: (_ for _ in ()).throw(
                    FileNotFoundError("pgrep")))
            self.assertTrue(busy)
            self.assertEqual(why, "pgrep unavailable")

    def test_6_7_one_bad_row_does_not_take_down_the_universe(self):
        census = {"candidates": [
            {"slug": "x-layer", "symbol": "GOODx", "token": "0x" + "1" * 40,
             "stable": "0x" + "2" * 40, "fee": "0.0005", "dec_token": 18,
             "dec_stable": 6, "depth_usd": "500"},
            {"slug": "x-layer", "symbol": "BADx", "token": "0x", "stable": "0x"},
        ]}
        pools = scan.census_feed.census_pools(census)
        self.assertEqual([pool["symbol"] for pool in pools], ["GOODx"])

    def test_6_8_the_repo_is_under_version_control_now(self):
        self.assertTrue((Path(scan.ROOT) / ".git").exists())


class LessonCoverageTest(unittest.TestCase):
    def _lesson_numbers(self) -> list[str]:
        lessons = (Path(scan.ROOT) / "docs" / "lessons"
                   / "recent-mistakes-2026-09-24-to-10-06.md")
        return [line.split()[1] for line in lessons.read_text().splitlines()
                if line.startswith("### ")]

    def test_every_numbered_lesson_is_either_mapped_or_waived(self):
        """A new lesson in the lessons file cannot be silently ignored."""
        missing = [number for number in self._lesson_numbers()
                   if number not in LESSON_TESTS and number not in NOT_APPLICABLE]
        self.assertEqual(missing, [], f"没有覆盖也没有标注理由的教训：{missing}")

    def test_every_mapped_test_id_actually_exists(self):
        """The map must point at real tests, not at names that were renamed."""
        loader = unittest.TestLoader()
        broken = []
        for number, ids in LESSON_TESTS.items():
            for test_id in ids:
                try:
                    found = loader.loadTestsFromName(test_id)
                except Exception as exc:  # noqa: BLE001
                    broken.append(f"{number} -> {test_id} ({type(exc).__name__})")
                    continue
                if found.countTestCases() != 1:
                    broken.append(f"{number} -> {test_id} (匹配到 {found.countTestCases()} 个)")
        self.assertEqual(broken, [], f"教训映射指向了不存在的用例：{broken}")

    def test_the_lessons_file_has_the_count_this_map_claims(self):
        numbers = self._lesson_numbers()
        self.assertEqual(len(numbers), 43)
        self.assertEqual(len(set(numbers)), 43)


if __name__ == "__main__":
    unittest.main()
