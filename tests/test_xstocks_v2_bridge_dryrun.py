"""The bridge leg must dry-run before it broadcasts, and refuse if it reverts.

Measured 2026-10-07: the only ``eth_call`` on this path ran in the ``once``
preflight, i.e. *before* the approve step, so it reverted with ``ERC20:
insufficient allowance`` on every single run and the preflight ignored it. The
one leg that moves the money across chains therefore went out un-simulated.
"""
from __future__ import annotations

import importlib.util
import json
import re
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from arb_monitor.xstocks_v2 import ccip_evm as EVM
from arb_monitor.xstocks_v2 import executor as ex, ledger

# The exact payload the X Layer RPC returned on 2026-10-07 (twice, verbatim).
ALLOWANCE_REVERT_DATA = (
    "0x08c379a0"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "000000000000000000000000000000000000000000000000000000000000001d"
    "45524332303a20696e73756666696369656e7420616c6c6f77616e6365000000")
ALLOWANCE_REVERT_ERROR = {
    "code": 3,
    "message": "execution reverted: ERC20: insufficient allowance",
    "data": ALLOWANCE_REVERT_DATA,
}


class RevertDecodeTest(unittest.TestCase):
    def test_the_real_allowance_revert_decodes_to_its_text(self):
        self.assertEqual(EVM.decode_revert_reason(ALLOWANCE_REVERT_ERROR),
                         "ERC20: insufficient allowance")

    def test_an_error_without_data_falls_back_to_the_node_message(self):
        self.assertEqual(
            EVM.decode_revert_reason({"code": 3, "message": "out of gas"}),
            "out of gas")

    def test_a_custom_error_is_not_forced_into_a_string(self):
        payload = {"code": 3, "message": "execution reverted",
                   "data": "0xdeadbeef"}
        self.assertEqual(EVM.decode_revert_reason(payload), "execution reverted")
        self.assertEqual(EVM.decode_revert_reason(None), "")


