"""The sixth family: buy on Ethereum -> bridge to Solana -> deposit into Kraken.

There was no slot for it until 2026-10-10 (A109): the scan priced
``chain_via_bridge_to_kraken`` for eth, ``capability`` said every leg was
ready, and the resident runner only knew the X Layer pair. This file pins the
two things about the new script that a second walk would otherwise re-learn
the hard way, plus the wiring that keeps it honest.
"""
from __future__ import annotations

import importlib.util
import unittest
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location(
        "v2ethviasol", ROOT / "scripts" / "xstocks_v2_eth_via_solana.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BridgeAmountTest(unittest.TestCase):
    """How much of the wallet the bridge may ask for."""

    def setUp(self):
        self.mod = _script()

    def test_the_quote_is_truncated_to_whole_8_decimal_units(self):
        # CCIP carries whole 8-decimal units; anything finer reverts.
        raw = self.mod.bridge_amount(quoted_tokens=D("0.005099998816312345"))
        self.assertEqual(raw % (10 ** 10), 0)
        self.assertLessEqual(raw, int(D("0.0051") * D(10 ** 18)))

    def test_the_wallet_balance_caps_the_quote(self):
        """Measured 14:08: the quote came back 32 wei larger than the balance.

        ``token_short: 5098860835200000 < 5099180000000000`` -- the wallet cannot
        pay a number a quote invented.
        """
        held = D("0.0050988608352")
        quoted = D("0.00509918")
        raw = self.mod.bridge_amount(quoted_tokens=quoted, held_tokens=held)
        # The cap is the balance, truncated to whole 8-decimal units.
        self.assertEqual(raw, 5098860000000000)
        self.assertLess(raw, int(quoted * D(10 ** 18)))
        self.assertLessEqual(raw, int(held * D(10 ** 18)))

    def test_without_a_balance_the_quote_stands(self):
        self.assertEqual(self.mod.bridge_amount(quoted_tokens=D("0.0051")),
                         int(D("0.0051") * D(10 ** 18)))

    def test_an_unreadable_quote_is_zero_not_a_guess(self):
        self.assertEqual(self.mod.bridge_amount(quoted_tokens=None), 0)
        self.assertEqual(self.mod.bridge_amount(quoted_tokens="abc"), 0)


class WiringTest(unittest.TestCase):
    """The parts a hermetic test can hold: no silent path, no bare broadcast."""

    def setUp(self):
        self.source = (ROOT / "scripts" / "xstocks_v2_eth_via_solana.py").read_text()

    def test_nothing_signs_without_confirm_live(self):
        self.assertIn('if not confirm_live:', self.source)
        self.assertIn('"state": "planned"', self.source)

    def test_the_state_machine_and_the_gate_are_the_shared_ones(self):
        self.assertIn("ex.Executor(", self.source)
        self.assertIn("live.live_gate(", self.source)
        self.assertIn("cycle_core.CycleLock(", self.source)

    def test_the_solana_legs_use_the_probe_transport_not_the_evm_one(self):
        """14:05: one transport for two clients ended in ``unreadable``.

        ``feeds.http.Http`` has ``.request``; the probe client has ``.rpc``.
        Handing the EVM one to ``solana_balance`` raises AttributeError, which
        the bridge step reports as ``solana_balance_unreadable``.
        """
        self.assertIn('sol_http = probe.Http()', self.source)
        self.assertIn('"sol_http": sol_http', self.source)
        self.assertIn('ctx["probe"].solana_balance(ctx["sol_http"], ctx["mint"])',
                      self.source)

    def test_the_runner_slot_map_knows_this_pair_is_x_layer_only(self):
        """The resident runner must not hand eth rows to the X Layer script."""
        runner = (ROOT / "scripts" / "xstocks_v2_live.py").read_text()
        self.assertIn('VIA_BRIDGE_TO_KRAKEN: "x-layer->solana"', runner)


if __name__ == "__main__":
    unittest.main()
