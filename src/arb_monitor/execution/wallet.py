from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .adapters import OrderResult


@dataclass
class SafeXLayerWallet:
    """Read-only wallet façade. No private key is accepted by this process."""

    chain_id: str = "196"
    allow_signing: bool = False
    allowed_tokens: frozenset[str] = frozenset()
    allowed_routers: frozenset[str] = frozenset()

    def validate_token(self, address: str) -> dict[str, Any]:
        if self.allowed_tokens and address.lower() not in {x.lower() for x in self.allowed_tokens}:
            raise PermissionError("token is not in wallet allow-list")
        return {"address": address, "chain_id": self.chain_id, "decimals": 18}

    def get_balance(self, token_address: str) -> Decimal:
        raise NotImplementedError("configure a read-only RPC provider")

    def quote_exact_in(self, token_in: str, token_out: str, amount_in: Decimal) -> dict[str, Any]:
        raise NotImplementedError("configure a quote provider")

    def swap_exact_in(self, quote: dict[str, Any], amount_out_minimum: Decimal) -> OrderResult:
        if not self.allow_signing:
            raise PermissionError("wallet signing is disabled")
        raise NotImplementedError("KMS/MPC signer not configured")

    def sell_exact_in(self, quote: dict[str, Any], amount_out_minimum: Decimal) -> OrderResult:
        return self.swap_exact_in(quote, amount_out_minimum)
