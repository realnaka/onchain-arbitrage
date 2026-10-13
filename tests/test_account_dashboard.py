from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import time
import unittest

from arb_monitor.apps.account_dashboard import (account_html, build_snapshot,
                                                exposure_section, grey_scale_section,
                                                load_toml, risk_parameters,
                                                token_universe, trade_universe)
from arb_monitor.execution.ledger import ExecutionLedger
from arb_monitor.execution.okx_account import OKXReadError, OKXReadOnlyClient
from arb_monitor.execution.wallet_cli import OnchainOSRunner, WalletCliError

ROOT = Path(__file__).resolve().parents[1]
XIAOMI = "0x076cf393e701839fc7a5832d2c68aafa235682ae"
USDG = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"


def fake_okx_transport(url: str, headers: dict):
    """Minimal OKX v5 read-only responder for shape/limit tests."""
    if url.endswith("/api/v5/account/balance"):
        return {"code": "0", "data": [{"details": [
            {"ccy": "USDT", "availBal": "10.00584115", "eq": "10.00584115",
             "eqUsd": "10.00", "frozenBal": "0"},
            {"ccy": "X", "availBal": "0", "eq": "0", "eqUsd": "0", "frozenBal": "0"},
        ]}]}
    if "/api/v5/account/positions" in url:
        return {"code": "0", "data": [
            {"instId": "XIAOMI-USDT-SWAP", "pos": "-1", "avgPx": "3.37",
             "markPx": "3.366", "notionalUsd": "3.37", "upl": "0.003",
             "lever": "2", "mgnMode": "cross", "liqPx": "", "cTime": "1"},
            # A historical zero row is not exposure.
            {"instId": "XIAOMI-USDT-SWAP", "pos": "0", "avgPx": "", "markPx": ""},
        ]}
    if "/api/v5/trade/orders-pending" in url:
        return {"code": "0", "data": []}
    if "/api/v5/trade/fills" in url:
        return {"code": "0", "data": [
            {"instId": "XIAOMI-USDT-SWAP", "side": "sell", "fillSz": "1",
             "fillPx": "3.37", "fee": "-0.0015165", "feeCcy": "USDT",
             "execType": "T", "ts": "1789470426640", "ordId": "1",
             "clOrdId": "arb1", "fillPnl": "0"},
        ]}
    if "/api/v5/account/trade-fee" in url:
        return {"code": "0", "data": [{"level": "VIP1", "takerU": "-0.00045",
                                       "makerU": "-0.00016"}]}
    return {"code": "0", "data": []}


class OkxSectionTests(unittest.TestCase):
    def client(self, transport=fake_okx_transport):
        return OKXReadOnlyClient("k", "s", "p", simulated=False, transport=transport)

    def test_reads_balance_positions_fills_and_the_real_taker_rate(self):
        snapshot = build_snapshot(client=self.client(), runner=None)
        okx = snapshot["okx"]
        self.assertEqual(okx["status"], "ok")
        # Zero balances are dropped instead of padding the table with noise.
        self.assertEqual([row["ccy"] for row in okx["balances"]], ["USDT"])
        self.assertEqual(len(okx["positions"]), 1)
        self.assertEqual(okx["positions"][0]["direction"], "short")
        self.assertEqual(okx["fees"][0]["taker_bps"], "4.50000")
        self.assertEqual(okx["fees"][0]["level"], "VIP1")
        # Fee in bps is derived from the fill, not from the fee schedule.
        self.assertEqual(okx["recent_fills"][0]["fee_bps"], "4.50000")
        self.assertEqual(okx["recent_fills"][0]["exec_type"], "T")

    def test_a_failed_read_is_reported_as_error_and_never_invented(self):
        def broken(url, headers):
            raise TimeoutError("no route to host")

        snapshot = build_snapshot(client=self.client(broken), runner=None)
        self.assertEqual(snapshot["okx"]["status"], "error")
        self.assertIn("TimeoutError", snapshot["okx"]["error"])
        self.assertNotIn("balances", snapshot["okx"])

    def test_missing_credentials_fail_closed(self):
        client = OKXReadOnlyClient("", "", "", transport=fake_okx_transport)
        self.assertFalse(client.configured)
        with self.assertRaises(OKXReadError):
            client.balances()


