"""The investigation launcher, without a model and without a network.

The two things that must never drift:

* the command stays **read-only** -- the sandbox flag is on the argv, so the
  promise does not depend on the model reading the prompt;
* one problem is investigated once, not once per watcher tick. A watcher that
  re-investigates a broken RPC every 30 seconds spends the operator's quota on
  one fault, and the fault is exactly what makes it tick fast.
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from arb_monitor.xstocks_v2 import investigate as inv


ALERT = {"kind": "held_position", "severity": "hold", "symbol": "QQQx",
         "family": "probe", "detail": "QQQx unknown @ unknown",
         "fingerprint": "abc123", "path": "/tmp/x"}


class _FakeProcess:

    def __init__(self, pid: int):
        self.pid = pid


class _RecordedPopen:
    """Stands in for ``subprocess.Popen``: proves the argv, starts nothing."""

    calls: list = []

    def __init__(self, argv, **kwargs):
        _RecordedPopen.calls.append({"argv": list(argv), "kwargs": kwargs})
        self.pid = 4242


class CommandTest(unittest.TestCase):

    def test_the_sandbox_is_read_only_on_the_command_line(self):
        argv = inv.command(alert=ALERT, report=Path("/tmp/r.md"),
                           repo_root="/repo", settings={"binary": "codex"})
        self.assertIn("-s", argv)
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertIn("--skip-git-repo-check", argv)
        self.assertEqual(argv[argv.index("-C") + 1], "/repo")
        self.assertEqual(argv[argv.index("-o") + 1], "/tmp/r.md")
        self.assertEqual(argv[0], "codex")

    def test_no_write_or_approval_flag_ever_appears(self):
        argv = inv.command(alert=ALERT, report=Path("/tmp/r.md"),
                           repo_root="/repo", settings={})
        for forbidden in ("workspace-write", "danger-full-access",
                          "--dangerously-bypass-approvals-and-sandbox",
                          "--approve-for-me"):
            self.assertNotIn(forbidden, argv)

    def test_a_model_override_is_passed_through_when_set(self):
        argv = inv.command(alert=ALERT, report=Path("/tmp/r.md"), repo_root="/r",
                           settings={"binary": "codex", "model": "gpt-5-codex"})
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-5-codex")


class PromptTest(unittest.TestCase):

    def test_the_prompt_names_the_alert_and_the_evidence(self):
        with TemporaryDirectory() as tmp:
            text = inv.prompt(alert=ALERT, data_dir=tmp,
                              evidence={"risk": "sent", "state": "halted",
                                        "sends": [{"kind": "onchainos:swap execute"}]},
                              now_ms=1_700_000_000_000)
            self.assertIn("QQQx", text)
            self.assertIn("held_position", text)
            self.assertIn("onchainos:swap execute", text)
            self.assertIn("只读", text)

    def test_the_prompt_forbids_the_switches_by_name(self):
        text = inv.prompt(alert=ALERT, evidence={})
        for forbidden in ("KILL_SWITCH", "AUTO_HALT", "xstocks_v2_HALT",
                          "live_ack"):
            self.assertIn(forbidden, text)

    def test_the_prompt_asks_the_four_questions(self):
        text = inv.prompt(alert=ALERT, evidence={})
        self.assertIn("真钱", text)          # 1. did money move?
        self.assertIn("根因", text)          # 2. why
        self.assertIn("证据", text)          # 3. how do we know
        self.assertIn("要人做什么", text)     # 4. what does the operator do


class DecideTest(unittest.TestCase):

    def setUp(self):
        self.now = int(time.time() * 1000)
        # ``decide`` refuses when the agent binary is not on this machine, so
        # the fixture has to name a binary that really exists here. A bare
        # ``"codex"`` made this suite pass on the trading Mac and fail on a CI
        # runner that has no such command (2026-10-10, main's CI had been red
        # since 2026-10-09). The missing-binary case has its own test below.
        self.settings = {"enabled": True, "max_per_day": 2, "cooldown_s": 300,
                         "binary": sys.executable}

    def _state(self, **over):
        state = {"day": time.strftime("%Y-%m-%d", time.localtime(self.now / 1000)),
                 "launched_today": 0, "seen": {}, "running": [], "done": []}
        state.update(over)
        return state

    def test_a_new_problem_may_launch(self):
        may, reason = inv.decide(alert=ALERT, state=self._state(),
                                 now_ms=self.now, settings=self.settings)
        self.assertTrue(may, reason)

    def test_the_cooldown_holds_the_same_problem_back(self):
        state = self._state(seen={"abc123": {"count": 1, "last_ms": self.now - 1000}})
        may, reason = inv.decide(alert=ALERT, state=state, now_ms=self.now,
                                 settings=self.settings)
        self.assertFalse(may)
        self.assertTrue(reason.startswith("cooldown"))

    def test_the_cooldown_expires(self):
        state = self._state(seen={"abc123": {"count": 1,
                                             "last_ms": self.now - 400_000}})
        may, _ = inv.decide(alert=ALERT, state=state, now_ms=self.now,
                            settings=self.settings)
        self.assertTrue(may)

    def test_a_repeat_that_keeps_failing_backs_off(self):
        """One stuck HALT must not eat the day's budget."""
        waits = [inv.cooldown_for({"count": n}, self.settings)
                 for n in range(1, 11)]
        self.assertEqual(waits[0], 300)
        self.assertEqual(waits[1], 600)
        self.assertEqual(waits[2], 1200)
        self.assertEqual(waits[-1], inv.DEFAULT_MAX_COOLDOWN_S)   # ceiling at 10
        self.assertEqual(inv.cooldown_for({"count": 99}, self.settings),
                         inv.DEFAULT_MAX_COOLDOWN_S)
        self.assertTrue(all(b >= a for a, b in zip(waits, waits[1:])))

    def test_the_backoff_is_what_the_second_look_uses(self):
        state = self._state(seen={"abc123": {"count": 2, "last_ms": self.now - 400_000}})
        may, reason = inv.decide(alert=ALERT, state=state, now_ms=self.now,
                                 settings=self.settings)
        self.assertFalse(may)
        self.assertIn("/600s", reason)

    def test_the_daily_cap_is_hard(self):
        state = self._state(launched_today=2)
        may, reason = inv.decide(alert=ALERT, state=state, now_ms=self.now,
                                 settings=self.settings)
        self.assertFalse(may)
        self.assertEqual(reason, "daily_cap")

    def test_the_counter_resets_on_a_new_day(self):
        state = self._state(day="2000-01-01", launched_today=99)
        may, _ = inv.decide(alert=ALERT, state=state, now_ms=self.now,
                            settings=self.settings)
        self.assertTrue(may)
        self.assertEqual(state["launched_today"], 0)

    def test_the_same_problem_is_not_investigated_twice_at_once(self):
        state = self._state(running=[{"fingerprint": "abc123", "pid": 1}])
        may, reason = inv.decide(alert=ALERT, state=state, now_ms=self.now,
                                 settings=self.settings)
        self.assertFalse(may)
        self.assertEqual(reason, "already_running")

    def test_a_missing_binary_is_a_refusal_not_a_crash(self):
        may, reason = inv.decide(alert=ALERT, state=self._state(),
                                 now_ms=self.now,
                                 settings=dict(self.settings,
                                               binary="/nonexistent/codex"))
        self.assertFalse(may)
        self.assertEqual(reason, "binary_missing")

    def test_disabled_means_disabled(self):
        may, reason = inv.decide(alert=ALERT, state=self._state(),
                                 now_ms=self.now,
                                 settings=dict(self.settings, enabled=False))
        self.assertFalse(may)
        self.assertEqual(reason, "disabled")


