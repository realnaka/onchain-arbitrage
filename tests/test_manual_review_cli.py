"""The CLI has to *tell* a human when a leg is stranded, not just exit non-zero.

A close that ends in `manual_intervention_required` finishes after the operator
has stopped watching the terminal. The result is printed, but the notification
is what makes the state survivable unattended — so it is verified here rather
than trusted, including the case where the notification channel itself is down.
"""
from __future__ import annotations

from decimal import Decimal
import unittest

import arb_monitor.apps.full_chain_cli as cli
import arb_monitor.ops.notify as notify


class Recorder:
    def __init__(self, ok=True, raises=None):
        self.texts: list[str] = []
        self.ok, self.raises = ok, raises

    def send(self, text: str) -> dict:
        self.texts.append(text)
        if self.raises:
            raise self.raises
        return {"ok": self.ok, "channel": "test", "detail": "recorded"}


class ManualReviewNotificationTests(unittest.TestCase):
    def setUp(self):
        self.original = notify.FeishuNotifier

    def tearDown(self):
        notify.FeishuNotifier = self.original

    def test_a_stranded_leg_sends_the_review_message(self):
        recorder = Recorder()
        notify.FeishuNotifier = lambda settings, **kwargs: recorder
        cli._notify_manual_review(
            {"notify": {"channel": "webhook"}},
            {"plan": {"asset": "XIAOMI", "perp": "okx:XIAOMI-USDT-SWAP",
                      "side": "sell", "size_quote": "10"},
             "reason": "spot_sell_failed_after_5_attempts:WalletExecutionError",
             "maker": {"order_id": "42", "price": "3.341", "filled": "3",
                       "requested": "3", "state": "filled"},
             "unhedged_tokens": "3", "fallback": {"style": "taker"}},
            "manual_intervention_required")
        self.assertEqual(len(recorder.texts), 1)
        text = recorder.texts[0]
        self.assertIn("【人工审核】XIAOMI", text)
        self.assertIn("after_5_attempts", text)
        self.assertIn("ordId=42", text)

    def test_a_broken_channel_does_not_raise(self):
        recorder = Recorder(raises=RuntimeError("webhook down"))
        notify.FeishuNotifier = lambda settings, **kwargs: recorder
        cli._notify_manual_review({"notify": {}}, {"plan": {"asset": "X"}},
                                  "maker_unknown")
        self.assertEqual(len(recorder.texts), 1)

    def test_a_stranded_leg_is_sent_to_the_alert_room(self):
        # Operator's decision 2026-09-16: "人工审核/平仓失败" must not share a
        # channel with the opportunity alerts.
        seen = {}

        class Recorder2:
            def __init__(self, settings, **kwargs):
                seen["audience"] = kwargs.get("audience")

            def send(self, text):
                return {"ok": True, "channel": "test", "detail": ""}

        notify.FeishuNotifier = Recorder2
        cli._notify_manual_review({"notify": {}},
                                  {"plan": {"asset": "X"}}, "maker_unknown")
        self.assertEqual(seen.get("audience"), "alert")

    def test_the_review_states_cover_the_stranded_ones(self):
        for state in ("manual_intervention_required", "maker_unknown",
                      "close_perp_underfilled", "spot_not_confirmed",
                      "unhedged_spot_only", "close_unknown"):
            self.assertIn(state, cli.REVIEW_STATES)
        # A completed close and a dry run are not things to page a human about.
        for state in ("closed", "dry_run_complete", "hedged", "maker_unfilled"):
            self.assertNotIn(state, cli.REVIEW_STATES)


if __name__ == "__main__":
    unittest.main()
