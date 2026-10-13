"""Hermetic tests for the multi-hop routes and the TON feed.

Kraken only lists a few chains for xStocks, so a deep X Layer pool is reachable
only through a bridge. These tests pin the two-hop catalogue and the reason it
stays non-executable.
"""
from __future__ import annotations

import json
import unittest
from decimal import Decimal as D

from arb_monitor.xstocks_v2 import scan
from arb_monitor.xstocks_v2.feeds import census as census_feed
from arb_monitor.xstocks_v2.feeds import ton as ton_feed
from arb_monitor.xstocks_v2.policy import V2Policy
from arb_monitor.xstocks_v2.spot_venue import BUY, REAL, SELL, Quote


class TonFeedTest(unittest.TestCase):
    def test_ask_units_parsing(self):
        self.assertEqual(ton_feed.parse_units({"ask_units": "123"}), 123)
        self.assertIsNone(ton_feed.parse_units({"ask_units": "0"}))
        self.assertIsNone(ton_feed.parse_units({"ask_units": ""}))
        self.assertIsNone(ton_feed.parse_units({"other": 1}))
        self.assertIsNone(ton_feed.parse_units(None))

    def test_the_url_carries_both_sides(self):
        buy = ton_feed.simulate_url("USDTaddr", "SPYaddr", 25_000_000)
        self.assertIn("offer_address=USDTaddr", buy)
        self.assertIn("ask_address=SPYaddr", buy)
        self.assertIn("slippage_tolerance=0.01", buy)
        sell = ton_feed.simulate_url("SPYaddr", "USDTaddr", 1)
        self.assertIn("offer_address=SPYaddr", sell)

    def test_a_transport_failure_is_not_a_price(self):
        class Boom:
            def request(self, *_args, **_kwargs):
                raise OSError("no route to host")

        feed = ton_feed.TonFeed(Boom())
        self.assertIsNone(feed.quote("a", "b", 100))
        self.assertEqual(feed.failures.get("transport:OSError"), 1)

    def test_ton_pools_survive_the_evm_address_filter(self):
        census = {"candidates": [
            {"slug": "ton", "symbol": "SPYx", "token": "EQjetton", "stable": "EQusdt",
             "pool_address": "EQpool", "dec_token": 8, "dec_stable": 6,
             "fee": "0.002", "depth_usd": "107918", "venue": "stonfi-v2"},
            {"slug": "ton", "symbol": "NOPEx", "token": "0x" + "1" * 40,
             "stable": "EQusdt", "depth_usd": "5"},
        ]}
        rows = census_feed.ton_pool_rows(census)
        self.assertEqual([row["symbol"] for row in rows], ["SPYx"])
        self.assertEqual(rows[0]["token_decimals"], 8)
        # And the EVM reader still ignores it.
        self.assertEqual(census_feed.census_pools(census), [])


