import unittest
from decimal import Decimal

from arb_monitor.execution.okx_wallet import (
    OKXAgenticWalletAdapter,
    WalletExecutionError,
)
from arb_monitor.execution.mev import MEVPolicy


class OKXWalletAdapterTests(unittest.TestCase):
    def setUp(self):
        self.wallet = "0x1111111111111111111111111111111111111111"
        self.usdg = "0x2222222222222222222222222222222222222222"
        self.kuai = "0x3333333333333333333333333333333333333333"
        self.router = "0x4444444444444444444444444444444444444444"
        self.calls = []

    def runner(self, args):
        self.calls.append(tuple(args))
        if args[:2] == ("swap", "quote"):
            return {"ok": True, "data": [{
                "action": "ok", "router": f"{self.usdg}--{self.kuai}",
                "fromToken": {"decimal": "6", "tokenContractAddress": self.usdg},
                "toToken": {"decimal": "18", "tokenContractAddress": self.kuai},
                "chainIndex": "196", "fromTokenAmount": "200000",
                "toTokenAmount": "200000000000000000",
                "minReceiveAmount": "198000000000000000",
                "priceImpactPercent": "-0.1", "tradeFee": "0.0003",
                "estimateGasFee": "162000",
            }]}
        if args[:2] == ("swap", "swap"):
            return {"ok": True, "data": [{
                "routerResult": {
                    "action": "ok", "chainIndex": "196",
                    "fromToken": {"decimal": "6", "tokenContractAddress": self.usdg},
                    "toToken": {"decimal": "18", "tokenContractAddress": self.kuai},
                    "fromTokenAmount": "200000", "toTokenAmount": "200000000000000000",
                    "priceImpactPercent": "-0.1", "tradeFee": "0.0003",
                },
                "tx": {"from": self.wallet, "to": self.router, "data": "0xabcd",
                       "gas": "162000", "value": "0",
                       "minReceiveAmount": "199000000000000000"},
            }]}
        if args[:2] == ("gateway", "simulate"):
            return {"ok": True, "data": [{"failReason": "", "risks": [],
                "intention": "Swap", "assetChange": [
                    {"address": self.usdg, "decimals": "6", "rawValue": "-200000"},
                    {"address": self.kuai, "decimals": "18",
                     "rawValue": "200000000000000000"}]}]}
        if args[:2] == ("security", "tx-scan"):
            return {"ok": True, "data": {"action": "", "riskItemDetail": [],
                                          "warnings": None,
                                          "simulator": {"revertReason": ""}}}
        return {"ok": True, "data": {"orderId": "1", "txHash": "0x" + "a" * 64}}

    def adapter(self, live=False):
        return OKXAgenticWalletAdapter(
            self.runner, wallet_address=self.wallet,
            allowed_tokens=frozenset((self.usdg, self.kuai)),
            allowed_routers=frozenset((self.router,)),
            allow_live_signing=live,
            mev_policy=MEVPolicy(enabled=False, require_private_route=False),
        )

    def test_quote_build_simulate_and_live_boundary(self):
        adapter = self.adapter()
        quote = adapter.quote_exact_in(self.usdg, self.kuai, Decimal("0.2"))
        self.assertEqual(quote.amount_out, Decimal("0.2"))
        plan = adapter.build_swap(quote)
        self.assertEqual(adapter.simulate(plan)["intention"], "Swap")
        with self.assertRaisesRegex(WalletExecutionError, "live_signing_disabled"):
            adapter.execute(plan)
        result = self.adapter(live=True).execute(plan, confirmed=True)
        self.assertEqual(result.tx_hash, "0x" + "a" * 64)

    def test_a_reverted_simulation_keeps_the_reason_and_the_transaction(self):
        """The ledger said only ``simulation_reverted`` on 2026-09-18.

        The cause (a spent ERC-20 allowance, selector 0xf4059071) and the
        calldata that produced it were both dropped, so the failure had to be
        reproduced from a fresh quote to be explained at all. Neither is
        optional evidence: the code is what callers switch on, the detail is
        what the venue said, and the plan is what can be re-simulated later.
        """
        detail = '{"error":"execution reverted","output":"0xf4059071"}'

        def reverting(args):
            if args[:2] == ("gateway", "simulate"):
                return {"ok": True, "data": [{"failReason": detail, "risks": [],
                                             "intention": "Swap",
                                             "assetChange": []}]}
            return self.runner(args)

        adapter = OKXAgenticWalletAdapter(
            reverting, wallet_address=self.wallet,
            allowed_tokens=frozenset((self.usdg, self.kuai)),
            allowed_routers=frozenset((self.router,)),
            mev_policy=MEVPolicy(enabled=False, require_private_route=False),
        )
        plan = adapter.build_swap(
            adapter.quote_exact_in(self.usdg, self.kuai, Decimal("0.2")))
        with self.assertRaises(WalletExecutionError) as caught:
            adapter.simulate(plan)
        exc = caught.exception
        self.assertEqual(exc.code, "simulation_reverted")
        self.assertIn("0xf4059071", exc.detail)
        self.assertIn("0xf4059071", str(exc))
        # The transaction travels with the failure, so the same call can be
        # re-simulated after the market has moved.
        self.assertEqual(exc.plan_event["calldata"], plan.calldata)
        self.assertEqual(exc.plan_event["tx_to"], self.router)
        self.assertEqual(exc.plan_event["amount_in"], "0.2")

    def test_quote_rejects_risky_action(self):
        def risky(args):
            return {"ok": True, "data": [{"action": "warn"}]}
        adapter = OKXAgenticWalletAdapter(
            risky, wallet_address=self.wallet,
            allowed_tokens=frozenset((self.usdg, self.kuai)),
        )
        with self.assertRaisesRegex(WalletExecutionError, "quote_action"):
            adapter.quote_exact_in(self.usdg, self.kuai, Decimal("0.2"))

    def test_a_slow_cli_call_does_not_return_an_expired_quote(self):
        # The venue prices the swap during the call, so a 12s round trip still
        # yields a fresh quote; measuring the TTL from the request start made
        # every slow call look expired and blocked closes that had sent nothing.
        clock = {"now": 1_000_000}

        def slow_runner(args):
            clock["now"] += 12_000        # the call itself takes 12 seconds
            return self.runner(args)

        adapter = OKXAgenticWalletAdapter(
            slow_runner, wallet_address=self.wallet,
            allowed_tokens=frozenset((self.usdg, self.kuai)),
            allowed_routers=frozenset((self.router,)),
            clock_ms=lambda: clock["now"],
            quote_ttl_ms=10_000,
            mev_policy=MEVPolicy(enabled=False, require_private_route=False),
        )
        quote = adapter.quote_exact_in(self.usdg, self.kuai, Decimal("0.2"))
        self.assertGreater(quote.expires_at_ms, clock["now"])


