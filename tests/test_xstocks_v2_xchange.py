"""xChange soft quotes and the scan rows they produce.

No network. The hard RFQ (the one that returns a signature) is not a scan path.
"""
from __future__ import annotations

import json
import re
import unittest
from decimal import Decimal as D
from pathlib import Path

from arb_monitor.xstocks_v2 import scan
from arb_monitor.xstocks_v2.feeds import xchange as xc
from arb_monitor.xstocks_v2.feeds.http import RateLimiter
from arb_monitor.xstocks_v2.policy import V2Policy
from arb_monitor.xstocks_v2.spot_venue import BUY, REAL, SELL, Quote

ROOT = Path(__file__).resolve().parents[1]
SOFT = "/trades/xchange/rfq/soft"
HARD = re.compile(r"/trades/xchange/rfq(?!/soft)")


class RecordingHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, url, *, data=None, headers=None):
        self.calls.append({"url": url, "data": data, "headers": headers})
        status, payload = self.responses.pop(0)
        if isinstance(payload, bytes):
            raw = payload
        else:
            raw = json.dumps(payload).encode()
        return status, raw


def _feed(http, key="k"):
    return xc.XchangeFeed(
        http, api_key=key, base="https://api.backed.fi/api/v2",
        limiter=RateLimiter(1000, sleep=lambda _s: None))


def _asset(**overrides):
    node = {
        "identifier": "AAPLx",
        "isTradingHalted": False,
        "minOrderFiatValue": 1000,
        "maxOrderFiatValue": 5000000,
        "executionTimeoutSeconds": 60,
        "networks": ["Solana", "Ethereum", "Ink", "Arbitrum"],
    }
    node.update(overrides)
    asset, ignored = xc.parse_asset(node)
    return asset, ignored


def _soft(side, cash, qty, *, expires="2099-01-01T00:00:00.000Z", price="1"):
    return {
        "id": f"soft-{side}",
        "network": "Solana",
        "side": side,
        "quantity": qty,
        "cashAmount": cash,
        "price": price,
        "expiresAt": expires,
    }


class CatalogTest(unittest.TestCase):
    def test_cents_become_dollars_and_arbitrum_is_not_quoted(self):
        asset, ignored = _asset()
        self.assertEqual(asset.symbol, "AAPLx")
        self.assertEqual(asset.min_usd, D("10"))
        self.assertEqual(asset.max_usd, D("50000"))
        self.assertEqual(asset.chains, frozenset({"solana", "eth", "ink"}))
        self.assertEqual(ignored, frozenset({"Arbitrum"}))
        self.assertFalse(asset.closed)

    def test_bsc_and_ink_are_rfq_chains(self):
        asset, ignored = _asset(networks=["Ink", "BinanceSmartChain", "Arbitrum"])
        self.assertEqual(asset.chains, frozenset({"ink", "bsc"}))
        self.assertEqual(ignored, frozenset({"Arbitrum"}))
        self.assertEqual(xc.RFQ_INSTEAD_OF_FACTORY, ("ink", "bsc"))
        self.assertFalse(asset.closed)

    def test_a_halted_asset_is_closed_without_a_request(self):
        asset, _ignored = _asset(isTradingHalted=True)
        self.assertTrue(asset.closed)
        self.assertEqual(xc.size_blocker(asset, D("25")), xc.CLOSED)

    def test_a_ticket_under_the_minimum_is_refused_before_the_request(self):
        asset, _ignored = _asset()
        self.assertEqual(xc.size_blocker(asset, D("9.99")), xc.BELOW_MIN)
        self.assertEqual(xc.size_blocker(asset, D("10")), "")
        self.assertEqual(xc.size_blocker(asset, D("50000.01")), xc.ABOVE_MAX)

    def test_no_key_does_not_send(self):
        http = RecordingHttp([])
        feed = _feed(http, key="")
        assets, note = feed.catalog()
        self.assertEqual(assets, {})
        self.assertEqual(note, xc.UNCONFIGURED)
        self.assertEqual(http.calls, [])
        self.assertEqual(feed.calls, 0)

    def test_pages_walk_until_the_catalog_says_stop(self):
        http = RecordingHttp([
            (200, {"nodes": [{"identifier": "AAPLx", "networks": ["Solana"]}],
                   "page": {"hasNextPage": True}}),
            (200, {"nodes": [{"identifier": "TSLAx", "networks": ["Ink"]}],
                   "page": {"hasNextPage": False}}),
        ])
        assets, note = _feed(http).catalog()
        self.assertEqual(note, "ok")
        self.assertEqual(set(assets), {"AAPLx", "TSLAx"})
        self.assertEqual(assets["TSLAx"].chains, frozenset({"ink"}))
        self.assertEqual(len(http.calls), 2)
        self.assertIn("page=1", http.calls[1]["url"])


