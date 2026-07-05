"""PCP-office call goals: order request, stall nudge, wrong-code correction.
All three share one outcome schema — same conversation, different opening stance."""
from __future__ import annotations

from app.agents.base import BASE_RULES, CONFIDENCE_FIELD, CallGoal
from app.models import Case, CallType

PCP_SCHEMA = {
    "type": "object",
    "properties": {
        "answered_meaningfully": {"type": "boolean"},
        "order_promised": {"type": "boolean", "description": "Did the office commit to sending the written order?"},
        "promised_within_hours": {"type": ["integer", "null"], "description": "Hours from now they committed to send it."},
        "office_had_no_record": {"type": "boolean", "description": "True if the office said they never received / have no record of the prior request."},
        "other_notes": {"type": "string"},
        **CONFIDENCE_FIELD,
    },
    "required": ["answered_meaningfully", "order_promised", "office_had_no_record", "confidence"],
}


def pcp_order_request_goal(case: Case) -> CallGoal:
    code, desc = case.equipment_hcpcs, case.equipment_desc
    prompt = f"""You are calling {case.pcp.practice} (front desk) on behalf of patient
{case.patient.name}, DOB on file, seen by {case.pcp.doctor} 3 days ago.
A verbal order for a {desc.lower()} (billing code {code}) is noted in
the chart. Your goal: request the signed STANDARD WRITTEN ORDER — Medicare
requires it before any supplier can deliver. Be specific that the order must say
"{desc.lower()}, {code}". Get a concrete commitment: who will send
it, and by when.
{BASE_RULES}"""
    return CallGoal(CallType.PCP_ORDER_REQUEST, case.pcp.practice, case.pcp.phone, prompt, PCP_SCHEMA)


def pcp_nudge_goal(case: Case) -> CallGoal:
    t = case.pcp_track
    prompt = f"""You are calling {case.pcp.practice} to FOLLOW UP: the written order for
{case.patient.name} ({case.equipment_desc.lower()}, {case.equipment_hcpcs}) was promised but has not
arrived. This is nudge #{t.stall_count}. Find out what happened. If they have no
record of the request, calmly re-place it in full. Either way, get a fresh,
concrete commitment with a due time.
{BASE_RULES}"""
    return CallGoal(CallType.PCP_NUDGE, case.pcp.practice, case.pcp.phone, prompt, PCP_SCHEMA)


def pcp_correction_goal(case: Case, invalid_reason: str) -> CallGoal:
    prompt = f"""You are calling {case.pcp.practice} because the written order they sent
for {case.patient.name} is WRONG and would cause a claim denial:
{invalid_reason}
Your goal: get them to issue a corrected order for "{case.equipment_desc.lower()},
billing code {case.equipment_hcpcs}", signed, with a concrete re-send commitment and due time.
{BASE_RULES}"""
    return CallGoal(CallType.PCP_CORRECTION, case.pcp.practice, case.pcp.phone, prompt, PCP_SCHEMA)
