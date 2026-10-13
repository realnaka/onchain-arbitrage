from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Any


@dataclass(frozen=True)
class SwapQuote:
    chain_id: str
    token_in: str
    token_out: str
    amount_in: Decimal
    amount_out: Decimal
    amount_out_minimum: Decimal
    route: tuple[str, ...]
    price_impact_bps: Decimal | None
    gas: int | None
    expires_at_ms: int | None
    raw: dict[str, Any]


class ReadOnlySwapQuoteAdapter:
    """Normalizes an injected OKX OnchainOS quote callable; never signs or broadcasts."""
    def __init__(self, fetch: Callable[..., dict[str, Any]], chain_id: str = "196"):
        self.fetch, self.chain_id = fetch, chain_id

    def quote(self, token_in: str, token_out: str, amount_in: Decimal, **kwargs) -> SwapQuote:
        raw = self.fetch(chain_id=self.chain_id, token_in=token_in, token_out=token_out,
                         amount_in=amount_in, **kwargs)
        if raw.get("action") not in (None, "ok"):
            raise ValueError(f"quote_not_executable:{raw.get('action')}")
        return SwapQuote(self.chain_id, token_in, token_out, amount_in,
                         Decimal(str(raw["amount_out"])),
                         Decimal(str(raw.get("amount_out_minimum", raw["amount_out"]))),
                         tuple(raw.get("route", ())), Decimal(str(raw["price_impact_bps"])) if raw.get("price_impact_bps") is not None else None,
                         int(raw["gas"]) if raw.get("gas") is not None else None,
                         int(raw["expires_at_ms"]) if raw.get("expires_at_ms") is not None else None, raw)
