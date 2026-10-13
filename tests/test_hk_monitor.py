from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
import json
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from arb_monitor.apps.hk_monitor import (
    HKMonitor, age_summary, dashboard_html, format_discovery_message,
    in_preopen_capture_window, load_config, quote_from_record,
)
from arb_monitor.core.hk import (
    Contract, PairMapping, RoundTripCosts, SpotQuote, decimal, dumps, paired_pnl,
)
from arb_monitor.core.hk_calendar import HK, HKCalendar
from arb_monitor.core.math import net_edge, quantity_for_budget
from arb_monitor.core.models import FeeModel, Level, OrderBook
from arb_monitor.data.hk_cex import parse_book, parse_contracts
from arb_monitor.data.onchainos import OnchainOSQuoteAdapter, parse_quote
from arb_monitor.ledger.store import EventStore
from arb_monitor.ops.dedup import SignalDeduplicator
from arb_monitor.risk.hk_basis import BasisLimits
from arb_monitor.strategy.hk_basis import HKBasisStrategy
from arb_monitor.strategy.hk_probe import AnchorReference, assess_probe
from arb_monitor.strategy.spread import CrossVenueSpreadStrategy

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/hk_monitor.toml"


def offline_config(**overrides):
    """The shipped config with the live X Layer reads switched off.

    Every test here drives the cycle with fake venues and a fake quote adapter,
    but the shipped config has ``onchain.enabled = true``. Used verbatim, a
    test read X Layer for real and then waited out the retry backoff
    (0.4s → 0.8s → 1.2s per call), so it measured the network instead of the
    monitor. That backoff also hid the streaming property under test: every leg
    finished at the same moment, so a slow probe *looked* like it held back the
    others. A test that wants chain reads provides a fake adapter.

    The runtime paths are redirected the same way ``test_full_chain_live`` does
    it for the stop flags: ``cycle()`` dispatches through the auto-executor,
    which opens ``execution_ledger`` (shipped default: the operator's live
    ``data/execution.db``) and takes a lock file. A test must not read or write
    either — a clean checkout has no ``data/`` at all, so the suite failed with
    ``sqlite3.OperationalError: unable to open database file``.
    """
    config = load_config(str(CONFIG))
    config.setdefault("onchain", {})["enabled"] = False
    scratch = Path(mkdtemp(prefix="arb-tests-hk-"))
    config["execution_ledger"] = str(scratch / "execution.db")
    execution = config.setdefault("execution_policy", {}).setdefault("execution", {})
    execution["auto_halt_file"] = str(scratch / "AUTO_HALT")
    execution["auto_lock_file"] = str(scratch / "auto_execute.lock")
    for key, value in overrides.items():
        section, _, field = key.partition(".")
        config.setdefault(section, {})[field] = value
    return config


def ms(day="2026-09-14", hour=9, minute=1) -> int:
    return int(datetime.fromisoformat(f"{day}T{hour:02}:{minute:02}:00").replace(tzinfo=HK).timestamp()) * 1000


NOW = ms()


def contract(**kwargs):
    c = Contract(
        "gate", "XIAOMI_USDT", "XIAOMI", "hk_stock_candidate", "usd_linear",
        D("0.1"), D("1"), D("1"), D("5"), D(".001"), True, "public:test", NOW, {},
    )
    return replace(c, **kwargs)


def spot(side="buy", price="100", quantity="2", **kwargs):
    q = D(quantity)
    result = SpotQuote(
        "XIAOMI", side, "196", "0x" + "1" * 40, "0x" + "2" * 40, "USDC",
        q * D(price) if side == "buy" else q,
        q, q * D(price), "ok", NOW - 100, NOW - 50, NOW + 1000, "test_quote",
        routes=("verified_rfq",), gas_quote=D(".01"),
    )
    return replace(result, **kwargs)


def perp(bid="103", ask="104", size="100", **kwargs):
    return replace(OrderBook(
        "gate", "XIAOMI_USDT",
        (Level(D(bid), D(size)),), (Level(D(ask), D(size)),),
        NOW - 30, NOW - 10, 123, "test_book",
    ), **kwargs)


def mapping(**kwargs):
    return replace(PairMapping("XIAOMI", D("1"), D("1"), D("1"), True, "fixture", NOW + 10000), **kwargs)


def strategy(**kwargs):
    defaults = dict(
        contract=contract(), mapping=mapping(), buy=spot(), sell=spot("sell", "99"),
        costs=RoundTripCosts(verified=True, valid_until_ms=NOW + 10000), limits=BasisLimits(),
        target_exit_basis_bps=D("0"), phase="preopen_0900_0930",
    )
    defaults.update(kwargs)
    return HKBasisStrategy(**defaults)


class DecimalAndFreshnessTests(unittest.TestCase):
    def test_binary_floats_and_nonfinite_rejected(self):
        for value in (0.1, True, "NaN", "Infinity"):
            with self.assertRaises(ValueError):
                decimal(value)

    def test_idle_book_ages_not_just_receive_lag(self):
        book = perp()
        self.assertTrue(book.is_fresh(NOW, 1000))
        self.assertFalse(book.is_fresh(NOW + 5000, 1000))

    def test_missing_or_future_venue_timestamp_is_rejected(self):
        self.assertFalse(perp(venue_ts_ms=None).is_fresh(NOW, 1000))
        self.assertFalse(perp(venue_ts_ms=NOW + 5000).is_fresh(NOW, 1000))
        self.assertFalse(perp(recv_ts_ms=NOW + 5000).is_fresh(NOW, 1000))

    def test_quote_request_latency_counts_against_freshness(self):
        self.assertFalse(spot(request_ts_ms=NOW - 30000).is_fresh(NOW, 15000))
        self.assertFalse(spot(expires_ts_ms=NOW).is_fresh(NOW, 15000))

    def test_depth_budget_not_best_ask_budget(self):
        levels = (Level(D("100"), D("1")), Level(D("200"), D("2")))
        self.assertEqual(quantity_for_budget(levels, D("200")), D("1.5"))
        books = [
            OrderBook("a", "X", (), levels, NOW, NOW),
            OrderBook("b", "X", (Level(D("300"), D("10")),), (), NOW, NOW),
        ]
        s = CrossVenueSpreadStrategy("a", "b", FeeModel(), D("200"), D("0"), D("0"))
        intent = list(s.on_snapshot(books, NOW))[0]
        self.assertEqual(intent.legs[0].notional, D("200"))

    def test_fixed_costs_included_in_bps_and_sell_fees_on_sell_notional(self):
        _, edge, profit = net_edge(
            D("100"), D("101"), FeeModel(sell_bps=D("100"), gas_quote=D("1")), D("100")
        )
        self.assertEqual(profit, D("-1.01"))
        self.assertEqual(edge, profit / D("100") * D("10000"))

    def test_dedup_does_not_reset_on_price_change(self):
        first = list(strategy().on_snapshot([perp()], NOW))[0]
        second = list(strategy().on_snapshot([perp(bid="103.1")], NOW + 1))[0]
        dedup = SignalDeduplicator(3000)
        self.assertTrue(dedup.accept(first))
        self.assertFalse(dedup.accept(second))


