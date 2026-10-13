"""Hermetic tests for the measured bridge fee table and its scan gate.

No network: the fee and price files are written into a temporary directory, and
the scan gate is driven with synthetic quotes.
"""
from __future__ import annotations

import json
import importlib.util
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from arb_monitor.xstocks_v2 import bridge_fees as BF
from arb_monitor.xstocks_v2 import scan
from arb_monitor.xstocks_v2.policy import V2Policy
from arb_monitor.xstocks_v2.spot_venue import BUY, REAL, SELL, Quote

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data-v2"
NOW = 1_791_371_170_000

#: ``data-v2/`` is runtime state: a fresh clone and a CI runner have neither the
#: measured fee table nor the route ledger. The guards below check that *this*
#: machine still holds the evidence its live decisions lean on, so they say
#: "needs the live ledger" instead of failing where there is no ledger.
LIVE_FEES = DATA / BF.FEES_NAME
LIVE_ROUTE_SAMPLES = DATA / "xstocks_v2_route_samples.jsonl"


def fees_doc(edges: dict) -> dict:
    return {"as_of": "test", "edges": edges}


def prices_doc(prices: dict) -> dict:
    return {"as_of_ms": NOW, "prices": prices}


class BridgeFeeTableTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.fees_path = self.dir / BF.FEES_NAME
        self.prices_path = self.dir / BF.PRICES_NAME

    def _write(self, edges, prices):
        self.fees_path.write_text(json.dumps(fees_doc(edges)))
        self.prices_path.write_text(json.dumps(prices_doc(prices)))

    def test_a_measured_fee_with_a_live_price_becomes_usd(self):
        self._write(
            {"x-layer->solana": {"fee_asset": "OKB",
                                 "fee_amount": "0.000791242725185203",
                                 "samples": 2, "evidence": ["0xaa"],
                                 "measured_ms": NOW}},
            {"OKB": {"usd": "133.03", "source": "test", "as_of_ms": NOW - 1000}})
        table, unknown = BF.load_table(data_dir=self.dir, now_ms=NOW)
        self.assertEqual(unknown, {})
        self.assertAlmostEqual(float(table[("x-layer", "solana")]), 0.105259, places=5)

    def test_a_fee_with_no_gas_price_is_still_unknown(self):
        """A fee in OKB is not a USD cost until somebody quotes OKB."""
        self._write(
            {"x-layer->solana": {"fee_asset": "OKB",
                                 "fee_amount": "0.000791242725185203",
                                 "measured_ms": NOW}},
            {})
        table, unknown = BF.load_table(data_dir=self.dir, now_ms=NOW)
        self.assertEqual(table, {})
        self.assertEqual(unknown, {"x-layer->solana": BF.GAS_PRICE_UNKNOWN})

    def test_a_stale_gas_price_is_not_a_price(self):
        self._write(
            {"x-layer->solana": {"fee_asset": "OKB", "fee_amount": "0.0007",
                                 "measured_ms": NOW}},
            {"OKB": {"usd": "133.03", "source": "test",
                     "as_of_ms": NOW - BF.MAX_PRICE_AGE_MS - 1}})
        table, unknown = BF.load_table(data_dir=self.dir, now_ms=NOW)
        self.assertEqual(table, {})
        self.assertEqual(unknown, {"x-layer->solana": BF.GAS_PRICE_STALE})

    def test_an_edge_with_no_measurement_is_refused_not_assumed(self):
        self._write({}, {"OKB": {"usd": "133.03", "source": "test",
                                 "as_of_ms": NOW}})
        amount, reason = BF.fee_usd(
            ("solana", "mantle"), fees=BF.load_fees(self.fees_path),
            prices=BF.load_prices(self.prices_path), now_ms=NOW)
        self.assertIsNone(amount)
        self.assertEqual(reason, BF.FEE_UNMEASURED)

    def test_a_row_without_an_amount_or_asset_is_dropped(self):
        self._write({"x-layer->solana": {"fee_asset": "OKB"},
                     "solana->x-layer": {"fee_amount": "0.0008"}},
                    {"OKB": {"usd": "1", "source": "t", "as_of_ms": NOW}})
        self.assertEqual(BF.load_fees(self.fees_path), {})

    def test_a_price_without_a_timestamp_is_dropped(self):
        self.prices_path.write_text(json.dumps({"prices": {"OKB": {"usd": "133.03"}}}))
        self.assertEqual(BF.load_prices(self.prices_path), {})

    @unittest.skipUnless(LIVE_FEES.exists(), "needs this machine's data-v2 fee table")
    def test_the_shipped_table_prices_both_measured_directions(self):
        """The two edges we have real receipts for must be priced in the repo."""
        fees = BF.load_fees(DATA / BF.FEES_NAME)
        prices = BF.load_prices(DATA / BF.PRICES_NAME)
        table = BF.fee_table(fees=fees, prices=prices, now_ms=NOW)
        self.assertIn(("x-layer", "solana"), table)
        self.assertIn(("solana", "x-layer"), table)
        for edge in (("x-layer", "solana"), ("solana", "x-layer")):
            self.assertGreater(table[edge], D("0.05"))
            self.assertLess(table[edge], D("0.20"))
        self.assertEqual(fees[("x-layer", "solana")]["fee_asset"], "OKB")
        self.assertEqual(fees[("solana", "x-layer")]["fee_asset"], "SOL")
        self.assertEqual(fees[("solana", "x-layer")]["fee_amount"], D("0.000852448"))