class LaunchTest(unittest.TestCase):

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name)
        self.now = int(time.time() * 1000)
        _RecordedPopen.calls = []

    def _state(self):
        return {"day": time.strftime("%Y-%m-%d", time.localtime(self.now / 1000)),
                "launched_today": 0, "seen": {}, "running": [], "done": []}

    def test_launch_starts_a_detached_process_and_writes_the_bookkeeping(self):
        state = self._state()
        record = inv.launch(alert=ALERT, state=state, now_ms=self.now,
                            settings={"binary": "codex", "sandbox": "read-only"},
                            data_dir=self.data, repo_root=self.data,
                            popen=_RecordedPopen)
        self.assertTrue(record["ok"])
        self.assertEqual(record["pid"], 4242)
        self.assertEqual(state["launched_today"], 1)
        self.assertEqual(len(state["running"]), 1)
        call = _RecordedPopen.calls[0]
        self.assertTrue(call["kwargs"]["start_new_session"])
        self.assertEqual(call["kwargs"]["cwd"], str(self.data))
        self.assertIn("read-only", call["argv"])
        # the alert is remembered, so a second tick does not re-launch it
        self.assertIn("abc123", state["seen"])
        # ... and the index gets one line, for a human reading the directory
        index = (self.data / inv.DIR_NAME / inv.INDEX_NAME).read_text().splitlines()
        self.assertEqual(len(index), 1)
        self.assertEqual(json.loads(index[0])["symbol"], "QQQx")

    def test_a_failed_spawn_is_recorded_rather_than_raised(self):
        def boom(*_args, **_kwargs):
            raise OSError("no such file")

        state = self._state()
        record = inv.launch(alert=ALERT, state=state, now_ms=self.now,
                            settings={"binary": "codex"}, data_dir=self.data,
                            repo_root=self.data, popen=boom)
        self.assertFalse(record["ok"])
        self.assertIn("no such file", record["error"])
        self.assertEqual(state["launched_today"], 0)   # nothing was started
        self.assertEqual(len(state["running"]), 0)
        self.assertEqual(len(state["done"]), 1)

    def test_reap_moves_a_dead_process_out_of_running(self):
        state = self._state()
        inv.launch(alert=ALERT, state=state, now_ms=self.now,
                   settings={"binary": "codex"}, data_dir=self.data,
                   repo_root=self.data, popen=_RecordedPopen)
        report = Path(state["running"][0]["report"])
        report.write_text("1. 一句话结论：没有真钱动过。\n2. 根因：读余额失败。\n")
        finished = inv.reap(state=state, now_ms=self.now + 60_000,
                            settings={"timeout_s": 1}, data_dir=self.data)
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["outcome"], "finished")
        self.assertIn("没有真钱动过", finished[0]["summary"])
        self.assertEqual(state["running"], [])
        self.assertEqual(len(state["done"]), 1)
        self.assertIn("没有真钱动过", inv.format_finished(finished[0]))

    def test_state_round_trips_through_the_file(self):
        state = self._state()
        state["seen"]["abc123"] = {"count": 3, "last_ms": self.now}
        inv.state_save(state, self.data)
        loaded = inv.state_load(self.data)
        self.assertEqual(loaded["seen"]["abc123"]["count"], 3)
        self.assertEqual(loaded["running"], [])

    def test_a_report_that_is_not_there_yet_is_an_empty_summary(self):
        self.assertEqual(inv.report_summary(self.data / "nope.md"), "")


class SettingsTest(unittest.TestCase):

    def test_defaults_are_on_and_bounded(self):
        found = inv.settings(env={})
        self.assertTrue(found["enabled"])
        self.assertEqual(found["sandbox"], "read-only")
        self.assertGreater(found["max_per_day"], 0)
        self.assertGreater(found["cooldown_s"], 0)

    def test_the_environment_can_turn_it_off(self):
        self.assertFalse(inv.settings(env={"ARB_INVESTIGATE_ENABLED": "0"})["enabled"])

    def test_a_broken_number_falls_back_to_the_default(self):
        found = inv.settings(env={"ARB_INVESTIGATE_MAX_PER_DAY": "many"})
        self.assertEqual(found["max_per_day"], inv.DEFAULT_MAX_PER_DAY)


if __name__ == "__main__":
    unittest.main()