class BasisTests(unittest.TestCase):
    def test_positive_entry_is_not_immediate_profit(self):
        result = strategy().evaluate([perp()], NOW)
        self.assertEqual(result["entry_basis_bps"], D("300"))
        self.assertLess(result["immediate_roundtrip_net_usdt"], 0)
        self.assertGreater(result["conditional_profit_usdt"], 0)
        self.assertEqual(result["contracts"], D("20"))
        self.assertEqual(result["status"], "conditional_signal")

    def test_no_target_no_profit_claim_or_intent(self):
        s = strategy(target_exit_basis_bps=None)
        self.assertIsNone(s.evaluate([perp()], NOW)["conditional_profit_usdt"])
        self.assertEqual(list(s.on_snapshot([perp()], NOW)), [])

    def test_negative_entry_can_profit_if_exit_basis_more_negative(self):
        s = strategy(target_exit_basis_bps=D("-400"), sell=spot("sell", "101"))
        result = s.evaluate([perp(bid="99", ask="100")], NOW)
        self.assertLess(result["entry_basis_bps"], 0)
        self.assertGreater(result["conditional_profit_usdt"], 0)

    def test_price_direction_not_a_paired_strategy_backtest(self):
        self.assertEqual(paired_pnl(D("2"), D("100"), D("110"), D("20"), D(".1"),
                                    D("103"), D("113"), D("0")), D("0"))
        self.assertEqual(paired_pnl(D("2"), D("100"), D("90"), D("20"), D(".1"),
                                    D("103"), D("93"), D("0")), D("0"))

    def test_signed_funding(self):
        args = (D("1"), D("100"), D("100"), D("1"), D("1"), D("100"), D("100"), D("0"))
        self.assertEqual(paired_pnl(*args, D("1")), D("1"))
        self.assertEqual(paired_pnl(*args, D("-1")), D("-1"))

    def test_gas_budget_is_explicit_not_native_units(self):
        s = strategy(buy=spot(gas_quote=None), sell=spot("sell", "99", gas_quote=None),
                     costs=RoundTripCosts(verified=True, valid_until_ms=NOW + 10000,
                                          roundtrip_gas_budget_usdt=D("1")))
        result = s.evaluate([perp()], NOW)
        self.assertEqual(result["gas_basis"], "explicit_roundtrip_USDT_budget_not_provider_estimate")
        self.assertGreater(result["roundtrip_cost_usdt"], D("1"))

    def test_expired_costs_suppress_intent(self):
        s = strategy(costs=RoundTripCosts(verified=True, valid_until_ms=NOW))
        self.assertEqual(list(s.on_snapshot([perp()], NOW)), [])

    def test_quanto_not_misread_as_huge_arbitrage(self):
        s = strategy(contract=contract(payout_model="hkd_quanto"))
        result = s.evaluate([perp(bid="780", ask="781")], NOW)
        self.assertIsNone(result["entry_basis_bps"])
        self.assertEqual(list(s.on_snapshot([perp()], NOW)), [])

    def test_mapping_and_costs_must_be_verified(self):
        for s in (
            strategy(mapping=mapping(verified=False)),
            strategy(mapping=mapping(valid_until_ms=NOW)),
            strategy(costs=RoundTripCosts()),
            strategy(buy=spot(gas_quote=None)),
        ):
            self.assertEqual(list(s.on_snapshot([perp()], NOW)), [])

    def test_shares_ratio_and_multiplier(self):
        s = strategy(mapping=mapping(shares_per_token=D("2")))
        result = s.evaluate([perp()], NOW)
        self.assertEqual(result["contracts"], D("40"))

    def test_depth_is_walked_and_both_sides_required(self):
        b = perp(bids=(Level(D("103"), D("10")), Level(D("102"), D("10"))))
        result = strategy().evaluate([b], NOW)
        self.assertEqual(result["perp_open_usdt"], D("205"))
        self.assertEqual(list(strategy().on_snapshot([perp(size="19")], NOW)), [])

    def test_rounding_leaves_residual_and_suppresses_intent(self):
        s = strategy(buy=spot(quantity="2.01"), sell=spot("sell", "99", "2.01"))
        result = s.evaluate([perp()], NOW)
        self.assertGreater(result["residual_delta_bps"], 0)
        self.assertEqual(list(s.on_snapshot([perp()], NOW)), [])

    def test_mismatched_sell_size_suppresses_intent(self):
        s = strategy(sell=spot("sell", "99", "3"))
        self.assertIn("spot_exit_quote_quantity_mismatch", s.evaluate([perp()], NOW)["rejections"])
        self.assertEqual(list(s.on_snapshot([perp()], NOW)), [])

    def test_mispaired_asset_direction_or_mapping_cannot_signal(self):
        for s in (
            strategy(buy=spot(asset="TENCENT")),
            strategy(buy=spot(side="sell")),
            strategy(mapping=mapping(asset="TENCENT")),
            strategy(sell=spot("sell", "99", chain_id="1")),
        ):
            self.assertEqual(list(s.on_snapshot([perp()], NOW)), [])

    def test_asynchronous_or_stale_legs_suppress_intent(self):
        self.assertEqual(list(strategy().on_snapshot([perp(venue_ts_ms=NOW - 20000)], NOW)), [])
        s = strategy(limits=BasisLimits(max_pair_skew_ms=20))
        self.assertEqual(list(s.on_snapshot([perp()], NOW)), [])

    def test_capital_not_two_times_user_cash(self):
        with self.assertRaises(ValueError):
            BasisLimits(total_capital=D("10000"), spot_allocation=D("10000"))
        with self.assertRaises(ValueError):
            BasisLimits(total_capital=D("10001"))
        s = strategy(limits=BasisLimits(spot_allocation=D("199")))
        self.assertEqual(list(s.on_snapshot([perp()], NOW)), [])

    def test_every_calendar_phase_is_inside_the_strategy_window(self):
        """The cash session is not a gate: both legs quote 24/7.

        Operator decision 2026-09-20, taken twice (AGENTS.md 全天候原则). This
        used to be `test_no_after_open_directional_short_intent`, which asserted
        the opposite for `continuous_am`; the weekend phase `closed` fell through
        the same check, so a whole weekend of token-vs-perp dislocations was
        recorded and then ignored.
        """
        for phase in ("before_0900", "preopen_0900_0930", "continuous_am",
                      "lunch", "continuous_pm", "closing_auction",
                      "after_close", "closed"):
            self.assertNotIn(
                "outside_strategy_window",
                strategy(phase=phase).evaluate([perp()], NOW)["rejections"])
        # The after-open suppression is gone, not merely re-labelled.
        self.assertEqual(
            len(list(strategy(phase="continuous_am").on_snapshot([perp()], NOW))), 1)
        # Fail closed on a calendar we cannot place: unknown is still refused.
        self.assertIn(
            "outside_strategy_window",
            strategy(phase="calendar_unknown").evaluate([perp()], NOW)["rejections"])


