"""US xStock rows: identity, evidence, sizing, liquidity state, discovery layers.

Everything here is hermetic. The one real file it reads is the repo's own
``config/hk_monitor.toml``, which existing tests already do; nothing touches a
network, a wallet or a venue.
"""
from __future__ import annotations

from dataclasses import replace
from contextlib import ExitStack
from concurrent.futures import Future
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from arb_monitor.apps.hk_monitor import (
    HKMonitor, POOL_READ_STALE, _market_short_label, _pool_state_label,
    load_config, mapping_from_token, promoted_exact_assets,
    quote_conversion_from_token, scan_scope, stablecoin_mid, token_listing_market,
)
from arb_monitor.core.cost_table import (
    CostTable, cost_table_from_tokens, costs_from_spec,
)
from arb_monitor.core.hk import (
    DEFAULT_MAX_PEG_DEVIATION_BPS, Contract, PairMapping, QuoteConversion,
    RoundTripCosts, SpotQuote,
)
from arb_monitor.core.hk_calendar import HK
from arb_monitor.core.liquidity_state import PoolObservation, classify_pool
from arb_monitor.core.markets import (
    UNKNOWN, US, describe_scope, market_label, normalize_market,
    validate_scan_countries,
)
from arb_monitor.core.models import Level, OrderBook
from arb_monitor.core.sizing import SizingInputs, plan_size
from arb_monitor.risk.hk_basis import BasisLimits, input_rejections
from arb_monitor.strategy.hk_basis import STRATEGY_PHASES, HKBasisStrategy
from arb_monitor.strategy.token_perp_basis import TokenPerpBasisStrategy

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/hk_monitor.toml"


def ms(day="2026-09-22", hour=10, minute=0) -> int:
    return int(datetime.fromisoformat(f"{day}T{hour:02}:{minute:02}:00")
               .replace(tzinfo=HK).timestamp()) * 1000


NOW = ms()


def aapl_contract(**kwargs) -> Contract:
    """OKX AAPL-USDT-SWAP as read on 2026-09-22: ctVal=1 ctMult=1 lotSz=0.01."""
    c = Contract(
        "okx", "AAPL-USDT-SWAP", "AAPL", "us_stock_candidate", "usd_linear",
        D("1"), D("0.01"), D("0.01"), D("5"), D("0.01"), True, "public:test",
        NOW, {},
    )
    return replace(c, **kwargs)


def aapl_token(**kwargs) -> dict:
    token = {
        "asset": "AAPL", "name": "Apple", "stock_symbol": "AAPL",
        "listing_country": "US", "symbol": "AAPLx",
        "address": "0x9d275685dc284c8eb1c79f6aba7a63dc75ec890a",
        "chain_id": "196", "decimals": 18, "mapping_verified": True,
        "trade_scan": True,
        "quote_conversion": {
            "quote_symbol": "USDG",
            "quote_address": "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8",
            "quote_to_usdt": "0.9998", "source": "fixture peg read",
            "observed_ts_ms": NOW - 1000, "valid_until_ms": NOW + 600_000,
            "peg_deviation_bps": "2", "verified": True,
        },
        "mappings": {
            "okx:AAPL-USDT-SWAP": {
                "shares_per_token": "1", "shares_per_perp_unit": "1",
                "quote_to_usdt": "0.9998", "perp_contract_multiplier": "1",
                "verified": True, "evidence": "fixture US mapping",
                "valid_until_ms": NOW + 600_000,
            },
        },
    }
    token.update(kwargs)
    return token


def spot(side="buy", price="200", quantity="3", **kwargs) -> SpotQuote:
    q = D(quantity)
    result = SpotQuote(
        "AAPL", side, "196", "0x" + "1" * 40, "0x" + "2" * 40, "USDG",
        q * D(price) if side == "buy" else q, q, q * D(price), "ok",
        NOW - 100, NOW - 50, NOW + 1000, "test_quote",
        routes=("verified_rfq",), gas_quote=D(".01"),
    )
    return replace(result, **kwargs)


def perp(bid="203", ask="204", size="100", **kwargs) -> OrderBook:
    return replace(OrderBook(
        "okx", "AAPL-USDT-SWAP",
        (Level(D(bid), D(size)),), (Level(D(ask), D(size)),),
        NOW - 30, NOW - 10, 123, "test_book",
    ), **kwargs)


def costs(**kwargs) -> RoundTripCosts:
    values = {"verified": True, "valid_until_ms": NOW + 10_000}
    values.update(kwargs)
    return RoundTripCosts(**values)


def us_mapping(conversion=True, **kwargs) -> PairMapping:
    fields = dict(
        asset="AAPL", shares_per_token=D("1"), shares_per_perp_unit=D("1"),
        quote_to_usdt=D("0.9998"), verified=True, evidence="fixture",
        valid_until_ms=NOW + 10_000, listing_market=US,
        underlying_symbol="AAPL", perp_contract_multiplier=D("1"),
        token_address="0x" + "1" * 40, wrapper_address="0x" + "2" * 40,
        quote_conversion=(QuoteConversion(
            quote_symbol="USDG", quote_address="0x" + "2" * 40,
            quote_to_usdt=D("0.9998"), source="fixture peg read",
            observed_ts_ms=NOW - 1000, valid_until_ms=NOW + 600_000,
            peg_deviation_bps=D("2"), verified=True) if conversion else None),
    )
    fields.update(kwargs)
    return PairMapping(**fields)


class ScanScopeTests(unittest.TestCase):
    def test_hk_and_us_are_both_scanable(self):
        self.assertEqual(validate_scan_countries(["HK", "US"]), ("HK", "US"))
        self.assertEqual(validate_scan_countries(["us", "hk", "HK"]), ("US", "HK"))

    def test_an_unattributable_or_unknown_market_is_refused(self):
        for bad in (["UNKNOWN"], ["GB"], []):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_scan_countries(bad)

    def test_adopt_must_be_inside_scan(self):
        scanned, adopted = scan_scope({"token_scan_countries": ["HK", "US"],
                                       "token_adopt_markets": ["HK"]})
        self.assertEqual(scanned, ("HK", "US"))
        self.assertEqual(adopted, ("HK",))
        with self.assertRaises(ValueError):
            scan_scope({"token_scan_countries": ["HK"],
                        "token_adopt_markets": ["US"]})
        with self.assertRaises(ValueError):
            scan_scope({"token_scan_countries": ["HK", "US"],
                        "token_adopt_markets": ["GB"]})

    def test_defaults_keep_the_historical_hong_kong_scope(self):
        self.assertEqual(scan_scope({}), (("HK",), ("HK",)))

    def test_labels_say_which_market(self):
        self.assertEqual(market_label(US, "AAPL"), "美股 AAPL")
        self.assertEqual(market_label("HK", "1810"), "港股 1810")
        self.assertIn("上市地未标注", market_label(UNKNOWN, "XRX"))
        self.assertEqual(describe_scope(["HK", "US"]), "港股/美股")
        self.assertEqual(_market_short_label("US"), "美股")
        self.assertEqual(_market_short_label(UNKNOWN), "上市地未知")


class TokenMarketAttributionTests(unittest.TestCase):
    def test_legacy_hk_block_is_hk(self):
        self.assertEqual(
            token_listing_market({"asset": "XIAOMI", "hk_code": "1810"}), "HK")

    def test_us_block_is_us(self):
        self.assertEqual(
            token_listing_market({"asset": "AAPL", "listing_country": "US"}), "US")

    def test_explicit_country_beats_the_numeric_code(self):
        self.assertEqual(
            token_listing_market({"listing_country": "US", "hk_code": "1810"}), "US")

    def test_a_row_with_neither_is_unknown_and_never_guessed(self):
        self.assertEqual(
            token_listing_market({"asset": "AAPL", "stock_symbol": "AAPL"}),
            "UNKNOWN")

    def test_every_configured_token_resolves_to_a_real_market(self):
        config = load_config(str(CONFIG))
        markets = {t["asset"]: token_listing_market(t) for t in config["tokens"]}
        self.assertEqual(markets["XIAOMI"], "HK")
        self.assertEqual(markets["AAPL"], "US")
        self.assertEqual(markets["GOOGL"], "US")
        self.assertNotIn("UNKNOWN", set(markets.values()))


class AppleIdentityAndEvidenceTests(unittest.TestCase):
    def test_mapping_from_token_carries_identity_and_the_observed_rate(self):
        mapping = mapping_from_token(aapl_token(), aapl_contract())
        self.assertEqual(mapping.listing_market, "US")
        self.assertEqual(mapping.underlying_symbol, "AAPL")
        self.assertEqual(mapping.perp_contract_multiplier, D("1"))
        # The spot leg's rate is the observation, not the 1.0 default.
        self.assertEqual(mapping.quote_to_usdt, D("0.9998"))
        self.assertEqual(mapping.quote_conversion.quote_symbol, "USDG")
        self.assertTrue(mapping.valid(NOW))

    def test_missing_conversion_block_leaves_the_rate_unknown(self):
        token = aapl_token(quote_conversion={})
        token["mappings"]["okx:AAPL-USDT-SWAP"].pop("quote_to_usdt")
        mapping = mapping_from_token(token, aapl_contract())
        self.assertIsNone(mapping.quote_to_usdt)
        self.assertIsNone(quote_conversion_from_token(token))
        self.assertFalse(mapping.valid(NOW))

    def test_a_contract_size_the_catalogue_disagrees_with_is_refused(self):
        mapping = us_mapping(perp_contract_multiplier=D("0.1"))
        reasons = input_rejections(
            aapl_contract(), mapping, costs(), spot(), spot("sell", "199"),
            perp(), BasisLimits(), NOW, listing_market=US,
            require_quote_conversion=True)
        self.assertIn("perp_contract_size_mismatch", reasons)

    def test_the_same_contract_size_passes(self):
        reasons = input_rejections(
            aapl_contract(), us_mapping(), costs(), spot(), spot("sell", "199"),
            perp(), BasisLimits(), NOW, listing_market=US,
            require_quote_conversion=True)
        self.assertEqual(reasons, [])

    def test_a_us_row_without_a_conversion_observation_is_refused(self):
        reasons = input_rejections(
            aapl_contract(), us_mapping(conversion=False), costs(), spot(),
            spot("sell", "199"), perp(), BasisLimits(), NOW,
            listing_market=US, require_quote_conversion=True)
        self.assertIn("quote_conversion_missing", reasons)

    def test_a_stale_or_depegged_conversion_is_refused(self):
        stale = replace(us_mapping().quote_conversion,
                        observed_ts_ms=NOW - 10 * 3600 * 1000)
        reasons = input_rejections(
            aapl_contract(), us_mapping(quote_conversion=stale), costs(), spot(),
            spot("sell", "199"), perp(), BasisLimits(), NOW,
            listing_market=US, require_quote_conversion=True)
        self.assertIn("quote_conversion_stale", reasons)
        depegged = replace(us_mapping().quote_conversion,
                           peg_deviation_bps=DEFAULT_MAX_PEG_DEVIATION_BPS + D("1"))
        reasons = input_rejections(
            aapl_contract(), us_mapping(quote_conversion=depegged), costs(), spot(),
            spot("sell", "199"), perp(), BasisLimits(), NOW,
            listing_market=US, require_quote_conversion=True)
        self.assertIn("quote_conversion_depeg", reasons)

    def test_an_unattributable_market_is_refused(self):
        reasons = input_rejections(
            aapl_contract(), replace(us_mapping(), listing_market=UNKNOWN),
            costs(), spot(), spot("sell", "199"), perp(), BasisLimits(), NOW,
            listing_market=UNKNOWN, require_quote_conversion=True)
        self.assertIn("unknown_listing_market", reasons)


