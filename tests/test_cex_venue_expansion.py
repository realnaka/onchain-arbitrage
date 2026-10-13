"""Bybit and Bitget as monitoring venues, tested offline.

Added 2026-09-16 on the operator's instruction ("也监控 bybit 和 bitget 的股票
合约，看看哪里的交易机会比较多"). Three things here are load-bearing and are
therefore pinned with fixtures rather than trusted:

1. **The multiplier comes from the venue's own field.** Bitget's ``sizeMultiplier``
   is 0.01 for KUAISHOU (0.0001 for BTCUSDT — the cross-check), while ccxt's
   unified market map reports ``contractSize = 1`` for the same symbol. The venue
   wins; a 100x hedge error starts with trusting the wrong one.
2. **Bybit's catalogue is paginated.** Its default page is 500 rows and the
   linear catalogue is 873, so a single default call silently hid four of our six
   assets until the limit was raised.
3. **HKD-number siblings stay excluded.** XIAOMIHKD / TENCENTHKD / SHEINHKD trade
   in HKD per share: a different instrument on a different ruler, and subtracting
   them from a USD token price is the oldest mistake this project has.
"""
from __future__ import annotations

from decimal import Decimal
import unittest

from arb_monitor.core.hk import Contract
from arb_monitor.data.hk_cex import (CexPublicAdapter, parse_book,
                                     parse_contracts)


def bybit_row(symbol="KUAISHOUUSDT", base="KUAISHOU", status="Trading",
              qty_step="0.1", tick="0.01"):
    return {"symbol": symbol, "contractType": "LinearPerpetual", "status": status,
            "baseCoin": base, "quoteCoin": "USDT", "settleCoin": "USDT",
            "priceFilter": {"tickSize": tick},
            "lotSizeFilter": {"qtyStep": qty_step, "minOrderQty": qty_step,
                              "minNotionalValue": "5"},
            "symbolType": "stock", "marketRegion": "HK",
            "underlyingTicker": "01024", "fullName": "Kuaishou Technology"}


def bitget_row(symbol="KUAISHOUUSDT", base="KUAISHOU", size="0.01",
               status="normal", price_place="3"):
    return {"symbol": symbol, "baseCoin": base, "quoteCoin": "USDT",
            "sizeMultiplier": size, "minTradeNum": size, "minTradeUSDT": "5",
            "pricePlace": price_place, "volumePlace": "2",
            "symbolStatus": status, "isRwa": "YES"}


def gate_row(name="AAPL_USDT", multiplier="0.01", min_size="1", tick="0.01",
             contract_type="stocks", status="trading"):
    """One row of Gate's ``/futures/usdt/contracts`` catalogue."""
    return {"name": name, "contract_type": contract_type,
            "quanto_multiplier": multiplier, "order_size_min": min_size,
            "order_price_round": tick, "status": status, "type": "direct",
            "in_delisting": False}


class GateParsingTests(unittest.TestCase):
    """One unusable catalogue row must not delete the whole venue.

    Measured 2026-09-22: Gate lists ``SOXS_USDT`` with ``order_size_min = 0``.
    While the parser raised on that row, the discovery loop kept only the union
    of the venues that parsed, so **Gate vanished from the registry** and its
    books stopped being sampled — every other venue kept reporting, so the
    summary looked healthy while one of the eight venues was silently gone.
    """

    def test_a_row_with_no_minimum_is_dropped_and_the_venue_survives(self):
        rows, skipped = self.parse([gate_row("SOXS_USDT", min_size="0"),
                                    gate_row("AAPL_USDT")])
        # The 152 usable rows are still here; only the broken one is gone.
        self.assertEqual([c.instrument_id for c in rows], ["AAPL_USDT"])
        self.assertEqual([row["instrument"] for row in skipped], ["SOXS_USDT"])
        self.assertEqual(skipped[0]["reason"], "nonpositive_spec")
        # 0 is "unknown", never "no minimum": the dropped row leaves no contract
        # behind that would size an order from a zero floor.
        self.assertEqual(skipped[0]["spec"]["min_contracts"], "0")

    def test_a_zero_tick_or_multiplier_is_dropped_too(self):
        rows, skipped = self.parse([gate_row("AAPL_USDT", tick="0"),
                                    gate_row("MSFT_USDT", multiplier="0"),
                                    gate_row("TSLA_USDT")])
        self.assertEqual([c.instrument_id for c in rows], ["TSLA_USDT"])
        self.assertEqual({row["instrument"] for row in skipped},
                         {"AAPL_USDT", "MSFT_USDT"})

    def test_the_call_without_a_skip_list_still_refuses(self):
        # Callers that ask for no report get the old strict behaviour, so a
        # parse that silently produced junk cannot hide behind the new one.
        with self.assertRaises(ValueError):
            parse_contracts("gate", [gate_row("SOXS_USDT", min_size="0")], 1)

    def parse(self, rows):
        skipped: list[dict] = []
        contracts = parse_contracts("gate", rows, 1_000_000, skipped=skipped)
        return contracts, skipped