class AdapterTests(unittest.TestCase):
    def setUp(self):
        config = load_config(str(CONFIG))
        self.token, self.quote_token = config["tokens"][0], config["quote_token"]

    def raw_quote(self):
        return {"ok": True, "data": [{
            "fromToken": {"tokenContractAddress": self.quote_token["address"], "decimal": "6"},
            "toToken": {"tokenContractAddress": self.token["address"], "decimal": "18"},
            "fromTokenAmount": "100000000", "toTokenAmount": "25000000000000000000",
            "dexRouterList": [{"subRouterList": [{"dexProtocol": [{"dexName": "AMM"}]}]}],
            "estimateGasFee": "100000", "priceImpactPercentage": ".2",
        }]}

    def parse(self, raw):
        return parse_quote(raw, self.token, self.quote_token, "buy", D("100"), NOW, NOW + 20, 1000)

    def test_exact_amount_and_addresses(self):
        q = self.parse(self.raw_quote())
        self.assertEqual(q.status, "ok")
        self.assertEqual(q.token_quantity, D("25"))
        self.assertEqual(q.price, D("4"))
        self.assertIsNone(q.gas_quote)
        self.assertEqual(q.routes, ("AMM",))

    def test_auth_is_not_no_liquidity(self):
        q = self.parse({"ok": False, "error": "API error (code=50114): Invalid Authority"})
        self.assertEqual(q.status, "auth_error")
        self.assertEqual(q.error_code, "50114")
        self.assertIsNone(q.price)

    def test_wrong_amount_token_schema_never_valid(self):
        raw = self.raw_quote()
        raw["data"][0]["fromTokenAmount"] = "100000001"
        self.assertEqual(self.parse(raw).status, "schema_error")
        raw = self.raw_quote()
        raw["data"][0]["toToken"]["tokenContractAddress"] = self.quote_token["address"]
        self.assertEqual(self.parse(raw).status, "schema_error")
        self.assertEqual(self.parse({"ok": True, "data": {}}).status, "schema_error")

    def test_quote_cli_does_not_sign_or_swap(self):
        completed = MagicMock(returncode=0, stdout=json.dumps(self.raw_quote()))
        with patch("arb_monitor.data.onchainos.subprocess.run", return_value=completed) as call:
            OnchainOSQuoteAdapter().quote(self.token, self.quote_token, "buy", D("100"))
        args = call.call_args.args[0]
        self.assertEqual(args[1:3], ["swap", "quote"])
        self.assertEqual(args[args.index("--amount") + 1], "100000000")
        self.assertNotIn("approve", args)

    def test_gate_native_depth_preserves_contract_units_and_time(self):
        raw = {"update": D("1789347660.123"), "current": D("1789347660.456"),
               "id": 99, "bids": [{"p": "3.3", "s": 17}], "asks": [{"p": "3.4", "s": 18}]}
        b = parse_book(contract(), raw, NOW)
        self.assertEqual(b.bids[0].quantity, D("17"))
        self.assertEqual(b.venue_ts_ms, 1789347660123)
        self.assertEqual(b.sequence, 99)
        self.assertEqual(b.metadata["contract_multiplier"], "0.1")

    def test_gate_contract_classifier_avoids_crypto_ticker_substrings(self):
        raw = {"name": "XIAOMI_USDT", "contract_type": "stocks", "status": "trading",
               "type": "direct", "quanto_multiplier": "1", "order_size_min": 1,
               "order_price_round": ".001", "in_delisting": False}
        rows = parse_contracts("gate", [
            raw, {**raw, "name": "XIAOMIHKD_USDT"},
            {**raw, "name": "MBABYDOGE_USDT", "contract_type": ""},
            {**raw, "name": "BABA_USDT"},
        ], NOW)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1].payout_model, "hkd_quanto")
        self.assertEqual(rows[2].payout_model, "unknown")

    def test_bad_order_book_never_qualifies(self):
        raw = {"data": [{"ts": str(NOW), "seqId": 1, "bids": [["105", "2"]],
                         "asks": [["104", "2"]]}]}
        with self.assertRaises(ValueError):
            parse_book(contract(venue="okx"), raw, NOW)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.calendar = HKCalendar.from_config(load_config(str(CONFIG))["calendar"])
        self.reference = AnchorReference(
            D("100"), D("104"), date(2026, 9, 11), NOW - 1000, NOW + 10000,
            True, "verified_test_reference", "XIAOMI", "USDC",
        )

    def samples(self, **kwargs):
        return [spot(request_ts_ms=NOW + i * 100, recv_ts_ms=NOW + i * 100 + 20,
                     expires_ts_ms=NOW + 5000, **kwargs) for i in range(3)]

    def assess(self, quotes, **kwargs):
        options = dict(special_route_ids={"verified_rfq"}, reference=self.reference)
        options.update(kwargs)
        return assess_probe(quotes, self.calendar, **options)

    def test_trade_day_time_window(self):
        self.assertEqual(self.calendar.phase(ms(hour=8, minute=59)), "before_0900")
        self.assertEqual(self.calendar.phase(ms(minute=0)), "preopen_0900_0930")
        self.assertEqual(self.calendar.phase(ms(minute=30)), "continuous_am")
        self.assertEqual(self.calendar.phase(ms("2026-09-12")), "closed")
        self.assertEqual(self.calendar.phase(ms("2026-10-01")), "closed")
        self.assertEqual(self.calendar.phase(ms("2027-09-14")), "calendar_unknown")

    def test_previous_session_skips_holiday_and_weekend(self):
        self.assertEqual(self.calendar.previous_session(date(2026, 9, 14)), date(2026, 9, 11))
        self.assertEqual(self.calendar.previous_session(date(2026, 4, 8)), date(2026, 4, 2))

    def test_scheduled_capture_window_is_bounded(self):
        self.assertFalse(in_preopen_capture_window(self.calendar, ms(hour=8, minute=54)))
        self.assertTrue(in_preopen_capture_window(self.calendar, ms(hour=8, minute=55)))
        self.assertTrue(in_preopen_capture_window(self.calendar, ms(minute=35)))
        self.assertFalse(in_preopen_capture_window(self.calendar, ms(minute=36)))
        self.assertFalse(in_preopen_capture_window(self.calendar, ms("2026-09-12")))

    def test_channel_and_anchor_are_distinct(self):
        r = self.assess(self.samples())
        self.assertEqual(r["channel"], "special_route_observed")
        self.assertEqual(r["yesterday_close_anchor"], "consistent_with_old_close")
        r = self.assess(self.samples(), reference=None)
        self.assertEqual(r["channel"], "special_route_observed")
        self.assertEqual(r["yesterday_close_anchor"], "inconclusive")

    def test_quote_update_refutes_old_anchor_not_channel(self):
        r = self.assess(self.samples(price="104"))
        self.assertEqual(r["channel"], "special_route_observed")
        self.assertEqual(r["yesterday_close_anchor"], "not_anchored_to_old_close_in_sample")

    def test_auth_network_and_empty_data_never_prove_channel_closed(self):
        for status in ("auth_error", "api_error", "schema_error"):
            r = self.assess(self.samples(status=status))
            self.assertEqual(r["channel"], "unknown")
        r = self.assess([])
        self.assertEqual(r["channel"], "unknown")

    def test_weekend_cannot_validate_nine_am_hypothesis(self):
        q = spot(request_ts_ms=ms("2026-09-12"))
        self.assertEqual(self.assess([q])["channel"], "outside_0900_window")

    def test_amm_is_not_dedicated_channel(self):
        r = self.assess(self.samples(routes=("Uniswap V3",)))
        self.assertEqual(r["channel"], "special_route_not_observed")
        r = self.assess(self.samples(), special_route_ids=set())
        self.assertEqual(r["channel"], "quote_available_special_route_unknown")

    def test_mixed_rfq_amm_quote_not_attributed_wholly_to_rfq(self):
        r = self.assess(self.samples(routes=("verified_rfq", "Uniswap")))
        self.assertEqual(r["channel"], "special_route_observed")
        self.assertEqual(r["yesterday_close_anchor"], "inconclusive")
        self.assertEqual(r["reason"], "mixed_or_unidentified_route_cannot_attribute_anchor")

    def test_no_overnight_move_inconclusive(self):
        r = self.assess(self.samples(), reference=replace(self.reference, independent_current_per_token=D("100.1")))
        self.assertEqual(r["reason"], "overnight_move_too_small_to_discriminate")

    def test_same_snapshot_repeated_does_not_count(self):
        self.assertEqual(self.assess([spot()] * 3)["reason"], "insufficient_distinct_fresh_measurements")

    def test_future_or_wrong_date_reference_disallowed(self):
        r = self.assess(self.samples(), reference=replace(self.reference, observed_ts_ms=NOW + 9000))
        self.assertEqual(r["reason"], "insufficient_distinct_fresh_measurements")
        r = self.assess(self.samples(), reference=replace(self.reference, previous_session=date(2026, 9, 10)))
        self.assertEqual(r["reason"], "reference_is_not_previous_trading_session")

    def test_different_assets_or_amounts_not_pooled(self):
        with self.assertRaises(ValueError):
            self.assess([spot(), spot(asset="TENCENT")])


