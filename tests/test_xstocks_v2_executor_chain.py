"""Hermetic tests: a row nobody can run must not print as runnable.

Operator, 2026-10-09: the board showed SPCXx on eth (uniswap 164.6544 ->
kraken 165.9100, +76.26 bps) with 阻挡原因「无」 and asked why it never ran.
The answer was that the live runner only drives solana, so the board was
describing a trade no adapter could make. These tests pin the one list both
readers use (``live.EXECUTOR_CHAINS``), and the blocker a row carries when no
executor reaches its chain.
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from arb_monitor.xstocks_v2 import live, scan
from arb_monitor.xstocks_v2.policy import V2Policy
from arb_monitor.xstocks_v2.spot_venue import BUY, REAL, Quote

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExecutorChainTest(unittest.TestCase):
    def test_every_chain_the_map_claims_has_an_executor_and_others_do_not(self):
        """The assertion is the invariant, not a frozen list.

        The list grows as executors land (solana -> eth 2026-10-09, ton and
        x-layer the same day from another session). What must never change is
        the rule: a chain *in* the map answers yes, a chain outside it answers
        no with ``no_executor_chain``.
        """
        for chain in sorted(live.EXECUTOR_CHAINS[live.CHAIN_TO_KRAKEN]):
            self.assertEqual(live.executor_chain(live.CHAIN_TO_KRAKEN, chain),
                             (True, ""), chain)
        for chain in ("ink", "monad", "no-such-chain"):
            self.assertEqual(
                live.executor_chain(live.CHAIN_TO_KRAKEN, chain),
                (False, live.NO_EXECUTOR_CHAIN), chain)

    def test_a_family_this_map_has_no_opinion_about_is_not_flagged(self):
        # dex_to_dex rows are parked by their own family gate; inventing a
        # second reason here would hide the real one.
        self.assertEqual(live.executor_chain("dex_to_dex", "eth"), (True, ""))

    def test_the_runner_and_the_scan_read_the_same_list(self):
        runner = _load("v2live_executor", "scripts/xstocks_v2_live.py")
        self.assertEqual(runner.ALLOWED_CHAINS,
                         live.EXECUTOR_CHAINS[live.CHAIN_TO_KRAKEN])

    def test_the_board_has_a_label_for_the_new_blocker(self):
        board = _load("v2board_executor", "scripts/xstocks_v2_board.py")
        self.assertIn(live.NO_EXECUTOR_CHAIN, board.REASON_LABEL)


class ExecutorCoverageOnTheRowTest(unittest.TestCase):
    """The scan must say "no executor" instead of "no blocker"."""

    def setUp(self):
        self.policy = V2Policy.load()

    def _quote(self, chain, price, venue):
        return Quote(venue=venue, chain=chain, symbol="SPCXx", side=BUY,
                     kind=REAL, price_usd=D(price), recv_ms=1000,
                     pool_id="0xpool", extra={"impact_bps": D("12")})

    def _row(self, chain, venue, deposit_method):
        entries = [{"symbol": "SPCXx", "chain": chain, "pool_id": "0xpool",
                    "errors": [], "sell": None,
                    "buy": self._quote(chain, "164.65", venue)}]
        rows = scan.build_candidates(
            symbol="SPCXx", chain_quotes=entries,
            kraken_row={"bid": D("165.91"), "ask": D("166.0"), "recv_ms": 1000},
            policy=self.policy, multiplier=D("1"),
            deposits={"SPCXx": [{"method": deposit_method}]},
            v4_tiers={}, now_ms=1000, phase="paper_kraken")
        return next(row for row in rows if row["family"] == "chain_to_kraken")

    def test_a_clean_eth_row_no_longer_carries_no_executor_chain(self):
        row = self._row("eth", "uniswap", "SPCXx - Ethereum")
        # The spread and the rail are fine -- that is what made the board say
        # "无" and the operator ask the question.
        self.assertGreater(D(row["edge_t0_bps"]), D("70"))
        self.assertNotIn("below_threshold", row["blockers"])
        self.assertNotIn(live.NO_EXECUTOR_CHAIN, row["blockers"])

    def test_a_row_on_an_uncovered_chain_carries_no_executor_chain(self):
        """A chain with a rail but no executor has to say so on the row."""
        covered = live.EXECUTOR_CHAINS[live.CHAIN_TO_KRAKEN]
        uncovered = [chain for chain in ("solana", "eth", "ton", "x-layer")
                     if chain not in covered]
        if not uncovered:
            self.skipTest("every phase chain has an executor today")
        chain = uncovered[0]
        row = self._row(chain, f"{chain}-swap", f"SPCXx - {chain.title()}")
        self.assertEqual(row["blockers"], [live.NO_EXECUTOR_CHAIN])
        self.assertFalse(row["executable"])

    def test_x_layer_pays_the_solana_bridge_instead_of_saying_no_executor(self):
        row = self._row("x-layer", "uniswap-v3-x-layer", "GOOGLx - Ethereum")
        self.assertNotIn(live.NO_EXECUTOR_CHAIN, row["blockers"])
        self.assertEqual(row.get("hub_fixed_note"), "bridge_to_solana")
        self.assertIsNone(row.get("hub_fixed_usd"))

    def test_the_same_row_on_solana_stays_executable(self):
        row = self._row("solana", "jupiter", "SPCXx - Solana")
        self.assertEqual(row["blockers"], [])
        self.assertTrue(row["executable"])

    def test_an_eth_row_never_enters_the_live_candidate_list(self):
        """The Solana planner must not be handed an eth row.

        Its own selector (``eth_candidates``) is what runs them, through the eth
        executor; ``candidates`` re-quotes Jupiter and sizes in Solana units.
        """
        runner = _load("v2live_candidates", "scripts/xstocks_v2_live.py")
        snapshot = {"rows": [
            self._row("eth", "uniswap", "SPCXx - Ethereum"),
            self._row("solana", "jupiter", "SPCXx - Solana"),
        ]}
        chains = {row["chain"]
                  for row in runner.candidates(snapshot, chains=runner.PLAN_CHAINS)}
        self.assertEqual(chains, {"solana"})


class NoExecutorPanelTest(unittest.TestCase):
    """The row the operator asked about has to be on the page, with its reason."""

    def _render(self, rows):
        board = _load("v2board_no_executor", "scripts/xstocks_v2_board.py")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / board.SCAN.name).write_text(json.dumps({
                "generated_ms": board.time.time() * 1000, "phase": "paper_kraken",
                "session": "premarket", "threshold_bps": "4", "rows": rows}))
            data = board.collect(data_dir=Path(tmp), interval_s=180.0)
            return data, board.render(data)

    def _row(self):
        return {
            "symbol": "SPCXx", "family": "chain_to_kraken", "chain": "eth",
            "buy_venue": "uniswap", "sell_venue": "kraken",
            "buy_price_usd": "164.6544221861611454140896693",
            "sell_price_usd": "165.91", "kraken_price_basis": "book_vwap",
            "book_slip_bps": "0.0", "edge_t0_bps": "76.255335093234030084",
            "required_edge_bps": "4", "notional_usdt": "25",
            "gross_usdt": "0.190638337733085075", "net_usdt": "0.1406",
            "executable": False, "blockers": ["no_executor_chain"],
            "reason": "no_executor_chain",
        }

    def test_the_row_is_listed_with_the_reason_and_not_as_a_trusted_row(self):
        data, page = self._render([self._row()])
        self.assertEqual([row["symbol"] for row in data["no_executor"]],
                         ["SPCXx"])
        self.assertEqual(data["top"], [])
        self.assertIn("有价差、但没有执行器的行", page)
        self.assertIn("没有接执行器", page)

    def test_a_row_with_an_executor_is_not_in_this_list(self):
        row = self._row()
        row["chain"], row["blockers"], row["reason"] = "solana", [], ""
        row["executable"] = True
        data, _page = self._render([row])
        self.assertEqual(data["no_executor"], [])


if __name__ == "__main__":
    unittest.main()
