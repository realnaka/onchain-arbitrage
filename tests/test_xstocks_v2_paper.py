"""Hermetic tests for the v2 paper runner, ledger and the v1 coexistence gate.

No network, no wallets, no halt files outside a temporary directory.
"""
from __future__ import annotations

import unittest
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory

from arb_monitor.xstocks_v2 import cycle as cycle_core
from arb_monitor.xstocks_v2 import costs, ledger, legs_kraken, paper
from arb_monitor.xstocks_v2.policy import V2Policy


class FakeResult:
    def __init__(self, returncode: int, stdout: str = ""):
        self.returncode = returncode
        self.stdout = stdout


class CostTest(unittest.TestCase):
    def setUp(self):
        self.policy = V2Policy.load()

    def test_first_solana_transfer_is_the_expensive_one(self):
        first, _ = paper.transfer_cost_usdt(
            chain="solana", first_for_mint=True, sol_usd=D("118"), policy=self.policy)
        repeat, _ = paper.transfer_cost_usdt(
            chain="solana", first_for_mint=False, sol_usd=D("118"), policy=self.policy)
        self.assertEqual(first, D("0.00313") * D("118"))
        self.assertLess(repeat, first)
        # 0.000015 SOL is the repeat fee, not the whole story (lessons 2.2).
        self.assertGreater(first / repeat, D("200"))

    def test_missing_sol_price_is_unknown_not_zero(self):
        cost, reason = paper.transfer_cost_usdt(
            chain="solana", first_for_mint=True, sol_usd=None, policy=self.policy)
        self.assertIsNone(cost)
        self.assertEqual(reason, "sol_price_unknown")

    def test_evm_gas_needs_a_price(self):
        cost, reason = paper.gas_cost_usdt(
            chain="eth", gas_price_wei=None, native_usd=D("3900"), policy=self.policy)
        self.assertIsNone(cost)
        self.assertEqual(reason, "gas_unknown")
        priced, reason = paper.gas_cost_usdt(
            chain="eth", gas_price_wei=2_000_000_000, native_usd=D("3900"),
            policy=self.policy)
        self.assertEqual(reason, "estimated")
        self.assertGreater(priced, D("0"))

    def test_solana_has_no_gas_line(self):
        cost, reason = paper.gas_cost_usdt(
            chain="solana", gas_price_wei=None, native_usd=None, policy=self.policy)
        self.assertEqual(cost, D("0"))
        self.assertEqual(reason, "not_applicable")

    def test_net_subtracts_transfer_fee_taker_and_buffer(self):
        """Taker and buffer are charged on the notional, from the shared model.

        This used to charge them on the *proceeds* while ``scan`` charged the
        notional -- two implementations of the same two terms for one row. The
        shared model (``costs.net_after_costs``) charges the notional, which is
        the size we know before the trade; the difference on this row is
        0.0005 USDT (spread x fee) and it is asserted below, not hidden.
        """
        result = paper.chain_to_kraken_net(
            size_usdt=D("25"), tokens=D("0.25"), buy_price_usd=D("100"),
            kraken_bid=D("101"), policy=self.policy,
            transfer_usdt=D("0.37"), gas_usdt=D("0"))
        # 0.25 * 101 = 25.25 proceeds, minus the 25 capital we spent, minus
        # 10 bps taker, 10 bps buffer (both on the 25 notional) and 0.37 transfer.
        expected = (D("25.25") - D("25") - D("25") * D("0.001")
                    - D("0.001") * D("25") - D("0.37"))
        self.assertAlmostEqual(float(result["net_usdt"]), float(expected), places=8)
        self.assertEqual(result["net_usdt"], costs.net_after_costs(
            gross_usdt=D("0.25"), notional_usdt=D("25"),
            fixed_cost_usd=D("0.37"), policy=self.policy,
            family="chain_to_kraken"))
        # The old (proceeds-based) reading, kept as a number so the change is
        # auditable rather than invisible.
        old = (D("25.25") - D("25") - D("25.25") * D("0.001")
               - D("0.001") * D("25.25") - D("0.37"))
        self.assertAlmostEqual(float(result["net_usdt"] - old), 0.0005, places=8)
        self.assertIn("kraken_fee_usdt", result["costs"])
        self.assertIn("capital_usdt", result["costs"])

    def test_capital_is_inside_the_net(self):
        """A 1% move on a 25 USDT trade cannot be worth 24 USDT."""
        result = paper.chain_to_kraken_net(
            size_usdt=D("25"), tokens=D("0.25"), buy_price_usd=D("100"),
            kraken_bid=D("101"), policy=self.policy,
            transfer_usdt=D("0.01"), gas_usdt=D("0"))
        self.assertLess(result["net_usdt"], D("0.30"))
        self.assertGreater(result["net_usdt"], D("0"))

    def test_unknown_term_makes_the_whole_path_unpriceable(self):
        result = paper.chain_to_kraken_net(
            size_usdt=D("25"), tokens=D("0.25"), buy_price_usd=D("100"),
            kraken_bid=D("101"), policy=self.policy,
            transfer_usdt=None, gas_usdt=D("0"))
        self.assertIsNone(result["net_usdt"])
        self.assertEqual(result["reason"], "cost_unknown")