class BridgeHopTest(unittest.TestCase):
    def setUp(self):
        self.policy = V2Policy.load()

    def _quote(self, side, price, *, chain, venue="uniswap", pool_id="p"):
        return Quote(venue=venue, chain=chain, symbol="SPYx", side=side, kind=REAL,
                     price_usd=D(price), recv_ms=1000, pool_id=pool_id)

    def _entries(self):
        """SPYx priced on X Layer (deep) and Solana (where Kraken has a rail)."""
        return [
            {"symbol": "SPYx", "chain": "x-layer", "pool_id": "xl", "errors": [],
             "depth_usd": D("1893245"),
             "buy": self._quote(BUY, "779.70", chain="x-layer", venue="uniswap-v3-x-layer"),
             "sell": self._quote(SELL, "783.63", chain="x-layer", venue="uniswap-v3-x-layer")},
            {"symbol": "SPYx", "chain": "solana", "pool_id": "sol", "errors": [],
             "depth_usd": D("100000"),
             "buy": self._quote(BUY, "785.00", chain="solana", venue="jupiter"),
             "sell": self._quote(SELL, "784.50", chain="solana", venue="jupiter")},
        ]

    def _build(self, **kwargs):
        return scan.build_candidates(
            symbol="SPYx", chain_quotes=self._entries(),
            kraken_row={"bid": D("779.78"), "ask": D("779.70"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"),
            deposits={"SPYx": [{"method": "SPYx", "minimum": "0.0005572"}]},
            v4_tiers={}, now_ms=1000, phase="paper_kraken", **kwargs)

    def test_buy_on_x_layer_bridge_to_solana_then_kraken_is_a_row(self):
        """X Layer has no Kraken rail at all, so the position has to cross."""
        rows = self._build(
            withdraw_methods={"SPYx": [{"method": "SPYx", "network": "Solana"}]})
        hops = [row for row in rows if row["family"] == "chain_via_bridge_to_kraken"
                and row["chain"] == "x-layer->solana"]
        self.assertEqual(len(hops), 1)
        row = hops[0]
        self.assertEqual(row["ccip_dialect"], "evm_to_solana")
        self.assertIn("unmeasured_route", row["blockers"])
        self.assertIn("cost_unknown", row["blockers"])
        self.assertFalse(row["executable"])
        # buy at the X Layer price, sell into the Kraken bid
        self.assertEqual(D(row["buy_price_usd"]), D("779.70"))
        self.assertEqual(D(row["sell_price_usd"]), D("779.78"))

    def test_a_measured_bridge_still_has_no_known_fee(self):
        rows = self._build(
            withdraw_methods={"SPYx": [{"method": "SPYx", "network": "Solana"}]},
            measured_bridges=frozenset({("x-layer", "solana")}))
        row = [item for item in rows
               if item["family"] == "chain_via_bridge_to_kraken"][0]
        self.assertNotIn("unmeasured_route", row["blockers"])
        self.assertIn("cost_unknown", row["blockers"])
        self.assertFalse(row["executable"])

    def test_kraken_to_hub_to_x_layer_is_the_reverse_hop(self):
        """The operator's case: withdraw on Solana, bridge to X Layer, sell there."""
        rows = self._build(
            withdraw_methods={"SPYx": [{"method": "SPYx", "network": "Solana"}]},
            address_book=[{"asset": "SPYx", "method": "SPYx"}])
        hops = [row for row in rows if row["family"] == "kraken_via_bridge_to_chain"
                and row["chain"] == "solana->x-layer"]
        self.assertEqual(len(hops), 1)
        row = hops[0]
        self.assertEqual(row["ccip_dialect"], "solana_source")
        self.assertIn("unmeasured_route", row["blockers"])
        self.assertNotIn("no_address_book", row["blockers"])
        # buy at the Kraken ask, sell into the X Layer pool
        self.assertEqual(D(row["buy_price_usd"]), D("779.70"))
        self.assertEqual(D(row["sell_price_usd"]), D("783.63"))
        self.assertGreater(D(row["edge_t0_bps"]), D(0))

    def test_the_reverse_hop_needs_the_address_even_with_the_rail(self):
        rows = self._build(
            withdraw_methods={"SPYx": [{"method": "SPYx", "network": "Solana"}]},
            address_book=[])
        row = [item for item in rows
               if item["family"] == "kraken_via_bridge_to_chain"][0]
        self.assertIn("no_address_book", row["blockers"])

    def test_a_chain_that_is_not_a_hub_produces_no_hop(self):
        """Kraken cannot receive SPYx from X Layer, so no X Layer hub hop."""
        rows = self._build(
            withdraw_methods={"SPYx": [{"method": "SPYx", "network": "Solana"}]})
        hubs = {row["dest_chain"] for row in rows
                if row["family"] == "chain_via_bridge_to_kraken"}
        self.assertNotIn("x-layer", hubs)

    def test_the_same_pool_is_never_both_sides_of_a_hop(self):
        rows = self._build(
            withdraw_methods={"SPYx": [{"method": "SPYx", "network": "Solana"}]})
        for row in rows:
            if row["family"].endswith("via_bridge_to_kraken"):
                self.assertNotIn(row["sell_venue"], (None, ""))


class WithdrawFeeTest(unittest.TestCase):
    """The withdrawal fee is what decides the Kraken -> chain direction."""

    def setUp(self):
        self.policy = V2Policy.load()

    def test_the_nested_fee_shape_is_read(self):
        rail = {"method": "STRCx", "network": "Solana", "minimum": "0.00436779",
                "fee": {"aclass": "tokenized_asset", "asset": "STRCx",
                        "fee": "0.00291186"}}
        self.assertEqual(scan.rail_fee_units(rail), D("0.00291186"))
        # DepositMethods uses a bare number; both shapes must work.
        self.assertEqual(scan.rail_fee_units({"fee": "0.5"}), D("0.5"))
        self.assertIsNone(scan.rail_fee_units(None))
        self.assertIsNone(scan.rail_fee_units({"fee": {}}))

    def test_the_rail_lookup_finds_the_solana_row(self):
        methods = [{"method": "STRCx - Ethereum", "network": "Ethereum"},
                   {"method": "STRCx", "network": "Solana"}]
        self.assertEqual(scan.withdraw_rail("STRCx", "solana", methods)["network"],
                         "Solana")
        self.assertEqual(scan.withdraw_rail("STRCx", "eth", methods)["network"],
                         "Ethereum")

    def test_a_fixed_withdrawal_fee_kills_a_small_ticket(self):
        """+37 bps of gross on 25 USDT against a 0.29 USDT fee is a loss."""
        net, reason = scan.kraken_to_chain_net(
            notional=D("25"), buy_price=D("99.74"), sell_price=D("100.1093"),
            fee_units=D("0.00291186"), policy=self.policy)
        self.assertEqual(reason, "")
        self.assertLess(net, D("0"))

    def test_the_same_spread_pays_at_a_bigger_ticket(self):
        """The fee is fixed, so it amortises: this is why size matters here."""
        small, _ = scan.kraken_to_chain_net(
            notional=D("25"), buy_price=D("99.74"), sell_price=D("100.1093"),
            fee_units=D("0.00291186"), policy=self.policy)
        big, _ = scan.kraken_to_chain_net(
            notional=D("400"), buy_price=D("99.74"), sell_price=D("100.1093"),
            fee_units=D("0.00291186"), policy=self.policy)
        self.assertLess(small, D("0"))
        self.assertGreater(big, D("0"))

    def test_an_unknown_fee_is_not_zero(self):
        net, reason = scan.kraken_to_chain_net(
            notional=D("25"), buy_price=D("99.74"), sell_price=D("100.10"),
            fee_units=None, policy=self.policy)
        self.assertIsNone(net)
        self.assertEqual(reason, "withdraw_fee_unknown")


if __name__ == "__main__":
    unittest.main()