class BybitParsingTests(unittest.TestCase):
    def parse(self, rows):
        return parse_contracts("bybit", {"result": {"list": rows}}, 1_000_000)

    def test_a_stock_perp_becomes_a_comparable_usd_contract(self):
        rows = self.parse([bybit_row()])
        self.assertEqual(len(rows), 1)
        c = rows[0]
        self.assertEqual(c.asset, "KUAISHOU")
        self.assertEqual(c.instrument_id, "KUAISHOUUSDT")
        self.assertEqual(c.multiplier, Decimal("1"))     # Bybit linear = base units
        self.assertEqual(c.lot_size, Decimal("0.1"))
        self.assertEqual(c.min_contracts, Decimal("0.1"))
        self.assertEqual(c.tick_size, Decimal("0.01"))
        self.assertEqual(c.payout_model, "usd_linear")
        self.assertEqual(c.category, "hk_stock_candidate")
        # The venue's own identification stays in the raw record.
        self.assertEqual(c.raw["underlyingTicker"], "01024")
        self.assertEqual(c.raw["marketRegion"], "HK")

    def test_only_trading_contracts_are_active(self):
        rows = self.parse([bybit_row(status="PreLaunch")])
        self.assertFalse(rows[0].active)

    def test_hkd_siblings_are_dropped(self):
        rows = self.parse([bybit_row(symbol="XIAOMIHKDUSDT", base="XIAOMIHKD")])
        self.assertEqual(rows, [])

    def test_an_unknown_name_is_not_promoted(self):
        rows = self.parse([bybit_row(symbol="BYDUSDT", base="BYD")])
        self.assertEqual(rows[0].payout_model, "unknown")
        self.assertEqual(rows[0].category, "hk_related_unmatched")


class BitgetParsingTests(unittest.TestCase):
    def parse(self, rows):
        return parse_contracts("bitget", {"data": rows}, 1_000_000)

    def test_the_venue_multiplier_is_used_not_the_unified_one(self):
        c = self.parse([bitget_row()])[0]
        # 1 contract = 0.01 share, straight from sizeMultiplier. ccxt's unified
        # map says 1 for this symbol; using it would size a hedge 100x too big.
        self.assertEqual(c.multiplier, Decimal("0.01"))
        self.assertEqual(c.asset, "KUAISHOU")
        self.assertEqual(c.payout_model, "usd_linear")
        self.assertEqual(c.tick_size, Decimal("0.001"))

    def test_btcusdt_confirms_what_size_multiplier_means(self):
        # The live cross-check that makes the KUAISHOU read safe is BTCUSDT: its
        # sizeMultiplier is 0.0001 while KUAISHOU's is 0.01, so the field is base
        # coins per contract rather than a display scale. BTC itself is not a
        # stock we track, so it is dropped by classification — but a *tracked*
        # name with a different multiplier must follow the venue's number.
        mixed = self.parse([
            bitget_row(),
            bitget_row(symbol="MINIMAXUSDT", base="MINIMAX", size="0.1"),
            bitget_row(symbol="BTCUSDT", base="BTC", size="0.0001"),
        ])
        self.assertEqual([(c.asset, c.multiplier) for c in mixed],
                         [("KUAISHOU", Decimal("0.01")),
                          ("MINIMAX", Decimal("0.1"))])

    def test_non_normal_status_is_inactive(self):
        self.assertFalse(self.parse([bitget_row(status="offline")])[0].active)

    def test_hkd_siblings_are_dropped(self):
        self.assertEqual(
            self.parse([bitget_row(symbol="SHEINHKDUSDT", base="SHEINHKD")]), [])


