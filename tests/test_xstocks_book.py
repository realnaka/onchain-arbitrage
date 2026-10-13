"""Hermetic tests for the standalone xStocks book. No network."""
from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from arb_monitor.xstocks_book import parse
from arb_monitor.xstocks_book.jobs import refresh, sample_once
from arb_monitor.xstocks_book.report import render
from arb_monitor.xstocks_book.store import BookStore

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000_000
OPEN_START = 1_700_000_000
OPEN_END = 1_700_003_600


class FakeClient:
    def __init__(self, handler):
        self.handler = handler
        self.urls: list[str] = []

    def get_json(self, url: str):
        self.urls.append(url)
        return self.handler(url)


def _chart(price, close, start, end, currency="USD"):
    return {"chart": {"result": [{
        "meta": {
            "currency": currency,
            "regularMarketPrice": price,
            "currentTradingPeriod": {"regular": {"start": start, "end": end}},
        },
        "timestamp": [OPEN_START],
        "indicators": {"quote": [{"close": [close]}]},
    }]}}


def _pools(address, price, reserve="5000", volume="12"):
    prefix = "solana" if not str(address).lower().startswith("0x") else "eth"
    return {"data": [{
        "id": f"{prefix}_pool",
        "attributes": {
            "reserve_in_usd": reserve,
            "base_token_price_usd": price,
            "volume_usd": {"h24": volume},
        },
        "relationships": {"base_token": {"data": {"id": f"{prefix}_{address}"}}},
    }]}


def _depth(bid, size, ask="101"):
    return {"error": [], "result": {"PAIR": {
        "bids": [[bid, size, 1]], "asks": [[ask, "1", 1]],
    }}}


def _assets(page: int):
    if page == 0:
        return {"page": {"hasNextPage": True}, "nodes": [_aapl(), _tsla()]}
    if page == 1:
        return {"page": {"hasNextPage": False}, "nodes": [_hk()]}
    raise AssertionError(page)


def _aapl():
    return {
        "symbol": "AAPLx",
        "name": "Apple xStock",
        "isin": "CH-AAPL",
        "underlyingSymbol": "AAPL",
        "underlyingIsin": "US0378331005",
        "underlyingName": "Apple Inc.",
        "minOrderFiatValue": "1000",
        "multiplier": "1000000000000000000",
        "deployments": [{
            "network": "Ethereum",
            "chainId": 1,
            "address": "0xAAA",
            "wrapperAddressV2": "0xBBB",
            "atomicSwaps": True,
            "tokens": [{"symbol": "USDC", "address": "0xUSDC", "redemption": True}],
        }, {
            "network": "Solana",
            "address": "XsAppleMint",
            "atomicSwaps": False,
        }],
    }


def _tsla():
    return {
        "symbol": "TSLAx",
        "name": "Tesla xStock",
        "underlyingSymbol": "TSLA",
        "underlying": {"listingCountry": "US", "name": "Tesla Inc.", "isin": "US88160R1014"},
        "sharesPerToken": "2",
        "deployments": [{
            "network": "Ethereum",
            "chainId": 1,
            "address": "0xTSLA",
            "sharesPerToken": "1",
        }],
    }


def _hk():
    return {
        "symbol": "XIAOx",
        "name": "Xiaomi xStock",
        "underlyingSymbol": "1810",
        "underlying": {"listingCountry": "HK", "name": "Xiaomi", "isin": "KYG9830T1067"},
        "sharesPerToken": "1",
        "deployments": [{
            "network": "X Layer",
            "chainId": 196,
            "address": "0xXIAO",
        }],
    }


def _pairs():
    return {"error": [], "result": {
        "AAPLxUSD": {
            "altname": "AAPLxUSD", "wsname": "AAPLx/USD", "aclass_base": "tokenized_asset",
            "base": "AAPLx", "quote": "ZUSD", "ordermin": "0.5", "status": "online",
            "lot_decimals": 8, "pair_decimals": 2,
        },
        "TSLAxUSD": {
            "altname": "TSLAxUSD", "aclass_base": "tokenized_asset",
            "base": "TSLAx", "quote": "ZUSD", "status": "online",
        },
        "XIAOxUSD": {
            "altname": "XIAOxUSD", "aclass_base": "tokenized_asset",
            "base": "XIAOx", "quote": "ZUSD", "status": "online",
        },
        "NVDAxUSD": {
            "altname": "NVDAxUSD", "aclass_base": "tokenized_asset",
            "base": "NVDAx", "quote": "ZUSD", "status": "online",
        },
        "XXBTZUSD": {"aclass_base": "currency", "base": "XXBT", "quote": "ZUSD", "altname": "XBTUSD"},
    }}