class PersistenceAndIntegrationTests(unittest.TestCase):
    def test_reading_stored_snapshot_reages_every_book(self):
        summary = {
            "evaluated_at_ms": NOW, "poll_interval_seconds": 60, "max_age_ms": 1000,
            "instruments": [{"book_age_ms": 10, "book_fresh": True}],
        }
        aged = age_summary(summary, NOW + 5000)
        self.assertFalse(aged["instruments"][0]["book_fresh"])
        self.assertTrue(aged["instruments"][0]["book_fresh_at_capture"])
        self.assertTrue(summary["instruments"][0]["book_fresh"])
        self.assertEqual(aged["instruments"][0]["book_age_now_ms"], 5010)

    def test_dashboard_escapes_external_content(self):
        page = dashboard_html({"hk_time": "<script>alert(1)</script>"})
        # The board ships one static inline script (the alert poller); nothing
        # from stored data may ever become part of it.
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertEqual(page.count("<script>"), 1)

    def test_roundtrip_decimal_record_and_sequence(self):
        with TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "events.db")
            q = spot()
            store.record_market_event("test", "spot_quote", q, NOW, sequence=42, source=q.source)
            recovered = quote_from_record(store.events("spot_quote")[0])
            self.assertEqual(q, recovered)
            self.assertEqual(store.conn.execute("SELECT sequence FROM monitor_events").fetchone()[0], 42)
            store.close()

    def test_intent_serializes_decimal_metadata_and_leg_symbol(self):
        with TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "events.db")
            intent = list(strategy().on_snapshot([perp()], NOW))[0]
            store.record_intent(intent)
            raw = store.conn.execute("SELECT payload_json FROM opportunities").fetchone()[0]
            payload = json.loads(raw)
            self.assertEqual(payload["legs"][1]["symbol"], "XIAOMI_USDT")
            self.assertEqual(payload["metadata"]["contracts"], "20")
            store.close()

    def test_readonly_end_to_end_auth_error_records_no_positions(self):
        config = offline_config()
        def client(venue, proxy):
            mock = MagicMock()
            c = contract(venue=venue)
            mock.instruments.return_value = ([c], {"fixture": True}, NOW)
            mock.book.return_value = perp(venue=venue)
            mock.funding.return_value = {
                "rate": D("0"), "next_funding_ts_ms": NOW + 50000,
                "venue_ts_ms": NOW, "recv_ts_ms": NOW, "source": "fixture",
            }
            return mock
        refs = MagicMock()
        refs.token.side_effect = lambda t: {
            "asset": t["asset"], "price_usd": D("100"), "source": "fixture",
            "recv_ts_ms": NOW, "raw": {},
        }
        for method in ("pool", "stock_daily", "hkd_daily"):
            getattr(refs, method).return_value = {"source": "fixture", "raw": {}, "recv_ts_ms": NOW}
        quotes = MagicMock()
        quotes.quote.return_value = spot(status="auth_error", error_code="50114")
        # Probes are whole-share exact-out quotes now, so the circuit breaker
        # trips on that call first.
        quotes.quote_exact_out.return_value = spot(status="auth_error", error_code="50114")
        # This fixture fakes three venues; the shipped config monitors five since
        # Bybit and Bitget were added (2026-09-16), so the count is pinned to the
        # venues under test rather than to whatever the file happens to list.
        config["monitor"]["venues"] = ["gate", "okx", "binance"]
        with TemporaryDirectory() as directory, \
             patch("arb_monitor.apps.hk_monitor.CexPublicAdapter", side_effect=client), \
             patch("arb_monitor.apps.hk_monitor.ReferenceAdapter", return_value=refs), \
             patch("arb_monitor.apps.hk_monitor.OnchainOSQuoteAdapter", return_value=quotes), \
             patch("arb_monitor.apps.hk_monitor.fetch_issuer_assets", return_value=[]), \
             patch("arb_monitor.apps.hk_monitor.now_ms", return_value=NOW):
            monitor = HKMonitor(config, db=str(Path(directory) / "monitor.db"))
            try:
                result = monitor.cycle()
                self.assertEqual(result["quote_status"], "auth_blocked")
                self.assertEqual(result["actual_quote_requests"], 1)
                self.assertEqual(quotes.quote_exact_out.call_count, 1)
                self.assertEqual(quotes.quote.call_count, 0)
                self.assertEqual(result["conditional_signals"], 0)
                self.assertEqual(len(monitor.store.events("perp_book")), 3)
                self.assertEqual(monitor.store.latest_summary()["run_id"], result["run_id"])
                self.assertEqual(monitor.store.conn.execute("SELECT COUNT(*) FROM manual_executions").fetchone()[0], 0)
            finally:
                monitor.close()


def issuer_asset(symbol, name, code, country, native, wrapper):
    return {"symbol": symbol, "name": name, "hk_code": code,
            "listing_country": country, "market": country or "UNKNOWN",
            "native_address": native, "wrapper_address": wrapper}


class AlternateVenueSamplingTests(unittest.TestCase):
    """隔轮采样: the alternating venues are read every other cycle, and on the
    cycles they are skipped they are not evaluated at all.

    Operator's instruction, 2026-09-17. The two things that must hold:

    * a skipped cycle leaves *no* basis_evaluation rows for that venue — an
      absent row has to mean "we did not look", never "the book was missing";
    * nothing else changes: the always-on venues keep being sampled every cycle.
    """

    def harness(self, alternate, every, runs=2):
        config = offline_config()
        config["monitor"]["venues"] = ["gate", "okx", "aster"]
        config["monitor"]["alternate_venues"] = alternate
        config["monitor"]["alternate_every_cycles"] = every
        # `_probe_bounds` refuses a contract with no reviewed mapping block, and
        # the shipped blocks are keyed per venue. The default fixture id is
        # gate's, so reusing it everywhere left okx and aster with no probe size
        # — and therefore no basis_evaluation row to count.
        instrument_ids = {"gate": "XIAOMI_USDT", "okx": "XIAOMI-USDT-SWAP",
                          "aster": "XIAOMIUSDT"}

        def client(venue, proxy):
            mock = MagicMock()
            mock.instruments.return_value = (
                [contract(venue=venue, instrument_id=instrument_ids[venue])],
                {"fixture": True}, NOW)
            mock.book.return_value = perp(venue=venue)
            mock.funding.return_value = {
                "rate": D("0"), "next_funding_ts_ms": NOW + 50000,
                "venue_ts_ms": NOW, "recv_ts_ms": NOW, "source": "fixture",
            }
            return mock

        refs = MagicMock()
        refs.token.return_value = {"asset": "XIAOMI", "price_usd": D("100"),
                                   "source": "fixture", "recv_ts_ms": NOW, "raw": {}}
        for method in ("pool", "stock_daily", "hkd_daily"):
            getattr(refs, method).return_value = {"source": "fixture", "raw": {},
                                                  "recv_ts_ms": NOW}
        quotes = MagicMock()
        quotes.quote.return_value = spot()
        quotes.quote_exact_out.return_value = spot()
        return config, client, refs, quotes, runs

    def run_cycles(self, alternate, every):
        config, client, refs, quotes, runs = self.harness(alternate, every)
        with TemporaryDirectory() as directory, \
             patch("arb_monitor.apps.hk_monitor.CexPublicAdapter", side_effect=client), \
             patch("arb_monitor.apps.hk_monitor.ReferenceAdapter", return_value=refs), \
             patch("arb_monitor.apps.hk_monitor.OnchainOSQuoteAdapter", return_value=quotes), \
             patch("arb_monitor.apps.hk_monitor.fetch_issuer_assets", return_value=[]), \
             patch("arb_monitor.apps.hk_monitor.now_ms", return_value=NOW):
            monitor = HKMonitor(config, db=str(Path(directory) / "monitor.db"))
            try:
                return [monitor.cycle() for _ in range(runs)]
            finally:
                monitor.close()

    def test_an_alternating_venue_is_sampled_only_on_every_other_cycle(self):
        summaries = self.run_cycles(alternate=["aster"], every=2)
        first, second = summaries
        self.assertIn("aster", first["sampled_venues"])
        self.assertEqual(first["skipped_venues"], [])
        self.assertNotIn("aster", second["sampled_venues"])
        self.assertEqual(second["skipped_venues"], ["aster"])

    def test_a_skipped_cycle_writes_no_rows_for_that_venue(self):
        config, client, refs, quotes, _runs = self.harness(["aster"], 2)
        with TemporaryDirectory() as directory, \
             patch("arb_monitor.apps.hk_monitor.CexPublicAdapter", side_effect=client), \
             patch("arb_monitor.apps.hk_monitor.ReferenceAdapter", return_value=refs), \
             patch("arb_monitor.apps.hk_monitor.OnchainOSQuoteAdapter", return_value=quotes), \
             patch("arb_monitor.apps.hk_monitor.fetch_issuer_assets", return_value=[]), \
             patch("arb_monitor.apps.hk_monitor.now_ms", return_value=NOW):
            monitor = HKMonitor(config, db=str(Path(directory) / "monitor.db"))
            try:
                monitor.cycle()
                monitor.cycle()
                rows = monitor.store.events("basis_evaluation")
                venues = [row["instrument"].split(":")[0] for row in rows]
                # gate/okx twice, aster once.
                self.assertEqual(venues.count("aster"), 1)
                self.assertEqual(venues.count("gate"), 2)
                books = [r["venue"] for r in monitor.store.events("perp_book")]
                self.assertEqual(books.count("aster"), 1)
            finally:
                monitor.close()

    def test_the_default_configuration_samples_every_venue_every_cycle(self):
        summaries = self.run_cycles(alternate=[], every=1)
        for summary in summaries:
            self.assertEqual(summary["skipped_venues"], [])
            self.assertIn("aster", summary["sampled_venues"])
        self.assertEqual(summaries[0]["alternate_every_cycles"], 1)

    def test_the_shipped_config_alternates_only_the_three_dex_venues(self):
        config = load_config(str(CONFIG))
        self.assertEqual(config["monitor"]["alternate_venues"],
                         ["aster", "hyperliquid", "lighter"])
        self.assertEqual(config["monitor"]["alternate_every_cycles"], 2)
        # The venues that share the execution allowlist must never be demoted:
        # an alternating schedule on okx would halve the samples of the only
        # venue this project can actually trade.
        for venue in config["execution_policy"]["allowlist"]["venues"]:
            self.assertNotIn(venue, config["monitor"]["alternate_venues"], venue)