class WalletSectionTests(unittest.TestCase):
    def fake_runner(self):
        def run(command, capture_output=True, text=True, timeout=30, env=None):
            payload = ('{"ok":true,"data":{"details":[{"tokenAssets":'
                       '[{"balance":"6.999199","decimal":"6","rawBalance":"6999199",'
                       '"symbol":"USDG","tokenAddress":"' + USDG + '",'
                       '"tokenPrice":"1","usdValue":"6.999199","chainIndex":"196"},'
                       '{"balance":"1.170314949169009549","decimal":"18",'
                       '"rawBalance":"1170314949169009549","symbol":"wXIAOx",'
                       '"tokenAddress":"' + XIAOMI + '","tokenPrice":"3.419967495935478",'
                       '"usdValue":"4.00243908","chainIndex":"196"}]}]}'
                       ',"totalValueUsd":"11.00"}' if "balance" in command
                       else '{"ok":true,"data":{"xlayer":[{"address":'
                            '"0xabababababababababababababababababababab",'
                            '"chainIndex":"196","chainName":"okb"}]}}')
            class Done:
                returncode = 0
                stdout = payload
                stderr = ""
            return Done()
        return OnchainOSRunner(binary="/bin/true", run=run, environ={})

    def test_wallet_balances_and_address_are_normalised(self):
        snapshot = build_snapshot(client=None, runner=self.fake_runner())
        wallet = snapshot["wallet"]
        self.assertEqual(wallet["status"], "ok")
        self.assertEqual(wallet["address"], "0xabababababababababababababababababababab")
        self.assertEqual([row["symbol"] for row in wallet["balances"]], ["USDG", "wXIAOx"])
        self.assertEqual(snapshot["exposure"]["spot"][1]["asset"], "XIAOMI")

    def test_unconfigured_sources_stay_visible_as_unconfigured(self):
        snapshot = build_snapshot(client=None, runner=None)
        self.assertEqual(snapshot["okx"]["status"], "unconfigured")
        self.assertEqual(snapshot["wallet"]["status"], "unconfigured")

    def test_a_dead_wallet_runner_does_not_break_the_page(self):
        def run(*args, **kwargs):
            raise OSError("no such file")
        snapshot = build_snapshot(client=None,
                                  runner=OnchainOSRunner(binary="/nope", run=run, environ={}))
        self.assertEqual(snapshot["wallet"]["status"], "error")
        self.assertTrue(account_html(snapshot).startswith("<!doctype html>"))