def _bridges():
    return [{
        "network": "Ethereum",
        "contract": "0xBRIDGE",
        "chainId": 1,
        "managedBy": "ChainlinkCCIP",
        "destinationNetworks": ["Solana"],
        "products": [{"symbol": "AAPLx", "address": "0xAAA"}],
    }]


class ParseTests(unittest.TestCase):
    def test_share_ratio_ignores_wei_scaled_multiplier(self):
        self.assertIsNone(parse.shares_per_token({"multiplier": "1000000000000000000"}, None))
        self.assertEqual(parse.shares_per_token({"sharesPerToken": "1.003"}, None), Decimal("1.003"))

    def test_yahoo_symbol_pads_hk_and_leaves_us(self):
        self.assertEqual(parse.yahoo_symbol("625", "HK"), "0625.HK")
        self.assertEqual(parse.yahoo_symbol("1810", "HK"), "1810.HK")
        self.assertEqual(parse.yahoo_symbol("AAPL", "US"), "AAPL")
        self.assertIsNone(parse.yahoo_symbol("AAA", None))
        self.assertIsNone(parse.yahoo_symbol("AAA", "FR"))

    def test_closed_session_uses_the_daily_close_not_the_live_print(self):
        payload = _chart("999", "100", OPEN_START, OPEN_END)
        closed = parse.parse_equity(payload, NOW)
        self.assertEqual(closed["session"], "closed")
        self.assertEqual(closed["kind"], "previous_close")
        self.assertEqual(closed["local_price"], Decimal("100"))
        noisy = _chart("341.07", "341.07000732421875", OPEN_START, OPEN_END)
        self.assertEqual(parse.parse_equity(noisy, NOW)["local_price"], Decimal("341.07"))
        open_at = (OPEN_START + 10) * 1000
        opened = parse.parse_equity(payload, open_at)
        self.assertEqual(opened["session"], "open")
        self.assertEqual(opened["local_price"], Decimal("999"))

    def test_missing_session_clock_is_unknown_but_keeps_the_close(self):
        payload = _chart("50", "100", OPEN_START, OPEN_END)
        payload["chart"]["result"][0]["meta"].pop("currentTradingPeriod")
        parsed = parse.parse_equity(payload, NOW)
        self.assertEqual(parsed["session"], "calendar_unknown")
        self.assertEqual(parsed["local_price"], Decimal("100"))

    def test_hkd_converts_by_dividing(self):
        self.assertEqual(parse.apply_fx(Decimal("78"), "divide", Decimal("7.8")), Decimal("10"))

    def test_bid_notional_keeps_only_the_50bps_band(self):
        book = parse.summarize_book(
            [(Decimal("100"), Decimal("1")), (Decimal("99"), Decimal("10"))],
            [(Decimal("100"), Decimal("1"))],
        )
        self.assertEqual(book["notional"], Decimal("100"))
        self.assertEqual(book["quality"], "ok")
        empty = parse.summarize_book([], [])
        self.assertEqual(empty["quality"], "empty_book")

    def test_zero_reserve_pool_is_not_a_price_and_zero_volume_is_stale(self):
        empty = parse.parse_gecko_pools(_pools("0xaaa", "90", reserve="0", volume="10"), "0xaaa")
        self.assertEqual(empty["quality"], "no_pool")
        stale = parse.parse_gecko_pools(_pools("0xaaa", "90", volume="0"), "0xaaa")
        self.assertEqual(stale["quality"], "stale")
        self.assertEqual(stale["price"], Decimal("90"))

    def test_screen_requires_a_share_ratio_a_known_session_and_a_bid(self):
        base = dict(equity_usd=Decimal("100"), equity_session="closed", token_usd=Decimal("90"),
                    shares=Decimal("1"), chain_quality="ok", bid_size=Decimal("2"),
                    quote_treatment_name="usd")
        hit = parse.decide_screen(**base)
        self.assertEqual(hit["screen_hit"], 1)
        self.assertEqual(hit["discount_bps"], Decimal("1000"))
        unknown = parse.decide_screen(**{**base, "shares": None})
        self.assertEqual(unknown["screen_hit"], 0)
        self.assertIsNone(unknown["discount_bps"])
        self.assertEqual(unknown["quality"], "share_ratio_unknown")
        no_bid = parse.decide_screen(**{**base, "bid_size": Decimal("0")})
        self.assertEqual(no_bid["screen_hit"], 0)
        stale = parse.decide_screen(**{**base, "chain_quality": "stale"})
        self.assertEqual(stale["screen_hit"], 0)
        self.assertEqual(stale["discount_bps"], Decimal("1000"))
        unknown_session = parse.decide_screen(**{**base, "equity_session": "calendar_unknown"})
        self.assertEqual(unknown_session["screen_hit"], 0)
        just_under = parse.decide_screen(**{**base, "token_usd": Decimal("99.501")})
        self.assertEqual(just_under["screen_hit"], 0)
        exact = parse.decide_screen(**{**base, "token_usd": Decimal("99.5")})
        self.assertEqual(exact["screen_hit"], 1)
        self.assertEqual(exact["discount_bps"], Decimal("50"))

    def test_currency_pair_is_not_an_xstock_exit(self):
        pairs = parse.parse_kraken_pairs(_pairs())
        self.assertNotIn("XBTUSD", [row["pair"] for row in pairs])
        self.assertIn("AAPLxUSD", [row["pair"] for row in pairs])

    def test_depth_url_is_the_tokenized_asset_book(self):
        self.assertIn("asset_class=tokenized_asset", parse.depth_url("AAPLxUSD"))

    def test_duplicate_kraken_keys_collapse_to_one_pair(self):
        payload = {"error": [], "result": {
            "AAOISPVUSD": {"altname": "AAOIxUSD", "base": "AAOIx", "quote": "ZUSD",
                           "aclass_base": "tokenized_asset", "status": "post_only"},
            "AAOIxUSD": {"altname": "AAOIxUSD", "base": "AAOIx", "quote": "ZUSD",
                         "aclass_base": "tokenized_asset", "status": "post_only"},
        }}
        rows = parse.parse_kraken_pairs(payload)
        self.assertEqual([row["pair"] for row in rows], ["AAOIxUSD"])

    def test_nested_bridge_deployment_does_not_cover_the_wrapper(self):
        bridges = [{
            "source_network": "Arbitrum",
            "chain_id": "42161",
            "products_json": json.dumps([{
                "symbol": "AAPLx",
                "deployments": [
                    {"network": "Arbitrum", "address": "0xAAA", "chainId": 42161},
                    {"network": "Tron", "address": "TADDR", "chainId": 728126428},
                ],
            }]),
        }]
        self.assertEqual(
            parse.exit_path("Arbitrum", "0xaaa", "AAPLx", "[]", bridges), "bridgeable")
        self.assertEqual(
            parse.exit_path("Arbitrum", "0xbbb", "AAPLx",
                            json.dumps([{"redemption": True}]), bridges),
            "redeem_only")
        self.assertEqual(parse.chain_ids_in_bridges(bridges)["Tron"], "728126428")
        bnb = [{
            "source_network": "BNB Smart Chain",
            "products_json": json.dumps([{
                "symbol": "AAPLx",
                "deployments": [{"network": "BinanceSmartChain", "address": "0xAAA"}],
            }]),
        }]
        self.assertEqual(
            parse.exit_path("BNB Smart Chain", "0xaaa", "AAPLx", "[]", bnb), "bridgeable")
        self.assertEqual(
            parse.exit_path("BNB Smart Chain", "0xbbb", "AAPLx", "[]", bnb), "unknown")

    def test_post_only_book_is_not_a_taker_exit(self):
        self.assertEqual(parse.book_quality("ok", "post_only"), "post_only")
        self.assertEqual(parse.book_quality("empty_book", "post_only"), "empty_book")
        self.assertEqual(parse.book_quality("ok", "online"), "ok")


