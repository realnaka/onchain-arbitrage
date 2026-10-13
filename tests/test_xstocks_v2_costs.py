"""One net per row: the scan, the board and the live gate must agree.

Measured 2026-10-07: the scan charged the Kraken taker fee only for paths that
trade on Kraken, the live gate charged it for every bridged family, and the two
nets for the same ``chain_to_chain`` row were 10 bps (0.20 USDT at a 200 ticket)
apart. The board printed one and the gate judged the other, so the gate could
refuse a trade the cost model called profitable.
"""
from __future__ import annotations

import importlib.util
import unittest
from decimal import Decimal as D
from pathlib import Path

from arb_monitor.xstocks_v2 import costs, paper, scan
from arb_monitor.xstocks_v2.policy import V2Policy


def _live_runner():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "v2liverunner", root / "scripts" / "xstocks_v2_live.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


KRAKEN_FAMILIES = ("chain_to_kraken", "kraken_to_chain",
                   "chain_via_bridge_to_kraken", "kraken_via_bridge_to_chain")
# ``chain_to_chain`` is the ledger's name for the bridged directions; the two
# executor names are the same walks under their own family strings, and both are
# handed to this model by the live selectors.
NO_KRAKEN_FAMILIES = ("chain_to_chain", "dex_to_dex",
                      "xlayer_via_bridge_to_solana",
                      "solana_via_bridge_to_xlayer")


class GasLineTest(unittest.TestCase):
    """Gas is the term a quoted price does *not* contain."""

    def setUp(self):
        self.policy = V2Policy.load()

    def test_an_evm_action_needs_both_inputs(self):
        gas, why = costs.gas_cost_usd(self.policy, chain="x-layer",
                                      gas_price_wei=D("20000000000"),
                                      native_usd=D("131"))
        self.assertEqual(why, "estimated")
        expected = (D(self.policy.evm_gas_units) * D("20000000000")
                    / D(10) ** 18 * D("131"))
        self.assertEqual(gas, expected)
        base = {"gas_price_wei": D("1"), "native_usd": D("1")}
        for missing in ("gas_price_wei", "native_usd"):
            value, why = costs.gas_cost_usd(
                self.policy, chain="x-layer", **{**base, missing: None})
            self.assertIsNone(value)
            self.assertEqual(why, "gas_unknown")

    def test_solana_has_no_gas_line_its_fee_is_measured_elsewhere(self):
        value, why = costs.gas_cost_usd(self.policy, chain="solana")
        self.assertEqual(value, D(0))
        self.assertEqual(why, "not_applicable")
        # Same function the paper model uses: one implementation.
        from arb_monitor.xstocks_v2 import paper
        self.assertEqual(paper.gas_cost_usdt(
            chain="x-layer", gas_price_wei=D("2"), native_usd=D("3"),
            policy=self.policy), costs.gas_cost_usd(
                self.policy, chain="x-layer", gas_price_wei=D("2"),
                native_usd=D("3")))

    def test_the_net_carries_the_gas_line(self):
        kwargs = {"gross_usdt": D("1"), "notional_usdt": D("200"),
                  "fixed_cost_usd": D("0.1"), "policy": self.policy,
                  "family": "chain_to_chain"}
        without = costs.net_after_costs(**kwargs)
        with_gas = costs.net_after_costs(**kwargs, gas_cost_usd=D("0.0012"))
        self.assertEqual(without - with_gas, D("0.0012"))
        # Unknown gas refuses: it must not be mistaken for a free action.
        self.assertIsNone(costs.net_after_costs(**kwargs, gas_cost_usd=None))

    def test_only_a_row_touching_a_priced_evm_chain_pays_that_line(self):
        """X Layer and Ethereum are priced; Solana's fee is a transfer line.

        Ethereum was added 2026-10-09 with the chain -> Kraken executor: before
        that an eth row's gas was 0 and the board's net was ~0.12 USDT too high
        at a 25 USDT ticket (441,831 gas at 0.11 gwei).
        """
        xlayer_row = {"family": "chain_to_chain", "chain": "solana->x-layer",
                      "source_chain": "solana", "dest_chain": "x-layer"}
        solana_row = {"family": "chain_to_chain", "chain": "solana->solana",
                      "source_chain": "solana", "dest_chain": "solana"}
        eth_row = {"family": "chain_to_kraken", "chain": "eth"}
        both_row = {"family": "chain_to_chain", "chain": "eth->x-layer",
                    "source_chain": "eth", "dest_chain": "x-layer"}
        self.assertEqual(scan.row_gas_usdt(xlayer_row, D("0.0012")), D("0.0012"))
        self.assertEqual(scan.row_gas_usdt(solana_row, D("0.0012")), D(0))
        self.assertEqual(scan.row_gas_usdt(eth_row, D("0.0012"), D("0.12")),
                         D("0.12"))
        # A path through two priced chains pays both.
        self.assertEqual(scan.row_gas_usdt(both_row, D("0.0012"), D("0.12")),
                         D("0.1212"))
        # Unknown is not free: one unpriced chain makes the whole line unknown.
        self.assertIsNone(scan.row_gas_usdt(eth_row, D("0.0012"), None))
        self.assertEqual(scan.row_gas_note(solana_row, D("0.0012"), "estimated"),
                         "no_evm_leg")
        self.assertEqual(scan.row_gas_note(xlayer_row, None, "gas_unknown"),
                         "unknown:gas_unknown")
        self.assertEqual(
            scan.row_gas_note(eth_row, D("0.0012"), "estimated", None, "gas_unknown"),
            "unknown:gas_unknown")
        self.assertEqual(
            scan.row_gas_note(eth_row, D("0.0012"), "estimated", D("0.12"),
                              "estimated"),
            "estimated")


