"""Dependency-injected OKX Agentic Wallet execution adapter.

The adapter deliberately contains no credentials, private keys, HTTP client, or
LLM integration.  Production wiring supplies a runner for the already
authenticated OnchainOS/Wallet API.  This keeps strategy code declarative and
makes the live boundary explicit and testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import json
import re
import time
from typing import Any, Callable, Mapping, Sequence

from .mev import MEVPolicy


def _compact(value: Any, limit: int = 200) -> str:
    """One bounded line of the venue's own words: evidence, not a log dump."""
    if value is None:
        return ""
    text = (value if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, default=str))
    return " ".join(str(text).split())[:limit]


class WalletExecutionError(RuntimeError):
    """Fail-closed wallet execution error.

    ``code`` is the stable name callers switch on, ``detail`` is what the venue
    actually said, and ``plan`` is the transaction it happened to. All three are
    kept because on 2026-09-18 the unattended chain recorded only
    ``simulation_reverted`` — the gateway's reason and the calldata were both
    dropped, and the failure could only be explained by rebuilding the swap from
    scratch and re-simulating it. Diagnosis has to be possible from the ledger.

    ``str(exc)`` keeps the ``code``/``code:detail`` shape, so a caller that only
    prints the message still shows *why*.
    """

    def __init__(self, code: str, *, detail: str = "",
                 plan: "WalletTxPlan | None" = None):
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code
        self.detail = detail
        self.plan = plan

    @property
    def plan_event(self) -> dict | None:
        """The failed transaction, ready to be written into an event."""
        return plan_record(self.plan) if self.plan is not None else None


@dataclass(frozen=True)
class WalletSwapQuote:
    chain_id: str
    token_in: str
    token_out: str
    amount_in: Decimal
    amount_out: Decimal
    amount_out_minimum: Decimal
    price_impact_pct: Decimal | None
    trade_fee_usd: Decimal | None
    gas_limit: int | None
    router: str
    expires_at_ms: int | None
    raw: Mapping[str, Any] = field(repr=False)
    # exactIn  : the input is fixed and the output is a floor.
    # exactOut : the output is fixed and the input is a ceiling. Share-count
    #            hedges need this: an exact-in buy can never be relied on to
    #            deliver a whole number of shares, so the hedge would always
    #            leave a rounded-off residual position.
    mode: str = "exactIn"
    max_amount_in: Decimal | None = None

    @property
    def is_exact_out(self) -> bool:
        return self.mode == "exactOut"


@dataclass(frozen=True)
class WalletTxPlan:
    quote: WalletSwapQuote
    tx_to: str
    calldata: str = field(repr=False)
    gas_limit: int | None
    value: int
    raw: Mapping[str, Any] = field(repr=False)


def plan_record(plan: WalletTxPlan) -> dict:
    """The exact transaction behind a failure, for the execution ledger.

    Enough to re-run the simulation by hand (``gateway simulate --to … --data
    …``) without re-quoting into a market that has since moved.
    """
    quote = plan.quote
    return {
        "tx_to": plan.tx_to,
        "calldata": plan.calldata,
        "value": plan.value,
        "gas_limit": plan.gas_limit,
        "token_in": quote.token_in,
        "token_out": quote.token_out,
        "mode": quote.mode,
        "router": quote.router,
        "amount_in": str(quote.amount_in),
        "amount_out": str(quote.amount_out),
        "amount_out_minimum": str(quote.amount_out_minimum),
        "max_amount_in": (str(quote.max_amount_in)
                          if quote.max_amount_in is not None else None),
    }


@dataclass(frozen=True)
class WalletExecutionResult:
    order_id: str
    tx_hash: str
    raw: Mapping[str, Any] = field(repr=False)