class CatalogTests(unittest.TestCase):
    def _client(self, assets=_assets, kraken_assets=None):
        def handler(url: str):
            if url.startswith(parse.ASSETS_API):
                page = int(url.rsplit("=", 1)[-1])
                return assets(page)
            if url == parse.BRIDGES_API:
                return _bridges()
            if url == parse.KRAKEN_PAIRS:
                return _pairs()
            if url == parse.KRAKEN_ASSETS:
                if kraken_assets is not None:
                    raise RuntimeError("kraken assets down")
                return {"error": [], "result": {"AAPLx": {"altname": "AAPLx"}}}
            raise AssertionError(url)
        return FakeClient(handler)

    def test_refresh_keeps_every_chain_and_only_kraken(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BookStore(Path(tmp) / "book.sqlite")
            client = self._client()
            summary = refresh(store, client, now_ms=NOW, pause_s=0, sleep=lambda _s: None)
            self.assertEqual(summary["errors"], [])
            symbols = store.issuer_symbols()
            self.assertEqual(symbols, {"AAPLx", "TSLAx", "XIAOx"})
            deployments = {(row["issuer_symbol"], row["network"], row["form"], row["address"])
                           for row in store.live_deployments()}
            self.assertIn(("AAPLx", "Ethereum", "native", "0xaaa"), deployments)
            eth = next(row for row in store.live_deployments("AAPLx")
                       if row["network"] == "Ethereum" and row["form"] == "native")
            self.assertEqual(eth["chain_id"], "1")
            self.assertEqual(eth["atomic_swaps"], 1)
            sol = next(row for row in store.live_deployments("AAPLx")
                       if row["network"] == "Solana")
            self.assertIsNone(sol["chain_id"])
            self.assertIn(("AAPLx", "Ethereum", "wrapper_v2", "0xbbb"), deployments)
            self.assertIn(("AAPLx", "Solana", "native", "XsAppleMint"), deployments)
            self.assertNotIn(("AAPLx", "Solana", "wrapper_v2", "XsAppleMint"), deployments)
            self.assertIn(("XIAOx", "XLayer", "native", "0xxiao"), deployments)
            self.assertEqual(store.exit_venues(), ["kraken"])
            exits = {row["pair"]: row for row in store.live_exits()}
            self.assertEqual(set(exits), {"AAPLxUSD", "TSLAxUSD", "XIAOxUSD", "NVDAxUSD"})
            self.assertEqual(exits["AAPLxUSD"]["issuer_symbol"], "AAPLx")
            self.assertIsNone(exits["NVDAxUSD"]["issuer_symbol"])
            self.assertEqual(exits["AAPLxUSD"]["deposit_networks"], "unknown")
            aapl = next(row for row in store.live_underlyings() if row["issuer_symbol"] == "AAPLx")
            self.assertIsNone(aapl["shares_per_token"])
            tsla = next(row for row in store.live_deployments("TSLAx"))
            self.assertEqual(tsla["shares_per_token"], "1")
            self.assertTrue(all("bybit" not in url and "okx.com" not in url and "gate.com" not in url
                                and "hyperliquid" not in url for url in client.urls))
            text = render(store)
            self.assertIn("其他交易所未采集", text)
            self.assertNotIn("Bybit", text)
            store.close()

    def test_a_failed_refresh_does_not_mark_the_catalog_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BookStore(Path(tmp) / "book.sqlite")
            refresh(store, self._client(), now_ms=NOW, pause_s=0, sleep=lambda _s: None)

            def broken(page):
                return {"error": "nope"}

            failed = refresh(store, self._client(assets=broken), now_ms=NOW + 1,
                             pause_s=0, sleep=lambda _s: None)
            self.assertTrue(any(item.startswith("assets:") for item in failed["errors"]))
            self.assertEqual(store.issuer_symbols(), {"AAPLx", "TSLAx", "XIAOx"})

            def pairs_down(url):
                if url == parse.KRAKEN_PAIRS:
                    return {"error": ["EGeneral:busy"]}
                return self._client().handler(url)

            refresh(store, FakeClient(pairs_down), now_ms=NOW + 2, pause_s=0, sleep=lambda _s: None)
            self.assertEqual(len(store.live_exits()), 4)
            store.close()

    def test_a_successful_refresh_marks_a_removed_token_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BookStore(Path(tmp) / "book.sqlite")
            refresh(store, self._client(), now_ms=NOW, pause_s=0, sleep=lambda _s: None)

            def only_aapl(page):
                self.assertEqual(page, 0)
                return {"page": {"hasNextPage": False}, "nodes": [_aapl()]}

            refresh(store, self._client(assets=only_aapl), now_ms=NOW + 5,
                    pause_s=0, sleep=lambda _s: None)
            self.assertEqual(store.issuer_symbols(), {"AAPLx"})
            total = store.counts()["underlyings"]
            self.assertEqual(total, 3)
            missing = store.conn.execute(
                "SELECT issuer_symbol FROM underlyings WHERE missing_since_ms IS NOT NULL"
            ).fetchall()
            self.assertEqual(sorted(row["issuer_symbol"] for row in missing), ["TSLAx", "XIAOx"])
            store.close()

    def test_kraken_asset_lookup_failure_still_stores_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BookStore(Path(tmp) / "book.sqlite")
            summary = refresh(store, self._client(kraken_assets=True), now_ms=NOW,
                              pause_s=0, sleep=lambda _s: None)
            self.assertTrue(any(item.startswith("kraken_assets:") for item in summary["errors"]))
            self.assertEqual(len(store.live_exits()), 4)
            self.assertEqual(store.live_exits()[0]["deposit_networks"], "unknown")
            store.close()


class SampleTests(unittest.TestCase):
    def test_sample_screens_a_known_ratio_and_refuses_an_unknown_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BookStore(Path(tmp) / "book.sqlite")
            refresh(store, CatalogTests()._client(), now_ms=NOW, pause_s=0, sleep=lambda _s: None)
            open_at = (OPEN_START + 30) * 1000

            def handler(url: str):
                if "geckoterminal" in url and "0xaaa" in url:
                    return _pools("0xaaa", "90")
                if "geckoterminal" in url and "0xbbb" in url:
                    return _pools("0xbbb", "99.501")
                if "geckoterminal" in url and "0xtsla" in url:
                    return _pools("0xtsla", "50")
                if "geckoterminal" in url and "xsapplemint" in url.lower():
                    return _pools("XsAppleMint", "80")
                if "geckoterminal" in url and "0xxiao" in url:
                    return _pools("0xxiao", "9")
                if "HKD" in url:
                    return _chart("7.8", "7.8", OPEN_START, OPEN_END, "HKD")
                if "finance/chart/" in url and "AAPL" in url:
                    return _chart("100", "80", OPEN_START, OPEN_END)
                if "finance/chart/" in url and "TSLA" in url:
                    return _chart("100", "80", OPEN_START, OPEN_END)
                if "finance/chart/" in url and "1810" in url:
                    return _chart("78", "70", OPEN_START, OPEN_END, "HKD")
                if "Depth" in url and "AAPLxUSD" in url:
                    self.assertIn("asset_class=tokenized_asset", url)
                    return _depth("100", "2")
                if "Depth" in url and "TSLAxUSD" in url:
                    return _depth("100", "3")
                if "Depth" in url and "XIAOxUSD" in url:
                    return _depth("10", "4")
                if "Depth" in url and "NVDAxUSD" in url:
                    return _depth("100", "1")
                raise AssertionError(url)

            client = FakeClient(handler)
            summary = sample_once(store, client, now_ms=open_at, pause_s=0, sleep=lambda _s: None)
            self.assertEqual(summary["errors"], 0)
            self.assertGreaterEqual(summary["screen_hits"], 1)
            rows = {(row["issuer_symbol"], row["form"], row["address"]): row
                    for row in store.latest_observations("chain_ref")}
            aapl_native = rows[("AAPLx", "native", "0xaaa")]
            self.assertIsNone(aapl_native["discount_bps"])
            self.assertEqual(aapl_native["quality"], "share_ratio_unknown")
            self.assertEqual(aapl_native["screen_hit"], 0)
            self.assertEqual(aapl_native["price"], "90")
            aapl_wrapper = rows[("AAPLx", "wrapper_v2", "0xbbb")]
            self.assertNotEqual(aapl_wrapper["address"], aapl_native["address"])
            tsla = rows[("TSLAx", "native", "0xtsla")]
            self.assertEqual(Decimal(tsla["discount_bps"]), Decimal("5000"))
            self.assertEqual(tsla["screen_hit"], 1)
            detail = json.loads(tsla["detail_json"])
            self.assertTrue(detail["not_executable"])
            xiao = rows[("XIAOx", "native", "0xxiao")]
            self.assertEqual(Decimal(xiao["equity_usd"]), Decimal("10"))
            self.assertEqual(xiao["equity_session"], "open")
            self.assertEqual(xiao["screen_hit"], 1)
            native_detail = json.loads(aapl_native["detail_json"])
            wrapper_detail = json.loads(aapl_wrapper["detail_json"])
            self.assertEqual(native_detail["exit_path"], "bridgeable")
            self.assertEqual(wrapper_detail["exit_path"], "redeem_only")
            solana = rows[("AAPLx", "native", "XsAppleMint")]
            self.assertEqual(json.loads(solana["detail_json"])["exit_path"], "unknown")
            self.assertTrue(all("bybit" not in url and "okx.com" not in url for url in client.urls))
            text = render(store, "TSLAx")
            self.assertIn("5000 bps", text)
            self.assertIn("参考价不可执行", text)
            store.close()

    def test_closed_session_is_stored_and_an_empty_kraken_book_does_not_screen(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BookStore(Path(tmp) / "book.sqlite")
            refresh(store, CatalogTests()._client(), now_ms=NOW, pause_s=0, sleep=lambda _s: None)

            def handler(url: str):
                if "geckoterminal" in url:
                    return _pools("0xtsla", "90")
                if "finance/chart/" in url and "TSLA" in url:
                    return _chart("999", "100", OPEN_START, OPEN_END)
                if "finance/chart/" in url:
                    return _chart("100", "100", OPEN_START, OPEN_END)
                if "Depth" in url and "TSLAxUSD" in url:
                    return {"error": [], "result": {"PAIR": {"bids": [], "asks": []}}}
                if "Depth" in url:
                    return _depth("100", "1")
                if "HKD" in url:
                    return _chart("7.8", "7.8", OPEN_START, OPEN_END)
                raise AssertionError(url)

            sample_once(store, FakeClient(handler), now_ms=NOW, pause_s=0, limit=None,
                        sleep=lambda _s: None)
            tsla = next(row for row in store.latest_observations("chain_ref")
                        if row["issuer_symbol"] == "TSLAx")
            self.assertEqual(tsla["equity_session"], "closed")
            self.assertEqual(tsla["equity_price_kind"], "previous_close")
            self.assertEqual(Decimal(tsla["equity_usd"]), Decimal("100"))
            self.assertEqual(tsla["screen_hit"], 0)
            book = next(row for row in store.latest_observations("kraken_book")
                        if row["pair"] == "TSLAxUSD")
            self.assertEqual(book["quality"], "empty_book")
            text = render(store)
            self.assertIn("无流动性", text)
            equity = next(row for row in store.latest_observations("equity")
                          if row["issuer_symbol"] == "TSLAx")
            self.assertEqual(equity["equity_session"], "closed")
            store.close()


class BoundaryTests(unittest.TestCase):
    def test_the_live_monitor_does_not_import_this_package(self):
        monitor = (ROOT / "src/arb_monitor/apps/hk_monitor.py").read_text()
        registry = (ROOT / "src/arb_monitor/data/xstocks_registry.py").read_text()
        self.assertNotIn("xstocks_book", monitor)
        self.assertNotIn("xstocks_book", registry)
        banned = (
            "arb_monitor.apps", "arb_monitor.data", "arb_monitor.execution",
            "arb_monitor.strategy", "arb_monitor.risk", "arb_monitor.ops",
            "arb_monitor.ledger", "arb_monitor.core",
        )
        for path in (ROOT / "src/arb_monitor/xstocks_book").glob("*.py"):
            text = path.read_text()
            for name in banned:
                self.assertNotIn(name, text, path.name)


if __name__ == "__main__":
    unittest.main()
