"""The Orchestrator: deterministic control flow for the whole case.

Every decision is explicit code — gates, retries, SLA policies, failover. LLMs
never choose transitions; they produce typed CallOutcomes that the flows apply.
The audit trail is the event log.

Structure: this module owns the shared plumbing (calls, timers, escalation,
the run loop); the four coordination surfaces live in app/flows/ as mixins —
supplier research, PCP order chase, match/failover, patient callbacks.

Inbound events ("a written order arrived", "the supplier called back") are
handled generically, the way webhooks/fax-intake would deliver them in
production. The simulated world produces them via a bridge wired at startup —
this module never reads scenarios.yaml."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Optional

from app import config, rules
from app.agents.base import CallGoal
from app.clock import Clock
from app.flows import MatchFlow, PatientFlow, PCPFlow, SupplierFlow
from app.models import (Case, CallRecord, CasePhase, Escalation, Event, Promise,
                        SupplierContact, SupplierState)
from app.scheduler import ScheduledAction, Scheduler
from app.store import Store
from app.voice.engine import VoiceEngine


class Orchestrator(SupplierFlow, PCPFlow, MatchFlow, PatientFlow):
    def __init__(self, store: Store, clock: Clock, scheduler: Scheduler,
                 engine: VoiceEngine, case: Case,
                 world_bridge=None):
        self.store = store
        self.clock = clock
        self.scheduler = scheduler
        self.engine = engine
        self.case = case
        # world_bridge(effects, clock, scheduler): demo-only hook that turns
        # scenario side-effects into inbound scheduler events.
        self.world_bridge = world_bridge
        self.step_delay = 0.0   # real-seconds pacing for live demos (set by run loop)
        self.active_calls: list[dict] = []  # live-call view: calls in flight + partial transcripts
        store.put_case(case)

    # --- plumbing (shared by all flows) -----------------------------------------

    def now(self) -> datetime:
        return self.clock.now()

    def log(self, kind: str, message: str, **data: Any) -> None:
        self.case.events.append(Event(at=self.now(), kind=kind, message=message, data=data))

    def set_phase(self, phase: CasePhase) -> None:
        if self.case.phase != phase:
            self.log("state", f"Case phase: {self.case.phase.value} → {phase.value}")
            self.case.phase = phase

    def schedule(self, action: str, delay_hours: float = 0, **data: Any) -> None:
        self.scheduler.schedule(ScheduledAction(
            due_at=self.now() + timedelta(hours=delay_hours), action=action, data=data))

    def escalate(self, reason: str, context: str, recommended: str) -> None:
        esc = Escalation(at=self.now(), reason=reason, context=context,
                         recommended_action=recommended)
        self.store.add_escalation(esc)
        self.log("escalation", f"⚠ Escalated to care advocate: {reason}", escalation_id=esc.id)

    def add_promise(self, who: str, what: str, due_hours: float, check_action: str,
                    grace_hours: float = 2, **check_data: Any) -> Promise:
        """Every commitment becomes a timer. Ghosting = this timer firing
        with fulfilled=False and no state advance."""
        p = Promise(who=who, what=what, due_at=self.now() + timedelta(hours=due_hours))
        self.case.promises.append(p)
        self.schedule(check_action, delay_hours=due_hours + grace_hours,
                      promise_id=p.id, **check_data)
        self.log("promise", f"Promise recorded — {who}: “{what}” due in {due_hours:.0f}h",
                 promise_id=p.id)
        return p

    def promise(self, pid: str) -> Optional[Promise]:
        return next((p for p in self.case.promises if p.id == pid), None)

    async def place_call(self, goal: CallGoal) -> CallRecord:
        self.log("call", f"📞 Calling {goal.counterparty_name} ({goal.call_type.value}) …")
        live = {"counterparty": goal.counterparty_name, "call_type": goal.call_type.value,
                "phone": goal.phone, "turns": []}
        self.active_calls.append(live)
        try:
            record, result = await self.engine.place_call(
                goal, self.now(),
                on_turn=lambda t: live["turns"].append({"speaker": t.speaker, "text": t.text}),
                turn_delay=min(self.step_delay * 0.9, 1.4))
        except Exception as e:
            # A telephony/LLM error must not crash the whole case (a live call
            # dies in isolation in the real world too). Record it as an
            # unanswered call and escalate so a human sees it.
            record = CallRecord(call_type=goal.call_type, counterparty=goal.counterparty_name,
                                phone=goal.phone, answered=False, at=self.now())
            self.store.put_call(record)
            self.escalate(f"Call to {goal.counterparty_name} failed with an error",
                          f"{goal.call_type.value}: {type(e).__name__}: {e}",
                          "Investigate the voice/LLM integration; retry the call manually.")
            self.log("error", f"✗ Call to {goal.counterparty_name} errored: {e}",
                     call_id=record.id)
            return record
        finally:
            self.active_calls.remove(live)
        self.store.put_call(record)
        if result.world_effects and self.world_bridge:
            self.world_bridge(result.world_effects, self.clock, self.scheduler)
        status = "answered" if record.answered else "NO ANSWER"
        self.log("call", f"Call to {goal.counterparty_name}: {status}"
                 + (f" (confidence {record.confidence:.2f})" if record.answered else ""),
                 call_id=record.id, answered=record.answered)
        return record

    def outcome_ok(self, record: CallRecord, what: str) -> bool:
        """Confidence gate: the FSM is not allowed to act on a guess."""
        if record.confidence < config.CONFIDENCE_THRESHOLD:
            self.escalate(f"Low-confidence extraction on {what}",
                          f"Call {record.id} to {record.counterparty} extracted with "
                          f"confidence {record.confidence:.2f} (< {config.CONFIDENCE_THRESHOLD}). "
                          "Transcript attached for review.",
                          "Review transcript; correct the outcome or re-run the call.")
            return False
        return True

    # --- case start ---------------------------------------------------------------

    def start(self) -> None:
        case = self.case
        self.log("state", f"Case opened for {case.patient.name}: "
                          f"{case.equipment_desc} ({case.equipment_hcpcs})")
        self.set_phase(CasePhase.COVERAGE_CHECK)
        case.checklist = rules.run_coverage_check(case)
        blocked = [c for c in case.checklist if c.status == "blocked"]
        if blocked:
            self.set_phase(CasePhase.NEEDS_HUMAN)
            self.escalate("Coverage check failed", blocked[0].detail, "Review pathway with patient.")
            return
        pa = next((c for c in case.checklist if c.requirement == "Prior authorization"), None)
        pa_note = ("prior authorization required — must clear before delivery"
                   if pa and pa.status == "pending" else "no prior authorization required")
        self.log("coverage", f"Coverage checklist generated — {case.equipment_hcpcs} covered "
                             f"under Part B; {pa_note}; written order + enrolled supplier "
                             "still needed.")
        self.set_phase(CasePhase.COORDINATING)

        # Geo prefilter — naive city-name match on purpose (production: geocode
        # + drive-time radius; the directory only gives us an address string).
        in_area: list[SupplierContact] = []
        for sc in case.suppliers:
            city_ok = any(c in sc.supplier.address.lower() for c in config.SERVICE_AREA_CITIES)
            if city_ok:
                in_area.append(sc)
            else:
                sc.state = SupplierState.OUT_OF_AREA
        self.log("suppliers", f"Directory: {len(case.suppliers)} suppliers; "
                              f"{len(in_area)} plausibly in service area; contacting up to "
                              f"{config.MAX_SUPPLIERS_TO_CONTACT} in parallel.")

        self.schedule("qualify_suppliers")          # S3 fan-out
        self.schedule("request_pcp_order")          # S2 critical path
        self.schedule("patient_callback",           # S5 milestone 1
                      milestone="coverage_confirmed",
                      summary=f"Medicare covers the {case.equipment_desc.lower()}; we're "
                              f"getting the written order from {case.pcp.doctor} and lining "
                              "up suppliers. Nothing needed from the patient right now.")

    # --- main loop -------------------------------------------------------------------

    async def run_to_completion(self, max_steps: int = 300,
                                step_delay: float = 0.0) -> None:
        """Demo driver: process due work; when idle, jump simulated time to the
        next timer. Production: a worker loop against real time.
        step_delay: real-seconds pause between actions so a live audience can
        watch the case unfold (0 = as fast as possible)."""
        self.step_delay = step_delay
        steps = 0
        while steps < max_steps and self.case.phase != CasePhase.DONE:
            item = self.scheduler.pop_due(self.now())
            if item:
                await self.handle(item)
                steps += 1
                if step_delay:
                    await asyncio.sleep(step_delay)
                continue
            nxt = self.scheduler.peek_next_time()
            if nxt is None:
                break  # nothing scheduled, nothing due → blocked or finished
            self.clock.advance_to(nxt)  # time-jump (SimClock only)

    async def handle(self, item: ScheduledAction) -> None:
        fn = getattr(self, f"h_{item.action}", None)
        if fn is None:
            self.log("error", f"No handler for action {item.action}")
            return
        await fn(**item.data)