class SoftQuoteTest(unittest.TestCase):
    def test_buy_and_sell_prices_are_cash_over_quantity(self):
        http = RecordingHttp([
            (200, _soft("Buy", "25", "0.1")),
            (200, _soft("Sell", "24", "0.1")),
        ])
        feed = _feed(http)
        asset, _ignored = _asset(minOrderFiatValue=100)
        entry = feed.pair_entry(asset, "solana", D("25"), recv_ms=1_000, now_ms=1_000)
        self.assertEqual(entry["buy"].price_usd, D("250"))
        self.assertEqual(entry["sell"].price_usd, D("240"))
        self.assertEqual(entry["buy"].venue, "xchange")
        self.assertEqual(entry["buy"].source, "xchange:soft")
        self.assertEqual(entry["pool_id"], "xchange:solana")
        self.assertEqual(entry["xchange_blockers"], [])
        body = json.loads(http.calls[0]["data"])
        self.assertEqual(body["side"], "Buy")
        self.assertEqual(body["network"], "Solana")
        self.assertEqual(body["cashAmount"], "25")
        self.assertNotIn("quantity", body)
        self.assertEqual(http.calls[0]["headers"]["X-API-KEY"], "k")

    def test_a_numeric_price_stays_a_decimal(self):
        raw = (b'{"id":"s","side":"Buy","network":"Solana",'
               b'"price":184.10,"expiresAt":"2099-01-01T00:00:00Z"}')
        quote, reason = xc.quote_from_soft(
            symbol="AAPLx", chain="eth", side="Buy",
            payload=xc._loads(raw), recv_ms=1, now_ms=1)
        self.assertEqual(reason, "")
        self.assertEqual(quote.price_usd, D("184.10"))
        self.assertIsInstance(quote.price_usd, D)

    def test_an_expired_soft_quote_is_not_a_price(self):
        payload = _soft("Buy", "25", "0.1", expires="2020-01-01T00:00:00Z")
        quote, reason = xc.quote_from_soft(
            symbol="AAPLx", chain="solana", side="Buy", payload=payload,
            recv_ms=1, now_ms=1_700_000_000_000)
        self.assertIsNone(quote)
        self.assertEqual(reason, xc.EXPIRED)

    def test_closed_and_undersized_do_not_call_the_soft_endpoint(self):
        http = RecordingHttp([])
        feed = _feed(http)
        halted, _ignored = _asset(isTradingHalted=True)
        closed = feed.pair_entry(halted, "eth", D("25"), recv_ms=1, now_ms=1)
        self.assertEqual(closed["xchange_blockers"], [xc.CLOSED])
        self.assertNotIn("buy", closed)
        small, _ignored = _asset(minOrderFiatValue=5000)
        refused = feed.pair_entry(small, "ink", D("25"), recv_ms=1, now_ms=1)
        self.assertEqual(refused["xchange_blockers"], [xc.BELOW_MIN])
        self.assertEqual(http.calls, [])

    def test_three_closed_answers_stop_the_rest_of_the_round(self):
        http = RecordingHttp([
            (400, {"code": "OUTSIDE_TRADING_HOURS"}),
            (400, {"code": "TRADING_HALTED"}),
            (400, {"code": "MARKET_CLOSED"}),
        ])
        feed = _feed(http)
        asset, _ignored = _asset(minOrderFiatValue=100)
        reasons = []
        for _ in range(4):
            entry = feed.pair_entry(asset, "solana", D("25"), recv_ms=1, now_ms=1)
            reasons.append(entry["xchange_blockers"])
        self.assertEqual(reasons, [[xc.CLOSED]] * 4)
        self.assertEqual(len(http.calls), 3)
        self.assertTrue(feed.session_closed)

    def test_a_chain_the_asset_does_not_list_is_not_an_entry(self):
        asset, _ignored = xc.parse_asset(
            {"identifier": "AAPLx", "networks": ["Ethereum"]})
        entry = _feed(RecordingHttp([])).pair_entry(
            asset, "solana", D("25"), recv_ms=1, now_ms=1)
        self.assertIsNone(entry)