class DecideTest(unittest.TestCase):
    def setUp(self):
        self.policy = V2Policy.load()

    def _row(self, **overrides):
        row = {"family": "chain_to_kraken", "symbol": "AAPLx", "chain": "solana",
               "blockers": [], "edge_t0_bps": "40", "buy_price_usd": "100"}
        row.update(overrides)
        return row

    def _decide(self, row=None, **kwargs):
        kwargs.setdefault("kraken_bid", D("101"))
        kwargs.setdefault("size_usdt", D("25"))
        kwargs.setdefault("tokens", D("0.25"))
        kwargs.setdefault("transfer_usdt", D("0.30"))
        kwargs.setdefault("gas_usdt", D("0"))
        kwargs.setdefault("phase_allows", True)
        kwargs.setdefault("slot_free", True)
        return paper.decide(row=row or self._row(), policy=self.policy, **kwargs)

    def test_clean_path_is_paper_and_never_broadcasts(self):
        # 0.25 tokens bought at 100, sold at 102 = 0.5 USDT gross, which has to
        # clear the transfer fee, both cost buffers and the 0.1 USDT floor.
        decision = self._decide(kraken_bid=D("102"))
        self.assertEqual(decision["state"], "paper")
        self.assertFalse(decision["broadcast"])

    def test_the_absolute_floor_binds_below_the_bps_gate(self):
        """At 25 USDT, 10 bps is 0.025 USDT, well under the 0.1 floor."""
        row = self._row(edge_t0_bps="15")
        decision = self._decide(row=row, kraken_bid=D("101.2"), transfer_usdt=D("0.20"))
        self.assertLess(decision["net_usdt"], D("0.1"))
        self.assertEqual(decision["state"], "refused")
        self.assertEqual(decision["reason"], "below_threshold")

    def test_other_families_are_not_in_phase_one(self):
        row = self._row(family="dex_to_dex")
        self.assertEqual(self._decide(row=row)["reason"], "family_not_in_phase_1")

    def test_a_blocker_from_the_scan_still_refuses(self):
        row = self._row(blockers=["below_depth"])
        decision = self._decide(row=row)
        self.assertEqual(decision["state"], "refused")
        self.assertEqual(decision["reason"], "below_depth")

    def test_the_refusal_uses_the_scans_headline_not_the_alphabet(self):
        """The row the operator read: -20 bps reported as "no arrival sample".

        ``sorted()`` put ``bridge_unmeasured_symbol`` first because "b" < "b"?
        No: because the scan only had the plumbing blocker to offer. Now the scan
        sends its own ``reason`` and the paper table repeats it.
        """
        row = self._row(blockers=["bridge_unmeasured_symbol", "below_threshold"],
                        reason="below_threshold")
        decision = self._decide(row=row)
        self.assertEqual(decision["state"], "refused")
        self.assertEqual(decision["reason"], "below_threshold")

    def test_without_a_headline_it_still_refuses_on_a_blocker(self):
        row = self._row(blockers=["bridge_unmeasured_symbol"])
        self.assertEqual(self._decide(row=row)["reason"], "bridge_unmeasured_symbol")

    def test_phase_scan_alone_does_not_hide_the_other_blockers(self):
        row = self._row(blockers=["phase_scan", "hook_not_green"])
        self.assertEqual(self._decide(row=row)["reason"], "hook_not_green")

    def test_scan_time_staleness_is_re_derived_from_the_fresh_pair(self):
        """The caller re-quotes both legs, so the scan's own verdict is replaced."""
        row = self._row(blockers=["stale_or_skew", "phase_scan"])
        self.assertEqual(self._decide(row=row, kraken_bid=D("102"))["state"], "paper")
        self.assertEqual(
            self._decide(row=row, kraken_bid=D("102"), pair_skew_ms=60_000)["reason"],
            "stale_or_skew")
        self.assertEqual(
            self._decide(row=row, kraken_bid=D("102"), pair_skew_ms=1_000)["state"],
            "paper")

    def test_phase_that_is_not_executable_refuses(self):
        self.assertEqual(self._decide(phase_allows=False)["reason"],
                         "phase_not_executable")

    def test_v1_holding_the_key_refuses_the_slot(self):
        decision = self._decide(slot_free=False, reason_if_busy="v1_live_busy: pgrep")
        self.assertEqual(decision["state"], "refused")
        self.assertIn("v1_live_busy", decision["reason"])

    def test_pass_at_t0_but_fail_on_requote_is_counted_as_false_t0(self):
        """The scan-time spread is not the path's profit (spec section 5.3)."""
        decision = self._decide(kraken_bid=D("99"))
        self.assertEqual(decision["state"], "false_t0")
        self.assertIsNotNone(decision["net_usdt"])
        self.assertLess(decision["net_usdt"], D("0"))

    def test_a_row_that_never_passed_at_t0_is_a_plain_refusal(self):
        row = self._row(edge_t0_bps="3")
        decision = self._decide(row=row, kraken_bid=D("99"))
        self.assertEqual(decision["state"], "refused")

    def test_no_book_at_size_is_not_a_zero_price(self):
        self.assertEqual(self._decide(kraken_bid=None)["reason"], "no_book_at_size")
        self.assertEqual(self._decide(tokens=None)["reason"], "no_book_at_size")

    def test_unknown_cost_refuses_instead_of_pricing_it_as_zero(self):
        decision = self._decide(transfer_usdt=None)
        self.assertEqual(decision["state"], "refused")
        self.assertEqual(decision["reason"], "cost_unknown")

    def test_a_stale_scan_without_fresh_legs_refuses(self):
        """A scan is a snapshot, not a live price (lessons 2.1).

        Without a fresh buy quote and a fresh book there is nothing to decide on,
        and the stale snapshot must not stand in for them.
        """
        decision = self._decide(scan_fresh=False, tokens=None)
        self.assertEqual(decision["state"], "refused")
        self.assertEqual(decision["reason"], "scan_stale")

    def test_a_stale_scan_with_fresh_legs_decides_on_the_fresh_numbers(self):
        """The re-quote is the measurement; the snapshot only picked the row.

        Measured 2026-10-07: a round takes ~40 s against a 15 s window, so the
        old rule refused 13-15 rows every round and the chain -> Kraken family
        could never be paper-tested (v2: 1016 refused, 0 fills).
        """
        decision = self._decide(scan_fresh=False)
        self.assertIn(decision["state"], ("paper", "false_t0"))
        self.assertTrue(decision["fresh_legs"])
        self.assertEqual(decision["scan_used_for"], "candidate_selection")


