"""Hermetic checks for the manual Ethereum execution helpers."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "xstocks_eth_live.py"
    spec = importlib.util.spec_from_file_location("xstocks_eth_live", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CalldataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_approve_calldata_shape(self):
        data = self.mod.approve_calldata(
            "0x40aa958dd87fc8305b97f2ba922cddca374bcd7f", 5_200_000)
        self.assertTrue(data.startswith("0x095ea7b3"))
        self.assertEqual(len(data), 2 + 8 + 64 * 2)

    def test_transfer_calldata_shape(self):
        data = self.mod.transfer_calldata(
            "0xfdeff442c1560def7dd32f149f66f77fc0f0c6f5", 51_842_761_266_571_511)
        self.assertTrue(data.startswith("0xa9059cbb"))
        self.assertEqual(len(data), 2 + 8 + 64 * 2)

    def test_known_token_resolution(self):
        self.assertEqual(
            self.mod.token_address("STRCx"),
            "0x1aad217b8f78dba5e6693460e8470f8b1a3977f3")


if __name__ == "__main__":
    unittest.main()