class FakeExchange:
    """Just enough of a ccxt exchange for the adapter's new branches."""

    def __init__(self, *, pages=None, book=None, ticker=None, funding=None,
                 merged=None):
        self.pages = list(pages or [])
        self.book, self.ticker, self.funding, self.merged = book, ticker, funding, merged
        self.calls: list[dict] = []

    def publicGetV5MarketInstrumentsInfo(self, params):
        self.calls.append(dict(params))
        page = self.pages.pop(0) if self.pages else {"result": {"list": []}}
        return page

    def publicGetV5MarketOrderbook(self, params):
        return self.book

    def publicGetV5MarketTickers(self, params):
        return self.ticker

    def publicMixGetV2MixMarketMergeDepth(self, params):
        return self.merged

    def publicMixGetV2MixMarketCurrentFundRate(self, params):
        return self.funding

    def publicMixGetV2MixMarketTicker(self, params):
        return self.ticker

    def fapiPublicGetV1PremiumIndex(self, params):
        self.calls.append(dict(params))
        return self.funding


def simple_contract(venue, instrument_id, asset="XIAOMI"):
    return Contract(venue=venue, instrument_id=instrument_id, asset=asset,
                    category="linear", payout_model="usd_linear",
                    multiplier=Decimal("1"), lot_size=Decimal("1"),
                    min_contracts=Decimal("1"), min_notional=Decimal("5"),
                    tick_size=Decimal("0.001"), active=True,
                    source="test", recv_ts_ms=1)


class AdapterTests(unittest.TestCase):
    def adapter(self, venue, **kwargs):
        a = CexPublicAdapter.__new__(CexPublicAdapter)   # skip ccxt construction
        a.venue = venue
        a.exchange = FakeExchange(**kwargs)
        a.version = "test"
        return a

    def test_bybit_pages_until_the_cursor_runs_out(self):
        a = self.adapter("bybit", pages=[
            {"result": {"list": [bybit_row()], "nextPageCursor": "next"}},
            {"result": {"list": [bybit_row(symbol="MEITUANUSDT", base="MEITUAN")],
                        "nextPageCursor": None}},
        ])
        contracts, raw, _ts = a.instruments()
        self.assertEqual([c.asset for c in contracts], ["KUAISHOU", "MEITUAN"])
        # limit=1000 on every page: the default 500 is what hid four assets.
        self.assertEqual([call.get("limit") for call in a.exchange.calls], [1000, 1000])
        self.assertEqual(a.exchange.calls[1].get("cursor"), "next")

    def test_bybit_book_and_funding_are_normalised(self):
        a = self.adapter("bybit",
                         book={"result": {"b": [["3.80", "90.2"]],
                                          "a": [["3.82", "152.5"]], "ts": 1_789_000_000_000, "u": 42}},
                         ticker={"result": {"list": [{"fundingRate": "-0.0002",
                                                      "nextFundingTime": "1789574400000",
                                                      "markPrice": "3.81",
                                                      "time": "1789563686927"}]}})
        contract = parse_contracts("bybit", {"result": {"list": [bybit_row()]}},
                                   1)[0]
        book = a.book(contract)
        self.assertEqual(book.bids[0].price, Decimal("3.80"))
        self.assertEqual(book.asks[0].price, Decimal("3.82"))
        self.assertEqual(book.venue_ts_ms, 1_789_000_000_000)
        self.assertEqual(book.sequence, 42)
        funding = a.funding(contract)
        self.assertEqual(funding["rate"], Decimal("-0.0002"))
        self.assertEqual(funding["next_funding_ts_ms"], 1_789_574_400_000)
        self.assertEqual(funding["mark_price"], Decimal("3.81"))

    def test_bitget_book_and_funding_are_normalised(self):
        a = self.adapter("bitget",
                         merged={"data": {"bids": [["3.805", "2.65"]],
                                          "asks": [["3.819", "8.15"]],
                                          "ts": "1789563688516"}},
                         funding={"data": [{"fundingRate": "0",
                                            "nextUpdate": "1789574400000"}]},
                         ticker={"data": [{"ts": "1789563688516",
                                           "markPrice": "3.807"}]})
        contract = parse_contracts("bitget", {"data": [bitget_row()]}, 1)[0]
        book = a.book(contract)
        self.assertEqual(book.bids[0].price, Decimal("3.805"))
        self.assertEqual(book.asks[0].price, Decimal("3.819"))
        self.assertEqual(book.venue_ts_ms, 1_789_563_688_516)
        funding = a.funding(contract)
        self.assertEqual(funding["rate"], Decimal("0"))
        self.assertEqual(funding["next_funding_ts_ms"], 1_789_574_400_000)

    def test_a_crossed_book_is_still_rejected(self):
        # The new venues reuse parse_book, so its invariants apply to them too.
        contract = parse_contracts("bybit", {"result": {"list": [bybit_row()]}}, 1)[0]
        with self.assertRaises(ValueError):
            parse_book(contract, {"bids": [["3.90", "1"]], "asks": [["3.80", "1"]],
                                  "ts": 1, "lastUpdateId": 1}, 2)


