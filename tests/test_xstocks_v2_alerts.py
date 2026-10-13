"""What the watcher is allowed to shout about.

Every case here is a rule a live failure taught, or one that a wrong version of
this module would break:

* a ``probed`` row is inventory we mean to hold, not a stuck position;
* an attempt that is *walking* is not an alert -- only one that died;
* "could not read the wallet" is a fact about us and must not be silent.
"""
from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from arb_monitor.xstocks_v2 import alerts, cycle, incident, ledger


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n")


def _fresh_runner_state(data_dir: Path, now_ms: int) -> None:
    for name in ("xstocks_v2_live.json", "xstocks_v2_paper.json"):
        _write(data_dir / name, {"updated_ms": now_ms})


class CollectTest(unittest.TestCase):

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name) / "data-v2"
        self.root = Path(self._tmp.name)
        self.data.mkdir(parents=True, exist_ok=True)
        self.now = int(time.time() * 1000)
        _fresh_runner_state(self.data, self.now)

    def _collect(self):
        return alerts.collect(data_dir=self.data, root=self.root, now_ms=self.now)

    def _row(self, **over):
        row = ledger.row(symbol="QQQx", chain="x-layer", family="probe",
                         state=ledger.REFUSED, mode="live", size_usdt="1")
        row.update(over)
        ledger.record(row, self.data / ledger.DEFAULT_PATH.name)

    def test_a_quiet_tree_raises_nothing(self):
        self.assertEqual(self._collect(), [])

    def test_the_halt_file_is_a_stop(self):
        cycle.halt_paths(self.data)["halt"].write_text("halt\n")
        found = self._collect()
        self.assertEqual([a["kind"] for a in found], [alerts.HALT])
        self.assertEqual(found[0]["severity"], "stop")

    def test_a_stale_lock_is_a_stop_and_names_the_step(self):
        lock = self.data / "xstocks_v2_cycle.lock"
        lock.write_text("999999999")
        Path(str(lock) + ".json").write_text(json.dumps({
            "pid": 999999999, "symbol": "QQQx", "family": "probe",
            "step": "bridge", "started_ms": self.now - 60_000}) + "\n")
        kinds = {a["kind"]: a for a in self._collect()}
        self.assertIn(alerts.STALE_LOCK, kinds)
        self.assertEqual(kinds[alerts.STALE_LOCK]["symbol"], "QQQx")
        self.assertIn("bridge", kinds[alerts.STALE_LOCK]["detail"])

    def test_designed_inventory_is_not_an_alert(self):
        """NVDAx/SPCXx sit in our wallet on purpose (operator 2026-10-07)."""
        self._row(state=ledger.PROBED, held_at="dest_wallet", symbol="NVDAx")
        self.assertEqual(self._collect(), [])

    def test_a_stopped_position_is_an_alert(self):
        self._row(state=ledger.STOPPED, held_at="kraken", symbol="SPCXx")
        found = self._collect()
        self.assertEqual([a["kind"] for a in found], [alerts.HELD])
        self.assertEqual(found[0]["symbol"], "SPCXx")
        self.assertEqual(found[0]["severity"], "hold")

    def test_an_attempt_that_is_still_walking_is_not_an_alert(self):
        incident.begin(family="probe", symbol="QQQx", data_dir=self.data)
        incident.enter_step("buy")
        incident.sent("onchainos:swap execute", "buy QQQx")
        self.assertEqual(self._collect(), [])

    def test_an_attempt_that_died_after_money_moved_is_an_alert(self):
        incident.begin(family="probe", symbol="QQQx", data_dir=self.data)
        incident.enter_step("bridge")
        incident.sent("onchainos:wallet contract-call", "bridge x-layer -> solana")
        incident.finish("halted", detail="crash:SSLError", data_dir=self.data)
        found = self._collect()
        self.assertEqual([a["kind"] for a in found], [alerts.INCIDENT])
        self.assertEqual(found[0]["symbol"], "QQQx")
        self.assertIn("HALT", found[0]["detail"])

    def test_an_attempt_that_ended_cleanly_is_not_an_alert(self):
        incident.begin(family="probe", symbol="QQQx", data_dir=self.data)
        incident.enter_step("buy")
        incident.finish("finished", detail="probed", data_dir=self.data)
        self.assertEqual(self._collect(), [])

    def test_a_legacy_evidence_file_with_a_dead_writer_is_an_alert(self):
        """An older process wrote no ``state``; it must not become invisible."""
        _write(self.data / incident.NAME,
               {"risk": "sent", "symbol": "QQQx", "family": "probe",
                "pid": 999999999, "updated_ms": self.now - 1000,
                "steps": [{"step": "bridge"}], "sends": [{"kind": "swap"}]})
        found = self._collect()
        self.assertEqual([a["kind"] for a in found], [alerts.INCIDENT])
        self.assertIn("已退出", found[0]["detail"])

    def test_a_legacy_evidence_file_from_a_live_writer_is_not(self):
        """A bridge step can walk for ~26 minutes without touching the file."""
        _write(self.data / incident.NAME,
               {"risk": "sent", "symbol": "QQQx", "family": "probe",
                "pid": os.getpid(), "updated_ms": self.now - 60_000,
                "steps": [{"step": "bridge"}], "sends": [{"kind": "swap"}]})
        self.assertEqual(self._collect(), [])

    def test_a_legacy_evidence_file_that_went_quiet_for_an_hour_is_an_alert(self):
        _write(self.data / incident.NAME,
               {"risk": "sent", "symbol": "QQQx", "family": "probe",
                "pid": os.getpid(),
                "updated_ms": self.now - int(alerts.INCIDENT_ABANDONED_S * 1000) - 1000,
                "steps": [{"step": "bridge"}], "sends": [{"kind": "swap"}]})
        found = self._collect()
        self.assertEqual([a["kind"] for a in found], [alerts.INCIDENT])

    def test_unreadable_wallets_need_a_threshold(self):
        for _ in range(alerts.WALLET_UNREADABLE_MIN - 1):
            self._row(reason="wallet_unreadable")
        self.assertEqual(self._collect(), [])
        self._row(reason="wallet_unreadable")
        found = self._collect()
        self.assertEqual([a["kind"] for a in found], [alerts.WALLET_UNREADABLE])
        self.assertEqual(found[0]["count"], alerts.WALLET_UNREADABLE_MIN)

    def test_old_unreadable_rows_do_not_count(self):
        for _ in range(alerts.WALLET_UNREADABLE_MIN + 2):
            self._row(reason="wallet_unreadable",
                      ts_ms=self.now - int(alerts.WALLET_WINDOW_S * 1000) - 60_000)
        self.assertEqual(self._collect(), [])

    def test_a_stale_runner_is_an_alert(self):
        _write(self.data / "xstocks_v2_live.json",
               {"updated_ms": self.now - int(alerts.RUNNER_STALE_S * 1000) - 1000})
        found = self._collect()
        self.assertEqual([a["kind"] for a in found], [alerts.RUNNER_STALE])

    def test_the_list_is_sorted_loudest_first(self):
        cycle.halt_paths(self.data)["halt"].write_text("halt\n")
        self._row(state=ledger.STOPPED, held_at="kraken")
        found = self._collect()
        self.assertEqual([a["severity"] for a in found], ["stop", "hold"])


class FingerprintTest(unittest.TestCase):

    def test_the_same_problem_keeps_one_fingerprint(self):
        one = {"kind": alerts.HELD, "symbol": "QQQx", "family": "probe"}
        two = {"kind": alerts.HELD, "symbol": "QQQx", "family": "probe",
               "detail": "a different message"}
        self.assertEqual(alerts.fingerprint(one), alerts.fingerprint(two))

    def test_a_different_symbol_is_a_different_problem(self):
        one = {"kind": alerts.HELD, "symbol": "QQQx", "family": "probe"}
        two = {"kind": alerts.HELD, "symbol": "NVDAx", "family": "probe"}
        self.assertNotEqual(alerts.fingerprint(one), alerts.fingerprint(two))

    def test_every_collected_alert_carries_one(self):
        with TemporaryDirectory() as tmp:
            data = Path(tmp) / "data-v2"
            data.mkdir(parents=True)
            now = int(time.time() * 1000)
            _fresh_runner_state(data, now)
            cycle.halt_paths(data)["halt"].write_text("halt\n")
            for alert in alerts.collect(data_dir=data, root=Path(tmp), now_ms=now):
                self.assertTrue(alert.get("fingerprint"))


if __name__ == "__main__":
    unittest.main()