class CalendarIsNeverAGateTests(unittest.TestCase):
    def _strategy(self, cls, **kwargs):
        defaults = dict(
            contract=aapl_contract(), mapping=us_mapping(), buy=spot(),
            sell=spot("sell", "199"), costs=costs(), limits=BasisLimits(),
            target_exit_basis_bps=D("3"), phase="closed")
        defaults.update(kwargs)
        return cls(**defaults)

    def test_us_rows_have_no_calendar_gate(self):
        strategy = self._strategy(TokenPerpBasisStrategy, listing_market=US)
        self.assertFalse(strategy.identity()["calendar_phase_is_a_gate"])
        self.assertNotIn("outside_strategy_window",
                         strategy.evaluate([perp()], NOW)["rejections"])

    def test_a_us_row_evaluates_in_every_recorded_phase(self):
        for phase in sorted(STRATEGY_PHASES):
            with self.subTest(phase=phase):
                strategy = self._strategy(TokenPerpBasisStrategy,
                                          listing_market=US, phase=phase,
                                          require_quote_conversion=True)
                self.assertNotIn("outside_strategy_window",
                                 strategy.evaluate([perp()], NOW)["rejections"])

    def test_a_us_row_does_not_consult_the_calendar_at_all(self):
        # A US row has no gate, so even an unreadable phase is only a report
        # label. The HK gate is what still fails closed on a broken calendar.
        strategy = self._strategy(TokenPerpBasisStrategy, listing_market=US,
                                  phase="calendar_unknown")
        self.assertNotIn("outside_strategy_window",
                         strategy.evaluate([perp()], NOW)["rejections"])
        hk_row = dict(
            contract=aapl_contract(), mapping=replace(us_mapping(),
                                                      listing_market="HK",
                                                      quote_conversion=None),
            buy=spot(), sell=spot("sell", "199"), costs=costs(),
            limits=BasisLimits(), target_exit_basis_bps=D("3"),
            phase="calendar_unknown")
        self.assertIn("outside_strategy_window",
                      HKBasisStrategy(**hk_row).evaluate([perp()], NOW)["rejections"])

    def test_hong_kong_rows_keep_their_reviewed_defaults(self):
        hk_contract = aapl_contract(instrument_id="XIAOMI-USDT-SWAP", asset="XIAOMI")
        hk_mapping = replace(us_mapping(), asset="XIAOMI", listing_market="HK",
                             quote_conversion=None)
        buy = replace(spot(), asset="XIAOMI")
        sell = replace(spot("sell", "199"), asset="XIAOMI")
        book = replace(perp(), symbol="XIAOMI-USDT-SWAP")
        strategy = HKBasisStrategy(
            hk_contract, hk_mapping, buy, sell, costs(), BasisLimits(),
            D("3"), "closed")
        self.assertTrue(strategy.identity()["calendar_phase_is_a_gate"])
        self.assertFalse(strategy.require_quote_conversion)
        self.assertNotIn("outside_strategy_window",
                         strategy.evaluate([book], NOW)["rejections"])


class ResidualCashCapTests(unittest.TestCase):
    def _evaluate(self, quantity):
        buy = spot(quantity=quantity, price="200")
        sell = spot("sell", "199", quantity)
        strategy = TokenPerpBasisStrategy(
            contract=aapl_contract(), mapping=us_mapping(), buy=buy, sell=sell,
            costs=costs(), limits=BasisLimits(), target_exit_basis_bps=D("3"),
            phase="closed", listing_market=US, require_quote_conversion=True)
        return strategy.evaluate([perp()], NOW)

    def test_long_dust_under_half_a_dollar_is_not_refused(self):
        # 1.002 tokens floors to 1.00 contract: ~20 bps, about 0.40 USDT.
        result = self._evaluate("1.002")
        self.assertNotIn("exact_hedge_required_for_intent", result["rejections"])
        self.assertNotIn("contract_rounding_leaves_excess_directional_exposure",
                         result["rejections"])
        self.assertLessEqual(result["residual_quote_usdt"], D("0.5"))

    def test_long_dust_over_half_a_dollar_is_still_refused(self):
        # 1.004 tokens floors to 1.00 contract: about 0.80 USDT.
        result = self._evaluate("1.004")
        self.assertIn("exact_hedge_required_for_intent", result["rejections"])
        self.assertGreater(result["residual_quote_usdt"], D("0.5"))


class SizingTests(unittest.TestCase):
    def inputs(self, **kwargs) -> SizingInputs:
        values = dict(
            token_quantity=D("3"), shares_per_token=D("1"),
            shares_per_perp_unit=D("1"), contract_multiplier=D("1"),
            lot_size=D("0.01"), min_contracts=D("0.01"), min_notional=D("5"),
            perp_price=D("203"), spot_open_usdt=D("600"),
            perp_open_usdt=D("609"), perp_close_usdt=D("609"),
            wallet_balance_usdt=D("10000"), margin_allocation_usdt=D("5000"),
            max_unhedged_bps=D("5"))
        values.update(kwargs)
        return SizingInputs(**values)

    def test_one_share_is_one_contract_when_every_ratio_is_one(self):
        result = plan_size(self.inputs())
        self.assertEqual(result.contracts, D("3"))
        self.assertEqual(result.residual_tokens, D("0"))
        self.assertEqual(result.residual_delta_bps, D("0"))
        self.assertTrue(result.ok)

    def test_a_lot_grid_leaves_a_named_residual(self):
        result = plan_size(self.inputs(
            token_quantity=D("3.7"), lot_size=D("1"), min_contracts=D("1")))
        self.assertEqual(result.contracts, D("3"))
        self.assertEqual(result.residual_tokens, D("0.7"))
        self.assertAlmostEqual(float(result.residual_delta_bps), 1891.89, places=1)
        self.assertIn("contract_rounding_leaves_excess_directional_exposure",
                      result.reasons)

    def test_the_minimum_executable_size_respects_the_notional_floor(self):
        result = plan_size(self.inputs(perp_price=D("2"), min_notional=D("5"),
                                       perp_open_usdt=D("5"),
                                       perp_close_usdt=D("5")))
        # 5 USDT / 2 per contract = 2.5 contracts, which is a legal 0.01 lot.
        self.assertEqual(result.min_executable_contracts, D("2.5"))
        self.assertEqual(result.min_executable_notional_usdt, D("5"))

    def test_a_smaller_ticket_than_the_floor_is_refused(self):
        result = plan_size(self.inputs(perp_open_usdt=D("2"),
                                       perp_close_usdt=D("2")))
        self.assertIn("below_min_notional", result.reasons)

    def test_the_wallet_and_the_margin_split_are_both_reported(self):
        result = plan_size(self.inputs(wallet_balance_usdt=D("500")))
        self.assertTrue(result.wallet_limited)
        self.assertIn("wallet_balance_below_spot_notional", result.reasons)
        result = plan_size(self.inputs(margin_allocation_usdt=D("100")))
        self.assertTrue(result.margin_limited)
        self.assertIn("margin_allocation_below_perp_notional", result.reasons)

    def test_an_unread_wallet_is_unknown_not_a_pass(self):
        result = plan_size(self.inputs(wallet_balance_usdt=None))
        self.assertFalse(result.wallet_limited)
        self.assertNotIn("wallet_balance_below_spot_notional", result.reasons)

    def test_a_non_positive_contract_size_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            plan_size(self.inputs(contract_multiplier=D("0")))

    def test_the_strategy_reports_the_plan_it_used(self):
        strategy = TokenPerpBasisStrategy(
            contract=aapl_contract(), mapping=us_mapping(), buy=spot(),
            sell=spot("sell", "199"), costs=costs(), limits=BasisLimits(),
            target_exit_basis_bps=D("3"), phase="continuous_am",
            listing_market=US, wallet_balance_usdt=D("10000"))
        result = strategy.evaluate([perp()], NOW)
        self.assertEqual(result["contract_spec"]["venue"], "okx")
        self.assertEqual(result["contract_spec"]["instrument"], "AAPL-USDT-SWAP")
        self.assertEqual(result["listing_market"], "US")
        self.assertEqual(result["underlying_symbol"], "AAPL")
        self.assertEqual(result["sizing_rejections"], [])
        # 5 USDT minimum notional / 203 per contract = 0.0246, rounded up to the
        # 0.01 lot grid. Passing the *notional* of the walked levels (609) here
        # would have said 0.01 and understated the venue's own floor.
        self.assertEqual(result["min_executable_contracts"], D("0.03"))
        self.assertEqual(result["min_executable_notional_usdt"], D("6.09"))
        self.assertFalse(result["wallet_limited"])

    def test_the_notional_floor_scales_with_the_venue_size_not_the_ticket(self):
        # A 600 USDT minimum notional against a 203 contract is 2.96 contracts.
        # Reading the walked levels' 609 notional as a *price* would have said
        # 0.99, i.e. a floor 3x too small at this size.
        contract = aapl_contract(min_notional=D("600"))
        strategy = TokenPerpBasisStrategy(
            contract=contract, mapping=us_mapping(), buy=spot(),
            sell=spot("sell", "199"), costs=costs(), limits=BasisLimits(),
            target_exit_basis_bps=D("3"), phase="continuous_am",
            listing_market=US)
        result = strategy.evaluate([perp()], NOW)
        self.assertEqual(result["min_executable_contracts"], D("2.96"))
        self.assertEqual(result["min_executable_notional_usdt"], D("600.88"))


class PoolStateTests(unittest.TestCase):
    def classify(self, **kwargs):
        values = dict(asset="AAPL", pool_address="0x" + "9" * 40,
                      liquidity=D("1000"), depth_usd=D("50000"),
                      slot0_price_usd=D("200"), exact_in_quote_ts_ms=NOW - 1000,
                      exact_in_quote_usdt=D("600"), quote_conversion_ok=True)
        values.update(kwargs)
        return classify_pool(PoolObservation(**values), now_ms=NOW,
                             min_depth_usd=D("1000"), max_quote_age_ms=15_000)

    def test_no_pool_is_not_a_price(self):
        self.assertEqual(self.classify(pool_address="", depth_usd=None).state,
                         "no_pool")

    def test_an_initialised_but_unfunded_pool_is_empty(self):
        verdict = self.classify(liquidity=D("0"), depth_usd=D("0"))
        self.assertEqual(verdict.state, "empty_pool")
        self.assertFalse(verdict.executable)

    def test_a_dust_pool_says_slot0_is_not_an_execution_price(self):
        verdict = self.classify(liquidity=D("1"), depth_usd=D("3"),
                                slot0_price_usd=D("178.36"))
        self.assertEqual(verdict.state, "pool_without_liquidity")
        self.assertIn("is not an execution price", verdict.detail)

    def test_prints_without_an_exact_in_quote_are_not_executable(self):
        verdict = self.classify(exact_in_quote_ts_ms=0, exact_in_quote_usdt=None,
                                last_print_ts_ms=NOW - 60_000)
        self.assertEqual(verdict.state, "prints_without_exact_in_quote")
        self.assertFalse(verdict.executable)

    def test_a_stale_exact_in_quote_is_unusable(self):
        verdict = self.classify(exact_in_quote_ts_ms=NOW - 60_000)
        self.assertEqual(verdict.state, "quote_unusable")
        self.assertIn("exact_in_quote_stale", verdict.reasons)

    def test_an_untrusted_quote_token_is_unusable(self):
        verdict = self.classify(quote_conversion_ok=False,
                                quote_conversion_reason="quote_conversion_depeg")
        self.assertEqual(verdict.state, "quote_unusable")
        self.assertIn("quote_conversion_depeg", verdict.reasons)

    def test_only_a_fresh_exact_in_quote_is_executable(self):
        verdict = self.classify()
        self.assertEqual(verdict.state, "exact_in_quote_available")
        self.assertTrue(verdict.executable)
        self.assertEqual(verdict.reasons, ())

    def test_an_unreadable_pool_is_no_pool_not_a_zero_price(self):
        verdict = self.classify(error="incomplete_rpc_reply")
        self.assertEqual(verdict.state, "no_pool")
        self.assertIn("pool_state_unreadable", verdict.reasons)

    def test_the_state_label_carries_the_reason(self):
        self.assertEqual(_pool_state_label("empty_pool", ["empty_pool"]),
                         "空池（empty_pool）")
        self.assertEqual(_pool_state_label(None), "—")


