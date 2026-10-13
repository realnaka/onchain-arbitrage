"""Hermetic checks for Kraken xStocks unit/minimum parsing."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from arb_monitor.xstocks_v2 import units


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "kraken_xstocks_units.py"
    spec = importlib.util.spec_from_file_location("kraken_xstocks_units", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_parses_apple_description(self):
        got = self.mod.parse_order_descr(
            "sell 1.000000 AAPLx/USD @ limit 1000.00 "
            "(0.996741 AAPLSPV/USD @ limit 1003.27)")
        self.assertEqual(got["market_symbol"], "AAPLx")
        self.assertEqual(got["underlying_symbol"], "AAPLSPV")
        self.assertEqual(got["underlying_per_market_unit"], Decimal("0.996741"))

    def test_parses_strcx_description(self):
        got = self.mod.parse_order_descr(
            "sell 0.054600 STRCx/USD @ limit 99.34 "
            "(0.050009 STRCSPV/USD @ limit 108.46)")
        self.assertEqual(got["underlying_symbol"], "STRCSPV")
        self.assertAlmostEqual(float(got["underlying_per_market_unit"]), 0.91591575, places=6)

    def test_rejects_an_unrelated_description(self):
        self.assertIsNone(self.mod.parse_order_descr("sell 1.0 BADPAIR"))


class OutputLocationTest(unittest.TestCase):
    """A refresh must not leave the runner's checkout dirty.

    Measured 2026-10-10: the table was written into the tracked
    ``config/kraken_xstocks_units.toml``, so the nightly refresh made
    ``~/arb-runtime`` dirty and ``scripts/deploy-from-github.sh`` answered
    "working tree dirty; not pulling" from then on -- the trading Mac stopped
    taking new code.
    """

    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_the_measured_table_is_written_outside_the_tracked_tree(self):
        self.assertIn("data-v2", self.mod.OUT.parts)
        self.assertEqual(self.mod.OUT.name, "kraken_xstocks_units.toml")
        self.assertEqual(self.mod.SEED, self.mod.OUT.parents[1] / "config" / self.mod.OUT.name)
        self.assertNotEqual(self.mod.OUT, self.mod.SEED)

    def test_the_reader_prefers_what_this_machine_measured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "data-v2").mkdir()
            seed = root / "config" / "kraken_xstocks_units.toml"
            live = root / "data-v2" / "kraken_xstocks_units.toml"
            seed.write_text("seed\n")
            self.assertEqual(units.units_config_path(root), seed)
            live.write_text("live\n")
            self.assertEqual(units.units_config_path(root), live)
            self.assertEqual(units.units_config_sources(root), [seed, live])

    def test_the_previous_table_is_read_from_both_files(self):
        """A ratio merged through a pull request must not be shadowed."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "data-v2").mkdir()
            seed = root / "config" / "kraken_xstocks_units.toml"
            live = root / "data-v2" / "kraken_xstocks_units.toml"
            seed.write_text('[kraken_units.SPCXx]\n'
                            'deposit_credit_units_per_balance_unit = "1"\n')
            live.write_text('[kraken_units.SPCXx]\n'
                            'deposit_credit_units_per_balance_unit = "0.5"\n'
                            '[kraken_units.TSLAx]\n'
                            'deposit_credit_units_per_balance_unit = "1"\n')
            original = (self.mod.SEED, self.mod.OUT)
            self.addCleanup(setattr, self.mod, "SEED", original[0])
            self.addCleanup(setattr, self.mod, "OUT", original[1])
            self.mod.SEED, self.mod.OUT = seed, live
            evidence = self.mod.load_existing_deposit_evidence()
            self.assertEqual(
                evidence["TSLAx"]["deposit_credit_units_per_balance_unit"], "1")
            # The live measurement wins over the committed seed for the same
            # symbol: the file this machine wrote is the newer one.
            self.assertEqual(
                evidence["SPCXx"]["deposit_credit_units_per_balance_unit"], "0.5")


if __name__ == "__main__":
    unittest.main()
