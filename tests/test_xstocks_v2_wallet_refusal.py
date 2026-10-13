"""A wallet that refused to sign is ``refused``, not ``unknown`` + HALT (A95)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arb_monitor.xstocks_v2 import executor as ex

REFUSAL = ('{"ok": false, "error": "Wallet API error (code=10004): '
           'This address doesn’t have enough native tokens."}')


class BroadcastFailureTest(unittest.TestCase):
    def test_refusal_with_a_still_nonce_is_refused(self):
        outcome = ex.broadcast_failure(RuntimeError(REFUSAL), nonce_before=31,
                                       nonce_after=31, label="swap_broadcast")
        self.assertEqual(outcome.status, ex.REFUSED)
        self.assertIn("wallet_refused", outcome.detail)

    def test_a_moved_nonce_stays_unknown(self):
        outcome = ex.broadcast_failure(RuntimeError(REFUSAL), nonce_before=31,
                                       nonce_after=32)
        self.assertEqual(outcome.status, ex.UNKNOWN)
        self.assertEqual(outcome.held_at, "wallet")

    def test_an_unreadable_nonce_stays_unknown(self):
        for before, after in ((None, 31), (31, None), (None, None)):
            outcome = ex.broadcast_failure(RuntimeError(REFUSAL), nonce_before=before,
                                           nonce_after=after)
            self.assertEqual(outcome.status, ex.UNKNOWN)

    def test_any_other_error_stays_unknown(self):
        outcome = ex.broadcast_failure(RuntimeError("timeout after 90 s"),
                                       nonce_before=31, nonce_after=31)
        self.assertEqual(outcome.status, ex.UNKNOWN)

    def test_a_refused_first_leg_writes_no_halt(self):
        halt = Path(tempfile.mkdtemp()) / "HALT"
        step = ex.Step("buy", lambda: ex.broadcast_failure(
            RuntimeError(REFUSAL), nonce_before=5, nonce_after=5),
            held_at="wallet", broadcasts=True, inventory=True, retry_once=True)
        machine = ex.Executor(symbol="MSTRx", chain="eth", family="chain_to_kraken",
                              mode="live", size_usdt="25", tokens="0.1", steps=[step],
                              gate=lambda: (True, "ok"),
                              ledger_path=halt.parent / "ledger.jsonl", halt_path=halt)
        result = machine.run()
        self.assertEqual(result["state"], ex.REFUSED)
        self.assertFalse(result["keep_lock"])
        self.assertFalse(halt.exists())

    def test_a_zero_amount_retry_does_not_halt_every_symbol(self):
        """2026-10-10 09:07: the wallet answered before any hash existed."""
        halt = Path(tempfile.mkdtemp()) / "HALT"
        step = ex.Step("buy", lambda: ex.Outcome(
            ex.RETRY, "--amount must be greater than zero"),
            held_at="dest_wallet", broadcasts=True, retry_once=True)
        machine = ex.Executor(symbol="SPYx", chain="x-layer",
                              family="xlayer_via_bridge_to_solana", mode="live",
                              size_usdt="0.000000999", tokens="0.001",
                              steps=[step], gate=lambda: (True, "ok"),
                              ledger_path=halt.parent / "ledger.jsonl",
                              halt_path=halt)
        result = machine.run()
        self.assertEqual(result["state"], ex.REFUSED)
        self.assertFalse(result["keep_lock"])
        self.assertFalse(result["halt"])
        self.assertFalse(halt.exists())


if __name__ == "__main__":
    unittest.main()
