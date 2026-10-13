"""Bridge monitoring Intents into the execution model.

This module deliberately preserves the monitoring safety gate: intents emitted by
the current research strategy are not executable until a later validation phase
sets ``execution_allowed`` explicitly.
"""
from __future__ import annotations

from decimal import Decimal
from arb_monitor.core.models import Intent, Side
from .intent import ExecutionIntent


def execution_intent_from_monitor(intent: Intent) -> ExecutionIntent:
    if len(intent.legs) != 2:
        raise ValueError("execution requires exactly two legs")
    spot = next((x for x in intent.legs if x.venue.startswith("onchain")), None)
    perp = next((x for x in intent.legs if not x.venue.startswith("onchain")), None)
    if spot is None or perp is None or spot.side != Side.BUY or perp.side != Side.SELL:
        raise ValueError("only spot-buy/perpetual-sell intents are supported")
    m = intent.metadata
    return ExecutionIntent(
        signal_id=str(m.get("signal_id") or m.get("dedupe_key") or intent.symbol),
        asset=intent.symbol,
        chain_id=str(m.get("chain_id", "196")),
        token_address=spot.symbol,
        quote_token_address=str(m.get("quote_token_address", "")),
        spot_amount=Decimal(str(m.get("spot_open_usdt", spot.notional))),
        expected_token_quantity=spot.quantity,
        perp_contract_quantity=perp.quantity,
        perp_venue=perp.venue,
        perp_instrument=perp.symbol,
        net_edge_bps=intent.net_edge_bps,
        expected_profit_quote=intent.expected_profit_quote,
        created_ts_ms=intent.created_ts_ms,
        expires_ts_ms=intent.created_ts_ms + intent.ttl_ms,
        max_slippage_bps=Decimal(str(m.get("max_slippage_bps", "0"))),
        gas_budget_quote=Decimal(str(m.get("gas_budget_usdt", "0"))),
        execution_allowed=bool(m.get("execution_allowed", False)),
        metadata=dict(m),
    )
