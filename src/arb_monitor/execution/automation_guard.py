"""Hard, deterministic gates for unattended spot-first execution."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import time


@dataclass(frozen=True)
class AutomationPolicy:
    per_trade_quote: Decimal = Decimal("4")
    daily_quote: Decimal = Decimal("4")
    max_slippage_bps: Decimal = Decimal("50")
    max_unhedged_tokens: Decimal = Decimal("0")
    max_open_contracts: Decimal = Decimal("1")
    quote_ttl_seconds: int = 10
    kill_switch_file: str = "data/KILL_SWITCH"

    def allows(self, *, asset: str, chain_id: str, spot_token: str,
               quote_token: str, venue: str, instrument: str, wallet: str,
               quote_amount: Decimal, quote_observed_at: float,
               spot_filled: Decimal, open_contracts: Decimal) -> tuple[bool, str]:
        if Path(self.kill_switch_file).exists():
            return False, "kill_switch"
        if quote_amount > self.per_trade_quote:
            return False, "per_trade_limit"
        if time.time() - quote_observed_at > self.quote_ttl_seconds:
            return False, "quote_expired"
        if spot_filled <= 0:
            return False, "spot_not_confirmed"
        if open_contracts >= self.max_open_contracts:
            return False, "open_contract_limit"
        return True, "allow"