class QuoteGridScopeTests(unittest.TestCase):
    """Only assets that could ever pair get a spot quote probe.

    Operator's instruction, 2026-09-17: "把没有永续腿的标的踢出报价网格 ——
    MIXUx / HKEXCx". They had pools but no perpetual anywhere, so their two CLI
    probes per cycle could never produce a pair — 25% of the waves the pairing
    budget has to survive, spent on rows that can only end in "缺侧".
    """

    def harness(self):
        config = offline_config()
        config["monitor"]["venues"] = ["gate"]
        config["monitor"]["alternate_venues"] = []
        config["monitor"]["alternate_every_cycles"] = 1
        # The shipped config carries the two orphan tokens; keep them, because
        # they are exactly what must not be probed.
        assets = [t["asset"] for t in config["tokens"]]
        self.assertIn("MIXUx", assets)
        self.assertIn("HKEXCx", assets)

        def client(venue, proxy):
            mock = MagicMock()
            # The exchange lists only XIAOMI — nothing for MIXUx/HKEXCx.
            mock.instruments.return_value = ([contract(venue=venue)], {"fixture": True}, NOW)
            mock.book.return_value = perp(venue=venue)
            mock.funding.return_value = {
                "rate": D("0"), "next_funding_ts_ms": NOW + 50000,
                "venue_ts_ms": NOW, "recv_ts_ms": NOW, "source": "fixture",
            }
            return mock

        refs = MagicMock()
        refs.token.return_value = {"asset": "XIAOMI", "price_usd": D("100"),
                                   "source": "fixture", "recv_ts_ms": NOW, "raw": {}}
        for method in ("pool", "stock_daily", "hkd_daily"):
            getattr(refs, method).return_value = {"source": "fixture", "raw": {},
                                                  "recv_ts_ms": NOW}
        quotes = MagicMock()
        quotes.quote.return_value = spot()
        quotes.quote_exact_out.return_value = spot()
        return config, client, refs, quotes

    def test_tokens_without_a_perpetual_are_not_quoted(self):
        config, client, refs, quotes = self.harness()
        with TemporaryDirectory() as directory, \
             patch("arb_monitor.apps.hk_monitor.CexPublicAdapter", side_effect=client), \
             patch("arb_monitor.apps.hk_monitor.ReferenceAdapter", return_value=refs), \
             patch("arb_monitor.apps.hk_monitor.OnchainOSQuoteAdapter", return_value=quotes), \
             patch("arb_monitor.apps.hk_monitor.fetch_issuer_assets", return_value=[]), \
             patch("arb_monitor.apps.hk_monitor.now_ms", return_value=NOW):
            monitor = HKMonitor(config, db=str(Path(directory) / "monitor.db"))
            try:
                result = monitor.cycle()
                # One buy probe + one sell probe, instead of the full grid
                # (8 assets would be 8 + 8 = 16).
                self.assertEqual(result["actual_quote_requests"], 2)
                self.assertEqual(quotes.quote_exact_out.call_count, 1)
                self.assertEqual(quotes.quote.call_count, 1)
                quoted = {r["asset"] for r in monitor.store.events("spot_quote")}
                self.assertEqual(quoted, {"XIAOMI"})
                # And the tokens that were skipped are precisely the ones with
                # no perpetual to pair against.
                self.assertEqual([t["asset"] for t in monitor.quotable_tokens()],
                                 ["XIAOMI"])
            finally:
                monitor.close()


class CatalogueRowFailureTests(unittest.TestCase):
    """One broken catalogue row must not erase a venue from the registry.

    Measured 2026-09-22: Gate lists ``SOXS_USDT`` with ``order_size_min = 0``.
    While the parser raised, the venue's parse failed, ``discover`` kept only
    the venues that succeeded, and Gate stopped being sampled — the summary
    still listed every other venue, so nothing looked wrong. The row is now
    dropped and reported, and the venue keeps its other 152 contracts.
    """

    def build(self):
        config = load_config(str(CONFIG))
        config["monitor"]["venues"] = ["gate", "okx"]
        config["monitor"]["alternate_venues"] = []
        config["monitor"]["alternate_every_cycles"] = 1

        def client(venue, proxy):
            mock = MagicMock()
            mock.instruments.return_value = (
                [contract(venue=venue, asset="XIAOMI",
                          instrument_id="XIAOMI_USDT")], {"fixture": True}, NOW)
            mock.skipped_specs = ([{"venue": venue, "instrument": "SOXS_USDT",
                                    "asset": "SOXS", "reason": "nonpositive_spec",
                                    "spec": {"min_contracts": "0"}}]
                                  if venue == "gate" else [])
            return mock

        return config, client

    def test_the_venue_survives_and_the_dropped_row_is_reported(self):
        config, client = self.build()
        with TemporaryDirectory() as directory, \
             patch("arb_monitor.apps.hk_monitor.CexPublicAdapter", side_effect=client), \
             patch("arb_monitor.apps.hk_monitor.ReferenceAdapter"), \
             patch("arb_monitor.apps.hk_monitor.OnchainOSQuoteAdapter"), \
             patch("arb_monitor.apps.hk_monitor.now_ms", return_value=NOW):
            monitor = HKMonitor(config, db=str(Path(directory) / "monitor.db"))
            try:
                errors = monitor.discover("fixture-run")
                # The venue is still there: a parse failure used to remove it.
                self.assertEqual({c.venue for c in monitor.contracts},
                                 {"gate", "okx"})
                self.assertEqual(errors, [])
                reported = [row for row in monitor.store.events("source_error")
                            if str(row.get("error", "")).startswith(
                                "contract_spec_skipped:")]
                self.assertEqual(len(reported), 1)
                self.assertEqual(reported[0]["venue"], "gate")
                self.assertIn("SOXS_USDT", reported[0]["error"])
                self.assertEqual(reported[0]["skipped"], 1)
            finally:
                monitor.close()


