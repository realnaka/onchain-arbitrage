"""Maker-first inverts the spot-first invariant, so the bounds are the feature.

The saving is real (measured: the perpetual spread alone was 41-48 bps, more
than every fee and the on-chain spread together). The risk is also real: between
the fill and the spot buy the position is short with nothing behind it. These
tests pin the four controls that make that acceptable instead of reckless.
"""
from __future__ import annotations

from decimal import Decimal
import unittest

from arb_monitor.execution.maker_first import MakerFirstError, MakerFirstOpener
from arb_monitor.execution.order_flows import classify_flow


class FakeOrder(dict):
    """The venue returns a dict; keep the fake the same shape."""

    def __init__(self, ordId="42"):
        super().__init__(ordId=ordId, sCode="0", sMsg="")


class FakeOKX:
    """A venue that fills, partly fills, or never fills, on demand."""

    def __init__(self, fill_plan=None, ask="3.367", state_after_cancel="canceled",
                 fill_by_order=None, fail_reads=None):
        # fill_plan: list of accFillSz values returned on successive polls
        self.fill_plan = list(fill_plan if fill_plan is not None else ["3", "3", "3"])
        # Optional per-order fill: order 1 unfilled, order 2 filled, and so on.
        self.fill_by_order = list(fill_by_order) if fill_by_order else None
        # fail_reads: how many get_order calls raise before answering.
        self.fail_reads = fail_reads
        self.ask = Decimal(ask)
        self.state_after_cancel = state_after_cancel
        self.placed: list[dict] = []
        self.cancelled: list[str] = []
        self.ioc: list[dict] = []
        self.poll_index = 0
        self.current_fill = Decimal(0)
        self.requested = Decimal(0)
        # The opener sets the instrument leverage before it rests the hedge, so
        # a fake without this method silently becomes a "leverage_not_set"
        # block and every flow test fails with the wrong reason.
        self.leverages: list[tuple[str, str]] = []

    class Read:
        def __init__(self, outer):
            self.outer = outer

        def get(self, path, query=""):
            if path.endswith("/trade/fills"):
                filled = self.outer.current_fill
                if filled <= 0:
                    return []
                price = str(self.outer.placed[-1]["price"]) if self.outer.placed else "3.36"
                # one row per contract, so partial fills look like real partials
                return [{"fillSz": "1", "fillPx": price, "fee": "-0.0000538"}
                        for _ in range(int(filled))]
            return []

    def __post_init__(self):
        pass

    def set_leverage(self, inst_id, lever="1", td_mode="cross"):
        self.leverages.append((inst_id, str(lever)))
        return {"instId": inst_id, "lever": str(lever), "mgnMode": td_mode}

    def place_post_only(self, *, inst_id, side, size, price, client_order_id, td_mode="cross"):
        assert side == "sell", "maker-first rests the hedge on the offer"
        # The venue, not this fake, enforces "never cross": post-only cancels a
        # marketable order. The tests assert on the recorded price instead.
        self.placed.append({"inst_id": inst_id, "size": size, "price": price,
                            "client_order_id": client_order_id})
        self.requested = Decimal(str(size))
        return FakeOrder()

    def get_order(self, inst_id, ord_id):
        if self.fail_reads is not None and self.fail_reads > 0:
            self.fail_reads -= 1
            raise RuntimeError("okx_transport:URLError")
        if self.fill_by_order is not None:
            index = min(len(self.fill_by_order) - 1, max(0, len(self.placed) - 1))
            step = self.fill_by_order[index]
        else:
            step = self.fill_plan[min(len(self.fill_plan) - 1, self.poll_index)]
        self.poll_index += 1
        self.current_fill = Decimal(str(step))
        state = "filled" if self.current_fill >= self.requested else "live"
        if self.cancelled:
            state = self.state_after_cancel
        return {"ordId": ord_id, "state": state, "accFillSz": str(self.current_fill)}

    def cancel_order(self, inst_id, ord_id):
        self.cancelled.append(ord_id)
        return {"cancelled": True, "sCode": "0"}

    def place_ioc(self, **kwargs):
        self.ioc.append(kwargs)
        return FakeOrder("unwind-1")

    def __getattr__(self, name):
        if name == "read":
            return FakeOKX.Read(self)
        raise AttributeError(name)