class FamilyCostTest(unittest.TestCase):
    def setUp(self):
        self.policy = V2Policy.load()

    def test_the_taker_is_charged_only_where_kraken_trades(self):
        for family in KRAKEN_FAMILIES:
            self.assertTrue(costs.has_kraken_leg(family), family)
            self.assertEqual(
                costs.proportional_bps(self.policy, family=family),
                D(self.policy.slippage_buffer_bps)
                + D(self.policy.kraken_taker_bps), family)
        for family in NO_KRAKEN_FAMILIES:
            self.assertFalse(costs.has_kraken_leg(family), family)
            self.assertEqual(
                costs.proportional_bps(self.policy, family=family),
                D(self.policy.slippage_buffer_bps), family)

    def test_an_unknown_family_charges_the_taker(self):
        """A missing cost loses money; an extra cost only misses a trade."""
        for family in (None, "", "some_new_family"):
            self.assertTrue(costs.has_kraken_leg(family), family)

    def test_a_missing_term_is_unknown_not_zero(self):
        for kwargs in ({"gross_usdt": None}, {"notional_usdt": None},
                       {"fixed_cost_usd": None}):
            base = {"gross_usdt": "1", "notional_usdt": "200",
                    "fixed_cost_usd": "0.1"}
            base.update(kwargs)
            self.assertIsNone(costs.net_after_costs(
                policy=self.policy, family="chain_to_chain", **base))