class FundingDispatchTests(unittest.TestCase):
    """Every venue reads funding through an endpoint that exists on *its* client.

    Measured 2026-09-17 15:47–16:47: 502 `source_error` rows in one hour, of
    which 198 were `AttributeError: '<venue>' object has no attribute
    'fapiPublicGetPremiumIndex'` — aster (5 contracts), hyperliquid (1) and
    lighter (4), once per funding cadence. The shared `else` branch assumed every
    remaining venue was Binance-shaped, so those three venues' funding series was
    never collected at all and the noise buried the real transport errors.
    """

    def adapter(self, venue, **kwargs):
        a = CexPublicAdapter.__new__(CexPublicAdapter)
        a.venue = venue
        a.exchange = FakeExchange(**kwargs)
        a.version = "test"
        return a

    def test_aster_reads_the_v1_premium_index_not_binances(self):
        a = self.adapter("aster", funding={
            "symbol": "XIAOMIUSDT", "markPrice": "3.40300000",
            "indexPrice": "3.33524480", "lastFundingRate": "0",
            "nextFundingTime": "1789660800000", "time": "1789635140000"})
        funding = a.funding(simple_contract("aster", "XIAOMIUSDT"))
        self.assertEqual(funding["rate"], Decimal("0"))
        self.assertEqual(funding["next_funding_ts_ms"], 1_789_660_800_000)
        self.assertEqual(funding["mark_price"], Decimal("3.40300000"))
        self.assertEqual(funding["source"], "ccxt_public:aster:funding")
        self.assertEqual(a.exchange.calls, [{"symbol": "XIAOMIUSDT"}])

    def test_hourly_settling_venues_report_no_rate_instead_of_raising(self):
        # Both settle hourly, so their native `funding` field is not the 8-hourly
        # rate these columns are shaped for. Copying it across would invent an 8x
        # error in a cost; the honest result is "no rate", as data.
        for venue, instrument in (("hyperliquid", "xyz:SHEIN"), ("lighter", "203")):
            funding = self.adapter(venue).funding(simple_contract(venue, instrument))
            self.assertIsNone(funding["rate"])
            self.assertIsNone(funding["next_funding_ts_ms"])
            self.assertEqual(funding["source"],
                             f"ccxt_public:{venue}:funding_unwired")
            self.assertIn("待确认", funding["note"])

    def test_an_unwired_venue_names_itself(self):
        # An unknown venue is a wiring bug. The message has to say which venue,
        # or the next reader greps ccxt's version history instead of this file.
        with self.assertRaises(ValueError) as caught:
            self.adapter("edgex").funding(simple_contract("edgex", "XIAOMIUSDT"))
        self.assertIn("edgex", str(caught.exception))


class ConfigTests(unittest.TestCase):
    def test_the_new_venues_are_monitored_but_not_executable(self):
        from arb_monitor.apps.account_dashboard import load_toml, trade_universe

        monitor = load_toml("config/hk_monitor.toml")
        # 2026-09-23: sampling is OKX only. Bybit/Bitget stay in the adapters,
        # not in this process's venue list. Execution was already OKX-only.
        self.assertEqual(monitor["monitor"]["venues"], ["okx"])
        policy = load_toml("config/automation_policy.toml")
        self.assertEqual(policy["allowlist"]["venues"], ["okx"])
        universe = trade_universe()
        traded = {row["venue"] for row in universe["auto_executable"]}
        self.assertEqual(traded, {"okx"})
        # The policy catalogue still names the other venues. The sampling
        # process does not read them: `monitor.venues` above is the list
        # that decides which books are fetched.
        monitored = {(row["venue"], row["instrument"])
                     for row in universe["monitor_only"]}
        self.assertIn(("bybit", "KUAISHOUUSDT"), monitored)
        self.assertIn(("bitget", "KUAISHOUUSDT"), monitored)


if __name__ == "__main__":
    unittest.main()
