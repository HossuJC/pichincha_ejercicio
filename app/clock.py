from datetime import date, datetime, time, timezone
from typing import Protocol


class Clock(Protocol):
    def today(self) -> date: ...

    def now(self) -> datetime: ...


class FixedClock:
    """Business date pinned by configuration (the exercise fixes it to 2026-10-26)."""

    def __init__(self, today: date):
        self._today = today

    def today(self) -> date:
        return self._today

    def now(self) -> datetime:
        wall = datetime.now(timezone.utc).time()
        return datetime.combine(self._today, time(wall.hour, wall.minute, wall.second), tzinfo=timezone.utc)
