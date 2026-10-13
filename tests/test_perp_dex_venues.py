"""Aster / Hyperliquid / Lighter as read-only monitoring venues, tested offline.

Added 2026-09-17 on the operator's instruction ("Aster / Lighter / edgeX / HL
要接成只读行情"). Everything here is pinned with fixtures rather than trusted,
because each venue breaks a different assumption:

1. **Aster is Binance-shaped but must not inherit Binance's blanket exclusion.**
   Binance's HK names were excluded wholesale (they mix HKD-number ticks), yet
   Aster's name tickers are plainly USDT and include KUAISHOU and SHEIN — which
   are *not* in this project's ``CORE`` set, so without a promotion they would
   silently never be evaluated.
2. **Hyperliquid's stock perps are not in Hyperliquid's own universe.** They live
   in a builder-deployed dex, and the price grid is derived from the venue's
   five-significant-figure rule rather than copied from a CEX.
3. **Lighter publishes the multiplier itself** ("1.000000000000000000"), which is
   the number that decides whether a basis is real or a unit artefact — so it is
   read from the market row, not assumed to be 1.
4. **Lighter's REST book carries no venue clock.** The adapter's fallback anchors
   freshness to the market's latest *trade*, which fails closed: an illiquid
   market is reported as stale instead of being passed off as a live price.
   Since 2026-09-22 the preferred path is the venue's order-book WebSocket, whose
   frames carry the venue's own timestamp — see ``test_lighter_stream.py``. The
   tests here pin the *fallback*: a REST-only book must stay unusable when it has
   no clock, so a dead socket degrades to less data, never to an unclocked price.
"""
from __future__ import annotations

from decimal import Decimal
import unittest

from arb_monitor.core.mapping_evidence import verify_venue_contract
from arb_monitor.data.hk_cex import _hl_tick, parse_book, parse_contracts


def aster_row(symbol="XIAOMIUSDT", base="XIAOMI", step="1", min_qty="1",
              tick="0.00100", notional="5", status="TRADING",
              ctype="PERPETUAL"):
    return {
        "symbol": symbol, "baseAsset": base, "quoteAsset": "USDT",
        "contractType": ctype, "status": status,
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": tick},
            {"filterType": "LOT_SIZE", "stepSize": step, "minQty": min_qty},
            {"filterType": "MIN_NOTIONAL", "notional": notional},
        ],
    }


def hl_payload(names, marks=None):
    universe = [{"name": name, "szDecimals": 1, "maxLeverage": 10} for name in names]
    ctxs = [{"markPx": str((marks or {}).get(name, "5.0")), "oraclePx": "5.0"}
            for name in names]
    return [{"universe": universe}, ctxs]


def lighter_row(symbol="XIAOMI", market_id=203, multiplier="1",
                market_type="perp", status="active"):
    return {
        "symbol": symbol, "market_id": market_id, "market_type": market_type,
        "status": status, "multiplier": multiplier,
        "supported_size_decimals": 2, "supported_price_decimals": 4,
        "min_base_amount": "2.50", "min_quote_amount": "10.000000",
    }


class AsterTests(unittest.TestCase):
    def test_a_usd_name_ticker_is_kept_with_its_own_filters(self):
        rows = parse_contracts("aster", {"symbols": [aster_row()]}, 1)
        self.assertEqual(len(rows), 1)
        contract = rows[0]
        self.assertEqual(contract.asset, "XIAOMI")
        self.assertEqual(contract.multiplier, Decimal("1"))
        self.assertEqual(contract.min_notional, Decimal("5"))
        self.assertEqual(contract.tick_size, Decimal("0.00100"))
        self.assertEqual(contract.payout_model, "usd_linear")

    def test_kuaishou_is_promoted_rather_than_left_unknown(self):
        # KUAISHOU is not in CORE; without the venue promotion it would sit at
        # pay-out "unknown" and never be comparable to the token.
        rows = parse_contracts("aster", {"symbols": [
            aster_row(symbol="KUAISHOUUSDT", base="KUAISHOU", step="0.01",
                      min_qty="0.01", tick="0.00100")]}, 1)
        self.assertEqual(rows[0].payout_model, "usd_linear")
        self.assertEqual(rows[0].multiplier, Decimal("1"))

    def test_hk_code_tickers_are_excluded_like_binances(self):
        rows = parse_contracts("aster", {"symbols": [
            aster_row(symbol="HK0700USDT", base="HK0700")]}, 1)
        self.assertEqual(rows, [])

    def test_a_halted_contract_is_kept_but_flagged_inactive(self):
        # Same shape as the other venues: the row survives so that
        # `mapping-verify` can report "the contract exists but is not trading"
        # instead of the pair silently vanishing from the catalogue.
        rows = parse_contracts("aster", {"symbols": [aster_row(status="SETTLING")]}, 1)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].active)

    def test_a_non_perpetual_contract_type_is_dropped(self):
        rows = parse_contracts("aster", {"symbols": [aster_row(ctype="FUTURES")]}, 1)
        self.assertEqual(rows, [])


