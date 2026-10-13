"""End-to-end orchestration: monitor signal → on-chain fill → hedge → ledger.

The order of operations is not configurable, because it is the whole point:

```text
plan (prices + costs + intent)
  → RiskGate (whitelist, size, TTL, kill switch, unhedged, open contracts)
  → on-chain exact-in swap
  → receipt: token quantity actually received
  → only then the perpetual taker order, sized from that quantity
  → readback: order, fills, balance, positions
  → ledger events + reconciliation
```

Every mode runs the same code path; ``dry`` stops after the plan, ``live`` adds
the two writes. Nothing here can place the hedge before the spot receipt, and a
spot fill that cannot cover one contract stops the run instead of opening a
naked short.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from typing import Any

from .intent import ExecutionIntent, taker_limit_price
from .ledger import ExecutionLedger
from .onchain_wallet import OnchainWalletLeg, WalletSwapOutcome
from .quantity import QuantitySpec

MARKET_SELL = "sell"
# The only order states the venue can state that mean "this order is over".
# Anything else — an empty string, a missing field, a state this project has
# never seen — is unknown, and unknown is not a fill.
TERMINAL_ORDER_STATES = frozenset({"filled", "canceled", "mmp_canceled"})


class FullChainError(RuntimeError):
    """The run cannot continue without a human decision."""


def now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass
class Plan:
    asset: str
    chain_id: str
    token_address: str
    token_symbol: str
    token_decimals: int
    quote_token_address: str
    quote_symbol: str
    quote_decimals: int
    pool_quote_symbol: str
    size_quote: Decimal
    # The ceiling this trade was allowed to spend; ``size_quote`` is the
    # expected cost. They are separate because exact-out fixes the shares and
    # only bounds the cash.
    budget_quote: Decimal
    spot_price_usd: Decimal
    perp_venue: str
    perp_instrument: str
    perp_bid_usd: Decimal
    perp_ask_usd: Decimal | None
    perp_contract_multiplier: Decimal
    perp_min_size: Decimal
    perp_lot_size: Decimal
    perp_price_tick: Decimal
    perp_max_slippage_bps: Decimal
    gross_bps: Decimal
    cost_bps: Decimal
    net_bps: Decimal
    threshold_bps: Decimal
    signal_qualified: bool
    forced_plumbing_test: bool
    expected_token_quantity: Decimal
    perp_contract_quantity: Decimal
    quote_observed_at_s: float
    # Filled in at submit time from the live top of book, so the ledger records
    # which price the taker limit was actually derived from.
    observed_reference_price: Decimal | None = None
    warnings: list[str] = field(default_factory=list)
    intent: ExecutionIntent | None = None

    @property
    def perp_limit_price(self) -> Decimal:
        band = taker_limit_price(self.perp_bid_usd, self.perp_max_slippage_bps,
                                 side="sell")
        return _round_to_tick(band, self.perp_price_tick)

    def perp_best_ask(self) -> Decimal:
        """Where a maker sell belongs: joining the best offer.

        The tick is the whole reason this strategy exists — on SHEIN one tick is
        21 bps — so the price is the venue's own best ask, not a computed band.
        """
        if self.perp_ask_usd is None or self.perp_ask_usd <= 0:
            raise FullChainError("no_best_ask_for_maker_order")
        return self.perp_ask_usd

    def perp_best_bid(self) -> Decimal:
        """Where a maker *close* belongs: joining the best bid.

        Closing is the mirror of opening, and so is the price: the buy-back
        rests on the bid instead of crossing up to the ask. Read at submit time
        for the same reason opening is — a plan-time price can already be stale
        by the time the on-chain sizing search has finished.
        """
        if self.perp_bid_usd is None or self.perp_bid_usd <= 0:
            raise FullChainError("no_best_bid_for_maker_order")
        return self.perp_bid_usd

    def perp_limit_price_for_close(self) -> Decimal:
        """Taker cap for closing the short (buy the ask, band rounded up)."""
        if self.perp_ask_usd is None:
            raise FullChainError("no_ask_for_close")
        raw = taker_limit_price(self.perp_ask_usd, self.perp_max_slippage_bps,
                                side="buy")
        return _round_up_to_tick(raw, self.perp_price_tick)

    def summary(self) -> dict:
        return {
            "asset": self.asset, "size_quote": str(self.size_quote),
            "budget_quote": str(self.budget_quote),
            "share_count": str(self.expected_token_quantity),
            "swap_mode": "exactIn_searched_for_whole_shares",
            "spot_token": self.token_symbol, "spot_token_address": self.token_address,
            "spot_ask_usd": str(self.spot_price_usd),
            "quote_token": self.quote_symbol,
            "quote_token_address": self.quote_token_address,
            "pool_quote_symbol": self.pool_quote_symbol,
            "perp": f"{self.perp_venue}:{self.perp_instrument}",
            "perp_bid_usd": str(self.perp_bid_usd),
            "gross_entry_basis_bps": str(self.gross_bps),
            "estimated_entry_cost_bps": str(self.cost_bps),
            "net_entry_basis_bps": str(self.net_bps),
            "min_net_edge_bps": str(self.threshold_bps),
            "signal_qualified": self.signal_qualified,
            "forced_plumbing_test": self.forced_plumbing_test,
            "expected_token_quantity": str(self.expected_token_quantity),
            "perp_contract_quantity": str(self.perp_contract_quantity),
            "perp_limit_price": str(self.perp_limit_price),
            "perp_order_style": "taker_ioc_at_best_bid",
            "chain_id": self.chain_id,
            "warnings": list(self.warnings),
        }


def _round_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return price
    steps = (price / tick).to_integral_value(rounding=ROUND_DOWN)
    return steps * tick


def _round_up_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return price
    steps = (price / tick).to_integral_value(rounding=ROUND_UP)
    return steps * tick


@dataclass
class RunResult:
    state: str
    reason: str = ""
    plan: Plan | None = None
    spot: WalletSwapOutcome | None = None
    perp_order: dict | None = None
    perp_fills: list[dict] = field(default_factory=list)
    positions_after: list[dict] = field(default_factory=list)
    unhedged_tokens: Decimal = Decimal(0)
    hedge_hint: str = ""
    warnings: list[str] = field(default_factory=list)
    audit: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "state": self.state, "reason": self.reason,
            "plan": self.plan.summary() if self.plan else None,
            "spot": self.spot.as_event() if self.spot else None,
            "perp_order": _jsonable(self.perp_order),
            "perp_fills": _jsonable(self.perp_fills),
            "positions_after": _jsonable(self.positions_after),
            "unhedged_tokens": str(self.unhedged_tokens),
            "hedge_hint": self.hedge_hint,
            "warnings": list(self.warnings),
            "audit": _jsonable(self.audit),
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


class FullChainRunner:
    """Plan and execute one round trip of the spot-long / perp-short pair."""

    def __init__(self, *, wallet_leg: OnchainWalletLeg, okx,
                 policy: dict, monitor_config: dict[str, Any],
                 ledger: ExecutionLedger | None = None,
                 clock=now_ms, sleeper=time.sleep,
                 quoter=None):
        self.wallet = wallet_leg
        self.okx = okx
        self.policy = policy or {}
        self.monitor_config = monitor_config or {}
        self.ledger = ledger
        self.clock = clock
        self.sleeper = sleeper
        # Exact-out pricing normally comes from the chain (QuoterV2 on X Layer).
        # Tests pass a fake so a suite that promises "no network" keeps that
        # promise; the default path is unchanged.
        self.quoter = quoter
        self.limits = (self.policy.get("limits") or {})
        self.allowlist = (self.policy.get("allowlist") or {})
        self.orders = (self.policy.get("orders") or {})

    # --- planning ------------------------------------------------------
    def build_close_plan(self, *, asset: str, perp_instrument: str,
                         token_quantity: Decimal, contract_quantity: Decimal,
                         quote_token: dict | None = None) -> Plan:
        """Plan the exit legs only.

        Closing needs the instrument's rules and the price the spot leg will
        sell at — not an entry sizing calculation. Sizing a buy here would be
        reading the wrong wallet balance (the USDG is in the token now) and
        would refuse a perfectly valid close.
        """
        token = self._token(asset)
        quote = quote_token or self._default_quote_token()
        # A residual spot-only recovery must not depend on a fresh CEX metadata
        # read: the perp is already flat, and the only write left is selling
        # tokens.  Requiring instrument/ticker here used to strand the spot
        # leg whenever OKX was temporarily unreadable after a successful
        # buy-back.
        spot_only = contract_quantity <= 0
        spec = {} if spot_only else self.okx.instrument(perp_instrument)
        ticker = {} if spot_only else self.okx.ticker(perp_instrument)
        multiplier = Decimal(str(spec.get("ctMult") or spec.get("ctVal") or "1"))
        if token_quantity > 0:
            try:
                sell = self.wallet.wallet.quote_exact_in(
                    token["address"], quote["address"], token_quantity)
            except Exception as exc:  # noqa: BLE001 - a refusal, not a crash
                # Selling more than the wallet holds (or a venue that will not
                # price the sale) is a refusal with a reason, not a traceback:
                # the operator asked to close a position that is not there.
                raise FullChainError(
                    "spot_sell_quote_unavailable:"
                    f"{type(exc).__name__}:{str(exc)[:120]}") from exc
            # Selling the token: what comes back is the quote-side amount.
            spot_price = sell.amount_out / token_quantity
        else:
            # Perp-only close: no spot leg, so no spot price to quote.
            spot_price = Decimal(0)
        perp_ask = Decimal(str(ticker.get("askPx") or "0"))
        # The basis shown for a close is the one being realised now, not an
        # entry assumption.
        exit_basis_bps = ((perp_ask - spot_price) / spot_price * Decimal(10000)
                          if spot_price > 0 else Decimal(0))
        plan = Plan(
            asset=asset, chain_id=str(token["chain_id"]),
            token_address=token["address"], token_symbol=token["symbol"],
            token_decimals=int(token["decimals"]),
            quote_token_address=quote["address"], quote_symbol=quote["symbol"],
            quote_decimals=int(quote["decimals"]),
            pool_quote_symbol=token["quote_symbol"],
            size_quote=token_quantity, budget_quote=Decimal(0),
            spot_price_usd=spot_price,
            perp_venue=_venue_of(perp_instrument), perp_instrument=perp_instrument,
            perp_bid_usd=Decimal(str(ticker.get("bidPx") or "0")),
            perp_ask_usd=Decimal(str(ticker["askPx"])) if ticker.get("askPx") else None,
            perp_contract_multiplier=multiplier,
            perp_min_size=Decimal(str(spec.get("minSz") or "1")),
            perp_lot_size=Decimal(str(spec.get("lotSz") or "1")),
            perp_price_tick=Decimal(str(spec.get("tickSz") or "0.001")),
            perp_max_slippage_bps=Decimal(
                str(self.orders.get("perp_max_slippage_bps", "20"))),
            gross_bps=exit_basis_bps, cost_bps=Decimal(0),
            net_bps=exit_basis_bps, threshold_bps=Decimal(0),
            signal_qualified=False, forced_plumbing_test=False,
            expected_token_quantity=token_quantity,
            perp_contract_quantity=contract_quantity,
            quote_observed_at_s=self.clock() / 1000,
            warnings=["平仓计划：只计算退出两腿，不做开仓额度判定"],
        )
        plan.intent = self._intent(plan)
        return plan

    def build_plan(self, *, asset: str, size_quote: Decimal,
                   perp_instrument: str, forced: bool = False,
                   quote_token: dict | None = None,
                   shares: Decimal | None = None,
                   balance_quote: Decimal | None = None) -> Plan:
        token = self._token(asset)
        # The token we pay with is what the wallet actually holds and the policy
        # allowlists; the pool's own quote token is only a price reference. On
        # XIAOMI the pool is quoted in USDC while the wallet holds USDG, and the
        # route (not the model) handles that hop.
        quote = quote_token or self._default_quote_token()
        quote_observed_at = self.clock() / 1000
        ticker = self.okx.ticker(perp_instrument)
        spec = self.okx.instrument(perp_instrument)
        perp_bid = Decimal(str(ticker["bidPx"]))
        costs = self.monitor_config.get("costs") or {}
        from ..strategy.xstocks_screen import entry_cost_bps
        cost = entry_cost_bps(costs, None, size_quote)
        threshold = Decimal(str((self.monitor_config.get("monitor") or {})
                                .get("min_net_edge_bps", "20")))
        ct_val = spec.get("ctVal")
        ct_mult = spec.get("ctMult")
        if ct_val in (None, "") or ct_mult in (None, ""):
            raise FullChainError("contract_multiplier_unknown")
        multiplier = Decimal(str(ct_val)) * Decimal(str(ct_mult))
        if spec.get("minSz") in (None, "") or spec.get("lotSz") in (None, ""):
            raise FullChainError("contract_minimum_unknown")
        min_size = Decimal(str(spec["minSz"]))
        lot_size = Decimal(str(spec["lotSz"]))
        if min(multiplier, min_size, lot_size) <= 0:
            raise FullChainError("contract_minimum_unknown")
        price_tick = Decimal(str(spec.get("tickSz") or "0.001"))
        max_slippage = Decimal(str(self.orders.get("perp_max_slippage_bps", "20")))
        # The cash budget is a ceiling. The order is an exact token amount that
        # hedges to a multiple of lotSz and is at least minSz. One whole share
        # is not that floor: OKX's floor is minSz contracts, which for these
        # stock perps is often a fraction of a share.
        budget = size_quote
        # Never probe above what the wallet can actually pay: the venue answers
        # an over-balance quote with a funding prompt (`ok:false` and no error
        # text), which is not a price and would abort the search.
        probe_cap = budget
        if balance_quote is not None:
            affordable = (Decimal(str(balance_quote)) * Decimal("0.999")).quantize(
                Decimal(1).scaleb(-int(quote["decimals"])), rounding=ROUND_DOWN)
            probe_cap = min(budget, affordable)
        if probe_cap <= 0:
            raise FullChainError("no_quote_balance_available")
        if shares is None:
            try:
                probe = self.wallet.wallet.quote_exact_in(
                    quote["address"], token["address"], probe_cap)
                lots = _floor_step(probe.amount_out / multiplier, lot_size)
            except Exception:
                lots = min_size
            if lots < min_size:
                lots = min_size
            tolerance_bps = Decimal(str(self.monitor_config.get("monitor", {})
                                        .get("max_unhedged_bps", "5")))
            spend, spot_quote = self._size_spend_for_shares(
                quote=quote, token=token, shares=lots, budget=probe_cap,
                tolerance_bps=tolerance_bps, price_hint=perp_bid)
        else:
            # The strategy already priced this share count on QuoterV2. Do not
            # ask OnchainOS to re-quote the cash ceiling.
            lots = _floor_step(Decimal(str(shares)) / multiplier, lot_size)
            if lots < min_size:
                raise FullChainError(
                    f"size_below_contract_minimum:{lots} < {min_size}")
            spend = self._quoter_spend(asset, lots)
            spot_quote = None
        # Both legs of one stock trade stay inside the per-trade cash ceiling.
        # The short is the expensive leg when the basis is positive, so the
        # share count is cut to that notional before the spot is bought.
        cap = Decimal(str(self.limits.get("per_trade_quote_usdt") or "0"))
        if cap > 0 and perp_bid > 0:
            max_lots = _floor_step(cap / (perp_bid * multiplier), lot_size)
            if max_lots < min_size:
                raise FullChainError(
                    f"per_trade_below_contract_minimum:{cap}<{min_size}@{perp_bid}")
            if lots > max_lots:
                lots = max_lots
                if shares is None:
                    spend, spot_quote = self._size_spend_for_shares(
                        quote=quote, token=token, shares=lots, budget=min(probe_cap, cap),
                        tolerance_bps=tolerance_bps, price_hint=perp_bid)
                else:
                    spend = self._quoter_spend(asset, lots)
                    spot_quote = None
        shares = lots
        contracts = lots
        spot_price = spend / lots
        gross = (perp_bid - spot_price) / spot_price * Decimal(10000)
        net = gross - cost["total_bps"]
        warnings = []
        if not token.get("mapping_verified"):
            warnings.append("wrapper/share 映射未验证（mapping_verified=false）")
        if forced:
            warnings.append("强制流程测试：本次跳过净基差阈值，只验证链路")
        if not costs.get("verified"):
            warnings.append("成本模型未验证，按保守默认值计算")
        if spec.get("ctType") not in (None, "linear"):
            warnings.append(f"合约类型 {spec.get('ctType')} 非 linear")
        plan = Plan(
            asset=asset, chain_id=str(token["chain_id"]),
            token_address=token["address"], token_symbol=token["symbol"],
            token_decimals=int(token["decimals"]),
            quote_token_address=quote["address"], quote_symbol=quote["symbol"],
            quote_decimals=int(quote["decimals"]),
            pool_quote_symbol=token["quote_symbol"],
            # size_quote is what this trade is expected to cost; budget_quote is
            # the ceiling it was allowed to cost.
            size_quote=spend, budget_quote=budget, spot_price_usd=spot_price,
            perp_venue=_venue_of(perp_instrument), perp_instrument=perp_instrument,
            perp_bid_usd=perp_bid, perp_contract_multiplier=multiplier,
            perp_ask_usd=Decimal(str(ticker["askPx"])) if ticker.get("askPx") else None,
            perp_min_size=min_size, perp_lot_size=lot_size, perp_price_tick=price_tick,
            perp_max_slippage_bps=max_slippage, gross_bps=gross,
            cost_bps=cost["total_bps"], net_bps=net, threshold_bps=threshold,
            signal_qualified=net >= threshold and not forced,
            forced_plumbing_test=forced,
            expected_token_quantity=shares,
            perp_contract_quantity=contracts,
            quote_observed_at_s=quote_observed_at, warnings=warnings,
        )
        plan.intent = self._intent(plan)
        return plan

    def _quoter_spend(self, asset: str, shares: Decimal) -> Decimal:
        """USDG in for an exact token amount, from the local QuoterV2."""
        onchain = self.monitor_config.get("onchain") or {}
        quoter_address = str(onchain.get("quoter_v2") or "")
        rpc_url = str(onchain.get("rpc_url") or "")
        if self.quoter is None and (not quoter_address or not rpc_url):
            raise FullChainError("quoter_not_configured")
        pool = None
        for token in self.monitor_config.get("tokens") or []:
            if token.get("asset") != asset:
                continue
            pools = token.get("pools") or []
            pool = pools[0] if pools else None
            break
        if not pool:
            raise FullChainError(f"no_reviewed_pool_for_{asset}")
        raw = int(shares * (Decimal(10) ** int(pool.get("base_decimals") or 18)))
        fee = int(pool.get("fee") or 500)
        if self.quoter is not None:
            paid_raw = self.quoter(pool=pool, amount_raw=raw, fee=fee)
        else:
            from arb_monitor.data.xlayer_onchain import XLayerRPC, quote_single

            paid_raw = quote_single(
                XLayerRPC(rpc_url), quoter_address,
                token_in=str(pool["quote_address"]),
                token_out=str(pool.get("base_address") or ""),
                amount_raw=raw, fee=fee, exact_out=True)
        if not paid_raw:
            raise FullChainError(f"quoter_reverted:{asset}:{shares}")
        return Decimal(paid_raw) / (Decimal(10) ** int(pool.get("quote_decimals") or 6))

    def _size_spend_for_shares(self, *, quote: dict, token: dict, shares: Decimal,
                               budget: Decimal, tolerance_bps: Decimal,
                               price_hint: Decimal | None = None,
                               max_probes: int = 6) -> tuple[Decimal, object]:
        """Smallest exact-in spend whose quote delivers [N, N + tolerance) shares.

        Rounding is unavoidable when the venue cannot do exact-out: the pool
        returns an integer number of token units, so the achievable outputs are
        discrete. The question is only whether the remainder can be kept inside
        the tolerance the strategy already declares (``max_unhedged_bps``), and
        with 18 token decimals against 6 payment decimals it can: a single
        payment unit moves the output by far less than 5 bps of a share.

        Every probe is a read-only quote; the returned amount is what the
        caller should actually spend.
        """
        unit = Decimal(1).scaleb(-int(quote["decimals"]))
        band = Decimal(str(tolerance_bps)) / Decimal(10000)

        def ask(amount: Decimal, attempts: int = 4):
            """One read-only quote, retried a bounded number of times.

            Sizing needs several probes, and a single proxy hiccup in the middle
            of them would otherwise abort a run that has not spent anything yet.
            Reads are retryable; writes in this project never are. The backoff
            is deliberately generous: a burst of probes gets rate limited by the
            venue, and waiting is cheaper than losing the sample.
            """
            last = None
            for attempt in range(attempts):
                try:
                    return self.wallet.wallet.quote_exact_in(
                        quote["address"], token["address"], amount)
                except Exception as exc:  # noqa: BLE001 - retry, then fail closed
                    last = exc
                    if attempt + 1 < attempts:
                        self.sleeper(1.0 * (2 ** attempt))
            raise FullChainError(f"quote_failed_while_sizing:{last}")

        target = shares * (Decimal(1) + band / 2)
        try:
            opening = ask(budget, attempts=1)
        except FullChainError:
            opening = None
        if opening is not None and opening.amount_out >= shares:
            # Aim at the lower edge of the allowed band: a fill that lands
            # slightly high still fits, a fill that lands slightly low still hedges.
            amount = (budget * target / opening.amount_out).quantize(
                unit, rounding=ROUND_DOWN)
        elif price_hint is not None and price_hint > 0:
            # The ceiling did not price. Start from the contract-sized spend
            # instead of refusing a smaller order the venue would accept.
            amount = (shares * price_hint).quantize(unit, rounding=ROUND_UP)
            if amount > budget:
                amount = budget
        else:
            seen = getattr(opening, "amount_out", None)
            raise FullChainError(
                f"budget_buys_too_few_shares:{seen} < {shares}")
        for _ in range(max_probes):
            if amount <= 0:
                raise FullChainError("size_search_underflow")
            # Pace the probes: the venue throttles bursts of quote calls.
            self.sleeper(0.25)
            try:
                candidate = ask(amount)
            except Exception as exc:  # noqa: BLE001 - a failed probe is not a price
                raise FullChainError(f"quote_failed_while_sizing:{exc}") from exc
            delivered = candidate.amount_out
            if shares <= delivered < shares * (Decimal(1) + band):
                return amount, candidate
            # One proportional step toward the target output. A single payment
            # unit moves the output by far less than the band, so this lands
            # inside it when the pool state has not moved.
            stepped = (amount * target / delivered) if delivered > 0 else amount
            if delivered < shares:
                amount = stepped.quantize(unit, rounding=ROUND_UP) + unit
            else:
                trimmed = stepped.quantize(unit, rounding=ROUND_DOWN)
                amount = trimmed if trimmed < amount else amount - unit
            if amount > budget:
                raise FullChainError(f"size_search_exceeds_budget:{amount} > {budget}")
        raise FullChainError(
            "could_not_size_whole_share_purchase_within_" f"{tolerance_bps}bps")

    def _intent(self, plan: Plan) -> ExecutionIntent:
        created = self.clock()
        ttl_seconds = int(self.limits.get("quote_ttl_seconds", 300))
        return ExecutionIntent(
            signal_id=f"{plan.asset.lower()}-{uuid.uuid4().hex[:10]}",
            asset=plan.asset, chain_id=plan.chain_id,
            token_address=plan.token_address,
            quote_token_address=plan.quote_token_address,
            spot_amount=plan.size_quote,
            expected_token_quantity=plan.expected_token_quantity,
            perp_contract_quantity=plan.perp_contract_quantity,
            perp_venue=plan.perp_venue, perp_instrument=plan.perp_instrument,
            net_edge_bps=plan.net_bps, expected_profit_quote=plan.size_quote * plan.net_bps / Decimal(10000),
            created_ts_ms=created, expires_ts_ms=created + ttl_seconds * 1000,
            max_slippage_bps=plan.perp_max_slippage_bps,
            gas_budget_quote=Decimal("0"),
            perp_limit_price=plan.perp_limit_price,
            execution_allowed=True,
            metadata={"plan": plan.summary(), "order_style": "taker_both_legs"},
        )

    def _token(self, asset: str) -> dict:
        for token in self.monitor_config.get("tokens") or []:
            if token.get("asset") != asset:
                continue
            pools = token.get("pools") or []
            if not pools:
                raise FullChainError(f"no_reviewed_pool_for_{asset}")
            pool = pools[0]
            return {
                "asset": asset, "symbol": token["symbol"], "address": token["address"],
                "decimals": int(token.get("decimals", 18)),
                "chain_id": str(token.get("chain_id", "196")),
                "quote_address": pool["quote_address"], "quote_symbol": pool["quote_symbol"],
                "quote_decimals": int(pool.get("quote_decimals", 6)),
                "mapping_verified": bool(token.get("mapping_verified")),
            }
        raise FullChainError(f"asset_not_in_monitor_config:{asset}")

    def _default_quote_token(self) -> dict:
        """Payment token: the first allowlisted quote token, named not guessed."""
        # Allowlist order is the payment priority. USDG is first because that
        # is what the wallet holds; a later stablecoin (USDC) is permitted on
        # the route, but it must not become the token we try to spend just
        # because some pool is denominated in it.
        allowlisted = [str(x).lower() for x in (self.allowlist.get("quote_tokens") or [])]
        pools = []
        for token in self.monitor_config.get("tokens") or []:
            for pool in token.get("pools") or []:
                address = str(pool.get("quote_address", "")).lower()
                if address:
                    pools.append((address, pool))
        for wanted in allowlisted:
            for address, pool in pools:
                if address == wanted:
                    return {"address": address, "symbol": pool.get("quote_symbol"),
                            "decimals": int(pool.get("quote_decimals", 6))}
        quote = self.monitor_config.get("quote_token") or {}
        if quote.get("address"):
            return {"address": str(quote["address"]), "symbol": quote.get("symbol"),
                    "decimals": int(quote.get("decimals", 6))}
        raise FullChainError("no_allowlisted_quote_token")

    # --- gate ----------------------------------------------------------
    def risk_check(self, plan: Plan, *, open_contracts: Decimal,
                   balance_quote: Decimal | None = None) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        allow = self.allowlist
        reasons += self._hard_gate_reasons(plan)
        # Entry only: while a halt is in force the machine must not open new
        # risk, but the exit gates below stay open so a human (and the funding
        # stop) can always reduce what is already on.
        halt = Path(str((self.policy.get("execution") or {})
                        .get("auto_halt_file") or "data/AUTO_HALT"))
        if halt.exists():
            reasons.append("auto_halt_active")
        if plan.asset not in (allow.get("assets") or []):
            reasons.append("asset_not_allowlisted")
        if plan.chain_id not in [str(x) for x in (allow.get("chains") or [])]:
            reasons.append("chain_not_allowlisted")
        if plan.token_address.lower() not in [str(x).lower() for x in (allow.get("spot_tokens") or [])]:
            reasons.append("spot_token_not_allowlisted")
        if plan.perp_instrument not in (allow.get("instruments") or []):
            reasons.append("instrument_not_allowlisted")
        if plan.perp_venue not in (allow.get("venues") or []):
            reasons.append("venue_not_allowlisted")
        per_trade = Decimal(str(self.limits.get("per_trade_quote_usdt", "0")))
        if plan.size_quote > per_trade:
            reasons.append("per_trade_limit")
        hedge_notional = (plan.perp_contract_quantity * plan.perp_contract_multiplier
                          * plan.perp_bid_usd)
        if hedge_notional > per_trade:
            reasons.append("per_trade_hedge_limit")
        if open_contracts >= Decimal(str(self.limits.get("max_open_contracts", "1"))):
            reasons.append("open_contract_limit")
        if plan.perp_max_slippage_bps > Decimal(str(self.limits.get("max_slippage_bps", "0"))):
            reasons.append("slippage_limit")
        if plan.perp_contract_quantity < plan.perp_min_size:
            reasons.append("spot_below_min_contract_size")
        if balance_quote is not None and balance_quote < plan.size_quote:
            reasons.append("insufficient_quote_balance")
        if not plan.signal_qualified and not plan.forced_plumbing_test:
            reasons.append("net_basis_below_threshold")
        return (not reasons), reasons

    def _hard_gate_reasons(self, plan: Plan) -> list[str]:
        """The gates that no flag may skip, in either direction.

        ``--forced`` exists to run the plumbing when the market is not paying.
        It is not a licence to trade through a kill switch, through an
        unresolved halt, or on a quote nobody re-read: those three joined this
        list on 2026-09-16, because the entry gate used to check them only in
        the unattended path (and the exit gate did not check them at all).

        ``AUTO_HALT`` is deliberately *not* here — it is an entry gate, checked
        by ``risk_check`` only (2026-09-18: a halt is a decision not to open new
        risk, and an emergency exit that the halt itself refuses is worse than
        no halt at all). The kill switch stays in both directions: it is the
        operator's "stop the machine" switch, not a bookkeeping state.
        """
        reasons: list[str] = []
        execution = self.policy.get("execution") or {}
        kill = Path(str(execution.get("kill_switch_file") or "data/KILL_SWITCH"))
        if kill.exists():
            reasons.append("kill_switch")
        ttl = Decimal(str(self.limits.get("quote_ttl_seconds", "0")))
        if ttl > 0:
            age_s = Decimal(str(self.clock())) / Decimal(1000) - Decimal(
                str(plan.quote_observed_at_s))
            if age_s > ttl:
                reasons.append(f"quote_expired:{age_s:.0f}s>{ttl:.0f}s")
        if plan.perp_bid_usd is None or plan.perp_bid_usd <= 0:
            reasons.append("perp_price_unavailable")
        return reasons

    def risk_check_close(self, plan: Plan, *, contracts: Decimal,
                         tokens: Decimal, positions: list[dict] | None = None,
                         pending_orders: list[dict] | None = None
                         ) -> tuple[bool, list[str]]:
        """The exit gate: the same hard gates as entry, plus exit-specific ones.

        Closing used to be checked by nobody. It has to answer the same
        identity questions as opening (allowlist, mapping, price) plus three of
        its own: is there something to close, is it ours, and is the order
        going to reduce rather than flip the position.
        """
        reasons: list[str] = self._hard_gate_reasons(plan)
        if contracts <= 0:
            # Spot-only recovery does not submit a perp order; a missing CEX
            # quote must not block the token sale that reduces residual risk.
            reasons = [r for r in reasons if r != "perp_price_unavailable"]
        allow = self.allowlist
        if plan.asset not in (allow.get("assets") or []):
            reasons.append("asset_not_allowlisted")
        if plan.chain_id not in [str(x) for x in (allow.get("chains") or [])]:
            reasons.append("chain_not_allowlisted")
        if plan.token_address.lower() not in [str(x).lower()
                                              for x in (allow.get("spot_tokens") or [])]:
            reasons.append("spot_token_not_allowlisted")
        if plan.perp_instrument not in (allow.get("instruments") or []):
            reasons.append("instrument_not_allowlisted")
        if plan.perp_venue not in (allow.get("venues") or []):
            reasons.append("venue_not_allowlisted")
        if tokens < 0 or contracts < 0:
            reasons.append("negative_close_quantity")
        if tokens <= 0 and contracts <= 0:
            reasons.append("nothing_to_close")
        if contracts > 0 and positions is not None:
            # "Reduce-only" is enforced by the venue, but a reduce-only order
            # with no short behind it is a request to open a long. Checked
            # against the account, not against the command line.
            short = sum((Decimal(str(row.get("pos") or 0))
                         for row in positions
                         if str(row.get("instId") or "") == plan.perp_instrument
                         and Decimal(str(row.get("pos") or 0)) < 0), Decimal(0))
            if short == 0:
                reasons.append("no_short_position_to_close")
            elif contracts > abs(short):
                reasons.append(f"close_exceeds_position:{contracts}>{abs(short)}")
        if pending_orders is not None:
            own = [row for row in pending_orders
                   if str(row.get("instId") or "") == plan.perp_instrument]
            if own:
                reasons.append(f"pending_orders_on_instrument:{len(own)}")
        return (not reasons), reasons

    # --- execution -----------------------------------------------------
    def execute_open(self, plan: Plan, *, live: bool = False,
                     balance_quote: Decimal | None = None,
                     open_contracts: Decimal = Decimal(0)) -> RunResult:
        result = RunResult(state="planned", plan=plan)
        intent = plan.intent
        allowed, reasons = self.risk_check(plan, open_contracts=open_contracts,
                                           balance_quote=balance_quote)
        result.audit["risk"] = {"allowed": allowed, "reasons": reasons}
        self._record(intent, "precheck", {"allowed": allowed, "reasons": reasons,
                                          "plan": plan.summary()})
        if not allowed:
            result.state, result.reason = "rejected", ",".join(reasons)
            return result
        if not live:
            result.state = "dry_run_complete"
            # A plan is not an open position. Recording it as terminal keeps the
            # dashboard from showing a dry run as unresolved exposure.
            self._record(intent, "dry_run_complete",
                         {"reason": "plan_only_no_order_sent"})
            return result
        # exact-in at a *searched* amount: the venue cannot deliver an exact
        # output on this route, so the share count is reached by choosing how
        # much to spend (see _size_spend_for_shares).
        spot = self.wallet.swap_exact_in(
            token_in=plan.quote_token_address, token_out=plan.token_address,
            amount_in=plan.size_quote, token_decimals=plan.token_decimals,
            quote_token=plan.quote_token_address, quote_decimals=plan.quote_decimals,
            leg="buy",
        )
        result.spot = spot
        self._record(intent, "spot_filled", spot.as_event())
        if spot.receipt.status != "filled" or spot.token_quantity <= 0:
            result.state = "spot_not_confirmed"
            result.reason = f"receipt={spot.receipt.status}"
            return result
        # Hedge the whole share count the receipt actually shows. The spend was
        # searched so that this is the planned N, and a fill that came in under
        # N must not be hedged with contracts that are not backed by shares.
        spec = QuantitySpec.from_plan(plan)
        contracts = spec.contracts_for_tokens(spot.token_quantity)
        if spot.token_quantity < plan.expected_token_quantity:
            result.state = "unhedged_spot_only"
            result.reason = (
                f"spot_below_planned_shares:{spot.token_quantity} < "
                f"{plan.expected_token_quantity}")
            result.hedge_hint = (
                f"实际到账少于计划股数，未提交合约腿；需要人工确认后按 "
                f"{contracts} 张补对冲或卖出")
            self._record(intent, "hedge_skipped", {
                "spot_token_quantity": str(spot.token_quantity),
                "planned": str(plan.expected_token_quantity),
                "contracts_available": str(contracts)})
            return result
        if contracts < plan.perp_min_size:
            # No hedge is opened rather than a smaller-than-minimum or naked one.
            result.state = "unhedged_spot_only"
            result.reason = f"spot_quantity_below_min_contract:{spot.token_quantity}"
            result.hedge_hint = f"需补足到 {plan.perp_min_size * plan.perp_contract_multiplier} 枚才能对冲"
            self._record(intent, "hedge_skipped", {
                "spot_token_quantity": str(spot.token_quantity),
                "min_contracts": str(plan.perp_min_size),
                "contract_multiplier": str(plan.perp_contract_multiplier),
            })
            return result
        limit_price = self._fresh_limit_price(plan, side=MARKET_SELL)
        order = self.okx.place_ioc(
            inst_id=plan.perp_instrument, side=MARKET_SELL, size=contracts,
            price=limit_price,
            client_order_id=self._client_id(intent.signal_id),
        )
        result.perp_order = order
        self._record(intent, "perp_submitted", {
            "order": order, "size": str(contracts),
            "limit_price": str(limit_price),
            "reference_price_at_submit": str(plan.observed_reference_price),
            "style": "taker_ioc_at_best_bid",
        })
        fills, average, fees, hedged, status, resolved = self._settle_perp(
            plan, order, contracts)
        result.perp_fills = fills
        result.positions_after = self.okx.read.positions()
        result.unhedged_tokens = spec.residual_tokens(hedged, spot.token_quantity)
        if not resolved:
            # The order may still be live. Whatever the fills said, the hedge is
            # not a fact: report the volume we know, the remainder we do not,
            # and stop instead of calling it hedged.
            result.state = "perp_unknown"
            result.reason = (
                f"order_state_{status or 'unreadable'}_after_"
                f"{hedged}_of_{contracts}_contracts_known")
            result.hedge_hint = (
                "合约订单终态未知：只记录已知成交量，未知剩余未对冲；"
                "先按 ordId 查询订单终态，再决定补对冲或平掉已成交部分")
            self._record(intent, "perp_unknown", {
                "filled_contracts": str(hedged), "average_price": str(average),
                "fees": str(fees), "spot": spot.as_event(),
                "known_filled": str(hedged),
                "unknown_remaining": str(max(contracts - hedged, Decimal(0))),
                "unhedged_tokens": str(result.unhedged_tokens)})
            result.warnings.append(result.hedge_hint)
            return result
        state = "hedged" if hedged >= contracts else "perp_underfilled"
        result.state = state
        self._record(intent, state, {
            "filled_contracts": str(hedged), "average_price": str(average),
            "fees": str(fees), "unhedged_tokens": str(result.unhedged_tokens),
            "spot": spot.as_event(),
        })
        if result.unhedged_tokens != 0:
            result.warnings.append(
                f"现货与合约数量不等，净敞口 {result.unhedged_tokens} 枚")
        return result

    def _fresh_limit_price(self, plan: Plan, *, side: str) -> Decimal:
        """Taker limit from the top of book at submit time, not at plan time.

        A stale reference would either miss a fill (sell limit above the market)
        or pay more than the plan assumed. The band is the same reviewed
        ``perp_max_slippage_bps``; only the reference price is refreshed.
        """
        ticker = self.okx.ticker(plan.perp_instrument)
        reference = Decimal(str(ticker["bidPx"] if side == "sell" else ticker["askPx"]))
        raw = taker_limit_price(reference, plan.perp_max_slippage_bps, side=side)
        limit = (_round_to_tick(raw, plan.perp_price_tick) if side == "sell"
                 else _round_up_to_tick(raw, plan.perp_price_tick))
        plan.observed_reference_price = reference
        return limit

    def _settle_perp(self, plan: Plan, order: dict, expected: Decimal
                     ) -> tuple[list[dict], Decimal, Decimal, Decimal, str, bool]:
        """Read the order back. An unconfirmed IOC is a state, not a success.

        Returns ``(fills, average, fees, filled, status, resolved)``.

        ``resolved`` is the honest answer to "does the venue say this order is
        over?". Fills are *not* that answer: they can be partial, they can be
        from an earlier read, and the order that produced them can still be
        resting. Before 2026-09-16 this loop broke out of the wait as soon as
        ``fills`` was non-empty and the caller then compared the summed fill
        size against the request — so a legacy or partial fill row with an
        unreadable order status could be reported as a completed hedge.
        """
        order_id = str(order.get("ordId") or "")
        deadline = self.clock() + 15000
        status, fills = "", []
        while self.clock() < deadline:
            try:
                rows = self.okx.read.get("/api/v5/trade/order",
                                         f"instId={plan.perp_instrument}&ordId={order_id}")
                status = str((rows[0] if rows else {}).get("state") or "")
                fills = self.okx.read.get("/api/v5/trade/fills",
                                          f"instType=SWAP&instId={plan.perp_instrument}&ordId={order_id}")
            except Exception:
                self.sleeper(1.0)
                continue
            if status in TERMINAL_ORDER_STATES:
                break
            self.sleeper(1.0)
        filled = Decimal(0)
        notional = Decimal(0)
        fees = Decimal(0)
        for fill in fills:
            size = Decimal(str(fill.get("fillSz") or fill.get("size") or 0))
            price = Decimal(str(fill.get("fillPx") or fill.get("price") or 0))
            filled += size
            notional += size * price
            fees += abs(Decimal(str(fill.get("fee") or 0)))
        average = (notional / filled) if filled > 0 else Decimal(0)
        resolved = status in TERMINAL_ORDER_STATES
        if not resolved:
            # Known volume and unknown remainder, recorded separately, because
            # "filled 2 of 3" and "we cannot tell" are different facts and only
            # one of them is safe to continue from.
            self._record(plan.intent, "perp_unknown", {
                "order_id": order_id, "expected": str(expected),
                "status": status or "unreadable",
                "known_filled": str(filled),
                "unknown_remaining": str(max(expected - filled, Decimal(0))),
                "fills_present": bool(fills),
                "detail": "readback_incomplete"})
        return fills, average, fees, filled, status, resolved

    def execute_close(self, plan: Plan, *, live: bool = False,
                      token_quantity: Decimal | None = None,
                      contract_quantity: Decimal | None = None,
                      opened_group_id: str | None = None) -> RunResult:
        result = RunResult(state="planned", plan=plan)
        intent = plan.intent
        tokens = token_quantity if token_quantity is not None else plan.expected_token_quantity
        contracts = contract_quantity if contract_quantity is not None else plan.perp_contract_quantity
        if not live:
            result.state = "dry_run_complete"
            return result
        # The hedge is bought back first and must actually fill before the spot
        # leg is sold. Measured 2026-09-16: an IOC buy-back missed, the run sold
        # the spot anyway and reported "closed", leaving a naked short. A failed
        # buy-back has to leave the position as it was (long spot, short perp),
        # which is recoverable; a naked short is not.
        if contracts > 0:
            limit_price = self._fresh_limit_price(plan, side="buy")
            order = self.okx.place_ioc(
                inst_id=plan.perp_instrument, side="buy", size=contracts,
                # Buying back is a taker buy in the other direction: the
                # reference is the ask and the cap rounds UP, so the allowed
                # band is not silently narrowed by tick rounding.
                price=limit_price,
                client_order_id=self._client_id(intent.signal_id + "c"),
                reduce_only=True,
            )
            result.perp_order = order
            fills, average, fees, filled, status, resolved = self._settle_perp(
                plan, order, contracts)
            result.perp_fills = fills
            self._record(intent, "perp_closed", {
                "order": order, "filled": str(filled), "average": str(average),
                "fees": str(fees), "limit_price": str(limit_price),
                "reference_price_at_submit": str(plan.observed_reference_price),
                "style": "taker_ioc_at_best_ask"})
            if not resolved:
                # The buy-back may still be live. Selling the spot now would be
                # selling against a hedge whose size we cannot read, and the
                # review of 2026-09-16 found exactly this reading — "fills
                # exist, therefore filled" — could be reached here.
                result.state = "perp_unknown"
                result.reason = (f"order_state_{status or 'unreadable'}_after_"
                                 f"{filled}_of_{contracts}_contracts_known")
                result.unhedged_tokens = -(contracts - filled)  # short remaining
                result.hedge_hint = (
                    f"合约买回订单终态未知（已知成交 {filled}/{contracts} 张）："
                    "现货腿未卖出；先按 ordId 查询订单终态再决定")
                self._record(intent, "close_aborted", {
                    "reason": result.reason,
                    "known_filled": str(filled),
                    "unknown_remaining": str(max(contracts - filled, Decimal(0))),
                    "spot_leg": "not_sent"})
                result.warnings.append(result.hedge_hint)
                return result
            if filled < contracts:
                result.state = "close_perp_underfilled"
                result.reason = f"perp_buyback_filled_{filled}_of_{contracts}"
                result.unhedged_tokens = filled - contracts  # short remaining
                result.hedge_hint = (
                    f"合约只买回 {filled}/{contracts} 张，现货腿未卖出，"
                    f"仓位仍是「现货多 + 永续空」；需要重新买回剩余合约")
                self._record(intent, "close_aborted", {
                    "reason": result.reason, "spot_leg": "not_sent",
                    "contracts_remaining": str(contracts - filled)})
                return result
        if tokens <= 0:
            # Perp-only recovery: no spot to sell (e.g. a short left open by an
            # earlier failed close).
            result.state = "perp_only_closed"
            self._record(intent, result.state, {"spot_leg": "not_applicable"})
            return result
        spot = self.wallet.swap_exact_in(
            token_in=plan.token_address, token_out=plan.quote_token_address,
            amount_in=tokens, token_decimals=plan.token_decimals,
            quote_token=plan.quote_token_address, quote_decimals=plan.quote_decimals,
            leg="sell",
        )
        result.spot = spot
        result.state = "closed" if spot.receipt.status == "filled" else "close_unknown"
        self._record(intent, result.state, {"spot": spot.as_event()})
        if result.state == "closed" and opened_group_id and self.ledger is not None:
            # Tie the cycle together: the opening group is no longer open once
            # its closing group has settled.
            self.ledger.append(opened_group_id, "closed_by", "closed_by", {
                "closing_group_id": intent.signal_id,
                "spot_tx": spot.tx_hash,
                "quote_received": str(spot.quote_amount),
            }, self.clock())
        return result

    def _client_id(self, signal_id: str) -> str:
        # OKX rejects non-alphanumeric clOrdId (error 51000).
        return "arb" + "".join(c for c in signal_id if c.isalnum())[-24:]

    def _record(self, intent: ExecutionIntent | None, state: str, payload: dict) -> None:
        if self.ledger is None or intent is None:
            return
        self.ledger.append(intent.signal_id, state, state,
                           {"signal_id": intent.signal_id, **payload}, self.clock())


def _floor_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _venue_of(instrument: str) -> str:
    """Which venue a contract belongs to. Only reviewed venues are allowlisted."""
    upper = instrument.upper()
    if upper.endswith("-USDT-SWAP") or upper.endswith("-USDC-SWAP"):
        return "okx"
    if upper.endswith("_USDT") or upper.endswith("USDT"):
        return "gate"
    return "okx"
