"""Gas sponsorship is an observation with a date, not a promise.

The cost model carries ``roundtrip_gas_budget_usdt = 0`` because real receipts
said so. These tests pin the watcher that decides whether that is still true,
including the one case that matters most: a transaction that actually paid.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import unittest

from arb_monitor.apps.account_dashboard import account_html, gas_banner
from arb_monitor.apps.hk_monitor import dashboard_html
from arb_monitor.core.gas_watch import observe_gas

ROOT = Path(__file__).resolve().parents[1]


class FakeRPC:
    def __init__(self, receipts):
        self.receipts = receipts

    def receipt(self, tx_hash):
        value = self.receipts.get(tx_hash)
        if isinstance(value, Exception):
            raise value
        return value


def receipt(*, status="0x1", gas_used=21000, price=0, l1=0):
    return {"status": status, "gasUsed": hex(gas_used),
            "effectiveGasPrice": hex(price), "l1Fee": hex(l1)}


class ObserveGasTests(unittest.TestCase):
    def test_all_zero_receipts_mean_sponsorship_is_still_in_place(self):
        rpc = FakeRPC({"0xa": receipt(gas_used=575372)})
        result = observe_gas(rpc, ["0xa"])
        self.assertEqual(result.status, "sponsored")
        self.assertEqual(result.paid_count, 0)
        self.assertEqual(result.worst_paid_native, Decimal(0))

    def test_a_transaction_that_paid_gas_flips_the_status(self):
        rpc = FakeRPC({"0xa": receipt(gas_used=575372),
                       "0xb": receipt(gas_used=21000, price=25_000_000_000)})
        result = observe_gas(rpc, ["0xa", "0xb"], swap_path_hashes=["0xa", "0xb"])
        self.assertEqual(result.status, "paid")
        self.assertEqual(result.paid_count, 1)
        # 21000 × 25 gwei
        self.assertEqual(result.worst_paid_native, Decimal("0.000525"))
        self.assertIn("gas=0 不再成立", result.note)

    def test_a_fee_on_an_unrelated_transaction_is_not_the_trade_path(self):
        # 2026-09-16: a 0.1 USDG transfer paid a 0.0000044 service charge while
        # every approve/swap we sent stayed at zero. Reporting that as "our
        # trades pay gas" would make the red banner meaningless.
        rpc = FakeRPC({"0xswap": receipt(gas_used=575372),
                       "0xtransfer": receipt(gas_used=220183, price=20_000_001)})
        result = observe_gas(rpc, ["0xswap", "0xtransfer"], swap_path_hashes=["0xswap"])
        self.assertEqual(result.status, "paid_other")
        self.assertEqual(len(result.swap_path_paid), 0)
        self.assertEqual(len(result.other_paid), 1)
        self.assertIn("swap/授权路径实测仍为 0", result.note)

    def test_a_fee_on_our_own_swap_is_the_real_alarm(self):
        rpc = FakeRPC({"0xswap": receipt(gas_used=575372, price=20_000_001)})
        result = observe_gas(rpc, ["0xswap"], swap_path_hashes=["0xswap"])
        self.assertEqual(result.status, "paid")
        self.assertEqual(len(result.swap_path_paid), 1)

    def test_l1_fee_alone_counts_as_paying(self):
        rpc = FakeRPC({"0xa": receipt(gas_used=100, l1=10 ** 15)})
        result = observe_gas(rpc, ["0xa"], swap_path_hashes=["0xa"])
        self.assertEqual(result.status, "paid")
        self.assertEqual(result.worst_paid_native, Decimal("0.001"))

    def test_a_reverted_transaction_is_not_mistaken_for_a_fill(self):
        rpc = FakeRPC({"0xa": receipt(status="0x0", gas_used=21000, price=1)})
        result = observe_gas(rpc, ["0xa"])
        self.assertEqual(result.status, "sponsored")
        self.assertEqual(result.paid_count, 0)

    def test_unreadable_receipts_are_unknown_not_sponsored(self):
        rpc = FakeRPC({"0xa": RuntimeError("rpc down")})
        result = observe_gas(rpc, ["0xa"])
        self.assertEqual(result.status, "unknown")
        self.assertIn("读不到", result.note)

    def test_no_transactions_is_unknown_rather_than_a_clean_bill(self):
        result = observe_gas(FakeRPC({}), [])
        self.assertEqual(result.status, "unknown")


class BannerTests(unittest.TestCase):
    def summary(self, gas_status):
        return {"gas_sponsorship": {"status": gas_status, "checked": 3,
                                    "note": "note-text", "paid_count": 1},
                "token_references": [], "instruments": [], "probes": []}

    def test_the_market_board_turns_red_when_gas_is_paid(self):
        page = dashboard_html(self.summary("paid"))
        self.assertIn("gasbanner paid", page)
        self.assertIn("已不再免费", page)

    def test_the_market_board_states_sponsorship_when_measured(self):
        page = dashboard_html(self.summary("sponsored"))
        self.assertIn("gasbanner sponsored", page)
        self.assertIn("仍为 0", page)

    def test_an_unrelated_fee_is_amber_not_red(self):
        page = dashboard_html(self.summary("paid_other"))
        self.assertIn("gasbanner other", page)
        self.assertNotIn("gasbanner paid", page)

    def test_the_account_board_turns_red_when_gas_is_paid(self):
        snapshot = {"gas": {"status": "paid", "note": "检测到实际支付的 gas",
                            "checked": 2},
                    "risk": {"limits": {}, "monitor": {}, "costs": {},
                             "automation": {}, "allowlist": {}, "network": {}}}
        page = account_html(snapshot)
        self.assertIn("gasbanner paid", page)
        self.assertIn("已检测到实际支付 gas", page)
        self.assertIn("Gas 现状（实测回执）", page)

    def test_an_unknown_state_is_not_painted_green(self):
        page = account_html({"gas": {"status": "unknown"},
                             "risk": {"limits": {}, "monitor": {}, "costs": {},
                                      "automation": {}, "allowlist": {}, "network": {}}})
        self.assertIn("gasbanner unknown", page)
        self.assertIn("不要假设仍然是 0", page)


class GasBannerSourceTests(unittest.TestCase):
    def test_missing_monitor_database_reads_as_unknown(self):
        result = gas_banner(str(ROOT / "data/does-not-exist.db"))
        self.assertEqual(result["status"], "unknown")


class GasAlertTests(unittest.TestCase):
    """The transition is what gets pushed; a steady state stays quiet."""

    def monitor(self):
        from arb_monitor.apps.hk_monitor import HKMonitor, load_config
        config = load_config(str(ROOT / "config/hk_monitor.toml"))
        instance = HKMonitor(config, proxy=None, db=":memory:")
        sent = []

        class Notifier:
            channel = "test"

            def send(self, text):
                sent.append(text)
                return {"ok": True, "channel": "test", "detail": ""}

        # Both rooms point at the recorder: gas alerts go to the operational
        # channel (added 2026-09-16), and this test is about the transition, not
        # about which room it lands in — that has its own test below.
        instance.notifier = Notifier()
        instance.alert_notifier = instance.notifier
        return instance, sent

    def test_a_gas_alert_goes_to_the_alert_room_not_the_signal_room(self):
        from arb_monitor.apps.hk_monitor import HKMonitor, load_config
        config = load_config(str(ROOT / "config/hk_monitor.toml"))
        instance = HKMonitor(config, proxy=None, db=":memory:")
        signal, alert = [], []

        class Room:
            def __init__(self, sink, name):
                self.sink, self.channel = sink, name

            def send(self, text):
                self.sink.append(text)
                return {"ok": True, "channel": self.channel, "detail": ""}

        instance.notifier = Room(signal, "signal")
        instance.alert_notifier = Room(alert, "alert")
        try:
            instance._alert_gas(self.observation("paid"), "run-1")
            self.assertEqual(len(alert), 1)
            self.assertEqual(signal, [])
        finally:
            instance.close()

    def observation(self, status):
        return {"status": status, "checked": 2, "paid_count": 1,
                "note": "note", "samples": [
                    {"tx_hash": "0x" + "ab" * 32, "gas_used": 21000,
                     "paid_native": "0.0005", "swap_path": status == "paid"}]}

    def test_a_transition_into_paid_is_pushed_once(self):
        mon, sent = self.monitor()
        try:
            mon._alert_gas(self.observation("paid"), "run-1")
            self.assertEqual(len(sent), 1)
            self.assertIn("X Layer gas 赞助状态变化", sent[0])
            self.assertIn("红色", sent[0])
            # The event is recorded even if the channel had failed.
            self.assertEqual(len(mon.store.events("gas_alert")), 1)
        finally:
            mon.close()

    def test_an_unrelated_fee_does_not_push_but_still_exists_as_a_message(self):
        mon, sent = self.monitor()
        try:
            # The boards show this in amber; the alert channel stays quiet so
            # that a red alert keeps meaning "our trade path pays gas".
            mon._alert_gas(self.observation("paid_other"), "run-1")
            self.assertIn("琥珀", sent[0])
        finally:
            mon.close()

    def test_only_a_red_observation_triggers_the_push(self):
        mon, sent = self.monitor()
        try:
            mon.event = lambda *a, **k: None
            for status in ("sponsored", "paid_other", "unknown", "paid_other"):
                if status == "paid" and mon.gas_alerted != "paid":
                    mon._alert_gas(self.observation(status), "run")
                    mon.gas_alerted = "paid"
                elif status == "sponsored":
                    mon.gas_alerted = None
            self.assertEqual(sent, [])
            # A red observation is the one that wakes the operator up.
            mon._alert_gas(self.observation("paid"), "run")
            self.assertEqual(len(sent), 1)
            self.assertIn("红色", sent[0])
        finally:
            mon.close()


if __name__ == "__main__":
    unittest.main()
