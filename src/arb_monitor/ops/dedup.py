from __future__ import annotations

from dataclasses import dataclass

from arb_monitor.core.models import Intent


@dataclass
class SignalDeduplicator:
    cooldown_ms: int = 3000

    def __post_init__(self) -> None:
        self._last_seen: dict[str, int] = {}

    def accept(self, intent: Intent) -> bool:
        key = str(intent.metadata.get("dedupe_key", intent.symbol))
        previous = self._last_seen.get(key)
        if previous is not None and intent.created_ts_ms - previous < self.cooldown_ms:
            return False
        self._last_seen[key] = intent.created_ts_ms
        return True

