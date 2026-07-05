"""Due-work scheduler: a heap of (due_at, action).

Every promise made on any call becomes a timer here. Ghost detection is not a
special feature — it is simply "a timer fired and the state hadn't advanced."
Production swap: Temporal / a DB-backed job table + worker. Interface is tiny
on purpose so that swap is mechanical."""
from __future__ import annotations

import heapq
import itertools
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class ScheduledAction(BaseModel):
    due_at: datetime
    action: str                    # e.g. "qualify_supplier", "check_promise"
    data: dict[str, Any] = {}


class Scheduler:
    def __init__(self) -> None:
        self._heap: list[tuple[datetime, int, ScheduledAction]] = []
        self._seq = itertools.count()  # tiebreaker: FIFO among equal due times

    def schedule(self, item: ScheduledAction) -> None:
        heapq.heappush(self._heap, (item.due_at, next(self._seq), item))

    def pop_due(self, now: datetime) -> Optional[ScheduledAction]:
        if self._heap and self._heap[0][0] <= now:
            return heapq.heappop(self._heap)[2]
        return None

    def peek_next_time(self) -> Optional[datetime]:
        return self._heap[0][0] if self._heap else None

    def pending(self) -> list[ScheduledAction]:
        return [x[2] for x in sorted(self._heap)]