class PerpFetchShapeTests(unittest.TestCase):
    """Perp reads are concurrent, and funding is re-read on a slow clock.

    Operator's instruction, 2026-09-17: "把每合约的 depth/funding 并发发出" +
    "funding 按 N 轮一次取". Before this, each venue had one thread with
    depth-then-funding serial inside it (gate: 16 sequential calls, measured
    3.2s) and `evaluated_at` waits for the slowest venue — which aged every spot
    quote fetched alongside them.
    """

    def build(self, *, contracts_per_venue=3, funding_cycle=20, funding_fails=0):
        config = offline_config()
        config["monitor"]["venues"] = ["gate"]
        config["monitor"]["alternate_venues"] = []
        config["monitor"]["alternate_every_cycles"] = 1
        config["monitor"]["perp_workers"] = 8
        config["monitor"]["funding_refresh_cycles"] = funding_cycle
        assets = ["XIAOMI", "TENCENT", "POPMART"][:contracts_per_venue]
        state = {"in_flight": 0, "max_in_flight": 0, "depth": 0, "funding": 0,
                 "funding_calls": 0}
        guard = threading.Lock()

        def enter():
            with guard:
                state["in_flight"] += 1
                state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])

        def leave():
            with guard:
                state["in_flight"] -= 1

        def client(venue, proxy):
            mock = MagicMock()
            # One contract per asset with a *distinct* instrument id: funding is
            # tracked per `venue:instrument`, so three copies of the same id
            # would collapse into one entry and hide a missing retry.
            mock.instruments.return_value = (
                [contract(venue=venue, asset=a, instrument_id=f"{a}_USDT")
                 for a in assets], {"fixture": True}, NOW)

            def book(c):
                enter()
                try:
                    time.sleep(0.2)
                    state["depth"] += 1
                    # `perp()` is an OrderBook: the instrument field is `symbol`.
                    # Passing `asset=` raised here, which is how an earlier
                    # version of this test passed while producing no books.
                    return perp(venue=venue, symbol=f"{c.asset}_USDT")
                finally:
                    leave()

            def funding(c):
                enter()
                try:
                    time.sleep(0.2)
                    state["funding_calls"] += 1
                    if state["funding_calls"] <= funding_fails:
                        raise RuntimeError("funding unavailable")
                    state["funding"] += 1
                    return {"rate": D("0"), "next_funding_ts_ms": NOW + 50000,
                            "venue_ts_ms": NOW, "recv_ts_ms": NOW, "source": "fixture"}
                finally:
                    leave()

            mock.book.side_effect = book
            mock.funding.side_effect = funding
            return mock

        refs = MagicMock()
        refs.token.return_value = {"asset": "XIAOMI", "price_usd": D("100"),
                                   "source": "fixture", "recv_ts_ms": NOW, "raw": {}}
        for method in ("pool", "stock_daily", "hkd_daily"):
            getattr(refs, method).return_value = {"source": "fixture", "raw": {},
                                                  "recv_ts_ms": NOW}
        quotes = MagicMock()
        quotes.quote.return_value = spot()
        quotes.quote_exact_out.return_value = spot()
        return config, client, refs, quotes, state

    def cycles(self, *, count, **kwargs):
        config, client, refs, quotes, state = self.build(**kwargs)
        with TemporaryDirectory() as directory, \
             patch("arb_monitor.apps.hk_monitor.CexPublicAdapter", side_effect=client), \
             patch("arb_monitor.apps.hk_monitor.ReferenceAdapter", return_value=refs), \
             patch("arb_monitor.apps.hk_monitor.OnchainOSQuoteAdapter", return_value=quotes), \
             patch("arb_monitor.apps.hk_monitor.fetch_issuer_assets", return_value=[]), \
             patch("arb_monitor.apps.hk_monitor.now_ms", return_value=NOW):
            monitor = HKMonitor(config, db=str(Path(directory) / "monitor.db"))
            try:
                summaries = [monitor.cycle() for _ in range(count)]
                return summaries, state
            finally:
                monitor.close()

    def test_depth_reads_overlap_instead_of_running_one_at_a_time(self):
        summaries, state = self.cycles(count=1)
        # A time-based assertion was the wrong invariant the last time this
        # project tried it ("偶发失败" = a real bug, see the session log §14), so
        # this pins "how many were in flight at once" instead.
        self.assertGreaterEqual(state["max_in_flight"], 2)
        self.assertEqual(state["depth"], 3)
        # And the reads have to actually land: counting calls alone let an
        # earlier version of this test pass while every book raised.
        self.assertEqual(len(summaries[0]["evaluations"]), 3)

    def test_funding_is_read_once_and_then_left_alone_for_n_cycles(self):
        summaries, state = self.cycles(count=3, funding_cycle=20)
        # Three contracts, so three funding reads — all on the first cycle.
        self.assertEqual(state["funding_calls"], 3)
        self.assertEqual(state["depth"], 9)              # books every cycle
        self.assertEqual(summaries[0]["funding_venues_read"], ["gate"])
        self.assertEqual(summaries[1]["funding_venues_read"], [])
        self.assertEqual(summaries[1]["perp_jobs"], 3)   # depth only

    def test_a_failed_funding_read_is_retried_next_cycle(self):
        # "Could not read" must never be remembered as "read" — and it is tracked
        # per contract, so a sibling's success cannot cover for a failure.
        summaries, state = self.cycles(count=2, funding_cycle=20, funding_fails=1)
        self.assertEqual(state["funding_calls"], 4)  # 3 on cycle 1, 1 retry
        self.assertEqual(state["funding"], 3)
        self.assertEqual(summaries[1]["funding_venues_read"], ["gate"])

    def test_a_cadence_of_one_reads_funding_every_cycle(self):
        summaries, state = self.cycles(count=2, funding_cycle=1)
        self.assertEqual(state["funding_calls"], 6)  # 3 contracts × 2 cycles
        self.assertEqual(summaries[1]["funding_venues_read"], ["gate"])

    def test_the_shipped_config_carries_both_knobs(self):
        config = load_config(str(CONFIG))
        self.assertGreaterEqual(config["monitor"]["perp_workers"], 1)
        self.assertGreaterEqual(config["monitor"]["funding_refresh_cycles"], 1)