class TwoNetsTest(unittest.TestCase):
    """Every family's net is the same function, called once per reader."""

    def setUp(self):
        self.policy = V2Policy.load()

    def test_scan_kraken_to_chain_net_is_the_shared_model(self):
        got, reason = scan.kraken_to_chain_net(
            notional=D("200"), buy_price=D("100"), sell_price=D("100.4"),
            fee_units=D("0.0029"), policy=self.policy)
        self.assertEqual(reason, "")
        expected = costs.net_after_costs(
            gross_usdt=D("200") / D("100") * D("100.4") - D("200"),
            notional_usdt=D("200"),
            fixed_cost_usd=D("0.0029") * D("100.4"), policy=self.policy,
            family="kraken_to_chain")
        self.assertEqual(got, expected)

    def test_paper_chain_to_kraken_net_is_the_shared_model(self):
        from arb_monitor.xstocks_v2 import paper
        result = paper.chain_to_kraken_net(
            size_usdt=D("25"), tokens=D("0.25"), buy_price_usd=D("100"),
            kraken_bid=D("101"), policy=self.policy,
            transfer_usdt=D("0.37"), gas_usdt=D("0.01"))
        # paper keeps the taker/buffer lines for the ledger: both must be the
        # shared model's own split, not a second multiplication.
        taker, buffer = costs.proportional_parts(
            self.policy, notional=D("25"), family="chain_to_kraken")
        self.assertEqual(result["costs"]["kraken_fee_usdt"], taker)
        self.assertEqual(result["costs"]["slippage_buffer_usdt"], buffer)
        self.assertEqual(result["net_usdt"], costs.net_after_costs(
            gross_usdt=D("25.25") - D("25"), notional_usdt=D("25"),
            fixed_cost_usd=D("0.37") + D("0.01"), policy=self.policy,
            family="chain_to_kraken"))

    def test_no_module_multiplies_the_two_bps_by_hand(self):
        """A guard against the next copy-paste of the cost model.

        ``policy.kraken_taker_bps`` / ``policy.slippage_buffer_bps`` may only be
        read by ``costs`` (and by ``policy`` itself when it loads them). This is
        the textual half of the "one net per row" rule: the numeric half is the
        agreement tests above.
        """
        root = Path(__file__).resolve().parents[1] / "src" / "arb_monitor" / "xstocks_v2"
        offenders: list[str] = []
        for path in sorted(root.glob("*.py")):
            if path.name in {"costs.py", "policy.py"}:
                continue
            text = path.read_text()
            if "policy.kraken_taker_bps" in text or "policy.slippage_buffer_bps" in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    """The exact row shape that used to produce two different nets."""

    def setUp(self):
        self.policy = V2Policy.load()
        self.runner = _live_runner()
        self.row = {"family": "chain_to_chain", "symbol": "SPYx",
                    "source_chain": "solana", "dest_chain": "x-layer",
                    "edge_t0_bps": "55", "edge_after_requote_bps": "55",
                    "notional_usdt": "200", "gross_usdt": "1.1",
                    "bridge_fee_usd": "0.1003056807744", "blockers": []}

    def _gate(self, row, *, size, threshold="20"):
        return self.runner.bridge_net_ok(
            row, snapshot={"threshold_bps": threshold},
            policy=self.policy, available_usdt=D(size))

    def test_the_scan_net_is_the_shared_model(self):
        for family in KRAKEN_FAMILIES + NO_KRAKEN_FAMILIES:
            row = dict(self.row, family=family)
            self.assertEqual(
                scan.bridge_net_usdt(
                    gross_usdt=row["gross_usdt"],
                    notional_usdt=row["notional_usdt"],
                    bridge_fee_usd=row["bridge_fee_usd"], policy=self.policy,
                    family=family),
                costs.net_after_costs(
                    gross_usdt=row["gross_usdt"],
                    notional_usdt=row["notional_usdt"],
                    fixed_cost_usd=row["bridge_fee_usd"], policy=self.policy,
                    family=family),
                family)

    def test_the_gate_and_the_scan_agree_at_the_size_the_gate_judges(self):
        """Same row, same size, same answer -- for every family."""
        for family in KRAKEN_FAMILIES + NO_KRAKEN_FAMILIES:
            row = dict(self.row, family=family)
            for size in ("200", "44.5405", "8.481916"):
                judged = costs.judged_notional(D(row["notional_usdt"]), D(size))
                net = costs.net_after_costs(
                    gross_usdt=(judged * D(row["edge_after_requote_bps"])
                                / D(10000)),
                    notional_usdt=judged,
                    fixed_cost_usd=D(row["bridge_fee_usd"]),
                    policy=self.policy, family=family)
                expected = costs.net_clears(
                    edge_bps=D(row["edge_after_requote_bps"]), net_usdt=net,
                    min_bps=D("20"),
                    min_usdt=D(self.policy.min_net_profit_usdt))
                self.assertEqual(self._gate(row, size=size), expected,
                                 (family, size, net))

    def test_the_phantom_taker_no_longer_hides_a_wallet_sized_trade(self):
        """The regression this file exists for.

        At the real Solana balance (44.5405 USDT, judged at 44.496 after the
        0.999 safety margin) and 60 bps gross, the shared model gives +0.1222
        USDT. The old gate charged an extra 10 bps of Kraken taker for a path
        with no Kraken leg, which put the same row at +0.0777 -- below the 0.10
        floor. Same row, opposite decisions.
        """
        judged = costs.judged_notional(D("200"), D("44.5405"))
        edge = D("60")
        gross = judged * edge / D(10000)
        shared = costs.net_after_costs(
            gross_usdt=gross, notional_usdt=judged,
            fixed_cost_usd=D(self.row["bridge_fee_usd"]), policy=self.policy,
            family="chain_to_chain")
        phantom = gross - D(self.row["bridge_fee_usd"]) - judged * (
            D(self.policy.slippage_buffer_bps)
            + D(self.policy.kraken_taker_bps)) / D(10000)
        self.assertGreaterEqual(shared, D(self.policy.min_net_profit_usdt))
        self.assertLess(phantom, D(self.policy.min_net_profit_usdt))
        row = dict(self.row, edge_t0_bps="60", edge_after_requote_bps="60")
        self.assertTrue(self._gate(row, size=D("44.5405")))