class HyperliquidTests(unittest.TestCase):
    def test_the_dex_prefix_is_kept_and_the_ticker_is_the_asset(self):
        rows = parse_contracts("hyperliquid", hl_payload(["xyz:SHEIN"]), 1)
        self.assertEqual(len(rows), 1)
        contract = rows[0]
        self.assertEqual(contract.instrument_id, "xyz:SHEIN")
        self.assertEqual(contract.asset, "SHEIN")
        self.assertEqual(contract.multiplier, Decimal("1"))
        self.assertEqual(contract.payout_model, "usd_linear")

    def test_an_unknown_ticker_is_not_invented_into_our_universe(self):
        rows = parse_contracts("hyperliquid", hl_payload(["xyz:NOTASTOCK"]), 1)
        self.assertEqual(rows, [])

    def test_the_minimum_order_value_is_left_unknown_not_guessed(self):
        # The venue's global floor is not in this payload; a made-up number would
        # silently mark real depth as "below minimum".
        rows = parse_contracts("hyperliquid", hl_payload(["xyz:SHEIN"]), 1)
        self.assertEqual(rows[0].min_notional, Decimal("0"))

    def test_size_step_comes_from_sz_decimals(self):
        payload = hl_payload(["xyz:SHEIN"])
        payload[0]["universe"][0]["szDecimals"] = 3
        rows = parse_contracts("hyperliquid", payload, 1)
        self.assertEqual(rows[0].lot_size, Decimal("0.001"))
        self.assertEqual(rows[0].min_contracts, Decimal("0.001"))

    def test_price_grid_follows_five_significant_figures(self):
        self.assertEqual(_hl_tick("5.0434"), Decimal("0.0001"))
        self.assertEqual(_hl_tick("54.60"), Decimal("0.001"))
        self.assertEqual(_hl_tick("428.09"), Decimal("0.01"))
        self.assertEqual(_hl_tick("76226.9"), Decimal("1"))
        # Degenerate input must not raise inside the sampling loop.
        self.assertGreater(_hl_tick(None), 0)
        self.assertGreater(_hl_tick("0"), 0)

    def test_the_delisted_flag_is_honoured(self):
        payload = hl_payload(["xyz:SHEIN"])
        payload[0]["universe"][0]["isDelisted"] = True
        self.assertEqual(parse_contracts("hyperliquid", payload, 1), [])


class LighterTests(unittest.TestCase):
    def test_the_published_multiplier_is_used_not_assumed(self):
        rows = parse_contracts("lighter", {"order_books": [
            lighter_row(multiplier="1.000000000000000000")]}, 1)
        self.assertEqual(rows[0].multiplier, Decimal("1.000000000000000000"))
        # And if the venue ever changed it, the contract follows the venue.
        rows = parse_contracts("lighter", {"order_books": [
            lighter_row(multiplier="0.01")]}, 1)
        self.assertEqual(rows[0].multiplier, Decimal("0.01"))

    def test_spot_markets_are_not_perpetuals(self):
        rows = parse_contracts("lighter", {"order_books": [
            lighter_row(market_type="spot")]}, 1)
        self.assertEqual(rows, [])

    def test_order_floors_come_from_the_market_row(self):
        rows = parse_contracts("lighter", {"order_books": [lighter_row()]}, 1)
        contract = rows[0]
        self.assertEqual(contract.instrument_id, "203")
        self.assertEqual(contract.min_contracts, Decimal("2.50"))
        self.assertEqual(contract.min_notional, Decimal("10.000000"))
        self.assertEqual(contract.lot_size, Decimal("0.01"))
        self.assertEqual(contract.tick_size, Decimal("0.0001"))


