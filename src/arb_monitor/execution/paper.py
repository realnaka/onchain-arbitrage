from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from .adapters import Balance, InstrumentSpec, OrderRequest, OrderResult
from .network import NetworkFault, NetworkState


@dataclass
class PaperCEXAdapter:
    venue: str
    instruments: dict[str, InstrumentSpec]
    balances: list[Balance] = field(default_factory=list)
    fill_ratio: Decimal = Decimal("1")
    fail_orders: bool = False
    timeout_orders: bool = False
    response_lost_orders: bool = False
    cancel_unknown: bool = False
    orders: dict[str, OrderResult] = field(default_factory=dict)
    _counter: int = 0

    def get_balances(self): return list(self.balances)
    def get_instrument(self, symbol): return self.instruments[symbol]
    def place_order(self, request: OrderRequest) -> OrderResult:
        if self.fail_orders: raise RuntimeError("paper_order_rejected")
        if self.timeout_orders:
            raise NetworkFault("paper_order_timeout", NetworkState.REQUEST_TIMEOUT,
                               may_have_reached_remote=True)
        self._counter += 1
        qty = (request.quantity * self.fill_ratio).quantize(self.instruments[request.symbol].quantity_step)
        result = OrderResult(f"paper-{self._counter}", "filled" if qty == request.quantity else "partially_filled", qty, request.price)
        self.orders[result.order_id] = result
        if self.response_lost_orders:
            raise NetworkFault("paper_response_lost", NetworkState.RESPONSE_UNKNOWN,
                               may_have_reached_remote=True)
        return result
    def cancel_order(self, symbol, order_id):
        if self.cancel_unknown:
            raise NetworkFault("paper_cancel_unknown", NetworkState.RESPONSE_UNKNOWN,
                               may_have_reached_remote=True)
        self.orders.pop(order_id, None)
    def get_order(self, symbol, order_id): return self.orders[order_id]


@dataclass
class PaperWalletAdapter:
    chain_id: str = "196"
    fill_ratio: Decimal = Decimal("1")
    fail_swaps: bool = False
    timeout_swaps: bool = False
    pending_swaps: bool = False
    quotes: list[dict] = field(default_factory=list)
    _counter: int = 0

    def validate_token(self, address): return {"address": address, "valid": True}
    def get_balance(self, token_address): return Decimal("1000000")
    def quote_exact_in(self, token_in, token_out, amount_in):
        quote = {"token_in": token_in, "token_out": token_out, "amount_in": str(amount_in),
                 "amount_out": str(amount_in), "amount_out_minimum": amount_in,
                 "route": ["paper"], "action": "ok"}
        self.quotes.append(quote)
        return quote
    def swap_exact_in(self, quote, amount_out_minimum):
        if self.fail_swaps: raise RuntimeError("paper_swap_reverted")
        if self.timeout_swaps:
            raise NetworkFault("paper_swap_broadcast_unknown",
                               NetworkState.CHAIN_BROADCAST_UNKNOWN,
                               may_have_reached_remote=True)
        self._counter += 1
        qty = Decimal(quote["amount_out"]) * self.fill_ratio
        status = "pending" if self.pending_swaps else "filled"
        return OrderResult(f"paper-swap-{self._counter}", status, qty)
    sell_exact_in = swap_exact_in