class ExactOutTests(unittest.TestCase):
    """Whole-share hedges need an exact output, not a hopeful one.

    An exact-in buy cannot be relied on to deliver 5.000000000000000000 shares,
    so the perpetual leg would always be a rounded-off hedge. Exact-out fixes
    the share count and bounds the cash instead.
    """

    def setUp(self):
        self.wallet = "0x1111111111111111111111111111111111111111"
        self.usdg = "0x2222222222222222222222222222222222222222"
        self.token = "0x3333333333333333333333333333333333333333"
        self.router = "0x4444444444444444444444444444444444444444"
        self.calls = []
        self.default_spend = 3_421_168          # est. input for 1 share
        self.default_bound = 3_438_274          # venue maxSpendAmount (+0.5%)

    def runner(self, args):
        self.calls.append(tuple(args))
        if args[:2] == ("swap", "quote"):
            return {"ok": True, "data": [{
                "action": "ok", "chainIndex": "196", "swapMode": "exactOut",
                "router": "0xrouter",
                "fromToken": {"decimal": "6", "tokenContractAddress": self.usdg},
                "toToken": {"decimal": "18", "tokenContractAddress": self.token},
                "fromTokenAmount": str(self.default_spend),
                "toTokenAmount": "1000000000000000000",
                "priceImpactPercent": "0.05", "tradeFee": "0.000625",
                "estimateGasFee": "338400",
            }]}
        if args[:2] == ("swap", "swap"):
            return {"ok": True, "data": [{
                "routerResult": {
                    "action": "ok", "chainIndex": "196", "swapMode": "exactOut",
                    "fromToken": {"decimal": "6", "tokenContractAddress": self.usdg},
                    "toToken": {"decimal": "18", "tokenContractAddress": self.token},
                    "fromTokenAmount": str(self.default_spend),
                    "toTokenAmount": "1000000000000000000",
                },
                "tx": {"from": self.wallet, "to": self.router, "data": "0xabcd",
                       "gas": "338400", "value": "0",
                       "minReceiveAmount": "1000000000000000000",
                       "maxSpendAmount": str(self.default_bound),
                       "slippagePercent": "0.5"},
            }]}
        if args[:2] == ("gateway", "simulate"):
            return {"ok": True, "data": [{"failReason": "", "risks": [],
                "intention": "Swap", "assetChange": [
                    {"address": self.usdg, "decimals": "6",
                     "rawValue": str(-self.default_spend)},
                    {"address": self.token, "decimals": "18",
                     "rawValue": "1000000000000000000"}]}]}
        if args[:2] == ("security", "tx-scan"):
            return {"ok": True, "data": {"action": "", "riskItemDetail": [],
                                         "warnings": None,
                                         "simulator": {"revertReason": ""}}}
        return {"ok": True, "data": {"orderId": "1", "txHash": "0x" + "a" * 64}}

    def adapter(self, live=False):
        return OKXAgenticWalletAdapter(
            self.runner, wallet_address=self.wallet,
            allowed_tokens=frozenset((self.usdg, self.token)),
            allowed_routers=frozenset((self.router,)),
            allow_live_signing=live,
            mev_policy=MEVPolicy(enabled=False, require_private_route=False),
        )

    def quote(self, adapter=None, shares="1", bound="3.44"):
        adapter = adapter or self.adapter()
        return adapter, adapter.quote_exact_out(
            self.usdg, self.token, Decimal(shares), out_decimals=18,
            max_amount_in=Decimal(bound))

    def test_the_output_amount_is_sent_in_output_token_units(self):
        adapter, quote = self.quote()
        argv = self.calls[0]
        self.assertEqual(argv[:2], ("swap", "quote"))
        self.assertEqual(argv[argv.index("--amount") + 1], "1000000000000000000")
        self.assertEqual(argv[argv.index("--swap-mode") + 1], "exactOut")
        self.assertEqual(quote.amount_out, Decimal("1"))
        self.assertTrue(quote.is_exact_out)
        # estimated input and the authorised ceiling are kept apart
        self.assertEqual(quote.amount_in, Decimal("3.421168"))
        self.assertEqual(quote.max_amount_in, Decimal("3.44"))

    def test_an_estimate_above_the_authorised_ceiling_is_refused(self):
        adapter = self.adapter()
        with self.assertRaisesRegex(WalletExecutionError, "exact_out_input_exceeds_bound"):
            adapter.quote_exact_out(self.usdg, self.token, Decimal("1"),
                                    out_decimals=18, max_amount_in=Decimal("3.42"))

    def test_the_calldata_bound_comes_from_the_venue_not_from_us(self):
        adapter, quote = self.quote()
        plan = adapter.build_swap(quote)
        # maxSpendAmount is the venue's own ceiling and is what simulation is
        # checked against.
        self.assertEqual(plan.quote.max_amount_in, Decimal("3.438274"))
        self.assertIn("--amount", self.calls[-1])
        self.assertNotIn("--readable-amount", self.calls[-1])

    def test_a_changed_share_count_invalidates_the_calldata(self):
        self.default_spend = 3_421_168
        adapter = self.adapter()
        original = self.runner

        def runner(args):
            if args[:2] == ("swap", "swap"):
                # The venue answers with 0.99 shares instead of the 1 we asked.
                return {"ok": True, "data": [{
                    "routerResult": {
                        "action": "ok", "chainIndex": "196",
                        "fromToken": {"decimal": "6", "tokenContractAddress": self.usdg},
                        "toToken": {"decimal": "18", "tokenContractAddress": self.token},
                        "fromTokenAmount": "3421168",
                        "toTokenAmount": "990000000000000000"},
                    "tx": {"from": self.wallet, "to": self.router, "data": "0xabcd",
                           "gas": "338400", "value": "0",
                           "maxSpendAmount": "3438274"},
                }]}
            return original(args)

        adapter = OKXAgenticWalletAdapter(
            runner, wallet_address=self.wallet,
            allowed_tokens=frozenset((self.usdg, self.token)),
            allowed_routers=frozenset((self.router,)),
            mev_policy=MEVPolicy(enabled=False, require_private_route=False))
        with self.assertRaises(WalletExecutionError):
            adapter.build_swap(adapter.quote_exact_out(
                self.usdg, self.token, Decimal("1"), out_decimals=18,
                max_amount_in=Decimal("3.44")))

    def test_simulation_must_show_the_exact_share_count(self):
        adapter, quote = self.quote()
        plan = adapter.build_swap(quote)
        self.assertEqual(adapter.simulate(plan)["intention"], "Swap")

        def short_sim(args):
            if args[:2] == ("gateway", "simulate"):
                return {"ok": True, "data": [{"failReason": "", "risks": [],
                    "intention": "Swap", "assetChange": [
                        {"address": self.usdg, "decimals": "6", "rawValue": "-3421168"},
                        {"address": self.token, "decimals": "18",
                         "rawValue": "999000000000000000"}]}]}
            return self.runner(args)

        other = OKXAgenticWalletAdapter(
            short_sim, wallet_address=self.wallet,
            allowed_tokens=frozenset((self.usdg, self.token)),
            allowed_routers=frozenset((self.router,)),
            mev_policy=MEVPolicy(enabled=False, require_private_route=False))
        with self.assertRaisesRegex(WalletExecutionError, "simulation_exact_out_mismatch"):
            other.simulate(plan)

    def test_simulation_must_stay_inside_the_spend_bound(self):
        adapter, quote = self.quote()
        plan = adapter.build_swap(quote)

        def overspend(args):
            if args[:2] == ("gateway", "simulate"):
                return {"ok": True, "data": [{"failReason": "", "risks": [],
                    "intention": "Swap", "assetChange": [
                        {"address": self.usdg, "decimals": "6", "rawValue": "-3439000"},
                        {"address": self.token, "decimals": "18",
                         "rawValue": "1000000000000000000"}]}]}
            return self.runner(args)

        other = OKXAgenticWalletAdapter(
            overspend, wallet_address=self.wallet,
            allowed_tokens=frozenset((self.usdg, self.token)),
            allowed_routers=frozenset((self.router,)),
            mev_policy=MEVPolicy(enabled=False, require_private_route=False))
        with self.assertRaisesRegex(WalletExecutionError, "exact_out_over_budget"):
            other.simulate(plan)


if __name__ == "__main__":
    unittest.main()
