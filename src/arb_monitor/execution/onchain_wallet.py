"""On-chain leg: exact-in swap plus receipt confirmation on X Layer.

Wraps the OnchainOS CLI behind the reviewed adapter and adds the two things the
execution path needs but the raw CLI does not give:

* a **receipt-confirmed fill quantity**, decoded from the transaction's own
  Transfer logs — the wallet's net change, not the router's promise;
* an explicit ``filled`` / ``pending`` / ``reverted`` state so the perpetual leg
  is only ever sized against tokens that actually arrived.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Mapping

from arb_monitor.data.xlayer_onchain import XLayerRPC, decode_transfers

from .okx_wallet import WalletExecutionError, plan_record

APPROVE_SELECTOR = "0x095ea7b3"


class OnchainFillError(RuntimeError):
    """The on-chain leg did not reach a confirmable state."""


class AllowanceError(RuntimeError):
    """The approval step could not be completed inside the bounded policy."""


def minimal_units(amount: Decimal, decimals: int) -> int:
    scaled = Decimal(str(amount)) * (Decimal(10) ** int(decimals))
    if scaled != scaled.to_integral_value():
        raise AllowanceError("amount_below_token_precision")
    return int(scaled)


def decode_approve_spender(calldata: str) -> str:
    """Spender of an ERC-20 ``approve`` calldata, read from the calldata itself.

    The address a wallet ends up trusting comes from the payload that will be
    signed, not from a separate API response that could disagree with it.
    """
    data = str(calldata or "")
    if not data.startswith(APPROVE_SELECTOR) or len(data) < 138:
        raise AllowanceError("not_an_erc20_approve_calldata")
    return "0x" + data[34:74].lower()


@dataclass
class ApprovalOutcome:
    action: str                 # reused | approved
    token: str
    spender: str
    spendable_before: int
    spendable_after: int
    required: int
    approved_amount: int | None = None
    tx_hash: str | None = None
    order_id: str | None = None
    receipt_status: str | None = None
    simulation: Mapping[str, Any] = field(default_factory=dict)

    def as_event(self) -> dict:
        return {
            "action": self.action, "token": self.token, "spender": self.spender,
            "spendable_before": self.spendable_before,
            "spendable_after": self.spendable_after,
            "required": self.required, "approved_amount": self.approved_amount,
            "tx_hash": self.tx_hash, "order_id": self.order_id,
            "receipt_status": self.receipt_status,
            "simulation": dict(self.simulation),
        }


def wallet_token_delta(receipt: Mapping[str, Any], wallet: str,
                       token: str) -> Decimal:
    """Net change of one token for one address, decoded from the logs."""
    wallet = wallet.lower()
    token = token.lower()
    raw = 0
    for row in decode_transfers(receipt.get("logs") or []):
        if row["token"] != token:
            continue
        if row["to"] == wallet:
            raw += row["raw_value"]
        if row["from"] == wallet:
            raw -= row["raw_value"]
    return Decimal(raw)


@dataclass
class ChainReceipt:
    tx_hash: str
    status: str          # filled | pending | reverted | unknown
    block_number: int | None = None
    gas_used: int | None = None
    effective_gas_price_wei: int | None = None
    l1_fee_wei: int | None = None
    token_delta_raw: int = 0
    quote_delta_raw: int = 0
    detail: str = ""

    @property
    def gas_paid_native(self) -> Decimal:
        """Native gas actually charged. X Layer sponsorship shows up as 0."""
        price = Decimal(self.effective_gas_price_wei or 0)
        used = Decimal(self.gas_used or 0)
        l1 = Decimal(self.l1_fee_wei or 0)
        return ((price * used) + l1) / (Decimal(10) ** 18)


def wait_for_receipt(rpc: XLayerRPC, tx_hash: str, *, token: str, wallet: str,
                     decimals: int, attempts: int = 20, sleep_seconds: float = 2.0,
                     sleeper: Callable[[float], None] = time.sleep,
                     quote_token: str | None = None,
                     ) -> ChainReceipt:
    """Poll for the receipt. Pending is a state, not a failure to hide."""
    for attempt in range(attempts):
        try:
            receipt = rpc.receipt(tx_hash)
        except Exception as exc:  # RPC hiccup: keep the leg pending, never guess
            if attempt == attempts - 1:
                return ChainReceipt(tx_hash, "unknown", detail=f"rpc:{type(exc).__name__}")
            sleeper(sleep_seconds)
            continue
        if not receipt:
            sleeper(sleep_seconds)
            continue
        status = str(receipt.get("status") or "")
        delta = wallet_token_delta(receipt, wallet, token)
        # The quote leg is decoded from the same receipt: for a sell it is what
        # the wallet actually received, not what the router promised.
        quote_delta = (wallet_token_delta(receipt, wallet, quote_token)
                       if quote_token else 0)
        base = dict(
            tx_hash=tx_hash, block_number=int(receipt.get("blockNumber", "0x0"), 16),
            gas_used=int(receipt.get("gasUsed", "0x0"), 16),
            quote_delta_raw=int(quote_delta),
        )
        if status == "0x1":
            if delta != 0:
                return ChainReceipt(status="filled", token_delta_raw=int(delta), **base)
            # A successful transaction that moved nothing is not a fill: the
            # hedge must not be sized on it.
            return ChainReceipt(status="filled", token_delta_raw=0,
                                detail="success_without_token_transfer", **base)
        return ChainReceipt(status="reverted", token_delta_raw=int(delta),
                            detail=str(receipt.get("revertReason") or "reverted"), **base)
    return ChainReceipt(tx_hash, "pending", detail="receipt_not_found_within_window")


@dataclass
class WalletSwapOutcome:
    leg: str                       # "buy" | "sell"
    tx_hash: str
    order_id: str
    receipt: ChainReceipt
    token_quantity: Decimal
    quote_amount: Decimal
    quoted_token_quantity: Decimal
    amount_out_minimum: Decimal
    price_usd: Decimal | None
    route: str | None
    price_impact_pct: Decimal | None
    trade_fee_usd: Decimal | None
    simulation: Mapping[str, Any] = field(default_factory=dict)

    def as_event(self) -> dict:
        return {
            "leg": self.leg, "tx_hash": self.tx_hash, "order_id": self.order_id,
            "receipt_status": self.receipt.status,
            "quote_received": str(self.quote_amount),
            "block_number": self.receipt.block_number,
            "gas_used": self.receipt.gas_used,
            "gas_paid_native": str(self.receipt.gas_paid_native),
            "token_quantity": str(self.token_quantity),
            "quote_amount": str(self.quote_amount),
            "quoted_token_quantity": str(self.quoted_token_quantity),
            "amount_out_minimum": str(self.amount_out_minimum),
            "price_usd": str(self.price_usd) if self.price_usd is not None else None,
            "route": self.route,
            "price_impact_pct": (str(self.price_impact_pct)
                                 if self.price_impact_pct is not None else None),
            "trade_fee_usd": (str(self.trade_fee_usd)
                              if self.trade_fee_usd is not None else None),
            "simulation": dict(self.simulation),
        }


class OnchainWalletLeg:
    """Quote → build → simulate → submit → confirm, for one swap direction."""

    def __init__(self, *, wallet, address: str, chain_id: str = "196",
                 rpc_url: str | None = None, proxy: str | None = None,
                 receipt_waiter=wait_for_receipt, runner=None):
        self.wallet = wallet
        self.address = address
        self.chain_id = str(chain_id)
        self.rpc = XLayerRPC(url=rpc_url or "https://rpc.xlayer.tech", proxy=proxy)
        self.receipt_waiter = receipt_waiter
        # Optional: the CLI runner behind the wallet adapter. Supplying it lets
        # the leg answer allowance/balance questions on its own, which is what
        # ``spot_preflight`` needs and what every caller already has in hand.
        self.runner = runner
        # Set immediately *before* the first broadcast, never after: the
        # executor classifies a failure as "a write may be in flight" from this
        # flag, and a flag set after the call would miss a request that reached
        # the chain and lost its answer on the way back.
        self.writes_started = False

    def balance_of(self, token_address: str, runner) -> Decimal:
        from .wallet_cli import wallet_balances
        for row in wallet_balances(runner, "xlayer"):
            if (row.get("token_address") or "").lower() == token_address.lower():
                return Decimal(str(row.get("balance") or "0"))
        return Decimal(0)

    def spot_preflight(self, *, token_in: str, amount: Decimal, decimals: int,
                       balance_quote: Decimal | None = None,
                       runner=None) -> dict:
        """Static preconditions of the on-chain leg, read *before* the hedge.

        The spot buy is the last leg, but its two hard preconditions — the
        ERC-20 allowance and the quote-token balance — cannot change between
        now and the fill. Discovering them after the perpetual leg has filled
        costs an unwind every time (2026-09-18: 2 contracts filled, the swap
        reverted on allowance, the unwind cost 0.0264 USDT), and this project's
        own checklist has always listed the allowance as an entry gate. So both
        are read here, in front of the order.

        Fail-closed by construction: a read that fails is *not* evidence that
        the leg can be paid for, so it blocks. Returns a JSON-able record; the
        caller decides whether a block is a skip or a stop.
        """
        runner = runner if runner is not None else self.runner
        record: dict = {"token_in": token_in, "required": str(amount),
                        "decimals": int(decimals)}
        if runner is None:
            return {**record, "ok": False, "reason": "preflight_no_runner"}
        try:
            spendable, spender = self.allowance(runner, token_in)
        except Exception as exc:  # noqa: BLE001 - unreadable is not sufficient
            return {**record, "ok": False,
                    "reason": f"allowance_unreadable:{type(exc).__name__}"}
        required = minimal_units(amount, decimals)
        record.update({"spendable_minimal_units": str(spendable),
                       "required_minimal_units": str(required),
                       "spender": spender})
        if spendable < required:
            return {**record, "ok": False,
                    "reason": f"allowance_short:{spendable}<{required}"}
        balance = balance_quote
        if balance is None:
            try:
                balance = self.balance_of(token_in, runner)
            except Exception as exc:  # noqa: BLE001 - same rule as above
                return {**record, "ok": False,
                        "reason": f"balance_unreadable:{type(exc).__name__}"}
        record["balance"] = str(balance)
        if Decimal(str(balance)) < amount:
            return {**record, "ok": False,
                    "reason": f"quote_balance_short:{balance}<{amount}"}
        return {**record, "ok": True, "reason": ""}

    # --- approval ------------------------------------------------------
    def allowance(self, runner, token_address: str) -> tuple[int, str]:
        """``(spendable_minimal_units, spender)`` for one token, or (0, '')."""
        payload = runner(("swap", "check-approvals", "--chain", self.chain_id,
                          "--address", self.address, "--token", token_address))
        data = payload.get("data")
        if isinstance(data, list):
            data = data[0] if data else {}
        for entry in (data or {}).get("tokens") or []:
            if (entry.get("tokenContractAddress") or "").lower() != token_address.lower():
                continue
            try:
                spendable = int(Decimal(str(entry.get("spendable") or "0")))
            except Exception as exc:  # noqa: BLE001 - any parse failure is fatal here
                raise AllowanceError(f"unreadable_spendable:{exc}") from exc
            return spendable, str((data or {}).get("spender") or "").lower()
        return 0, str((data or {}).get("spender") or "").lower()

    def ensure_allowance(self, runner, *, token_address: str, token_symbol: str,
                         decimals: int, amount: Decimal, cap: Decimal,
                         receipt_waiter=None, sleeper=time.sleep) -> ApprovalOutcome:
        """Make sure the swap spender can move exactly this much, no more.

        Order of operations: read → (maybe) build → compare the spender in the
        calldata with the spender the API will use → scan → simulate → broadcast
        → read the allowance back. Approval is a prerequisite, never the
        outcome, and it is never unlimited.
        """
        required = minimal_units(amount, decimals)
        approved_amount = min(minimal_units(cap, decimals), max(required, required * 11 // 10))
        spendable, spender = self.allowance(runner, token_address)
        if spendable >= required:
            return ApprovalOutcome("reused", token_address, spender, spendable,
                                   spendable, required)
        payload = runner(("swap", "approve", "--token", token_address,
                          "--amount", str(approved_amount), "--chain", self.chain_id))
        data = payload.get("data")
        if isinstance(data, list):
            data = data[0] if data else {}
        item = (data or {}).get("tx") or data or {}
        calldata = str(item.get("data") or "")
        # An ERC-20 approval is a call to the token contract; the API also names
        # the spender it will hand the allowance to.
        to = str(item.get("to") or token_address).lower()
        if to != token_address.lower():
            raise AllowanceError("approve_target_is_not_the_token_contract")
        calldata_spender = decode_approve_spender(calldata)
        declared = str(item.get("dexContractAddress") or "").lower()
        if declared and declared != calldata_spender:
            raise AllowanceError("approve_payload_disagrees_with_its_own_calldata")
        if spender and calldata_spender != spender:
            # A changed spender means a changed route; the old decision to trust
            # one address must not silently carry over.
            raise AllowanceError("approve_spender_changed_requires_review")
        if item.get("value") not in ("0", 0, None):
            raise AllowanceError("approve_carries_native_value")
        scan = runner(("security", "tx-scan", "--chain", self.chain_id,
                       "--from", self.address, "--to", to, "--data", calldata))
        verdict = scan.get("data") if isinstance(scan.get("data"), dict) else {}
        if (scan.get("ok") is not True or verdict.get("action") != ""
                or verdict.get("warnings") or verdict.get("riskItemDetail") != []
                or (verdict.get("simulator") or {}).get("revertReason") != ""):
            raise AllowanceError("approve_security_scan_not_clear")
        simulation = runner(("gateway", "simulate", "--chain", self.chain_id,
                             "--from", self.address, "--to", to, "--data", calldata))
        sim_data = simulation.get("data")
        if isinstance(sim_data, list):
            sim_data = sim_data[0] if sim_data else {}
        if (sim_data or {}).get("failReason") not in ("", None):
            raise AllowanceError("approve_simulation_reverted")
        result = runner(("wallet", "contract-call", "--chain", self.chain_id,
                         "--to", to, "--input-data", calldata))
        broadcast = result.get("data") if isinstance(result.get("data"), dict) else {}
        tx_hash = str(broadcast.get("txHash") or "")
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", tx_hash):
            raise AllowanceError("approve_broadcast_status_unknown")
        waiter = receipt_waiter or self.receipt_waiter
        receipt = waiter(self.rpc, tx_hash, token=token_address, wallet=self.address,
                         decimals=decimals, attempts=15, sleep_seconds=2.0,
                         sleeper=sleeper)
        spendable_after, spender_after = self.allowance(runner, token_address)
        if receipt.status != "filled":
            raise AllowanceError(f"approve_not_confirmed:{receipt.status}")
        if not (required <= spendable_after <= approved_amount):
            raise AllowanceError(
                f"allowance_outside_bounded_policy:{spendable_after}/{approved_amount}")
        if spender and spender_after and spender_after != spender:
            raise AllowanceError("spender_changed_after_approval")
        return ApprovalOutcome("approved", token_address, spender_after or spender,
                               spendable, spendable_after, required,
                               approved_amount=approved_amount, tx_hash=tx_hash,
                               order_id=str(broadcast.get("orderId") or "") or None,
                               receipt_status=receipt.status,
                               simulation=dict(sim_data or {}))

    def swap_exact_in(self, *, token_in: str, token_out: str, amount_in: Decimal,
                      token_decimals: int, quote_token: str, quote_decimals: int,
                      leg: str = "buy", simulate: bool = True) -> WalletSwapOutcome:
        """One exact-in swap, confirmed from its own receipt.

        ``amount_in`` is the exact input the wallet spends. The received side is
        decoded from the Transfer logs, so a router that delivers less than it
        promised cannot be mistaken for a fill.
        """
        quote = self.wallet.quote_exact_in(token_in, token_out, amount_in)
        plan = self.wallet.build_swap(quote)
        try:
            if getattr(self.wallet, "router_discovery", False):
                # Pin the router this run actually uses, then close discovery.
                # From here on the address is checked, not learned.
                observed = {plan.tx_to.lower()} | set(
                    getattr(self.wallet, "observed_routers", set()))
                self.wallet.allowed_routers = frozenset(observed)
                self.wallet.router_discovery = False
            simulation = {}
            if simulate:
                simulation = dict(self.wallet.simulate(plan))
            self.writes_started = True
            result = self.wallet.execute(plan, confirmed=True)
        except WalletExecutionError as exc:
            # Keep the transaction with the failure. Without it a failed swap
            # cannot be re-examined once the market has moved: on 2026-09-18
            # the recorded reason was "simulation_reverted" and nothing else.
            if exc.plan is None:
                exc.plan = plan
            raise
        # Which token's movement is "the quantity"? For a buy it is what arrived
        # (token_out); for a sell it is what left (token_in). Decoding the wrong
        # one silently reports a sell as ~0 tokens at a nonsense price.
        watched = token_out if leg == "buy" else token_in
        receipt = self.receipt_waiter(
            self.rpc, result.tx_hash, token=watched, wallet=self.address,
            decimals=token_decimals, quote_token=quote_token,
        )
        if receipt.status != "filled":
            raise OnchainFillError(f"onchain_leg_not_filled:{receipt.status}")
        signed = Decimal(receipt.token_delta_raw) / (Decimal(10) ** token_decimals)
        quantity = signed if leg == "buy" else -signed
        if quantity <= 0:
            raise OnchainFillError(
                f"onchain_leg_moved_no_tokens:{leg}:{quantity}")
        if leg == "sell":
            # The quote side is what actually landed in the wallet: decode it
            # instead of trusting the router's estimate.
            quote_amount = (abs(Decimal(receipt.quote_delta_raw))
                            / (Decimal(10) ** quote_decimals))
            if quote_amount <= 0:
                raise OnchainFillError("onchain_leg_received_no_quote_tokens")
            price = (quote_amount / quantity) if quantity > 0 else None
        else:
            quote_amount = amount_in
            price = (quote_amount / quantity) if quantity > 0 else None
        return WalletSwapOutcome(
            leg=leg,
            tx_hash=result.tx_hash, order_id=result.order_id, receipt=receipt,
            token_quantity=quantity, quote_amount=quote_amount,
            quoted_token_quantity=plan.quote.amount_out,
            amount_out_minimum=plan.quote.amount_out_minimum,
            price_usd=price,
            route=plan.quote.router, price_impact_pct=plan.quote.price_impact_pct,
            trade_fee_usd=plan.quote.trade_fee_usd, simulation=simulation,
        )

    def swap_exact_out(self, *, token_in: str, token_out: str,
                       amount_out: Decimal, token_decimals: int,
                       quote_token: str, quote_decimals: int,
                       max_amount_in: Decimal, leg: str = "buy",
                       simulate: bool = True) -> WalletSwapOutcome:
        """Buy an exact number of tokens, with a hard ceiling on the cash spent.

        This is what makes a whole-share hedge possible: the delivered quantity
        is fixed by the calldata (``minReceiveAmount``), so the perpetual leg
        can be exactly that many contracts, and the receipt is checked against
        it rather than against a hope.

        NOT USABLE on the current X Layer route: measured 2026-09-16, OKX's
        exact-out calldata reverts in simulation with "Min return not reached"
        at 1, 2 and 3 shares, on both the USDG (multi-hop) and USDC (single-hop)
        entry, and at 0.8%, 2% and 5% slippage. The runner therefore reaches a
        whole share count by searching the exact-in amount instead. This path is
        kept, and tested, for the day the venue supports it.
        """
        quote = self.wallet.quote_exact_out(
            token_in, token_out, amount_out, out_decimals=token_decimals,
            max_amount_in=max_amount_in)
        plan = self.wallet.build_swap(quote)
        try:
            simulation = {}
            if simulate:
                simulation = dict(self.wallet.simulate(plan))
            result = self.wallet.execute(plan, confirmed=True)
        except WalletExecutionError as exc:
            if exc.plan is None:
                exc.plan = plan
            raise
        receipt = self.receipt_waiter(
            self.rpc, result.tx_hash, token=token_out, wallet=self.address,
            decimals=token_decimals, quote_token=quote_token,
        )
        if receipt.status != "filled":
            raise OnchainFillError(f"onchain_leg_not_filled:{receipt.status}")
        quantity = Decimal(receipt.token_delta_raw) / (Decimal(10) ** token_decimals)
        if quantity != amount_out:
            # exact-out exists so this cannot happen; if it did, the hedge size
            # would be wrong, so stop rather than guess.
            raise OnchainFillError(f"exact_out_quantity_mismatch:{quantity}")
        spent = (abs(Decimal(receipt.quote_delta_raw)) / (Decimal(10) ** quote_decimals)
                 if receipt.quote_delta_raw else plan.quote.amount_in)
        if spent > max_amount_in:
            raise OnchainFillError(f"exact_out_over_budget:{spent}")
        return WalletSwapOutcome(
            leg=leg, tx_hash=result.tx_hash, order_id=result.order_id,
            receipt=receipt, token_quantity=quantity, quote_amount=spent,
            quoted_token_quantity=plan.quote.amount_out,
            amount_out_minimum=plan.quote.amount_out_minimum,
            price_usd=(spent / quantity) if quantity > 0 else None,
            route=plan.quote.router, price_impact_pct=plan.quote.price_impact_pct,
            trade_fee_usd=plan.quote.trade_fee_usd, simulation=simulation,
        )
