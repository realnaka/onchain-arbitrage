"""The per-venue opportunity tally, tested on the ways it could lie.

Operator's request, 2026-09-16: "我要统计哪个交易所出现的套利机会最多，然后就先接
那个交易所". A statistic that decides which venue gets built first has to survive its
own failure modes, so each test below pins one:

1. **Ranking must not reward monitoring age.** Bybit and Bitget joined at 21:03 on
   the day they were added; Gate has rows back to 09-13. Counting rows ranks the
   longest-observed venue first, which is a statement about the observer.
2. **"Priced" is not "opportunity".** A row where both legs were quoted but the
   basis is negative is data, not a signal.
3. **"No data" is not "no opportunity".** A venue that could not be priced at all
   must read as unevaluable, with the reasons attached, never as 0%.
4. **The session split is load-bearing.** Every Gate/Bybit/Bitget opportunity
   recorded in their first hours was `after_close`, where KUAISHOU sat at +90 to
   +150 bps for hours with the HK cash market shut. Ranking those against OKX's
   in-session opportunities would compare two different regimes.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from arb_monitor.ops.venue_scoreboard import (build_scoreboard, collect,
                                              executable_from_policy,
                                              executable_pairs, format_report,
                                              load_rows, phase_group,
                                              scoreboard_html)

DAY_MS = 86_400_000


def row(run_id, venue, asset, gross, *, phase="continuous_am",
        instrument=None, rejections=(), ts=1_700_000_000_000,
        cost=None, spot_open=None, profit=None, quantity=None,
        evaluated_at_ms=..., legacy=False):
    """One recorded `basis_evaluation` row, as `load_rows` would hand it back.

    The four money fields are optional on purpose: a row that never reached the
    cost model has none of them, and that is a case the tally has to handle.

    `evaluated_at_ms` defaults to a moment of this row's own, because that is
    what the streaming evaluator writes and the tally only counts rows that have
    it. `legacy=True` writes a row from the older wait-for-the-whole-cycle
    regime (no `evaluated_at_ms`), which the tally excludes.
    """
    if legacy:
        evaluated_at_ms = None
    elif evaluated_at_ms is ...:
        evaluated_at_ms = ts + 1000
    return {
        "run_id": run_id,
        "recv_ts_ms": ts,
        "evaluated_at_ms": evaluated_at_ms,
        "asset": asset,
        "venue": venue,
        "instrument": instrument or f"{venue}:{asset}USDT",
        "gross_bps": None if gross is None else Decimal(str(gross)),
        "rejections": list(rejections),
        "phase": phase,
        "cost_quote": None if cost is None else Decimal(str(cost)),
        "spot_open_quote": None if spot_open is None else Decimal(str(spot_open)),
        "profit_quote": None if profit is None else Decimal(str(profit)),
        "spot_quantity": None if quantity is None else Decimal(str(quantity)),
        "contracts": None,
        "status": "watch_only",
    }


class PhaseGroupTests(unittest.TestCase):
    def test_session_and_off_hours_are_separated(self):
        for phase in ("continuous_am", "continuous_pm", "lunch",
                      "closing_auction"):
            self.assertEqual(phase_group(phase), "session")
        for phase in ("preopen_0900_0930", "before_0900", "after_close"):
            self.assertEqual(phase_group(phase), "off_hours")
        # Weekends and holidays. Both legs of the trade quote 24/7, so this
        # bucket is reported next to the other two (AGENTS.md 全天候原则).
        self.assertEqual(phase_group("closed"), "other")
        self.assertEqual(phase_group("who_knows"), "other")
        self.assertEqual(phase_group(None), "other")


class ClassificationTests(unittest.TestCase):
    def test_a_stale_priced_row_is_counted_but_never_as_an_opportunity(self):
        """A row can carry a basis and still be one the monitor refused to trust.

        Measured 2026-09-17: a cycle had 39 of 39 rows flagged
        `missing_or_stale_*` while still producing `entry_basis_bps`. Counting
        those as opportunities would answer "where is the biggest number" with
        prices the monitor itself will not act on.
        """
        board = build_scoreboard([
            row("r1", "gate", "SHEIN", 120, rejections=["missing_or_stale_spot_buy_quote"]),
            row("r1", "gate", "SHEIN", 120, rejections=["non_simultaneous_quotes"]),
            row("r1", "gate", "SHEIN", 120),  # clean
        ])
        entry = board["venues"]["gate"]
        self.assertEqual(entry["evaluable_rows"], 1)
        self.assertEqual(entry["opportunity_rows"], 1)
        self.assertEqual(entry["stale_rows"], 2)
        self.assertEqual(str(entry["stale_rate"]), "66.7")

    def test_economics_flags_do_not_disqualify_a_row(self):
        # "below_min_notional" and "outside_strategy_window" are about economics
        # and session policy, not about whether the price can be trusted.
        board = build_scoreboard([
            row("r1", "gate", "SHEIN", 120,
                rejections=["outside_strategy_window", "below_min_notional",
                            "conditional_profit_below_threshold"]),
        ])
        entry = board["venues"]["gate"]
        self.assertEqual(entry["opportunity_rows"], 1)
        self.assertEqual(entry["stale_rows"], 0)

    def test_opportunity_and_candidate_are_different_bars(self):
        board = build_scoreboard([
            row("r1", "okx", "SHEIN", 30),    # >= 20 gross, < 35 net
            row("r1", "okx", "SHEIN", 40),    # >= 35 net -> candidate
            row("r1", "okx", "SHEIN", "19.9"),  # below both bars
            row("r1", "okx", "SHEIN", -500),  # wrong direction entirely
        ])
        entry = board["venues"]["okx"]
        self.assertEqual(entry["evaluable_rows"], 4)
        self.assertEqual(entry["opportunity_rows"], 2)
        self.assertEqual(entry["candidate_rows"], 1)

    def test_negative_basis_is_never_an_opportunity(self):
        board = build_scoreboard([row("r1", "okx", "XIAOMI", -120)])
        entry = board["venues"]["okx"]
        self.assertEqual(entry["evaluable_rows"], 1)
        self.assertEqual(entry["opportunity_rows"], 0)
        self.assertEqual(entry["opportunity_rate"], Decimal("0.0"))

    def test_a_pair_that_could_not_be_priced_is_not_a_zero_opportunity(self):
        board = build_scoreboard([
            row("r1", "binance", "KUAISHOU", None,
                rejections=["quanto_or_unknown_payout_requires_separate_hedge_model",
                            "unverified_or_expired_share_ratio_fx_mapping"]),
        ])
        entry = board["venues"]["binance"]
        self.assertEqual(entry["evaluable_rows"], 0)
        # No denominator at all, rather than a 0% that reads as "measured, none".
        self.assertIsNone(entry["opportunity_rate"])
        self.assertIsNone(entry["candidate_rate"])
        self.assertEqual(dict(entry["blocked"])[
            "quanto_or_unknown_payout_requires_separate_hedge_model"], 1)

    def test_reasons_are_attributed_to_the_asset(self):
        board = build_scoreboard([
            row("r1", "gate", "XIAOMI", None,
                rejections=["missing_or_stale_perp_book"]),
        ])
        self.assertEqual(
            dict(board["venues"]["gate"]["pairs_blocked"]),
            {"XIAOMI:missing_or_stale_perp_book": 1})


class RankingTests(unittest.TestCase):
    def test_ranking_ignores_how_long_a_venue_was_watched(self):
        """Gate has 10x the rows; Bitget has the opportunities.

        This is the deliberate fix for the raw-count ranking: rows scale with
        observation time, so a venue monitored all week beats one added an hour
        ago without a single real difference in the market.
        """
        rows = []
        for index in range(20):
            rows.append(row(f"g{index}", "gate", "XIAOMI", -5,
                            ts=1_700_000_000_000 - index))
        for index in range(2):
            rows.append(row(f"b{index}", "bitget", "KUAISHOU", 120))
        board = build_scoreboard(rows)
        self.assertEqual(board["ranking"][0], "bitget")
        self.assertGreater(board["venues"]["gate"]["rows"],
                           board["venues"]["bitget"]["rows"])

    def test_the_cash_session_is_not_privileged_in_the_ranking(self):
        """Weekend rows are candidates, not leftovers.

        The trade is a token against a perp; both quote around the clock, so the
        cash session does not decide which regime a row belongs to. Ranking on
        the session alone used to bury venues whose edges live in the
        pre-open / after-close / weekend windows (AGENTS.md 全天候原则,
        operator decision 2026-09-20).
        """
        board = build_scoreboard([
            row("s1", "okx", "SHEIN", 60, phase="continuous_pm"),
            row("c1", "bybit", "KUAISHOU", 150, phase="after_close"),
            row("c2", "bybit", "KUAISHOU", 150, phase="after_close"),
            row("c3", "bybit", "KUAISHOU", 150, phase="after_close"),
            row("w1", "gate", "TENCENT", 150, phase="closed"),
        ])
        self.assertEqual(board["ranking"][0], "bybit")
        self.assertEqual(
            board["venues"]["bybit"]["phases"]["session"]["candidate_rows"], 0)
        self.assertEqual(
            board["venues"]["bybit"]["phases"]["off_hours"]["candidate_rows"], 3)
        # A weekend row counts in the venue-level rate, not only in its bucket.
        self.assertEqual(board["venues"]["gate"]["phases"]["other"]["candidate_rows"], 1)
        self.assertEqual(str(board["venues"]["gate"]["candidate_rate"]), "100.0")

    def test_a_venue_is_charged_only_for_its_own_evaluations(self):
        # No shared-cycle intersection any more: a venue absent from a cycle
        # simply has no row there, and its denominator is its own rows. This is
        # what replaced the `common_*` figures when the unit became the
        # evaluation instead of the cycle.
        rows = [
            row("shared", "gate", "KUAISHOU", 120),
            row("shared", "bybit", "KUAISHOU", 120),
            row("gate-only", "gate", "KUAISHOU", 120),
        ]
        board = build_scoreboard(rows)
        self.assertEqual(board["window"]["cycles"], 2)
        self.assertEqual(board["venues"]["gate"]["candidate_rows"], 2)
        self.assertEqual(board["venues"]["bybit"]["candidate_rows"], 1)
        self.assertEqual(str(board["venues"]["gate"]["candidate_rate"]), "100.0")

    def test_the_ranking_leads_with_the_rate_not_the_listing_breadth(self):
        # A venue with six assets takes six times as many evaluations as one
        # with a single asset. With the same per-asset behaviour it would win a
        # count-based ranking; the rate keeps the comparison about the market.
        rows = [row("wide", "gate", asset, 120)
                for asset in ("XIAOMI", "TENCENT", "POPMART", "MEITUAN",
                              "KUAISHOU", "SHEIN")]
        rows += [row("narrow", "bitget", "KUAISHOU", 120)]
        rows += [row(f"miss{i}", "gate", "TENCENT", -100) for i in range(6)]
        board = build_scoreboard(rows)
        self.assertEqual(board["venues"]["gate"]["candidate_rows"], 6)
        self.assertEqual(board["venues"]["bitget"]["candidate_rows"], 1)
        self.assertEqual(board["ranking"][0], "bitget")


class SizeGateTests(unittest.TestCase):
    """A bps edge is not a trade.

    With `min_net_profit_quote = 1` USDT and a 10 USDT per-trade cap, an 180 bps
    dislocation on a 10 USDT position is worth 0.18 USDT. The tally has to say so,
    otherwise it ranks venues by a number nothing can act on.
    """

    def test_required_notional_is_derived_from_the_net_edge(self):
        # gross 180 - 15 cost = 165 bps net; 1 USDT / 0.0165 = ~60.6 USDT.
        board = build_scoreboard([row("r1", "gate", "KUAISHOU", 180)])
        entry = board["venues"]["gate"]
        self.assertEqual(entry["required_notional_usd_median"], "60.6")
        self.assertEqual(entry["opportunity_rows_affordable"], 0)

    def test_a_huge_edge_inside_the_cap_is_counted_as_affordable(self):
        # gross 3000 - 15 = 2985 bps net -> 1 USDT needs 3.35 USDT, inside the cap.
        board = build_scoreboard([row("r1", "gate", "KUAISHOU", 3000)])
        entry = board["venues"]["gate"]
        self.assertEqual(entry["opportunity_rows_affordable"], 1)
        self.assertEqual(entry["required_notional_usd_median"], "3.4")

    def test_a_negative_net_edge_needs_infinite_size_and_is_skipped(self):
        board = build_scoreboard(
            [row("r1", "gate", "KUAISHOU", 10)],  # net -5 bps
            min_gross_bps=Decimal("0"))
        entry = board["venues"]["gate"]
        self.assertIsNone(entry["required_notional_usd_median"])
        self.assertEqual(entry["opportunity_rows_affordable"], 0)

    def test_the_cap_and_the_profit_bar_are_parameters_not_constants(self):
        board = build_scoreboard([row("r1", "gate", "KUAISHOU", 180)],
                                 trade_quote_usdt=Decimal("100"))
        self.assertEqual(board["venues"]["gate"]["opportunity_rows_affordable"], 1)
        self.assertEqual(board["thresholds"]["trade_quote_usdt"], "100")

    def test_report_states_the_size_bar(self):
        text = format_report(build_scoreboard([row("r1", "gate", "KUAISHOU", 180)]))
        self.assertIn("最小利润", text)
        self.assertIn("够本金", text)


class RegimeTests(unittest.TestCase):
    """Only rows judged without waiting for the whole cycle may be counted.

    Operator, 2026-09-17 evening: "这些数据很有可能都是错的，因为是按之前有大延迟的
    价格按组统计的……按新的不用等所有价格返回就判断是否机会成立的方式来统计".

    Measured that day: every `basis_evaluation` row written up to 16:16:51 HKT
    (9,117 rows, 299 cycles) has no `evaluated_at_ms` — the old evaluator only
    ran once all probes of the cycle had returned, so a row's price could be
    twenty seconds old and nothing recorded said so. From 16:19:46 each row
    carries its own judgement moment and the timestamps within a cycle spread
    over 4–13 seconds.
    """

    def test_rows_from_the_wait_for_everything_regime_are_excluded(self):
        board = build_scoreboard([
            row("streamed", "gate", "KUAISHOU", 120),
            row("legacy", "gate", "KUAISHOU", 120, legacy=True),
        ])
        self.assertEqual(board["venues"]["gate"]["rows"], 1)
        self.assertEqual(board["venues"]["gate"]["candidate_rows"], 1)
        # Excluded, and counted. A silent drop would read as "we looked and
        # there was nothing", which is the opposite of what happened.
        self.assertEqual(board["venues"]["gate"]["legacy_rows"], 1)
        self.assertEqual(board["window"]["legacy_rows"], 1)
        self.assertEqual(board["window"]["legacy_cycles"], 1)

    def test_include_legacy_puts_them_back_for_an_audit(self):
        rows = [
            row("streamed", "gate", "KUAISHOU", 120),
            row("legacy", "gate", "KUAISHOU", 120, legacy=True),
        ]
        board = build_scoreboard(rows, include_legacy=True)
        self.assertEqual(board["venues"]["gate"]["rows"], 2)
        self.assertEqual(board["venues"]["gate"]["candidate_rows"], 2)
        self.assertEqual(board["venues"]["gate"]["legacy_rows"], 0)
        self.assertTrue(board["window"]["include_legacy"])

    def test_the_report_says_out_loud_how_much_it_excluded(self):
        board = build_scoreboard([
            row("streamed", "gate", "KUAISHOU", 120),
            row("legacy", "gate", "KUAISHOU", 120, legacy=True),
        ])
        text = format_report(board)
        self.assertIn("已排除旧口径", text)
        self.assertIn("--include-legacy", text)
        self.assertIn("--include-legacy", scoreboard_html(board))
        # Nothing to announce when nothing was excluded.
        clean = build_scoreboard([row("s", "gate", "KUAISHOU", 120)])
        self.assertNotIn("已排除旧口径", format_report(clean))

    def test_each_evaluation_is_its_own_observation_not_a_cycle_slot(self):
        # Two assets of the same venue, judged at different moments inside one
        # cycle: two observations, not one. The old unit would have called this
        # a single cycle and charged the venue once.
        ts = 1_700_000_000_000
        board = build_scoreboard([
            row("c1", "gate", "XIAOMI", 120, ts=ts),
            row("c1", "gate", "SHEIN", -50, ts=ts),
            row("c1", "gate", "KUAISHOU", 120, ts=ts),
        ])
        entry = board["venues"]["gate"]
        self.assertEqual(board["window"]["cycles"], 1)
        self.assertEqual(entry["cycles"], 1)
        self.assertEqual(entry["evaluable_rows"], 3)
        self.assertEqual(entry["candidate_rows"], 2)
        self.assertEqual(str(entry["candidate_rate"]), "66.7")

    def test_a_legacy_only_window_reports_zero_rather_than_the_old_numbers(self):
        board = build_scoreboard([
            row("old", "gate", "KUAISHOU", 300, legacy=True),
            row("old", "bybit", "KUAISHOU", 300, legacy=True),
        ])
        self.assertEqual(board["window"]["cycles"], 0)
        self.assertEqual(board["window"]["legacy_rows"], 2)
        for venue in ("gate", "bybit"):
            self.assertEqual(board["venues"][venue]["candidate_rows"], 0)
            self.assertIsNone(board["venues"][venue]["candidate_rate"])


class RecordedCostTests(unittest.TestCase):
    """The cost subtracted must be the one the monitor charged, not a constant.

    Measured 2026-09-17 over an 8h window (5,175 evaluable rows): every venue's
    recorded `roundtrip_cost_usdt` is **50.1 bps** of its own `spot_open_usdt`,
    because `RoundTripCosts.total` levies slippage+failure on all four legs'
    turnover and the perp fee on the perp pair. The tally was subtracting a flat
    15 bps instead — the sum of `[costs]`'s parameters, applied once — which
    answered "how big is the number" with a cost nobody pays.
    """

    # 3.76 USDT a share, 0.0188 USDT of recorded round-trip cost = 50 bps.
    SPOT = Decimal("3.76")
    COST = Decimal("0.0188")

    def _row(self, gross, **kw):
        return row("r1", "gate", "KUAISHOU", gross,
                   cost=self.COST, spot_open=self.SPOT, **kw)

    def test_recorded_cost_wins_over_the_flat_constant(self):
        # gross 60 at a recorded 50 bps cost is net 10: priced, above the 20 bps
        # opportunity bar, and still not worth doing. Under the old flat 15 bps
        # it was net 45 and counted as a candidate.
        entry = build_scoreboard([self._row(60)])["venues"]["gate"]
        self.assertEqual(entry["opportunity_rows"], 1)
        self.assertEqual(entry["candidate_rows"], 0)
        # With a row that does clear the bar, the cost column shows the 50 bps
        # the monitor actually charged rather than the 15 bps parameter.
        entry = build_scoreboard([self._row(60), self._row(120)])["venues"]["gate"]
        self.assertEqual(entry["opportunity_rows"], 2)
        self.assertEqual(entry["candidate_rows"], 1)
        self.assertEqual(entry["cost_bps_median"], "50.0")
        self.assertEqual(entry["cost_quote_median"], "0.0188")
        self.assertEqual(entry["cost_fallback_rows"], 0)

    def test_flat_mode_reproduces_the_pre_change_numbers(self):
        entry = build_scoreboard([self._row(60), self._row(120)],
                                 cost_mode="flat",
                                 cost_bps=Decimal("15"))["venues"]["gate"]
        self.assertEqual(entry["candidate_rows"], 2)
        # Flat mode charges the constant to every row, so the reported cost is
        # the parameter and nothing was measured: `cost_quote_median` is empty
        # and every candidate is flagged as a fallback.
        self.assertEqual(entry["cost_bps_median"], "15.0")
        self.assertIsNone(entry["cost_quote_median"])
        self.assertEqual(entry["cost_fallback_rows"], 2)

    def test_a_row_without_a_recorded_cost_still_falls_back(self):
        # A row that never reached the cost model carries no cost. Dropping it
        # would silently delete an opportunity; charging it 0 would understate
        # the cost. It falls back to the documented constant and says so.
        entry = build_scoreboard([row("r1", "gate", "KUAISHOU", 60)])["venues"]["gate"]
        self.assertEqual(entry["candidate_rows"], 1)
        self.assertEqual(entry["cost_bps_median"], "15.0")
        self.assertEqual(entry["cost_fallback_rows"], 1)

    def test_cost_mode_is_validated(self):
        with self.assertRaises(ValueError):
            build_scoreboard([], cost_mode="whatever")


class MoneyTests(unittest.TestCase):
    """金额 / 利润估计: the recorded USDT, resized to what the wallet can buy.

    3.76 USDT a share, 50 bps of recorded cost, a 3 bps assumed exit:
      net bps = gross - 50;  deployable = floor(min(cap, wallet x 0.999) / 3.76)
      profit   = deployable_notional x (net bps - 3) / 10000
    """

    SPOT = Decimal("3.76")
    COST = Decimal("0.0188")

    def _row(self, gross, run_id="r1", **kw):
        return row(run_id, "gate", "KUAISHOU", gross,
                   cost=self.COST, spot_open=self.SPOT,
                   profit=Decimal("0.0252"), **kw)

    def test_money_is_charged_to_candidates_not_to_every_opportunity(self):
        # The 30 bps row clears the 20 bps *opportunity* bar but not the 20 bps
        # *net* bar (net -20 bps). Averaging its loss into the venue's profit
        # would answer "how much did we avoid losing" instead of "how much is
        # there", so the money columns describe the single candidate only.
        entry = build_scoreboard([self._row(30), self._row(120)])["venues"]["gate"]
        self.assertEqual(entry["opportunity_rows"], 2)
        self.assertEqual(entry["candidate_rows"], 1)
        # 10 / 3.76 -> 2 whole shares -> 7.52 notional; (70-3) bps of it.
        self.assertEqual(entry["deployable_shares_median"], "2")
        self.assertEqual(entry["deployable_notional_median"], "7.52")
        self.assertEqual(entry["profit_probe_median"], "0.0252")
        self.assertEqual(entry["profit_deployable_median"], "0.0504")

    def test_the_wallet_is_the_size_constraint_not_the_ceiling(self):
        # A 5 USDT wallet against a 10 USDT ceiling: min() picks the wallet, then
        # whole shares. Ignoring the wallet would report a size no trade reaches.
        board = build_scoreboard([self._row(120)],
                                 trade_quote_usdt=Decimal("10"),
                                 wallet_quote_usdt=Decimal("5"))
        self.assertEqual(board["thresholds"]["effective_cap_usdt"], "4.995")
        entry = board["venues"]["gate"]
        self.assertEqual(entry["deployable_shares_median"], "1")
        self.assertEqual(entry["deployable_notional_median"], "3.76")

    def test_an_unknown_wallet_falls_back_to_the_ceiling_and_says_so(self):
        board = build_scoreboard([self._row(120)], trade_quote_usdt=Decimal("10"))
        self.assertIsNone(board["thresholds"]["wallet_quote_usdt"])
        self.assertEqual(board["thresholds"]["effective_cap_usdt"], "10")
        self.assertEqual(board["venues"]["gate"]["deployable_notional_median"], "7.52")
        self.assertIn("钱包未知", format_report(board))

    def test_a_share_above_the_budget_is_reported_not_hidden(self):
        # Measured 2026-09-17: POPMART at 19.44 USDG against a 10.91 USDG wallet
        # buys zero whole shares. That is a different blocker from "no basis",
        # and it has to be visible rather than averaged into a median of 0.
        entry = build_scoreboard(
            [row("r1", "okx", "POPMART", 120, cost=Decimal("0.0973"),
                 spot_open=Decimal("19.44"))],
            wallet_quote_usdt=Decimal("10.92"))["venues"]["okx"]
        self.assertEqual(entry["deployable_shares_median"], "0")
        self.assertEqual(entry["deployable_notional_median"], "0.00")
        self.assertEqual(entry["unaffordable_rows"], 1)

    def test_profitable_rows_uses_the_project_minimum_profit(self):
        # 7.52 notional x 67 bps = 0.0504 USDT: real, but short of the project's
        # own 1 USDT bar. This is the column that decides "wire it up now" vs
        # "fund the wallet first".
        entry = build_scoreboard([self._row(120)])["venues"]["gate"]
        self.assertEqual(entry["profitable_rows"], 0)
        entry = build_scoreboard([self._row(120)],
                                 min_profit_quote=Decimal("0.05"))["venues"]["gate"]
        self.assertEqual(entry["profitable_rows"], 1)

    def test_a_venue_without_candidates_shows_dashes_not_zeroes(self):
        board = build_scoreboard([self._row(10)])  # net -40 bps
        entry = board["venues"]["gate"]
        self.assertEqual(entry["candidate_rows"], 0)
        self.assertIsNone(entry["profit_deployable_median"])
        self.assertIsNone(entry["cost_bps_median"])

    def test_report_renders_the_money_table(self):
        text = format_report(build_scoreboard([self._row(120)]))
        self.assertIn("成本中位", text)
        self.assertIn("可部署名义中位", text)
        self.assertIn("利润中位@可部署", text)
        html = scoreboard_html(build_scoreboard([self._row(120)]))
        self.assertIn("金额 / 利润估计 / 成本估计", html)


class ExecutableTests(unittest.TestCase):
    def test_executable_pairs_come_from_the_allowlist_product(self):
        pairs = executable_pairs(["okx"],
                                 ["XIAOMI-USDT-SWAP", "SHEIN-USDT-SWAP"])
        self.assertEqual(pairs,
                         {"okx:XIAOMI-USDT-SWAP", "okx:SHEIN-USDT-SWAP"})

    def test_a_candidate_on_a_venue_without_an_adapter_is_not_executable(self):
        board = build_scoreboard(
            [row("r1", "bybit", "KUAISHOU", 150,
                 instrument="bybit:KUAISHOUUSDT")],
            executable=executable_pairs(["okx"], ["XIAOMI-USDT-SWAP"]))
        entry = board["venues"]["bybit"]
        self.assertEqual(entry["candidate_rows"], 1)
        self.assertEqual(entry["executable_rows"], 0)
        self.assertFalse(entry["has_execution_adapter"])

    def test_the_allowlisted_okx_instrument_is_marked_executable(self):
        board = build_scoreboard(
            [row("r1", "okx", "XIAOMI", 60, instrument="okx:XIAOMI-USDT-SWAP")],
            executable=executable_pairs(["okx"], ["XIAOMI-USDT-SWAP"]))
        entry = board["venues"]["okx"]
        self.assertEqual(entry["executable_rows"], 1)
        self.assertTrue(entry["has_execution_adapter"])

    def test_policy_file_is_read_not_assumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.toml"
            path.write_text(
                "[allowlist]\n"
                'venues = ["okx"]\n'
                'instruments = ["XIAOMI-USDT-SWAP"]\n',
                encoding="utf-8")
            self.assertEqual(executable_from_policy(str(path)),
                             {"okx:XIAOMI-USDT-SWAP"})
            # A policy that cannot be read yields no executable pairs: the
            # report must never invent an executable venue.
            missing = Path(tmp) / "missing.toml"
            self.assertEqual(executable_from_policy(str(missing)), set())


class LoadRowsTests(unittest.TestCase):
    def _db(self, rows):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        conn = sqlite3.connect(handle.name)
        conn.execute(
            "create table monitor_events (id integer primary key, run_id text,"
            " kind text, asset text, venue text, source text, venue_ts_ms integer,"
            " recv_ts_ms integer, sequence integer, status text, payload_json text)")
        for entry in rows:
            conn.execute(
                "insert into monitor_events (run_id, kind, asset, venue, source,"
                " recv_ts_ms, status, payload_json) values (?,?,?,?,?,?,?,?)",
                (entry["run_id"], "basis_evaluation", entry["asset"],
                 entry["venue"], "test", entry["recv_ts_ms"], "watch_only",
                 json.dumps({
                     "instrument": entry["instrument"],
                     "entry_basis_bps": (None if entry["gross_bps"] is None
                                         else str(entry["gross_bps"])),
                     "rejections": entry["rejections"],
                     "phase": entry["phase"],
                     "roundtrip_cost_usdt": (None if entry["cost_quote"] is None
                                             else str(entry["cost_quote"])),
                     # Written by the streaming evaluator; the tally counts a
                     # row only when this is present.
                     "evaluated_at_ms": entry["evaluated_at_ms"],
                     "spot_open_usdt": (None if entry["spot_open_quote"] is None
                                        else str(entry["spot_open_quote"])),
                     "conditional_profit_usdt": (None if entry["profit_quote"] is None
                                                 else str(entry["profit_quote"])),
                     "spot_quantity": (None if entry["spot_quantity"] is None
                                       else str(entry["spot_quantity"])),
                 })))
        conn.commit()
        conn.close()
        self.addCleanup(Path(handle.name).unlink, missing_ok=True)
        return handle.name

    def test_loads_only_the_window_and_only_basis_evaluations(self):
        now = 1_700_000_000_000
        path = self._db([
            row("old", "okx", "XIAOMI", 50, ts=now - 2 * DAY_MS),
            row("new", "okx", "XIAOMI", 60, ts=now - 60_000),
        ])
        inside = load_rows(path, since_ms=now - DAY_MS)
        self.assertEqual([entry["run_id"] for entry in inside], ["new"])
        self.assertEqual(inside[0]["gross_bps"], Decimal("60"))
        self.assertEqual(inside[0]["phase"], "continuous_am")
        self.assertEqual(len(load_rows(path)), 2)

    def test_collect_end_to_end_on_a_real_file(self):
        now = 1_700_000_000_000
        path = self._db([
            row("a", "okx", "SHEIN", 60, instrument="okx:SHEIN-USDT-SWAP",
                ts=now - 1000),
            row("b", "bybit", "KUAISHOU", 150,
                instrument="bybit:KUAISHOUUSDT", phase="after_close",
                ts=now - 1000),
        ])
        board = collect(db_path=path, hours=1,
                        executable=executable_pairs(["okx"], ["SHEIN-USDT-SWAP"]),
                        now_ms=now)
        # Both venues have one candidate; the wider basis breaks the tie, and
        # the after-close row is not demoted for being outside the cash session.
        self.assertEqual(board["ranking"][0], "bybit")
        self.assertEqual(board["venues"]["okx"]["executable_rows"], 1)
        self.assertEqual(board["venues"]["bybit"]["executable_rows"], 0)

    def test_the_money_fields_round_trip_out_of_the_database(self):
        # The whole point of the 2026-09-17 change is that these numbers are
        # already recorded; a reader that drops them forces the tally back onto
        # bps extrapolation. This pins the round trip, not the arithmetic.
        now = 1_700_000_000_000
        path = self._db([
            row("m1", "gate", "KUAISHOU", 120, ts=now - 1000,
                cost=Decimal("0.0376"), spot_open=Decimal("7.52"),
                profit=Decimal("0.0504"), quantity=Decimal("2")),
        ])
        loaded = load_rows(path)[0]
        self.assertEqual(loaded["cost_quote"], Decimal("0.0376"))
        self.assertEqual(loaded["spot_open_quote"], Decimal("7.52"))
        self.assertEqual(loaded["profit_quote"], Decimal("0.0504"))
        self.assertEqual(loaded["spot_quantity"], Decimal("2"))
        # 0.0376 / 7.52 = 50 bps of the row's own notional, and the per-share
        # price the resize step needs is notional / quantity (3.76), not the
        # notional itself (7.52) — dividing by the quantity is what keeps a
        # 2-share probe from being read as a 7.52-a-share token.
        board = collect(db_path=path, hours=1, now_ms=now)
        entry = board["venues"]["gate"]
        self.assertEqual(entry["cost_bps_median"], "50.0")
        self.assertEqual(entry["deployable_shares_median"], "2")
        self.assertEqual(entry["deployable_notional_median"], "7.52")


class RenderTests(unittest.TestCase):
    def _board(self):
        return build_scoreboard(
            [
                row("s1", "okx", "SHEIN", 60, phase="continuous_pm",
                    instrument="okx:SHEIN-USDT-SWAP"),
                row("o1", "bybit", "KUAISHOU", 150, phase="after_close",
                    instrument="bybit:KUAISHOUUSDT"),
                row("o1", "binance", "KUAISHOU", None,
                    rejections=["quanto_or_unknown_payout_requires_separate_hedge_model"]),
            ],
            executable=executable_pairs(["okx"], ["SHEIN-USDT-SWAP"]))

    def test_text_report_names_every_venue_and_the_reading_rule(self):
        text = format_report(self._board())
        for venue in ("okx", "bybit", "binance"):
            self.assertIn(venue, text)
        self.assertIn("分母", text)
        self.assertIn("监控 != 可执行", text)

    def test_html_report_renders_and_marks_the_missing_phase(self):
        html = scoreboard_html(self._board())
        self.assertIn("场馆机会统计", html)
        # bybit has no session rows at all: a dash, not a zero.
        self.assertIn("—", html)
        self.assertIn("无适配器", html)
        self.assertIn("够本金", html)

    def test_empty_input_renders_instead_of_raising(self):
        board = build_scoreboard([])
        self.assertEqual(board["ranking"], [])
        self.assertEqual(board["window"]["cycles"], 0)
        self.assertIn("场馆机会统计", scoreboard_html(board))
        self.assertIn("场馆机会统计", format_report(board))

    def test_the_weekend_bucket_is_printed_not_hidden(self):
        """A closed-market evaluation has to be readable, not silently bucketed.

        Before 2026-09-20 both report formats printed only 交易时段 and 盘前盘后,
        so weekend rows — the majority of a 54h window — appeared nowhere while
        still counting in the venue-level rate.
        """
        board = build_scoreboard([row("w1", "binance", "TENCENT", 80,
                                      phase="closed")])
        self.assertIn("其他/休市", format_report(board))
        self.assertIn("其他 / 休市", scoreboard_html(board))


if __name__ == "__main__":
    unittest.main()
