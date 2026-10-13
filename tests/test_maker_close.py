"""Maker close: rest the bid, sell what was bought back, fall back to taker.

Closing is the mirror of opening, but it is not the same trade. The window is
minutes rather than seconds, the order is not chased, and the dangerous leg is
inverted: opening risks a naked *short*, closing risks a naked *long* if the
spot sale fails after the perpetual is already flat. These tests pin the five
controls the operator asked for on 2026-09-16.
"""
from __future__ import annotations

from decimal import Decimal
import unittest

from arb_monitor.execution.maker_first import MakerFirstCloser
from arb_monitor.execution.onchain_wallet import ChainReceipt, WalletSwapOutcome


class FakeOrder(dict):
    def __init__(self, ordId="close-1"):
        super().__init__(ordId=ordId, sCode="0", sMsg="")


class FakeOKX:
    """A venue whose resting order fills, partly fills or never fills."""

    def __init__(self, fill_plan=None, ioc_fill="2", state_after_cancel="canceled",
                 fail_reads=None, fill_by_order=None):
        self.fill_plan = list(fill_plan if fill_plan is not None else ["3"])
        # Per-order fills: order 1 unfilled, order 2 filled, and so on.
        self.fill_by_order = list(fill_by_order) if fill_by_order else None
        self.ioc_fill = Decimal(str(ioc_fill))
        self.state_after_cancel = state_after_cancel
        self.fail_reads = fail_reads
        self.placed: list[dict] = []
        self.cancelled: list[str] = []
        self.ioc: list[dict] = []
        self.poll_index = 0
        self.current_fill = Decimal(0)
        self.requested = Decimal(0)
        self.fills_by_order: dict[str, Decimal] = {}

    class Read:
        def __init__(self, outer):
            self.outer = outer

        def get(self, path, query=""):
            if not path.endswith("/trade/fills"):
                return []
            ord_id = ""
            for part in query.split("&"):
                if part.startswith("ordId="):
                    ord_id = part.split("=", 1)[1]
            filled = self.outer.fills_by_order.get(ord_id, Decimal(0))
            return [{"fillSz": "1", "fillPx": "3.34", "fee": "-0.000059"}
                    for _ in range(int(filled))]

    def place_post_only(self, *, inst_id, side, size, price, client_order_id,
                        td_mode="cross", **kwargs):
        assert side == "buy", "a close rests a bid, it does not sell"
        self.placed.append({"inst_id": inst_id, "size": size, "price": price,
                            "client_order_id": client_order_id, **kwargs})
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
        self.fills_by_order[ord_id] = self.current_fill
        return {"ordId": ord_id, "state": state, "accFillSz": str(self.current_fill)}

    def cancel_order(self, inst_id, ord_id):
        self.cancelled.append(ord_id)
        return {"cancelled": True, "sCode": "0"}

    def place_ioc(self, **kwargs):
        self.ioc.append(kwargs)
        order_id = f"ioc-{len(self.ioc)}"
        self.fills_by_order[order_id] = self.ioc_fill
        return FakeOrder(order_id)

    def __getattr__(self, name):
        if name == "read":
            return FakeOKX.Read(self)
        raise AttributeError(name)


