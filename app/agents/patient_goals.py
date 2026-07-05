"""Patient-facing call goal: milestone status updates + cost clarity.
Benefit language comes from the case (capped-rental DME), not invented by the LLM."""
from __future__ import annotations

from app.agents.base import BASE_RULES, CONFIDENCE_FIELD, CallGoal
from app.models import Case, CallType


def patient_update_goal(case: Case, milestone: str, status_summary: str) -> CallGoal:
    supplemental = ("has a supplemental plan that may cover the coinsurance"
                    if case.patient.has_supplemental else
                    "has no supplemental plan, so the coinsurance is out of pocket")
    prompt = f"""You are calling patient {case.patient.name}, {case.patient.age}, with a status update.
Milestone: {milestone}.
Current status to convey, in plain language (no jargon, no code numbers unless asked):
{status_summary}

Also, exactly once per call, make sure the patient understands cost. This is a
RENTAL benefit, not a purchase: Medicare pays the supplier about 80% of the
approved amount each month (up to 13 months, after any remaining Part B
deductible); the patient owes about 20% coinsurance per rental month and owns
the equipment after 13 months. The patient {supplemental}.
The supplier bills Medicare directly — never pay the full price upfront to anyone.
Reassure the patient they do not need to call anyone; we do the chasing. Answer
questions from the briefing only.
{BASE_RULES}"""
    schema = {
        "type": "object",
        "properties": {
            "answered_meaningfully": {"type": "boolean"},
            "patient_understood": {"type": ["boolean", "null"]},
            "cost_explained": {"type": "boolean"},
            "patient_concerns": {"type": "string", "description": "Any worry/confusion that might need advocate follow-up."},
            **CONFIDENCE_FIELD,
        },
        "required": ["answered_meaningfully", "cost_explained", "confidence"],
    }
    return CallGoal(CallType.PATIENT_UPDATE, case.patient.name, case.patient.phone, prompt, schema,
                    extra={"milestone": milestone})