class MonitorPoolStateTests(unittest.TestCase):
    """The monitor's own plumbing over the five states."""

    def monitor(self, directory):
        config = load_config(str(CONFIG))
        with patch("arb_monitor.apps.hk_monitor.CexPublicAdapter"), \
             patch("arb_monitor.apps.hk_monitor.ReferenceAdapter"), \
             patch("arb_monitor.apps.hk_monitor.OnchainOSQuoteAdapter"), \
             patch("arb_monitor.apps.hk_monitor.XLayerOnchainAdapter"), \
             patch("arb_monitor.apps.hk_monitor.FeishuNotifier"):
            return HKMonitor(config, db=str(Path(directory) / "monitor.db"))

    def pool_row(self, **kwargs):
        # Real pool rows come straight from the configured pool block, so they
        # carry the stablecoin the pool actually pays in. The conversion check
        # needs it: without it we cannot tell which stablecoin a rate was read
        # for, and that is a refusal rather than an assumption.
        row = {"address": "0x" + "9" * 40, "depth_usd": D("50000"),
               "liquidity": 10**18, "price_usd": D("200"),
               "quote_address": aapl_token()["quote_conversion"]["quote_address"],
               "quote_symbol": "USDG",
               "recv_ts_ms": NOW - 1000, "error": None}
        row.update(kwargs)
        return row

    def test_an_empty_pool_state_is_empty_not_a_price(self):
        with TemporaryDirectory() as directory:
            monitor = self.monitor(directory)
            try:
                state = monitor.pool_state(
                    "AAPL", aapl_token(),
                    [self.pool_row(depth_usd=D("0"), liquidity=0)], {}, NOW)
                self.assertEqual(state["state"], "empty_pool")
                self.assertFalse(state["executable"])
            finally:
                monitor.close()

    def test_an_uninitialised_pool_reads_as_empty_not_unreadable(self):
        with TemporaryDirectory() as directory:
            monitor = self.monitor(directory)
            try:
                state = monitor.pool_state(
                    "AAPL", aapl_token(),
                    [self.pool_row(depth_usd=None, liquidity=0, price_usd=None,
                                   error="pool_not_initialized")], {}, NOW)
                self.assertEqual(state["state"], "empty_pool")
            finally:
                monitor.close()

    def test_a_us_pool_without_a_conversion_cannot_be_executable(self):
        with TemporaryDirectory() as directory:
            monitor = self.monitor(directory)
            try:
                state = monitor.pool_state("AAPL", aapl_token(quote_conversion={}),
                                           [self.pool_row()], {}, NOW)
                self.assertFalse(state["quote_conversion_ok"])
                self.assertEqual(state["state"], "quote_unusable")
                self.assertIn("quote_conversion_missing",
                              state["quote_conversion_reason"])
            finally:
                monitor.close()

    def test_a_conversion_for_another_stablecoin_says_so(self):
        with TemporaryDirectory() as directory:
            monitor = self.monitor(directory)
            try:
                state = monitor.pool_state(
                    "AAPL", aapl_token(), [self.pool_row()],
                    {}, NOW)
                self.assertTrue(state["quote_conversion_ok"])
                other = aapl_token()
                other["quote_conversion"] = dict(other["quote_conversion"],
                                                 quote_address="0x" + "f" * 40)
                state = monitor.pool_state("AAPL", other, [self.pool_row()],
                                           {}, NOW)
                self.assertEqual(state["quote_conversion_reason"],
                                 "quote_conversion_wrong_token")
            finally:
                monitor.close()

    def test_a_hong_kong_row_keeps_its_reviewed_quote_to_usdt(self):
        token = {"asset": "XIAOMI", "hk_code": "1810", "symbol": "wXIAOx",
                 "address": "0x" + "1" * 40,
                 "mappings": {"okx:XIAOMI-USDT-SWAP": {"quote_to_usdt": "1"}}}
        with TemporaryDirectory() as directory:
            monitor = self.monitor(directory)
            try:
                state = monitor.pool_state("XIAOMI", token,
                                           [self.pool_row(price_usd=D("3.35"))],
                                           {}, NOW)
                self.assertTrue(state["quote_conversion_ok"])
            finally:
                monitor.close()

    def test_an_hk_row_without_a_reviewed_rate_says_so(self):
        token = {"asset": "XIAOMI", "hk_code": "1810"}
        with TemporaryDirectory() as directory:
            monitor = self.monitor(directory)
            try:
                state = monitor.pool_state("XIAOMI", token, [self.pool_row()],
                                           {}, NOW)
                self.assertFalse(state["quote_conversion_ok"])
                self.assertEqual(state["quote_conversion_reason"],
                                 "no_reviewed_quote_to_usdt")
            finally:
                monitor.close()

    def test_the_last_exact_in_quote_is_what_makes_a_pool_executable(self):
        with TemporaryDirectory() as directory:
            monitor = self.monitor(directory)
            try:
                before = monitor.pool_state("AAPL", aapl_token(),
                                            [self.pool_row()], {}, NOW)
                self.assertEqual(before["state"], "prints_without_exact_in_quote")
                monitor.last_exact_in_quote["AAPL"] = {
                    "observed_ts_ms": NOW - 1000, "quote_usdt": D("600")}
                after = monitor.pool_state("AAPL", aapl_token(),
                                           [self.pool_row()], {}, NOW)
                self.assertEqual(after["state"], "exact_in_quote_available")
                self.assertTrue(after["executable"])
            finally:
                monitor.close()

    def test_not_sampled_this_cycle_is_not_reported_as_no_pool(self):
        # The on-chain refresh is asynchronous and lands every few minutes
        # (measured 2026-09-22: ~3 min) while a read stops counting after
        # `pool_state_max_age_ms` (120 s). Several cycles in a row therefore
        # have no *fresh* row for a name that plainly has a pool. Printing
        # "无池" there would turn "we have not looked yet" into "there is
        # nothing there" — the two must stay distinguishable.
        with TemporaryDirectory() as directory:
            monitor = self.monitor(directory)
            try:
                stale = [self.pool_row(recv_ts_ms=NOW - 300_000)]
                state = monitor.pool_state("AAPL", aapl_token(), [], {}, NOW,
                                           unsampled=stale)
                self.assertEqual(state["state"], POOL_READ_STALE)
                self.assertEqual(state["state"], "pool_read_stale")
                self.assertFalse(state["executable"])
                self.assertIn("pool_read_not_sampled_this_cycle",
                              state["reasons"])
                # The age of the last read is carried, so the board can be
                # checked without guessing how stale "stale" is.
                self.assertIn("300000ms", state["detail"])
                # The last read stays visible. It is still not an order.
                self.assertEqual(state["slot0_price_usd"], D("200"))
                self.assertEqual(_pool_state_label(state["state"], state["reasons"]),
                                 "本轮未采样（pool_read_not_sampled_this_cycle）")
            finally:
                monitor.close()

    def test_a_token_with_no_configured_pool_is_still_no_pool(self):
        # The "not sampled" state must not swallow the genuine case: a token
        # with no configured pool is `no_pool` even when the caller hands over
        # an empty read set.
        with TemporaryDirectory() as directory:
            monitor = self.monitor(directory)
            try:
                state = monitor.pool_state("AAPL", aapl_token(), [], {}, NOW,
                                           unsampled=None)
                self.assertEqual(state["state"], "no_pool")
            finally:
                monitor.close()


def issuer_asset(symbol, name, code, country, native, wrapper) -> dict:
    """One row of the issuer's asset list, as ``fetch_issuer_assets`` returns it."""
    return {"symbol": symbol, "name": name, "hk_code": code,
            "listing_country": country, "market": country or "UNKNOWN",
            "native_address": native, "wrapper_address": wrapper}


def okx_stock_perp(ticker, *, category="3") -> dict:
    """One row of ``fetch_okx_stock_perps``: OKX's own instrument row."""
    return {"instId": f"{ticker}-USDT-SWAP", "ctVal": "1", "ctValCcy": ticker,
            "ctMult": "1", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.01",
            "instCategory": category, "state": "live", "settleCcy": "USDT"}