class ExposureTests(unittest.TestCase):
    def test_a_short_contract_offsets_the_token_it_hedges(self):
        universe = token_universe(str(ROOT / "config/hk_monitor.toml"))
        wallet = {"balances": [{"symbol": "wXIAOx", "token_address": XIAOMI,
                                "balance": "1.170314949169009549",
                                "price_usd": "3.42", "usd_value": "4.00"}]}
        okx = {"positions": [{"instrument": "XIAOMI-USDT-SWAP", "size": "-1"}]}
        exposure = exposure_section(wallet, okx, universe, {"XIAOMI-USDT-SWAP": "1"})
        row = exposure["unhedged"][0]
        self.assertEqual(row["token_held"], "1.170314949169009549")
        self.assertEqual(row["token_shorted"], "1")
        self.assertEqual(row["net_exposure_tokens"], "0.170314949169009549")
        self.assertFalse(row["flat"])

    def test_spot_average_and_floating_pnl_use_the_wallet_price(self):
        universe = token_universe(str(ROOT / "config/hk_monitor.toml"))
        wallet = {"balances": [{"symbol": "wMRNAx", "token_address": "0xce0fbc16e820ab7fd6d2936f1533c2654ad49ae9",
                                "balance": "0.56", "price_usd": "187.88",
                                "usd_value": "105.2128"}]}
        okx = {"positions": [{"instrument": "MRNA-USDT-SWAP", "size": "-0.56",
                              "mark_price": "177.4", "unrealized_pnl": "-0.0336"}]}
        exposure = exposure_section(
            wallet, okx, universe, {"MRNA-USDT-SWAP": "1"},
            {"MRNA": Decimal("94.440592")})
        row = exposure["unhedged"][0]
        self.assertEqual(Decimal(row["spot_avg_price"]).quantize(Decimal("0.0001")),
                         Decimal("168.6439"))
        self.assertEqual(Decimal(row["spot_floating_pnl_usd"]).quantize(Decimal("0.0001")),
                         Decimal("10.7722"))
        spot = exposure["spot"][0]
        self.assertEqual(spot["open_avg_price"], row["spot_avg_price"])
        page = account_html({"exposure": exposure, "wallet": wallet, "okx": okx,
                             "grey_scale": {}, "risk": {}, "ledger": {}, "gas": {}})
        self.assertIn("168.64391", page)
        self.assertIn("现货浮动盈亏", page)
        from arb_monitor.apps.board import tabbed_html
        from arb_monitor.apps.account_dashboard import holdings_banner_html
        shell = tabbed_html(market_html="", account_html_text="",
                            holdings_html=holdings_banner_html({"exposure": exposure}))
        self.assertIn("现货开仓均价", shell)
        self.assertIn("两腿合计浮动盈亏", shell)

    def test_an_unknown_multiplier_is_not_assumed_to_be_one(self):
        universe = token_universe(str(ROOT / "config/hk_monitor.toml"))
        wallet = {"balances": []}
        okx = {"positions": [{"instrument": "XIAOMI-USDT-SWAP", "size": "-1"}]}
        exposure = exposure_section(wallet, okx, universe, {})
        self.assertIsNone(exposure["perp"][0]["token_equivalent"])
        row = exposure["unhedged"][0]
        self.assertFalse(row["multiplier_known"])
        self.assertIsNone(row["token_shorted"])
        # An open contract is never reported as flat just because we cannot
        # convert it into tokens.
        self.assertFalse(row["flat"])


class RiskParameterTests(unittest.TestCase):
    def test_limits_come_from_the_committed_config(self):
        risk = risk_parameters(str(ROOT / "config/automation_policy.toml"),
                               str(ROOT / "config/hk_monitor.toml"))
        # The gray-scale ceiling is the operator's number and it is expected to
        # change ("跑一段时间再改"), so the assertion is that the board reports
        # the committed value — not that it reports a particular one.
        committed = load_toml(ROOT / "config/automation_policy.toml")
        self.assertEqual(risk["limits"]["per_trade_quote_usdt"],
                         committed["limits"]["per_trade_quote_usdt"])
        self.assertEqual(risk["limits"]["daily_quote_usdt"],
                         committed["limits"]["daily_quote_usdt"])
        self.assertEqual(risk["limits"]["max_unhedged_tokens"], "0")
        self.assertEqual(risk["limits"]["max_unhedged_bps"],
                         committed["limits"]["max_unhedged_bps"])
        self.assertEqual(risk["limits"]["max_open_contracts"], "1")
        self.assertTrue(risk["automation"]["spot_must_confirm_before_hedge"])
        self.assertEqual(risk["monitor"]["min_net_edge_bps"], "20")
        self.assertIn("XIAOMI", risk["allowlist"]["assets"])
        self.assertEqual(risk["automation"]["kill_switch_file"], "data/KILL_SWITCH")

    def test_kill_switch_file_presence_is_shown_not_guessed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "KILL_SWITCH"
            risk = risk_parameters(str(ROOT / "config/automation_policy.toml"),
                                   str(ROOT / "config/hk_monitor.toml"),
                                   kill_switch_file=str(path))
            self.assertFalse(risk["automation"]["kill_switch_tripped"])
            path.write_text("stop")
            risk = risk_parameters(str(ROOT / "config/automation_policy.toml"),
                                   str(ROOT / "config/hk_monitor.toml"),
                                   kill_switch_file=str(path))
            self.assertTrue(risk["automation"]["kill_switch_tripped"])