class CreditUnitTest(unittest.TestCase):
    """The sale and the Kraken bid are priced in one unit (2026-10-08).

    The scan prices the buy side per *credit* (``notional / credit``) and Kraken
    quotes per credit. Pricing the sale as ``raw tokens × bid`` therefore
    understated every Solana row by ``multiplier − 1`` bps: 139 bps on JPMx
    (1.0139175), 538 on PFEx, 901 on NFLXx (10.0). The live plan published
    −22.55 USDT for an NFLXx row whose real net was −0.2.

    Numbers below are the recorded JPMx round of 2026-10-08 13:24:14.
    """

    MULT = D("1.0139174713894084")
    BUY = D("326.5737734347531055894182468")
    BID = D("328.98")
    TOKENS = D("0.07550159")
    SIZE = D("25")

    def setUp(self):
        self.policy = V2Policy.load()

    def _net(self, *, credit_per_token):
        return paper.chain_to_kraken_net(
            size_usdt=self.SIZE, tokens=self.TOKENS, buy_price_usd=self.BUY,
            kraken_bid=self.BID, policy=self.policy,
            transfer_usdt=D("0.361828"), gas_usdt=D("0"),
            credit_per_token=credit_per_token)

    def test_the_gross_edge_reproduces_the_row_it_came_from(self):
        credit = self._net(credit_per_token=self.MULT)
        gross = (credit["costs"]["proceeds_usdt"] / self.SIZE - 1) * D(10000)
        self.assertAlmostEqual(
            float(gross), float((self.BID / self.BUY - 1) * D(10000)), places=9)
        self.assertAlmostEqual(float(gross), 73.680949328517947344, places=9)

    def test_the_raw_unit_sale_is_lower_by_exactly_the_multiplier(self):
        credit = self._net(credit_per_token=self.MULT)
        raw = self._net(credit_per_token=D("1"))
        self.assertEqual(
            credit["costs"]["proceeds_usdt"] / raw["costs"]["proceeds_usdt"],
            self.MULT)
        self.assertGreater(credit["net_usdt"], raw["net_usdt"])
        self.assertEqual(credit["credited_units"], self.TOKENS * self.MULT)

    def test_the_evm_chains_keep_the_factor_of_one(self):
        """``balanceOf`` is already the credited unit; only Solana/TON scale."""
        from arb_monitor.xstocks_v2 import units as units_mod
        self.assertNotIn("x-layer", units_mod.SCALED_RAW_CHAINS)
        self.assertIn("solana", units_mod.SCALED_RAW_CHAINS)
        result = paper.chain_to_kraken_net(
            size_usdt=D("200"), tokens=D("0.530061149842545518"),
            buy_price_usd=D("377.3149570750656431189244787"),
            kraken_bid=D("377.35"), policy=self.policy,
            transfer_usdt=D("0"), gas_usdt=D("0.0011435938854656914143"))
        self.assertEqual(result["credit_per_token"], D("1"))


if __name__ == "__main__":
    unittest.main()
