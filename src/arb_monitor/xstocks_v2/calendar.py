"""2026 NYSE session labels. They do not change the v2 edge threshold."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

# NYSE cash-session holidays for 2026. Source: NYSE Group holiday calendar.
HOLIDAYS_2026 = frozenset({
    date(2026, 1, 1),
    date(2026, 1, 19),
    date(2026, 2, 16),
    date(2026, 4, 3),
    date(2026, 5, 25),
    date(2026, 6, 19),
    date(2026, 7, 3),
    date(2026, 9, 7),
    date(2026, 11, 26),
    date(2026, 12, 25),
})

# Early close 13:00 ET. After that minute the session is not regular.
HALF_DAYS_2026 = frozenset({
    date(2026, 11, 27),
    date(2026, 12, 24),
})


def session_label(moment: datetime) -> str:
    local = moment.astimezone(NY)
    day = local.date()
    if day.year != 2026:
        return "calendar_unknown"
    if day.weekday() >= 5 or day in HOLIDAYS_2026:
        return "closed"
    minute = local.hour * 60 + local.minute
    close_minute = 13 * 60 if day in HALF_DAYS_2026 else 16 * 60
    if minute < 9 * 60 + 30:
        return "premarket"
    if minute >= close_minute:
        return "after_hours"
    return "regular"
