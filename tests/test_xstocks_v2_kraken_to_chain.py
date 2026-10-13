"""Family 2 (Kraken -> chain, no bridge): the executor and its gate.

Added 2026-10-07 because Kraken's own rail already lands on Solana for the
symbols we trade there: paying a bridge message fee and waiting 2-27 minutes for
that path is pure cost. The legs are borrowed from family 6 (Kraken buy,
withdraw) and from the reverse script (Jupiter sell).
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from arb_monitor.xstocks_v2 import costs, live
from arb_monitor.xstocks_v2.policy import V2Policy


def _runner():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "v2liverunner", root / "scripts" / "xstocks_v2_live.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _family2():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "v2k2c", root / "scripts" / "xstocks_v2_kraken_to_chain.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RegistryTest(unittest.TestCase):
    def test_the_family_is_executable_now(self):
        self.assertEqual(live.KRAKEN_TO_CHAIN, "kraken_to_chain")
        self.assertIn(live.KRAKEN_TO_CHAIN, live.EXECUTABLE_FAMILIES)


class ExitRequoteTest(unittest.TestCase):
    """The second real quote has to happen BEFORE the Kraken order.

    Operator 2026-10-08: "你不是还有第二轮真实询价吗？真实询价的时候发现池子深度
    不够再拒绝套利啊". For this family there was none -- it bought on Kraken,
    withdrew, and only then quoted the sell, so a fake exit price (measured that
    day: a one-sided pool quoting 758 against a 194 market) was discovered with
    the tokens already off Kraken.
    """

    def setUp(self):
        self.module = _family2()
        self.runner = _runner()
        self.policy = V2Policy.load()

    def test_the_plan_refuses_when_the_fresh_exit_is_below_the_bar(self):
        self.module.fresh_exit_price = lambda **kwargs: D("180.00")
        ok, note = self.module.exit_clears(
            symbol="KLACx", notional=D("25"), ask=D("194.37"),
            policy=self.policy, http=None)
        self.assertFalse(ok)
        self.assertIn("exit_requote_below_bar", note)

    def test_a_price_level_fake_still_clears_the_gross_bar(self):
        """The fresh quote cannot see a *fake price level* -- it is the same fake.

        Measured 2026-10-08: KLACx quoted 758 against a 194 market. A second
        quote returns 758 again, so the depth check passes and the row would be
        bought. That case is caught by ``price_outlier`` in the scan, which is a
        data-quality flag, not a depth check -- the two guards are not
        substitutes and both are needed.
        """
        self.module.fresh_exit_price = lambda **kwargs: D("758.50")
        ok, note = self.module.exit_clears(
            symbol="KLACx", notional=D("25"), ask=D("194.37"),
            policy=self.policy, http=None)
        self.assertTrue(ok, note)

    def test_a_fresh_exit_that_still_pays_clears(self):
        self.module.fresh_exit_price = lambda **kwargs: D("200.00")
        ok, note = self.module.exit_clears(
            symbol="KLACx", notional=D("25"), ask=D("194.37"),
            policy=self.policy, http=None)
        self.assertTrue(ok, note)
        self.assertIn("exit_requote_ok", note)

    def test_a_missing_fresh_quote_is_a_refusal_not_a_pass(self):
        self.module.fresh_exit_price = lambda **kwargs: None
        ok, note = self.module.exit_clears(
            symbol="KLACx", notional=D("25"), ask=D("194.37"),
            policy=self.policy, http=None)
        self.assertFalse(ok)
        self.assertEqual(note, "exit_requote_failed")

    def test_the_trade_uses_the_size_the_row_was_priced_at(self):
        """Quote size and trade size have to be the same size."""
        self.assertEqual(self.runner.row_notional({"notional_usdt": "25"},
                                                 self.policy), D("25"))
        self.assertEqual(self.runner.row_notional({}, self.policy),
                         self.policy.per_trade_quote_usdt)
        self.assertEqual(self.runner.row_notional({"notional_usdt": "0"},
                                                 self.policy),
                         self.policy.per_trade_quote_usdt)

    def test_the_runner_uses_the_forward_and_reverse_legs(self):
        module = _family2()
        self.assertEqual(module.FAMILY, live.KRAKEN_TO_CHAIN)
        for name in ("kraken_buy_runner", "withdraw_step_runner"):
            self.assertTrue(hasattr(module.FWD, name), name)
        for name in ("sell_step_runner", "sell_verifier"):
            self.assertTrue(hasattr(module.REV, name), name)


class GateTest(unittest.TestCase):
    def setUp(self):
        self.policy = V2Policy.load()
        self.runner = _runner()

    def _row(self, **over):
        row = {"family": "kraken_to_chain", "symbol": "SPYx", "chain": "solana",
               "buy_price_usd": "775.73", "sell_price_usd": "779.2",
               "edge_t0_bps": "45", "edge_after_requote_bps": "45",
               "notional_usdt": "200", "withdraw_fee_units": "0.00045901",
               "withdraw_fee_usdt": "0.3575", "blockers": []}
        row.update(over)
        return row

    def _ok(self, row, *, size="40", threshold="20"):
        return self.runner.kraken_to_chain_net_ok(
            row, snapshot={"threshold_bps": threshold}, policy=self.policy,
            available_usdt=D(size))

    def test_the_fee_has_to_be_measured(self):
        """No withdrawal fee means no net at any size, so it refuses."""
        self.assertFalse(self._ok(self._row(withdraw_fee_usdt=None), size="200"))
        self.assertTrue(self._ok(self._row(), size="200"))

    def test_the_net_is_judged_at_the_wallet_size(self):
        # 45 bps on 40 USDT is 0.18, minus the 0.3575 withdraw fee and 20 bps
        # (0.08) -> negative. At 200 it clears.
        self.assertFalse(self._ok(self._row(), size="40"))
        self.assertTrue(self._ok(self._row(), size="200"))

    def test_a_balance_we_cannot_read_is_a_refusal(self):
        self.assertFalse(self._ok(self._row(), size="0"))

    def test_the_gate_matches_the_shared_model(self):
        row = self._row()
        size = D("40") * D("0.999")
        net = costs.net_after_costs(
            gross_usdt=size * D("45") / D(10000), notional_usdt=size,
            fixed_cost_usd=D("0.3575"), policy=self.policy,
            family="kraken_to_chain", gas_cost_usd=D(0))
        expected = costs.net_clears(edge_bps=D("45"), net_usdt=net,
                                    min_bps=D("20"),
                                    min_usdt=self.policy.min_net_profit_usdt)
        self.assertEqual(self._ok(row), expected)

    def test_only_solana_rows_are_candidates(self):
        snapshot = {"threshold_bps": "4", "rows": [
            self._row(chain="solana"),
            self._row(chain="eth"),
            self._row(blockers=["below_depth"]),
            self._row(blockers=["stale_or_skew"]),
            self._row(family="chain_to_kraken"),
        ]}
        rows = self.runner.kraken_to_chain_candidates(snapshot)
        symbols = [(row["chain"], row["family"]) for row in rows]
        self.assertEqual(symbols, [("solana", "kraken_to_chain"),
                                   ("solana", "kraken_to_chain")])


class ScanPriceTest(unittest.TestCase):
    """The plan prices itself from the row the selector ranked."""

    def setUp(self):
        self.module = _family2()
        self.tmp = Path(tempfile.mkdtemp())

    def _write_scan(self, rows):
        path = self.tmp / "xstocks_v2_scan.json"
        path.write_text(json.dumps({"rows": rows}))
        return path

    def test_the_kraken_ask_comes_from_the_family_row(self):
        original = self.module.SCAN
        self.module.SCAN = self._write_scan([
            {"symbol": "SPYx", "family": "kraken_to_chain", "buy_venue": "kraken",
             "buy_price_usd": "775.734534"},
            {"symbol": "SPYx", "family": "chain_to_kraken", "buy_venue": "jupiter",
             "buy_price_usd": "779"},          # a different venue's price
        ])
        try:
            self.assertEqual(self.module.ask_from_scan("SPYx"), D("775.734534"))
        finally:
            self.module.SCAN = original

    def test_a_symbol_without_a_family_row_has_no_price(self):
        original = self.module.SCAN
        self.module.SCAN = self._write_scan([])
        try:
            self.assertIsNone(self.module.ask_from_scan("SPYx"))
        finally:
            self.module.SCAN = original


if __name__ == "__main__":
    unittest.main()
