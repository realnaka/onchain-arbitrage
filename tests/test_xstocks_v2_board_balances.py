"""Hermetic tests for the account-balance panel of the v2 board.

Operator, 2026-10-07: the panel was unreadable. It listed every X Layer row
with 8 decimals, so 23 rows of ``0.00000000`` buried the two rows that were a
real position.

Three invariants are pinned here:

1. A failed read prints 「读不到」 and never a 0 that looks like an empty wallet.
2. A wallet whose cash could not be read is left out of the headline total
   instead of being counted as nothing.
3. Zero and dust rows are folded, not deleted -- hidden money is a worse bug
   than a noisy panel.

No network, no venue, no clock: every snapshot is built here.
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path


def _board():
    """The read-only board, loaded the way it is run (it is a script)."""
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "v2board", root / "scripts" / "xstocks_v2_board.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(asset, amount, *, error="", note="", raw=None):
    return {"asset": asset, "amount": amount, "raw": raw,
            "readable": amount is not None, "error": error, "note": note}


def _group(chain, rows, *, label="", account="0xabc", **extra):
    return {"chain": chain, "account": account, "label": label or chain,
            "rows": rows, **extra}


class BalancePanelTest(unittest.TestCase):
    def _panel(self, groups, *, age_ms=1000):
        board = _board()
        now_ms = 1_800_000_000_000
        snapshot = ({"groups": groups} if groups is not None else
                    {"error": "rpc timeout"})
        snapshot["as_of_ms"] = now_ms - age_ms
        return board._balances_section(snapshot, now_ms)

    def test_zero_rows_fold_away_instead_of_filling_the_card(self):
        rows = [_row("USDG", "52.092093"), _row("OKB", "0.00139686")]
        rows += [_row(f"X{i}x", "0") for i in range(23)]
        page = self._panel([_group("x-layer", rows, label="X Layer")])
        self.assertIn("其余 23 项为 0", page)
        self.assertNotIn("0.00000000", page)
        # The zero-only symbols are still listed, just folded.
        self.assertIn("X0x", page)
        self.assertLess(page.index("其余 23 项为 0"), page.index("X0x"))
        self.assertIn("52.092093", page)

    def test_a_failed_read_never_looks_like_an_empty_wallet(self):
        page = self._panel([_group("kraken", [
            _row("ZUSD", None, error="EFunding:No funding method"),
            _row("USDC", "0.1")])])
        self.assertIn("读不到", page)
        self.assertIn("EFunding:No funding method", page)
        self.assertNotIn("0.00000000", page)


class MultiChainPanelTest(unittest.TestCase):
    """Every chain we can hold money on gets a card (2026-10-09).

    The panel used to show X Layer, Solana and Kraken only, so the USDC and ETH
    sitting on Ethereum were invisible. The wallet service's USD valuation is the
    one number on the page that can see an xStock or a gas token; it must be
    labelled as a venue quote, and a chain it could not read must count as
    unreadable -- never as an empty wallet.
    """

    def _panel(self, groups, *, age_ms=1000):
        board = _board()
        now_ms = 1_800_000_000_000
        snapshot = {"as_of_ms": now_ms - age_ms, "groups": groups}
        return board._balances_section(snapshot, now_ms)

    def test_each_chain_card_prints_the_services_usd_total(self):
        page = self._panel([
            _group("x-layer", [_row("USDG", "43.797561")], label="X Layer",
                   usd_total="44.05", source="indexer"),
            _group("eth", [_row("USDC", "109.6774"), _row("ETH", "0.001310896")],
                   label="Ethereum", usd_total="112.96", source="indexer"),
            _group("bsc", [_row("USDT", "2.469576542")], label="BNB Chain",
                   usd_total="3.67", source="indexer"),
        ])
        self.assertIn("Ethereum", page)
        self.assertIn("≈ 112.96 USD", page)
        self.assertIn("≈ 3.67 USD", page)
        # The headline is the wallet service's own sum, and says so.
        self.assertIn("全部持仓", page)
        self.assertIn("≈ 160.68 USD", page)
        self.assertIn("指数报价", page)

    def test_a_chain_it_could_not_read_lowers_the_total_and_says_so(self):
        page = self._panel([
            _group("eth", [_row("USDC", "109.6774")], label="Ethereum",
                   usd_total="109.6774", source="indexer"),
            _group("hyperevm", [_row("(整链)", None, error="timeout")],
                   label="HyperEVM", note="读不到 ≠ 0", source="indexer"),
        ])
        self.assertIn("≈ 109.6774 USD", page)
        self.assertIn("另有 1 条链读不到，合计偏低", page)
        self.assertIn("读不到 ≠ 0", page)

    def test_a_snapshot_without_the_service_renders_exactly_as_before(self):
        page = self._panel([_group("x-layer", [_row("USDG", "52.09")],
                                   label="X Layer")])
        self.assertIn("52.09", page)
        self.assertNotIn("全部持仓 <b>", page)
        self.assertNotIn("bal-usd", page)

    def test_the_headline_total_skips_a_wallet_it_could_not_read(self):
        page = self._panel([
            _group("x-layer", [_row("USDG", "52.092093")], label="X Layer"),
            _group("kraken", [_row("ZUSD", None, error="timeout"),
                              _row("USDC", "0.1")], label="Kraken")])
        # Only the readable wallet is added: 52.092093, never "52.19".
        self.assertIn(">52.092093</b>", page)
        self.assertNotIn("52.192093", page)
        self.assertIn("合计可能偏低", page)

    def test_dust_is_kept_but_marked(self):
        page = self._panel([_group("x-layer", [
            _row("USDG", "52"), _row("SPYx 原生", "0.000000000000000001")])])
        self.assertIn("1 项为 dust", page)
        self.assertIn("<span class='dust'>SPYx 原生</span>", page)

    def test_a_missing_snapshot_says_read_failed(self):
        page = self._panel(None)
        self.assertIn("读不到余额快照", page)
        self.assertIn("不是 0", page)

    def test_every_chain_gets_its_own_row_on_one_surface(self):
        """Operator 2026-10-09: 「颜色、大小、对齐都很乱…宁愿不要那么复杂」.

        The card grid with a colour per chain is gone; one aligned row per chain
        on a single surface replaces it. The colour mark must not come back.
        """
        page = self._panel([
            _group("x-layer", [_row("USDG", "1")], label="X Layer"),
            _group("solana", [_row("USDT", "2")], label="Solana"),
            _group("kraken", [_row("ZUSD", "3")], label="Kraken")])
        self.assertEqual(page.count("class='bal-row'"), 3)
        self.assertEqual(page.count("class='bal-led'"), 1)
        for label in ("X Layer", "Solana", "Kraken"):
            self.assertIn(label, page)
        self.assertNotIn("--dot:", page)
        self.assertNotIn("bal-card", page)

    def test_the_chain_with_more_money_comes_first(self):
        """Operator 2026-10-09: 「余额多的，排在上面」.

        Rank is the wallet service's USD total. A chain it did not price
        (Kraken) falls back to the stablecoin sum this panel reads itself, and a
        chain nobody could read ranks last -- never dropped, never re-numbered.
        """
        page = self._panel([
            _group("kraken", [_row("ZUSD", "3")], label="Kraken"),
            _group("solana", [_row("USDT", "11")], label="Solana",
                   usd_total="11", source="indexer"),
            _group("eth", [_row("USDC", "111")], label="Ethereum",
                   usd_total="111", source="indexer"),
            _group("monad", [_row("(整链)", None, error="timeout")],
                   label="Monad", note="读不到 ≠ 0", source="indexer"),
            _group("bsc", [_row("USDT", "6")], label="BNB Chain",
                   usd_total="6", source="indexer")])
        labels = ["Ethereum", "Solana", "BNB Chain", "Kraken", "Monad"]
        self.assertEqual(sorted(labels, key=page.index), labels)
        # The ranking reorders; it never removes a chain.
        self.assertEqual(page.count("class='bal-row'"), len(labels))

    def test_the_account_address_is_shortened_but_copyable(self):
        page = self._panel([_group("x-layer", [_row("USDG", "1")],
                                   account="0xabababababababababababababababababababab")])
        self.assertIn("0xabab…ababab", page)
        self.assertIn("data-copy='0xabababababababababababababababababababab'", page)

    def test_an_old_snapshot_is_flagged(self):
        page = self._panel([_group("x-layer", [_row("USDG", "1")])],
                           age_ms=20 * 60 * 1000)
        self.assertIn("可能已过期", page)


class AmountFormatTest(unittest.TestCase):
    def test_trailing_zeros_go_away(self):
        board = _board()
        self.assertEqual(board._fmt_amount("52.09209300"), "52.092093")
        self.assertEqual(board._fmt_amount("1.50000000"), "1.5")
        self.assertEqual(board._fmt_amount("0"), "0")
        self.assertEqual(board._fmt_amount(None), None)

    def test_a_dust_amount_keeps_its_exponent(self):
        board = _board()
        self.assertTrue(board._fmt_amount("0.000000000000000001").endswith("E-18"))

    def test_a_big_balance_keeps_thousands_separators(self):
        board = _board()
        self.assertEqual(board._fmt_amount("1234.5"), "1,234.50")


class WholePageTest(unittest.TestCase):
    """The page is one ``str.format``; a stray brace breaks every section."""

    def test_the_page_renders_with_an_empty_data_dir(self):
        board = _board()
        with tempfile.TemporaryDirectory() as tmp:
            data = board.collect(data_dir=Path(tmp), interval_s=180.0)
            page = board.render(data)
        self.assertIn("<h1>", page)
        self.assertIn("账户余额（按链 / 账户）", page)
        self.assertIn("读不到余额快照", page)

    def test_the_page_renders_a_real_snapshot(self):
        board = _board()
        rows = [_row("USDG", "52.09209303"), _row("AAPLx 原生", "0")]
        snapshot = {"as_of_ms": 1_800_000_000_000,
                    "groups": [_group("x-layer", rows, label="X Layer")]}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / board.BALANCES.name).write_text(json.dumps(snapshot))
            data = board.collect(data_dir=Path(tmp), interval_s=180.0)
            page = board.render(data)
        self.assertIn("52.092093", page)
        self.assertIn("其余 1 项为 0", page)
        self.assertNotIn("{body}", page)


class InflightPanelTest(unittest.TestCase):
    """A bridge in flight and a stuck process must not look the same."""

    def _page(self, lock_text=None, sidecar=None):
        board = _board()
        snapshot = {"generated_ms": 1_800_000_000_000, "phase": "paper_kraken",
                    "session": "regular", "rows": [], "counters": {},
                    "universe": {}, "threshold_bps": "20",
                    "min_net_profit_usdt": "0.1"}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / board.SCAN.name).write_text(json.dumps(snapshot))
            if lock_text is not None:
                (Path(tmp) / board.LOCK.name).write_text(lock_text)
            if sidecar is not None:
                (Path(tmp) / (board.LOCK.name + ".json")).write_text(
                    json.dumps(sidecar))
            data = board.collect(data_dir=Path(tmp), interval_s=180.0,
                                 now_ms=1_800_000_001_000)
            return board.render(data)

    def test_a_live_cycle_shows_its_step_and_age(self):
        import os
        page = self._page(
            lock_text=str(os.getpid()),
            sidecar={"pid": os.getpid(), "symbol": "SPYx", "step": "bridge",
                     "label": "SPYx chain_via_bridge_to_kraken",
                     # ``collect`` is given now_ms = 1_800_000_001_000 below, so
                     # this is exactly 720 s old.
                     "started_ms": 1_800_000_001_000 - 12 * 60 * 1000})
        self.assertIn("在飞：", page)
        self.assertIn("bridge", page)
        self.assertIn("720", page)

    def test_a_lock_with_a_dead_pid_is_called_out(self):
        page = self._page(lock_text="999999")
        self.assertIn("锁还在，但 pid 999999", page)

    def test_no_lock_means_no_banner(self):
        page = self._page()
        self.assertNotIn("在飞：", page)
        self.assertNotIn("锁还在，但 pid", page)


class DeadPoolPanelTest(unittest.TestCase):
    """The pool-thinness panel is evidence now, not a skip list (2026-10-08)."""

    def _page(self, dead):
        board = _board()
        snapshot = {"generated_ms": 1_800_000_000_000, "phase": "paper_kraken",
                    "session": "regular", "round_seconds": 1.0, "rows": [],
                    "counters": {}, "universe": {}, "threshold_bps": "20",
                    "min_net_profit_usdt": "0.1", "dead_pools": dead}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / board.SCAN.name).write_text(json.dumps(snapshot))
            data = board.collect(data_dir=Path(tmp), interval_s=180.0,
                                 now_ms=1_800_000_001_000)
            return board.render(data)

    def test_the_count_and_the_skips_are_on_the_page(self):
        page = self._page({
            "count": 58, "marked_this_round": 23, "expired_this_round": 0,
            "ttl_hours": 6,
            "entries": {"jupiter:solana:MSTRx": {
                "since_ms": 1, "hits": 3, "reason": "pool_evidence",
                "depth_usd": "29.4"}}})
        self.assertIn("薄池 / 异常池（仍然报价）", page)
        self.assertIn("薄池 / 异常池的证据（仍然报价）", page)
        self.assertIn("jupiter:solana:MSTRx", page)
        self.assertIn("29.4", page)
        self.assertIn("本轮标记 23", page)
        self.assertIn("不再跳过报价", page)

    def test_no_dead_pools_means_no_section(self):
        page = self._page({"count": 0, "marked_this_round": 0,
                           "ttl_hours": 6, "entries": {}})
        self.assertNotIn("薄池 / 异常池的证据", page)

    def test_the_evidence_columns_are_on_the_page(self):
        """A verdict nobody can re-read is a verdict nobody can challenge.

        Measured 2026-10-08: the panel promised "当时深度" and every entry said
        "未知", because the scan never recorded it -- one signal (``below_depth``)
        covered three different causes.
        """
        page = self._page({
            "count": 1, "marked_this_round": 1, "expired_this_round": 0,
            "rechecked_this_round": 4, "revived_this_round": 2,
            "ttl_hours": 1.0, "confirm_hits": 2,
            "entries": {"uniswap-v3-x-layer:0x90cf20f3": {
                "since_ms": 1, "hits": 2, "reason": "below_depth",
                "detail": "same_pool_round_trip", "depth_usd": "355158.78",
                "notional_usdt": "200", "round_trip_bps": "9430.17"}}})
        self.assertIn("同一口池买完再卖", page)
        self.assertIn("355,158.78", page)
        self.assertIn("9,430.2 bps", page)
        self.assertIn("连续命中 2 轮", page)
        self.assertIn("本轮复活 2 口", page)


class RailCoveragePanelTest(unittest.TestCase):
    """The third list: rails nobody quoted this round."""

    def _page(self, coverage):
        board = _board()
        snapshot = {"generated_ms": 1_800_000_000_000, "phase": "paper_kraken",
                    "session": "regular", "round_seconds": 1.0, "rows": [],
                    "counters": {}, "universe": {},
                    "threshold_bps": "20", "min_net_profit_usdt": "0.1",
                    "address_requests": [], "address_requests_deferred": [],
                    "rail_coverage": coverage}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / board.SCAN.name).write_text(json.dumps(snapshot))
            data = board.collect(data_dir=Path(tmp), interval_s=180.0,
                                 now_ms=1_800_000_001_000)
            return board.render(data)

    def test_the_unquoted_rails_are_named_with_their_counts(self):
        page = self._page({"rails": 300, "quoted": 44, "missing_count": 2,
                           "missing": [
                               {"symbol": "SOXLx", "hub": "solana",
                                "network_label": "Solana", "has_address": False},
                               {"symbol": "JPMx", "hub": "solana",
                                "network_label": "Solana", "has_address": True}]})
        self.assertIn("有 rail、但本轮没有报价", page)
        self.assertIn("300", page)
        self.assertIn("SOXLx", page)
        self.assertIn("JPMx", page)
        self.assertIn("展开这 2 条", page)

    def test_nothing_missing_means_no_section(self):
        page = self._page({"rails": 300, "quoted": 300, "missing_count": 0,
                           "missing": []})
        self.assertNotIn("有 rail、但本轮没有报价", page)


class RotationPanelTest(unittest.TestCase):
    """The answer to the list above: which slots the blind spot got (2026-10-08)."""

    def _page(self, rotation_section, *, stage2_max=20):
        board = _board()
        snapshot = {"generated_ms": 1_800_000_000_000, "phase": "paper_kraken",
                    "session": "regular", "round_seconds": 1.0, "rows": [],
                    "counters": {}, "universe": {"stage2_max": stage2_max},
                    "threshold_bps": "20", "min_net_profit_usdt": "0.1",
                    "address_requests": [], "address_requests_deferred": [],
                    "rail_coverage": {}, "rotation": rotation_section}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / board.SCAN.name).write_text(json.dumps(snapshot))
            data = board.collect(data_dir=Path(tmp), interval_s=180.0,
                                 now_ms=1_800_000_001_000)
            return board.render(data)

    def test_the_slots_are_named_with_the_queue_behind_them(self):
        page = self._page({
            "slots": 4, "candidates": 166,
            "picked": [{"symbol": "SOXLx", "never_quoted": True, "slots": 1,
                        "last_slot_ms": None, "waited_s": None}],
            "waiting_count": 155, "never_quoted_count": 154,
            "waiting": [{"symbol": "JPMx", "never_quoted": True, "slots": 0,
                         "last_slot_ms": None, "waited_s": None},
                        {"symbol": "NVDAx", "never_quoted": False, "slots": 2,
                         "last_slot_ms": 1_799_988_480_000, "waited_s": 11520}]})
        self.assertIn("轮转名额", page)
        self.assertIn("SOXLx", page)
        self.assertIn("JPMx", page)
        self.assertIn("166", page)          # the rails competing for the slots
        self.assertIn("155", page)          # still waiting
        self.assertIn("154", page)          # never quoted at all
        self.assertIn("从未报过价", page)
        self.assertIn("从未", page)          # the never-quoted timestamp cells
        self.assertIn("3.2 小时", page)      # 11520 s, the longest wait

    def test_no_section_when_no_slot_is_reserved(self):
        page = self._page({"slots": 0, "candidates": 166, "picked": [],
                           "waiting_count": 0, "never_quoted_count": 0,
                           "waiting": []})
        self.assertNotIn("轮转名额", page)


class ChainCoverageTest(unittest.TestCase):
    """A chain whose rows are all blocked must still be visible on the board.

    Measured 2026-10-09: BSC was quoted every round with zero errors (counters
    ``evm_quotes.bsc = 3``, ``evm_quote_errors.bsc = 0``) and the board never
    mentioned it -- only rows that pass the "clean" filter are printed anywhere,
    so an entirely-blocked chain disappears. The operator asked why BSC was
    missing; nothing on the page said it was being priced at all.
    """

    def _page(self, rows):
        board = _board()
        snapshot = {"generated_ms": 1_800_000_000_000, "phase": "paper_kraken",
                    "session": "regular", "round_seconds": 1.0, "rows": rows,
                    "counters": {}, "universe": {},
                    "threshold_bps": "20", "min_net_profit_usdt": "0.1",
                    "address_requests": [], "address_requests_deferred": []}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / board.SCAN.name).write_text(json.dumps(snapshot))
            data = board.collect(data_dir=Path(tmp), interval_s=180.0,
                                 now_ms=1_800_000_001_000)
            return board.render(data), data

    def test_a_fully_blocked_chain_still_shows_up(self):
        row = {"symbol": "MSTRx", "chain": "bsc", "family": "chain_to_kraken",
               "buy_venue": "pancakeswap-v3", "sell_venue": "kraken",
               "buy_price_usd": "12.30", "sell_price_usd": "155.22",
               "edge_t0_bps": "-8188.6", "notional_usdt": "25",
               "blockers": ["chain_out_of_phase", "unmeasured_route",
                            "no_executor_chain", "below_threshold"],
               "executable": False, "errors": []}
        page, data = self._page([row])
        self.assertIn("各链覆盖", page)
        coverage = {item["chain"]: item for item in data["chain_coverage"]}
        self.assertIn("bsc", coverage)
        self.assertEqual(coverage["bsc"]["rows"], 1)
        self.assertFalse(coverage["bsc"]["executable"])
        self.assertIn("bsc", page)
        self.assertIn("chain_out_of_phase", page)


class DeferredAddressRequestTest(unittest.TestCase):
    """Rows that need a Kraken entry but are not one step from tradable.

    Measured 2026-10-07: SOXLx/Solana has a rail and no address in the book, but
    its row is a 414,906 bps thin-pool fake, so the shopping list (whose whole
    meaning is "register this and the route unblocks") correctly left it out --
    and the (asset, network) disappeared from the board entirely.
    """

    def _page(self, *, deferred, requests=None):
        """Through ``collect`` on a real (minimal) scan file, as the board runs."""
        board = _board()
        snapshot = {"generated_ms": 1_800_000_000_000, "phase": "paper_kraken",
                    "session": "regular", "round_seconds": 1.0, "rows": [],
                    "counters": {}, "universe": {},
                    "threshold_bps": "20", "min_net_profit_usdt": "0.1",
                    "address_requests": requests or [],
                    "address_requests_deferred": deferred}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / board.SCAN.name).write_text(json.dumps(snapshot))
            data = board.collect(data_dir=Path(tmp), interval_s=180.0,
                                 now_ms=1_800_000_001_000)
            return board.render(data)

    def test_a_deferred_row_is_shown_with_its_other_blocker(self):
        page = self._page(deferred=[{
            "symbol": "SOXLx", "chain": "solana", "hub": "solana",
            "kraken_key_hint": "solsoxlx1", "network_label": "Solana",
            "address": "AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev",
            "edge_t0_bps": "414906.49", "net_usdt": None,
            "clears_threshold": False, "reason": "below_depth"}])
        self.assertIn("也要登记、但现在没有价差", page)
        self.assertIn("SOXLx", page)
        self.assertIn("已降级为证据", page)

    def test_the_section_is_absent_when_nothing_is_deferred(self):
        page = self._page(deferred=[])
        self.assertNotIn("也要登记、但现在没有价差", page)

    def test_a_hub_without_an_address_prints_the_note_not_an_address(self):
        """Measured 2026-10-09: TON was handed the EVM address."""
        page = self._page(deferred=[], requests=[{
            "symbol": "SPYx", "chain": "ton", "hub": "ton",
            "kraken_key_hint": "tonspyx1", "network_label": "The Open Network",
            "address": "", "address_ready": False,
            "address_note": "还没有 TON 钱包（C1）：Binance Wallet 支持 TON，"
                            "但创建它必须是有 KYC 的 Binance 用户",
            "edge_t0_bps": "30", "net_usdt": "1.2",
            "clears_threshold": True, "reason": "no_address_book"}])
        self.assertIn("缺地址", page)
        self.assertIn("KYC", page)
        self.assertNotIn("0xabababababababababababababababababababab", page)


class HubAddressTest(unittest.TestCase):
    """One address per hub chain; a missing one is reported, never substituted.

    Kraken sends the token on the **hub** chain, so an address from another
    chain is not a registration -- it is a lost withdrawal (todo C1 warns about
    exactly this).
    """

    def test_solana_and_the_evm_hubs_have_an_address(self):
        from arb_monitor.xstocks_v2 import scan
        for hub in ("solana", "eth", "x-layer", "bsc", "arbitrum", "optimism"):
            with self.subTest(hub=hub):
                address, note = scan.hub_withdraw_address(hub)
                self.assertTrue(address.startswith(("0x", "Am")))
                self.assertEqual(note, "")

    def test_ton_has_none_and_says_why(self):
        from arb_monitor.xstocks_v2 import scan
        address, note = scan.hub_withdraw_address(
            "ton", ton_env=Path("/nonexistent/ton.env"))
        self.assertEqual(address, "")
        self.assertIn("TON", note)
        self.assertIn("KYC", note)

    def test_an_existing_ton_address_file_is_the_hub_address(self):
        from arb_monitor.xstocks_v2 import scan
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "ton.env"
            env.write_text('TON_MNEMONIC="not read"\nTON_ADDRESS=UQAbcdef\n')
            address, note = scan.hub_withdraw_address("ton", ton_env=env)
        self.assertEqual(address, "UQAbcdef")
        self.assertEqual(note, "")

    def test_an_unknown_hub_is_reported_as_missing_not_guessed(self):
        from arb_monitor.xstocks_v2 import scan
        for hub in ("mantle", "hyperevm", ""):
            with self.subTest(hub=hub):
                address, note = scan.hub_withdraw_address(hub)
                self.assertEqual(address, "")
                self.assertNotEqual(note, "")


class FundingGapPanelTest(unittest.TestCase):
    """Operator 2026-10-10: a refusal that prints one word is a silent one.

    Measured 2026-10-09: 4,882 of the day's 5,703 ledger rows ended on
    ``funding_short``, and the board said nothing about which chain, which
    ticket, or how far off the wallet was.
    """

    def _entry(self, chain, symbol, gap, state="refused"):
        return {"state": state, "chain": chain, "symbol": symbol, "mode": "paper",
                "reason": "funding_short", "extra": {"funding_gap": gap}}

    def test_rows_are_grouped_by_chain_and_the_smallest_gap_leads(self):
        board = _board()
        cycles = [
            self._entry("solana", "QQQx", {"need_usdt": "25", "have_usdc": "8.4",
                                           "have_usdt": "0.9",
                                           "short_usdt": "15.7"}),
            self._entry("solana", "TSLAx", {"need_usdt": "25", "have_usdc": "8.4",
                                            "have_usdt": "0.9",
                                            "short_usdt": "12.1"}),
            self._entry("eth", "SPYx", {"need_usdt": "25", "have_usdc": "0.02",
                                        "have_usdt": "0.9",
                                        "short_usdt": "24.1"}),
        ]
        gaps = board._funding_gaps(cycles)
        self.assertEqual([item["chain"] for item in gaps], ["solana", "eth"])
        solana = gaps[0]
        self.assertEqual(solana["rows"], 2)
        self.assertEqual(solana["priced_rows"], 2)
        self.assertEqual(solana["short_usdt"], Decimal("12.1"))
        self.assertEqual(solana["symbol"], "TSLAx")

    def test_a_row_without_a_number_is_counted_but_not_priced_as_zero(self):
        board = _board()
        gaps = board._funding_gaps([
            self._entry("solana", "QQQx", {}),
            self._entry("solana", "TSLAx", {"short_usdt": "12.1"}),
        ])
        self.assertEqual(gaps[0]["rows"], 1)          # the empty gap is not a row
        self.assertEqual(gaps[0]["priced_rows"], 1)

    def test_an_entry_without_any_gap_is_invisible(self):
        board = _board()
        self.assertEqual(board._funding_gaps([
            {"state": "refused", "chain": "solana", "reason": "funding_short",
             "extra": {}}]), [])

    def test_the_panel_names_the_chain_and_the_missing_amount(self):
        board = _board()
        page = board._funding_gap_section([
            {"chain": "solana", "rows": 12, "priced_rows": 12, "symbol": "TSLAx",
             "need_usdt": Decimal("25"), "have_usdc": Decimal("8.4"),
             "have_usdt": Decimal("0.9"), "short_usdt": Decimal("12.1"),
             "wallet_chain": "solana"}])
        self.assertIn("solana", page)
        self.assertIn("12.1000", page)
        self.assertIn("8.4000", page)
        self.assertIn("TSLAx", page)

    def test_the_wallet_the_balances_came_from_is_named(self):
        """The row's ``chain`` is where the token lives; the wallet is where the
        money is. Printing one next to the other unnamed is a silent number."""
        board = _board()
        page = board._funding_gap_section([
            {"chain": "eth", "rows": 2, "priced_rows": 2, "symbol": "SPCXx",
             "need_usdt": Decimal("25"), "have_usdc": Decimal("0.0168"),
             "have_usdt": Decimal("0.9287"), "short_usdt": Decimal("24.08"),
             "wallet_chain": "solana"}])
        self.assertIn("solana 0.0168 / 0.9287", page)

    def test_no_gaps_render_nothing_at_all(self):
        board = _board()
        self.assertEqual(board._funding_gap_section([]), "")


if __name__ == "__main__":
    unittest.main()
