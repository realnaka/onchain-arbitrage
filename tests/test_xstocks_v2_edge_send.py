"""Edge sender ledgers: only an arrival opens a route, and a rerun adds nothing."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "edge_send_under_test", ROOT / "scripts" / "xstocks_v2_edge_send.py")
EDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EDGE)


def _row(source, dest, reason, tx, **extra):
    return {"kind": "evm_to_evm", "source": source, "dest": dest, "status": "ready",
            "sent": True, "reason": reason, "tx": tx, **extra}


class SyncLedgersTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.saved = (EDGE.PROOF, EDGE.ROUTE_LEDGER, EDGE.FEES_FILE)
        EDGE.PROOF = self.dir / "proof.json"
        EDGE.ROUTE_LEDGER = self.dir / "routes.jsonl"
        EDGE.FEES_FILE = self.dir / "fees.json"
        EDGE.PROOF.write_text(json.dumps({"rows": [
            _row("eth", "ink", "arrived", "0xaaa", arrived_tokens="0.0006",
                 fee_native="0.000208769294292097 ETH"),
            _row("eth", "optimism", "source_confirmed", "0xbbb",
                 fee_native="0.0002 ETH"),
            _row("solana", "bsc", "arrived", "sig1", arrived_symbol="TSLAx",
                 fee_native="0.001230561 SOL"),
        ]}))
        EDGE.FEES_FILE.write_text(json.dumps({"edges": {}}))

    def tearDown(self):
        EDGE.PROOF, EDGE.ROUTE_LEDGER, EDGE.FEES_FILE = self.saved

    def _routes(self):
        return [json.loads(line) for line in EDGE.ROUTE_LEDGER.read_text().splitlines()]

    def test_only_arrivals_open_an_edge(self):
        result = EDGE.sync_ledgers(http=object())
        self.assertEqual(sorted(result["routes"]), ["SPYx eth->ink", "TSLAx solana->bsc"])
        pairs = {(row["source_chain"], row["dest_chain"]) for row in self._routes()}
        self.assertNotIn(("eth", "optimism"), pairs)
        self.assertTrue(all(row["arrived"] is True for row in self._routes()))
        fees = json.loads(EDGE.FEES_FILE.read_text())["edges"]
        self.assertEqual(fees["eth->ink"]["fee_asset"], "ETH")
        self.assertEqual(fees["solana->bsc"]["fee_asset"], "SOL")
        self.assertNotIn("eth->optimism", fees)

    def test_a_rerun_adds_nothing(self):
        EDGE.sync_ledgers(http=object())
        again = EDGE.sync_ledgers(http=object())
        self.assertEqual(again, {"routes": [], "fees": []})
        self.assertEqual(len(self._routes()), 2)
        self.assertEqual(json.loads(EDGE.FEES_FILE.read_text())["edges"]["eth->ink"]["samples"], 1)


if __name__ == "__main__":
    unittest.main()