class KrakenSellLegTest(unittest.TestCase):
    def test_still_open_at_5s_is_not_a_fill(self):
        """Lesson 3.5: the first read says open, not unfilled."""
        state = legs_kraken.settle_state(
            placed_ms=0, polls=[(5_000, legs_kraken.OPEN)])
        self.assertEqual(state, legs_kraken.UNKNOWN)

    def test_a_late_fill_is_recognised(self):
        state = legs_kraken.settle_state(
            placed_ms=0, polls=[(5_000, legs_kraken.OPEN), (90_000, legs_kraken.FILLED)])
        self.assertEqual(state, legs_kraken.FILLED)

    def test_open_past_the_window_is_open(self):
        state = legs_kraken.settle_state(
            placed_ms=0, polls=[(5_000, legs_kraken.OPEN), (130_000, legs_kraken.OPEN)])
        self.assertEqual(state, legs_kraken.OPEN)

    def test_unreadable_order_is_unknown_not_open(self):
        state = legs_kraken.settle_state(
            placed_ms=0, polls=[(5_000, legs_kraken.UNKNOWN)], timeout_ms=1)
        self.assertEqual(state, legs_kraken.UNKNOWN)

    def test_order_state_reads_kraken_status(self):
        self.assertEqual(
            legs_kraken.order_state({"status": "closed", "vol": "0.06", "vol_exec": "0.06"}),
            legs_kraken.FILLED)
        self.assertEqual(legs_kraken.order_state({"status": "open"}), legs_kraken.OPEN)
        self.assertEqual(legs_kraken.order_state(None), legs_kraken.UNKNOWN)

    def test_dust_is_not_a_deposit(self):
        self.assertFalse(legs_kraken.credited_units(
            before=D("0.500000"), after=D("0.500001"), minimum=D("0.05459063574")))
        self.assertTrue(legs_kraken.credited_units(
            before=D("0.500000"), after=D("0.554591"), minimum=D("0.05459063574")))
        self.assertFalse(legs_kraken.credited_units(
            before=None, after=D("1"), minimum=D("0.05")))