class BridgeCostGateTest(unittest.TestCase):
    """``cost_unknown`` disappears only when the fee is really priced."""

    def setUp(self):
        self.policy = V2Policy.load()

    def _entries(self):
        def quote(side, price, chain, pool):
            return Quote(venue="uniswap-v3", chain=chain, symbol="SPYx", side=side,
                         kind=REAL, price_usd=D(price), recv_ms=1000, pool_id=pool)
        return [
            {"symbol": "SPYx", "chain": "x-layer", "errors": [], "pool_id": "0xA",
             "buy": quote(BUY, "778", "x-layer", "0xA"), "sell": None},
            {"symbol": "SPYx", "chain": "solana", "errors": [], "pool_id": "poolB",
             "buy": None, "sell": quote(SELL, "800", "solana", "poolB")},
        ]

    def _rows(self, **kwargs):
        return scan.build_candidates(
            symbol="SPYx", chain_quotes=self._entries(),
            kraken_row={"bid": D("790"), "ask": D("792"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"), deposits={}, v4_tiers={},
            now_ms=1000, phase="scan", **kwargs)

    def _chain_to_chain(self, rows):
        return next(item for item in rows
                    if item["family"] == "chain_to_chain"
                    and item.get("source_chain") == "x-layer")

    def test_a_priced_bridge_is_not_cost_unknown_any_more(self):
        rows = self._rows(measured_bridges=frozenset({("x-layer", "solana")}),
                          bridge_fees={("x-layer", "solana"): D("0.105259")})
        row = self._chain_to_chain(rows)
        self.assertNotIn("cost_unknown", row["blockers"])
        self.assertEqual(row["bridge_fee_usd"], "0.105259")

    def test_a_negative_net_is_marked_and_not_executable(self):
        """The board used to call a -64 bps bridged row "executable"."""
        def quote(side, price, chain, pool):
            return Quote(venue="uniswap-v3", chain=chain, symbol="SPYx", side=side,
                         kind=REAL, price_usd=D(price), recv_ms=1000, pool_id=pool)
        entries = [
            {"symbol": "SPYx", "chain": "x-layer", "errors": [], "pool_id": "0xA",
             "buy": quote(BUY, "778", "x-layer", "0xA"), "sell": None},
            {"symbol": "SPYx", "chain": "solana", "errors": [], "pool_id": "poolB",
             "buy": None, "sell": quote(SELL, "778.6", "solana", "poolB")},
        ]
        rows = scan.build_candidates(
            symbol="SPYx", chain_quotes=entries,
            kraken_row={"bid": D("790"), "ask": D("792"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"), deposits={}, v4_tiers={},
            now_ms=1000, phase="scan",
            measured_bridges=frozenset({("x-layer", "solana")}),
            bridge_fees={("x-layer", "solana"): D("0.105259")})
        row = self._chain_to_chain(rows)
        # 7.7 bps over 200 USDT = 0.154 gross, minus a 0.105 fixed fee and the
        # 0.20 slippage buffer: below the 0.10 floor.
        self.assertLess(D(row["net_usdt"]), self.policy.min_net_profit_usdt)
        self.assertIn("below_net", row["blockers"])
        self.assertFalse(row["executable"])

    def test_a_clearing_net_stays_executable(self):
        def quote(side, price, chain, pool):
            return Quote(venue="uniswap-v3", chain=chain, symbol="SPYx", side=side,
                         kind=REAL, price_usd=D(price), recv_ms=1000, pool_id=pool)
        entries = [
            {"symbol": "SPYx", "chain": "x-layer", "errors": [], "pool_id": "0xA",
             "buy": quote(BUY, "700", "x-layer", "0xA"), "sell": None},
            {"symbol": "SPYx", "chain": "solana", "errors": [], "pool_id": "poolB",
             "buy": None, "sell": quote(SELL, "800", "solana", "poolB")},
        ]
        rows = scan.build_candidates(
            symbol="SPYx", chain_quotes=entries,
            kraken_row={"bid": D("790"), "ask": D("792"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"), deposits={}, v4_tiers={},
            now_ms=1000, phase="scan",
            measured_bridges=frozenset({("x-layer", "solana")}),
            bridge_fees={("x-layer", "solana"): D("0.105259")})
        row = self._chain_to_chain(rows)
        self.assertGreater(D(row["net_usdt"]), self.policy.min_net_profit_usdt)
        self.assertNotIn("below_net", row["blockers"])

    def test_an_unpriced_bridge_keeps_cost_unknown(self):
        rows = self._rows(measured_bridges=frozenset({("x-layer", "solana")}))
        row = self._chain_to_chain(rows)
        self.assertIn("cost_unknown", row["blockers"])
        self.assertIsNone(row["bridge_fee_usd"])

    def test_a_chain_to_chain_row_is_not_judged_by_kraken_rails(self):
        """X Layer has no Kraken rail for SPYx, and that is irrelevant here.

        The chain -> chain row never touches Kraken, so the deposit-rail check
        must not run: it put ``bridge_unmeasured_symbol`` on every reverse-family
        row and would keep a profitable one non-executable forever.
        """
        rows = self._rows(measured_bridges=frozenset({("x-layer", "solana")}),
                          bridge_fees={("x-layer", "solana"): D("0.105259")})
        row = self._chain_to_chain(rows)
        self.assertNotIn("bridge_unmeasured_symbol", row["blockers"])
        self.assertNotIn("unmeasured_route", row["blockers"])
        # ``phase=scan`` is the only thing left, and it is informational.
        self.assertEqual(row["blockers"], ["phase_scan"])

    def test_the_kraken_families_still_check_the_rail(self):
        """The same helper must keep refusing a Kraken path with no rail."""
        entries = [{"symbol": "SPYx", "chain": "x-layer", "errors": [], "pool_id": "0xA",
                    "buy": Quote(venue="uniswap-v3", chain="x-layer", symbol="SPYx",
                                 side=BUY, kind=REAL, price_usd=D("700"),
                                 recv_ms=1000, pool_id="0xA"), "sell": None}]
        rows = scan.build_candidates(
            symbol="SPYx", chain_quotes=entries,
            kraken_row={"bid": D("790"), "ask": D("792"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"), deposits={}, v4_tiers={},
            now_ms=1000, phase="scan",
            measured_bridges=frozenset({("x-layer", "solana")}))
        row = next(item for item in rows if item["family"] == "chain_to_kraken")
        self.assertIn("bridge_unmeasured_symbol", row["blockers"])

    def test_the_hub_family_uses_the_same_gate(self):
        """A Kraken-hub row is refused for the same reason and freed the same way."""
        def quote(side, price, chain, pool):
            return Quote(venue="uniswap-v3", chain=chain, symbol="SPYx", side=side,
                         kind=REAL, price_usd=D(price), recv_ms=1000, pool_id=pool)
        entries = [
            {"symbol": "SPYx", "chain": "x-layer", "errors": [], "pool_id": "0xA",
             "buy": None, "sell": quote(SELL, "800", "x-layer", "0xA")},
            {"symbol": "SPYx", "chain": "solana", "errors": [], "pool_id": "poolB",
             "buy": None, "sell": quote(SELL, "799", "solana", "poolB")},
        ]
        base = dict(symbol="SPYx", chain_quotes=entries,
                    kraken_row={"bid": D("790"), "ask": D("792"), "recv_ms": 1000},
                    policy=self.policy, multiplier=D("1"), deposits={}, v4_tiers={},
                    now_ms=1000, phase="scan",
                    withdraw_methods={"SPYx": [{"method": "SPYx", "network": "Solana"}]},
                    address_book=[{"asset": "SPYx", "key": "solanspyx1",
                                   "method": "SPYx"}])
        without = scan.build_candidates(
            **base, measured_bridges=frozenset({("solana", "x-layer")}))
        hub_rows = [row for row in without
                    if row["family"] == "kraken_via_bridge_to_chain"]
        self.assertTrue(hub_rows)
        self.assertIn("cost_unknown", hub_rows[0]["blockers"])
        with_fee = scan.build_candidates(
            **base, measured_bridges=frozenset({("solana", "x-layer")}),
            bridge_fees={("solana", "x-layer"): D("0.100306")})
        hub_rows = [row for row in with_fee
                    if row["family"] == "kraken_via_bridge_to_chain"]
        self.assertNotIn("cost_unknown", hub_rows[0]["blockers"])
        self.assertEqual(hub_rows[0]["bridge_fee_usd"], "0.100306")


class ReverseWatchTest(unittest.TestCase):
    """The reverse family needs its Solana side quoted, or it cannot be seen."""

    def _pools(self):
        return [{"symbol": "SPYx", "chain": "x-layer"},
                {"symbol": "TSLAx", "chain": "x-layer"},
                {"symbol": "MSTRx", "chain": "bsc"},
                {"symbol": "SPYx", "chain": "ton"}]

    def test_only_chains_with_a_measured_bridge_to_solana_are_watched(self):
        picked = scan.reverse_watch_symbols(
            pools=self._pools(), solana={"SPYx": {}, "TSLAx": {}, "MSTRx": {}},
            measured_bridges=frozenset({("x-layer", "solana")}),
            picked=set(), limit=10)
        # bsc has no measured bridge to Solana here, and ton is not on the bridge.
        self.assertEqual(picked, ["SPYx", "TSLAx"])

    def test_a_symbol_without_a_solana_side_is_skipped(self):
        picked = scan.reverse_watch_symbols(
            pools=self._pools(), solana={"SPYx": {}},
            measured_bridges=frozenset({("x-layer", "solana")}),
            picked=set(), limit=10)
        self.assertEqual(picked, ["SPYx"])

    def test_a_symbol_already_in_stage_two_is_not_quoted_twice(self):
        picked = scan.reverse_watch_symbols(
            pools=self._pools(), solana={"SPYx": {}, "TSLAx": {}},
            measured_bridges=frozenset({("x-layer", "solana")}),
            picked={"SPYx"}, limit=10)
        self.assertEqual(picked, ["TSLAx"])

    def test_the_watch_list_is_bounded_by_the_stage_two_budget(self):
        picked = scan.reverse_watch_symbols(
            pools=self._pools(), solana={"SPYx": {}, "TSLAx": {}},
            measured_bridges=frozenset({("x-layer", "solana")}),
            picked=set(), limit=1)
        self.assertEqual(picked, ["SPYx"])

    def test_no_measured_bridge_means_no_extra_quotes(self):
        self.assertEqual(scan.reverse_watch_symbols(
            pools=self._pools(), solana={"SPYx": {}},
            measured_bridges=frozenset(), picked=set(), limit=10), [])

    @unittest.skipUnless(LIVE_ROUTE_SAMPLES.exists(),
                         "needs this machine's data-v2 route ledger")
    def test_the_repo_really_has_the_x_layer_to_solana_sample(self):
        """The watch list is only useful while that arrival sample exists."""
        self.assertIn(("x-layer", "solana"), scan.load_measured_bridges(
            Path(scan.ROOT) / "data-v2" / "xstocks_v2_route_samples.jsonl"))


def _fee_script():
    """The refresh script, loaded the way it is run (it is a script)."""
    spec = importlib.util.spec_from_file_location(
        "v2bridgefees", ROOT / "scripts" / "xstocks_v2_bridge_fees.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OkbPriceFallbackTest(unittest.TestCase):
    """The quote API is balance-gated, so OKB is priced from either side.

    Measured 2026-10-07: a swap left 0.0003 WOKB in the wallet; the wallet
    service then refused every ``WOKB -> USDG`` quote (``no_route``), the OKB
    price went past its limit, and both x-layer bridge edges fell to
    ``gas_price_stale``. The reverse quote needs USDG instead, and it is the
    same pool. The rigour must not depend on which side answered.
    """

    def setUp(self):
        self.mod = _fee_script()
        self.rows: dict[tuple, dict] = {}
        self.calls: list[tuple] = []

        def fake_quote(*, chain, src, dst, amount):
            self.calls.append((src, dst, amount))
            return self.rows.get((src, dst, amount), {})

        self.mod.quote = fake_quote
        self.usdg = self.mod.USDG_XLAYER
        self.wokb = self.mod.WOKB_XLAYER

    def _row(self, out_raw, in_raw, *, unit_price=None):
        row = {"toTokenAmount": str(out_raw), "fromTokenAmount": str(in_raw)}
        if unit_price is not None:
            row["toToken"] = {"tokenSymbol": "WOKB",
                              "tokenUnitPrice": str(unit_price)}
        return row

    def test_the_direct_side_is_used_when_the_wallet_can_pay_it(self):
        self.rows[(self.wokb, self.usdg, 10 ** 16)] = {
            "toTokenAmount": "1329548", "fromTokenAmount": str(10 ** 16)}
        self.rows[(self.wokb, self.usdg, 10 ** 18)] = {
            "toTokenAmount": "132947227", "fromTokenAmount": str(10 ** 18)}
        value, reason = self.mod.price_okb()
        self.assertEqual(reason, "ok")
        self.assertAlmostEqual(float(value), 132.95, places=2)

    def test_the_reverse_side_prices_okb_when_the_direct_side_cannot(self):
        self.rows[(self.usdg, self.wokb, 10 ** 6)] = self._row(
            7658618352412585, 10 ** 6, unit_price="130.47")
        self.rows[(self.usdg, self.wokb, 10 ** 7)] = self._row(
            76585259687134230, 10 ** 7, unit_price="130.47")
        value, reason = self.mod.price_okb()
        self.assertEqual(reason, "ok(reverse:no_route)")
        self.assertAlmostEqual(float(value), 130.57, places=2)

    def test_the_reverse_side_still_refuses_a_size_disagreement(self):
        self.rows[(self.usdg, self.wokb, 10 ** 6)] = self._row(
            7658618352412585, 10 ** 6, unit_price="130.47")
        # 20% off the first size: a thin pool, not a price.
        self.rows[(self.usdg, self.wokb, 10 ** 7)] = self._row(
            61268142800000000, 10 ** 7, unit_price="130.47")
        self.assertIsNone(self.mod.price_okb()[0])

    def test_a_label_that_contradicts_the_quote_is_not_a_price(self):
        self.rows[(self.usdg, self.wokb, 10 ** 6)] = self._row(
            7658618352412585, 10 ** 6, unit_price="200.0")
        self.rows[(self.usdg, self.wokb, 10 ** 7)] = self._row(
            76585259687134230, 10 ** 7, unit_price="200.0")
        self.assertIsNone(self.mod.price_okb()[0])

    def test_both_sides_failing_stays_unknown(self):
        self.assertIsNone(self.mod.price_okb()[0])
        # Both directions were actually tried, not just one.
        self.assertIn((self.usdg, self.wokb, 10 ** 6), self.calls)


if __name__ == "__main__":
    unittest.main()