class PageTests(unittest.TestCase):
    def test_page_shows_rules_positions_and_the_source_states(self):
        snapshot = build_snapshot(client=OkxSectionTests().client(),
                                  runner=WalletSectionTests().fake_runner())
        page = account_html(snapshot)
        self.assertIn("账户与风控看板", page)
        self.assertIn("XIAOMI-USDT-SWAP", page)
        self.assertIn("X Layer 钱包：ok", page)
        self.assertIn("单笔上限（USDT）", page)
        self.assertIn("kill switch", page.lower())
        # The page is a viewer only.
        self.assertIn("页面不提供下单入口", page)


class GreyScaleTests(unittest.TestCase):
    """The gray-scale ceiling is on the board, with what is left of it today.

    Operator's instruction 2026-09-16: "现在先保持10Usd，在面板上显示出来这个参数".
    The board has to answer three questions without a lookup: what the policy
    allows, what a trade will actually be, and how much of today is left.
    """

    def test_the_ceiling_the_affordable_trade_and_today_are_all_shown(self):
        # "How much of today is left" is a query over the execution ledger. The
        # live one is ``data/execution.db``, which a fresh clone does not have,
        # so this test used to get ``today_real_used_quote = None`` on CI while
        # passing on the trading Mac. Write one real fill into a temporary
        # ledger instead: same code path, no dependency on the operator's box.
        with TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "execution.db"
            book = ExecutionLedger(ledger_path)
            try:
                book.append(group_id="g1", state="filled", event_type="spot_filled",
                            payload={"leg": "buy", "quote_amount": "3"},
                            created_ts_ms=int(time.time() * 1000))
            finally:
                book.close()
            grey = grey_scale_section(
                ledger_path=str(ledger_path),
                wallet_balances_rows=[
                    {"symbol": "USDG", "token_address": USDG.lower(),
                     "balance": "10.935337"}])
        limits = risk_parameters()["limits"]
        self.assertEqual(grey["per_trade_quote_usdt"], limits["per_trade_quote_usdt"])
        self.assertEqual(grey["daily_quote_usdt"], limits["daily_quote_usdt"])
        # Affordable = min(ceiling, wallet × 0.999): the plan search then rounds
        # down to whole shares, so the trade is never above this number.
        self.assertEqual(Decimal(grey["affordable_trade_quote"]),
                         min(Decimal(grey["per_trade_quote_usdt"]),
                             Decimal("10.935337") * Decimal("0.999")))
        self.assertEqual(Decimal(grey["today_real_used_quote"]), Decimal("3"))
        self.assertIn("灰度额度", grey["note"])

    def test_real_and_modelled_spend_are_reported_separately(self):
        grey = grey_scale_section(wallet_balances_rows=[])
        self.assertIn("today_real_used_quote", grey)
        self.assertIn("today_paper_used_quote", grey)
        # A wallet read that failed leaves the affordable number unknown rather
        # than inventing a size.
        self.assertIsNone(grey["affordable_trade_quote"])

    def test_the_board_renders_the_gray_scale_rows(self):
        snapshot = build_snapshot(client=OkxSectionTests().client(),
                                  runner=WalletSectionTests().fake_runner())
        page = account_html(snapshot)
        self.assertIn("灰度单笔上限（quote）", page)
        self.assertIn("灰度每日上限（quote）", page)
        self.assertIn("灰度实际单笔（min(上限, 钱包)）", page)
        self.assertIn(snapshot["grey_scale"]["per_trade_quote_usdt"], page)