class LedgerTest(unittest.TestCase):
    def test_held_at_only_accepts_known_places(self):
        entry = ledger.row(symbol="A", chain="solana", family="chain_to_kraken",
                           state=ledger.UNKNOWN, mode="paper", size_usdt=D("25"),
                           held_at="maybe_gone")
        self.assertEqual(entry["held_at"], "")
        kept = ledger.row(symbol="A", chain="solana", family="chain_to_kraken",
                          state=ledger.STOPPED, mode="paper", size_usdt=D("25"),
                          held_at="in_bridge")
        self.assertEqual(kept["held_at"], "in_bridge")

    def test_a_ledger_row_is_json_serialisable_without_a_default(self):
        """A ledger that cannot be printed is a ledger nobody reads."""
        import json

        entry = ledger.row(symbol="A", chain="solana", family="chain_to_kraken",
                           state=ledger.PAPER, mode="paper", size_usdt=D("25"),
                           tokens=D("0.123"), net_usdt=D("0.15"),
                           costs={"kraken_fee_usdt": D("0.02")})
        text = json.dumps(entry)
        self.assertIn("0.123", text)
        self.assertIn("0.15", text)

    def test_open_positions_track_the_last_word_per_symbol(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "cycles.jsonl"
            ledger.record(ledger.row(symbol="A", chain="solana", family="f",
                                     state=ledger.SOLD, mode="live", size_usdt=D("25")), path)
            ledger.record(ledger.row(symbol="B", chain="solana", family="f",
                                     state=ledger.UNKNOWN, mode="live", size_usdt=D("25"),
                                     held_at="kraken"), path)
            self.assertEqual([row["symbol"] for row in ledger.open_positions(path)], ["B"])
            ledger.record(ledger.row(symbol="B", chain="solana", family="f",
                                     state=ledger.SOLD, mode="live", size_usdt=D("25")), path)
            self.assertEqual(ledger.open_positions(path), [])

    def test_a_paper_refusal_does_not_erase_a_real_position(self):
        """Paper rows share the file. They are not inventory.

        Measured 2026-10-07: after the NVDAx probe landed, the next paper round
        wrote ``refused`` rows for NVDAx and ``open_positions`` dropped it --
        while 0.0065 NVDAx sat in our Solana wallet.
        """
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "cycles.jsonl"
            ledger.record(ledger.row(symbol="NVDAx", chain="x-layer", family="f",
                                     state=ledger.PROBED, mode="live",
                                     size_usdt=D("1.54"), held_at="dest_wallet"), path)
            ledger.record(ledger.row(symbol="NVDAx", chain="x-layer", family="f",
                                     state=ledger.REFUSED, mode="paper",
                                     size_usdt=D("200")), path)
            self.assertEqual([row["symbol"] for row in ledger.open_positions(path)],
                             ["NVDAx"])
            self.assertIn("NVDAx", ledger.carried_inventory(path))
            # And the paper view is still reachable when someone asks for it.
            self.assertEqual(ledger.open_positions(path, modes=("paper",)), [])

    def test_a_position_older_than_the_tail_window_is_still_open(self):
        """``recent()`` is a 50 line tail. One scan round is bigger than that."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "cycles.jsonl"
            ledger.record(ledger.row(symbol="NVDAx", chain="x-layer", family="f",
                                     state=ledger.PROBED, mode="live",
                                     size_usdt=D("1.54"), held_at="dest_wallet"), path)
            for index in range(80):
                ledger.record(ledger.row(symbol=f"P{index}", chain="solana",
                                         family="f", state=ledger.REFUSED,
                                         mode="paper", size_usdt=D("200")), path)
            self.assertEqual([row["symbol"] for row in ledger.open_positions(path)],
                             ["NVDAx"])

    def test_last_holding_clears_after_a_clean_close(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "cycles.jsonl"
            self.assertIsNone(ledger.last_holding(path))
            ledger.record(ledger.row(symbol="A", chain="eth", family="f",
                                     state=ledger.UNKNOWN, mode="live",
                                     size_usdt=D("25")), path)
            self.assertEqual(ledger.last_holding(path)["state"], ledger.UNKNOWN)


class V1CoexistenceTest(unittest.TestCase):
    def test_locks_and_processes_both_count_as_busy(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "data" / "xstocks_eth_cycle.lock").write_text("1")
            busy, why = cycle_core.v1_busy(root, check_processes=False)
            self.assertTrue(busy)
            self.assertIn("xstocks_eth_cycle.lock", why)

    def test_a_running_v1_chain_is_busy(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            busy, why = cycle_core.v1_busy(
                root, runner=lambda *a, **k: FakeResult(0, "12345\n"))
            self.assertTrue(busy)
            self.assertIn("xstocks_auto_live.py", why)

    def test_missing_pgrep_is_busy_not_idle(self):
        """The v1 runner once reported 'not running' from a sandbox that could
        not see processes at all, so an unknown answer must fail closed."""
        def boom(*_args, **_kwargs):
            raise FileNotFoundError("pgrep")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            busy, why = cycle_core.v1_busy(root, runner=boom)
            self.assertTrue(busy)
            self.assertEqual(why, "pgrep unavailable")

    def test_clear_field_is_not_busy(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            busy, why = cycle_core.v1_busy(root, runner=lambda *a, **k: FakeResult(1))
            self.assertFalse(busy)
            self.assertEqual(why, "")

    def test_halt_paths_live_in_v2_own_directory(self):
        paths = cycle_core.halt_paths("/tmp/v2data")
        self.assertEqual(paths["halt"].name, "xstocks_v2_HALT")
        self.assertEqual(paths["lock"].name, "xstocks_v2_cycle.lock")
        self.assertNotEqual(str(paths["halt"].parent), "/home/operator/Documents/arb/data")


if __name__ == "__main__":
    unittest.main()