class FakeSpot:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls: list[dict] = []
        # Fail the first N attempts, then succeed (or fail all of them).
        self.fail_attempts = 0

    def swap_exact_in(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail or len(self.calls) <= self.fail_attempts:
            raise RuntimeError("api_failed_or_confirmation_required")
        quantity = Decimal(str(kwargs["amount_in"]))
        return WalletSwapOutcome(
            leg="sell", tx_hash="0x" + "cd" * 32, order_id="196",
            receipt=ChainReceipt("0x" + "cd" * 32, "filled"),
            token_quantity=quantity,
            quote_amount=quantity * Decimal("3.3"),
            quoted_token_quantity=quantity, amount_out_minimum=Decimal("0"),
            price_usd=Decimal("3.3"), route="0xrouter",
            price_impact_pct=Decimal("0.05"), trade_fee_usd=Decimal("0.0006"),
            simulation={})


class FakePlan:
    perp_instrument = "XIAOMI-USDT-SWAP"
    perp_contract_multiplier = Decimal("1")
    perp_lot_size = Decimal("1")
    chain_id = "196"
    token_address = "0xtoken"
    quote_token_address = "0xusdg"
    token_decimals = 18
    quote_decimals = 6
    perp_ask_usd = Decimal("3.371")
    perp_bid_usd = Decimal("3.367")

    def perp_best_bid(self):
        return self.perp_bid_usd

    def perp_limit_price_for_close(self):
        return Decimal("3.378")


class _Clock:
    def __init__(self, step_ms=100):
        self.now, self.step = 0, step_ms

    def __call__(self):
        self.now += self.step
        return self.now


def close(okx, spot, **overrides):
    kwargs = dict(plan=FakePlan(), client_order_id="arbc1", contracts=Decimal("3"),
                  token_quantity=Decimal("3"), timeout_seconds=600,
                  poll_seconds=2, fallback="taker")
    kwargs.update(overrides)
    balance = kwargs.pop("token_balance", None)
    attempts = kwargs.pop("spot_attempts", 5)
    closer = MakerFirstCloser(okx=okx, wallet_leg=spot, sleeper=lambda _s: None,
                              clock_ms=_Clock(step_ms=100),
                              token_balance=balance, spot_attempts=attempts,
                              spot_backoff_seconds=0)
    return closer.close(**kwargs)


class MakerCloseTests(unittest.TestCase):
    def test_a_filled_bid_sells_exactly_the_contracts_bought_back(self):
        okx, spot = FakeOKX(fill_plan=["3"]), FakeSpot()
        result = close(okx, spot)
        self.assertEqual(result.state, "closed")
        self.assertEqual(result.maker.filled, Decimal("3"))
        # The resting order joins the bid, never the ask.
        self.assertEqual(okx.placed[0]["price"], Decimal("3.367"))
        self.assertTrue(okx.placed[0].get("reduce_only"))
        # Sold the shares the fill covers, and nothing else.
        self.assertEqual(len(spot.calls), 1)
        self.assertEqual(spot.calls[0]["leg"], "sell")
        self.assertEqual(spot.calls[0]["amount_in"], Decimal("3"))
        self.assertEqual(okx.ioc, [])          # no fallback needed

    def test_a_partial_fill_sells_the_spot_that_fill_covers(self):
        # Operator's decision, 2026-09-16: a partial fill sells the spot it
        # covers immediately, instead of waiting for the whole buy-back. 1 of 3
        # contracts fill at the bid → 1 share is sold; the taker fallback buys
        # back the remaining 2 → the other 2 shares are sold. Both legs shrink
        # together, and no share is left without a short behind it.
        okx, spot = FakeOKX(fill_plan=["1"], ioc_fill="2"), FakeSpot()
        result = close(okx, spot)
        self.assertEqual(result.state, "closed")
        self.assertEqual(len(okx.ioc), 1)
        self.assertEqual(okx.ioc[0]["size"], Decimal("2"))
        self.assertTrue(okx.ioc[0]["reduce_only"])
        self.assertEqual([call["amount_in"] for call in spot.calls],
                         [Decimal("1"), Decimal("2")])
        self.assertEqual(sum(call["amount_in"] for call in spot.calls), Decimal("3"))
        self.assertEqual(result.residual_tokens, Decimal("0"))

    def test_a_partial_fill_with_no_fallback_leaves_a_balanced_partial_close(self):
        # No taker fallback configured: the run stops with both legs shrunk by
        # the same amount. That is unfinished, not stranded — the residual is
        # zero and nothing has to be paged, but a second run is needed.
        okx, spot = FakeOKX(fill_plan=["1"]), FakeSpot()
        result = close(okx, spot, fallback="none")
        self.assertEqual(result.state, "closed_partial")
        self.assertEqual([call["amount_in"] for call in spot.calls], [Decimal("1")])
        self.assertEqual(result.residual_tokens, Decimal("0"))
        self.assertEqual(result.filled_contracts, Decimal("1"))
        from arb_monitor.execution.order_flows import classify_flow
        self.assertEqual(classify_flow("close", "closed_partial", Decimal("0")),
                         (False, False))

    def test_an_underfilled_taker_fallback_never_sells_the_spot(self):
        okx, spot = FakeOKX(fill_plan=["0"], ioc_fill="2"), FakeSpot()
        result = close(okx, spot, timeout_seconds=4)
        self.assertEqual(result.state, "close_perp_underfilled")
        self.assertEqual(spot.calls, [])       # no naked short was created

    def test_a_full_timeout_falls_back_to_a_taker_close(self):
        okx, spot = FakeOKX(fill_plan=["0"], ioc_fill="3"), FakeSpot()
        result = close(okx, spot, timeout_seconds=4)
        self.assertEqual(result.state, "closed")
        self.assertEqual(len(okx.cancelled), 1)        # resting order pulled
        self.assertEqual(len(okx.ioc), 1)              # perp bought back
        self.assertEqual(okx.ioc[0]["side"], "buy")
        self.assertEqual(spot.calls[0]["amount_in"], Decimal("3"))
        self.assertEqual(result.fallback["style"], "taker")

    def test_no_fallback_leaves_both_legs_hedged_and_untouched(self):
        # "maker_close_fallback = none": a timeout must not sell anything. The
        # position stays long spot / short perp, which is recoverable.
        okx, spot = FakeOKX(fill_plan=["0"]), FakeSpot()
        result = close(okx, spot, timeout_seconds=4, fallback="none")
        self.assertEqual(result.state, "maker_unfilled")
        self.assertEqual(okx.ioc, [])
        self.assertEqual(spot.calls, [])

    def test_a_failed_spot_sale_asks_for_a_human_instead_of_retrying(self):
        # Perpetual flat, spot still held: a long spot leg, not a naked short.
        # The sale is retried five times, and only then is a human asked — the
        # usual cause is transport, not the market.
        okx, spot = FakeOKX(fill_plan=["3"]), FakeSpot(fail=True)
        result = close(okx, spot)
        self.assertEqual(result.state, "manual_intervention_required")
        self.assertIn("spot_sell_failed", result.reason)
        self.assertIn("after_5_attempts", result.reason)
        self.assertEqual(len(spot.calls), 5)   # five attempts, then stop
        self.assertFalse(okx.ioc)              # and no phantom order

    def test_a_transient_spot_sale_failure_is_retried_and_succeeds(self):
        # Measured 2026-09-16: the wallet's quote call answered
        # `api_failed_or_confirmation_required` once. A second attempt worked.
        okx, spot = FakeOKX(fill_plan=["3"]), FakeSpot()
        spot.fail_attempts = 2
        result = close(okx, spot)
        self.assertEqual(result.state, "closed")
        self.assertEqual(len(spot.calls), 3)
        attempts = [e.get("attempt") for e in result.events
                    if e.get("event") == "spot_sell_attempt_failed"]
        self.assertEqual(attempts, [1, 2])
        self.assertTrue(any(e.get("event") == "spot_sold" for e in result.events))

    def test_a_retry_only_sells_what_the_wallet_still_holds(self):
        # The idempotency guard: if a sale the process never confirmed actually
        # landed, the next attempt must see an empty wallet and stop instead of
        # selling a second time.
        okx, spot = FakeOKX(fill_plan=["3"]), FakeSpot(fail=True)
        balances = iter([Decimal("3"), Decimal("0")])
        result = close(okx, spot, token_balance=lambda _address: next(balances))
        self.assertEqual(result.state, "closed")
        self.assertEqual(len(spot.calls), 1)     # sold once, then nothing left
        self.assertEqual(result.reason, "balance_already_zero")

    def test_a_retry_never_asks_for_more_than_is_held(self):
        okx, spot = FakeOKX(fill_plan=["3"]), FakeSpot()
        spot.fail_attempts = 1
        balances = iter([Decimal("3"), Decimal("1.5")])
        result = close(okx, spot, token_balance=lambda _address: next(balances))
        self.assertEqual(result.state, "closed")
        self.assertEqual(spot.calls[1]["amount_in"], Decimal("1.5"))

    def test_a_post_only_cancellation_rejoins_the_new_bid(self):
        # Same lesson as opening: a price read seconds ago can be pulled as
        # would-cross. Re-join the *new* bid, and only as often as configured.
        okx, spot = FakeOKX(fill_by_order=["0", "3"]), FakeSpot()
        prices = iter([Decimal("3.361"), Decimal("3.365"), Decimal("3.367")])
        result = close(okx, spot, timeout_seconds=30, max_reprices=2,
                       price_source=lambda: next(prices))
        self.assertEqual(result.state, "closed")
        self.assertEqual([row["price"] for row in okx.placed],
                         [Decimal("3.361"), Decimal("3.365")])

    def test_repricing_stops_at_the_configured_limit(self):
        okx, spot = FakeOKX(fill_plan=["0"]), FakeSpot()
        result = close(okx, spot, timeout_seconds=30, max_reprices=2,
                       price_source=lambda: Decimal("3.367"), fallback="none")
        self.assertEqual(result.state, "maker_unfilled")
        self.assertEqual(len(okx.placed), 3)   # initial + 2 re-joins
        self.assertEqual(spot.calls, [])

    def test_an_unreadable_order_does_nothing_at_all(self):
        # A read blip is not a cancellation: the order may be live. Never sell
        # against a hedge state that cannot be read.
        okx, spot = FakeOKX(fill_plan=["3"], fail_reads=99), FakeSpot()
        result = close(okx, spot, timeout_seconds=5)
        self.assertEqual(result.state, "maker_unknown")
        self.assertIn("manual_reconciliation", result.reason)
        self.assertEqual(spot.calls, [])
        self.assertEqual(okx.ioc, [])
        self.assertEqual(len(okx.cancelled), 1)   # tried to pull it

    def test_a_leftover_spot_leg_is_sold_without_resting_anything(self):
        okx, spot = FakeOKX(), FakeSpot()
        result = close(okx, spot, contracts=Decimal("0"), token_quantity=Decimal("3"))
        self.assertEqual(result.state, "closed")
        self.assertEqual(okx.placed, [])
        self.assertEqual(spot.calls[0]["amount_in"], Decimal("3"))
        self.assertEqual(result.maker.state, "not_required")

    def test_a_perp_only_close_never_sells_tokens_it_was_not_given(self):
        # `--close --contract-quantity 3` alone says "there is no spot side"
        # (a short left behind by an earlier failure). It must not invent a
        # three-share sale from the contract multiplier.
        okx, spot = FakeOKX(fill_plan=["3"]), FakeSpot()
        result = close(okx, spot, token_quantity=Decimal("0"))
        self.assertEqual(result.state, "perp_only_closed")
        self.assertEqual(result.maker.filled, Decimal("3"))
        self.assertEqual(spot.calls, [])

    def test_a_perp_only_timeout_fallback_also_sells_nothing(self):
        okx, spot = FakeOKX(fill_plan=["0"], ioc_fill="3"), FakeSpot()
        result = close(okx, spot, token_quantity=Decimal("0"), timeout_seconds=4)
        self.assertEqual(result.state, "perp_only_closed")
        self.assertEqual(spot.calls, [])

    def test_the_contract_multiplier_scales_the_spot_sale(self):
        # One contract = two shares: buying back 1 contract must sell 2 shares.
        okx, spot = FakeOKX(fill_plan=["1"]), FakeSpot()

        class TwoSharePlan(FakePlan):
            perp_contract_multiplier = Decimal("2")

        result = close(okx, spot, plan=TwoSharePlan(), contracts=Decimal("1"),
                       token_quantity=Decimal("2"))
        self.assertEqual(result.state, "closed")
        self.assertEqual(spot.calls[0]["amount_in"], Decimal("2"))


if __name__ == "__main__":
    unittest.main()
