"""Clock abstraction. The workflow spans days; the demo spans minutes.

SimClock lets multi-day SLA timers fire in seconds: the orchestrator advances
simulated time to the next scheduled item instead of sleeping. This is also
the standard trick for unit-testing time-based logic (freeze / advance)."""
from __future__ import annotations

from datetime import datetime, timedelta


class Clock:
    def now(self) -> datetime:
        raise NotImplementedError


class RealClock(Clock):
    def now(self) -> datetime:
        return datetime.now()


class SimClock(Clock):
    def __init__(self, start: datetime | None = None):
        # Default: start the simulated clock at the real current time, so a demo
        # reads "today" and fast-forwards from now. Tests pass a fixed start.
        self._now = start or datetime.now().replace(microsecond=0)

    def now(self) -> datetime:
        return self._now

    def advance_to(self, t: datetime) -> None:
        if t > self._now:
            self._now = t

    def advance(self, **kwargs) -> None:
        self._now += timedelta(**kwargs)
