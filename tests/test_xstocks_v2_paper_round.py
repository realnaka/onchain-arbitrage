"""A whole paper round must not die on a number that is a string (A47).

Measured 2026-10-09: `data-v2/xstocks-v2-paper.log` carried

    {"round": N, "error": "ValueError: Unknown format code 'f' for object of type 'str'"}

27 times between round 4 and round 267 (~1 in 10 rounds), and each time the round
produced no paper state at all. The cause was one line:

    "book_short": format(fill["short"], "f")

``kraken.book_fill`` reports ``short`` as a decimal **string** ("0.491489"), so
formatting it as a float raises. It only runs when the book cannot fill the size
-- which is exactly why it looked intermittent instead of constant.

This file drives ``run_once`` with every network call stubbed, so the failing
branch is reached on purpose.
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import time
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from arb_monitor.xstocks_v2 import ledger
from arb_monitor.xstocks_v2.policy import V2Policy

ROOT = Path(__file__).resolve().parents[1]


def _paper_script():
    spec = importlib.util.spec_from_file_location(
        "v2paper_round", ROOT / "scripts" / "xstocks_v2_paper.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Quote:
    def __init__(self, price, recv_ms):
        self.price_usd = price
        self.recv_ms = recv_ms


class PaperRoundSurvivesShortfallTest(unittest.TestCase):
    """``book_short`` is a string; a round may not raise on it."""

    def setUp(self):
        self.papr = _paper_script()
        self.policy = V2Policy.load()

    def _snapshot(self, symbol="PFEx"):
        return {
            "generated_ms": time.time() * 1000,
            "phase": "paper_kraken", "auto_orders": "live",
            "session": "closed", "threshold_bps": "4",
            "rows": [{
                "symbol": symbol, "family": "chain_to_kraken", "chain": "solana",
                "buy_price_usd": "25", "edge_t0_bps": "300",
                "notional_usdt": "25", "blockers": [],
            }],
        }

    def _run(self, tmp, bids, *, usdc="50", usdt="0"):
        papr = self.papr
        now = int(time.time() * 1000)
        entry = {"buy": _Quote(D("25"), now), "tokens": D("1")}
        depth = {"depth": {"PFEx": {"bids": bids}}, "errors": []}
        with mock.patch.object(papr, "gate", lambda policy, data_dir: (True, "ok")), \
                mock.patch.object(papr.scan, "requote_buy",
                                  lambda **kwargs: entry), \
                mock.patch.object(papr.kraken, "fetch_depth",
                                  lambda http, symbols: depth), \
                mock.patch.object(papr, "native_usd",
                                  lambda http, chains: {"SOL": D("110")}), \
                mock.patch.object(papr.live_mod, "spl_balance",
                                  staticmethod(lambda http, mint: (
                                      D(usdc) if mint == papr.live_mod.SOLANA_QUOTES["USDC"]
                                      else D(usdt)))), \
                mock.patch.object(papr.live_mod, "first_transfer_for",
                                  lambda symbol, path: False), \
                mock.patch.object(papr, "JupiterFeed", lambda http: object()):
            return papr.run_once(policy=self.policy, data_dir=tmp, http=object())

    def test_a_book_too_thin_to_fill_does_not_kill_the_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            scan_path = Path(tmp) / self.papr.SCAN.name
            scan_path.write_text(json.dumps(self._snapshot()))
            state = self._run(tmp, bids=[["100", "0.0001"]])
            self.assertEqual(state["states"], {"refused:kraken_depth_short": 1})
            rows = ledger.recent(Path(tmp) / "xstocks_v2_cycles.jsonl")
            self.assertEqual(len(rows), 1)
            book_short = rows[0]["extra"]["book_short"]
            # A string, written as a string -- not formatted as a float.
            self.assertIsInstance(book_short, str)
            self.assertGreater(D(book_short), D("0"))

    def test_a_fillable_book_still_decides(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / self.papr.SCAN.name).write_text(json.dumps(self._snapshot()))
            state = self._run(tmp, bids=[["30", "100"]])
            self.assertEqual(len(state["decisions"]), 1)
            self.assertNotEqual(state["decisions"][0]["reason"],
                                "kraken_depth_short")


class NoFloatFormatOnBookShortTest(unittest.TestCase):
    """The regression guard: nothing formats ``book_fill``'s ``short``."""

    def test_no_source_formats_book_short_as_a_float(self):
        offenders = []
        for root in ("src/arb_monitor/xstocks_v2", "scripts"):
            for path in (ROOT / root).rglob("*.py"):
                text = path.read_text()
                if 'format(fill["short"]' in text:
                    offenders.append(str(path))
        self.assertEqual(offenders, [])

    def test_book_fill_reports_short_as_a_string(self):
        from arb_monitor.xstocks_v2.feeds import kraken
        fill = kraken.book_fill([["100", "0.0001"]], D("1"), side="sell")
        self.assertFalse(fill["ok"])
        self.assertIsInstance(fill["short"], str)


