from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import threading


@dataclass
class KillSwitch:
    path: Path | None = None
    _tripped: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def trip(self) -> None:
        with self._lock:
            self._tripped = True
            if self.path:
                self.path.write_text("TRIPPED\n", encoding="utf-8")

    def reset(self) -> None:
        with self._lock:
            self._tripped = False
            if self.path and self.path.exists():
                self.path.unlink()

    def is_tripped(self) -> bool:
        with self._lock:
            return self._tripped or bool(self.path and self.path.exists())

    def assert_allows_new_orders(self) -> None:
        if self.is_tripped():
            raise RuntimeError("kill_switch_tripped: new orders are disabled")