class StreamingEvaluationTests(unittest.TestCase):
    """Each pair is judged when its own two legs are in hand.

    Operator, 2026-09-17: "每条配对在自己两条腿都到位时就立刻评估…这样一条慢探针
    只作废它自己那一个配对，不会连累其它行". Before this, `evaluated_at` was the
    moment the *slowest probe of the whole grid* returned, so one 20-second probe
    aged every book and quote past the 15s budget and voided all 39 rows at once.
    """

    def build(self, slow_asset="XIAOMI", slow_seconds=0.6, book_error_for=()):
        config = offline_config()
        config["monitor"]["venues"] = ["gate"]
        config["monitor"]["alternate_venues"] = []
        config["monitor"]["alternate_every_cycles"] = 1
        assets = ["XIAOMI", "TENCENT", "POPMART"]

        def client(venue, proxy):
            mock = MagicMock()
            mock.instruments.return_value = (
                [contract(venue=venue, asset=a, instrument_id=f"{a}_USDT")
                 for a in assets], {"fixture": True}, NOW)

            def book(c):
                if c.asset in book_error_for:
                    raise RuntimeError("book unavailable")
                return perp(venue=venue, symbol=f"{c.asset}_USDT")

            mock.book.side_effect = book
            mock.funding.side_effect = lambda c: {
                "rate": D("0"), "next_funding_ts_ms": NOW + 50000,
                "venue_ts_ms": NOW, "recv_ts_ms": NOW, "source": "fixture"}
            return mock

        refs = MagicMock()
        refs.token.return_value = {"asset": "XIAOMI", "price_usd": D("100"),
                                   "source": "fixture", "recv_ts_ms": NOW, "raw": {}}
        for method in ("pool", "stock_daily", "hkd_daily"):
            getattr(refs, method).return_value = {"source": "fixture", "raw": {},
                                                  "recv_ts_ms": NOW}
        quotes = MagicMock()

        def exact_out(token, quote_token, shares):
            if token["asset"] == slow_asset:
                time.sleep(slow_seconds)
            return spot()

        quotes.quote_exact_out.side_effect = exact_out
        quotes.quote.return_value = spot()
        return config, client, refs, quotes

    def cycle_once(self, **kwargs):
        config, client, refs, quotes = self.build(**kwargs)
        with TemporaryDirectory() as directory, \
             patch("arb_monitor.apps.hk_monitor.CexPublicAdapter", side_effect=client), \
             patch("arb_monitor.apps.hk_monitor.ReferenceAdapter", return_value=refs), \
             patch("arb_monitor.apps.hk_monitor.OnchainOSQuoteAdapter", return_value=quotes), \
             patch("arb_monitor.apps.hk_monitor.fetch_issuer_assets", return_value=[]):
            monitor = HKMonitor(config, db=str(Path(directory) / "monitor.db"))
            try:
                monitor.cycle()
                return monitor.store.events("basis_evaluation")
            finally:
                monitor.close()

    def test_one_slow_probe_does_not_hold_back_the_other_pairs(self):
        # `now_ms` is deliberately NOT patched here: the claim under test is about
        # *when* each pair was judged, so the test needs a real clock.
        rows = self.cycle_once(slow_asset="XIAOMI", slow_seconds=0.6)
        seen = {r["instrument"]: r["evaluated_at_ms"] for r in rows}
        self.assertEqual(len(seen), 3)
        # The two fast pairs must be judged well before the slow one finishes;
        # under the old batch shape they would all share one timestamp.
        self.assertLess(seen["gate:TENCENT_USDT"], seen["gate:XIAOMI_USDT"])
        self.assertLess(seen["gate:POPMART_USDT"], seen["gate:XIAOMI_USDT"])
        self.assertGreaterEqual(seen["gate:XIAOMI_USDT"] - seen["gate:TENCENT_USDT"], 400)

    def test_a_failed_book_is_resolved_not_silently_dropped(self):
        # "We tried and the read failed" is a different fact from "we did not
        # sample this venue this cycle", and the two must not look alike.
        rows = self.cycle_once(book_error_for=("TENCENT",))
        by_instrument = {r["instrument"]: r for r in rows}
        self.assertIn("gate:TENCENT_USDT", by_instrument)
        self.assertIn("missing_or_stale_perp_book",
                      by_instrument["gate:TENCENT_USDT"]["rejections"])
        # No book means no basis: the row records the failure, it does not
        # invent a price.
        self.assertIsNone(by_instrument["gate:TENCENT_USDT"]["entry_basis_bps"])

    def test_every_sampled_pair_still_gets_exactly_one_row(self):
        rows = self.cycle_once()
        self.assertEqual(sorted(r["instrument"] for r in rows),
                         ["gate:POPMART_USDT", "gate:TENCENT_USDT", "gate:XIAOMI_USDT"])


class VenueScopeTests(unittest.TestCase):
    """Which venues this project reads is a scope decision, not a detail.

    Operator, 2026-09-22: "之后不需要再监控 gate 这个交易所，我决定不使用这个
    交易所". Pinned in a test because the failure mode is silent: nothing breaks if
    `gate` is added back to the list, the boards just start reporting rows for a
    venue this project has decided not to use — and the previous recommendation
    (`docs/venue-selection-2026-09-20.md` ranked Gate ≈ Bitget first) is right
    there to be copied from. The adapter and the reviewed `gate:` mapping blocks
    stay in the repo as evidence; this test is what says they are not monitored.
    """

    def test_gate_is_no_longer_monitored(self):
        venues = load_config(str(CONFIG))["monitor"]["venues"]
        self.assertNotIn("gate", venues)
        # 2026-09-23 operator: only OKX plus the X Layer spot leg. The other
        # adapters stay in the repo; they are not sampled.
        self.assertEqual(venues, ["okx"])


class TokenUniverseScopeTests(unittest.TestCase):
    def test_us_listings_are_neither_probed_nor_announced_as_hong_kong(self):
        config = load_config(str(CONFIG))
        # The shipped config now scans *and* adopts US (2026-09-22 operator:
        # "现在开始对这114个公司也进行监控"). This test pins the rule that did not
        # change: a market that is not in scope is neither probed nor announced,
        # and its rows stay out of the config file entirely. Narrowing the scan
        # scope therefore has to narrow the adopted set with it — `scan_scope`
        # refuses the half-switched state rather than adopting a market nobody
        # scanned (see `test_adopt_can_never_name_an_unscanned_market`).
        config["onchain"]["token_scan_countries"] = ["HK"]
        config["onchain"]["token_adopt_markets"] = ["HK"]
        hk = issuer_asset("TESTHKx", "Test HK Corp xStock", "1234", "HK",
                          "0x" + "1" * 40, "0x" + "2" * 40)
        us = issuer_asset("SKHYx", "SK hynix xStock", "SKHY", "US",
                          "0x" + "5" * 40, "0x" + "6" * 40)
        adapter = MagicMock()
        adapter.rpc = MagicMock()
        adapter.pool_states.return_value = [
            {"depth_usd": D("50000"), "price_usd": D("9.6"), "block_number": 7,
             "error": None},
        ]
        notifier = MagicMock()
        notifier.channel = "webhook"
        notifier.send.return_value = {"ok": True, "channel": "webhook"}
        pool = {"address": "0x" + "9" * 40, "quote_symbol": "USDG",
                "quote_address": "0x" + "8" * 40, "fee": 500}
        with TemporaryDirectory() as directory, \
             patch("arb_monitor.apps.hk_monitor.CexPublicAdapter"), \
             patch("arb_monitor.apps.hk_monitor.ReferenceAdapter"), \
             patch("arb_monitor.apps.hk_monitor.OnchainOSQuoteAdapter"), \
             patch("arb_monitor.apps.hk_monitor.XLayerOnchainAdapter",
                   return_value=adapter), \
             patch("arb_monitor.apps.hk_monitor.FeishuNotifier",
                   return_value=notifier), \
             patch("arb_monitor.apps.hk_monitor.fetch_issuer_assets",
                   return_value=[hk, us]), \
             patch("arb_monitor.apps.hk_monitor.discover_pools",
                   return_value=[pool]), \
             patch("arb_monitor.apps.hk_monitor.now_ms", return_value=NOW):
            monitor = HKMonitor(config, db=str(Path(directory) / "monitor.db"))
            try:
                monitor.discovered_config = str(Path(directory) / "discovered.toml")
                monitor.discovered_tokens = []
                result = monitor.refresh_token_universe("fixture-run", NOW)
                self.assertEqual(monitor.token_scan_countries, {"HK"})
                self.assertEqual([a["asset"] for a in result["added"]], ["TESTHKx"])
                self.assertEqual(result["skipped_out_of_scope"], {"US": 1})
                # The US name never reaches a pool probe, the config file or an
                # alert line: the whole batch is filtered before probing.
                text = notifier.send.call_args[0][0]
                self.assertIn("（港股 1234）", text)
                self.assertNotIn("SKHY", text)
                self.assertFalse((Path(directory) / "discovered.toml")
                                 .read_text().count("market = \"US\""))
            finally:
                monitor.close()

    def test_alert_reports_the_issuer_market_not_a_hard_coded_hk(self):
        entry = {
            "asset": "SKHYx", "name": "SK hynix", "hk_code": "SKHY",
            "market": "US", "symbol": "wSKHYx", "pool": "0x" + "9" * 40,
            "pools": [{"quote_symbol": "USDG"}], "depth_usd": "232182.19",
            "pool_price_usd": "178.36", "cex_match": "",
        }
        text = format_discovery_message([entry], ["HK"])
        self.assertIn("标的: SK hynix（美股 SKHY）代币 wSKHYx", text)
        self.assertIn("范围：港股", text)
        self.assertNotIn("HK SKHY", text)


