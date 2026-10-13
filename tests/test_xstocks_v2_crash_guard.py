"""A39: 异常必须留痕，而且不能把风险留在外面 (2026-10-08)。

Measured that night: the buy + deposit + Kraken credit all succeeded, the sell
leg raised ``AttributeError``, and **no ledger row was written** while the cycle
lock was released -- so the next round could buy again with the 0.98401 PFEx.T
of the previous one still sitting unsold on Kraken. Records are cheap; a naked
leg nobody wrote down is not.
"""
from __future__ import annotations

import importlib.util
import json
import unittest
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from arb_monitor.xstocks_v2 import ledger
from arb_monitor.xstocks_v2.policy import V2Policy

ROOT = Path(__file__).resolve().parents[1]


def _live_module():
    spec = importlib.util.spec_from_file_location(
        "v2live_crash", ROOT / "scripts" / "xstocks_v2_live.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Exploding:
    """Stands in for a client whose call raises (the A31 AttributeError)."""

    def __getattr__(self, name):            # noqa: ARG002
        raise AttributeError("'V2Policy' object has no attribute 'min_price_vs_ref'")


class ExecuteCycleCrashTest(unittest.TestCase):
    """``execute_cycle`` must return an ``unknown`` row and keep the lock."""

    def test_a_crash_writes_an_unknown_row_and_holds_the_lock(self):
        module = _live_module()
        row = {"symbol": "PFEx",
               "token_address": "XsAtbqkAP1HJxy7hFDeq7ok6yM43DQ9mQ1Rh861X8rw",
               "notional_usdt": "25"}
        plan = {"size": "25", "tokens": "0.93384998", "chosen_usdt": "25"}
        box = SimpleNamespace(jupiter=_Exploding(), solana=object(),
                              wallet=object(), kraken=object())
        with TemporaryDirectory() as tmp:
            entry = module.execute_cycle(row, plan, policy=V2Policy.load(),
                                         box=box, data_dir=tmp)
            self.assertEqual(entry["state"], ledger.UNKNOWN)
            self.assertEqual(entry["held_at"], "unknown")
            self.assertIn("crash:AttributeError", entry["reason"])
            self.assertIn("traceback", entry["extra"])
            # The lock stays and HALT is written: the gate closes on the next
            # round instead of buying a second position.
            self.assertTrue((Path(tmp) / "xstocks_v2_cycle.lock").exists())
            self.assertTrue((Path(tmp) / "xstocks_v2_HALT").exists())
            # ... and the row is recordable, which is what ``run_once`` does.
            path = ledger.record(entry, Path(tmp) / "xstocks_v2_cycles.jsonl")
            written = json.loads(Path(path).read_text().strip().split("\n")[-1])
            self.assertEqual(written["state"], ledger.UNKNOWN)
            self.assertEqual(written["held_at"], "unknown")

    def test_a_busy_lock_is_still_a_plain_refusal_not_a_crash_row(self):
        module = _live_module()
        row = {"symbol": "PFEx",
               "token_address": "XsAtbqkAP1HJxy7hFDeq7ok6yM43DQ9mQ1Rh861X8rw"}
        plan = {"size": "25", "tokens": "0.9", "chosen_usdt": "25"}
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "xstocks_v2_cycle.lock").write_text("1")
            entry = module.execute_cycle(
                row, plan, policy=V2Policy.load(),
                box=SimpleNamespace(jupiter=_Exploding()), data_dir=tmp)
            self.assertEqual(entry["state"], ledger.REFUSED)
            self.assertIn("cycle_busy", entry["reason"])


class GuardFamilyTest(unittest.TestCase):
    """A family script that raises must leave a ledger row behind."""

    def test_a_raising_family_is_recorded_and_halts_when_money_was_in_flight(self):
        """A39, still the rule for a crash *inside* a broadcasting step.

        A51 (2026-10-09) split the old blanket rule in two: this is the half
        that keeps halting, because here the coins may be somewhere we have not
        looked.
        """
        module = _live_module()
        from arb_monitor.xstocks_v2 import incident

        def boom():
            incident.enter_step("bridge")
            raise RuntimeError("bridge exploded")

        with TemporaryDirectory() as tmp:
            summary, crashed = module.guard_family("probe", "PFEx", boom,
                                                   data_dir=tmp)
            self.assertTrue(crashed)
            self.assertEqual(summary["state"], ledger.UNKNOWN)
            self.assertTrue((Path(tmp) / "xstocks_v2_HALT").exists())
            rows = [json.loads(line) for line in
                    (Path(tmp) / "xstocks_v2_cycles.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["state"], ledger.UNKNOWN)
            self.assertEqual(rows[0]["family"], "probe")
            self.assertIn("RuntimeError", rows[0]["reason"])
            self.assertIn("traceback", rows[0]["extra"])

    def test_a_raising_family_before_money_moves_is_a_refusal(self):
        """A51: the crash that started at 10:43 must not stop the machine.

        Reading a wallet balance is not a position. Measured 2026-10-09: the
        probe died in ``usdg_balance``, the machine halted, and the board asked
        a human to reconcile a QQQx position that did not exist.
        """
        module = _live_module()

        def boom():
            raise RuntimeError("rpc read failed")

        with TemporaryDirectory() as tmp:
            summary, crashed = module.guard_family("probe", "QQQx", boom,
                                                   data_dir=tmp)
            self.assertTrue(crashed)
            self.assertEqual(summary["state"], ledger.REFUSED)
            self.assertTrue(summary["reason"].startswith("crash_before_money:"))
            self.assertFalse((Path(tmp) / "xstocks_v2_HALT").exists())
            rows = [json.loads(line) for line in
                    (Path(tmp) / "xstocks_v2_cycles.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["state"], ledger.REFUSED)
            self.assertEqual(rows[0]["held_at"], "")
            self.assertIn("traceback", rows[0]["extra"])
            self.assertEqual([row["symbol"] for row in
                              ledger.open_positions(Path(tmp) / "xstocks_v2_cycles.jsonl")],
                             [])

    def test_a_healthy_family_is_untouched(self):
        module = _live_module()
        with TemporaryDirectory() as tmp:
            summary, crashed = module.guard_family(
                "probe", "PFEx", lambda: {"symbol": "PFEx", "state": "sold"},
                data_dir=tmp)
            self.assertFalse(crashed)
            self.assertEqual(summary["state"], "sold")
            self.assertFalse((Path(tmp) / "xstocks_v2_HALT").exists())
            self.assertFalse((Path(tmp) / "xstocks_v2_cycles.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