class BookShapesTests(unittest.TestCase):
    """The two new book shapes must land in the same OrderBook as everyone else."""

    def contract(self, venue, instrument):
        rows = {
            "hyperliquid": parse_contracts(
                "hyperliquid", hl_payload(["xyz:SHEIN"]), 1)[0],
            "lighter": parse_contracts(
                "lighter", {"order_books": [lighter_row()]}, 1)[0],
        }
        return rows[venue]

    def test_hyperliquid_px_sz_levels_are_normalised(self):
        contract = self.contract("hyperliquid", "xyz:SHEIN")
        book = parse_book(contract, {
            "bids": [["5.03", "10"]], "asks": [["5.05", "20"]], "ts": 1_789_624_621_924,
            "lastUpdateId": None}, 1_789_624_621_930)
        self.assertEqual(book.bids[0].price, Decimal("5.03"))
        self.assertEqual(book.bids[0].quantity, Decimal("10"))
        self.assertEqual(book.asks[0].quantity, Decimal("20"))
        self.assertEqual(book.venue_ts_ms, 1_789_624_621_924)

    def test_lighter_rest_fallback_anchors_on_the_last_trade_and_stays_stale(self):
        contract = self.contract("lighter", "203")
        book = parse_book(contract, {
            "bids": [["3.30", "15.00"]], "asks": [["3.33", "5602.72"]],
            "ts": 1_789_622_878_501, "lastUpdateId": None}, 1_789_624_601_961)
        self.assertEqual(book.bids[0].price, Decimal("3.30"))
        self.assertEqual(book.venue_ts_ms, 1_789_622_878_501)
        # A book whose only clock is a 28-minute-old trade must read as stale,
        # which is what keeps an illiquid venue from looking like a live price.
        self.assertFalse(book.is_fresh(book.recv_ts_ms, 15_000))

    def test_a_missing_venue_clock_is_never_passed_off_as_fresh(self):
        contract = self.contract("lighter", "203")
        book = parse_book(contract, {
            "bids": [["3.30", "15.00"]], "asks": [["3.33", "1"]],
            "ts": None, "lastUpdateId": None}, 1_789_624_601_961)
        self.assertIsNone(book.venue_ts_ms)
        self.assertFalse(book.is_fresh(book.recv_ts_ms + 1, 15_000))


class VenueIdentityTests(unittest.TestCase):
    """Layer 4 for the new venues: how big is a contract, and which stock is it."""

    def spec(self, **over):
        base = {"token_symbol": "XIAOx", "perp_value_currency": "XIAOMI",
                "perp_contract_multiplier": "1", "shares_per_token": "1",
                "shares_per_perp_unit": "1", "issuer_underlying_symbol": "1810"}
        base.update(over)
        return base

    def names(self, checks):
        return {c.name: c.ok for c in checks}

    def test_aster_identity_uses_the_name_ticker(self):
        checks = verify_venue_contract(spec=self.spec(), venue_contract={
            "venue": "aster", "instrument_id": "XIAOMIUSDT",
            "multiplier": "1", "active": True,
            "raw": {"baseAsset": "XIAOMI", "quoteAsset": "USDT"}})
        self.assertTrue(self.names(checks)["perp_venue_identity"])
        self.assertTrue(self.names(checks)["perp_contract_size"])

    def test_aster_hk_code_ticker_is_refused(self):
        checks = verify_venue_contract(spec=self.spec(), venue_contract={
            "venue": "aster", "instrument_id": "HK1810USDT",
            "multiplier": "1", "active": True,
            "raw": {"baseAsset": "HK1810", "quoteAsset": "USDT"}})
        self.assertFalse(self.names(checks)["perp_venue_identity"])

    def test_hyperliquid_identity_needs_the_dex_prefix_and_the_ticker(self):
        good = verify_venue_contract(spec=self.spec(perp_value_currency="SHEIN",
                                                    token_symbol="SHEINx"),
                                     venue_contract={
            "venue": "hyperliquid", "instrument_id": "xyz:SHEIN",
            "multiplier": "1", "active": True, "raw": {"name": "xyz:SHEIN"}})
        self.assertTrue(self.names(good)["perp_venue_identity"])
        wrong = verify_venue_contract(spec=self.spec(perp_value_currency="SHEIN",
                                                     token_symbol="SHEINx"),
                                      venue_contract={
            "venue": "hyperliquid", "instrument_id": "xyz:TENCENT",
            "multiplier": "1", "active": True, "raw": {"name": "xyz:TENCENT"}})
        self.assertFalse(self.names(wrong)["perp_venue_identity"])

    def test_lighter_identity_requires_a_live_perp_with_the_right_symbol(self):
        good = verify_venue_contract(spec=self.spec(), venue_contract={
            "venue": "lighter", "instrument_id": "203", "multiplier": "1",
            "active": True,
            "raw": {"symbol": "XIAOMI", "market_type": "perp",
                    "status": "active", "multiplier": "1"}})
        self.assertTrue(self.names(good)["perp_venue_identity"])
        spot = verify_venue_contract(spec=self.spec(), venue_contract={
            "venue": "lighter", "instrument_id": "203", "multiplier": "1",
            "active": True,
            "raw": {"symbol": "XIAOMI", "market_type": "spot",
                    "status": "active", "multiplier": "1"}})
        self.assertFalse(self.names(spot)["perp_venue_identity"])


if __name__ == "__main__":
    unittest.main()