def _roundtrip():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "v2sol", root / "scripts" / "xstocks_v2_solana_roundtrip.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Session:
    """The tiniest stand-in for ``Http.session`` the script uses."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, json=None, timeout=None):  # noqa: A002 - requests' name
        self.calls.append(json)
        outer = self

        class _Answer:
            def json(self):
                return outer.payload

        return _Answer()


class _Probe:
    def __init__(self, held=D("0.011")):
        self.held = held

    def xlayer_balance(self, http, token):
        return self.held

    def solana_balance(self, http, mint):
        return 12345


class DryRunTest(unittest.TestCase):
    def setUp(self):
        self.runner = _roundtrip()
        self.state: dict = {}

    def _ctx(self, payload):
        return {"probe": _Probe(), "http": type("H", (), {"session": _Session(payload)})(),
                "product": {"symbol": "SPYx", "mint": "MINT",
                            "evm_xlayer": "0x90a2a4c76b5d8c0bc892a69ea28aa775a8f2dd48"}}

    def test_simulate_outcome_reads_the_revert_text(self):
        ok, why = self.runner.simulate_outcome(
            self._ctx({"error": ALLOWANCE_REVERT_ERROR}), "0xdead",
            791242725185203)
        self.assertFalse(ok)
        self.assertIn("insufficient allowance", why.lower())

    def test_the_preflight_note_does_not_call_the_expected_revert_an_error(self):
        note = self.runner.dry_run_note(ok=False,
                                        reason="ERC20: insufficient allowance")
        self.assertIn("deferred", note)
        self.assertIn("approve", note)
        self.assertNotIn("eth_call error", note)
        self.assertIn("eth_call error", self.runner.dry_run_note(
            ok=False, reason="execution reverted: out of gas"))
        self.assertIn("eth_call ok", self.runner.dry_run_note(ok=True, reason=""))

    def test_a_reverting_dry_run_refuses_and_never_broadcasts(self):
        """The broadcast is the expensive, irreversible step: no dry-run, no send."""
        calls: list[list[str]] = []
        self.runner.precheck = lambda ctx, raw, fee: ("0xdead", None, [], [])
        self.runner.solana_token_amount = lambda probe, http, mint: 0
        self.runner.simulate = lambda ctx, data, fee: (None, ALLOWANCE_REVERT_ERROR)
        self.runner.broadcast = lambda args, **kw: calls.append(args) or {
            "ok": True, "data": {"txHash": "0xshouldnotbeused"}}

        outcome = self.runner.bridge_step_runner(
            self._ctx({"error": ALLOWANCE_REVERT_ERROR}), self.state,
            791242725185203)()

        self.assertEqual(outcome.status, ex.REFUSED)
        self.assertIn("dry_run", outcome.detail)
        self.assertIn("insufficient allowance", outcome.detail.lower())
        self.assertEqual(outcome.held_at, "dest_wallet")
        self.assertEqual(calls, [], "a refused dry-run must not broadcast")
        self.assertNotIn("bridge_tx", self.state)

    def test_a_dry_run_without_a_tx_hash_is_not_a_broadcast(self):
        """``wallet contract-call`` can answer ok with no hash at all."""
        self.runner.precheck = lambda ctx, raw, fee: ("0xdead", None, [], [])
        self.runner.solana_token_amount = lambda probe, http, mint: 0
        self.runner.simulate = lambda ctx, data, fee: ("0x", None)
        self.runner.broadcast = lambda args, **kw: {"ok": True, "data": {}}

        outcome = self.runner.bridge_step_runner(
            self._ctx(None), self.state, 791242725185203)()

        self.assertEqual(outcome.status, ex.OK)
        self.assertIsNone(outcome.observed["tx"])

    def test_a_passing_dry_run_broadcasts_exactly_once(self):
        """Order: precheck -> dry-run -> broadcast, exactly once."""
        calls: list[list[str]] = []
        self.runner.precheck = lambda ctx, raw, fee: ("0xdead", None, [], [])
        self.runner.solana_token_amount = lambda probe, http, mint: 0
        self.runner.simulate = lambda ctx, data, fee: ("0x", None)

        def _broadcast(args, **kw):
            calls.append(args)
            return {"ok": True, "data": {"txHash": "0xbridge"}}

        self.runner.broadcast = _broadcast
        outcome = self.runner.bridge_step_runner(
            self._ctx(None), self.state, 791242725185203)()

        self.assertEqual(outcome.status, ex.OK)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.state["bridge_tx"], "0xbridge")
        self.assertEqual(outcome.held_at, "in_bridge")

    def test_a_dry_run_that_cannot_answer_is_a_retry_not_a_broadcast(self):
        """A dead RPC earns one retry; it never earns a broadcast."""
        calls: list[list[str]] = []
        self.runner.precheck = lambda ctx, raw, fee: ("0xdead", None, [], [])

        def _boom(ctx, data, fee):
            raise OSError("no proxy")

        self.runner.simulate = _boom
        self.runner.broadcast = lambda args, **kw: calls.append(args) or {"ok": True}

        outcome = self.runner.bridge_step_runner(
            self._ctx(None), self.state, 791242725185203)()

        self.assertEqual(outcome.status, ex.RETRY)
        self.assertIn("dry_run", outcome.detail)
        self.assertEqual(calls, [])


class SolanaGasTest(unittest.TestCase):
    """Same rule on the Solana side: check the fee payer before broadcasting.

    The Solana -> X Layer fee (0.000852448 SOL, measured twice) is paid from our
    own wallet. Without this check a wallet that cannot pay it fails *after* the
    leg was declared "in flight", which is the shape that halted the family-5 run.
    """

    def _runner(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "v2xlayer", root / "scripts" / "xstocks_v2_xlayer_roundtrip.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    class _Http:
        def __init__(self, value):
            self.value = value
            self.calls = []

        def rpc(self, url, method, params):
            self.calls.append((method, params))
            if isinstance(self.value, Exception):
                raise self.value
            return {"value": self.value}

    def test_a_wallet_that_can_pay_passes(self):
        runner = self._runner()
        http = self._Http(11_000_000)          # 0.011 SOL, the real balance
        self.assertIsNone(runner.sol_shortfall(http))
        self.assertEqual(http.calls[0][0], "getBalance")

    def test_a_short_wallet_names_both_numbers_and_refuses(self):
        runner = self._runner()
        short = runner.sol_shortfall(self._Http(100_000))   # 0.0001 SOL
        self.assertIsNotNone(short)
        self.assertTrue(short.startswith("gas_low:"), short)
        self.assertIn("0.000852448", short)

    def test_an_unreadable_balance_is_unknown_not_funded(self):
        runner = self._runner()
        for value in (None, "0.011", OSError("no proxy")):
            self.assertEqual(runner.sol_shortfall(self._Http(value)),
                             f"sol_unknown:{runner.WALLET}")


class SolanaProbeDryRunTest(unittest.TestCase):
    """The same rule on the Solana side of the probe: no dry-run, no send."""

    def _probe(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "v2ccipprobe", root / "scripts" / "xstocks_v2_ccip_solana_probe.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_a_passing_simulation_is_no_refusal(self):
        probe = self._probe()
        self.assertEqual(probe.simulation_refusal({"err": None}), "")
        self.assertEqual(probe.simulation_refusal({}), "")

    def test_a_failing_simulation_names_the_reason(self):
        probe = self._probe()
        # A structured error is kept verbatim: the operator needs the program
        # error, not a summary that loses which instruction failed.
        self.assertEqual(
            probe.simulation_refusal(
                {"err": {"InstructionError": [0, {"Custom": 0}]}}),
            '{"InstructionError": [0, {"Custom": 0}]}')
        self.assertEqual(probe.simulation_refusal({"err": "AccountNotFound"}),
                         "AccountNotFound")


class GasShortfallTest(unittest.TestCase):
    """The fee is paid in the source chain's gas token, and it is checked first.

    Measured on the live chain 2026-10-07 22:42: the dry-run answered
    ``insufficient funds for gas * price + value: have 605621410468124 want
    791242725185203`` -- the wallet had 0.0006056 OKB and the message needed
    0.000791. Reading the balance turns that into a refusal *before* the buy leg.
    """

    FEE = 791242725185203          # 0.000791242725185203 OKB, measured twice

    def setUp(self):
        self.runner = _roundtrip()

    def _http(self, balance_hex):
        return type("H", (), {"session": _Session({"result": balance_hex})})()

    def test_the_measured_shortfall_is_a_refusal_with_both_numbers(self):
        short = self.runner.gas_shortfall(
            self._http(hex(605621410468124)), fee_wei=self.FEE)
        self.assertIsNotNone(short)
        self.assertTrue(short.startswith("gas_low:"))
        self.assertIn("605621410468124", short)
        self.assertIn("791242725185203", short)

    def test_a_wallet_that_can_pay_the_fee_passes(self):
        self.assertIsNone(self.runner.gas_shortfall(
            self._http(hex(self.FEE * 2)), fee_wei=self.FEE))

    def test_an_unreadable_balance_is_unknown_not_funded(self):
        for answer in (None, "0x", "not-hex", {}):
            self.assertEqual(
                self.runner.gas_shortfall(self._http(answer), fee_wei=self.FEE),
                f"gas_unknown:{self.runner.EVM_WALLET}")

    def test_no_fee_means_the_guard_says_nothing(self):
        self.assertIsNone(self.runner.gas_shortfall(
            self._http(hex(1)), fee_wei=None))

    def test_a_read_that_raises_is_unknown_not_funded(self):
        """A dead proxy must not read as "the wallet can pay"."""
        class Boom:
            def post(self, *a, **k):
                raise OSError("no proxy")

        short = self.runner.gas_shortfall(
            type("H", (), {"session": Boom()})(), fee_wei=self.FEE)
        self.assertTrue(short.startswith("gas_unknown:"), short)


class CrossModuleReferenceTest(unittest.TestCase):
    """Every ``FWD.x`` / ``LIVE.x`` this script calls must exist on the module it loads.

    Measured 2026-10-07 22:49: the family-5 run bought 0.01106987 SPYx, bridged
    it, saw the arrival on Solana -- and then stopped with
    ``credit_unreadable:NameError`` in the deposit step, because the script
    referenced a module-level ``LIVE`` that nothing ever bound. The leg code was
    fine; the reference was missing, and no test reached that branch.
    """

    def setUp(self):
        self.runner = _roundtrip()
        self.source = Path(self.runner.__file__).read_text()

    def _missing(self, alias: str) -> set:
        module = getattr(self.runner, alias)
        used = set(re.findall(rf"\b{alias}\.([A-Za-z_][A-Za-z0-9_]*)", self.source))
        return {name for name in used if not hasattr(module, name)}

    def test_every_reference_to_the_forward_helpers_exists(self):
        self.assertEqual(self._missing("FWD"), set())

    def test_every_reference_to_the_live_runner_legs_exists(self):
        self.assertEqual(self._missing("LIVE"), set())

    def test_the_kraken_legs_are_the_runners_own(self):
        """Not a copy: the same functions the resident live runner uses."""
        self.assertIs(self.runner.LIVE.legs.__module__, "v2liverunner")
        for name in ("legs", "_wait_credit", "_sell"):
            self.assertTrue(callable(getattr(self.runner.LIVE, name)), name)


class DepositLegTest(unittest.TestCase):
    """The Kajen ending: deposit to Kraken, then sell. No network, no key."""

    MINT = "XsoCS1TfEyfFhfvj8EtZ528L3CaKBDBRqRapnBbDF2W"
    DEST = "48SEVUduDW84t9SLxzGdxXb55qnx74wgLA3eGAYtKPyX"

    def setUp(self):
        self.runner = _roundtrip()
        self.state: dict = {}
        self.sent: list[tuple] = []

    def _ctx(self, *, raw=1_100_697):
        class _Probe:
            def solana_balance(self, http, mint):
                return raw

        return {"probe": _Probe(), "http": object(),
                "product": {"symbol": "SPYx", "mint": self.MINT,
                            "evm_xlayer": "0x90a2a4c76b5d8c0bc892a69ea28aa775a8f2dd48"}}

    def _legs(self, *, balance=(), plan=None, send_ok=True, plan_raises=None):
        plan = plan or type("P", (), {"ok": True, "destination": self.DEST,
                                      "raw_amount": 1_100_697, "reason": ""})()
        sent = self.sent
        outer = self

        class _Kraken:
            def balance(self):
                if isinstance(balance, Exception):
                    raise balance
                return {"SPYx.T": balance}

            def deposit_plan(self, symbol, mint, tokens):
                if plan_raises:
                    raise plan_raises
                outer.plan_tokens = tokens
                return plan

        class _Wallet:
            def send(self, mint, destination, raw):
                sent.append((mint, destination, raw))
                return {"ok": send_ok, "tx": "0xdeposit", "error": None}

        box = type("B", (), {})()
        box.kraken = _Kraken()
        box.wallet = _Wallet()
        return lambda: box

    def test_a_credited_deposit_records_the_tx_and_the_amount(self):
        run = self.runner.deposit_step_runner(
            self._ctx(), self.state, legs=self._legs(balance="0.0"))
        outcome = run()
        self.assertEqual(outcome.status, ex.OK)
        self.assertEqual(outcome.held_at, "kraken")
        self.assertEqual(self.sent, [(self.MINT, self.DEST, 1_100_697)])
        self.assertEqual(outcome.observed["destination"], self.DEST)
        self.assertEqual(self.state["deposit_tx"], "0xdeposit")
        # The plan is asked for the token amount, not the raw amount.
        self.assertEqual(self.state["deposit_tokens"], D("0.01100697"))
        self.assertEqual(self.plan_tokens, D("0.01100697"))

    def test_the_2026_10_07_name_error_is_now_a_named_refusal(self):
        """The exact shape of the halt: a missing name in the credit read.

        It used to be reported as ``credit_unreadable:NameError`` -- the bug was
        found through it, and the refusal must keep naming the cause instead of
        crashing the cycle. Here the same failure arrives from the balance read.
        """
        class _Legs:
            class kraken:
                @staticmethod
                def balance():
                    raise NameError("name 'LIVE' is not defined")

            class wallet:
                @staticmethod
                def send(*a, **k):
                    raise AssertionError("must not send when the credit is unreadable")

        run = self.runner.deposit_step_runner(
            self._ctx(), self.state, legs=lambda: _Legs())
        outcome = run()
        self.assertEqual(outcome.status, ex.REFUSED)
        self.assertEqual(outcome.detail, "credit_unreadable:NameError")
        self.assertEqual(self.sent, [])

    def test_below_the_deposit_minimum_the_coins_stay_in_our_wallet(self):
        """Below the minimum they are never credited, and a top-up does not help."""
        plan = type("P", (), {"ok": False, "reason": "below_min",
                              "destination": "", "raw_amount": 0})()
        run = self.runner.deposit_step_runner(
            self._ctx(), self.state, legs=self._legs(plan=plan))
        outcome = run()
        self.assertEqual(outcome.status, ex.STOPPED)
        self.assertEqual(outcome.detail, "deposit_gate:below_min")
        self.assertEqual(outcome.held_at, "wallet")
        self.assertEqual(self.sent, [])

    def test_an_empty_solana_balance_stops_without_touching_kraken(self):
        run = self.runner.deposit_step_runner(
            self._ctx(raw=0), self.state, legs=self._legs())
        outcome = run()
        self.assertEqual(outcome.status, ex.STOPPED)
        self.assertEqual(outcome.held_at, "wallet")
        self.assertEqual(self.sent, [])

    def test_a_transfer_that_reports_an_error_is_not_retried(self):
        """A second send would double-send; the state stays recoverable by hand."""
        run = self.runner.deposit_step_runner(
            self._ctx(), self.state, legs=self._legs(send_ok=False))
        outcome = run()
        self.assertEqual(outcome.status, ex.STOPPED)
        self.assertTrue(outcome.detail.startswith("transfer:"))
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("deposit_tx", self.state)


class SellLegTest(unittest.TestCase):
    """Kraken sell leg（family 5/6 的收尾）。不碰网络、不读密钥。

    2026-10-07 的两轮 live 都是人工补完卖出的：22:49 那轮靠人转 Kraken 再卖。
    这条腿到现在没有 hermetic 用例，所以这里把它的四种结局钉住。
    """

    def setUp(self):
        self.runner = _roundtrip()
        self.state = {"credit": D("0.011070"), "ref_price": D("774.28")}
        self.calls: list[dict] = []

    def _ctx(self):
        return {"product": {"symbol": "SPYx"},
                "policy": {"min_price_vs_ref": D("0.995")}}

    def _legs(self):
        box = type("B", (), {})()
        box.kraken = type("K", (), {"client": "kraken-client"})()
        return lambda: box

    def _sell(self, result):
        outer = self

        def fn(kraken, client, policy, *, symbol, tokens, ref):
            outer.calls.append({"kraken": kraken, "client": client, "policy": policy,
                                "symbol": symbol, "tokens": tokens, "ref": ref})
            return result

        return fn

    def _run(self, result):
        run = self.runner.sell_kraken_runner(
            self._ctx(), self.state, legs=self._legs(), sell_fn=self._sell(result))
        return run()

    def test_a_fill_records_txid_average_cost_and_fee(self):
        out = self._run({"ok": True, "txid": "OOVB3K-P4U5W-MDTTFL", "price": "774.28",
                         "avg": "775.10", "cost": "8.5714", "fee": "0.0086"})
        self.assertEqual(out.status, ex.OK)
        self.assertEqual(self.state["sold"]["txid"], "OOVB3K-P4U5W-MDTTFL")
        # 记的是实际成交均价，不是挂单价（2026-10-07 复盘过的口径）。
        self.assertEqual(out.observed["price"], "775.10")
        self.assertEqual(out.observed["cost"], "8.5714")
        self.assertEqual(out.observed["fee"], "0.0086")
        # 卖的是 Kraken 入账的枚数，参考价按原样传下去。
        self.assertEqual(self.calls[0]["tokens"], D("0.011070"))
        self.assertEqual(self.calls[0]["ref"], D("774.28"))
        self.assertEqual(self.calls[0]["symbol"], "SPYx")

    def test_a_fill_without_an_average_falls_back_to_the_limit_price(self):
        out = self._run({"ok": True, "txid": "abc", "price": "774.28"})
        self.assertEqual(out.status, ex.OK)
        self.assertEqual(out.observed["price"], "774.28")

    def test_no_credited_units_stops_without_touching_the_book(self):
        self.state.pop("credit")
        out = self._run({"ok": True, "txid": "never"})
        self.assertEqual((out.status, out.held_at), (ex.STOPPED, "kraken"))
        self.assertEqual(self.calls, [])

    def test_a_zero_credit_is_not_sold(self):
        self.state["credit"] = D("0")
        out = self._run({"ok": True, "txid": "never"})
        self.assertEqual(out.status, ex.STOPPED)
        self.assertEqual(self.calls, [])

    def test_a_refused_sell_keeps_the_reason_and_says_where_the_coins_are(self):
        out = self._run({"ok": False, "reason": "盘口吃不下：占 50 bps 内盘口 1664%"})
        self.assertEqual((out.status, out.held_at), (ex.STOPPED, "kraken"))
        self.assertTrue(out.detail.startswith("sell:"), out.detail)
        self.assertIn("盘口吃不下", out.detail)
        self.assertNotIn("sold", self.state)


class PostBuyRefusalTest(unittest.TestCase):
    """A bridge we refuse *after* buying is inventory, not a flat cycle."""

    def test_the_cycle_stops_that_symbol_and_leaves_the_runner_open(self):
        tmp = Path(tempfile.mkdtemp())
        halt = tmp / "HALT"
        steps = [
            ex.Step("buy", lambda: ex.Outcome(ex.OK), held_at="dest_wallet",
                    broadcasts=True, inventory=True),
            ex.Step("approve", lambda: ex.Outcome(ex.OK), held_at="dest_wallet",
                    broadcasts=True, inventory=True),
            ex.Step("bridge",
                    lambda: ex.Outcome(ex.REFUSED,
                                       "dry_run: ERC20: insufficient allowance",
                                       held_at="dest_wallet"),
                    held_at="in_bridge", broadcasts=True, inventory=True),
        ]
        symbols = tmp / "symbol_halts"
        machine = ex.Executor(symbol="SPYx", chain="x-layer",
                              family="chain_via_bridge_to_kraken", mode="live",
                              size_usdt=D("8.58"), tokens=D("0.011"),
                              steps=steps, gate=lambda: (True, "ok"),
                              ledger_path=tmp / "cycles.jsonl", halt_path=halt,
                              symbol_halt_dir=symbols)
        result = machine.run()

        self.assertEqual(result["state"], ledger.STOPPED)
        self.assertEqual(result["held_at"], "dest_wallet")
        self.assertFalse(result["keep_lock"])
        self.assertFalse(result["halt"])
        self.assertFalse(halt.exists())
        self.assertTrue((symbols / "SPYx").exists())
        self.assertIn("dry_run", result["reason"])
        # The buy and approve legs are still on the ledger: the coins are real.
        rows = [json.loads(line) for line in
                (tmp / "cycles.jsonl").read_text().splitlines() if line.strip()]
        self.assertEqual([row["extra"]["step"] for row in rows],
                         ["buy", "approve", "finish"])
        self.assertEqual(rows[-1]["held_at"], "dest_wallet")


if __name__ == "__main__":
    unittest.main()