class ScanRowTest(unittest.TestCase):
    def setUp(self):
        self.policy = V2Policy.load()

    def _xchange(self, chain, buy, sell, blockers=()):
        entry = {
            "symbol": "AAPLx", "chain": chain, "pool_id": f"xchange:{chain}",
            "dex": "xchange", "source": "xchange:soft", "errors": list(blockers),
            "xchange_blockers": list(blockers),
        }
        if buy is not None:
            entry["buy"] = Quote(
                venue="xchange", chain=chain, symbol="AAPLx", side=BUY, kind=REAL,
                price_usd=D(buy), recv_ms=1000, pool_id=f"xchange:{chain}")
        if sell is not None:
            entry["sell"] = Quote(
                venue="xchange", chain=chain, symbol="AAPLx", side=SELL, kind=REAL,
                price_usd=D(sell), recv_ms=1000, pool_id=f"xchange:{chain}")
        return entry

    def _rows(self, entries, **kwargs):
        return scan.build_candidates(
            symbol="AAPLx", chain_quotes=entries,
            kraken_row={"bid": D("255"), "ask": D("256"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"), deposits={}, v4_tiers={},
            now_ms=1000, phase="scan", **kwargs)

    def test_a_solana_xchange_buy_is_a_chain_to_kraken_row(self):
        rows = self._rows([self._xchange("solana", "250", "240")])
        row = next(item for item in rows if item["family"] == "chain_to_kraken")
        self.assertEqual(row["buy_venue"], "xchange")
        self.assertEqual(row["sell_venue"], "kraken")
        self.assertEqual(D(row["edge_t0_bps"]), D("200"))
        self.assertNotIn("two_tx_inventory", row["blockers"])

    def test_a_closed_xchange_still_prints_a_row(self):
        rows = self._rows([self._xchange("solana", None, None, [xc.CLOSED])])
        families = {row["family"] for row in rows}
        self.assertIn("chain_to_kraken", families)
        row = next(item for item in rows if item["family"] == "chain_to_kraken")
        self.assertIn(xc.CLOSED, row["blockers"])
        self.assertEqual(row["reason"], xc.CLOSED)
        self.assertIsNone(row["buy_price_usd"])
        self.assertFalse(row["executable"])

    def test_pool_against_xchange_is_its_own_family_and_not_executable(self):
        pool = {
            "symbol": "AAPLx", "chain": "solana", "pool_id": "pool-1",
            "dex": "jupiter", "errors": [], "depth_usd": D("1000000"),
            "buy": Quote(venue="jupiter", chain="solana", symbol="AAPLx",
                         side=BUY, kind=REAL, price_usd=D("100"), recv_ms=1000,
                         pool_id="pool-1"),
            "sell": Quote(venue="jupiter", chain="solana", symbol="AAPLx",
                          side=SELL, kind=REAL, price_usd=D("99"), recv_ms=1000,
                          pool_id="pool-1"),
        }
        rows = self._rows([pool, self._xchange("solana", "101", "102")])
        paired = [row for row in rows if row["family"] == "xchange_vs_pool"]
        self.assertEqual(len(paired), 1)
        self.assertEqual(paired[0]["buy_venue"], "jupiter")
        self.assertEqual(paired[0]["sell_venue"], "xchange")
        self.assertIn("two_tx_inventory", paired[0]["blockers"])
        self.assertFalse(paired[0]["executable"])
        self.assertFalse(any(row["family"] == "dex_to_dex" for row in rows))
        for row in paired:
            self.assertFalse(any(str(key).startswith("xchange:")
                                 for key in row.get("quoted_pools") or []))

    def test_ink_gas_unknown_refuses_the_row_and_names_the_missing_executor(self):
        rows = self._rows(
            [self._xchange("ink", "100", "99")], gas_usdt_ink=None)
        row = next(item for item in rows if item["family"] == "chain_to_kraken")
        self.assertIn("cost_unknown", row["blockers"])
        self.assertIn("no_executor_chain", row["blockers"])
        self.assertIsNone(row["gas_usdt"])
        self.assertFalse(row["executable"])

    def test_ink_gas_is_charged_when_the_read_succeeded(self):
        self.assertEqual(
            scan.row_gas_usdt({"chain": "ink"}, ink_gas_usdt=D("0.01")), D("0.01"))
        self.assertIsNone(scan.row_gas_usdt({"chain": "ink"}, ink_gas_usdt=None))
        self.assertEqual(
            scan.row_gas_usdt({"chain": "solana"}, D("1"), ink_gas_usdt=D("9")), D(0))

    def test_the_later_row_for_the_same_venue_pair_wins(self):
        first = {"symbol": "AAPLx", "family": "chain_to_kraken", "chain": "solana",
                 "buy_venue": "jupiter", "sell_venue": "kraken", "note": "old"}
        second = {**first, "note": "new"}
        other = {**first, "buy_venue": "xchange", "note": "xchange"}
        kept = scan.dedupe_scan_rows([first, other, second])
        self.assertEqual([row["note"] for row in kept], ["xchange", "new"])


class RotationTest(unittest.TestCase):
    def test_hot_names_are_asked_again_and_the_rest_rotate(self):
        online = [f"S{i}x" for i in range(6)]
        hot, picks, queue = xc.plan_extra_symbols(
            online=online, catalog_symbols=online, already={"S0x"},
            hot=["S1x", "S0x"], rails={}, now_ms=1_000, slots=2, hot_max=12)
        self.assertEqual(hot, ["S1x"])
        self.assertEqual(len(picks), 2)
        self.assertNotIn("S0x", picks)
        self.assertNotIn("S1x", picks)
        self.assertEqual(len(queue), 4)

    def test_hot_from_rows_keeps_the_names_above_the_buffer(self):
        rows = [
            {"family": "chain_to_kraken", "buy_venue": "xchange",
             "sell_venue": "kraken", "symbol": "AAPLx", "edge_t0_bps": "30"},
            {"family": "chain_to_kraken", "buy_venue": "xchange",
             "sell_venue": "kraken", "symbol": "TSLAx", "edge_t0_bps": "1"},
            {"family": "chain_to_kraken", "buy_venue": "jupiter",
             "sell_venue": "kraken", "symbol": "QQQx", "edge_t0_bps": "80"},
        ]
        self.assertEqual(xc.hot_from_rows(rows, floor_bps=D("10")), ["AAPLx"])

    def test_a_name_with_no_kraken_listing_and_no_pool_is_not_quoted(self):
        self.assertEqual(
            xc.without_counterparty(
                catalog_symbols=["AAPLx", "ORPHx"],
                kraken_online=["AAPLx"], pool_symbols=[]),
            ["ORPHx"])


class SourceGuardTest(unittest.TestCase):
    def test_the_scan_does_not_name_the_hard_rfq(self):
        scan_src = (ROOT / "src/arb_monitor/xstocks_v2/scan.py").read_text()
        feed_src = (ROOT / "src/arb_monitor/xstocks_v2/feeds/xchange.py").read_text()
        self.assertIsNone(HARD.search(scan_src))
        self.assertIsNone(HARD.search(feed_src))
        self.assertIn(SOFT, feed_src)

    def test_the_board_names_the_new_reasons(self):
        import importlib.util
        path = ROOT / "scripts/xstocks_v2_board.py"
        spec = importlib.util.spec_from_file_location("v2board_xchange", path)
        board = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(board)
        for name in (xc.CLOSED, xc.UNCONFIGURED, xc.BELOW_MIN, "two_tx_inventory"):
            self.assertIn(name, board.REASON_LABEL)
        self.assertIn("xchange_vs_pool", board.FAMILY_LABEL)