class OKXAgenticWalletAdapter:
    """OKX wallet adapter with an explicit, injectable transport boundary.

    ``runner`` receives an immutable argv tuple and returns decoded JSON.  It
    may wrap the OnchainOS CLI or a direct OKX API client.  No secret is
    accepted or stored here; TEE signing remains inside the wallet service.
    """

    def __init__(
        self,
        runner: Callable[[Sequence[str]], Mapping[str, Any]],
        *,
        wallet_address: str,
        chain_id: str = "196",
        allowed_tokens: frozenset[str] = frozenset(),
        allowed_routers: frozenset[str] = frozenset(),
        allow_live_signing: bool = False,
        clock_ms: Callable[[], int] | None = None,
        quote_ttl_ms: int = 10000,
        max_slippage_pct: Decimal = Decimal("0.8"),
        mev_policy: MEVPolicy | None = None,
        router_discovery: bool = False,
    ) -> None:
        self.runner = runner
        self.wallet_address = wallet_address
        self.chain_id = str(chain_id)
        self.allowed_tokens = frozenset(x.lower() for x in allowed_tokens)
        self.allowed_routers = frozenset(x.lower() for x in allowed_routers)
        self.allow_live_signing = allow_live_signing
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.quote_ttl_ms = quote_ttl_ms
        self.max_slippage_pct = max_slippage_pct
        self.mev_policy = mev_policy or MEVPolicy()
        # The reviewed quote names a router, but the transaction target is only
        # known once the calldata is built. With discovery on, the first build
        # records that target instead of trusting it; execution still requires
        # the (by then pinned) allowlist. Python's default is off, so nothing
        # permissive happens unless a caller asks for this one-time discovery.
        self.router_discovery = router_discovery
        self.observed_routers: set[str] = set()
        self._submitted: set[str] = set()

    @staticmethod
    def _one(raw: Mapping[str, Any]) -> Mapping[str, Any]:
        if raw.get("ok") is not True:
            raise WalletExecutionError("api_failed_or_confirmation_required")
        data = raw.get("data")
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise WalletExecutionError("invalid_response_schema")
        return data[0]

    @staticmethod
    def _positive(value: Decimal) -> Decimal:
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise WalletExecutionError("amount_requires_positive_finite_decimal")
        return value

    def _fresh(self, quote: WalletSwapQuote) -> None:
        if quote.expires_at_ms is None or self.clock_ms() >= quote.expires_at_ms:
            raise WalletExecutionError("quote_expired")

    def _check_token(self, address: str) -> str:
        address = address.lower()
        if not re.fullmatch(r"0x[0-9a-f]{40}", address):
            raise WalletExecutionError("invalid_token_address")
        if address not in self.allowed_tokens:
            raise WalletExecutionError("token_not_allowlisted")
        return address

    def _check_action(self, item: Mapping[str, Any]) -> None:
        action = item.get("action")
        if action != "ok":
            raise WalletExecutionError(f"quote_action:{action}")

    def _quote_from_item(
        self,
        item: Mapping[str, Any],
        token_in: str,
        token_out: str,
        amount_in: Decimal,
        expires_at_ms: int,
        *,
        amount_out: Decimal | None = None,
        mode: str = "exactIn",
    ) -> WalletSwapQuote:
        self._check_action(item)
        if str(item.get("chainIndex")) != self.chain_id:
            raise WalletExecutionError("chain_mismatch")
        for key, expected in (("fromToken", token_in), ("toToken", token_out)):
            if item[key].get("tokenContractAddress", "").lower() != expected:
                raise WalletExecutionError("quote_token_mismatch")
        in_decimals = int(item["fromToken"]["decimal"])
        out_decimals = int(item["toToken"]["decimal"])
        self._positive(amount_in)
        self._positive(amount_out if amount_out is not None else amount_in)
        input_raw = amount_in * (Decimal(10) ** in_decimals)
        if input_raw != input_raw.to_integral_value():
            raise WalletExecutionError("input_precision")
        if mode == "exactOut":
            # The requested side is the output; the input is an estimate that
            # must not be below the quote's own estimate.
            output_raw = (amount_out if amount_out is not None else amount_in) * (
                Decimal(10) ** out_decimals)
            if output_raw != output_raw.to_integral_value():
                raise WalletExecutionError("output_precision")
            if str(item.get("toTokenAmount")) != str(int(output_raw)):
                raise WalletExecutionError("quote_output_mismatch")
            quote_in_raw = Decimal(str(item["fromTokenAmount"]))
            if quote_in_raw <= 0 or quote_in_raw > input_raw:
                raise WalletExecutionError("exact_out_input_exceeds_bound")
        elif str(item.get("fromTokenAmount")) != str(int(input_raw)):
            raise WalletExecutionError("quote_amount_mismatch")
        router = str(item.get("router", "")).lower()
        amount_out = Decimal(str(item["toTokenAmount"])) / (
            Decimal(10) ** out_decimals
        )
        self._positive(amount_out)
        minimum = Decimal(str(item.get("minReceiveAmount", item["toTokenAmount"]))) / (
            Decimal(10) ** out_decimals
        )
        impact = item.get("priceImpactPercent")
        fee = item.get("tradeFee")
        # For an exact-out quote ``amount_in`` is the venue's *estimate*; the
        # bound the caller authorised travels separately in ``max_amount_in``.
        estimated_in = (Decimal(str(item["fromTokenAmount"])) / (Decimal(10) ** in_decimals)
                        if mode == "exactOut" else amount_in)
        return WalletSwapQuote(
            chain_id=self.chain_id,
            token_in=token_in,
            token_out=token_out,
            amount_in=estimated_in,
            amount_out=amount_out,
            amount_out_minimum=minimum,
            price_impact_pct=Decimal(str(impact)) if impact is not None else None,
            trade_fee_usd=Decimal(str(fee)) if fee is not None else None,
            gas_limit=int(item["estimateGasFee"]) if item.get("estimateGasFee") else None,
            router=router,
            expires_at_ms=expires_at_ms,
            raw=item,
            mode=mode,
            max_amount_in=amount_in if mode == "exactOut" else None,
        )

    def quote_exact_in(
        self,
        token_in: str,
        token_out: str,
        amount_in: Decimal,
    ) -> WalletSwapQuote:
        token_in = self._check_token(token_in)
        token_out = self._check_token(token_out)
        self._positive(amount_in)
        started = self.clock_ms()
        raw = self.runner(
            (
                "swap",
                "quote",
                "--from",
                token_in,
                "--to",
                token_out,
                "--readable-amount",
                str(amount_in),
                "--chain",
                self.chain_id,
            )
        )
        # The venue prices the swap while the call is in flight, so the quote is
        # fresh when it *arrives*. Measuring the TTL from the request start made
        # a slow CLI call (10s+ under load) return an already-expired quote and
        # abort a close that had not sent anything.
        received = self.clock_ms()
        result = self._quote_from_item(self._one(raw), token_in, token_out, amount_in,
                                       received + self.quote_ttl_ms)
        self._fresh(result)
        return result

    def quote_exact_out(
        self,
        token_in: str,
        token_out: str,
        amount_out: Decimal,
        *,
        out_decimals: int,
        max_amount_in: Decimal,
    ) -> WalletSwapQuote:
        """Quote for an exact output (whole shares) with a hard input ceiling.

        ``max_amount_in`` is the operator's bound, not the venue's estimate: the
        quote is only accepted when its own estimate fits inside it, and the
        signed calldata carries the venue's ``maxSpendAmount`` on top.
        """
        token_in = self._check_token(token_in)
        token_out = self._check_token(token_out)
        self._positive(amount_out)
        self._positive(max_amount_in)
        units = amount_out * (Decimal(10) ** int(out_decimals))
        if units != units.to_integral_value():
            raise WalletExecutionError("output_precision")
        started = self.clock_ms()
        raw = self.runner(
            (
                # exactOut takes the *output* amount, in the output token's
                # minimal units: --readable-amount would be converted against
                # the input token's decimals and quote something else entirely.
                "swap", "quote", "--from", token_in, "--to", token_out,
                "--amount", str(int(units)), "--chain", self.chain_id,
                "--swap-mode", "exactOut",
            )
        )
        received = self.clock_ms()
        result = self._quote_from_item(
            self._one(raw), token_in, token_out, max_amount_in,
            received + self.quote_ttl_ms, amount_out=amount_out, mode="exactOut")
        self._fresh(result)
        return result

    def build_swap(self, quote: WalletSwapQuote) -> WalletTxPlan:
        self._fresh(quote)
        self._check_token(quote.token_in)
        self._check_token(quote.token_out)
        started = self.clock_ms()
        if quote.is_exact_out:
            # Ask for an exact number of shares; the venue returns a bounded
            # maximum spend instead of a minimum output.
            units = quote.amount_out * (
                Decimal(10) ** int(quote.raw["toToken"]["decimal"]))
            if units != units.to_integral_value():
                raise WalletExecutionError("output_precision")
            args = (
                "swap", "swap", "--from", quote.token_in, "--to", quote.token_out,
                "--amount", str(int(units)), "--chain", self.chain_id,
                "--wallet", self.wallet_address, "--swap-mode", "exactOut",
                "--max-auto-slippage", str(self.max_slippage_pct),
            )
        else:
            args = (
                "swap", "swap", "--from", quote.token_in, "--to", quote.token_out,
                "--readable-amount", str(quote.amount_in), "--chain", self.chain_id,
                "--wallet", self.wallet_address,
                "--max-auto-slippage", str(self.max_slippage_pct),
            )
        raw = self.runner(args)
        item = self._one(raw)
        tx = item["tx"]
        if str(tx["from"]).lower() != self.wallet_address.lower():
            raise WalletExecutionError("calldata_from_mismatch")
        to = str(tx["to"]).lower()
        if to in self.allowed_routers:
            pass
        elif self.router_discovery:
            self.observed_routers.add(to)
        else:
            raise WalletExecutionError("tx_router_not_allowlisted")
        if tx.get("value") not in ("0", 0):
            raise WalletExecutionError("unexpected_native_value")
        if not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})+", str(tx.get("data", ""))):
            raise WalletExecutionError("invalid_calldata")
        route = dict(item["routerResult"], minReceiveAmount=tx.get("minReceiveAmount"))
        if quote.is_exact_out:
            # The bound is what we are willing to spend, never the estimate.
            bound = Decimal(str(tx.get("maxSpendAmount") or 0)) / (
                Decimal(10) ** int(route["fromToken"]["decimal"]))
            if bound <= 0:
                raise WalletExecutionError("exact_out_missing_spend_bound")
            fresh_quote = self._quote_from_item(
                route, quote.token_in, quote.token_out, bound,
                started + self.quote_ttl_ms, amount_out=quote.amount_out,
                mode="exactOut")
            if fresh_quote.amount_out != quote.amount_out:
                # The whole point of this mode is a known share count.
                raise WalletExecutionError("exact_out_quantity_changed")
            if fresh_quote.amount_in > bound:
                raise WalletExecutionError("exact_out_estimate_above_bound")
            self._fresh(fresh_quote)
            return WalletTxPlan(
                quote=fresh_quote,
                tx_to=to,
                calldata=str(tx["data"]),
                gas_limit=int(tx["gas"]) if tx.get("gas") else None,
                value=int(tx.get("value", "0")),
                raw=item,
            )
        fresh_quote = self._quote_from_item(route, quote.token_in, quote.token_out,
                                            quote.amount_in, started + self.quote_ttl_ms)
        minimum = fresh_quote.amount_out_minimum
        if not minimum.is_finite() or not (0 < minimum <= fresh_quote.amount_out):
            raise WalletExecutionError("invalid_minimum_output")
        if minimum < fresh_quote.amount_out * (1 - self.max_slippage_pct / 100):
            # At most one smallest-unit rounding loss is accepted.
            unit = Decimal(10) ** -int(route["toToken"]["decimal"])
            if minimum + unit < fresh_quote.amount_out * (1 - self.max_slippage_pct / 100):
                raise WalletExecutionError("excess_slippage")
        self._fresh(fresh_quote)
        return WalletTxPlan(
            quote=fresh_quote,
            tx_to=to,
            calldata=str(tx["data"]),
            gas_limit=int(tx["gas"]) if tx.get("gas") else None,
            value=int(tx.get("value", "0")),
            raw=item,
        )

    def simulate(self, plan: WalletTxPlan) -> Mapping[str, Any]:
        self._fresh(plan.quote)
        raw = self.runner(
            (
                "gateway",
                "simulate",
                "--chain",
                self.chain_id,
                "--from",
                self.wallet_address,
                "--to",
                plan.tx_to,
                "--data",
                plan.calldata,
            )
        )
        result = self._one(raw)
        if result.get("failReason") != "":
            # The venue's own words, kept verbatim: "insufficient allowance"
            # and "too little received" are different incidents and the caller
            # cannot tell them apart from the state name alone.
            raise WalletExecutionError("simulation_reverted",
                                       detail=_compact(result.get("failReason")),
                                       plan=plan)
        if result.get("risks") != []:
            raise WalletExecutionError("simulation_risk",
                                       detail=_compact(result.get("risks")),
                                       plan=plan)
        if result.get("intention") != "Swap":
            raise WalletExecutionError("simulation_intention",
                                       detail=_compact(result.get("intention")),
                                       plan=plan)
        changes: dict[str, Decimal] = {}
        for row in result.get("assetChange", []):
            token = str(row.get("address", "")).lower()
            # The live API uses rawVaule; other revisions use rawValue.
            amount = Decimal(str(row.get("rawValue", row.get("rawVaule"))))
            if not amount.is_finite() or amount != amount.to_integral_value():
                raise WalletExecutionError("invalid_simulation_amount",
                                           detail=_compact(row), plan=plan)
            amount /= Decimal(10) ** int(row["decimals"])
            changes[token] = changes.get(token, Decimal(0)) + amount
        spent = changes.get(plan.quote.token_in, Decimal(0))
        received = changes.get(plan.quote.token_out, Decimal(0))
        if plan.quote.is_exact_out:
            # Fixed output, bounded input: the share count must be exactly what
            # was asked for, and the cash spent must stay inside the bound.
            if received != plan.quote.amount_out:
                raise WalletExecutionError(
                    "simulation_exact_out_mismatch",
                    detail=f"received={received} expected={plan.quote.amount_out}",
                    plan=plan)
            bound = plan.quote.max_amount_in or plan.quote.amount_in
            if not (-bound <= spent < 0):
                raise WalletExecutionError(
                    "simulation_exact_out_over_budget",
                    detail=f"spent={spent} bound={bound}", plan=plan)
        else:
            if spent != -plan.quote.amount_in:
                raise WalletExecutionError(
                    "simulation_input_mismatch",
                    detail=f"spent={spent} expected={-plan.quote.amount_in}",
                    plan=plan)
            if received < plan.quote.amount_out_minimum:
                raise WalletExecutionError(
                    "simulation_output_below_minimum",
                    detail=(f"received={received} "
                            f"minimum={plan.quote.amount_out_minimum}"),
                    plan=plan)
        if set(changes) - {plan.quote.token_in, plan.quote.token_out}:
            raise WalletExecutionError(
                "unexpected_simulation_asset",
                detail=_compact(sorted(set(changes) - {plan.quote.token_in,
                                                        plan.quote.token_out})),
                plan=plan)
        self._fresh(plan.quote)
        return result

    def execute(self, plan: WalletTxPlan, *, confirmed: bool = False) -> WalletExecutionResult:
        if not self.allow_live_signing:
            raise WalletExecutionError("live_signing_disabled")
        if not confirmed:
            raise WalletExecutionError("explicit_confirmation_required")
        self.mev_policy.validate(self.chain_id)
        self._fresh(plan.quote)
        if plan.tx_to not in self.allowed_routers or plan.value != 0:
            raise WalletExecutionError("invalid_execution_target")
        self._check_token(plan.quote.token_in)
        self._check_token(plan.quote.token_out)
        if plan.calldata in self._submitted:
            raise WalletExecutionError("submission_unknown_or_already_attempted")
        scan = self.runner(("security", "tx-scan", "--chain", self.chain_id,
                            "--from", self.wallet_address, "--to", plan.tx_to,
                            "--data", plan.calldata))
        if scan.get("ok") is not True or not isinstance(scan.get("data"), dict):
            raise WalletExecutionError("security_scan_failed",
                                       detail=_compact(scan.get("msg") or scan.get("error")),
                                       plan=plan)
        verdict = scan["data"]
        if (verdict.get("action") != "" or verdict.get("warnings")
                or verdict.get("riskItemDetail") != []
                or verdict.get("simulator", {}).get("revertReason") != ""):
            raise WalletExecutionError(
                "security_scan_not_clear",
                detail=_compact({"action": verdict.get("action"),
                                 "warnings": verdict.get("warnings"),
                                 "riskItemDetail": verdict.get("riskItemDetail"),
                                 "revertReason": (verdict.get("simulator") or {})
                                 .get("revertReason")}),
                plan=plan)
        self.simulate(plan)
        args = [
            "wallet",
            "contract-call",
            "--chain",
            self.chain_id,
            "--to",
            plan.tx_to,
            "--input-data",
            plan.calldata,
        ]
        # Let the wallet's execution preflight estimate gas; quote gas is not a
        # verified gas limit (the observed quote was lower than receipt usage).
        if self.mev_policy.enabled and self.chain_id in self.mev_policy.supported_chains:
            args += ["--mev-protection"]
        self._submitted.add(plan.calldata)  # An uncertain send must not be retried.
        raw = self.runner(tuple(args))
        if raw.get("ok") is not True or not isinstance(raw.get("data"), dict):
            raise WalletExecutionError("wallet_execution_failed")
        data = raw["data"]
        if not data.get("orderId") or not re.fullmatch(r"0x[0-9a-fA-F]{64}", str(data.get("txHash", ""))):
            raise WalletExecutionError("submission_status_unknown")
        return WalletExecutionResult(str(data["orderId"]), str(data["txHash"]), raw)
