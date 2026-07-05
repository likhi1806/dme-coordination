"""Domain models. Pydantic is the contract layer between probabilistic (LLM)
and deterministic (FSM) code: every LLM output must validate into one of these
before it is allowed to touch workflow state."""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


def _id() -> str:
    return uuid.uuid4().hex[:10]


# --- Enums / states -----------------------------------------------------------

class CasePhase(str, Enum):
    INTAKE_COMPLETE = "INTAKE_COMPLETE"
    COVERAGE_CHECK = "COVERAGE_CHECK"
    COORDINATING = "COORDINATING"        # PCP chase + supplier research in parallel
    READY_TO_MATCH = "READY_TO_MATCH"    # order valid AND >=1 supplier qualified
    MATCHED = "MATCHED"                  # delivery confirmed with a supplier
    DELIVERY_SCHEDULED = "DELIVERY_SCHEDULED"
    DONE = "DONE"
    NEEDS_HUMAN = "NEEDS_HUMAN"


class SupplierState(str, Enum):
    NOT_CONTACTED = "NOT_CONTACTED"
    CALLING = "CALLING"
    NO_ANSWER = "NO_ANSWER"
    DISQUALIFIED = "DISQUALIFIED"
    QUALIFIED = "QUALIFIED"
    CONFIRMING = "CONFIRMING"
    CONFIRMED = "CONFIRMED"
    GHOSTED = "GHOSTED"
    OUT_OF_AREA = "OUT_OF_AREA"          # geo prefilter, never called


class PCPOrderState(str, Enum):
    NOT_REQUESTED = "NOT_REQUESTED"
    REQUESTED = "REQUESTED"              # promise outstanding
    STALLED = "STALLED"                  # promised-by passed, nothing arrived
    RECEIVED = "RECEIVED"                # order arrived, pending validation
    INVALID = "INVALID"                  # wrong code/equipment -> back to PCP
    VALID = "VALID"


class CallType(str, Enum):
    SUPPLIER_QUALIFICATION = "SUPPLIER_QUALIFICATION"
    SUPPLIER_CONFIRMATION = "SUPPLIER_CONFIRMATION"
    SUPPLIER_CHASE = "SUPPLIER_CHASE"
    PCP_ORDER_REQUEST = "PCP_ORDER_REQUEST"
    PCP_NUDGE = "PCP_NUDGE"
    PCP_CORRECTION = "PCP_CORRECTION"
    PATIENT_UPDATE = "PATIENT_UPDATE"


# --- Core entities --------------------------------------------------------------

class Patient(BaseModel):
    name: str
    age: int
    phone: str = "(312) 555-0111"
    address: str = "Chicago, IL"
    insurance: str = "Original Medicare Part B"
    has_supplemental: bool = False


class PCP(BaseModel):
    doctor: str
    practice: str
    phone: str


class Supplier(BaseModel):
    id: str = Field(default_factory=_id)
    name: str
    phone: str
    address: str


class WrittenOrder(BaseModel):
    hcpcs_code: str
    description: str
    signed_by: str
    received_at: Optional[datetime] = None


class Promise(BaseModel):
    """Anything a counterparty committed to, with a due time.
    Promises are the unit of chasing: ghost detection = expired promise."""
    id: str = Field(default_factory=_id)
    who: str
    what: str
    due_at: datetime
    fulfilled: bool = False


class ChecklistItem(BaseModel):
    requirement: str
    status: str                      # "met" | "pending" | "blocked"
    detail: str = ""


class TranscriptTurn(BaseModel):
    speaker: str                     # "agent" | counterparty name
    text: str


class CallRecord(BaseModel):
    id: str = Field(default_factory=_id)
    call_type: CallType
    counterparty: str
    phone: str
    answered: bool
    transcript: list[TranscriptTurn] = []
    outcome: dict[str, Any] = {}
    confidence: float = 0.0
    at: datetime


class SupplierContact(BaseModel):
    supplier: Supplier
    state: SupplierState = SupplierState.NOT_CONTACTED
    attempts: int = 0
    disqualify_reason: str = ""
    # qualification facts extracted from the call
    accepting_medicare: Optional[bool] = None
    k0001_in_stock: Optional[bool] = None
    accepts_assignment: Optional[bool] = None
    serves_address: Optional[bool] = None
    delivery_window_days: Optional[int] = None
    confirmed_delivery_date: Optional[datetime] = None
    call_ids: list[str] = []


class PCPOrderTrack(BaseModel):
    state: PCPOrderState = PCPOrderState.NOT_REQUESTED
    promised_by: Optional[datetime] = None
    request_attempts: int = 0
    stall_count: int = 0
    order: Optional[WrittenOrder] = None
    invalid_reason: str = ""
    call_ids: list[str] = []


class Escalation(BaseModel):
    id: str = Field(default_factory=_id)
    at: datetime
    reason: str
    context: str
    recommended_action: str
    resolved: bool = False
    resolution: str = ""


class Event(BaseModel):
    """Append-only case event log — the audit trail and the demo timeline."""
    at: datetime
    kind: str                        # e.g. "state", "call", "promise", "escalation"
    message: str
    data: dict[str, Any] = {}


class Case(BaseModel):
    id: str = "eleanor-001"
    patient: Patient
    pcp: PCP
    equipment_hcpcs: str = "K0001"
    equipment_desc: str = "Standard manual wheelchair"
    phase: CasePhase = CasePhase.INTAKE_COMPLETE
    checklist: list[ChecklistItem] = []
    pcp_track: PCPOrderTrack = PCPOrderTrack()
    suppliers: list[SupplierContact] = []
    matched_supplier_id: Optional[str] = None
    promises: list[Promise] = []
    events: list[Event] = []
    patient_unreachable_count: int = 0

    # -- convenience -----------------------------------------------------------
    def supplier_by_id(self, sid: str) -> Optional[SupplierContact]:
        return next((s for s in self.suppliers if s.supplier.id == sid), None)

    def qualified_ranked(self) -> list[SupplierContact]:
        """Qualified suppliers, best delivery window first."""
        q = [s for s in self.suppliers if s.state == SupplierState.QUALIFIED]
        return sorted(q, key=lambda s: s.delivery_window_days or 999)
