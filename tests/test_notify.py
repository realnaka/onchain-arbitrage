from __future__ import annotations

from decimal import Decimal as D
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from arb_monitor.apps.hk_monitor import (dashboard_html, execution_blockers,
                                         load_config)
from arb_monitor.ops.notify import (
    AlertGate, FeishuNotifier, format_alert, format_manual_review,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/hk_monitor.toml"


def candidate(asset="XIAOMI", venue="gate", instrument="XIAOMI_USDT", net="25"):
    return {
        "asset": asset, "perp_venue": venue, "perp_instrument": instrument,
        "spot_ask_usd": D("3.39"), "perp_bid_usd": D("3.40"),
        "gross_entry_basis_bps": D("29.5"), "estimated_entry_cost_bps": D("28"),
        "estimated_net_entry_bps": D(net), "spot_source": "X Layer 池",
        "pool_depth_usd": D("300000"), "cost_verified": False,
        "chain_id": "196", "token_symbol": "wXIAOx",
        "token_address": "0x076cf393e701839fc7a5832d2c68aafa235682ae",
        "quote_token_symbol": "USDC",
        "quote_token_address": "0xb6ceceab302e2e4948951ee7843fc24e92933061",
        "pool_address": "0xdc7f2f41b48cd4f482d8c900ac2fa1b5ad058417",
        "pool_fee_bps": D("5"),
    }


class AlertGateTests(unittest.TestCase):
    def test_negative_net_basis_is_never_announced(self):
        gate = AlertGate(D("20"), cooldown_seconds=900)
        self.assertEqual(gate.evaluate([candidate(net="-3")], 1_000_000), [])

    def test_thin_positive_net_basis_below_threshold_is_not_announced(self):
        gate = AlertGate(D("20"), cooldown_seconds=900)
        self.assertEqual(gate.evaluate([candidate(net="0.5")], 1_000_000), [])
        self.assertEqual(gate.evaluate([candidate(net="19.9")], 1_000_000), [])

    def test_candidate_at_or_above_threshold_is_announced_once_per_cooldown(self):
        gate = AlertGate(D("20"), cooldown_seconds=900)
        first = gate.evaluate([candidate(net="25")], 1_000_000)
        self.assertEqual(len(first), 1)
        gate.mark_sent(first[0], 1_000_000)
        # Same asset+venue inside the cooldown: silent.
        self.assertEqual(gate.evaluate([candidate(net="40")], 1_000_000 + 899_000), [])
        # After the cooldown it may fire again.
        again = gate.evaluate([candidate(net="40")], 1_000_000 + 901_000)
        self.assertEqual(len(again), 1)

    def test_different_venue_is_a_different_alert_key(self):
        gate = AlertGate(D("20"), cooldown_seconds=900)
        first = gate.evaluate([candidate(net="25")], 1_000_000)
        gate.mark_sent(first[0], 1_000_000)
        other = gate.evaluate([candidate(venue="okx", instrument="XIAOMI-USDT-SWAP", net="25")],
                              1_000_000)
        self.assertEqual(len(other), 1)


class AudienceRoutingTests(unittest.TestCase):
    """Signal room vs alert room, and what happens when only one exists.

    Operator's decision 2026-09-16: "报警频道和交易信号频道分开". A stranded leg
    or an automatic halt goes to the alert room; a candidate opportunity stays in
    the signal room. When no alert destination is configured the alert falls
    back to the signal room **and reports that it did** — the alert is never
    dropped, and the delivery record shows the routing was degraded.
    """

    @staticmethod
    def poster(sink):
        def http_post(url, payload):
            sink.append((url, payload["content"]["text"]))
            return 200, "ok"
        return http_post

    def test_the_two_audiences_use_different_webhooks(self):
        signal, alert = [], []
        settings = {"channel": "webhook", "webhook_url": "https://signal.example/hook",
                    "alert_channel": "webhook",
                    "alert_webhook_url": "https://alert.example/hook"}
        FeishuNotifier(settings, http_post=self.poster(signal)).send("机会")
        FeishuNotifier(settings, http_post=self.poster(alert),
                       audience="alert").send("报警")
        self.assertEqual([url for url, _ in signal], ["https://signal.example/hook"])
        self.assertEqual([url for url, _ in alert], ["https://alert.example/hook"])

    def test_the_alert_env_var_is_used_when_set(self):
        from unittest import mock
        alert = []
        settings = {"channel": "webhook", "webhook_url": "https://signal.example/hook",
                    "alert_channel": "webhook"}
        with mock.patch.dict(os.environ, {"ARB_FEISHU_ALERT_WEBHOOK":
                                          "https://alert-env.example/hook"}):
            notifier = FeishuNotifier(settings, http_post=self.poster(alert),
                                      audience="alert")
            notifier.send("报警")
            self.assertFalse(notifier.fell_back_to_signal)
        self.assertEqual([url for url, _ in alert], ["https://alert-env.example/hook"])

    def test_an_alert_with_no_room_falls_back_to_the_signal_room_and_says_so(self):
        signal = []
        settings = {"channel": "webhook", "webhook_url": "https://signal.example/hook",
                    "alert_channel": "webhook"}
        notifier = FeishuNotifier(settings, http_post=self.poster(signal),
                                  audience="alert")
        result = notifier.send("报警")
        self.assertTrue(result["ok"])
        self.assertTrue(result["fell_back_to_signal"])
        self.assertEqual(result["audience"], "alert")
        self.assertEqual([url for url, _ in signal], ["https://signal.example/hook"])

    def test_an_alert_with_nothing_configured_at_all_is_not_silent(self):
        result = FeishuNotifier({}, audience="alert").send("报警")
        self.assertFalse(result["ok"])
        self.assertEqual(result["detail"], "no_channel_configured")

    def test_hydrate_prevents_re_announcing_after_restart(self):
        gate = AlertGate(D("20"), cooldown_seconds=900)
        gate.hydrate([{"key": "XIAOMI@gate:XIAOMI_USDT", "recv_ts_ms": 1_000_000}])
        self.assertEqual(gate.evaluate([candidate(net="25")], 1_000_100), [])

    def test_max_alerts_per_cycle_is_respected(self):
        gate = AlertGate(D("20"), cooldown_seconds=900, max_per_cycle=1)
        alerts = gate.evaluate([candidate("A"), candidate("B")], 1_000_000)
        self.assertEqual(len(alerts), 1)


class DeliveryTests(unittest.TestCase):
    def test_alert_text_carries_venue_prices_and_the_unverified_cost_flag(self):
        text = format_alert({**candidate(net="25"), "alert_key": "k",
                             "min_net_edge_bps": "20"}, 1_700_000_000_000)
        self.assertIn("现货多 + 永续空", text)
        self.assertIn("gate:XIAOMI_USDT", text)
        self.assertIn("+25", text)
        self.assertIn("成本是否已验证: 否", text)
        self.assertIn("不是可执行报价", text)

    def test_alert_text_names_the_exact_on_chain_token_contract(self):
        text = format_alert({**candidate(net="25"), "alert_key": "k",
                             "min_net_edge_bps": "20"}, 1_700_000_000_000)
        # The ticker alone is not an address: several wrappers can share a name.
        self.assertIn("链上合约地址", text)
        self.assertIn("0x076cf393e701839fc7a5832d2c68aafa235682ae", text)
        self.assertIn("wXIAOx", text)
        self.assertIn("0xb6ceceab302e2e4948951ee7843fc24e92933061", text)
        self.assertIn("0xdc7f2f41b48cd4f482d8c900ac2fa1b5ad058417", text)
        self.assertIn("chain 196", text)

    def test_alert_text_stays_honest_when_the_contract_is_unknown(self):
        alert = candidate(net="25")
        for key in ("token_address", "quote_token_address", "pool_address",
                    "token_symbol", "chain_id"):
            alert.pop(key)
        text = format_alert({**alert, "alert_key": "k", "min_net_edge_bps": "20"},
                            1_700_000_000_000)
        # Never invent an address to fill the line.
        self.assertIn("买入代币 —: —", text)

    def test_alert_text_shows_what_the_cost_estimate_is_made_of(self):
        alert = {**candidate(net="25"), "alert_key": "k", "min_net_edge_bps": "20",
                 "cost_breakdown": {"spot_fee_bps": "0", "perp_fee_bps": "5",
                                    "slippage_bps": "5", "failure_bps": "5",
                                    "gas_bps": "0", "total_bps": "15",
                                    "verified": False},
                 "cost_evidence": "永续腿 4.5 bps taker 实测"}
        text = format_alert(alert, 1_700_000_000_000)
        self.assertIn("开仓成本构成", text)
        self.assertIn("永续腿 taker 手续费: 5.0 bps", text)
        self.assertIn("合计: 15.0 bps", text)
        self.assertIn("校准依据", text)

    def test_lark_cli_command_targets_the_configured_user_as_bot(self):
        seen = {}

        def runner(argv, capture_output=True, text=True, timeout=20):
            seen["argv"] = argv
            class Done:
                returncode = 0
                stdout = '{"code":0}'
                stderr = ""
            return Done()

        notifier = FeishuNotifier({"channel": "lark_cli", "lark_user_id": "ou_test",
                                   "lark_identity": "bot"}, runner=runner)
        result = notifier.send("hello")
        self.assertTrue(result["ok"])
        self.assertEqual(seen["argv"][:3], ["lark-cli", "im", "+messages-send"])
        self.assertIn("--user-id", seen["argv"])
        self.assertIn("--as", seen["argv"])
        self.assertEqual(seen["argv"][seen["argv"].index("--as") + 1], "bot")

    def test_lark_cli_without_target_fails_closed(self):
        notifier = FeishuNotifier({"channel": "lark_cli"}, runner=lambda *a, **k: None)
        # A channel missing its credentials is dropped rather than half-tried.
        self.assertEqual(notifier.channel, "off")
        self.assertFalse(notifier.send("hello")["ok"])
        # The direct call still reports the missing target explicitly.
        self.assertEqual(notifier._send_lark_cli("hello")["detail"], "missing_lark_target")

    def test_delivery_failure_is_reported_not_raised(self):
        def runner(*args, **kwargs):
            raise FileNotFoundError("lark-cli")
        notifier = FeishuNotifier({"channel": "lark_cli", "lark_user_id": "ou_test"},
                                  runner=runner)
        result = notifier.send("hello")
        self.assertFalse(result["ok"])
        self.assertIn("FileNotFoundError", result["detail"])

    def test_webhook_channel_without_url_is_off(self):
        notifier = FeishuNotifier({"channel": "webhook", "webhook_url": ""})
        self.assertEqual(notifier.channel, "off")
        self.assertFalse(notifier.send("hello")["ok"])

    def test_webhook_payload_is_feishu_text_shape(self):
        seen = {}

        def post(url, payload):
            seen["url"], seen["payload"] = url, payload
            return 200, "ok"

        notifier = FeishuNotifier({"channel": "webhook", "webhook_url": "https://example/hook"},
                                  http_post=post)
        self.assertTrue(notifier.send("hello")["ok"])
        self.assertEqual(seen["payload"]["msg_type"], "text")
        self.assertEqual(seen["payload"]["content"]["text"], "hello")

    def test_comma_separated_channels_deliver_to_each_and_report_per_channel(self):
        seen = {}

        def post(url, payload):
            seen["webhook"] = payload
            return 200, "ok"

        def runner(argv, capture_output=True, text=True, timeout=20):
            seen["argv"] = argv
            class Done:
                returncode = 1
                stdout = ""
                stderr = "boom"
            return Done()

        notifier = FeishuNotifier(
            {"channel": "webhook,lark_cli", "webhook_url": "https://example/hook",
             "lark_user_id": "ou_test"},
            runner=runner, http_post=post)
        self.assertEqual(notifier.channel, "webhook,lark_cli")
        result = notifier.send("hello")
        # One channel succeeding is enough to honour the cooldown, but both
        # attempts stay visible in the record.
        self.assertTrue(result["ok"])
        self.assertEqual([d["ok"] for d in result["deliveries"]], [True, False])
        self.assertIn("webhook", seen)
        self.assertIn("argv", seen)


class ConfigAndPageTests(unittest.TestCase):
    def test_dispatch_sends_once_records_event_and_respects_cooldown(self):
        from arb_monitor.apps.hk_monitor import HKMonitor
        from arb_monitor.ledger.store import EventStore

        sent = []

        class FakeNotifier:
            channel = "fake"

            def send(self, text):
                sent.append(text)
                return {"ok": True, "channel": "fake", "detail": "test"}

        with TemporaryDirectory() as directory:
            config = load_config(str(CONFIG))
            monitor = HKMonitor(config, proxy=None, db=str(Path(directory) / "alert.db"))
            try:
                monitor.notifier = FakeNotifier()
                alert = candidate(net="25")
                first = monitor._dispatch_alerts("run-1", [alert], 1_000_000)
                self.assertEqual(first["alerts_sent"], 1)
                self.assertEqual(len(sent), 1)
                self.assertIn("gate:XIAOMI_USDT", sent[0])
                # Inside the cooldown: nothing new is sent.
                second = monitor._dispatch_alerts("run-2", [alert], 1_000_000 + 60_000)
                self.assertEqual(second["alerts_sent"], 0)
                # Stored as an event so a restart can hydrate the cooldown.
                stored = EventStore(Path(directory) / "alert.db")
                try:
                    rows = stored.events("alert")
                finally:
                    stored.close()
                self.assertEqual(len(rows), 1)
                self.assertTrue(rows[0]["delivery"]["ok"])
                self.assertEqual(rows[0]["alert_key"], "XIAOMI@gate:XIAOMI_USDT")
            finally:
                monitor.close()

    def test_dispatch_ignores_negative_net_basis(self):
        from arb_monitor.apps.hk_monitor import HKMonitor
        with TemporaryDirectory() as directory:
            config = load_config(str(CONFIG))
            monitor = HKMonitor(config, proxy=None, db=str(Path(directory) / "alert.db"))
            try:
                monitor.notifier = type("N", (), {"channel": "fake", "send": lambda self, t: {"ok": True}})()
                result = monitor._dispatch_alerts("run", [candidate(net="-12")], 1_000_000)
                self.assertEqual(result["alerts_sent"], 0)
            finally:
                monitor.close()

    def test_shipped_config_has_a_notify_channel(self):
        config = load_config(str(CONFIG))
        self.assertTrue(config["notify"]["enabled"])
        self.assertEqual(config["notify"]["lark_identity"], "bot")
        self.assertTrue(config["notify"]["lark_user_id"].startswith("ou_"))

    def test_lark_channel_requires_a_target_when_enabled(self):
        broken = CONFIG.read_text().replace('channel = "webhook"', 'channel = "lark_cli"', 1)
        broken = broken.replace(
            'lark_user_id = "ou_fc878c61e20cd6947eadbcec01756d88"', 'lark_user_id = ""', 1)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "hk_monitor.toml"
            path.write_text(broken)
            with self.assertRaises(ValueError):
                load_config(str(path))

    def test_unknown_notify_channel_is_rejected(self):
        broken = CONFIG.read_text().replace('channel = "webhook"', 'channel = "telegram"', 1)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "hk_monitor.toml"
            path.write_text(broken)
            with self.assertRaises(ValueError):
                load_config(str(path))

    def test_webhook_secret_is_not_stored_in_the_committed_config(self):
        text = CONFIG.read_text()
        self.assertIn('webhook_url = ""', text)
        self.assertNotIn("open-apis/bot/v2/hook", text)

    def test_page_has_sound_alert_and_no_0900_panel(self):
        page = dashboard_html({"min_net_edge_bps": "20", "candidates": [],
                               "token_references": [], "instruments": []})
        self.assertIn("AudioContext", page)
        self.assertIn("soundBtn", page)
        self.assertIn("var MIN_EDGE = 20;", page)
        self.assertNotIn("09:00", page)
        self.assertNotIn("昨日收盘锚定", page)


class ManualReviewMessageTests(unittest.TestCase):
    """When a leg is stranded, the message has to say what to check."""

    def test_the_message_names_the_stranded_leg_and_the_ledger_group(self):
        text = format_manual_review(
            asset="XIAOMI", instrument="okx:XIAOMI-USDT-SWAP",
            state="manual_intervention_required",
            reason="spot_sell_failed_after_5_attempts:RuntimeError",
            detail="挂单 ordId=42 成交 3/3；未对冲 3", group_id="xiaomi-abc123")
        self.assertIn("【人工审核】XIAOMI", text)
        self.assertIn("合约已平、现货仍在", text)
        self.assertIn("spot_sell_failed_after_5_attempts", text)
        self.assertIn("xiaomi-abc123", text)
        self.assertIn("ledger-reconcile", text)
        self.assertIn("AUTO_HALT", text)

    def test_an_unconfirmed_order_says_so_instead_of_claiming_a_position(self):
        text = format_manual_review(
            asset="SHEIN", instrument="okx:SHEIN-USDT-SWAP", state="maker_unknown",
            reason="order_state_unreadable_manual_reconciliation_required")
        self.assertIn("挂单状态未确认", text)
        self.assertIn("随时可能成交", text)

    def test_an_unknown_state_still_produces_a_readable_message(self):
        text = format_manual_review(asset="A", instrument="i", state="something_new",
                                    reason="x")
        self.assertIn("执行未达终态", text)


class CandidateExecutabilityTests(unittest.TestCase):
    """A candidate that clears the alert bar must say whether it can be ordered.

    Operator's question, 2026-09-16: the banner showed "候选 1 个：KUAISHOU 空
    gate:KUAISHOU_USDT 净 88.0295830519884860325575393 bps". Two things were
    wrong: the number was a raw 28-digit Decimal in a sentence, and nothing said
    that KUAISHOU cannot be traded at all (Gate has no execution adapter and its
    share mapping is unverified).
    """

    POLICY = {"allowlist": {"venues": ["okx"],
                            "instruments": ["XIAOMI-USDT-SWAP"],
                            "assets": ["XIAOMI"]}}

    def kuaishou(self) -> dict:
        return {"asset": "KUAISHOU", "perp_venue": "gate",
                "perp_instrument": "KUAISHOU_USDT",
                "mapping_verified": False, "cost_verified": True}

    def test_a_candidate_on_a_venue_without_an_adapter_is_flagged(self):
        reasons = execution_blockers(self.kuaishou(), self.POLICY)
        self.assertIn("该交易所（gate）没有执行适配器", reasons)
        self.assertIn("合约不在执行白名单", reasons)
        self.assertIn("份额映射未核验", reasons)

    def test_a_fully_admissible_candidate_has_no_blockers(self):
        candidate = {"asset": "XIAOMI", "perp_venue": "okx",
                     "perp_instrument": "XIAOMI-USDT-SWAP",
                     "mapping_verified": True, "cost_verified": True}
        self.assertEqual(execution_blockers(candidate, self.POLICY), [])

    def test_the_banner_prints_one_decimal_and_the_reason(self):
        candidate = dict(self.kuaishou(),
                         estimated_net_entry_bps="88.0295830519884860325575393",
                         execution_blockers=execution_blockers(self.kuaishou(),
                                                               self.POLICY))
        page = dashboard_html({"status": "ok", "min_net_edge_bps": "20",
                               "candidates": [candidate], "instruments": [],
                               "token_references": [], "probes": []})
        # The sentence is built client-side, so the page must carry the formatter
        # and the reasons rather than the raw digits.
        self.assertIn("toFixed(1)", page)
        self.assertIn("execution_blockers", page)
        self.assertNotIn("' 净 ' + c.estimated_net_entry_bps + ' bps'", page)

    def test_the_alert_text_says_when_a_candidate_cannot_be_ordered(self):
        from arb_monitor.ops.notify import format_alert

        row = candidate()
        row.update({"execution_blockers": ["该交易所（gate）没有执行适配器",
                                           "份额映射未核验"],
                    "mapping_verified": False})
        text = format_alert(row, 1_789_550_000_000)
        self.assertIn("不可执行", text)
        self.assertIn("没有执行适配器", text)
        self.assertIn("不是下单信号", text)

    def test_an_executable_candidate_says_so_instead(self):
        from arb_monitor.ops.notify import format_alert

        row = candidate()
        row.update({"execution_blockers": [], "mapping_verified": True})
        text = format_alert(row, 1_789_550_000_000)
        self.assertIn("都在执行白名单内", text)
        self.assertNotIn("不可执行", text)

    def test_an_alert_without_the_field_falls_back_to_the_mapping_flag(self):
        from arb_monitor.ops.notify import format_alert

        # Older stored alerts (and any caller that does not pass blockers) still
        # get the warning when the mapping is unverified.
        row = candidate()
        row.pop("execution_blockers", None)
        row["mapping_verified"] = False
        self.assertIn("不可执行", format_alert(row, 1_789_550_000_000))


if __name__ == "__main__":
    unittest.main()
