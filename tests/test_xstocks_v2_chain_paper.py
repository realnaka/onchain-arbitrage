"""chain → chain paper requote: the numbers come from the injected quotes, and nothing signs."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "route_paper_under_test", ROOT / "scripts" / "xstocks_v2_route.py")
ROUTE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ROUTE)


class PaperOnceTest(unittest.TestCase):
    def setUp(self):
        self.saved = ROUTE.PAPER_LEDGER
        ROUTE.PAPER_LEDGER = Path(tempfile.mkdtemp()) / "paper.jsonl"

    def tearDown(self):
        ROUTE.PAPER_LEDGER = self.saved

    def test_net_is_proceeds_minus_spend_fee_and_gas(self):
        def quote(chain, src, dst, amount):
            if chain == "x-layer":
                return 10 ** 16          # 0.01 token
            return 8_200_000              # 8.2 USDC

        out = ROUTE.paper_once(
            symbol="SPYx", source="x-layer", dest="eth", notional="8",
            bridge_fee_usd="1.5", gas_usd="0.1", quote_fn=quote,
            token="0x90a2a4c76b5d8c0bc892a69ea28aa775a8f2dd48")
        self.assertEqual(out["state"], "paper")
        self.assertFalse(out["broadcast"])
        self.assertEqual(Decimal(out["net_usdt"]), Decimal("8.2") - Decimal("8")
                         - Decimal("1.5") - Decimal("0.1"))

    def test_a_missing_fee_is_refused_before_any_quote(self):
        def quote(*_args):
            raise AssertionError("quoted without a fee")

        out = ROUTE.paper_once(
            symbol="SPYx", source="x-layer", dest="eth", notional="8",
            bridge_fee_usd=None, quote_fn=quote, token="0x" + "11" * 20)
        self.assertEqual(out["state"], "refused")
        self.assertEqual(out["reason"], "cost_unknown")

    def test_a_chain_without_a_quote_token_is_refused(self):
        out = ROUTE.paper_once(
            symbol="SPYx", source="hyperevm", dest="eth", notional="8",
            bridge_fee_usd="1", quote_fn=lambda *_a: 1, token="0x" + "11" * 20)
        self.assertEqual(out["reason"], "no_paper_quote_token")


if __name__ == "__main__":
    unittest.main()