class DiscoveryLayerTests(unittest.TestCase):
    """Layer 1 (issuer universe) and layer 2 (pairable) never touch layer 3.

    Scanning a market and adopting it are two different authorisations. These
    tests pin the boundary: with ``token_scan_countries = ["HK", "US"]`` and
    ``token_adopt_markets = ["HK"]``, a US listing with a deep pool and a real
    OKX perp is *reported in full* and hot-added to nothing — no config file,
    no monitoring set, no alert.
    """

    def run_scan(self, directory, assets, *, scanned, adopted, pools=None,
                 cap=None, contracts=(), perps=None, gated_markets=None):
        config = load_config(str(CONFIG))
        config["onchain"]["token_scan_countries"] = list(scanned)
        config["onchain"]["token_adopt_markets"] = list(adopted)
        if gated_markets is not None:
            config["onchain"]["token_scan_perp_gate_markets"] = list(gated_markets)
        if cap is not None:
            config["onchain"]["token_scan_max_probes_per_round"] = cap
        adapter = MagicMock()
        adapter.rpc = MagicMock()
        adapter.pool_states.return_value = [
            {"depth_usd": D("50000"), "price_usd": D("9.6"), "block_number": 7,
             "error": None},
        ]
        notifier = MagicMock()
        notifier.channel = "webhook"
        notifier.send.return_value = {"ok": True, "channel": "webhook"}
        pool = pools if pools is not None else [
            {"address": "0x" + "9" * 40, "quote_symbol": "USDG",
             "quote_address": "0x" + "8" * 40, "fee": 500}]
        # The probe gate reads OKX's own stock-perp list. Stubbed here so the
        # test states the intersection it depends on instead of the network.
        if perps is None:
            perps = {"AAPL": okx_stock_perp("AAPL")}
        with patch("arb_monitor.apps.hk_monitor.CexPublicAdapter"), \
             patch("arb_monitor.apps.hk_monitor.ReferenceAdapter"), \
             patch("arb_monitor.apps.hk_monitor.OnchainOSQuoteAdapter"), \
             patch("arb_monitor.apps.hk_monitor.XLayerOnchainAdapter",
                   return_value=adapter), \
             patch("arb_monitor.apps.hk_monitor.FeishuNotifier",
                   return_value=notifier), \
             patch("arb_monitor.apps.hk_monitor.fetch_issuer_assets",
                   return_value=assets), \
             patch("arb_monitor.apps.hk_monitor.fetch_okx_stock_perps",
                   return_value=perps), \
             patch("arb_monitor.apps.hk_monitor.discover_pools",
                   return_value=pool), \
             patch("arb_monitor.apps.hk_monitor.probe_pools_cheap",
                   return_value=pool), \
             patch("arb_monitor.apps.hk_monitor.now_ms", return_value=NOW):
            monitor = HKMonitor(config, db=str(Path(directory) / "monitor.db"))
            try:
                monitor.discovered_config = str(Path(directory) / "discovered.toml")
                # The live discovered file is production state. A scan test
                # has to start empty, or yesterday's US rows look like this
                # scan wrote them.
                monitor.discovered_tokens = []
                monitor.contracts = list(contracts)
                result = monitor.refresh_token_universe("fixture-run", NOW)
                written = (Path(directory) / "discovered.toml")
                return result, monitor, notifier, (
                    written.read_text() if written.exists() else None)
            finally:
                monitor.close()

    def us_asset(self, symbol="AAPLx", native=None, wrapper=None):
        return issuer_asset(symbol, symbol.replace("x", "") + " xStock", "AAPL",
                            "US", native or "0x" + "5" * 40,
                            wrapper or "0x" + "6" * 40)

    def test_a_scan_is_written_where_a_restart_can_read_it_back(self):
        """The funnel has to survive a restart without waiting 30 minutes.

        `run_summary` is written every 60 s and the scan every 30 min, so the
        newest summary after a restart is exactly the one with no funnel. The
        scan therefore writes its own kind, stamped with the time it was
        measured, and the next process restores from that.
        """
        from arb_monitor.ledger.store import EventStore

        with TemporaryDirectory() as directory:
            db = str(Path(directory) / "monitor.db")
            hk = issuer_asset("TESTHKx", "Test HK Corp xStock", "1234", "HK",
                              "0x" + "1" * 40, "0x" + "2" * 40)
            self.run_scan(directory, [hk, self.us_asset()], scanned=["HK", "US"],
                          adopted=["HK", "US"], contracts=[aapl_contract()])
            store = EventStore(db)
            try:
                event = store.latest_event("token_universe_scan") or {}
            finally:
                store.close()
            self.assertEqual(event.get("scanned_at_ms"), NOW)
            self.assertEqual(event.get("scope_countries"), ["HK", "US"])
            self.assertEqual(
                (event.get("layers") or {}).get("issuer_universe"), {"HK": 1, "US": 1})
            self.assertEqual((event.get("perp_gate") or {}).get("okx_stock_perps"), 1)
            # And the next process reads it back instead of showing "—".
            config = load_config(str(CONFIG))
            with patch("arb_monitor.apps.hk_monitor.CexPublicAdapter"), \
                 patch("arb_monitor.apps.hk_monitor.ReferenceAdapter"), \
                 patch("arb_monitor.apps.hk_monitor.OnchainOSQuoteAdapter"), \
                 patch("arb_monitor.apps.hk_monitor.XLayerOnchainAdapter"), \
                 patch("arb_monitor.apps.hk_monitor.now_ms", return_value=NOW):
                monitor = HKMonitor(config, db=db)
                try:
                    self.assertEqual(
                        (monitor.last_token_scan_report or {}).get("layers"),
                        event.get("layers"))
                    self.assertEqual(monitor.last_token_scan_report_ms, NOW)
                finally:
                    monitor.close()

    def test_scanning_us_without_adopting_reports_it_and_writes_nothing(self):
        with TemporaryDirectory() as directory:
            hk = issuer_asset("TESTHKx", "Test HK Corp xStock", "1234", "HK",
                              "0x" + "1" * 40, "0x" + "2" * 40)
            result, monitor, notifier, written = self.run_scan(
                directory, [hk, self.us_asset()], scanned=["HK", "US"],
                adopted=["HK"], contracts=[aapl_contract()])
            # Layer 1: the issuer universe is counted per market, so a US sweep
            # is visible without a single US row being adopted.
            self.assertEqual(result["layers"]["issuer_universe"],
                             {"HK": 1, "US": 1})
            # Layer 2: the US listing is reported with everything needed to
            # review it...
            self.assertEqual([row["symbol"] for row in result["report_only"]],
                             ["AAPLx"])
            row = result["report_only"][0]
            self.assertEqual(row["status"], "market_scanned_not_adopted")
            self.assertEqual(row["market"], "US")
            self.assertEqual(row["depth_usd"], "50000")
            self.assertEqual(row["quote_symbol"], "USDG")
            # A real OKX USD-linear perp exists for it, which is exactly why it
            # is a *candidate* — and why "candidate" must not mean "adopted".
            self.assertTrue(row["has_perp"])
            self.assertEqual(row["cex_match"], "AAPL")
            self.assertEqual(result["layers"]["pairable_candidates"],
                             {"HK": 1, "US": 1})
            # ...and adopted into nothing: layer 3 is untouched, nothing was
            # added to the monitored set, and no config file was written. An
            # alert would have meant "a new tradable name"; there is none.
            self.assertEqual([a["asset"] for a in result["added"]], ["TESTHKx"])
            # Layer 3 is the execution allowlist, read-only. This scan must not
            # be what puts a name there. The signed US names are already on it
            # (operator, 2026-09-23); the unscanned AAPLx above is not adopted.
            self.assertEqual(result["layers"]["executable_allowlist"].get("HK"), 6)
            self.assertGreaterEqual(
                result["layers"]["executable_allowlist"].get("US", 0), 1)
            self.assertNotIn("AAPLx", [t["asset"] for t in monitor.tokens])
            # The one alert that did go out names the Hong Kong listing only:
            # the US candidate is not in it, because it was not adopted.
            text = notifier.send.call_args[0][0]
            self.assertIn("TESTHKx", text)
            self.assertNotIn("AAPL", text)
            # The file that was rewritten (for the Hong Kong listing) carries no
            # US row at all: the report-only candidate never reaches disk.
            self.assertNotIn('market = "US"', written)
            self.assertNotIn("AAPL", written)

    def test_a_us_row_is_added_with_its_own_market_when_us_is_adopted(self):
        with TemporaryDirectory() as directory:
            result, monitor, _, written = self.run_scan(
                directory, [self.us_asset()], scanned=["HK", "US"],
                adopted=["HK", "US"], contracts=[aapl_contract()])
            self.assertEqual(len(result["added"]), 1)
            entry = result["added"][0]
            self.assertEqual(entry["asset"], "AAPL")
            self.assertEqual(entry["market"], "US")
            # The label travels into the file the next process reads back, so a
            # US listing can never be re-read as a Hong Kong one.
            self.assertIn('market = "US"', written)
            self.assertIn("AAPL", [t["asset"] for t in monitor.tokens])

    def test_a_listing_with_no_country_is_never_adopted_or_guessed(self):
        with TemporaryDirectory() as directory:
            # Alphabetic ticker, no listingCountry: UNKNOWN, and UNKNOWN is not
            # scope. The HK-shaped cross-check must not adopt it either.
            unlabeled = issuer_asset("XRXx", "Xerox xStock", "XRX", None,
                                     "0x" + "7" * 40, "0x" + "8" * 40)
            result, monitor, _, written = self.run_scan(
                directory, [unlabeled], scanned=["HK", "US"], adopted=["HK", "US"])
            self.assertEqual(result["skipped_out_of_scope"], {"UNKNOWN": 1})
            self.assertEqual(result["added"], [])
            self.assertEqual(result["report_only"], [])
            self.assertNotIn("XRXx", [t["asset"] for t in monitor.tokens])
            self.assertIsNone(written)

    def test_a_digit_shaped_code_without_a_country_is_flagged_not_adopted(self):
        with TemporaryDirectory() as directory:
            # A numeric code with no country *looks* like HKEX. It is still not
            # evidence of a listing venue, so it is recorded for review only.
            unlabeled = issuer_asset("MIXUx", "Mixu xStock", "1234", None,
                                     "0x" + "7" * 40, "0x" + "8" * 40)
            result, _, _, _ = self.run_scan(
                directory, [unlabeled], scanned=["HK", "US"], adopted=["HK"])
            self.assertEqual(result["unlabeled_hk_shaped"], ["MIXUx"])
            self.assertEqual(result["added"], [])

    def test_the_probe_cap_rotates_the_backlog_across_rounds(self):
        assets = [self.us_asset(f"US{i}x", "0x" + str(i) * 40, "0x" + str(i + 5) * 40)
                  for i in (1, 2, 3)]
        config = load_config(str(CONFIG))
        config["onchain"]["token_scan_countries"] = ["HK", "US"]
        config["onchain"]["token_adopt_markets"] = ["HK"]
        config["onchain"]["token_scan_max_probes_per_round"] = 1
        # The shipped config walks the scoped universe every round (see the
        # comment there); pin it so this test states the behaviour it asserts.
        config["onchain"]["token_scan_full_every"] = 1
        adapter = MagicMock()
        adapter.rpc = MagicMock()
        adapter.pool_states.return_value = [
            {"depth_usd": D("50000"), "price_usd": D("9.6"), "block_number": 7,
             "error": None}]
        pool = [{"address": "0x" + "9" * 40, "quote_symbol": "USDG",
                 "quote_address": "0x" + "8" * 40, "fee": 500}]
        with TemporaryDirectory() as directory, \
             patch("arb_monitor.apps.hk_monitor.CexPublicAdapter"), \
             patch("arb_monitor.apps.hk_monitor.ReferenceAdapter"), \
             patch("arb_monitor.apps.hk_monitor.OnchainOSQuoteAdapter"), \
             patch("arb_monitor.apps.hk_monitor.XLayerOnchainAdapter",
                   return_value=adapter), \
             patch("arb_monitor.apps.hk_monitor.FeishuNotifier"), \
             patch("arb_monitor.apps.hk_monitor.fetch_issuer_assets",
                   return_value=assets), \
             patch("arb_monitor.apps.hk_monitor.fetch_okx_stock_perps",
                   return_value={"AAPL": okx_stock_perp("AAPL")}), \
             patch("arb_monitor.apps.hk_monitor.discover_pools",
                   return_value=pool), \
             patch("arb_monitor.apps.hk_monitor.probe_pools_cheap",
                   return_value=pool), \
             patch("arb_monitor.apps.hk_monitor.now_ms", return_value=NOW):
            monitor = HKMonitor(config, db=str(Path(directory) / "monitor.db"))
            try:
                monitor.discovered_config = str(Path(directory) / "discovered.toml")
                first = monitor.refresh_token_universe("run-1", NOW)
                second = monitor.refresh_token_universe("run-2", NOW)
            finally:
                monitor.close()
        # One probe per round, and the backlog is reported rather than dropped.
        self.assertEqual(first["probes_pending_after_cap"], 2)
        self.assertEqual(second["probes_pending_after_cap"], 2)
        # Two different counts, because they answer two different questions:
        # the intersection is three names wide for the whole market, but this
        # round spent exactly one probe. A board that printed only the first
        # number would read as "114 names were measured a minute ago".
        self.assertEqual(first["layers"]["probed"], {"US": 3})
        self.assertEqual(first["layers"]["probed_this_round"], {"US": 1})
        self.assertEqual(second["layers"]["probed_this_round"], {"US": 1})
        self.assertEqual(len(first["report_only"]), 1)
        self.assertEqual(len(second["report_only"]), 1)
        # The cursor walks the list instead of re-probing the same first name.
        self.assertNotEqual(first["report_only"][0]["symbol"],
                            second["report_only"][0]["symbol"])