class FundingGapIsNotOneWordTest(unittest.TestCase):
    """Operator 2026-10-10: ``funding_short`` may not be a single word.

    Measured 2026-10-09: 4,882 of the day's 5,703 ledger rows ended on that one
    word. It never said which chain, which ticket, or how far off the wallet
    was -- so "busy" and "3 USDT away from a trade" looked the same. The row now
    carries the gap, and the board prints it.
    """

    def setUp(self):
        self.papr = _paper_script()
        self.policy = V2Policy.load()

    def _round(self, tmp, *, usdc, usdt):
        papr = self.papr
        now = int(time.time() * 1000)
        entry = {"buy": _Quote(D("25"), now), "tokens": D("1")}
        depth = {"depth": {"PFEx": {"bids": [["30", "100"]]}}, "errors": []}
        snapshot = {"generated_ms": now, "phase": "paper_kraken",
                    "auto_orders": "live", "session": "closed",
                    "threshold_bps": "4",
                    "rows": [{"symbol": "PFEx", "family": "chain_to_kraken",
                              "chain": "solana", "buy_price_usd": "25",
                              "edge_t0_bps": "300", "notional_usdt": "25",
                              "blockers": []}]}
        (Path(tmp) / papr.SCAN.name).write_text(json.dumps(snapshot))
        with mock.patch.object(papr, "gate", lambda policy, data_dir: (True, "ok")), \
                mock.patch.object(papr.scan, "requote_buy", lambda **kw: entry), \
                mock.patch.object(papr.kraken, "fetch_depth",
                                  lambda http, symbols: depth), \
                mock.patch.object(papr, "native_usd",
                                  lambda http, chains: {"SOL": D("110")}), \
                mock.patch.object(papr.live_mod, "spl_balance",
                                  staticmethod(lambda http, mint: (
                                      D(usdc) if mint == papr.live_mod.SOLANA_QUOTES["USDC"]
                                      else D(usdt)))), \
                mock.patch.object(papr.live_mod, "first_transfer_for",
                                  lambda symbol, path: False), \
                mock.patch.object(papr, "JupiterFeed", lambda http: object()):
            return papr.run_once(policy=self.policy, data_dir=tmp, http=object())

    def test_a_row_that_only_lacks_money_publishes_the_shortfall(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self._round(tmp, usdc="20", usdt="0")
            decision = state["decisions"][0]
            self.assertEqual(decision["reason"], "funding_short")
            gap = decision["extra"]["funding_gap"]
            slack = D("25") * D(self.policy.max_slippage_bps) / D(10000)
            self.assertEqual(D(gap["short_usdt"]), D("25") + slack - D("20"))
            self.assertEqual(D(gap["have_usdc"]), D("20"))
            self.assertEqual(D(gap["need_usdt"]), D("25"))
            # The ledger row carries the same numbers, so an audit does not
            # have to re-derive them from the wallet.
            rows = ledger.recent(Path(tmp) / "xstocks_v2_cycles.jsonl")
            self.assertEqual(rows[0]["reason"], "funding_short")
            self.assertEqual(D(rows[0]["extra"]["funding_gap"]["short_usdt"]),
                             D(gap["short_usdt"]))

    def test_a_funded_row_publishes_no_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self._round(tmp, usdc="50", usdt="0")
            self.assertNotEqual(state["decisions"][0]["reason"], "funding_short")
            self.assertEqual(state["decisions"][0]["extra"]["funding_gap"], {})


if __name__ == "__main__":
    unittest.main()
