"""A transient X Layer RPC error must not halt the chain.

Measured 2026-10-09 10:43: ``rpc.xlayer.tech`` dropped the TLS connection inside
``usdg_balance`` -- the **first read** of a live probe, before a single wei moved
-- and the SSL error propagated out of the cycle. The executor turned it into an
``unknown`` ledger row plus ``data-v2/xstocks_v2_HALT``, and every round after
that was refused with ``allowed: false / halt`` (the wallet never changed:
USDG 50.4633 = 50.8633 - the 0.4 gas swap).

Three properties are pinned here:

* a read is retried -- reads are idempotent, so a retry cannot double-spend;
* a read that still fails is ``None``: "could not read", never 0;
* the code may not size a trade off that ``None``.
"""
from __future__ import annotations

import importlib.util
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest import mock


def _mod():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "v2sol_rpc", root / "scripts" / "xstocks_v2_solana_roundtrip.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Session:
    """Fails ``fail_times`` times, then answers with ``payload``."""

    def __init__(self, *, payload=None, fail_times=0):
        self.payload = payload
        self.fail_times = fail_times
        self.calls = 0

    def post(self, url, json=None, timeout=None):  # noqa: A002 - requests API
        self.calls += 1
        if self.calls <= self.fail_times:
            raise OSError("SSLEOFError: UNEXPECTED_EOF_WHILE_READING")
        return _Response(self.payload)


class _Http:
    def __init__(self, session):
        self.session = session


class XLayerReadRetryTest(unittest.TestCase):
    def setUp(self):
        self.mod = _mod()
        # The backoff is real in production; a test must not spend it.
        patcher = mock.patch.object(self.mod.time, "sleep", lambda *_: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.owner = "0x" + "22" * 20
        self.spender = "0x" + "33" * 20
        self.token = "0x" + "11" * 20

    def test_a_transient_failure_is_retried_and_the_read_succeeds(self):
        http = _Http(_Session(payload={"result": hex(50_000_000)}, fail_times=1))
        self.assertEqual(self.mod.usdg_balance(http), D("50"))
        self.assertEqual(http.session.calls, 2)

    def test_a_read_that_never_answers_is_none_not_zero(self):
        http = _Http(_Session(fail_times=99))
        self.assertIsNone(self.mod.usdg_balance(http))
        self.assertEqual(http.session.calls, self.mod.XLAYER_RPC_TRIES)

    def test_an_empty_result_is_none(self):
        http = _Http(_Session(payload={"result": "0x"}))
        self.assertIsNone(self.mod.usdg_balance(http))
        self.assertIsNone(self.mod.allowance(http, self.token, self.owner,
                                             self.spender))

    def test_allowance_reads_a_number_and_none_when_it_cannot(self):
        live = _Http(_Session(payload={"result": hex(1234)}))
        self.assertEqual(
            self.mod.allowance(live, self.token, self.owner, self.spender), 1234)
        dead = _Http(_Session(fail_times=99))
        self.assertIsNone(
            self.mod.allowance(dead, self.token, self.owner, self.spender))

    def test_native_balance_also_retries(self):
        http = _Http(_Session(payload={"result": hex(3_779_196_824_057_683)},
                              fail_times=1))
        self.assertEqual(self.mod.native_balance(http),
                         D("3779196824057683") / D(10 ** 18))
        self.assertEqual(http.session.calls, 2)

    def test_a_simulation_that_never_answered_is_not_a_pass(self):
        http = _Http(_Session(fail_times=99))
        result, error = self.mod.simulate({"http": http}, "0xdead", 1)
        self.assertIsNone(result)
        self.assertTrue(str(error).startswith("transport:"), error)

    def test_the_module_never_sizes_a_trade_off_an_unreadable_balance(self):
        """Source guard: ``usdg_balance(...) or 0`` is the bug that was fixed."""
        source = (Path(__file__).resolve().parents[1]
                  / "scripts" / "xstocks_v2_solana_roundtrip.py").read_text()
        self.assertNotIn('usdg_balance(http) or', source)
        self.assertIn("usdg_unreadable", source)


class BridgeFeeTest(unittest.TestCase):
    """The bridge leg pays the fee the chain asks **now** (measured 2026-10-09).

    The 2026-09-26 constant was still what the leg paid while the scan's cost
    model had already moved to a live read (A45). At 12:28 the dry-run reverted
    with ``InsufficientFee(provided=791242725185203, required=797003538787366)``
    and the probe stopped with the coins in our wallet.
    """

    FLOOR = 791_242_725_185_203

    def setUp(self):
        self.mod = _mod()

    def _fee(self, quoted, fallback=None):
        fallback = self.FLOOR if fallback is None else fallback
        with mock.patch.object(self.mod.EVM, "fee_quote",
                               lambda **kw: (quoted, "ccip-dryrun:x-layer->solana")):
            return self.mod.bridge_fee_wei(fallback=fallback)

    def test_it_takes_the_chain_number_plus_headroom(self):
        raw = int(D("0.000797") * D(10 ** 18))
        self.assertEqual(self._fee(D("0.000797")), raw + raw // 50)
        self.assertGreater(self._fee(D("0.000797")), 797_003_538_787_366)

    def test_a_quote_below_the_measured_floor_still_pays_the_floor(self):
        self.assertEqual(self._fee(D("0.0001")),
                         self.FLOOR + self.FLOOR // 50)

    def test_a_failed_read_pays_the_floor(self):
        self.assertEqual(self._fee(None), self.FLOOR)

    def test_a_raising_read_pays_the_floor(self):
        def boom(**kwargs):
            raise OSError("SSLEOFError")

        with mock.patch.object(self.mod.EVM, "fee_quote", boom):
            self.assertEqual(self.mod.bridge_fee_wei(fallback=self.FLOOR),
                             self.FLOOR)

    def test_the_bridge_step_pays_the_fresh_fee_not_the_bound_constant(self):
        """Source guard: the runner must call ``bridge_fee_wei``."""
        source = (Path(__file__).resolve().parents[1]
                  / "scripts" / "xstocks_v2_solana_roundtrip.py").read_text()
        self.assertIn("fee = bridge_fee_wei(fallback=fee_wei)", source)
        self.assertIn('"--amt", str(fee), "--from"', source)


if __name__ == "__main__":
    unittest.main()