class ProbeRotationPersistenceTests(unittest.TestCase):
    """The probe rotation has to survive a restart.

    Measured 2026-09-22 *before* this fix: the scan clock starts at process
    start (`last_token_scan = now`, so the first scan lands ~30 minutes after a
    restart), while the rotation position was an in-memory index into the
    pending list. Every restart put that index back to 0, so two scans 51
    minutes apart — with a restart in between — probed *exactly* the same 20
    names (`HK 9 + US 11`, byte-identical `no_pool`) and both reported
    `probes_pending_after_cap = 158`. "A 193-name sweep every ~5 hours" was
    therefore not true of a monitor that gets restarted, which this one does
    several times a day.

    The rotation is now "least recently probed first", and the ledger of when
    each listing was last probed travels in the `token_universe_scan` event —
    the same event the next process reads at startup to restore the funnel.
    """

    def config_for(self, *, cap):
        config = load_config(str(CONFIG))
        config["onchain"]["token_scan_countries"] = ["HK", "US"]
        # Report-only: a US candidate with a deep pool is still not adopted, so
        # it stays in the pending list and the rotation keeps working on it.
        config["onchain"]["token_adopt_markets"] = ["HK"]
        config["onchain"]["token_scan_perp_gate_markets"] = ["US"]
        config["onchain"]["token_scan_max_probes_per_round"] = cap
        config["onchain"]["token_scan_full_every"] = 1
        return config

    def listings(self, count):
        return [issuer_asset(f"US{i}x", f"US{i} xStock", f"US{i}", "US",
                             "0x" + str(i) * 40, "0x" + str(i + 5) * 40)
                for i in range(1, count + 1)]

    def stack(self, assets):
        """Every outbound call stubbed; ``now_ms`` pinned so runs are comparable."""
        adapter = MagicMock()
        adapter.rpc = MagicMock()
        adapter.pool_states.return_value = [
            {"depth_usd": D("50000"), "price_usd": D("9.6"), "block_number": 7,
             "error": None}]
        pool = [{"address": "0x" + "9" * 40, "quote_symbol": "USDG",
                 "quote_address": "0x" + "8" * 40, "fee": 500}]
        # Every listing carries a real OKX stock perp, so ``count`` names is the
        # whole intersection and the cap is the only thing deciding a batch.
        perps = {f"US{i}": okx_stock_perp(f"US{i}") for i in range(1, len(assets) + 1)}
        stack = ExitStack()
        for name in ("CexPublicAdapter", "ReferenceAdapter", "OnchainOSQuoteAdapter"):
            stack.enter_context(patch(f"arb_monitor.apps.hk_monitor.{name}"))
        stack.enter_context(patch("arb_monitor.apps.hk_monitor.XLayerOnchainAdapter",
                                  return_value=adapter))
        stack.enter_context(patch("arb_monitor.apps.hk_monitor.FeishuNotifier"))
        stack.enter_context(patch("arb_monitor.apps.hk_monitor.fetch_issuer_assets",
                                  return_value=assets))
        stack.enter_context(patch("arb_monitor.apps.hk_monitor.fetch_okx_stock_perps",
                                  return_value=perps))
        stack.enter_context(patch("arb_monitor.apps.hk_monitor.discover_pools",
                                  return_value=pool))
        stack.enter_context(patch("arb_monitor.apps.hk_monitor.probe_pools_cheap",
                                  return_value=pool))
        stack.enter_context(patch("arb_monitor.apps.hk_monitor.now_ms",
                                  return_value=NOW))
        return stack

    def scan_once(self, directory, *, cap, run="run"):
        """One process's worth of scan: construct, scan, close."""
        monitor = HKMonitor(self.config_for(cap=cap),
                            db=str(Path(directory) / "monitor.db"))
        try:
            monitor.discovered_config = str(Path(directory) / "discovered.toml")
            return monitor.refresh_token_universe(run, NOW)
        finally:
            monitor.close()

    def test_a_restart_resumes_the_rotation_instead_of_replaying_it(self):
        with TemporaryDirectory() as directory:
            assets = self.listings(4)
            with self.stack(assets):
                first = self.scan_once(directory, cap=2, run="run-1")
                # A restart. Same DB, same day, new process: the only thing that
                # can carry the rotation across is the ledger in the event.
                second = self.scan_once(directory, cap=2, run="run-2")
        self.assertEqual(first["probed_assets"], ["US1x", "US2x"])
        self.assertEqual(second["probed_assets"], ["US3x", "US4x"])
        # The number that used to be the only thing on the board cannot tell
        # these two rounds apart — which is exactly why the batch is now
        # reported as well.
        self.assertEqual(first["probes_pending_after_cap"], 2)
        self.assertEqual(second["probes_pending_after_cap"], 2)

    def test_a_listing_never_probed_goes_ahead_of_one_that_was(self):
        with TemporaryDirectory() as directory:
            assets = self.listings(4)
            with self.stack(assets):
                self.scan_once(directory, cap=3, run="run-1")
                # US4x was left over, and it sorts *last* by key — so if it is
                # probed first here, it is the "never probed" rule doing it and
                # not alphabetical order.
                second = self.scan_once(directory, cap=2, run="run-2")
        self.assertEqual(second["probed_assets"], ["US4x", "US1x"])
        self.assertEqual(second["probe_rotation"]["never_probed"], 0)

    def test_every_name_is_reached_within_ceiling_rounds(self):
        """The cap spreads the backlog; it must not starve a name for ever."""
        with TemporaryDirectory() as directory:
            assets = self.listings(5)
            with self.stack(assets):
                monitor = HKMonitor(self.config_for(cap=2),
                                    db=str(Path(directory) / "monitor.db"))
                try:
                    monitor.discovered_config = str(Path(directory) / "discovered.toml")
                    rounds = [monitor.refresh_token_universe(f"run-{n}", NOW)
                              for n in range(3)]
                finally:
                    monitor.close()
        seen = [name for report in rounds for name in report["probed_assets"]]
        # ceil(5 / 2) = 3 rounds, and the third round already has to re-probe an
        # older name rather than keep walking forward — so the union, not the
        # count, is the invariant.
        self.assertEqual(sorted(set(seen)), ["US1x", "US2x", "US3x", "US4x", "US5x"])
        self.assertEqual(rounds[2]["probe_rotation"]["never_probed"], 0)

    def test_the_ledger_is_carried_by_the_scan_event_and_read_back(self):
        from arb_monitor.ledger.store import EventStore

        with TemporaryDirectory() as directory:
            db = str(Path(directory) / "monitor.db")
            assets = self.listings(3)
            with self.stack(assets):
                self.scan_once(directory, cap=2, run="run-1")
                store = EventStore(db)
                try:
                    event = store.latest_event("token_universe_scan") or {}
                finally:
                    store.close()
                # Written where the restart can find it, keyed by market+symbol
                # (an index into this round's list would be meaningless in a
                # process that has not read the issuer list yet).
                self.assertEqual(sorted((event.get("probe_seen_ms") or {})),
                                 ["US:US1x", "US:US2x"])
                self.assertEqual(event["probed_assets"], ["US1x", "US2x"])
                self.assertEqual(event["probe_rotation"]["order"],
                                 "least_recently_probed")
                self.assertEqual(event["probe_rotation"]["scoped"], 3)
                self.assertEqual(event["probe_rotation"]["never_probed"], 1)
                # And the next process starts with the rotation restored, not
                # with an empty ledger that would replay the first batch.
                monitor = HKMonitor(self.config_for(cap=2), db=db)
                try:
                    self.assertEqual(sorted(monitor.token_probe_seen_ms),
                                     ["US:US1x", "US:US2x"])
                    self.assertEqual(monitor.token_probe_seen_ms["US:US1x"], NOW)
                finally:
                    monitor.close()

    def test_the_ledger_is_pruned_to_the_scoped_universe(self):
        """A delisted name must not grow the payload without bound."""
        with TemporaryDirectory() as directory:
            assets = self.listings(3)
            with self.stack(assets):
                self.scan_once(directory, cap=3, run="run-1")
                # The issuer drops two of the three; the next scan must not
                # carry their entries for ever.
                with patch("arb_monitor.apps.hk_monitor.fetch_issuer_assets",
                           return_value=assets[:1]):
                    shrunk = self.scan_once(directory, cap=3, run="run-2")
        self.assertEqual(shrunk["probe_rotation"]["scoped"], 1)
        self.assertEqual(sorted(shrunk["probe_seen_ms"]), ["US:US1x"])


class ProbeGateTests(unittest.TestCase):
    """Two lists intersected before a single pool is probed.

    The issuer lists 754 US tokens and OKX quotes 181 tokenised-stock perps
    (2026-09-22); a sweep of the whole universe costs ~19 h of the same local
    proxy the sampling loop uses, to produce rows that cannot be hedged. So a US
    listing has to appear in *both* lists before it is probed.
    """

    def scan(self, directory, assets, *, perps, scanned=("HK", "US"),
             gated=("US",), raises=None):
        config = load_config(str(CONFIG))
        config["onchain"]["token_scan_countries"] = list(scanned)
        config["onchain"]["token_adopt_markets"] = ["HK"]
        config["onchain"]["token_scan_perp_gate_markets"] = list(gated)
        adapter = MagicMock()
        adapter.rpc = MagicMock()
        adapter.pool_states.return_value = [
            {"depth_usd": D("50000"), "price_usd": D("9.6"), "block_number": 7,
             "error": None}]
        pool = [{"address": "0x" + "9" * 40, "quote_symbol": "USDG",
                 "quote_address": "0x" + "8" * 40, "fee": 500}]
        notifier = MagicMock()
        notifier.channel = "webhook"
        notifier.send.return_value = {"ok": True, "channel": "webhook"}
        gate = (patch("arb_monitor.apps.hk_monitor.fetch_okx_stock_perps",
                      side_effect=raises) if raises else
                patch("arb_monitor.apps.hk_monitor.fetch_okx_stock_perps",
                      return_value=perps))
        with TemporaryDirectory() as directory, \
             patch("arb_monitor.apps.hk_monitor.CexPublicAdapter"), \
             patch("arb_monitor.apps.hk_monitor.ReferenceAdapter"), \
             patch("arb_monitor.apps.hk_monitor.OnchainOSQuoteAdapter"), \
             patch("arb_monitor.apps.hk_monitor.XLayerOnchainAdapter",
                   return_value=adapter), \
             patch("arb_monitor.apps.hk_monitor.FeishuNotifier",
                   return_value=notifier), \
             patch("arb_monitor.apps.hk_monitor.fetch_issuer_assets",
                   return_value=assets), \
             patch("arb_monitor.apps.hk_monitor.discover_pools",
                   return_value=pool), \
             patch("arb_monitor.apps.hk_monitor.probe_pools_cheap",
                   return_value=pool), \
             gate, \
             patch("arb_monitor.apps.hk_monitor.now_ms", return_value=NOW):
            monitor = HKMonitor(config, db=str(Path(directory) / "monitor.db"))
            try:
                monitor.discovered_config = str(Path(directory) / "discovered.toml")
                return monitor.refresh_token_universe("gate-run", NOW), monitor
            finally:
                monitor.close()

    def test_only_the_intersection_is_probed(self):
        aapl = issuer_asset("AAPLx", "Apple xStock", "AAPL", "US",
                            "0x" + "5" * 40, "0x" + "6" * 40)
        xerox = issuer_asset("XRXx", "Xerox xStock", "XRX", "US",
                             "0x" + "7" * 40, "0x" + "8" * 40)
        with TemporaryDirectory() as directory:
            result, _ = self.scan(directory, [aapl, xerox],
                                  perps={"AAPL": okx_stock_perp("AAPL")})
        # AAPL is in both lists, so it is probed and reported in full…
        self.assertEqual([row["symbol"] for row in result["report_only"]],
                         ["AAPLx"])
        # …and Xerox, with a token but no OKX perp, never reaches a pool probe.
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["perp_gate"]["okx_stock_perps"], 1)
        self.assertEqual(result["perp_gate"]["gated_out"], {"US": 1})
        self.assertEqual(result["perp_gate"]["probed"], {"US": 1})
        self.assertIsNone(result["perp_gate"]["error"])

    def test_hong_kong_is_not_gated_on_one_venue(self):
        # HK's hedgeable set spans Gate/Bybit/Bitget too, so a HK name with no
        # OKX perp is still probed: monitoring is allowed to be wider than one
        # venue. Only the markets named in the setting are gated.
        hk = issuer_asset("TESTHKx", "Test HK Corp xStock", "1234", "HK",
                          "0x" + "1" * 40, "0x" + "2" * 40)
        us = issuer_asset("XRXx", "Xerox xStock", "XRX", "US",
                          "0x" + "7" * 40, "0x" + "8" * 40)
        with TemporaryDirectory() as directory:
            result, _ = self.scan(directory, [hk, us],
                                  perps={"AAPL": okx_stock_perp("AAPL")})
        # HK is adopted in this fixture, so the HK name is added; the US name is
        # the only one the gate dropped.
        self.assertEqual([row["symbol"] for row in result["report_only"]], [])
        self.assertEqual([row["asset"] for row in result["added"]], ["TESTHKx"])
        self.assertEqual(result["perp_gate"]["gated_out"], {"US": 1})
        self.assertEqual(result["perp_gate"]["probed"], {"HK": 1})

    def test_an_unreadable_perp_list_does_not_licence_a_full_sweep(self):
        # Unknown is not permission: a gate that cannot be read probes nothing
        # in the gated market and says so, rather than falling back to all 754.
        us = issuer_asset("AAPLx", "Apple xStock", "AAPL", "US",
                          "0x" + "5" * 40, "0x" + "6" * 40)
        with TemporaryDirectory() as directory:
            result, _ = self.scan(directory, [us], perps={},
                                  raises=RuntimeError("okx_unreachable"))
        self.assertIsNotNone(result["perp_gate"]["error"])
        self.assertIn("okx_unreachable", result["perp_gate"]["error"])
        self.assertEqual(result["perp_gate"]["okx_stock_perps"], 0)
        self.assertEqual(result["perp_gate"]["gated_out"], {"US": 1})
        self.assertEqual(result["report_only"], [])
        self.assertEqual(result["probes_pending_after_cap"], 0)

    def test_a_crypto_contract_whose_ticker_looks_like_a_stock_is_not_a_hedge(self):
        # OKX's stock series is `instCategory = 3`. A category-1 contract that
        # happens to share a ticker with a US name must not qualify it, which is
        # why the gate reads the venue's own classification instead of matching
        # tickers against every SWAP row.
        from arb_monitor.data.xstocks_registry import fetch_okx_stock_perps
        rows = {"data": [
            {"instId": "AAPL-USDT-SWAP", "ctValCcy": "AAPL", "instCategory": "3"},
            {"instId": "RAM-USDT-SWAP", "ctValCcy": "RAM", "instCategory": "1"},
            {"instId": "XAU-USDT-SWAP", "ctValCcy": "XAU", "instCategory": "4"},
            {"instId": "AAPL-USD-SWAP", "ctValCcy": "AAPL", "instCategory": "3"},
        ]}
        with patch("arb_monitor.data.xstocks_registry.fetch_json",
                   return_value=rows):
            perps = fetch_okx_stock_perps(None)
        self.assertEqual(sorted(perps), ["AAPL"])


