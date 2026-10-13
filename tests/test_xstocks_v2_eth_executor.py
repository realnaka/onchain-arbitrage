"""Hermetic tests for the Ethereum chain -> Kraken executor.

No network, no wallet, no Kraken key: every leg that moves money is driven
through injected values, and the assertions are about the decisions taken
*before* anything is signed.
"""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from arb_monitor.xstocks_v2 import legs_eth
from arb_monitor.xstocks_v2.policy import V2Policy

ROOT = Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location(
        "eth_roundtrip", ROOT / "scripts" / "xstocks_v2_eth_roundtrip.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _module_live_runner():
    spec = importlib.util.spec_from_file_location(
        "v2live_for_eth_tests", ROOT / "scripts" / "xstocks_v2_live.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _module_board():
    spec = importlib.util.spec_from_file_location(
        "v2board_for_eth_tests", ROOT / "scripts" / "xstocks_v2_board.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EthereumLegTest(unittest.TestCase):
    """The calldata and the rail parsing, both pure."""

    def test_transfer_calldata_is_a_plain_erc20_transfer(self):
        to = "0x" + "ab" * 20
        data = legs_eth.transfer_calldata(to, 8127359)
        self.assertTrue(data.startswith("0xa9059cbb"))
        self.assertEqual(len(data), 2 + 8 + 64 * 2)
        self.assertEqual(data[10:74], "0" * 24 + "ab" * 20)
        self.assertEqual(int(data[74:], 16), 8127359)
        with self.assertRaises(ValueError):
            legs_eth.transfer_calldata(to, 0)

    def test_the_ethereum_rail_is_found_by_network_not_by_position(self):
        methods = [{"method": "SPCXx - Solana", "minimum": "0.0081"},
                   {"method": "SPCXx - Ethereum", "minimum": "0.0081",
                    "fee": "0.00000000"}]
        found = legs_eth.ethereum_method(methods)
        self.assertEqual(found["method"], "SPCXx - Ethereum")
        self.assertEqual(legs_eth.deposit_min_units(found), D("0.0081"))
        self.assertEqual(legs_eth.deposit_fee_units(found), D("0"))
        self.assertIsNone(legs_eth.ethereum_method([{"method": "SPCXx - TON"}]))
        # Unknown is not zero: a missing minimum must not read as "no minimum".
        self.assertIsNone(legs_eth.deposit_min_units({"method": "SPCXx - Ethereum"}))


class GasGuardTest(unittest.TestCase):
    """A cycle that cannot pay for its own gas is refused before it starts."""

    def setUp(self):
        self.module = _module()

    def test_unknown_balance_or_price_refuses(self):
        self.assertEqual(self.module.gas_guard({"eth_balance": None,
                                                "gas_wei": 110_000_000})[0], False)
        self.assertEqual(self.module.gas_guard({"eth_balance": D("1"),
                                                "gas_wei": None})[0], False)

    def test_a_wallet_below_two_actions_of_gas_refuses(self):
        cheap = self.module.GAS_UNITS_FOR_ONE_CYCLE * D(110_000_000) / D(10 ** 18)
        ok, why = self.module.gas_guard({"eth_balance": cheap, "gas_wei": 110_000_000})
        self.assertFalse(ok)
        self.assertTrue(why.startswith("gas_low"))
        ok, why = self.module.gas_guard({"eth_balance": cheap * D("2.5"),
                                         "gas_wei": 110_000_000})
        self.assertTrue(ok, why)

    def test_the_buffer_is_two_actions(self):
        self.assertEqual(self.module.GAS_LIMIT_BUFFER, D("2"))


class CreditGuardTest(unittest.TestCase):
    """A trade may not be sized against a credit ratio nobody measured."""

    def setUp(self):
        self.module = _module()

    def test_an_unmeasured_rail_refuses_the_trade_and_names_the_probe(self):
        ok, why = self.module.credit_guard("SPCXx", units={}, probe_only=False,
                                           min_units="0.00810000")
        self.assertFalse(ok)
        self.assertIn("not measured", why)
        self.assertIn("--probe-only", why)
        self.assertIn("0.00810000", why)

    def test_the_probe_itself_is_allowed(self):
        ok, why = self.module.credit_guard("SPCXx", units={}, probe_only=True)
        self.assertTrue(ok, why)

    def test_a_measured_rail_lets_the_trade_through(self):
        units = {"STRCx": {"deposit_credit_units_per_balance_unit": "1"}}
        ok, why = self.module.credit_guard("STRCx", units=units, probe_only=False)
        self.assertTrue(ok, why)


class EthSelectorsTest(unittest.TestCase):
    """The resident runner's two eth lists: trade vs sample."""

    def setUp(self):
        self.live_runner = _module_live_runner()

    def _row(self, net="0.5", blockers=()):
        return {
            "symbol": "SPCXx", "family": "chain_to_kraken", "chain": "eth",
            "buy_venue": "uniswap", "sell_venue": "kraken",
            "buy_price_usd": "165", "sell_price_usd": "166.5",
            "edge_t0_bps": "94", "net_usdt": net,
            "blockers": list(blockers), "executable": not blockers,
        }

    def test_an_unmeasured_symbol_is_a_sample_not_a_trade(self):
        snapshot = {"threshold_bps": "4", "rows": [self._row()]}
        self.assertEqual(self.live_runner.eth_candidates(snapshot, units={}), [])
        probes = self.live_runner.eth_probe_candidates(snapshot, units={})
        self.assertEqual([row["symbol"] for row in probes], ["SPCXx"])

    def test_a_measured_symbol_is_a_trade(self):
        units = {"SPCXx": {"deposit_credit_units_per_balance_unit": "1"}}
        snapshot = {"threshold_bps": "4", "rows": [self._row()]}
        rows = self.live_runner.eth_candidates(snapshot, units=units)
        self.assertEqual([row["symbol"] for row in rows], ["SPCXx"])
        # And it is not also queued as a sample.
        self.assertEqual(
            self.live_runner.eth_probe_candidates(snapshot, units=units), [])

    def test_a_losing_row_is_neither_a_trade_nor_a_sample(self):
        snapshot = {"threshold_bps": "4",
                    "rows": [self._row(net="0.02", blockers=["below_net"])]}
        units = {"SPCXx": {"deposit_credit_units_per_balance_unit": "1"}}
        self.assertEqual(self.live_runner.eth_candidates(snapshot, units=units), [])
        self.assertEqual(
            self.live_runner.eth_probe_candidates(snapshot, units=units), [])

    def test_a_bad_observation_is_not_probed(self):
        snapshot = {"threshold_bps": "4",
                    "rows": [self._row(blockers=["price_outlier"])]}
        self.assertEqual(
            self.live_runner.eth_probe_candidates(snapshot, units={}), [])

    def test_a_row_with_no_ethereum_rail_is_not_this_executors_sample(self):
        """``unmeasured_route`` means a bridge, not a Kraken deposit."""
        snapshot = {"threshold_bps": "4",
                    "rows": [self._row(blockers=["unmeasured_route", "below_threshold"])]}
        self.assertEqual(
            self.live_runner.eth_probe_candidates(snapshot, units={}), [])


class FinalHeldAtOnTheRowTest(unittest.TestCase):
    """``probed`` on Kraken must not read back as ``dest_wallet``."""

    def test_a_probed_row_carries_where_the_coins_are(self):
        from arb_monitor.xstocks_v2 import ledger

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cycles.jsonl"
            eth_row = ledger.row(
                symbol="TSLAx", chain="eth", family="chain_to_kraken",
                state=ledger.PROBED, mode="live", size_usdt=D("1.11"),
                held_at="kraken", reason="probe: deposit minimum sent",
                final_held_at="kraken")
            ledger.record(eth_row, path)
            self.assertEqual(ledger.effective_held_at(eth_row), "kraken")
            # An old row without the field keeps the legacy Solana reading.
            old = {"symbol": "NVDAx", "state": ledger.PROBED, "held_at": "unknown"}
            self.assertEqual(ledger.effective_held_at(old), "dest_wallet")


class PolicyAttributeGuardTest(unittest.TestCase):
    """A missing policy attribute must fail here, not every round in production.

    2026-10-09: the new eth sample block read ``policy.probe_max_usdt_per_probe``
    while the field is ``probe_max_usdt``. Nothing in the test suite touched that
    line, so the resident live process raised ``AttributeError`` at the top of
    every round for ~5 minutes — the whole chain idled, solana families
    included, and the only trace was one ``{"error": ...}`` line per round.
    """

    def test_every_policy_attribute_the_runner_reads_exists(self):
        import re

        source = (ROOT / "scripts" / "xstocks_v2_live.py").read_text()
        names = sorted(set(re.findall(r"\bpolicy\.([a-z_][a-z0-9_]*)", source)))
        self.assertTrue(names)
        policy = V2Policy.load()
        missing = [name for name in names if not hasattr(policy, name)]
        self.assertEqual(missing, [])


class SellCarriesTheInventoryTest(unittest.TestCase):
    """The sell leg takes the whole Kraken balance, not only this slice."""

    class _Kraken:
        def __init__(self, held):
            self.held = held

        def balance(self):
            return {"SPCXx.T": self.held}

    def setUp(self):
        self.module = _module()
        self.sold = {}

    def _ctx(self, held):
        return {"symbol": "SPCXx", "policy": V2Policy.load(),
                "price": D("166.5"), "kraken": self._Kraken(held)}

    def _run(self, held, credit):
        def fake_sell(kraken, client, policy, *, symbol, tokens, ref):
            self.sold["tokens"] = tokens
            return {"ok": True, "price": "166.5", "txid": "T", "cost": "100"}

        original = self.module.LIVE._sell
        self.module.LIVE._sell = fake_sell
        try:
            state = {"credit": D(str(credit))}
            out = self.module.sell_runner(self._ctx(held), state)()
        finally:
            self.module.LIVE._sell = original
        return out, state

    def test_the_probe_slice_is_sold_along_with_this_cycles_slice(self):
        out, _state = self._run("0.61", "0.6")
        self.assertEqual(out.status, "ok")
        self.assertEqual(self.sold["tokens"], D("0.61"))

    def test_an_unreadable_balance_falls_back_to_the_credited_units(self):
        out, state = self._run(None, "0.6")
        self.assertEqual(out.status, "ok")
        self.assertEqual(self.sold["tokens"], D("0.6"))
        self.assertNotIn("sell_tokens", state)


class GasLinePanelTest(unittest.TestCase):
    """The board has to print the cost the net subtracts."""

    def test_both_gas_lines_render(self):
        import json
        import tempfile

        board = _module_board()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / board.SCAN.name).write_text(json.dumps({
                "generated_ms": board.time.time() * 1000, "phase": "paper_kraken",
                "threshold_bps": "4", "rows": [],
                "gas_lines": {"xlayer_usdt": "0.0011",
                              "xlayer_gas_price_wei": "20000001",
                              "source": "eth_gasPrice",
                              "eth_usdt": "0.1276",
                              "eth_gas_price_wei": "115712379",
                              "eth_source": "eth_gasPrice"}}))
            data = board.collect(data_dir=Path(tmp), interval_s=180.0)
            page = board.render(data)
        self.assertIn("Ethereum 一次动作", page)
        self.assertIn("0.1276", page)
        self.assertIn("X Layer 一次动作", page)


class BuyPriceGuardTest(unittest.TestCase):
    """The decision price is the pool quote; a worse fill is a different trade."""

    def setUp(self):
        self.module = _module()
        self.module.erc20_balance = lambda *a, **k: D("0")
        self.policy = V2Policy.load()

    def _ctx(self, quote_price, ref):
        return {
            "policy": self.policy, "http": object(), "notional": D("25"),
            "ref_buy": D(str(ref)),
            "price": D("205"), "gas_usdt": D("0.1"),
            "probe_only": False,
            "required_edge_bps": D("4"),
            "product": {"token_address": "0x" + "11" * 20,
                        "quote_address": "0x" + "22" * 20,
                        "token_decimals": 18},
            "quote": {"ok": True},
        }

    def test_a_quote_that_no_longer_pays_refuses(self):
        """Profit is the judge, not the distance from the snapshot."""
        self.module.quote_buy = lambda http, product, notional: {
            "ok": True, "tokens": D("0.1"), "price_usd": D("206"), "route": ["x"]}
        out = self.module.buy_runner(self._ctx("206", "200"), {})()
        self.assertEqual(out.status, "refused")
        self.assertTrue(out.detail.startswith("below_net_at_execution"), out.detail)

    def test_a_sample_buys_at_any_price(self):
        """「无条件先拿样本」: the price is recorded, never consulted.

        219 bps below the bid is the real TSLAx case from 2026-10-09.
        """
        dear = D("200") * (1 + D("0.0219"))
        self.module.quote_buy = lambda http, product, notional: {
            "ok": True, "tokens": D("0.1"), "price_usd": dear, "route": ["x"],
            "impact_pct": D("0.1")}
        self.module.build_tx = lambda *a, **k: {"amount_raw": 25000000,
                                                "to": "0x" + "33" * 20,
                                                "data": "0xdead", "gas": None}
        self.module.ensure_allowance = lambda *a, **k: {"state": "already_approved"}
        self.module.simulate = lambda *a, **k: (False, "stop here")
        ctx = self._ctx(dear, "200")
        ctx["price"] = D("200")
        ctx["probe_only"] = True
        state = {}
        out = self.module.buy_runner(ctx, state)()
        self.assertEqual(out.detail, "swap_simulation:stop here")
        self.assertTrue(state.get("probe_unconditional"))
        # The trade path still refuses the same quote.
        ctx = self._ctx(dear, "200")
        ctx["price"] = D("200")
        out = self.module.buy_runner(ctx, {})()
        self.assertEqual(out.status, "refused")
        self.assertTrue(out.detail.startswith("below_net_at_execution"), out.detail)

    def test_a_worse_quote_that_still_pays_is_accepted(self):
        # 10 bps worse than the snapshot (200 -> 200.2), but 205 still pays.
        price = D("200.2")
        self.module.quote_buy = lambda http, product, notional: {
            "ok": True, "tokens": D("0.1"), "price_usd": price, "route": ["x"],
            "impact_pct": D("0.5")}
        self.module.build_tx = lambda *a, **k: {"amount_raw": 25000000,
                                                "to": "0x" + "33" * 20,
                                                "data": "0xdead", "gas": None}
        self.module.ensure_allowance = lambda *a, **k: {"state": "already_approved"}
        self.module.simulate = lambda *a, **k: (False, "stop here")
        state = {}
        out = self.module.buy_runner(self._ctx(price, "200"), state)()
        # The stub stops it at the simulation, i.e. it passed the profit check.
        self.assertEqual(out.detail, "swap_simulation:stop here")
        self.assertEqual(state["exec_vs_row_bps"], "10.00")


if __name__ == "__main__":
    unittest.main()
