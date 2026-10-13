"""Maker-first execution: rest the perpetual leg at the touch, then do the spot
leg it pays for. Both directions share this machinery.

Opening (**rest the offer, then buy the spot**). Why this exists: measured
2026-09-16, the dominant cost of a basis round trip is the perpetual's bid/ask
spread, which is a function of its tick size — 41 bps per side on
SHEIN-USDT-SWAP, and up to 47 bps on XIAOMI off-hours. Crossing that spread
twice costs more than every fee and the whole on-chain spread combined. Resting
at the best offer earns the spread instead of paying it, and the venue pays a
maker rebate instead of charging a taker fee.

Opening costs this: it **inverts** the project's oldest invariant. The position
exists — short the perpetual, long nothing — between the fill and the spot buy.
That naked window is the price of the saving, so it is bounded by code:

* post-only, so the order can never cross and become a taker;
* a hard deadline; on timeout the remainder is cancelled and no spot is bought;
* if the spot leg fails after a fill, the short is closed immediately with a
  taker order rather than left naked;
* only quantities actually filled are ever bought on-chain.

Closing (**rest the bid, then sell the spot**), added 2026-09-16 on the
operator's instruction. Closing is the mirror image and reuses the same
place/wait/cancel machinery: a post-only *buy* is rested at the best bid, and
what fills is sold on-chain. The economics are different from opening, which is
why the parameters are separate — a close is not in a hurry (the spot and the
perpetual trade next to each other and stay there), so the window is minutes,
not seconds, and the order is never chased. The asymmetry is bounded in code
too:

* only the quantity actually bought back is ever sold on-chain, so the two legs
  stay balanced at every instant (partial fill → partial sale);
* the fallback is explicit (``maker_close_fallback = "taker"``): after the
  window, cancel, buy back the remainder as a taker, and only then sell;
* a spot sale that fails leaves a *long* spot leg with the perpetual flat — the
  mild direction — which is reported as ``manual_intervention_required`` and
  never retried automatically.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any, Callable

from .okx_trade import OKXTradeError
from .quantity import QuantityError, QuantitySpec


class MakerFirstError(RuntimeError):
    """The maker-first leg could not be completed safely."""


@dataclass
class MakerFill:
    order_id: str
    client_order_id: str
    requested: Decimal
    filled: Decimal
    average_price: Decimal | None
    fee: Decimal
    state: str
    cancelled: bool = False
    wait_ms: int = 0
    # The price the order actually rested at. Recorded because it is the whole
    # difference between this strategy and crossing the spread.
    price: Decimal | None = None

    @property
    def filled_all(self) -> bool:
        return self.filled >= self.requested and self.requested > 0

    def as_event(self) -> dict:
        return {"order_id": self.order_id, "client_order_id": self.client_order_id,
                "requested": str(self.requested), "filled": str(self.filled),
                "price": str(self.price) if self.price is not None else None,
                "average_price": str(self.average_price) if self.average_price is not None else None,
                "fee": str(self.fee), "state": self.state,
                "cancelled": self.cancelled, "wait_ms": self.wait_ms}


@dataclass
class MakerFirstResult:
    state: str
    maker: MakerFill
    spot: Any | None = None
    unwind: dict | None = None
    reason: str = ""
    events: list[dict] = field(default_factory=list)
    direction: str = "open"
    # What the taker fallback did, if it ran (close direction only).
    fallback: dict | None = None
    # The two legs as *measured*, not as planned. ``residual_tokens`` is the
    # only number that answers "is this group hedged": positive is unhedged
    # long dust, negative is a naked short of the hedge.
    planned_contracts: Decimal = Decimal(0)
    filled_contracts: Decimal = Decimal(0)
    required_tokens: Decimal = Decimal(0)
    received_tokens: Decimal = Decimal(0)
    residual_tokens: Decimal = Decimal(0)
    residual_bps: Decimal = Decimal(0)
    residual_tolerance_bps: Decimal | None = None

    def as_dict(self) -> dict:
        return {"state": self.state, "reason": self.reason,
                "direction": self.direction,
                "maker": self.maker.as_event(),
                "spot": self.spot.as_event() if self.spot is not None else None,
                "unwind": self.unwind,
                "fallback": self.fallback,
                "planned_contracts": str(self.planned_contracts),
                "filled_contracts": str(self.filled_contracts),
                "required_tokens": str(self.required_tokens),
                "received_tokens": str(self.received_tokens),
                "residual_tokens": str(self.residual_tokens),
                "residual_bps": str(self.residual_bps),
                "events": self.events}

    def legs_as_event(self) -> dict:
        """The auditable two-leg record: planned, filled, received, residual."""
        return {
            "planned_contracts": str(self.planned_contracts),
            "filled_contracts": str(self.filled_contracts),
            "required_tokens": str(self.required_tokens),
            "received_tokens": str(self.received_tokens),
            "residual_tokens": str(self.residual_tokens),
            "residual_bps": str(self.residual_bps),
            "residual_tolerance_bps": (None if self.residual_tolerance_bps is None
                                       else str(self.residual_tolerance_bps)),
            "direction": self.direction,
        }


@dataclass
class SpotLegResult:
    """What the on-chain leg did, before it is attached to a maker fill."""

    state: str
    spot: Any | None = None
    reason: str = ""


def _order_progress(rows: list[dict]) -> tuple[Decimal, Decimal, str]:
    """(filled contracts, average price, venue state) from fill rows."""
    filled = Decimal(0)
    notional = Decimal(0)
    for row in rows:
        size = Decimal(str(row.get("fillSz") or 0))
        price = Decimal(str(row.get("fillPx") or 0))
        filled += size
        notional += size * price
    average = (notional / filled) if filled > 0 else Decimal(0)
    return filled, average, ""


class MakerFirstLeg:
    """Rest one perpetual order at the touch, then trade the spot it covers."""

    def __init__(self, *, okx, wallet_leg, ledger=None, clock_ms=None,
                 sleeper=time.sleep, token_balance=None):
        self.okx = okx
        self.wallet = wallet_leg
        self.ledger = ledger
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.sleeper = sleeper
        # ``token_balance(address) -> Decimal`` — what the wallet holds *now*.
        # Optional, but the opener uses it as evidence after a spot failure: if
        # the broadcast had actually landed, the tokens are there and the
        # unwind below would be closing a hedge that no longer exists.
        self.token_balance = token_balance

    def place_and_wait(self, *, inst_id: str, side: str, size: Decimal,
                       price: Decimal, client_order_id: str,
                       timeout_seconds: int, poll_seconds: float = 1.0,
                       reduce_only: bool = False) -> MakerFill:
        """Rest one post-only order and watch it until it is resolved.

        ``side`` is the only thing that differs between opening and closing:
        opening rests a sell on the offer, closing rests a buy on the bid. The
        waiting, the deadline, the cancel and the "never chase" rule are the
        same for both, which is why they live here.
        """
        extras = {"reduce_only": True} if reduce_only else {}
        order = self.okx.place_post_only(
            inst_id=inst_id, side=side, size=size, price=price,
            client_order_id=client_order_id, **extras)
        order_id = str(order.get("ordId") or "")
        if not order_id:
            resolved = self.okx.get_order(inst_id, order.get("ordId", "")) \
                if order.get("ordId") else None
            raise MakerFirstError(f"post_only_order_id_missing:{resolved}")
        started = self.clock_ms()
        deadline = started + timeout_seconds * 1000
        filled = Decimal(0)
        average: Decimal | None = None
        state = "live"
        reads_lost = False
        while self.clock_ms() < deadline:
            try:
                row = self._read_order(inst_id, order_id)
            except MakerFirstError:
                # Measured 2026-09-16: a transport blip while polling aborted
                # the whole flow with the order possibly still live. An order we
                # cannot read is not an order we may forget about — stop
                # watching it, tell the caller, and pull it if we still can.
                reads_lost = True
                break
            state = str(row.get("state") or "")
            acc = Decimal(str(row.get("accFillSz") or 0))
            if acc > 0:
                fills = self._fills(inst_id, order_id)
                filled, average, _ = _order_progress(fills)
                filled = filled or acc
            if state in {"filled", "canceled", "mmp_canceled"}:
                break
            self.sleeper(poll_seconds)
        wait_ms = self.clock_ms() - started
        cancelled = False
        if reads_lost:
            # We cannot even read the order, so there is nothing to confirm
            # against: one best-effort pull, then report the unknown state.
            try:
                self.okx.cancel_order(inst_id, order_id)
            except Exception:  # noqa: BLE001 - unknown stays unknown
                pass
        elif state not in {"filled", "canceled", "mmp_canceled"} or filled < size:
            # Never chase: cancel what is left, then *confirm* it is gone.
            state = self._cancel_until_confirmed(inst_id, order_id, state)
            fills = self._fills(inst_id, order_id)
            filled, average, _ = _order_progress(fills)
            cancelled = state in {"canceled", "mmp_canceled"}
        fee = Decimal(0)
        for row in self._fills(inst_id, order_id):
            fee += abs(Decimal(str(row.get("fee") or 0)))
        if reads_lost or state not in {"filled", "canceled", "mmp_canceled"}:
            # Measured 2026-09-16: a post-only sell whose cancel was never
            # confirmed rested on the book for 16 minutes and then filled,
            # opening a naked short that the run had already reported as
            # "unfilled". An order that may still be live is an unknown state,
            # never an absence of orders — the caller must be able to tell the
            # difference, because one of them buys no spot and the other stops
            # everything.
            return MakerFill(order_id=order_id, client_order_id=client_order_id,
                             requested=size, filled=filled, average_price=average,
                             fee=fee, state="unknown", cancelled=cancelled,
                             wait_ms=wait_ms, price=price)
        return MakerFill(order_id=order_id, client_order_id=client_order_id,
                         requested=size, filled=filled, average_price=average,
                         fee=fee, state=state, cancelled=cancelled, wait_ms=wait_ms,
                         price=price)

    def _cancel_until_confirmed(self, inst_id: str, order_id: str, state: str,
                                attempts: int = 3) -> str:
        """Cancel, then read the order back until its state is resolved.

        "We asked to cancel" is not evidence that the order is gone. The cancel
        request can be lost, refused, or answered with an error while the order
        keeps resting. So the only accepted answers are the ones the venue
        states on a read: ``filled``, ``canceled``, ``mmp_canceled``. Anything
        else is returned as-is and becomes an unknown state at the call site.
        """
        for attempt in range(max(1, attempts)):
            try:
                self.okx.cancel_order(inst_id, order_id)
            except Exception:  # noqa: BLE001 - an unconfirmed cancel is not a cancel
                pass
            self.sleeper(0.5 * (attempt + 1))
            try:
                row = self.okx.get_order(inst_id, order_id)
            except Exception:  # noqa: BLE001 - retry, then report what we last saw
                continue
            state = str(row.get("state") or state)
            if state in {"filled", "canceled", "mmp_canceled"}:
                return state
        return state

    def _rest(self, *, inst_id: str, side: str, size: Decimal,
              timeout_seconds: int, client_order_id: str,
              price_source, poll_seconds: float, max_reprices: int,
              maker_price: Decimal | None = None,
              reduce_only: bool = False) -> tuple[MakerFill, list[dict]]:
        """One bounded resting window, with a limited number of re-joins.

        Each attempt gets a slice of the budget: spending the whole window on
        one resting order would make "reprice" mean "wait forever".
        """
        events: list[dict] = []
        deadline_ms = self.clock_ms() + timeout_seconds * 1000
        attempts = 1 + max(0, int(max_reprices))
        slice_seconds = max(1, timeout_seconds // attempts)
        fill: MakerFill | None = None
        for attempt in range(attempts):
            price = maker_price if maker_price is not None else Decimal(str(price_source()))
            remaining = max(1, min(slice_seconds,
                                   int((deadline_ms - self.clock_ms()) / 1000)))
            fill = self.place_and_wait(
                inst_id=inst_id, side=side, size=size, price=price,
                client_order_id=(client_order_id if attempt == 0
                                 else f"{client_order_id}r{attempt}"),
                timeout_seconds=remaining, poll_seconds=poll_seconds,
                reduce_only=reduce_only)
            events.append({"event": "maker_order", "attempt": attempt + 1,
                           **fill.as_event()})
            if fill.filled > 0 or self.clock_ms() >= deadline_ms:
                break
            # Nothing filled and the venue pulled it (post-only would cross, or
            # the touch moved): re-join the new touch if there is room.
        assert fill is not None
        return fill, events

    def _read_order(self, inst_id: str, order_id: str, attempts: int = 3) -> dict:
        """Order state, retried: a read blip is not a resolved order."""
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                return self.okx.get_order(inst_id, order_id)
            except Exception as exc:  # noqa: BLE001 - retry, then report
                last = exc
                if attempt + 1 < attempts:
                    self.sleeper(1.0 * (attempt + 1))
        raise MakerFirstError(f"order_read_failed:{last}")

    def _fills(self, inst_id: str, order_id: str) -> list[dict]:
        """Fill rows for one order, or [] when they cannot be read.

        These reads are *evidence*: the fill count used for decisions comes from
        the order row itself (``accFillSz``). Letting one raise aborted the whole
        flow *after* the order had been placed — measured 2026-09-21 06:45→06:50,
        a transport blip on ``/api/v5/trade/fills`` raised out of
        ``place_and_wait`` into the executor's generic handler, which wrote
        ``AUTO_HALT``. The resting order was left live and filled three minutes
        later as a naked short that no ledger row mentioned.
        """
        try:
            return list(self.okx.read.get(
                "/api/v5/trade/fills",
                f"instType=SWAP&instId={inst_id}&ordId={order_id}"))
        except Exception:  # noqa: BLE001 - evidence is allowed to be missing
            return []
    def open(self, *, plan, client_order_id: str, timeout_seconds: int,
             poll_seconds: float = 1.0, maker_price: Decimal | None = None,
             price_source=None, max_reprices: int = 0,
             residual_tolerance_bps: Decimal | None = None,
             residual_tolerance_quote: Decimal | None = None,
             balance_quote: Decimal | None = None) -> MakerFirstResult:
        """Rest the hedge at the best offer; buy the spot only for what filled.

        ``residual_tolerance_bps`` bounds the *unhedged long* dust the exact-in
        route leaves behind (the spend is searched to deliver at least N shares,
        so it usually delivers a hair more). It never applies to the other
        direction: a fill that delivers *fewer* tokens than the contracts it
        has to hedge is a naked short, whatever its size, and is reported as
        ``spot_short_of_hedge`` for a human to reconcile.

        The on-chain leg's *static* preconditions (allowance, quote balance) are
        read before the order is rested, because discovering them afterwards
        costs an unwind. A block here is a skip: nothing was sent, so it is not
        a halt and it is not a success.
        """
        lever = str(getattr(plan, "leverage", None) or "1")
        try:
            self.okx.set_leverage(plan.perp_instrument, lever)
        except Exception as exc:  # noqa: BLE001 - refuse the open rather than guess 3x
            return MakerFirstResult(
                state="spot_preflight_blocked",
                maker=MakerFill(order_id="", client_order_id=client_order_id,
                                requested=plan.perp_contract_quantity,
                                filled=Decimal(0), average_price=None,
                                fee=Decimal(0), state="not_placed"),
                reason=f"leverage_not_set:{type(exc).__name__}:{str(exc)[:120]}",
                events=[{"event": "leverage_not_set", "lever": lever}],
                planned_contracts=plan.perp_contract_quantity)
        checker = getattr(self.wallet, "spot_preflight", None)
        if checker is None:
            # A wallet leg that cannot answer the question has not answered it.
            # Before 2026-09-18 the answer was simply assumed and paid for with
            # an unwind; an unsupported leg blocks like a short one.
            preflight = {"ok": False, "reason": "preflight_unsupported_by_wallet_leg"}
        else:
            try:
                preflight = checker(token_in=plan.quote_token_address,
                                    amount=plan.size_quote,
                                    decimals=plan.quote_decimals,
                                    balance_quote=balance_quote)
            except Exception as exc:  # noqa: BLE001 - a read failure is a block
                # Reading the allowance/balance can fail at the RPC. Nothing has
                # been sent at this point, so the answer is the same skip as a
                # running-short preflight — not an exception. Letting it escape
                # used to reach the executor's generic handler, which treats any
                # failure after `entered_flow` as "a write may be in flight".
                preflight = {"ok": False,
                             "reason": f"preflight_error:{type(exc).__name__}"}
        if not preflight.get("ok"):
            # Fail closed and *before* the naked window: the allowance and the
            # balance cannot move between now and the fill, so a short here is
            # a cost with no information in it.
            return MakerFirstResult(
                state="spot_preflight_blocked",
                maker=MakerFill(order_id="", client_order_id=client_order_id,
                                requested=plan.perp_contract_quantity,
                                filled=Decimal(0), average_price=None,
                                fee=Decimal(0), state="not_placed"),
                reason=f"spot_preflight_{preflight.get('reason')}",
                events=[{"event": "spot_preflight_blocked", **preflight}],
                planned_contracts=plan.perp_contract_quantity)
        # The price has to be read at submit time. Measured 2026-09-16: a price
        # taken from the plan (built ~20s earlier, including the on-chain size
        # search) arrived below the market bid, so post-only cancelled it —
        # correctly, because it would have crossed.
        read_price = price_source or (lambda: plan.perp_best_ask())
        fill, events = self._rest(
            inst_id=plan.perp_instrument, side="sell",
            size=plan.perp_contract_quantity, timeout_seconds=timeout_seconds,
            client_order_id=client_order_id, price_source=read_price,
            poll_seconds=poll_seconds, max_reprices=max_reprices,
            maker_price=maker_price)
        if fill.state == "unknown":
            # Do not buy against a position whose hedge state we cannot read.
            return MakerFirstResult(
                state="maker_unknown", maker=fill,
                reason="order_state_unreadable_manual_reconciliation_required",
                events=events, planned_contracts=plan.perp_contract_quantity)
        if fill.filled <= 0:
            return MakerFirstResult(
                state="maker_unfilled", maker=fill, reason="no_fill_within_window",
                events=events, planned_contracts=plan.perp_contract_quantity)
        # Contracts × shares-per-contract, floored on the *token* step. The
        # review found this floored with the contract lot step instead, which
        # is a different number as soon as the multiplier is not 1.
        try:
            spec = QuantitySpec.from_plan(plan)
        except QuantityError as exc:
            # The hedge covered contracts whose share value we cannot express.
            # The short is real, so this is not a "skip": stop and reconcile.
            return MakerFirstResult(
                state="naked_short_unconfirmed", maker=fill,
                reason=f"quantity_conversion_failed:{exc}", events=events,
                planned_contracts=plan.perp_contract_quantity,
                filled_contracts=fill.filled)
        quantity = spec.tokens_for_contracts(fill.filled)
        try:
            spot = self.wallet.swap_exact_in(
                token_in=plan.quote_token_address, token_out=plan.token_address,
                amount_in=_spot_spend_for(plan, quantity),
                token_decimals=plan.token_decimals,
                quote_token=plan.quote_token_address,
                quote_decimals=plan.quote_decimals, leg="buy")
        except Exception as exc:  # noqa: BLE001 - the short is naked right now
            events.append({"event": "spot_failed", "reason": str(exc)[:200],
                           "filled_contracts": str(fill.filled),
                           "required_tokens": str(quantity),
                           "classified": _classify_spot_failure(exc),
                           **_failure_plan(exc)})
            # Evidence before acting: if the broadcast did land after all, the
            # tokens are in the wallet and the unwind below would close a hedge
            # that no longer has to exist.
            held = self._read_balance(plan, events)
            unwind = self.unwind(plan=plan, contracts=fill.filled,
                                 client_order_id=client_order_id + "u")
            unwind_state = str((unwind or {}).get("state") or "unknown")
            if unwind_state != "unwound":
                # Partially unwound, refused, or unreadable: a short of unknown
                # size is exactly the state this project never continues from.
                unwound = Decimal(str((unwind or {}).get("filled") or 0))
                residual = -(quantity - spec.tokens_for_contracts(unwound))
                return MakerFirstResult(
                    state="naked_short_unconfirmed", maker=fill, unwind=unwind,
                    reason=f"spot_leg_failed_and_unwind_{unwind_state}",
                    events=events,
                    planned_contracts=plan.perp_contract_quantity,
                    filled_contracts=fill.filled,
                    required_tokens=quantity,
                    received_tokens=held if held is not None else Decimal(0),
                    residual_tokens=residual,
                    residual_bps=(residual / quantity * Decimal(10000)
                                  if quantity > 0 else Decimal(0)))
            return MakerFirstResult(
                state="unwound_after_spot_failure", maker=fill, unwind=unwind,
                reason=f"spot_leg_failed:{type(exc).__name__}:{str(exc)[:140]}",
                events=events,
                planned_contracts=plan.perp_contract_quantity,
                filled_contracts=fill.filled, required_tokens=quantity,
                received_tokens=held if held is not None else Decimal(0))
        events.append({"event": "spot_filled", **spot.as_event()})
        received = Decimal(str(spot.token_quantity))
        residual = spec.residual_tokens(fill.filled, received)
        residual_bps = spec.residual_bps(fill.filled, received)
        tolerance = (Decimal(str(residual_tolerance_bps))
                     if residual_tolerance_bps is not None else None)
        cash_cap = (Decimal(str(residual_tolerance_quote))
                    if residual_tolerance_quote is not None else None)
        residual_quote = None
        if residual > 0 and received > 0 and spot.quote_amount is not None:
            residual_quote = residual * (Decimal(spot.quote_amount) / received)
        within_cash = (cash_cap is not None and residual_quote is not None
                       and residual_quote <= cash_cap)
        if residual < 0:
            # Naked short of the hedge. No tolerance applies: this is the
            # direction that can lose more than the basis.
            state = "spot_short_of_hedge"
            events.append({"event": "spot_short_of_hedge",
                           "filled_contracts": str(fill.filled),
                           "required_tokens": str(quantity),
                           "received_tokens": str(received),
                           "residual_tokens": str(residual),
                           "residual_bps": str(residual_bps)})
        elif tolerance is not None and residual_bps > tolerance and not within_cash:
            # Unhedged long dust, but bigger than the policy allows.
            state = "spot_over_hedge"
            events.append({"event": "spot_over_hedge", "residual_tokens": str(residual),
                           "residual_bps": str(residual_bps),
                           "tolerance_bps": str(tolerance)})
        else:
            state = "hedged"
            if residual > 0:
                events.append({"event": "spot_dust_unhedged",
                               "residual_tokens": str(residual),
                               "residual_bps": str(residual_bps),
                               "tolerance_bps": (None if tolerance is None
                                                 else str(tolerance))})
        return MakerFirstResult(
            state=state, maker=fill, spot=spot, events=events,
            planned_contracts=plan.perp_contract_quantity,
            filled_contracts=fill.filled, required_tokens=quantity,
            received_tokens=received, residual_tokens=residual,
            residual_bps=residual_bps, residual_tolerance_bps=tolerance)

    def _read_balance(self, plan, events: list[dict]) -> Decimal | None:
        """Wallet balance as evidence, or None. Never raises, never decides."""
        if self.token_balance is None:
            return None
        try:
            held = self.token_balance(plan.token_address)
        except Exception as exc:  # noqa: BLE001 - evidence, not a gate
            events.append({"event": "spot_balance_read_failed",
                           "reason": f"{type(exc).__name__}:{str(exc)[:120]}"})
            return None
        events.append({"event": "spot_balance_observed", "balance": str(held)})
        return Decimal(str(held))

    def unwind(self, *, plan, contracts: Decimal, client_order_id: str,
               price: Decimal | None = None) -> dict:
        """Close a naked short immediately, as a taker, and report the cost."""
        try:
            order = self.okx.place_ioc(
                inst_id=plan.perp_instrument, side="buy", size=contracts,
                price=(price if price is not None
                       else plan.perp_limit_price_for_close()),
                client_order_id=client_order_id, reduce_only=True)
        except Exception as exc:  # noqa: BLE001 - manual intervention required
            return {"state": "unwind_failed", "reason": str(exc)[:200],
                    "contracts": str(contracts)}
        order_id = str(order.get("ordId") or "")
        fills = self._fills(plan.perp_instrument, order_id)
        filled, average, _ = _order_progress(fills)
        fee = sum((abs(Decimal(str(row.get("fee") or 0))) for row in fills), Decimal(0))
        return {"state": "unwound" if filled >= contracts else "unwind_partial",
                "order_id": order_id, "filled": str(filled),
                "average_price": str(average), "fee": str(fee),
                "contracts": str(contracts)}


class MakerFirstOpener(MakerFirstLeg):
    """Rest the perpetual offer, then buy exactly what filled."""


class MakerFirstCloser(MakerFirstLeg):
    """Rest a perpetual bid, then sell exactly what was bought back.

    The mirror of :class:`MakerFirstOpener`, with the parameters that make
    closing a different trade: a much longer window, no chasing by default, and
    an explicit taker fallback once the window runs out.
    """

    def __init__(self, *, okx, wallet_leg, ledger=None, clock_ms=None,
                 sleeper=time.sleep, token_balance=None,
                 spot_attempts: int = 5, spot_backoff_seconds: float = 2.0):
        super().__init__(okx=okx, wallet_leg=wallet_leg, ledger=ledger,
                         clock_ms=clock_ms, sleeper=sleeper)
        # ``token_balance(address) -> Decimal`` — what the wallet holds *now*.
        # It is what makes a retry safe: each attempt sells only what is still
        # there, so an attempt whose result was lost cannot sell twice.
        self.token_balance = token_balance
        self.spot_attempts = max(1, int(spot_attempts))
        self.spot_backoff_seconds = max(0.0, float(spot_backoff_seconds))

    def close(self, *, plan, client_order_id: str, contracts: Decimal,
              token_quantity: Decimal, **kwargs) -> MakerFirstResult:
        """Close, then state the two legs in named units.

        The residual is ``spot left - the tokens the remaining short still
        hedges``. It is zero for a completed close and for a perp-only
        recovery, negative when the spot was over-sold (a naked short) and
        positive when the short was bought back but its spot was not sold —
        which is what a partial maker close leaves behind.
        """
        contracts = Decimal(str(contracts))
        token_quantity = Decimal(str(token_quantity))
        result = self._close(plan=plan, client_order_id=client_order_id,
                             contracts=contracts, token_quantity=token_quantity,
                             **kwargs)
        spec = QuantitySpec.from_plan(plan)
        filled = min(Decimal(str(result.maker.filled)), contracts)
        # Tokens sold, summed from the sale events the ledger also receives.
        # A partial close sells twice (once for the maker fill, once for the
        # taker remainder), so "the last spot leg" is not the total.
        sold = sum((Decimal(str(event.get("token_quantity") or 0))
                    for event in result.events
                    if event.get("event") == "spot_sold"), Decimal(0))
        # How much of the short is *gone*: the maker fill, plus whatever the
        # taker fallback bought back. A completed close leaves nothing short
        # even when only part of it came from the resting order.
        bought_back = filled
        unwind = result.unwind or {}
        if result.state in {"closed", "perp_only_closed"} or unwind.get("state") == "unwound":
            bought_back = contracts
        elif str(unwind.get("filled") or ""):
            bought_back = min(contracts, filled + Decimal(str(unwind["filled"])))
        short_remaining = max(contracts - bought_back, Decimal(0))
        residual = ((token_quantity - sold)
                    - spec.tokens_for_contracts(short_remaining))
        planned_tokens = spec.tokens_for_contracts(contracts)
        result.planned_contracts = contracts
        result.filled_contracts = filled
        result.required_tokens = spec.tokens_for_contracts(filled)
        result.received_tokens = sold
        result.residual_tokens = residual
        result.residual_bps = (residual / planned_tokens * Decimal(10000)
                               if planned_tokens > 0 else Decimal(0))
        return result

    def _close(self, *, plan, client_order_id: str, contracts: Decimal,
               token_quantity: Decimal, timeout_seconds: int,
               poll_seconds: float = 2.0, fallback: str = "taker",
               maker_price: Decimal | None = None, price_source=None,
               max_reprices: int = 0, reduce_only: bool = True,
               taker_price_source=None) -> MakerFirstResult:
        """Rest the bid, sell what filled; on timeout, taker first then spot.

        ``contracts`` is what has to be bought back and ``token_quantity`` is
        what has to be sold. They are passed in rather than derived, because a
        close is defined by what the account actually holds (read from the
        position and the wallet), not by what an entry plan once intended.
        """
        contracts = Decimal(str(contracts))
        token_quantity = Decimal(str(token_quantity))
        events: list[dict] = []
        # What "done" means for this call. A close with no spot leg is a
        # perp-only recovery (a short left behind by an earlier failure), and it
        # must not sell tokens the caller never nominated: `--close
        # --contract-quantity 3` alone says "there is no spot side", not "sell
        # three shares".
        final_state = "closed" if token_quantity > 0 else "perp_only_closed"
        if contracts <= 0:
            # Recovery of a leftover spot leg: the perpetual is already flat,
            # so there is nothing to rest and no hedge to lose.
            spot = self._sell_spot(plan, token_quantity, events, state="closed",
                                   reason="", note="perp_already_flat")
            return MakerFirstResult(
                state=spot.state, spot=spot.spot, direction="close",
                reason=spot.reason or "perp_already_flat", events=events,
                maker=_no_order(client_order_id))
        read_price = price_source or (lambda: plan.perp_best_bid())
        try:
            fill, rest_events = self._rest(
                inst_id=plan.perp_instrument, side="buy", size=contracts,
                timeout_seconds=timeout_seconds, client_order_id=client_order_id,
                price_source=read_price, poll_seconds=poll_seconds,
                max_reprices=max_reprices, maker_price=maker_price,
                reduce_only=reduce_only)
        except Exception as exc:  # noqa: BLE001 - nothing was placed, so nothing to undo
            # The resting order never reached the book (rejected, or the
            # connection failed before the write). The position is exactly as it
            # was — long spot, short perpetual — so the safe answer is to say so
            # and stop, not to start crossing the spread on a rejection.
            events.append({"event": "maker_order_not_placed",
                           "reason": str(exc)[:200], "side": "buy"})
            return MakerFirstResult(
                state="maker_order_rejected", maker=_no_order(client_order_id),
                direction="close", reason=f"maker_order_not_placed:{type(exc).__name__}",
                events=events)
        events += rest_events
        if fill.state == "unknown":
            # An order we cannot read is not an order we may act on: the
            # perpetual leg may be live, partially filled or gone.
            return MakerFirstResult(
                state="maker_unknown", maker=fill, direction="close",
                reason="order_state_unreadable_manual_reconciliation_required",
                events=events)
        filled = min(fill.filled, contracts)
        if fill.filled > contracts:
            # Cannot happen with a reduce-only order; if it does, the account is
            # in a shape this flow did not plan for.
            events.append({"event": "close_fill_exceeds_request",
                           "filled": str(fill.filled), "requested": str(contracts)})
        if filled >= contracts:
            # The whole short was bought back at the bid. Sell the shares it
            # was hedging — the size comes from the *fill*, not from the plan.
            spec = QuantitySpec.from_plan(plan)
            quantity = spec.tokens_for_contracts(filled)
            if token_quantity > 0:
                quantity = min(quantity, token_quantity)
            else:
                # Perp-only recovery: nothing was nominated to sell.
                quantity = Decimal(0)
            result = self._sell_spot(plan, quantity, events, state=final_state,
                                     reason="")
            return MakerFirstResult(state=result.state, maker=fill,
                                    spot=result.spot, direction="close",
                                    reason=result.reason, events=events)
        # Partially filled inside the window. Operator's decision, 2026-09-16:
        # **sell the spot the filled contracts cover, right now**. Two reasons:
        # the hedge has already been reduced by `filled`, so leaving all the
        # spot in place means holding `filled × multiplier` shares with nothing
        # behind them; and the sale is the leg with the slippage, so doing it
        # once per fill instead of once at the end keeps the size honest.
        # The remainder stays hedged and a second close run can finish it.
        spec = QuantitySpec.from_plan(plan)
        sold = Decimal(0)
        partial_spot = None
        if filled > 0 and token_quantity > 0:
            covered = spec.tokens_for_contracts(filled)
            partial_quantity = min(covered, token_quantity)
            if partial_quantity > 0:
                events.append({"event": "close_partial_sale",
                               "filled_contracts": str(filled),
                               "contracts_remaining": str(contracts - filled),
                               "tokens_sold": str(partial_quantity)})
                partial = self._sell_spot(plan, partial_quantity, events,
                                          state="closed", reason="")
                if partial.state not in {"closed", "perp_only_closed"}:
                    # The perp is now `filled` smaller than the spot it hedges:
                    # hand over rather than guess.
                    return MakerFirstResult(
                        state=partial.state, maker=fill, spot=partial.spot,
                        direction="close",
                        reason=(f"partial_sale_failed_after_{filled}_of_"
                                f"{contracts}_contracts:{partial.reason}"),
                        events=events)
                sold = (Decimal(str(partial.spot.token_quantity))
                        if partial.spot is not None else partial_quantity)
                partial_spot = partial.spot
        if fallback != "taker":
            return MakerFirstResult(
                state="closed_partial" if filled > 0 else "maker_unfilled",
                maker=fill, direction="close",
                spot=partial_spot,
                reason=("maker_close_window_expired_partial_fill_spot_sold"
                        if filled > 0 else "no_fill_within_window"),
                events=events)
        remaining = contracts - filled
        remaining_tokens = max(token_quantity - sold, Decimal(0))
        events.append({"event": "maker_timeout_fallback", "fallback": "taker",
                       "filled": str(filled), "remaining": str(remaining),
                       "tokens_already_sold": str(sold),
                       "tokens_remaining": str(remaining_tokens)})
        taker_price = None
        if taker_price_source is not None:
            try:
                taker_price = Decimal(str(taker_price_source()))
            except Exception as exc:  # noqa: BLE001 - fall back to the plan's band
                events.append({"event": "taker_cap_read_failed",
                               "reason": str(exc)[:120]})
        unwind = self.unwind(plan=plan, contracts=remaining,
                             client_order_id=client_order_id + "t",
                             price=taker_price)
        if unwind.get("state") not in {"unwound"}:
            # The perp leg is still short: the spot is still its hedge. Selling
            # now would leave a naked short, which is the direction this project
            # never accepts, so the spot leg is not sent.
            return MakerFirstResult(
                state="close_perp_underfilled", maker=fill, unwind=unwind,
                direction="close",
                reason=(f"taker_fallback_bought_back_{unwind.get('filled')}"
                        f"_of_{remaining}"),
                events=events)
        # Only the tokens the *remaining* buy-back has to cover: whatever the
        # partial sale already sold is gone.
        result = self._sell_spot(plan, remaining_tokens, events, state=final_state,
                                 reason="")
        return MakerFirstResult(state=result.state, maker=fill, spot=result.spot,
                                unwind=unwind, direction="close",
                                reason=result.reason, events=events,
                                fallback={"style": "taker", "filled": str(filled),
                                          "unwind": unwind.get("state")})

    def _sell_spot(self, plan, token_quantity: Decimal, events: list[dict], *,
                   state: str, reason: str, note: str = "") -> SpotLegResult:
        """Sell spot for the perpetual that was already bought back.

        Retried, deliberately. Once the perpetual is flat this leg carries no
        hedge at all, so leaving the spot behind is an unfinished trade that
        still has to be finished — and the usual reason a sale fails here is a
        transport problem, not a market judgement (measured 2026-09-16: a
        quote call answering `api_failed_or_confirmation_required`, a proxy
        hiccup, an RPC timeout). The operator's instruction is five attempts,
        then a human.

        The retry is bounded and *idempotent by construction*: before every
        attempt the wallet's own token balance is read, and only what is still
        held is sold. So an attempt whose broadcast silently succeeded cannot
        be repeated into a second sale — the next attempt sees nothing left and
        stops. That is the difference between retrying a *known* failure and
        replaying an *unknown* write, which this project never does.
        """
        if note:
            events.append({"event": "spot_leg_skip", "note": note})
        if token_quantity <= 0:
            return SpotLegResult(state=state, reason=reason)
        remaining = token_quantity
        for attempt in range(1, self.spot_attempts + 1):
            amount = remaining
            if self.token_balance is not None:
                try:
                    held = self.token_balance(plan.token_address)
                except Exception as exc:  # noqa: BLE001 - sell what we planned
                    # The review of 2026-09-16 found this branch keeping
                    # ``amount = remaining`` and broadcasting anyway. That is
                    # the one thing this path may not do: if an earlier attempt
                    # broadcast successfully and only the response was lost, a
                    # failed balance read leaves no way to tell "nothing sold"
                    # from "already sold", so the next broadcast would be a
                    # replay of an unknown write. Stop and hand over instead.
                    events.append({"event": "spot_balance_read_failed",
                                   "attempt": attempt,
                                   "reason": f"{type(exc).__name__}:{str(exc)[:120]}",
                                   "action": "stopped_before_swap"})
                    return SpotLegResult(
                        state="close_unknown",
                        reason=("spot_balance_unreadable_before_sale:"
                                f"{type(exc).__name__}"))
                else:
                    if held is not None and held <= 0:
                        # Nothing left: an earlier attempt did land after all.
                        events.append({"event": "spot_already_gone",
                                       "attempt": attempt, "balance": str(held),
                                       "requested": str(remaining)})
                        return SpotLegResult(state=state, reason="balance_already_zero")
                    if held is not None:
                        amount = min(remaining, held)
            try:
                spot = self.wallet.swap_exact_in(
                    token_in=plan.token_address,
                    token_out=plan.quote_token_address, amount_in=amount,
                    token_decimals=plan.token_decimals,
                    quote_token=plan.quote_token_address,
                    quote_decimals=plan.quote_decimals, leg="sell")
            except Exception as exc:  # noqa: BLE001 - retry, then ask for a human
                classification = _classify_spot_failure(exc)
                events.append({
                    "event": "spot_sell_attempt_failed", "attempt": attempt,
                    "attempts": self.spot_attempts, "amount": str(amount),
                    "classified": classification,
                    "reason": f"{type(exc).__name__}:{str(exc)[:160]}",
                    **_failure_plan(exc)})
                if classification == "unknown_after_broadcast":
                    # The swap may be on chain right now. A retry would be a
                    # second, competing sale of the same tokens: only a
                    # receipt/balance read may resolve this, and that is a
                    # human's call, not a loop's.
                    return SpotLegResult(
                        state="close_unknown",
                        reason=("spot_sale_result_unknown_after_broadcast:"
                                f"{type(exc).__name__}:{str(exc)[:120]}"))
                if attempt < self.spot_attempts:
                    self.sleeper(self.spot_backoff_seconds * attempt)
                    continue
                return SpotLegResult(
                    state="manual_intervention_required",
                    reason=(f"spot_sell_failed_after_{self.spot_attempts}_attempts:"
                            f"{type(exc).__name__}"))
            events.append({"event": "spot_sold", "attempt": attempt,
                           **spot.as_event()})
            return SpotLegResult(state=state, spot=spot, reason=reason)
        # Unreachable: the loop either returns a sale or the manual state.
        return SpotLegResult(state="manual_intervention_required",
                             reason="spot_sell_attempts_exhausted")


def _floor_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


# A spot leg can fail before, during, or after the write leaves this process,
# and only the first of those is safe to simply try again. The distinction is
# made from the venue's own words (plus ``may_have_reached_remote`` for the
# network layer), because "the call raised" says nothing about whether a
# transaction is on chain right now.
#
#   pre_broadcast        nothing was signed or sent → retry is just a retry
#   definitively_failed  a confirmed receipt moved no tokens → retry is safe
#                        (the balance is unchanged; the tx is already spent)
#   unknown_after_broadcast  the write may be live → only a receipt/balance
#                        read may resolve it, never a second broadcast
_UNKNOWN_AFTER_BROADCAST_MARKERS = (
    "onchain_leg_not_filled:pending",
    "onchain_leg_not_filled:unknown",
    "submission_status_unknown",
    "wallet_execution_failed",
    "unknown_write_state",
)
_DEFINITIVELY_FAILED_MARKERS = (
    "onchain_leg_not_filled:reverted",
    "onchain_leg_moved_no_tokens",
)


def classify_spot_failure(exc: BaseException) -> str:
    """Which of the three failure classes a spot-leg exception belongs to.

    A ``WalletExecutionError`` is classified by its stable ``code``, never by its
    ``detail``: the detail is the venue's free text, and a swap whose risk
    report happened to contain the word "unknown" must not be promoted into
    "an unknown write may be in flight" — or demoted out of it.
    """
    if getattr(exc, "may_have_reached_remote", False):
        return "unknown_after_broadcast"
    code = getattr(exc, "code", None)
    text = (f"{type(exc).__name__}:{code}" if code
            else f"{type(exc).__name__}:{exc}").lower()
    for marker in _UNKNOWN_AFTER_BROADCAST_MARKERS:
        if marker in text:
            return "unknown_after_broadcast"
    for marker in _DEFINITIVELY_FAILED_MARKERS:
        if marker in text:
            return "definitively_failed"
    return "pre_broadcast"


# Backwards-compatible private alias: the module used this name before it was
# exported for the fault-injection tests.
_classify_spot_failure = classify_spot_failure


def _no_order(client_order_id: str) -> MakerFill:
    """A fill record for a leg that needed no perpetual order at all."""
    return MakerFill(order_id="", client_order_id=client_order_id,
                     requested=Decimal(0), filled=Decimal(0),
                     average_price=None, fee=Decimal(0), state="not_required")


def _failure_plan(exc: BaseException) -> dict:
    """The failed transaction, when the error carries one.

    A spot-leg failure is only diagnosable if the calldata that reverted is in
    the record. 2026-09-18: the ledger said ``simulation_reverted`` and nothing
    else, so the cause had to be rebuilt by hand from a fresh quote.
    """
    record = getattr(exc, "plan_event", None)
    return {"tx_plan": record} if record else {}


def _spot_spend_for(plan, quantity: Decimal) -> Decimal:
    """Cash for ``quantity`` shares, scaled from the plan's own searched spend.

    The plan already found the exact-in amount that delivers a whole share
    count; scaling that is more faithful than re-deriving it from a mid price.
    """
    if not plan.expected_token_quantity or plan.expected_token_quantity <= 0:
        raise MakerFirstError("plan_has_no_expected_quantity")
    return (plan.size_quote * quantity / plan.expected_token_quantity).quantize(
        Decimal(1).scaleb(-int(plan.quote_decimals)), rounding=ROUND_UP)
