"""The spot-first invariant, tested on the real orchestration code path.

No network, no signing: the wallet leg and the exchange are fakes, so every
test asserts what the runner *would* do — in particular that the perpetual leg
is never submitted without a confirmed spot fill.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from arb_monitor.apps.account_dashboard import load_toml
from arb_monitor.execution.full_chain import FullChainError, FullChainRunner
from arb_monitor.execution.ledger import ExecutionLedger
from arb_monitor.execution.onchain_wallet import ChainReceipt, WalletSwapOutcome

ROOT = Path(__file__).resolve().parents[1]
XIAOMI = "0x076cf393e701839fc7a5832d2c68aafa235682ae"
USDG = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"


class FakeQuote:
    def __init__(self, amount_out: Decimal):
        self.amount_out = amount_out
        self.amount_out_minimum = amount_out * Decimal("0.992")
        self.router = "0x7c5bee2a8091c3ef39072f64f18fac913060aeaf"
        self.price_impact_pct = Decimal("-0.02")
        self.trade_fee_usd = Decimal("0.000307773")
        self.gas_limit = 656400
        self.mode = "exactIn"
        self.max_amount_in = None


class FakeWallet:
    def __init__(self, amount_out: Decimal):
        self.amount_out = amount_out
        self.quoted: list[tuple] = []
        self.exact_out_quoted: list[tuple] = []
        self.share_price = Decimal("3.42")

    def quote_exact_in(self, token_in, token_out, amount_in):
        self.quoted.append((token_in, token_out, amount_in))
        # A smooth, monotone pool: cash in / price = shares out.
        return FakeQuote(Decimal(str(amount_in)) / self.share_price)

    def quote_exact_out(self, token_in, token_out, amount_out, *,
                        out_decimals, max_amount_in):
        """Exact shares for a bounded spend — the whole-share hedge path."""
        self.exact_out_quoted.append((token_in, token_out, Decimal(str(amount_out)),
                                      Decimal(str(max_amount_in))))
        shares = Decimal(str(amount_out))
        spend = shares * Decimal("3.42")
        if spend > Decimal(str(max_amount_in)):
            raise ValueError("exact_out_input_exceeds_bound")
        quote = FakeQuote(shares)
        quote.amount_in = spend
        quote.mode = "exactOut"
        quote.max_amount_in = Decimal(str(max_amount_in))
        return quote


class FakeWalletLeg:
    def __init__(self, *, short_delivery=None, status="filled",
                 amount_out="1.170333476316361541"):
        self.wallet = FakeWallet(Decimal(amount_out))
        # By default the swap delivers exactly what the quote promised, which is
        # what the searched spend is aiming at.
        self.short_delivery = Decimal(str(short_delivery)) if short_delivery else None
        self.status = status
        self.swaps: list[dict] = []
        self.preflights: list[dict] = []

    def spot_preflight(self, *, token_in, amount, decimals, balance_quote=None):
        """The entry pre-flight: allowance and balance, read before the order."""
        self.preflights.append({"token_in": token_in, "amount": str(amount),
                                "decimals": decimals,
                                "balance_quote": (str(balance_quote)
                                                  if balance_quote is not None else None)})
        return {"ok": True, "reason": ""}

    def swap_exact_out(self, **kwargs):
        """Whole-share buy: deliver exactly the requested shares."""
        self.swaps.append({**kwargs, "mode": "exactOut"})
        quantity = Decimal(str(kwargs["amount_out"]))
        receipt = ChainReceipt("0x" + "cd" * 32, self.status, block_number=124,
                               gas_used=21000, effective_gas_price_wei=0,
                               l1_fee_wei=0,
                               token_delta_raw=int(quantity * Decimal(10 ** 18)),
                               quote_delta_raw=int(Decimal(str(kwargs["max_amount_in"]))
                                                   * Decimal(10 ** 6)))
        if self.status != "filled":
            raise RuntimeError("onchain_leg_not_filled:" + self.status)
        return WalletSwapOutcome(
            leg="buy", tx_hash="0x" + "cd" * 32, order_id="196001", receipt=receipt,
            token_quantity=quantity,
            quote_amount=Decimal(str(kwargs["max_amount_in"])),
            quoted_token_quantity=quantity, amount_out_minimum=quantity,
            price_usd=Decimal(str(kwargs["max_amount_in"])) / quantity,
            route="0xrouter", price_impact_pct=Decimal("0.05"),
            trade_fee_usd=Decimal("0.0006"), simulation={},
        )

    def swap_exact_in(self, **kwargs):
        self.swaps.append(kwargs)
        # The searched spend delivers the planned shares (the receipt is what
        # the runner trusts, so the fake has to be explicit about it).
        delivered = (self.short_delivery if self.short_delivery is not None
                     else Decimal(str(kwargs["amount_in"])) / self.wallet.share_price)
        receipt = ChainReceipt(kwargs["token_in"] and "tx-1", self.status,
                               block_number=123, gas_used=21000,
                               effective_gas_price_wei=0, l1_fee_wei=0,
                               token_delta_raw=int(delivered * Decimal(10 ** 18)),
                               quote_delta_raw=int(Decimal(str(kwargs["amount_in"]))
                                                   * Decimal(10 ** int(kwargs["quote_decimals"]))))
        if self.status != "filled":
            raise RuntimeError("onchain_leg_not_filled:" + self.status)
        quantity = delivered
        return WalletSwapOutcome(
            leg=kwargs.get("leg", "buy"), tx_hash="0x" + "ab" * 32, order_id="196000",
            receipt=receipt, token_quantity=quantity,
            quote_amount=Decimal(str(kwargs["amount_in"])),
            quoted_token_quantity=Decimal(str(kwargs["amount_in"])) / Decimal("3.4178"),
            amount_out_minimum=Decimal("0"), price_usd=Decimal("4") / quantity,
            route="0xrouter", price_impact_pct=Decimal("-0.02"),
            trade_fee_usd=Decimal("0.000307773"), simulation={},
        )


class FakeQuoter:
    """The chain's exact-out price, answered locally.

    ``build_plan(shares=...)`` normally asks QuoterV2 on X Layer what a whole
    number of shares costs. That made this file read the network even though it
    promises "no network, no signing", so the answer now comes from the same
    3.42/share the fake pool uses.
    """

    def __init__(self, share_price="3.42"):
        self.share_price = Decimal(str(share_price))
        self.calls: list[tuple[int, int]] = []

    def __call__(self, *, pool, amount_raw, fee):
        self.calls.append((amount_raw, fee))
        shares = Decimal(amount_raw) / (Decimal(10) ** int(pool.get("base_decimals") or 18))
        spend = shares * self.share_price
        return int(spend * (Decimal(10) ** int(pool.get("quote_decimals") or 6)))


class FakeRead:
    def __init__(self, fills, positions=None, order_state="filled"):
        self.fills = fills
        self._positions = positions or []
        self.order_state = order_state

    def get(self, path, query=""):
        if path.endswith("/trade/fills"):
            return self.fills
        return [{"state": self.order_state}]

    def positions(self):
        return self._positions


class FakeOKX:
    def __init__(self, *, bid="3.369", fills=None, positions=None,
                 order_state="filled", spec=None):
        self.bid = Decimal(bid)
        self.orders: list[dict] = []
        self.read = FakeRead(fills if fills is not None else [
            {"fillSz": "1", "fillPx": "3.366", "fee": "-0.00151470"}],
            positions or [], order_state)
        self.spec = spec or {
            "instId": "XIAOMI-USDT-SWAP", "ctType": "linear", "ctVal": "1",
            "ctMult": "1", "minSz": "1", "lotSz": "1", "tickSz": "0.001",
        }

    def ticker(self, inst_id):
        return {"instId": inst_id, "bidPx": str(self.bid), "askPx": str(self.bid + Decimal("0.001"))}

    def instrument(self, inst_id):
        return self.spec

    def place_ioc(self, **kwargs):
        self.orders.append(kwargs)
        return {"ordId": "3920000000000000001", "sCode": "0", "sMsg": ""}


def runner(wallet_leg=None, okx=None, ledger=None, policy_overrides=None,
           monitor_overrides=None, quoter=None) -> FullChainRunner:
    policy = load_toml(str(ROOT / "config/automation_policy.toml"))
    monitor = load_toml(str(ROOT / "config/hk_monitor.toml"))
    # The tests must never read the operator's *live* stop flags: a drill that
    # writes data/AUTO_HALT or data/KILL_SWITCH is a fact about the running
    # system, and a test suite that fails because of it is measuring the wrong
    # thing. Both paths are redirected to one that cannot exist.
    policy.setdefault("execution", {})["auto_halt_file"] = str(
        Path(tempfile.gettempdir()) / "arb-tests-no-such-AUTO_HALT")
    policy["execution"]["kill_switch_file"] = str(
        Path(tempfile.gettempdir()) / "arb-tests-no-such-KILL_SWITCH")
    for key, value in (policy_overrides or {}).items():
        section, _, field = key.partition(".")
        policy.setdefault(section, {})[field] = value
    for key, value in (monitor_overrides or {}).items():
        section, _, field = key.partition(".")
        monitor.setdefault(section, {})[field] = value
    return FullChainRunner(wallet_leg=wallet_leg or FakeWalletLeg(), okx=okx or FakeOKX(),
                           policy=policy, monitor_config=monitor,
                           ledger=ledger or ExecutionLedger(), clock=lambda: 1_000_000,
                           sleeper=lambda _seconds: None,
                           quoter=quoter or FakeQuoter())


class PlanTests(unittest.TestCase):
    def test_plan_prices_the_spot_leg_and_blocks_a_negative_basis(self):
        chain = runner()
        plan = chain.build_plan(asset="XIAOMI", size_quote=Decimal("4"),
                                perp_instrument="XIAOMI-USDT-SWAP")
        # Whole-share sizing: the budget is a ceiling, the trade is 1 share, and
        # the spend is searched so the delivered quantity lands in
        # [1, 1 + max_unhedged_bps).
        self.assertEqual(plan.perp_contract_quantity, Decimal("1"))
        self.assertGreaterEqual(plan.expected_token_quantity, Decimal("1"))
        self.assertLess(plan.expected_token_quantity, Decimal("1.0005"))
        self.assertEqual(plan.perp_contract_quantity, Decimal("1"))
        self.assertGreaterEqual(plan.size_quote, Decimal("3.42"))
        self.assertLessEqual(plan.size_quote, Decimal("3.5"))
        self.assertEqual(plan.budget_quote, Decimal("4"))
        self.assertEqual(plan.quote_token_address, USDG.lower())
        self.assertEqual(plan.pool_quote_symbol, "USDC")
        self.assertLess(plan.net_bps, Decimal("0"))
        self.assertFalse(plan.signal_qualified)

    def test_an_explicit_share_count_is_honoured_within_tolerance(self):
        wallet = FakeWalletLeg()
        chain = runner(wallet, policy_overrides={"limits.per_trade_quote_usdt": "100"})
        plan = chain.build_plan(asset="XIAOMI", size_quote=Decimal("20"),
                                perp_instrument="XIAOMI-USDT-SWAP", shares=Decimal("5"))
        self.assertGreaterEqual(plan.expected_token_quantity, Decimal("5"))
        self.assertLess(plan.expected_token_quantity, Decimal("5.0025"))
        self.assertEqual(plan.perp_contract_quantity, Decimal("5"))
        # The spend never exceeds the budget, and the search stays bounded.
        self.assertLessEqual(plan.size_quote, Decimal("20"))
        self.assertLessEqual(len(wallet.wallet.quoted), 7)

    def test_a_budget_too_small_for_one_share_is_refused(self):
        with self.assertRaises(FullChainError):
            runner().build_plan(asset="XIAOMI", size_quote=Decimal("1"),
                                perp_instrument="XIAOMI-USDT-SWAP")

    def test_taker_limit_is_below_the_best_bid_and_tick_rounded(self):
        chain = runner()
        plan = chain.build_plan(asset="XIAOMI", size_quote=Decimal("4"),
                                perp_instrument="XIAOMI-USDT-SWAP")
        # 3.369 × (1 − 20 bps) = 3.3622618 → rounded down to tick 0.001.
        self.assertEqual(plan.perp_limit_price, Decimal("3.362"))
        self.assertLess(plan.perp_limit_price, plan.perp_bid_usd)

    def test_planning_does_not_touch_the_exchange_or_the_wallet_swap(self):
        wallet, okx = FakeWalletLeg(), FakeOKX()
        chain = runner(wallet, okx)
        chain.build_plan(asset="XIAOMI", size_quote=Decimal("4"),
                         perp_instrument="XIAOMI-USDT-SWAP")
        self.assertEqual(wallet.swaps, [])
        self.assertEqual(okx.orders, [])
        # Sizing reads prices only; nothing is signed or sent.
        self.assertGreaterEqual(len(wallet.wallet.quoted), 1)

    def test_unknown_asset_is_refused_instead_of_guessed(self):
        with self.assertRaises(FullChainError):
            runner().build_plan(asset="NOT_A_LISTING", size_quote=Decimal("4"),
                                perp_instrument="XIAOMI-USDT-SWAP")


class RiskGateTests(unittest.TestCase):
    def plan(self, chain):
        return chain.build_plan(asset="XIAOMI", size_quote=Decimal("4"),
                                perp_instrument="XIAOMI-USDT-SWAP", forced=True)

    def test_negative_basis_is_rejected_unless_the_operator_forces_it(self):
        chain = runner()
        plan = chain.build_plan(asset="XIAOMI", size_quote=Decimal("4"),
                                perp_instrument="XIAOMI-USDT-SWAP")
        allowed, reasons = chain.risk_check(plan, open_contracts=Decimal(0))
        self.assertFalse(allowed)
        self.assertIn("net_basis_below_threshold", reasons)
        forced = self.plan(chain)
        allowed, reasons = chain.risk_check(forced, open_contracts=Decimal(0))
        self.assertTrue(allowed, reasons)

    def test_instrument_outside_the_allowlist_is_rejected(self):
        chain = runner()
        plan = chain.build_plan(asset="XIAOMI", size_quote=Decimal("4"),
                                perp_instrument="XIAOMI-USDT-SWAP", forced=True)
        plan.perp_instrument = "TENCENT-USDT-SWAP"
        allowed, reasons = chain.risk_check(plan, open_contracts=Decimal(0))
        self.assertFalse(allowed)
        self.assertIn("instrument_not_allowlisted", reasons)

    def test_size_above_the_per_trade_cap_is_rejected(self):
        chain = runner()
        plan = chain.build_plan(asset="XIAOMI", size_quote=Decimal("4"),
                                perp_instrument="XIAOMI-USDT-SWAP", forced=True)
        plan.size_quote = Decimal("150")
        allowed, reasons = chain.risk_check(plan, open_contracts=Decimal(0))
        self.assertFalse(allowed)
        self.assertIn("per_trade_limit", reasons)

    def test_a_hedge_above_the_per_trade_cap_is_rejected(self):
        chain = runner()
        plan = chain.build_plan(asset="XIAOMI", size_quote=Decimal("4"),
                                perp_instrument="XIAOMI-USDT-SWAP", forced=True)
        plan.perp_contract_quantity = Decimal("100")
        allowed, reasons = chain.risk_check(plan, open_contracts=Decimal(0))
        self.assertFalse(allowed)
        self.assertIn("per_trade_hedge_limit", reasons)

    def test_an_explicit_share_count_is_cut_so_the_hedge_stays_within_10(self):
        chain = runner()
        plan = chain.build_plan(asset="XIAOMI", size_quote=Decimal("20"),
                                perp_instrument="XIAOMI-USDT-SWAP", shares=Decimal("5"))
        hedge = plan.perp_contract_quantity * plan.perp_contract_multiplier * plan.perp_bid_usd
        self.assertLessEqual(hedge, Decimal("10"))
        self.assertLessEqual(plan.size_quote, Decimal("10"))
        self.assertLess(plan.perp_contract_quantity, Decimal("5"))

    def test_an_open_contract_blocks_a_second_one(self):
        chain = runner()
        allowed, reasons = chain.risk_check(self.plan(chain), open_contracts=Decimal(1))
        self.assertFalse(allowed)
        self.assertIn("open_contract_limit", reasons)

    def test_insufficient_balance_is_rejected_before_any_swap(self):
        chain = runner()
        allowed, reasons = chain.risk_check(self.plan(chain), open_contracts=Decimal(0),
                                            balance_quote=Decimal("1"))
        self.assertFalse(allowed)
        self.assertIn("insufficient_quote_balance", reasons)

    def test_an_asset_outside_the_allowlist_is_rejected(self):
        chain = runner()
        plan = chain.build_plan(asset="XIAOMI", size_quote=Decimal("4"),
                                perp_instrument="XIAOMI-USDT-SWAP", forced=True)
        plan.asset = "NOT_ALLOWLISTED"
        allowed, reasons = chain.risk_check(plan, open_contracts=Decimal(0))
        self.assertFalse(allowed)
        self.assertIn("asset_not_allowlisted", reasons)


class ExecutionOrderTests(unittest.TestCase):
    def test_open_limit_rounds_down_and_uses_the_live_bid(self):
        wallet, okx = FakeWalletLeg(), FakeOKX(bid="3.369")
        chain = runner(wallet, okx)
        plan = chain.build_plan(asset="XIAOMI", size_quote=Decimal("4"),
                                perp_instrument="XIAOMI-USDT-SWAP", forced=True)
        plan.perp_price_tick = Decimal("0.01")
        chain.execute_open(plan, live=True)
        # 3.369 × (1 − 0.002) = 3.362262 → round DOWN to the 0.01 tick.
        self.assertEqual(okx.orders[0]["price"], Decimal("3.36"))

    def test_close_limit_uses_the_ask_and_rounds_up(self):
        wallet, okx = FakeWalletLeg(), FakeOKX(bid="3.369")
        chain = runner(wallet, okx)
        plan = chain.build_plan(asset="XIAOMI", size_quote=Decimal("4"),
                                perp_instrument="XIAOMI-USDT-SWAP", forced=True)
        plan.perp_price_tick = Decimal("0.01")
        chain.execute_close(plan, live=True, contract_quantity=Decimal("1"),
                            token_quantity=Decimal("1"))
        # Buying back pays the ask (3.370) + the band, rounded UP so the band is
        # never narrowed by tick rounding.
        self.assertEqual(okx.orders[0]["price"], Decimal("3.38"))

    def test_the_submit_time_reference_price_goes_into_the_ledger(self):
        wallet, okx = FakeWalletLeg(), FakeOKX(bid="3.35")
        ledger = ExecutionLedger()
        chain = runner(wallet, okx, ledger)
        plan = chain.build_plan(asset="XIAOMI", size_quote=Decimal("4"),
                                perp_instrument="XIAOMI-USDT-SWAP", forced=True)
        chain.execute_open(plan, live=True)
        submitted = [e for e in ledger.events(plan.intent.signal_id)
                     if e["event_type"] == "perp_submitted"][0]
        self.assertEqual(submitted["payload"]["reference_price_at_submit"], "3.35")
        self.assertEqual(submitted["payload"]["style"], "taker_ioc_at_best_bid")

    def test_dry_run_never_places_an_order(self):
        wallet, okx = FakeWalletLeg(), FakeOKX()
        chain = runner(wallet, okx)
        plan = chain.build_plan(asset="XIAOMI", size_quote=Decimal("4"),
                                perp_instrument="XIAOMI-USDT-SWAP", forced=True)
        result = chain.execute_open(plan, live=False)
        self.assertEqual(result.state, "dry_run_complete")
        self.assertEqual(wallet.swaps, [])
        self.assertEqual(okx.orders, [])

    def test_the_hedge_is_only_sent_after_a_confirmed_spot_fill(self):
        wallet, okx = FakeWalletLeg(), FakeOKX()
        ledger = ExecutionLedger()
        chain = runner(wallet, okx, ledger)
        plan = chain.build_plan(asset="XIAOMI", size_quote=Decimal("4"),
                                perp_instrument="XIAOMI-USDT-SWAP", forced=True)
        result = chain.execute_open(plan, live=True)
        self.assertEqual(result.state, "hedged")
        self.assertEqual(len(okx.orders), 1)
        order = okx.orders[0]
        self.assertEqual(order["side"], "sell")
        self.assertEqual(order["size"], Decimal("1"))
        self.assertEqual(order["price"], plan.perp_limit_price)
        self.assertNotIn("-", order["client_order_id"])
        events = [event["event_type"] for event in ledger.events(plan.intent.signal_id)]
        self.assertEqual(events, ["precheck", "spot_filled", "perp_submitted", "hedged"])

    def test_a_reverted_spot_leg_stops_before_the_hedge(self):
        wallet, okx = FakeWalletLeg(status="reverted"), FakeOKX()
        chain = runner(wallet, okx)
        plan = chain.build_plan(asset="XIAOMI", size_quote=Decimal("4"),
                                perp_instrument="XIAOMI-USDT-SWAP", forced=True)
        with self.assertRaises(RuntimeError):
            chain.execute_open(plan, live=True)
        self.assertEqual(okx.orders, [])

    def test_a_spot_leg_that_does_not_deliver_the_shares_is_not_hedged(self):
        # The spend is searched so the fill lands at the planned share count.
        # If the receipt still disagrees, the run must stop instead of hedging a
        # guessed quantity.
        wallet, okx = FakeWalletLeg(short_delivery="0.9"), FakeOKX()
        chain = runner(wallet, okx)
        plan = chain.build_plan(asset="XIAOMI", size_quote=Decimal("4"), shares=Decimal("1"),
                                perp_instrument="XIAOMI-USDT-SWAP", forced=True)
        result = chain.execute_open(plan, live=True)
        self.assertEqual(result.state, "unhedged_spot_only")
        self.assertEqual(okx.orders, [])
        self.assertIn("未提交合约腿", result.hedge_hint)

    def test_a_partial_hedge_is_reported_as_unhedged_not_as_hedged(self):
        # 2 shares bought exactly, but the venue only fills 1 contract.
        wallet, okx = FakeWalletLeg(), FakeOKX(
            fills=[{"fillSz": "1", "fillPx": "3.366", "fee": "-0.00151470"}])
        chain = runner(wallet, okx)
        plan = chain.build_plan(asset="XIAOMI", size_quote=Decimal("20"),
                                perp_instrument="XIAOMI-USDT-SWAP",
                                shares=Decimal("2"), forced=True)
        result = chain.execute_open(plan, live=True)
        # 2 shares wanted 2 contracts; only 1 filled → 1 share unhedged.
        self.assertEqual(okx.orders[0]["size"], Decimal("2"))
        self.assertEqual(result.state, "perp_underfilled")
        # 2 shares delivered, 1 contract filled → the residual is that one
        # contract. The exact-out quote is exact, so the buy lands on 2.0 and
        # the unhedged amount is the unfilled contract, not dust on top of it.
        self.assertGreaterEqual(result.unhedged_tokens, Decimal("1"))
        self.assertLess(result.unhedged_tokens, Decimal("1.001"))
        self.assertTrue(result.warnings)

    def test_close_reduces_only_and_sells_the_spot_back(self):
        wallet, okx = FakeWalletLeg(), FakeOKX()
        chain = runner(wallet, okx)
        plan = chain.build_plan(asset="XIAOMI", size_quote=Decimal("4"),
                                perp_instrument="XIAOMI-USDT-SWAP", forced=True)
        result = chain.execute_close(plan, live=True, token_quantity=Decimal("1.1703149"),
                                     contract_quantity=Decimal("1"))
        self.assertEqual(result.state, "closed")
        self.assertTrue(okx.orders[0]["reduce_only"])
        self.assertEqual(okx.orders[0]["side"], "buy")
        self.assertEqual(wallet.swaps[0]["leg"], "sell")

    def test_a_close_that_cannot_buy_back_the_hedge_does_not_sell_the_spot(self):
        # Measured 2026-09-16: an IOC buy-back missed, the run sold the spot
        # anyway and reported "closed", which left a naked short. The spot leg
        # must not be sent unless the hedge is actually back.
        wallet, okx = FakeWalletLeg(), FakeOKX(fills=[])
        chain = runner(wallet, okx)
        plan = chain.build_plan(asset="XIAOMI", size_quote=Decimal("15"),
                                perp_instrument="XIAOMI-USDT-SWAP",
                                shares=Decimal("3"), forced=True)
        result = chain.execute_close(plan, live=True,
                                     token_quantity=Decimal("3.0007"),
                                     contract_quantity=Decimal("3"))
        self.assertEqual(result.state, "close_perp_underfilled")
        self.assertEqual(wallet.swaps, [])          # spot leg never sent
        self.assertIn("现货腿未卖出", result.hedge_hint)

    def test_a_perp_only_close_skips_the_spot_leg(self):
        # Recovery path for a short left open by an earlier failed close.
        wallet, okx = FakeWalletLeg(), FakeOKX(
            fills=[{"fillSz": "3", "fillPx": "3.354", "fee": "-0.00452745"}])
        chain = runner(wallet, okx)
        plan = chain.build_close_plan(asset="XIAOMI",
                                      perp_instrument="XIAOMI-USDT-SWAP",
                                      token_quantity=Decimal("0"),
                                      contract_quantity=Decimal("3"))
        result = chain.execute_close(plan, live=True, token_quantity=Decimal("0"),
                                     contract_quantity=Decimal("3"))
        self.assertEqual(result.state, "perp_only_closed")
        self.assertEqual(wallet.swaps, [])
        self.assertEqual(len(okx.orders), 1)
        self.assertTrue(okx.orders[0]["reduce_only"])


class LedgerPersistenceTests(unittest.TestCase):
    def test_the_run_survives_a_restart_through_the_ledger(self):
        import tempfile
        from pathlib import Path as P

        with tempfile.TemporaryDirectory() as directory:
            path = P(directory) / "exec.db"
            ledger = ExecutionLedger(path)
            chain = runner(ledger=ledger)
            plan = chain.build_plan(asset="XIAOMI", size_quote=Decimal("4"),
                                    perp_instrument="XIAOMI-USDT-SWAP", forced=True)
            chain.execute_open(plan, live=True)
            ledger.close()
            reopened = ExecutionLedger(path)
            try:
                groups = reopened.recover()
                self.assertIn(plan.intent.signal_id, groups)
                self.assertEqual(groups[plan.intent.signal_id]["state"], "hedged")
                self.assertEqual([e["event_type"] for e in
                                  reopened.events(plan.intent.signal_id)][0], "precheck")
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
