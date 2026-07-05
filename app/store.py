"""In-memory store (per assignment constraint: skip persistent DB).

The interface is the point: state lives behind get/put, events are append-only,
so swapping in Postgres (or replaying into Temporal) is mechanical, not a
redesign. Event-sourcing-shaped on purpose."""
from __future__ import annotations

from typing import Optional

from app.models import CallRecord, Case, Escalation


class Store:
    def __init__(self) -> None:
        self.cases: dict[str, Case] = {}
        self.calls: dict[str, CallRecord] = {}
        self.escalations: list[Escalation] = []

    # cases
    def put_case(self, case: Case) -> None:
        self.cases[case.id] = case

    def get_case(self, case_id: str) -> Optional[Case]:
        return self.cases.get(case_id)

    # calls
    def put_call(self, rec: CallRecord) -> None:
        self.calls[rec.id] = rec

    def get_call(self, call_id: str) -> Optional[CallRecord]:
        return self.calls.get(call_id)

    # escalations
    def add_escalation(self, esc: Escalation) -> None:
        self.escalations.append(esc)

    def open_escalations(self) -> list[Escalation]:
        return [e for e in self.escalations if not e.resolved]
