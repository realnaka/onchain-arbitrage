"""执行时兑换（A33）与 /swap 重试（A38），2026-10-08。

operator 的口径：**机会发现不看钱包** —— 「不要按钱包真有的钱选币，先找机会，
有机会可以把币兑换」。所以这里锁两件事：换多少钱由票面决定；缺币时加一跳兑换，
而不是把这行否掉。
"""
from __future__ import annotations

import importlib.util
import unittest
from decimal import Decimal as D
from pathlib import Path

from arb_monitor.xstocks_v2 import costs, paper
from arb_monitor.xstocks_v2.feeds.jupiter import build_swap_retry
from arb_monitor.xstocks_v2.policy import V2Policy

ROOT = Path(__file__).resolve().parents[1]


def _live_module():
    """The resident executor, loaded the way its own tests load it."""
    spec = importlib.util.spec_from_file_location(
        "v2live_funding", ROOT / "scripts" / "xstocks_v2_live.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FundingPlanTest(unittest.TestCase):
    """Which stablecoin pays, and how much has to be swapped."""

    def setUp(self):
        self.mod = _live_module()

    def test_enough_usdc_needs_no_swap(self):
        got = self.mod.funding_plan(usdc=D("50"), usdt=D("44"), need=D("25"))
        self.assertEqual(got["funding"], "usdc")
        self.assertEqual(got["convert_amount_usdt"], D("0"))

    def test_usdt_only_swaps_the_ticket_plus_the_swap_slack(self):
        got = self.mod.funding_plan(usdc=D("8.475511"), usdt=D("44.5405"),
                                    need=D("25"), slack=D("0.025"))
        self.assertEqual(got["funding"], "convert_usdt_to_usdc")
        # 25 + 10 bps of slack: swapping exactly 25 leaves ~5 bps less USDC
        # than the ticket, which is a second refusal a minute later (22:47).
        self.assertEqual(got["convert_amount_usdt"], D("25.025"))

    def test_neither_stablecoin_covers_it_is_a_funding_answer_not_a_price_one(self):
        got = self.mod.funding_plan(usdc=D("1"), usdt=D("2"), need=D("25"))
        self.assertEqual(got["funding"], "insufficient")
        self.assertEqual(got["have_usdc"], D("1"))
        self.assertEqual(got["have_usdt"], D("2"))

    def test_neither_alone_covers_it_swaps_only_the_gap(self):
        """Measured 22:5x: USDC 8.413241 + USDT 19.5405 could not fund 25.

        Refusing there is "we hold the wrong dollar", not "this trade loses".
        """
        got = self.mod.funding_plan(usdc=D("8.413241"), usdt=D("19.5405"),
                                    need=D("25"), slack=D("0.025"))
        self.assertEqual(got["funding"], "top_up_usdc_from_usdt")
        self.assertEqual(got["convert_amount_usdt"],
                         D("25.025") - D("8.413241"))

    def test_an_unreadable_wallet_is_zero_money_not_a_crash(self):
        got = self.mod.funding_plan(usdc=None, usdt="", need=D("25"))
        self.assertEqual(got["funding"], "insufficient")

    def test_a_refusal_says_how_much_money_is_missing(self):
        """Operator 2026-10-10: 「哪条链缺多少钱」, not one word.

        Measured 2026-10-09: 4,882 rows in one day read ``funding_short`` and
        nothing else, so a busy machine and a machine 3 USDT away from a trade
        looked identical on the board.
        """
        got = self.mod.funding_plan(usdc=D("8.4"), usdt=D("0.9"), need=D("25"),
                                    slack=D("0.025"))
        self.assertEqual(got["funding"], "insufficient")
        self.assertEqual(got["have_usdc"], D("8.4"))
        self.assertEqual(got["have_usdt"], D("0.9"))
        self.assertEqual(got["need_usdt"], D("25"))
        self.assertEqual(got["required_usdt"], D("25.025"))
        self.assertEqual(got["short_usdt"], D("25.025") - D("9.3"))

    def test_a_row_that_can_be_funded_has_no_gap_to_publish(self):
        from arb_monitor.xstocks_v2 import live as live_core
        for funding in ({"funding": "usdc", "need_usdt": D("25"),
                         "have_usdc": D("50"), "have_usdt": D("0")},
                        {"funding": "convert_usdt_to_usdc"}):
            with self.subTest(funding=funding["funding"]):
                self.assertEqual(live_core.funding_gap(funding), {})

    def test_the_gap_is_the_shape_the_runners_write_and_the_board_reads(self):
        from arb_monitor.xstocks_v2 import live as live_core
        gap = live_core.funding_gap({"funding": "insufficient",
                                     "need_usdt": D("25"),
                                     "have_usdc": D("8.4"),
                                     "have_usdt": D("0.9"),
                                     "short_usdt": D("15.7")})
        self.assertEqual(gap, {"need_usdt": "25", "have_usdc": "8.4",
                               "have_usdt": "0.9", "short_usdt": "15.7",
                               "wallet_chain": "solana"})
        # Both callers read the Solana wallet, and the board says so: the row's
        # own ``chain`` is where the token lives, not where the money is.
        self.assertEqual(live_core.funding_gap(
            {"funding": "insufficient", "short_usdt": D("1")},
            wallet_chain="eth")["wallet_chain"], "eth")

    def test_an_insufficient_row_with_no_shortfall_publishes_nothing(self):
        """A gap of zero is not a gap: it must not print as 「还缺 0」."""
        from arb_monitor.xstocks_v2 import live as live_core
        self.assertEqual(live_core.funding_gap(
            {"funding": "insufficient", "short_usdt": D("0")}), {})
        self.assertEqual(live_core.funding_gap(None), {})


class ConversionChargeTest(unittest.TestCase):
    """The conversion is a real cost, so the net charges it."""

    def setUp(self):
        self.policy = V2Policy.load()

    def _net(self, conversion):
        return paper.chain_to_kraken_net(
            size_usdt=D("25"), tokens=D("0.1"), buy_price_usd=D("250"),
            kraken_bid=D("255"), policy=self.policy,
            transfer_usdt=D("0.36"), gas_usdt=D("0"),
            conversion_usdt=conversion)

    def test_the_swap_is_charged_on_the_net(self):
        without = self._net(D("0"))["net_usdt"]
        with_swap = self._net(D("0.01"))["net_usdt"]
        self.assertEqual(without - with_swap, D("0.01"))
        self.assertEqual(self._net(D("0.01"))["costs"]["conversion_usdt"], D("0.01"))

    def test_a_swap_nobody_could_price_is_unknown_not_free(self):
        result = self._net(None)
        self.assertIsNone(result["net_usdt"])
        self.assertEqual(result["reason"], "cost_unknown")

    def test_the_proportional_terms_are_unchanged_by_an_extra_fixed_cost(self):
        """The swap is fixed, so the bps terms must not move with it."""
        fee, buffer = costs.proportional_parts(self.policy, notional=D("25"),
                                               family=paper.CHAIN_TO_KRAKEN)
        self.assertEqual(self._net(D("0.01"))["costs"]["kraken_fee_usdt"], fee)
        self.assertEqual(self._net(D("0.01"))["costs"]["slippage_buffer_usdt"],
                         buffer)


class SwapBuildRetryTest(unittest.TestCase):
    """/swap: retry what means "ask again", never what means "no route"."""

    class _Client:
        def __init__(self, script):
            self.script = list(script)
            self.calls = 0

        def build_swap_status(self, quote):      # noqa: ARG002
            self.calls += 1
            return self.script.pop(0) if self.script else (400, None)

    def _retry(self, script, **kwargs):
        client = self._Client(script)
        slept: list[float] = []
        swap, note = build_swap_retry(client, {}, sleep=slept.append, **kwargs)
        return client, swap, note, slept

    def test_a_rate_limited_build_is_retried(self):
        client, swap, note, slept = self._retry(
            [(429, None), (200, {"swapTransaction": "AAA"})])
        self.assertEqual(swap["swapTransaction"], "AAA")
        self.assertEqual(note, "")
        self.assertEqual(client.calls, 2)
        self.assertEqual(slept, [1.0])

    def test_a_request_that_never_arrived_is_retried(self):
        client, swap, _note, _slept = self._retry(
            [(0, None), (0, None), (200, {"swapTransaction": "BBB"})])
        self.assertEqual(swap["swapTransaction"], "BBB")
        self.assertEqual(client.calls, 3)

    def test_a_dead_route_is_not_retried(self):
        client, swap, note, slept = self._retry([(400, None)])
        self.assertEqual(swap, {})
        self.assertEqual(note, "http_400")
        self.assertEqual(client.calls, 1)
        self.assertEqual(slept, [])

    def test_giving_up_names_the_last_reason(self):
        client, swap, note, _slept = self._retry(
            [(429, None), (429, None), (429, None), (429, None)], tries=4)
        self.assertEqual(swap, {})
        self.assertEqual(note, "http_429")
        self.assertEqual(client.calls, 4)

    def test_a_200_without_a_transaction_is_not_success(self):
        client, swap, note, _slept = self._retry([(200, {"error": "x"})])
        self.assertEqual(swap, {})
        self.assertEqual(note, "http_200")
        self.assertEqual(client.calls, 1)


class RealizedNetTest(unittest.TestCase):
    """账本的净利必须和余额变化对得上 (measured 2026-10-08).

    The old formula (``cost - fee - plan size``) published +0.63337 for a cycle
    that really made +0.619 USDT: it ignored the stablecoin swap and the gas.
    """

    def setUp(self):
        self.mod = _live_module()

    def test_every_line_is_subtracted(self):
        net, parts, missing = self.mod.realized_net_usdt(
            proceeds=D("25.63337"), capital=D("25"),
            conversion_usdt=D("0.008223"), gas_usd=D("0.0064"))
        self.assertEqual(missing, [])
        self.assertEqual(net, D("25.63337") - D("25") - D("0.008223") - D("0.0064"))
        self.assertEqual(parts["capital_usdt"], D("25"))

    def test_an_unread_gas_price_is_unknown_not_zero(self):
        net, parts, missing = self.mod.realized_net_usdt(
            proceeds=D("25.63337"), capital=D("25"),
            conversion_usdt=D("0"), gas_usd=None)
        self.assertIsNone(net)
        self.assertEqual(missing, ["gas_usd"])
        self.assertEqual(parts["gas_usd"], None)

    def test_a_cycle_with_no_conversion_charges_zero_for_it(self):
        net, _parts, missing = self.mod.realized_net_usdt(
            proceeds=D("25.6"), capital=D("25"), conversion_usdt=D("0"),
            gas_usd=D("0.006"))
        self.assertEqual(missing, [])
        self.assertEqual(net, D("25.6") - D("25") - D("0.006"))


class SplBalanceTest(unittest.TestCase):
    """One wallet reader for the paper runner and the executor."""

    class _Http:
        def __init__(self, payload):
            self.payload = payload

        def post_json(self, url, body):          # noqa: ARG002
            return 200, self.payload

    def _payload(self, amounts):
        rows = [{"account": {"data": {"parsed": {"info": {
            "tokenAmount": {"amount": str(amount)}}}}}} for amount in amounts]
        return {"result": {"value": rows}}

    def test_it_sums_every_token_account(self):
        from arb_monitor.xstocks_v2 import live as live_mod
        http = self._Http(self._payload([8_413_241, 16_603_536]))
        self.assertEqual(live_mod.spl_balance(http, "mint"), D("25.016777"))

    def test_an_unreadable_wallet_is_none_not_zero(self):
        """A42: "the RPC did not answer" and "the wallet is empty" differ."""
        from arb_monitor.xstocks_v2 import live as live_mod

        class Boom:
            def post_json(self, url, body):      # noqa: ARG002
                raise RuntimeError("rpc down")

        self.assertIsNone(live_mod.spl_balance(Boom(), "mint"))
        self.assertIsNone(live_mod.spl_balance(self._Http({}), "mint"))
        # An answer that says "no accounts" is zero money, not an unknown.
        self.assertEqual(live_mod.spl_balance(self._Http({"result": {"value": []}}),
                                              "mint"), D("0"))


if __name__ == "__main__":
    unittest.main()