class FakeSpot:
    def __init__(self, fail=False, deliver=None, preflight_ok=True,
                 preflight_reason=""):
        self.fail, self.deliver = fail, deliver
        self.calls: list[dict] = []
        # The entry pre-flight reads the allowance and the balance before the
        # perpetual leg is rested. The default is "fine"; the tests that care
        # pass ``preflight_ok=False``.
        self.preflight_ok = preflight_ok
        self.preflight_reason = preflight_reason
        self.preflights: list[dict] = []

    def spot_preflight(self, *, token_in, amount, decimals, balance_quote=None):
        self.preflights.append({"token_in": token_in, "amount": str(amount),
                                "decimals": decimals,
                                "balance_quote": (str(balance_quote)
                                                  if balance_quote is not None else None)})
        return {"ok": self.preflight_ok, "reason": self.preflight_reason}

    def swap_exact_in(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("simulation_reverted")
        from arb_monitor.execution.onchain_wallet import ChainReceipt, WalletSwapOutcome
        quantity = self.deliver if self.deliver is not None else Decimal("3")
        return WalletSwapOutcome(
            leg="buy", tx_hash="0x" + "ab" * 32, order_id="196",
            receipt=ChainReceipt("0x" + "ab" * 32, "filled"),
            token_quantity=quantity, quote_amount=Decimal(str(kwargs["amount_in"])),
            quoted_token_quantity=quantity, amount_out_minimum=quantity,
            price_usd=Decimal(str(kwargs["amount_in"])) / quantity,
            route="0xrouter", price_impact_pct=Decimal("0.05"),
            trade_fee_usd=Decimal("0.0006"), simulation={})


class FakePlan:
    perp_instrument = "XIAOMI-USDT-SWAP"
    perp_contract_quantity = Decimal("3")
    perp_contract_multiplier = Decimal("1")
    perp_lot_size = Decimal("1")
    perp_price_tick = Decimal("0.001")
    perp_max_slippage_bps = Decimal("20")
    chain_id = "196"
    quote_token_address = "0xusdg"
    token_address = "0xtoken"
    token_decimals = 18
    quote_decimals = 6
    expected_token_quantity = Decimal("3.0007")
    size_quote = Decimal("10.25")

    def perp_best_ask(self):
        return Decimal("3.367")

    def perp_limit_price_for_close(self):
        return Decimal("3.377")


def opener(okx, spot):
    return MakerFirstOpener(okx=okx, wallet_leg=spot, sleeper=lambda _s: None,
                            clock_ms=_Clock())


class _Clock:
    def __init__(self, step_ms=1000):
        self.now = 0
        self.step = step_ms

    def __call__(self):
        self.now += self.step
        return self.now


def slow_opener(okx, spot):
    """100ms per clock call, so a multi-attempt flow fits in a small window."""
    return MakerFirstOpener(okx=okx, wallet_leg=spot, sleeper=lambda _s: None,
                            clock_ms=_Clock(step_ms=100))


class MakerFirstTests(unittest.TestCase):
    def test_a_fill_is_followed_by_a_spot_buy_of_exactly_that_size(self):
        okx, spot = FakeOKX(fill_plan=["3"]), FakeSpot()
        result = opener(okx, spot).open(plan=FakePlan(), client_order_id="arb1",
                                        timeout_seconds=30)
        self.assertEqual(result.state, "hedged")
        self.assertEqual(result.maker.filled, Decimal("3"))
        # Scaled from the plan's own searched spend for 3.0007 shares.
        self.assertAlmostEqual(float(spot.calls[0]["amount_in"]), 10.2476, places=4)
        self.assertEqual(okx.ioc, [])            # nothing to unwind

    def test_a_partial_fill_only_buys_what_filled(self):
        okx, spot = FakeOKX(fill_plan=["1", "1", "1"]), FakeSpot(deliver=Decimal("1"))
        result = opener(okx, spot).open(plan=FakePlan(), client_order_id="arb1",
                                        timeout_seconds=5)
        self.assertEqual(result.maker.filled, Decimal("1"))
        # 1 of 3 contracts → about a third of the planned cash, not all of it.
        self.assertLess(spot.calls[0]["amount_in"], Decimal("4"))
        self.assertIn(result.state, {"hedged", "spot_short_of_hedge"})

    def test_no_fill_means_no_spot_purchase_and_no_naked_position(self):
        okx, spot = FakeOKX(fill_plan=["0", "0", "0"]), FakeSpot()
        result = opener(okx, spot).open(plan=FakePlan(), client_order_id="arb1",
                                        timeout_seconds=2)
        self.assertEqual(result.state, "maker_unfilled")
        self.assertEqual(spot.calls, [])          # never bought anything
        self.assertEqual(len(okx.cancelled), 1)   # the resting order was pulled
        self.assertEqual(okx.ioc, [])

    def test_a_failed_spot_leg_unwinds_the_short_immediately(self):
        okx, spot = FakeOKX(fill_plan=["3"]), FakeSpot(fail=True)
        result = opener(okx, spot).open(plan=FakePlan(), client_order_id="arb1",
                                        timeout_seconds=30)
        self.assertEqual(result.state, "unwound_after_spot_failure")
        self.assertEqual(len(okx.ioc), 1)
        self.assertTrue(okx.ioc[0]["reduce_only"])
        self.assertEqual(okx.ioc[0]["side"], "buy")
        self.assertEqual(result.unwind["state"], "unwound")

    def test_the_maker_price_joins_the_best_ask(self):
        okx, spot = FakeOKX(fill_plan=["3"], ask="4.86"), FakeSpot()
        opener(okx, spot).open(plan=FakePlan(), client_order_id="arb1",
                               timeout_seconds=5, maker_price=Decimal("4.86"))
        self.assertEqual(okx.placed[0]["price"], Decimal("4.86"))

    def test_a_partial_fill_is_cancelled_not_chased(self):
        okx, spot = FakeOKX(fill_plan=["1", "1"]), FakeSpot(deliver=Decimal("1"))
        opener(okx, spot).open(plan=FakePlan(), client_order_id="arb1",
                               timeout_seconds=1)
        # One cancel, no second order, no price chasing.
        self.assertEqual(len(okx.placed), 1)
        self.assertEqual(len(okx.cancelled), 1)

    def test_an_unfilled_would_cross_order_rejoins_the_offer(self):
        # Measured 2026-09-16: post-only cancelled the order because the price
        # read earlier had slipped below the bid. It must re-join the *new* ask,
        # not re-send the stale price.
        okx, spot = FakeOKX(fill_plan=["0", "0", "0", "3"]), FakeSpot()
        okx = FakeOKX(fill_by_order=["0", "3"])
        prices = iter([Decimal("3.341"), Decimal("3.343"), Decimal("3.345")])
        result = slow_opener(okx, spot).open(
            plan=FakePlan(), client_order_id="arb1", timeout_seconds=30,
            price_source=lambda: next(prices), max_reprices=3)
        self.assertEqual(result.state, "hedged")
        # First order never filled (stale price pulled by post-only), the second
        # one joined the new offer and filled.
        self.assertEqual([p["price"] for p in okx.placed],
                         [Decimal("3.341"), Decimal("3.343")])
        self.assertNotEqual(okx.placed[0]["client_order_id"],
                            okx.placed[1]["client_order_id"])

    def test_repricing_stops_at_the_configured_limit(self):
        okx, spot = FakeOKX(fill_plan=["0"]), FakeSpot()
        prices = iter([Decimal("3.341")] * 5)
        result = slow_opener(okx, spot).open(
            plan=FakePlan(), client_order_id="arb1", timeout_seconds=30,
            price_source=lambda: next(prices), max_reprices=2)
        self.assertEqual(result.state, "maker_unfilled")
        self.assertEqual(len(okx.placed), 3)   # initial + 2 reprices
        self.assertEqual(spot.calls, [])

    def test_an_unreadable_order_never_triggers_a_spot_purchase(self):
        # Measured 2026-09-16: a transport blip while polling aborted the flow
        # with the order possibly still live. The safe answer is "unknown":
        # never buy against a hedge state we cannot read, and try to pull it.
        okx, spot = FakeOKX(fill_by_order=["3"], fail_reads=99), FakeSpot()
        result = slow_opener(okx, spot).open(
            plan=FakePlan(), client_order_id="arb1", timeout_seconds=5,
            price_source=lambda: Decimal("3.341"), max_reprices=0)
        self.assertEqual(result.state, "maker_unknown")
        self.assertEqual(spot.calls, [])                    # nothing bought
        self.assertIn("manual_reconciliation", result.reason)
        self.assertEqual(len(okx.cancelled), 1)             # tried to pull it

    def test_a_transient_read_failure_is_retried_not_fatal(self):
        okx, spot = FakeOKX(fill_by_order=["3"], fail_reads=2), FakeSpot()
        result = slow_opener(okx, spot).open(
            plan=FakePlan(), client_order_id="arb1", timeout_seconds=10,
            price_source=lambda: Decimal("3.341"), max_reprices=0)
        self.assertEqual(result.state, "hedged")

    def test_an_unconfirmed_cancel_is_unknown_not_unfilled(self):
        # Measured 2026-09-16: the venue never confirmed the cancel, the flow
        # reported "unfilled", and the order rested for 16 minutes before
        # filling into a naked short. An order that may still be live has to be
        # reported as unknown, and no spot may be bought against it.
        okx, spot = FakeOKX(fill_plan=["0"], state_after_cancel="live"), FakeSpot()
        result = opener(okx, spot).open(plan=FakePlan(), client_order_id="arb1",
                                        timeout_seconds=2)
        self.assertEqual(result.state, "maker_unknown")
        self.assertEqual(spot.calls, [])
        # It kept trying to pull the order instead of believing one request.
        self.assertEqual(len(okx.cancelled), 3)


class EntryPreflightTests(unittest.TestCase):
    """Allowance and balance are checked before the naked window opens.

    2026-09-18 (SHEIN, live): 2 contracts filled, the spot swap reverted on the
    ERC-20 allowance, and buying the short back cost 0.0264 USDT for a failure
    that was knowable before the order was sent. The trade-off is explicit: a
    blocked candidate is a *skip* — nothing was sent, so it is neither a success
    nor a halt, and automation carries on with the next cycle.
    """

    def test_a_short_allowance_blocks_the_order_before_it_is_rested(self):
        okx = FakeOKX(fill_plan=["3"])
        spot = FakeSpot(preflight_ok=False,
                        preflight_reason="allowance_short:1006008<10339067")
        result = opener(okx, spot).open(plan=FakePlan(), client_order_id="arb1",
                                        timeout_seconds=30)
        self.assertEqual(result.state, "spot_preflight_blocked")
        self.assertEqual(okx.placed, [])          # no order was ever rested
        self.assertEqual(okx.ioc, [])             # so there was nothing to unwind
        self.assertEqual(spot.calls, [])          # and no swap was attempted
        self.assertIn("allowance_short", result.reason)
        # A skip, not a stop: nothing was sent, so nothing has to be reconciled.
        executed, halt = classify_flow("open", result.state, result.residual_tokens)
        self.assertFalse(executed)
        self.assertFalse(halt)
        self.assertTrue(result.events[0]["event"] == "spot_preflight_blocked")

    def test_the_preflight_asks_for_the_full_planned_spend(self):
        # The perpetual fill may be partial, so the planned spend is the upper
        # bound of what the spot leg can need — it is what has to be affordable.
        okx, spot = FakeOKX(fill_plan=["3"]), FakeSpot()
        opener(okx, spot).open(plan=FakePlan(), client_order_id="arb1",
                               timeout_seconds=30, balance_quote=Decimal("10.9"))
        self.assertEqual(len(spot.preflights), 1)
        self.assertEqual(spot.preflights[0]["amount"], str(FakePlan.size_quote))
        self.assertEqual(spot.preflights[0]["balance_quote"], "10.9")

    def test_a_wallet_leg_that_cannot_answer_blocks_rather_than_assumes(self):
        class BlindSpot(FakeSpot):
            spot_preflight = None       # e.g. a leg that was never wired up

        okx, spot = FakeOKX(fill_plan=["3"]), BlindSpot()
        result = opener(okx, spot).open(plan=FakePlan(), client_order_id="arb1",
                                        timeout_seconds=30)
        self.assertEqual(result.state, "spot_preflight_blocked")
        self.assertEqual(okx.placed, [])

    def test_a_preflight_read_failure_is_a_block_not_an_exception(self):
        """The allowance read is an RPC read; losing it cannot mean exposure.

        Nothing has been sent when the pre-flight runs, so a failure here has to
        come back as the same skip as "the allowance is short". It used to
        escape as an exception, and the unattended executor reports any failure
        after ``entered_flow`` as a possible write in flight — 2026-09-21 06:47
        is exactly that story with the OKX position read.
        """
        class Unreadable(FakeSpot):
            def spot_preflight(self, **kwargs):
                raise RuntimeError("rpc_timeout")

        okx, spot = FakeOKX(fill_plan=["3"]), Unreadable()
        result = opener(okx, spot).open(plan=FakePlan(), client_order_id="arb1",
                                        timeout_seconds=30)
        self.assertEqual(result.state, "spot_preflight_blocked")
        self.assertIn("preflight_error:RuntimeError", result.reason)
        self.assertEqual(okx.placed, [])
        self.assertEqual(okx.ioc, [])
        executed, halt = classify_flow("open", result.state, result.residual_tokens)
        self.assertFalse(executed)
        self.assertFalse(halt)

    def test_an_unreadable_fills_read_does_not_abort_the_order_flow(self):
        """`/trade/fills` is evidence; losing it must not end the flow.

        Measured 2026-09-21 06:45→06:50: that read raised with the transport
        down, escaped `place_and_wait` into the executor's generic handler, and
        wrote AUTO_HALT *after* the maker order had been placed. The order kept
        resting and filled three minutes later as a naked short. The decision
        inputs are the order row's own state and `accFillSz`; the fee is not.
        """
        okx, spot = FakeOKX(fill_plan=["3"]), FakeSpot()

        def broken(path, query=""):
            raise RuntimeError("okx_transport:URLError")

        okx.read = type("Unreadable", (), {"get": staticmethod(broken)})()
        result = opener(okx, spot).open(plan=FakePlan(), client_order_id="arb1",
                                        timeout_seconds=30)
        self.assertEqual(result.state, "hedged")
        self.assertEqual(result.maker.fee, Decimal(0))
        self.assertEqual(len(spot.calls), 1)


if __name__ == "__main__":
    unittest.main()