class CostTableTests(unittest.TestCase):
    """Per-asset/venue costs: the reviewed global value unless overridden."""

    def test_a_block_without_a_cost_table_keeps_the_global_value(self):
        default = costs(spot_fee_bps=D("10"))
        table = cost_table_from_tokens(default, [aapl_token()])
        self.assertEqual(table.for_pair("AAPL", "okx", "AAPL-USDT-SWAP"), default)
        self.assertFalse(table.is_override("AAPL", "okx", "AAPL-USDT-SWAP"))
        self.assertEqual(table.describe(), [])

    def test_an_override_is_read_from_the_matching_mapping_block(self):
        default = costs(spot_fee_bps=D("10"))
        token = aapl_token()
        token["mappings"]["okx:AAPL-USDT-SWAP"]["cost"] = {
            "spot_fee_bps": "25", "slippage_bps": "12", "verified": True,
            "valid_until_ms": NOW + 10_000,
        }
        table = cost_table_from_tokens(default, [token])
        found = table.for_pair("AAPL", "okx", "AAPL-USDT-SWAP")
        self.assertEqual(found.spot_fee_bps, D("25"))
        self.assertEqual(found.slippage_bps, D("12"))
        # An override is per venue:instrument, so another venue keeps the
        # reviewed default instead of inheriting OKX's pool fee.
        self.assertEqual(table.for_pair("AAPL", "gate", "AAPL_USDT"), default)
        self.assertEqual([row["key"] for row in table.describe()],
                         ["AAPL|okx|AAPL-USDT-SWAP"])

    def test_a_typo_in_a_cost_field_is_refused_not_ignored(self):
        with self.assertRaises(ValueError) as caught:
            costs_from_spec(costs(), {"cost": {"spot_fee_bps": "1",
                                               "slippage_bp": "5"}})
        self.assertIn("unknown cost fields: slippage_bp", str(caught.exception))
        with self.assertRaises(ValueError):
            costs_from_spec(costs(), {"cost": {"spot_fee_bps": "cheap"}})

    def test_the_costs_never_move_a_threshold_of_their_own(self):
        # A cost record says what a round trip costs; it carries no bar. The
        # reviewed global values are the base, so an override can only replace
        # fields it names.
        default = costs(failure_bps=D("7"))
        merged = costs_from_spec(default, {"cost": {"spot_fee_bps": "3"}})
        self.assertEqual(merged.failure_bps, D("7"))
        self.assertEqual(merged.perp_fee_bps, default.perp_fee_bps)


