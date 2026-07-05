"""S5 — patient milestone callbacks: keep the patient informed (and the cost
story straight) without them ever having to chase anyone. Bounded retries;
unreachable escalates once, then the advocate owns follow-up."""
from __future__ import annotations

from app import config
from app.agents import goals
from app.models import CasePhase


class PatientFlow:
    async def h_patient_callback(self, milestone: str, summary: str, attempt: int = 1) -> None:
        record = await self.place_call(goals.patient_update_goal(self.case, milestone, summary))
        if not record.answered:
            self.case.patient_unreachable_count += 1
            if self.case.patient_unreachable_count == 2:      # escalate once, on crossing
                self.escalate("Patient unreachable",
                              f"{self.case.patient.name} missed {self.case.patient_unreachable_count} "
                              f"update calls (milestone: {milestone}).",
                              "Try alternate contact / emergency contact on file.")
            if attempt >= config.NO_ANSWER_MAX_RETRIES:       # terminal: don't retry forever
                self.log("patient", f"Patient unreachable after {attempt} attempts "
                                    f"({milestone}); advocate owns follow-up.")
                return
            self.log("patient", f"Patient didn't pick up ({milestone}); retrying in 6h.")
            self.schedule("patient_callback", delay_hours=6, milestone=milestone,
                          summary=summary, attempt=attempt + 1)
            return
        self.case.patient_unreachable_count = 0
        if not self.outcome_ok(record, f"patient update ({milestone})"):
            return
        o = record.outcome
        if o.get("cost_explained"):
            for c in self.case.checklist:
                if c.requirement.startswith("Patient cost share"):
                    c.status, c.detail = "met", ("Capped-rental cost (~20%/month coinsurance, "
                                                 "owns after 13 months) explained on patient call.")
        concerns = o.get("patient_concerns") or "none noted"
        self.log("patient", f"☎ Patient updated ({milestone}). Concerns: {concerns}")
        if milestone == "delivery_scheduled":
            self.set_phase(CasePhase.DONE)
            self.log("state", "🎉 Case complete: order valid, supplier confirmed, delivery "
                              "scheduled, patient informed. Total coordination handled "
                              "without an advocate chasing anyone.")
