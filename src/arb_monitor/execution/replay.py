from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable


@dataclass(frozen=True)
class ReplayEvent:
    ts_ms: int
    payload: dict


def load_events(path: str | Path) -> list[ReplayEvent]:
    events = []
    for line in Path(path).read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            events.append(ReplayEvent(int(row["ts_ms"]), row["payload"]))
    return sorted(events, key=lambda e: e.ts_ms)


class PaperReplay:
    """Deterministic event driver. It never performs external IO."""
    def __init__(self, events: Iterable[ReplayEvent], on_event: Callable[[ReplayEvent], object]):
        self.events = sorted(events, key=lambda e: e.ts_ms)
        self.on_event = on_event

    def run(self) -> list[object]:
        return [self.on_event(event) for event in self.events]