class TradeUniverseTests(unittest.TestCase):
    """Monitored and auto-executable must be two visibly different lists.

    Operator asked for this on the board (2026-09-16): "你现在在监控的，和可能
    自动执行的是那些合约和标的？列出来看看。也列在监控网页吧。"
    """

    def test_only_allowlisted_venues_with_verified_mappings_can_be_traded(self):
        universe = trade_universe()
        traded = {(row["venue"], row["instrument"])
                  for row in universe["auto_executable"]}
        self.assertIn(("okx", "XIAOMI-USDT-SWAP"), traded)
        self.assertIn(("okx", "SHEIN-USDT-SWAP"), traded)
        self.assertIn(("okx", "POPMART-USDT-SWAP"), traded)
        # Gate and Binance are monitored, never executed: no adapter exists.
        self.assertFalse([row for row in traded if row[0] in {"gate", "binance"}])
        for row in universe["auto_executable"]:
            self.assertTrue(row["mapping_verified"])

    def test_every_monitored_instrument_names_its_asset_and_its_blocker(self):
        universe = trade_universe()
        self.assertEqual(universe["counts"]["monitored"],
                         len(universe["monitor_only"]))
        for row in universe["monitor_only"]:
            self.assertIsNotNone(row["asset"], row["instrument"])
            if not row["executable"]:
                self.assertTrue(row["blocked_by"])

    def test_the_board_shows_both_lists(self):
        page = account_html({"universe": trade_universe(), "grey_scale": {},
                             "okx": {}, "wallet": {}, "exposure": {}, "risk": {},
                             "ledger": {}, "gas": {}})
        self.assertIn("监控范围 vs 可自动执行", page)
        self.assertIn("okx:XIAOMI-USDT-SWAP", page)
        self.assertIn("该交易所没有已接入的执行适配器", page)
        self.assertIn("可自动下单", page)

    def test_the_market_dashboard_carries_the_same_line(self):
        from arb_monitor.apps.hk_monitor import dashboard_html

        page = dashboard_html({"execution_universe": trade_universe(),
                               "instruments": [], "probes": [],
                               "token_references": [], "status": "ok"})
        self.assertIn("现货 Ask", page)
        self.assertIn("合约 Bid", page)
        self.assertNotIn("链上现货明细", page)


class PricePrecisionTests(unittest.TestCase):
    """Every price on the boards shows five decimals — no more, no less.

    Operator's instruction, 2026-09-16: "价格保留5位小数已经足够". Before this,
    the market board used 4 in some columns and 6 in others, so the same number
    looked different depending on which table you read.
    """

    def test_market_board_shows_the_two_traded_prices_at_four_decimals(self):
        from arb_monitor.apps.hk_monitor import dashboard_html

        summary = {
            "status": "ok", "hk_time": "18:30",
            "price_screen": {"rows": [{
                "asset": "XIAOMI", "instrument": "XIAOMI-USDT-SWAP",
                "okx_bid": "3.349123456789",
                "spot_ask_usd": "3.353577333333333",
                "display_basis_bps": "-13.284567",
            }]},
            "token_references": [],
            "probes": [],
        }
        page = dashboard_html(summary)
        self.assertIn("3.3491", page)
        self.assertIn("3.3536", page)
        self.assertIn("-13.2846", page)
        self.assertNotIn("3.349123456789", page)
        self.assertNotIn("3.353577333333333", page)

    def test_the_funding_rate_keeps_its_own_precision(self):
        # A funding rate is a rate, not a price: at 5 decimals a typical
        # 4.99e-4 would render as 0.00050 and hide the tick.
        from arb_monitor.apps.hk_monitor import dashboard_html

        page = dashboard_html({
            "status": "ok",
            "instruments": [],
            "token_references": [{
                "asset": "XIAOMI",
                "onchain_pool_ask_usd": "3.35",
                "directions": {"long_spot_short_perp": {
                    "perp_price_usd": "3.36",
                    "perp": {"instrument": "XIAOMI-USDT-SWAP"},
                    "gross_entry_basis_bps": "1",
                }},
                "perp_quotes": [{"venue": "okx", "instrument": "XIAOMI-USDT-SWAP",
                                 "bid": "3.35", "ask": "3.36",
                                 "funding_rate": "0.0004987703675489",
                                 "book_age_ms": 1200, "fresh": True}]}],
            "probes": []})
        self.assertNotIn("0.0004987703675489", page)

    def test_account_board_prices_use_five_decimals_theoretically(self):
        from arb_monitor.apps.account_dashboard import _fmt

        self.assertEqual(_fmt("3.349123456789"), "3.34912")
        # Quantities and bps keep their own precision: a token balance is not a
        # price, and rounding it to five places would hide dust.
        self.assertEqual(_fmt("3.000751839364783311", 9), "3.000751839")
        self.assertEqual(_fmt("11.29", 2), "11.29")


if __name__ == "__main__":
    unittest.main()
