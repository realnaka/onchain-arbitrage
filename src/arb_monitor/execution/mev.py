from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class MEVPolicy:
    """Broadcast-time policy; protection is never inferred from wallet branding."""

    enabled: bool = True
    require_private_route: bool = True
    supported_chains: frozenset[str] = frozenset({"1", "56", "501", "8453"})
    max_slippage_bps: int = 30
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self, chain_id: str) -> None:
        if self.require_private_route and self.enabled and chain_id not in self.supported_chains:
            raise ValueError(
                f"MEV protected broadcast is not verified for chain {chain_id}; "
                "refusing public-mempool fallback"
            )

    def extra_data(self, chain_id: str) -> dict[str, Any]:
        self.validate(chain_id)
        return {"enableMevProtection": self.enabled}


class Broadcaster(Protocol):
    def broadcast(self, signed_tx: str, chain_id: str, address: str,
                  extra_data: dict[str, Any] | None = None) -> dict[str, Any]: ...


@dataclass
class SafeBroadcaster:
    """Transport-neutral broadcaster; real HTTP implementation is intentionally separate."""

    policy: MEVPolicy = field(default_factory=MEVPolicy)
    allow_public_fallback: bool = False

    def prepare(self, signed_tx: str, chain_id: str, address: str) -> dict[str, Any]:
        self.policy.validate(chain_id)
        return {
            "signedTx": signed_tx,
            "chainIndex": chain_id,
            "address": address,
            "extraData": self.policy.extra_data(chain_id),
        }

    def broadcast(self, signed_tx: str, chain_id: str, address: str,
                  extra_data: dict[str, Any] | None = None) -> dict[str, Any]:
        # No network side effect in the default implementation.
        payload = self.prepare(signed_tx, chain_id, address)
        if extra_data:
            payload["extraData"].update(extra_data)
        raise NotImplementedError("configure OKX Broadcast API or an X Layer private RPC")