class TokenUniversePersistenceTests(unittest.TestCase):
    def test_a_first_run_reports_no_scan_age_instead_of_the_process_age(self):
        """With nothing in the store there is no measurement to date.

        ``last_token_scan`` is the *clock*, reset to "now" at startup, so
        falling back to it printed "扫描数据 9 分钟前" for a funnel that had
        never been measured at all. The age belongs to a report; no report,
        no age.
        """
        config = offline_config()
        config["monitor"]["venues"] = ["gate"]
        client = MagicMock()
        client.return_value.instruments.return_value = ([contract()], {"fixture": True}, NOW)
        client.return_value.book.return_value = perp()
        client.return_value.funding.return_value = {
            "rate": D("0"), "next_funding_ts_ms": NOW + 50000,
            "venue_ts_ms": NOW, "recv_ts_ms": NOW, "source": "fixture"}
        refs = MagicMock()
        refs.token.return_value = {"asset": "XIAOMI", "price_usd": D("100"),
                                   "source": "fixture", "recv_ts_ms": NOW, "raw": {}}
        for method in ("pool", "stock_daily", "hkd_daily"):
            getattr(refs, method).return_value = {"source": "fixture", "raw": {},
                                                  "recv_ts_ms": NOW}
        quotes = MagicMock()
        quotes.quote.return_value = spot()
        quotes.quote_exact_out.return_value = spot()
        with TemporaryDirectory() as directory, \
             patch("arb_monitor.apps.hk_monitor.CexPublicAdapter", client), \
             patch("arb_monitor.apps.hk_monitor.ReferenceAdapter", return_value=refs), \
             patch("arb_monitor.apps.hk_monitor.OnchainOSQuoteAdapter", return_value=quotes), \
             patch("arb_monitor.apps.hk_monitor.fetch_issuer_assets", return_value=[]), \
             patch("arb_monitor.apps.hk_monitor.now_ms", return_value=NOW):
            monitor = HKMonitor(config, db=str(Path(directory) / "monitor.db"))
            try:
                scan = monitor.cycle()["token_universe_scan"]
                self.assertIsNone(scan["layers"])
                self.assertIsNone(scan["scan_age_ms"])
                self.assertFalse(scan["scan_fresh"])
            finally:
                monitor.close()

    def test_the_discovery_funnel_survives_a_restart(self):
        """A restart must not blank the board's funnel for 30 minutes.

        ``last_token_scan`` is reset to "now" at startup on purpose (a restart
        should not fire a 20-probe burst at the one shared local proxy), so the
        first cycle after every restart has no scan of its own. The board
        therefore re-reads the last completed one — measured 2026-09-22: after
        the restart that enabled US monitoring, the page read "本轮还没有跑过
        发行人列表扫描" and the funnel numbers the operator asked for (754 → 114
        → how many actually have a pool) were invisible.
        """
        from arb_monitor.apps.hk_monitor import market_coverage_html

        config = offline_config()
        layers = {"issuer_universe": {"HK": 79, "US": 754},
                  "gated_out": {"US": 640}, "probed": {"HK": 79, "US": 114},
                  "pairable_candidates": {"HK": 6, "US": 1},
                  "no_pool": {"US": 113}, "executable_allowlist": {"HK": 6}}
        with TemporaryDirectory() as directory:
            db = str(Path(directory) / "monitor.db")
            store = EventStore(db)
            # The funnel lives under its own event kind, written by
            # `refresh_token_universe` with the time it was measured. A
            # `run_summary` row (which is written every 60 s, funnel or not)
            # would be the wrong thing to read back: right after a restart the
            # newest summary is exactly the one without a scan in it.
            store.record_market_event("earlier-run", "token_universe_scan", {
                "scanned": True, "scanned_at_ms": 1_000, "new_listings": 0,
                "layers": layers, "perp_gate": {"okx_stock_perps": 181},
                "scope_countries": ["HK", "US"], "adopt_countries": ["HK", "US"],
                "report_only": [],
                "skipped": [{"symbol": "NVDAX", "market": "US",
                             "status": "no_pool"}]}, 1_000)
            # The trap this guards against: a newer summary with no funnel.
            store.record_market_event("later-run", "run_summary", {
                "run_id": "later-run",
                "token_universe_scan": {"layers": None, "new_listings": None}},
                2_000)
            store.close()
            with patch("arb_monitor.apps.hk_monitor.CexPublicAdapter"), \
                 patch("arb_monitor.apps.hk_monitor.ReferenceAdapter"), \
                 patch("arb_monitor.apps.hk_monitor.OnchainOSQuoteAdapter"), \
                 patch("arb_monitor.apps.hk_monitor.XLayerOnchainAdapter"):
                monitor = HKMonitor(config, db=db)
                try:
                    self.assertEqual((monitor.last_token_scan_report or {})
                                     .get("layers"), layers)
                    # The age printed is the scan's, not the process's: a
                    # restored funnel must not be dressed up as fresh.
                    self.assertEqual(monitor.last_token_scan_report_ms, 1_000)
                    self.assertNotEqual(monitor.last_token_scan, 1_000)
                    page = market_coverage_html({
                        "token_references": [],
                        "token_universe_scan": {
                            "layers": layers, "scan_age_ms": 600_000,
                            "interval_seconds": 1800}})
                    self.assertIn("美股：发行方 754 · 无 OKX 股票永续 640（未探针） · "
                                  "交集内 114 · 本轮新增有池候选 1 · "
                                  "探针无池 113", page)
                    # The assertion above is contiguous, so it also pins what
                    # is *absent*: this funnel was written before
                    # `probed_this_round` existed, and the round-scoped clause
                    # is left out rather than printed as a zero that would read
                    # like a measurement.
                    self.assertIn("扫描数据 10 分钟前", page)
                finally:
                    monitor.close()

    def test_the_writer_starts_from_what_is_already_on_disk(self):
        config = offline_config()
        with TemporaryDirectory() as directory, \
             patch("arb_monitor.apps.hk_monitor.CexPublicAdapter"), \
             patch("arb_monitor.apps.hk_monitor.ReferenceAdapter"), \
             patch("arb_monitor.apps.hk_monitor.OnchainOSQuoteAdapter"), \
             patch("arb_monitor.apps.hk_monitor.XLayerOnchainAdapter"):
            monitor = HKMonitor(config, db=str(Path(directory) / "monitor.db"))
            try:
                # Every entry the merged config carries is kept, so one later
                # batch can no longer drop an earlier process's discoveries.
                self.assertEqual(
                    sorted(t["asset"] for t in monitor.discovered_tokens),
                    sorted(t["asset"] for t in config["tokens"]
                           if t.get("auto_discovered")),
                )
            finally:
                monitor.close()


if __name__ == "__main__":
    unittest.main()