class ShippedConfigTests(unittest.TestCase):
    """What the shipped config actually authorises, read as data."""

    def setUp(self):
        self.config = load_config(str(CONFIG))

    def test_us_is_scanned_and_adopted_but_both_gates_stay_on(self):
        """Adoption is on; the two gates that decide *which* US names get in are not.

        Operator, 2026-09-22: "现在开始对这114个公司也进行监控". Adopting a market
        is only the first of three conditions a US row has to clear, and this
        test pins the other two, because turning adoption on is exactly the
        change that would be tempted to relax them.
        """
        scanned, adopted = scan_scope(self.config["onchain"])
        self.assertIn("HK", scanned)
        self.assertIn("US", scanned)
        self.assertIn("US", adopted)
        # Gate 1: the intersection gate. 754 issuer US tokens ∩ 181 OKX stock
        # perps = 114, and an unreachable OKX list must *not* be read as "probe
        # everything" (that path is tested hermetically in ProbeGateTests).
        self.assertEqual(self.config["onchain"]["token_scan_perp_gate_markets"], ["US"])
        # Gate 2: pool depth. Adoption writes a token only after its X Layer pool
        # carries real depth, so "adopted US" cannot mean "114 names priced".
        self.assertGreater(D(self.config["onchain"]["min_pool_depth_usd"]), D("0"))

    def test_signed_us_names_are_allowlisted_but_not_quoted_every_cycle(self):
        """Operator 2026-09-23 signed the depth-list mappings and the allowlist.

        Exact-in probes stay on the six HK names. A signed US name is quoted
        only when the previous cycle's slot0 basis clears the pre-screen, so
        signing forty names does not put forty OnchainOS probes on every round.
        An auto-discovered row that was not in the signed file stays unsigned
        and off the allowlist.
        """
        us_rows = [t for t in self.config["tokens"]
                   if normalize_market(t.get("listing_country") or t.get("market"))
                   == US]
        reviewed = {"AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "TSLA"}
        self.assertTrue(reviewed <= {t["asset"] for t in us_rows})
        policy = self.config.get("execution_policy") or {}
        allowlist = policy.get("allowlist") or {}
        allow_assets = set(allowlist.get("assets") or [])
        for token in us_rows:
            if token.get("mapping_verified"):
                self.assertIn(token["asset"], self.config["monitor"]["assets"])
                self.assertIn(token["asset"], allow_assets)
                self.assertIn(f'{token["asset"]}-USDT-SWAP',
                              allowlist.get("instruments") or [])
                if token["asset"] in reviewed:
                    self.assertTrue(token.get("auto_discovered") is None)
            else:
                self.assertTrue(token.get("auto_discovered"))
                self.assertNotIn(token["asset"], allow_assets)
        self.assertIn("AAPL", allow_assets)
        self.assertNotIn("TER", allow_assets)
        self.assertEqual(allowlist.get("venues"), ["okx"])
        self.assertEqual(allowlist.get("assets")[:6],
                         ["XIAOMI", "TENCENT", "POPMART", "MEITUAN",
                          "KUAISHOU", "SHEIN"])
        self.assertEqual(self.config["monitor"]["quote_slice_markets"], ["US"])
        self.assertGreater(
            int(self.config["monitor"]["quote_slice_assets_per_cycle"]), 0)
        self.assertEqual(self.config["monitor"]["quote_exact_assets"],
                         ["XIAOMI", "TENCENT", "POPMART", "MEITUAN",
                          "KUAISHOU", "SHEIN"])
        # That list is the OnchainOS fallback. The cycle itself quotes through
        # QuoterV2, so it is not capped at those six names.
        self.assertTrue(self.config["onchain"]["pool_quotes"])
        self.assertEqual(self.config["onchain"]["quoter_v2"],
                         "0xd1b797d92d87b688193a2b976efc8d577d204343")
        self.assertEqual(self.config["monitor"]["venues"], ["okx"])
        self.assertEqual(self.config["onchain"]["token_adopt_trade_scan_markets"],
                         ["HK"])

    def test_a_signed_us_row_keeps_the_measured_ratio(self):
        for token in self.config["tokens"]:
            if normalize_market(token.get("listing_country")) != US:
                continue
            if not token.get("mapping_verified"):
                continue
            # The ratio is not "1 token = 1 contract". Operator signed the
            # measured convertToAssets rate on 2026-09-23. The USDG/USDC peg is
            # not stored in the file; the monitor reads it each cycle.
            self.assertEqual(token.get("stock_symbol"), token["asset"])
            specs = token.get("mappings") or {}
            self.assertEqual(list(specs), [f'okx:{token["stock_symbol"]}-USDT-SWAP'])
            spec = specs[f'okx:{token["stock_symbol"]}-USDT-SWAP']
            self.assertTrue(spec.get("verified"))
            self.assertGreater(int(spec.get("valid_until_ms") or 0), 0)
            self.assertEqual(spec["perp_value_currency"], token["stock_symbol"])
            self.assertEqual(spec["perp_contract_multiplier"], "1")
            self.assertEqual(spec["native_shares_per_unit"], "1")
            self.assertEqual(spec["wrapper_address"], token["address"])
            self.assertTrue(D(spec["shares_per_token"]) >= D("1"))
            self.assertNotIn("quote_to_usdt", spec)
            self.assertNotIn("quote_conversion", token)
            for pool in token.get("pools") or []:
                self.assertTrue(pool.get("quote_address"))
                self.assertEqual(pool["base_address"], token["address"])

    def test_the_us_pools_are_named_with_the_stablecoin_they_pay_in(self):
        # Every pool names the stablecoin it actually pays in, and the address
        # and the symbol have to agree — one conversion rate cannot vouch for
        # another stablecoin, and a mislabelled pool is how a USDC pool would
        # silently be priced with a USDG rate.
        known = {
            "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8": "USDG",
            "0x779ded0c9e1022225f8e0630b35a9b54be713736": "USDT0",
            "0xb6ceceab302e2e4948951ee7843fc24e92933061": "USDC",
            "0x1e4a5963abfd975d8c9021ce480b42188849d41d": "USDT",
        }
        us_quotes = set()
        for token in self.config["tokens"]:
            if normalize_market(token.get("listing_country")) != US:
                continue
            for pool in token.get("pools") or []:
                quote = pool["quote_address"].lower()
                self.assertIn(quote, known, pool)
                self.assertEqual(pool["quote_symbol"], known[quote], pool)
                us_quotes.add(known[quote])
        # Measured 2026-09-22: the deep US wrappers pay in USDG or USDC; the
        # USDT0 tiers that exist for these names are all depth 0.
        self.assertEqual(us_quotes, {"USDG", "USDC"})
        table = cost_table_from_tokens(costs(), self.config["tokens"])
        token = [t for t in self.config["tokens"] if t["asset"] == "AAPL"][0]
        self.assertEqual(token["pools"][0]["quote_symbol"], "USDG")
        # No shipped block overrides its costs yet: the reviewed global values
        # stand until the operator decides otherwise.
        self.assertEqual(table.describe(), [])


class _NeverSubmits:
    """Minimal pool stand-in: only ``submit`` is ever reached."""

    def __init__(self):
        self.submits = 0

    def submit(self, _fn, *_args, **_kwargs):
        self.submits += 1
        return Future()  # left pending: "one scan in flight"


class _StubMonitor:
    """Just enough object for ``HKMonitor._token_scan`` to bind to.

    The real method reads three attributes and nothing else, so calling it
    unbound keeps this test free of any config, DB, network or venue.
    """

    _token_scan = HKMonitor._token_scan

    def __init__(self, future=None, pool=None):
        self._token_scan_future = future
        self._token_pool = pool if pool is not None else _NeverSubmits()
        self.token_scan_errors = []
        self.refresh_token_universe = lambda *_a, **_k: {}


class TokenScanHarvestTests(unittest.TestCase):
    """A finished scan must not wait for the next due time to be reported.

    The 30-minute clock decides when a *new* scan starts; collecting a finished
    one is a separate decision.  Gating both on the clock left the three
    discovery layers ``null`` on the board for a full interval after the work
    was already done (measured 2026-09-22: the scan was kicked off on the
    15:15:05 cycle, its worker finished ~15:22, and ``layers`` was still
    ``null`` at 15:25 even though ``last_scan_ms`` had already advanced).
    """

    def test_a_finished_scan_is_reported_even_when_the_clock_says_not_due(self):
        done = Future()
        done.set_result({"scanned": True, "layers": {"issuer_universe": {"US": 7}}})
        monitor = _StubMonitor(future=done)
        out = monitor._token_scan("fixture-run", NOW, due=False)
        self.assertEqual(out["layers"], {"issuer_universe": {"US": 7}})
        self.assertIsNone(monitor._token_scan_future)
        # ...and it is kept, because the next 29 cycles have no scan of their
        # own and the board still has to print the funnel on all of them.
        self.assertEqual(monitor.last_token_scan_report["layers"],
                         {"issuer_universe": {"US": 7}})

    def test_not_due_with_nothing_in_flight_starts_nothing(self):
        pool = _NeverSubmits()
        monitor = _StubMonitor(pool=pool)
        out = monitor._token_scan("fixture-run", NOW, due=False)
        self.assertEqual(out["reason"], "not_due")
        self.assertEqual(pool.submits, 0)
        self.assertIsNone(monitor._token_scan_future)

    def test_due_with_nothing_in_flight_starts_exactly_one_scan(self):
        pool = _NeverSubmits()
        monitor = _StubMonitor(pool=pool)
        out = monitor._token_scan("fixture-run", NOW, due=True)
        self.assertEqual(out["reason"], "started_background_scan")
        self.assertEqual(pool.submits, 1)
        self.assertIsNotNone(monitor._token_scan_future)

    def test_a_scan_in_flight_is_neither_restarted_nor_faked(self):
        pool = _NeverSubmits()
        monitor = _StubMonitor(future=Future(), pool=pool)
        out = monitor._token_scan("fixture-run", NOW, due=True)
        self.assertEqual(out["reason"], "in_flight_not_sampled")
        self.assertEqual(pool.submits, 0)

    def test_a_failed_scan_reports_the_error_and_does_not_retry_immediately(self):
        broken = Future()
        broken.set_exception(RuntimeError("boom"))
        monitor = _StubMonitor(future=broken)
        out = monitor._token_scan("fixture-run", NOW, due=False)
        self.assertEqual(out["reason"], "background_scan_failed")
        self.assertEqual(out["new_listings"], 0)
        self.assertIn("boom", monitor.token_scan_errors[0]["error"])


class USUniverseRegistryTests(unittest.TestCase):
    """The 114-name registry is the measured intersection, not a preference."""

    def test_the_registry_holds_the_measured_intersection(self):
        from arb_monitor.data.hk_cex import US_CANDIDATES

        # 754 issuer US tokens ∩ 181 OKX `instCategory="3"` perps = 114. If the
        # count moves, the two source lists moved and the doc table
        # (`docs/research/us-okx-overlap-2026-09-22.md`) has to move with it.
        self.assertEqual(len(US_CANDIDATES), 114)
        # The seven that already have `mapping-verify` evidence must be in it;
        # the five the operator corrected on 2026-09-22 (they *are* Nasdaq
        # listings) must be too, so a re-tightening to "public companies only"
        # cannot silently drop them again.
        for name in ("AAPL", "NVDA", "TSLA", "MSFT", "AMZN", "META", "GOOGL",
                     "SPCX", "SHAZ", "BOT", "BSP", "STRC"):
            self.assertIn(name, US_CANDIDATES)
        # OKX-only names with no issuer token are *not* in the intersection and
        # must not be pulled in by a tidier-looking list.
        for name in ("OPENAI", "ANTHROPIC", "SHEIN", "XIAOMI"):
            self.assertNotIn(name, US_CANDIDATES)

    def test_a_crypto_contract_sharing_a_stock_ticker_is_not_an_equity_leg(self):
        """The CEX registry keeps OKX rows only when OKX calls them equity.

        `QNT`/`RAM`/`PURR`/`FLY` are crypto contracts on OKX whose tickers look
        like US listings. Once the name list is 114 wide, matching on the ticker
        alone would read one of them as a hedgeable stock leg, so the parser
        reads `instCategory` — measured 2026-09-22: XIAOMI/POPMART/SHEIN are all
        `"3"`, so the guard does not cost the Hong Kong names anything.
        """
        from arb_monitor.data.hk_cex import parse_contracts

        raw = {"data": [
            okx_stock_perp("AAPL"),
            okx_stock_perp("XIAOMI"),
            {**okx_stock_perp("AAPL"), "instId": "AAPL-USD-SWAP"},
            {**okx_stock_perp("FLY"), "instCategory": "1"},
        ]}
        contracts = parse_contracts("okx", raw, NOW)
        kept = sorted(c.instrument_id for c in contracts)
        self.assertEqual(kept, ["AAPL-USDT-SWAP", "XIAOMI-USDT-SWAP"])


class QuoteGridSliceTests(unittest.TestCase):
    """A screened market rotates through the grid instead of multiplying it.

    Operator, 2026-09-22: monitor the 114 companies. Each quote probe is an
    OnchainOS CLI round trip over the one local proxy the 60 s sampling loop
    also uses, so 114 names × 2 probes cannot join a single round — the Hong
    Kong legs would be the ones aged out. The slice is the mechanism that makes
    "monitor them all" affordable, so it has to be pinned: the hand-written
    (reviewed) blocks stay every-cycle, the machine-adopted ones rotate, and the
    rotation is reported rather than inferred from missing rows.
    """

    def monitor(self, *, slice_markets=("US",), per_cycle=3, tokens=()):
        config = load_config(str(CONFIG))
        config["monitor"]["assets"] = [t["asset"] for t in tokens]
        config["monitor"]["quote_slice_markets"] = list(slice_markets)
        config["monitor"]["quote_slice_assets_per_cycle"] = per_cycle
        # The shipped config limits OnchainOS to the HK execution names. These
        # tests are about the slice itself, so they turn that limit off.
        config["monitor"]["quote_exact_assets"] = []
        config["tokens"] = list(tokens)
        with patch("arb_monitor.apps.hk_monitor.CexPublicAdapter"), \
             patch("arb_monitor.apps.hk_monitor.ReferenceAdapter"), \
             patch("arb_monitor.apps.hk_monitor.OnchainOSQuoteAdapter"), \
             patch("arb_monitor.apps.hk_monitor.XLayerOnchainAdapter"):
            return HKMonitor(config)

    def us_token(self, asset, *, discovered):
        token = {"asset": asset, "symbol": f"w{asset}x", "address": "0x" + "1" * 40,
                 "chain_id": "196", "decimals": 18, "listing_country": "US",
                 "pools": [], "mapping_verified": False}
        if discovered:
            token["auto_discovered"] = True
        return token

    def test_a_reviewed_block_is_quoted_every_cycle_and_a_discovered_one_rotates(self):
        reviewed = self.us_token("AAPL", discovered=False)
        found = [self.us_token(t, discovered=True)
                 for t in ("AAOI", "ADBE", "ALAB", "AMAT", "AMD", "AMZN")]
        monitor = self.monitor(tokens=[reviewed, *found])
        try:
            monitor.contracts = [
                replace(aapl_contract(), asset=t["asset"])
                for t in [reviewed, *found]]
            rounds = [monitor.quote_grid_tokens() for _ in range(2)]
            for grid in rounds:
                # AAPL is in every round; only the discovered names rotate.
                self.assertIn("AAPL", [t["asset"] for t in grid])
                self.assertEqual(len(grid), 1 + 3)
            first, second = [[t["asset"] for t in g] for g in rounds]
            # Two disjoint halves of the six: no name is quoted twice before
            # every other one has had its turn.
            self.assertEqual(set(first[1:]) & set(second[1:]), set())
            self.assertEqual(sorted(first[1:] + second[1:]), sorted(
                t["asset"] for t in found))
            self.assertEqual(monitor.quote_slice_report["sliced"]["US"],
                             sorted(second[1:]))
            self.assertEqual(monitor.quote_slice_report["deferred"]["US"], 3)
        finally:
            monitor.close()

    def test_the_whole_market_is_covered_once_the_cursor_comes_round(self):
        found = [self.us_token(f"T{i:02d}", discovered=True) for i in range(7)]
        monitor = self.monitor(tokens=found, per_cycle=3)
        try:
            monitor.contracts = [replace(aapl_contract(), asset=t["asset"])
                                 for t in found]
            seen = []
            for _ in range(3):
                grid = monitor.quote_grid_tokens()
                # A round is never wider than the slice, whatever the market
                # size — that is the whole point of the knob.
                self.assertLessEqual(len(grid), 3)
                seen.extend(t["asset"] for t in grid)
            # 7 names at 3 per cycle: covered within ceil(7/3) = 3 cycles. The
            # last round wraps and re-quotes two, which is correct — coverage is
            # the invariant, "no repeats" is not.
            self.assertEqual(set(seen), {t["asset"] for t in found})
            self.assertEqual(len(seen), 9)
        finally:
            monitor.close()

    def test_no_slice_configured_keeps_the_old_every_asset_behaviour(self):
        found = [self.us_token(t, discovered=True) for t in ("AAOI", "ADBE", "ALAB")]
        monitor = self.monitor(slice_markets=(), per_cycle=0, tokens=found)
        try:
            monitor.contracts = [replace(aapl_contract(), asset=t["asset"])
                                 for t in found]
            grid = monitor.quote_grid_tokens()
            self.assertEqual(sorted(t["asset"] for t in grid),
                             ["AAOI", "ADBE", "ALAB"])
            self.assertEqual(monitor.quote_slice_report["sliced"], {})
        finally:
            monitor.close()

    def test_a_slice_with_no_width_is_refused_at_load_time(self):
        # Fail closed on the configuration, not on the board: a zero-width slice
        # would look exactly like "that market has nothing to quote".
        config = load_config(str(CONFIG))
        self.assertGreater(
            int(config["monitor"]["quote_slice_assets_per_cycle"]), 0)
        source = CONFIG.read_text().replace(
            "quote_slice_assets_per_cycle = 3",
            "quote_slice_assets_per_cycle = 0")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "hk_monitor.toml"
            path.write_text(source.replace(
                'discovered_config = "config/discovered_tokens.toml"',
                f'discovered_config = "{directory}/none.toml"'))
            with self.assertRaises(ValueError):
                load_config(str(path))

    def test_the_trade_scan_list_cannot_name_a_market_nothing_adopts(self):
        config = load_config(str(CONFIG))
        onchain = dict(config["onchain"])
        onchain["token_adopt_trade_scan_markets"] = ["GB"]
        with self.assertRaises(ValueError):
            scan_scope(onchain)


class AdoptedMarketTradeScanTests(unittest.TestCase):
    """The archive print scan is opt-in per adopted market.

    ``eth_getLogs`` cost 43-176 s for eight tokens on 2026-09-21 (this RPC caps
    the range at 100 blocks), so a market adopted for screening must not spend
    it on a hundred names that cannot be executed yet. The flag has to survive
    the round trip through `config/discovered_tokens.toml`: the writer used to
    hard-code `trade_scan = true`, which would have silently turned it back on
    at the next restart.
    """

    def test_an_adopted_market_outside_the_scan_list_gets_the_scan_off(self):
        from arb_monitor.data.xstocks_registry import (discovered_token_entry,
                                                       render_discovered_toml)

        asset = issuer_asset("AAPLx", "Apple xStock", "AAPL", "US",
                             "0x" + "5" * 40, "0x" + "6" * 40)
        pool = {"address": "0x" + "9" * 40, "quote_symbol": "USDG",
                "quote_address": "0x" + "8" * 40, "fee": 500}
        off = discovered_token_entry(asset, "wrapper", "0x" + "6" * 40, pool,
                                     D("50000"), "AAPL", NOW, trade_scan=False)
        self.assertFalse(off["trade_scan"])
        self.assertIn("trade_scan = false", render_discovered_toml([off]))
        # And the default is unchanged, so the Hong Kong path reads the same as
        # it always did.
        on = discovered_token_entry(asset, "wrapper", "0x" + "6" * 40, pool,
                                    D("50000"), "AAPL", NOW)
        self.assertTrue(on["trade_scan"])
        self.assertIn("trade_scan = true", render_discovered_toml([on]))


class MarketCoverageBoardTests(unittest.TestCase):
    """The board has to say *per market* what is monitored, priced and candidate.

    Operator, 2026-09-22: "监控结果也要展示在看板上，现在看板上只有港股相关
    的内容". Two things had to be true and only one was: the basis table has always
    carried a 上市地 column (so a monitored US row appears in it), but a market
    that is adopted and sampled on a rotation would have read as a market full of
    holes. This pins the strip that names the difference.
    """

    def summary(self):
        def direction(judgement):
            return {"directions": {"long_spot_short_perp": {"judgement": judgement}}}

        return {
            "quote_grid_slice": {
                "markets": ["US"], "assets_per_cycle": 3, "cursor": 3,
                "sliced": {"US": ["ADBE", "ALAB"]},
                "deferred": {"US": 105}, "rotating_pool": {"US": 107},
                # What the cycle actually put on the wire. AAPL is hand-reviewed
                # so it is here every round; AAOI is machine-adopted and this
                # round was not its turn.
                "issued": ["XIAOMI", "TENCENT", "AAPL", "ADBE", "ALAB"]},
            "quote_universe_assets": ["XIAOMI", "TENCENT", "AAPL", "AAOI"],
            "token_references": [
                {"asset": "XIAOMI", "listing_market": "HK",
                 **direction("candidate")},
                {"asset": "TENCENT", "listing_market": "HK",
                 **direction("below_threshold")},
                {"asset": "AAPL", "listing_market": "US",
                 **direction("display_price_only")},
                {"asset": "AAOI", "listing_market": "US", "directions": {}},
            ],
            "token_universe_scan": {
                "interval_seconds": 1800, "scan_age_ms": 240000,
                "layers": {
                    "issuer_universe": {"HK": 79, "US": 754},
                    "gated_out": {"US": 640},
                    "probed": {"HK": 79, "US": 114},
                    # The intersection is a ceiling; this is what the last
                    # round actually spent probes on under the per-round cap.
                    "probed_this_round": {"HK": 20, "US": 3},
                    "pairable_candidates": {"HK": 6, "US": 1},
                    "no_pool": {"US": 113},
                    "executable_allowlist": {"HK": 6}}},
        }

    def row(self, page, market):
        for block in page.split("<tr>")[1:]:
            if f"<td>{market}</td>" in block:
                cells = [cell.split("</td>")[0] for cell in block.split("<td>")[1:]]
                return [re.sub(r"<[^>]+>", "", cell).strip() for cell in cells]
        return None

    def test_each_market_states_its_own_monitored_and_priced_counts(self):
        from arb_monitor.apps.hk_monitor import market_coverage_html

        page = market_coverage_html(self.summary())
        hk = self.row(page, "港股")
        us = self.row(page, "美股")
        # HK does not rotate, so both monitored names were on the grid and one
        # cleared the candidate bar.
        self.assertEqual(hk[:2], ["港股", "2"])
        self.assertEqual(hk[2:], ["2", "1"])
        # US rotates: two monitored, one on this cycle's grid (AAPL is reviewed
        # and therefore quoted every round; AAOI's turn had not come), none a
        # candidate — and the row says how many names are still waiting.
        self.assertEqual(us[:2], ["美股", "2"])
        self.assertTrue(us[2].startswith("1"), us[2])
        self.assertEqual(us[3], "0")
        self.assertIn("轮转池 107 个", page)
        self.assertIn("待轮 105 个", page)

    def test_priced_counts_the_grid_that_was_issued_not_the_whole_universe(self):
        """The bug this pins: a rotating market read "本周期有报价 0".

        Measured 2026-09-22 on the live page, all seven reviewed US names were
        being quoted every cycle while the strip said "美股 本周期有报价 0",
        because the count asked the *slice* (machine-discovered names only) and
        never the grid. `quote_universe_assets` cannot fix it either — that list
        is everything quotable, so it would say "all quoted" always.
        """
        from arb_monitor.apps.hk_monitor import market_coverage_html

        summary = self.summary()
        us = self.row(market_coverage_html(summary), "美股")
        self.assertEqual(us[2].split(" ")[0], "1")
        # AAOI is monitored and quotable but was not issued this round, so the
        # count must be 1 — not 2 (both are quotable) and not 0.
        self.assertEqual(us[1], "2")
        # Same summary without `issued` falls back to the old rule rather than
        # inventing a number for a page rendered from a pre-upgrade summary —
        # and that old rule is exactly the 0 that made the row look empty.
        summary["quote_grid_slice"].pop("issued")
        old = self.row(market_coverage_html(summary), "美股")
        self.assertEqual(old[2].split(" ")[0], "0")

    def test_a_rotating_market_with_nothing_to_rotate_says_so(self):
        from arb_monitor.apps.hk_monitor import market_coverage_html

        summary = self.summary()
        # Every US name monitored is hand-reviewed: the rotating pool is empty,
        # which is the opposite of "we forgot to sample them".
        summary["quote_grid_slice"].update(
            {"sliced": {}, "deferred": {}, "rotating_pool": {"US": 0},
             "issued": ["XIAOMI", "TENCENT", "AAPL", "AAOI"]})
        page = market_coverage_html(summary)
        us = self.row(page, "美股")
        self.assertEqual(us[:2], ["美股", "2"])
        self.assertEqual(us[2].split(" ")[0], "2")
        self.assertIn("无待轮标的", page)
        self.assertNotIn("待轮 105 个", page)

    def test_the_funnel_prints_every_stage_measured_and_how_stale_it_is(self):
        from arb_monitor.apps.hk_monitor import market_coverage_html

        page = market_coverage_html(self.summary())
        # 754 issuer tokens, 640 dropped for having no OKX stock perp before any
        # probe, 114 actually probed, 113 of those with no pool at depth, 1 that
        # can be monitored, 0 that may be executed. Each number is a measured
        # count, and the page says when it was measured.
        self.assertIn("美股：发行方 754 · 无 OKX 股票永续 640（未探针） · "
                      "交集内 114 · 本轮探针 3 · 本轮新增有池候选 1 · "
                      "探针无池 113", page)
        self.assertIn("港股：发行方 79 · 交集内 79 · 本轮探针 20 · "
                      "本轮新增有池候选 6 · 执行白名单 6",
                      page)
        self.assertIn("扫描数据 4 分钟前", page)
        # A summary from before the first scan landed says so instead of
        # printing a dash that reads like a zero.
        pending = {"token_references": [
            {"asset": "AAPL", "listing_market": "US", "directions": {}}],
            "token_universe_scan": {"interval_seconds": 1800}}
        self.assertIn("本轮还没有跑过发行人列表扫描",
                      market_coverage_html(pending))

    def test_the_page_still_distinguishes_monitoring_from_executing(self):
        from arb_monitor.apps.hk_monitor import market_coverage_html

        page = market_coverage_html(self.summary())
        self.assertIn("监控 ≠ 可执行", page)
        self.assertIn("尚未核验份额映射", page)

    def test_a_screening_verdict_on_an_unsigned_mapping_says_it_is_not_executable(self):
        """The board's verdict cell is a price read, not a permission.

        The strategy rejects every US row with
        `unverified_or_expired_share_ratio_fx_mapping` before sizing, so a row
        that clears the basis threshold in the *screening* table (which only
        compares two prices) must not be readable as actionable. Operator,
        2026-09-22: "监控 ≠ 可执行" has to be visible on the row itself.
        """
        from arb_monitor.apps.hk_monitor import dashboard_html

        summary = self.summary()
        # One US row that screens as a candidate, one HK row that does the same
        # thing with a signed mapping. Same verdict, different authorisation.
        unsigned = {"asset": "AAPL", "listing_market": "US", "mapping": {"verified": False},
                    "onchain_pool_ask_usd": "100",
                    "pool_state_executable": True,
                    "directions": {"long_spot_short_perp": {
                        "judgement": "candidate", "gross_entry_basis_bps": "92.5",
                        "estimated_net_entry_bps": "77.5", "perp_price_usd": "101",
                        "perp": {"instrument": "AAPL-USDT-SWAP"}}}}
        signed = {"asset": "TENCENT", "listing_market": "HK", "mapping": {"verified": True},
                  "onchain_pool_ask_usd": "100",
                  "pool_state_executable": True,
                  "directions": {"long_spot_short_perp": {
                      "judgement": "candidate", "gross_entry_basis_bps": "89.0",
                      "estimated_net_entry_bps": "74.0", "perp_price_usd": "101",
                      "perp": {"instrument": "TENCENT-USDT-SWAP"}}}}
        summary["token_references"] = [unsigned, signed]
        page = dashboard_html(summary)
        rows = [re.sub(r"<[^>]+>", "|", block)
                for block in page.split("<tr")[1:]]
        aapl = next(r for r in rows if "AAPL" in r)
        tencent = next(r for r in rows if "TENCENT" in r)
        self.assertIn("映射没签，不能下单", aapl)
        self.assertNotIn("可以马上下单", aapl)
        self.assertIn("可以马上下单", tencent)



class OkxPriceScreenTests(unittest.TestCase):
    def test_one_ticker_snapshot_prices_every_contract_and_marks_slot0_display_only(self):
        from arb_monitor.apps.hk_monitor import okx_price_screen, price_screen_html

        contracts = [
            aapl_contract(),
            replace(aapl_contract(), asset="NVDA", instrument_id="NVDA-USDT-SWAP"),
            replace(aapl_contract(), asset="ZZZ", instrument_id="ZZZ-USDT-SWAP"),
        ]
        tickers = [
            {"instId": "AAPL-USDT-SWAP", "bidPx": "101", "askPx": "102", "last": "101"},
            {"instId": "NVDA-USDT-SWAP", "bidPx": "50", "askPx": "50", "last": "50"},
            {"instId": "NOT-OURS-USDT-SWAP", "bidPx": "1", "askPx": "1", "last": "1"},
        ]
        pools = {"AAPL": {"slot0_price_usd": "100", "spot_ask_usd": "100",
                          "state": "display_only"}}
        screen = okx_price_screen(contracts, tickers, pools, NOW)
        self.assertEqual(screen["okx_contracts"], 1)
        self.assertEqual(screen["both_legs"], 1)
        by_asset = {row["asset"]: row for row in screen["rows"]}
        # mid 101 vs spot 100 = 100 bps. Display only.
        self.assertEqual(D(by_asset["AAPL"]["display_basis_bps"]), D("100"))
        self.assertFalse(by_asset["AAPL"]["executable"])
        self.assertNotIn("NVDA", by_asset)
        self.assertNotIn("ZZZ", by_asset)
        page = price_screen_html({"price_screen": screen})
        self.assertIn("AAPL-USDT-SWAP", page)
        self.assertNotIn("NVDA-USDT-SWAP", page)
        self.assertIn("合约 Bid", page)
        self.assertIn("现货 Ask", page)
        self.assertIn("100.0000", page)

    def test_exact_in_grid_ignores_names_outside_the_execution_list(self):
        helper = QuoteGridSliceTests()
        reviewed = helper.us_token("AAPL", discovered=False)
        found = helper.us_token("AMD", discovered=True)
        monitor = helper.monitor(tokens=[reviewed, found])
        try:
            monitor.contracts = [replace(aapl_contract(), asset="AAPL"),
                                 replace(aapl_contract(), asset="AMD",
                                         instrument_id="AMD-USDT-SWAP")]
            monitor.pool_quotes = False
            monitor.quote_exact_assets = {"AAPL"}
            grid = monitor.quote_grid_tokens()
            self.assertEqual([t["asset"] for t in grid], ["AAPL"])
            self.assertEqual(monitor.quote_slice_report["exact_in_only"], ["AAPL"])
        finally:
            monitor.close()


class ExactQuotePromotionTests(unittest.TestCase):
    def test_only_a_positive_allowlisted_basis_is_promoted(self):
        screen = {"rows": [
            {"asset": "AMD", "instrument": "AMD-USDT-SWAP", "display_basis_bps": "40"},
            {"asset": "NVDA", "instrument": "NVDA-USDT-SWAP", "display_basis_bps": "3"},
            {"asset": "INTC", "instrument": "INTC-USDT-SWAP", "display_basis_bps": "-20"},
            {"asset": "XIAOMI", "instrument": "XIAOMI-USDT-SWAP", "display_basis_bps": "80"},
        ]}
        tokens = [{"asset": "AMD", "mapping_verified": True},
                  {"asset": "NVDA", "mapping_verified": True},
                  {"asset": "INTC", "mapping_verified": True},
                  {"asset": "XIAOMI", "mapping_verified": True}]
        allow = {"assets": ["AMD", "NVDA", "INTC", "XIAOMI"],
                 "instruments": ["AMD-USDT-SWAP", "NVDA-USDT-SWAP",
                                 "INTC-USDT-SWAP", "XIAOMI-USDT-SWAP"]}
        promoted = promoted_exact_assets(
            screen, tokens, allow, standing={"XIAOMI"})
        self.assertEqual(promoted, ["AMD"])

    def test_a_crossed_stablecoin_book_is_not_a_rate(self):
        self.assertIsNone(stablecoin_mid(D("1.01"), D("0.99")))
        mid, deviation = stablecoin_mid(D("1.0002"), D("1.0003"))
        self.assertEqual(mid, D("1.00025"))
        self.assertLess(deviation, D("25"))


if __name__ == "__main__":
    unittest.main()
