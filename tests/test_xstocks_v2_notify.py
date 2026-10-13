"""Hermetic tests for v2 alerting: a HALT must reach a human."""
from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from arb_monitor.xstocks_v2 import executor as ex
from arb_monitor.xstocks_v2 import notify as v2notify


class SettingsTest(unittest.TestCase):
    def test_channels_come_from_the_two_webhook_env_names(self):
        cfg = v2notify.settings({"ARB_FEISHU_WEBHOOK": "a",
                                 "ARB_FEISHU_ALERT_WEBHOOK": "b"})
        self.assertEqual(cfg["webhook_url"], "a")
        self.assertEqual(cfg["alert_webhook_url"], "b")
        self.assertEqual(cfg["channel"], "webhook")

    def test_a_broken_sender_never_raises(self):
        class Boom:
            def send(self, text):
                raise RuntimeError("network down")

        send = v2notify.sender(factory=lambda *a, **k: Boom())
        result = send("hi")
        self.assertFalse(result["ok"])
        self.assertIn("network down", result["error"])

    def test_a_configured_sender_is_used(self):
        seen = []

        class Ok:
            def send(self, text):
                seen.append(text)
                return {"ok": True}

        send = v2notify.sender(factory=lambda *a, **k: Ok())
        self.assertTrue(send("hello")["ok"])
        self.assertEqual(seen, ["hello"])

    def test_the_message_says_where_the_coins_are(self):
        text = v2notify.format_hold(symbol="SPYx", family="kraken_via_bridge_to_chain",
                                    state=ex.UNKNOWN, held_at="in_bridge",
                                    reason="bridge: no receipt")
        self.assertIn("SPYx", text)
        self.assertIn("币在桥上", text)
        self.assertIn("xstocks_v2_HALT", text)


class ExecutorAlertTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.seen: list[dict] = []

    def _executor(self, steps):
        return ex.Executor(
            symbol="SPYx", chain="x-layer", family="kraken_via_bridge_to_chain",
            mode="live", size_usdt=D("7.8"), tokens=D("0.01"), steps=steps,
            gate=lambda: (True, "ok"),
            ledger_path=self.dir / "cycles.jsonl",
            halt_path=self.dir / "HALT",
            notify=lambda **kwargs: self.seen.append(kwargs))

    def test_unknown_and_stopped_alert(self):
        for status, held in ((ex.UNKNOWN, "in_bridge"), (ex.STOPPED, "dest_wallet")):
            self.seen.clear()
            self._executor([ex.Step("x", lambda s=status: ex.Outcome(s, "boom"),
                                    held_at=held, broadcasts=True)]).run()
            self.assertEqual(len(self.seen), 1)
            self.assertEqual(self.seen[0]["held_at"], held)

    def test_a_clean_sale_does_not_alert(self):
        self._executor([ex.Step("x", lambda: ex.Outcome(ex.OK), broadcasts=True)]).run()
        self.assertEqual(self.seen, [])

    def test_a_refusal_does_not_alert(self):
        self._executor([ex.Step("x", lambda: ex.Outcome(ex.REFUSED, "below_min"),
                                broadcasts=True)]).run()
        self.assertEqual(self.seen, [])

    def test_a_broken_notifier_does_not_break_the_cycle(self):
        machine = ex.Executor(
            symbol="SPYx", chain="x-layer", family="f", mode="live",
            size_usdt=D("1"), tokens=D("1"),
            steps=[ex.Step("x", lambda: ex.Outcome(ex.UNKNOWN, "boom"),
                           held_at="wallet", broadcasts=True)],
            gate=lambda: (True, "ok"), ledger_path=self.dir / "l.jsonl",
            halt_path=self.dir / "HALT",
            notify=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("no net")))
        result = machine.run()
        self.assertEqual(result["state"], ex.UNKNOWN)
        self.assertTrue(result["keep_lock"])


if __name__ == "__main__":
    unittest.main()
