from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

HK = ZoneInfo("Asia/Hong_Kong")


@dataclass(frozen=True)
class HKCalendar:
    """Reviewed cash-market calendar with explicit coverage and overrides.

    Unknown years fail closed. Security suspensions / exceptional closures
    require an operator override; perpetual trading hours are not HKEX hours.
    """
    years: frozenset[int]
    holidays: frozenset[date]
    half_days: frozenset[date] = frozenset()
    extra_closures: frozenset[date] = frozenset()

    @classmethod
    def from_config(cls, config: dict) -> "HKCalendar":
        return cls(
            frozenset(config["years"]),
            frozenset(date.fromisoformat(x) for x in config["holidays"]),
            frozenset(date.fromisoformat(x) for x in config.get("half_days", [])),
            frozenset(date.fromisoformat(x) for x in config.get("extra_closures", [])),
        )

    def is_session(self, day: date) -> bool | None:
        if day.year not in self.years:
            return None
        return day.weekday() < 5 and day not in self.holidays | self.extra_closures

    def previous_session(self, day: date) -> date | None:
        for offset in range(1, 31):
            candidate = day - timedelta(days=offset)
            status = self.is_session(candidate)
            if status is None:
                return None
            if status:
                return candidate
        return None

    def phase(self, ts_ms: int) -> str:
        dt = datetime.fromtimestamp(ts_ms // 1000, HK)
        session = self.is_session(dt.date())
        if session is None:
            return "calendar_unknown"
        if not session:
            return "closed"
        minute = dt.hour * 60 + dt.minute
        if minute < 540:
            return "before_0900"
        if minute < 570:
            return "preopen_0900_0930"
        if minute < 720:
            return "continuous_am"
        if dt.date() in self.half_days:
            return "after_close"
        if minute < 780:
            return "lunch"
        if minute < 960:
            return "continuous_pm"
        if minute <= 970:
            return "closing_auction"
        return "after_close"
