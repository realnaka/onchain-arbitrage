"""A51: read failure and money movement must not share one outcome.

Measured 2026-10-09 10:43:31: a QQQx probe died in ``usdg_balance`` -- reading
a wallet balance, before the first step of the plan -- and the machine wrote
``state=unknown`` plus ``xstocks_v2_HALT``. It then stood still for two hours
while the board listed QQQx as an open position that needed a human, at an
X Layer wallet and a Solana wallet that both held zero QQQx.

These tests pin the two halves of the fix:

* a crash before any money-moving step is a **refusal** -- no HALT, no lock, and
  the symbol does not appear in ``open_positions``;
* once a step that broadcasts, or a real send, has started, the answer stays
  **unknown** + HALT, because there the honest answer is "we do not know".
"""
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from arb_monitor.xstocks_v2 import executor as ex
from arb_monitor.xstocks_v2 import incident, ledger

ROOT = Path(__file__).resolve().parents[1]


def _live_module():
    spec = importlib.util.spec_from_file_location(
        "v2live_incident", ROOT / "scripts" / "xstocks_v2_live.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _SslLikeFailure(OSError):
    """The shape of the 10:43 crash: an RPC read that did not answer."""


class RiskLevelTest(unittest.TestCase):
    """``none`` -> ``step`` -> ``sent``, and never back down."""

    def setUp(self):
        # Redirected on purpose: ``incident`` has a real default data dir, and a
        # test that writes there leaves the live process's evidence file holding
        # a fake attempt (found 2026-10-09 the moment the file was inspected).
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        incident.begin(family="probe", symbol="QQQx",
                       data_dir=self._tmp.name)

    def test_a_fresh_attempt_is_none(self):
        self.assertEqual(incident.risk(), incident.NONE)

    def test_entering_a_broadcasting_step_raises_the_level(self):
        incident.enter_step("buy")
        self.assertEqual(incident.risk(), incident.STEP)

    def test_a_send_outranks_a_step_and_never_goes_back(self):
        incident.enter_step("bridge")
        incident.sent("onchainos:wallet contract-call", "0xdead")
        self.assertEqual(incident.risk(), incident.SENT)
        incident.enter_step("sell")            # a later step must not lower it
        self.assertEqual(incident.risk(), incident.SENT)
        self.assertEqual(len(incident.snapshot()["sends"]), 1)

    def test_the_snapshot_carries_what_an_investigator_needs(self):
        incident.enter_step("bridge")
        incident.sent("onchainos:wallet contract-call", "bridge x-layer -> solana")
        snapshot = incident.snapshot()
        self.assertEqual(snapshot["family"], "probe")
        self.assertEqual(snapshot["symbol"], "QQQx")
        self.assertEqual([row["step"] for row in snapshot["steps"]], ["bridge"])
        self.assertEqual(snapshot["sends"][0]["kind"],
                         "onchainos:wallet contract-call")

    def test_the_evidence_file_lands_in_the_data_dir_it_was_given(self):
        """The default is the live tree; a caller that passes one must win."""
        self.assertEqual(incident.path(self._tmp.name).parent,
                         Path(self._tmp.name))
        self.assertTrue(incident.path(self._tmp.name).exists())


class GuardFamilyClassificationTest(unittest.TestCase):
    """The outer guard: crash before money = refused, after = unknown."""

    def test_a_read_that_dies_before_the_first_step_is_a_refusal(self):
        """The 10:43 regression, end to end."""
        module = _live_module()

        def family():
            raise _SslLikeFailure(
                "HTTPSConnectionPool(host='rpc.xlayer.tech', port=443): "
                "Max retries exceeded (Caused by SSLEOFError)")

        with TemporaryDirectory() as tmp:
            summary, crashed = module.guard_family(
                "probe", "QQQx", family, data_dir=tmp)
            path = Path(tmp) / "xstocks_v2_cycles.jsonl"
            self.assertTrue(crashed)
            self.assertEqual(summary["state"], ledger.REFUSED)
            self.assertEqual(summary["held_at"], "")
            self.assertTrue(summary["reason"].startswith("crash_before_money:"))
            # No HALT: the next round may try again, nothing has to be cleared.
            self.assertFalse((Path(tmp) / "xstocks_v2_HALT").exists())
            # ... and the board's "open positions" stays empty for this symbol.
            self.assertEqual([row["symbol"] for row in ledger.open_positions(path)],
                             [])
            written = ledger.recent(path, limit=1)[0]
            self.assertEqual(written["state"], ledger.REFUSED)
            self.assertEqual(written["extra"]["incident"]["risk"], incident.NONE)

    def test_a_crash_inside_a_broadcasting_step_still_halts(self):
        module = _live_module()

        def family():
            incident.enter_step("bridge")
            raise _SslLikeFailure("wallet backend did not answer")

        with TemporaryDirectory() as tmp:
            summary, _ = module.guard_family("probe", "QQQx", family,
                                             data_dir=tmp)
            self.assertEqual(summary["state"], ledger.UNKNOWN)
            self.assertEqual(summary["held_at"], "unknown")
            self.assertTrue(summary["reason"].startswith("crash:"))
            self.assertTrue((Path(tmp) / "xstocks_v2_HALT").exists())
            path = Path(tmp) / "xstocks_v2_cycles.jsonl"
            self.assertEqual([row["symbol"] for row in ledger.open_positions(path)],
                             ["QQQx"])

    def test_a_crash_after_a_send_records_the_send(self):
        module = _live_module()

        def family():
            incident.enter_step("buy")
            incident.sent("onchainos:swap execute", "buy QQQx on xlayer")
            raise _SslLikeFailure("read back failed")

        with TemporaryDirectory() as tmp:
            _, _ = module.guard_family("probe", "QQQx", family, data_dir=tmp)
            written = ledger.recent(Path(tmp) / "xstocks_v2_cycles.jsonl",
                                    limit=1)[0]
            self.assertEqual(written["state"], ledger.UNKNOWN)
            self.assertEqual(written["extra"]["incident"]["risk"], incident.SENT)
            self.assertEqual(written["extra"]["incident"]["sends"][0]["kind"],
                             "onchainos:swap execute")

    def test_a_clean_family_reads_held_at_from_the_ledger_not_from_a_guess(self):
        module = _live_module()

        def family(tmp):
            # An old-style probe row: the in-flight worst case was recorded,
            # the arrival was not. ``effective_held_at`` is what must decide.
            row = ledger.row(symbol="NVDAx", chain="x-layer", family="probe",
                             state=ledger.PROBED, mode="live", size_usdt="1.54",
                             held_at="in_bridge")
            ledger.record(row, Path(tmp) / "xstocks_v2_cycles.jsonl")
            return {"symbol": "NVDAx", "exit_code": 0, "state": "probed"}

        with TemporaryDirectory() as tmp:
            summary, crashed = module.guard_family(
                "probe", "NVDAx", lambda: family(tmp), data_dir=tmp)
            self.assertFalse(crashed)
            self.assertEqual(summary["state"], "probed")
            self.assertEqual(summary["held_at"], "dest_wallet")
            self.assertEqual(summary["crashed"], False)

    def test_a_family_row_from_an_earlier_cycle_is_not_borrowed(self):
        """The window: a stale row of the same symbol must not answer for this."""
        module = _live_module()

        def family(tmp):
            old = ledger.row(symbol="NVDAx", chain="x-layer", family="probe",
                             state=ledger.PROBED, mode="live", size_usdt="1",
                             held_at="dest_wallet")
            old["ts_ms"] = 1_000_000            # long before this call started
            ledger.record(old, Path(tmp) / "xstocks_v2_cycles.jsonl")
            return {"symbol": "NVDAx", "exit_code": 0, "state": "probed"}

        with TemporaryDirectory() as tmp:
            summary, _ = module.guard_family("probe", "NVDAx",
                                             lambda: family(tmp), data_dir=tmp)
            self.assertEqual(summary["held_at"], "")


class ExecutorMarksTheStepTest(unittest.TestCase):
    """The executor is where every family inherits the marker."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_a_broadcasting_step_marks_the_attempt_before_it_runs(self):
        seen: dict = {}

        def run():
            seen["risk"] = incident.risk()
            return ex.Outcome(ex.REFUSED, "not today")

        machine = ex.Executor(
            symbol="QQQx", chain="x-layer", family="probe", mode="live",
            size_usdt="2", tokens="0.001",
            steps=[ex.Step("buy", run, held_at="dest_wallet", broadcasts=True)],
            gate=lambda: (True, "ok"),
            ledger_path=Path(self._tmp.name) / "cycles.jsonl")
        incident.begin(family="probe", symbol="QQQx", data_dir=self._tmp.name)
        machine.run()
        self.assertEqual(seen["risk"], incident.STEP)

    def test_a_step_that_does_not_broadcast_leaves_the_level_alone(self):
        seen: dict = {}

        def run():
            seen["risk"] = incident.risk()
            return ex.Outcome(ex.REFUSED, "not today")

        machine = ex.Executor(
            symbol="QQQx", chain="x-layer", family="probe", mode="live",
            size_usdt="2", tokens="0.001",
            steps=[ex.Step("read", run, broadcasts=False)],
            gate=lambda: (True, "ok"),
            ledger_path=Path(self._tmp.name) / "cycles.jsonl")
        incident.begin(family="probe", symbol="QQQx", data_dir=self._tmp.name)
        machine.run()
        self.assertEqual(seen["risk"], incident.NONE)


class ProbeBudgetChargeTest(unittest.TestCase):
    """The daily probe cap must be charged on what was spent.

    Measured 2026-10-09 12:28: a sample probe bought 1.43124 USDG of QQQx on
    X Layer, the bridge dry-run reverted, the cycle ended ``stopped`` -- and the
    day's counter stayed at 0. The cap bounds a day's spend, so it has to count
    a spend that happened, not a walk that finished neatly.
    """

    def setUp(self):
        self.module = _live_module()

    def test_a_finished_probe_is_charged(self):
        self.assertTrue(self.module.probe_spent_money(
            {"state": "probed"}, incident.STEP))

    def test_a_probe_that_stopped_after_sending_is_charged(self):
        self.assertTrue(self.module.probe_spent_money(
            {"state": "stopped"}, incident.SENT))

    def test_a_probe_that_died_before_sending_is_not_charged(self):
        self.assertFalse(self.module.probe_spent_money(
            {"state": "refused"}, incident.STEP))
        self.assertFalse(self.module.probe_spent_money(
            {"state": "stopped"}, incident.NONE))


class NoHardcodedHeldAtTest(unittest.TestCase):
    """A47's rule, applied to the executed list: report the ledger, not a wish.

    ``xstocks_v2_live.py`` used to append ``{"held_at": "dest_wallet"}`` for a
    probe and ``{"held_at": ""}`` for everything else, whatever the ledger said.
    2026-10-09: the probe crashed before the buy, the ledger wrote
    ``held_at=unknown``, and the run log told the operator the coins were in the
    destination wallet. Exactly backwards, in the one line a human reads first.
    """

    def test_the_executed_rows_do_not_invent_held_at(self):
        source = (ROOT / "scripts" / "xstocks_v2_live.py").read_text()
        self.assertNotIn('"held_at": "dest_wallet"', source)
        self.assertNotIn('"reason": "", "held_at": ""', source)
        self.assertNotIn('"state": summary["state"], "reason": "", "held_at": ""',
                         source)

    def test_every_executed_row_takes_held_at_from_the_summary(self):
        source = (ROOT / "scripts" / "xstocks_v2_live.py").read_text()
        # Every site that appends a **dict built from a summary** must take
        # ``held_at`` from that summary. The count is derived rather than
        # pinned: families keep landing (6 -> 8 -> 10 on 2026-10-09), and a
        # frozen number turns each new family into a red test instead of a
        # check. The one append that carries a ledger ``record`` is not counted.
        self.assertEqual(source.count('"held_at": summary.get("held_at", "")'),
                         source.count("executed.append({"))


class UnknownLegKeepsItsEvidenceTest(unittest.TestCase):
    """The 2026-10-10 14:17 stop: the answer stayed honest, the evidence did not.

    The Ethereum probe's buy step returned ``ok`` with a txHash. The verifier
    waited a minute: the token balance had not moved and ``wallet history`` had
    never heard of the hash. The leg ended ``unknown`` and the global HALT
    stopped every chain for nine hours.

    Reconciling that stop meant reading the chain by hand -- because the hash
    was recorded nowhere. No ``ok`` row is written for a leg whose verify
    failed, the finish row kept only ``step=finish``, and the Ethereum buy leg
    never called ``incident.sent`` (the deposit leg does), so the incident file
    answered ``sends: []`` for a broadcast that may have gone out.

    The state stays ``unknown``: a hash the chain may still see is not a
    refusal, and silently retrying it could buy the same slice twice. What
    must change is that the answer carries its own evidence.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        incident.begin(family="eth_sample", symbol="MSTRx",
                       data_dir=self.data_dir)

    def _machine(self, ledger_path: Path, **kwargs) -> ex.Executor:
        return ex.Executor(
            symbol="MSTRx", chain="eth", family="chain_to_kraken", mode="live",
            size_usdt="2", tokens="0.0081",
            gate=lambda: (True, "ok"), ledger_path=ledger_path,
            halt_path=self.data_dir / "HALT", **kwargs)

    def _rows(self, ledger_path: Path) -> list[dict]:
        return [json.loads(line) for line in
                ledger_path.read_text().splitlines() if line.strip()]

    def test_the_hash_of_an_unknown_leg_reaches_the_ledger_row(self):
        hash_ = "0x" + "ab" * 32
        ledger_path = self.data_dir / "cycles.jsonl"

        def run():
            return ex.Outcome(ex.OK, "", held_at="wallet",
                              observed={"tx": hash_, "nonce_before": 77})

        def verify(outcome):
            return ex.Outcome(ex.UNKNOWN, "buy status=unknown, balance unchanged",
                              held_at="wallet")

        machine = self._machine(ledger_path, steps=[
            ex.Step("buy", run, verify=verify, held_at="wallet",
                    broadcasts=True)])
        result = machine.run()

        self.assertEqual(result["state"], ex.UNKNOWN)
        self.assertTrue(result["halt"])
        self.assertTrue(result["keep_lock"])
        finish = self._rows(ledger_path)[-1]
        self.assertEqual(finish["state"], ex.UNKNOWN)
        self.assertEqual(finish["extra"]["observed"]["tx"], hash_)
        self.assertEqual(finish["extra"]["observed"]["nonce_before"], 77)

    def test_a_leg_that_saw_nothing_does_not_invent_an_observed_block(self):
        ledger_path = self.data_dir / "cycles.jsonl"
        machine = self._machine(ledger_path, steps=[
            ex.Step("buy", lambda: ex.Outcome(ex.REFUSED, "gas_short:1<2"),
                    held_at="wallet", broadcasts=True)])
        result = machine.run()
        self.assertEqual(result["state"], ex.REFUSED)
        self.assertNotIn("observed", self._rows(ledger_path)[-1]["extra"])

    def test_the_eth_buy_leg_names_its_send_before_it_broadcasts(self):
        """A broadcast step that never says so leaves ``sends: []`` behind."""
        source = (ROOT / "scripts" / "xstocks_v2_eth_roundtrip.py").read_text()
        buy = source.split("def buy_runner(")[1].split("def buy_verifier(")[0]
        self.assertIn('incident.sent("eth_swap"', buy)
        self.assertLess(buy.index('incident.sent("eth_swap"'),
                        buy.index("broadcast_with_retry(tx[\"to\"]"))


if __name__ == "__main__":
    unittest.main()
