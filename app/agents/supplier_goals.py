"""Supplier-facing call goals: qualification, order confirmation, chase.

Prompts are parameterized from the Case (equipment code/description, patient
area) so the engine works for any DME case, not just Eleanor's wheelchair.
NOTE: the outcome field is still named `k0001_in_stock` for historical reasons;
its *meaning* is "the requested HCPCS code is in stock" (see schema description).
"""
from __future__ import annotations

from app.agents.base import BASE_RULES, CONFIDENCE_FIELD, CallGoal
from app.models import Case, CallType, SupplierContact


def supplier_qualification_goal(case: Case, sc: SupplierContact) -> CallGoal:
    code, desc = case.equipment_hcpcs, case.equipment_desc
    prompt = f"""You are calling {sc.supplier.name}, a Medicare-enrolled DME supplier,
on behalf of a patient (details withheld until needed). Your goal is to
qualify them on five points:
1. Are they currently accepting new Medicare patients?
2. Do they stock a {desc.lower()} (billing code {code})?
3. Do they accept Medicare assignment (approved amount as payment in full)?
4. Do they deliver to the patient's area ({case.patient.address})?
5. What is their earliest delivery window, in business days?

Do NOT place an order on this call — the written order isn't confirmed yet.
If they qualify, tell them you may call back shortly to confirm an order.
If they offer a different/upgraded model instead of {code}, that does NOT count
as stocking {code} — note it and treat {code} as unavailable.
{BASE_RULES}"""
    schema = {
        "type": "object",
        "properties": {
            "answered_meaningfully": {"type": "boolean", "description": "Did we reach someone who could answer?"},
            "accepting_medicare": {"type": ["boolean", "null"]},
            "k0001_in_stock": {"type": ["boolean", "null"],
                               "description": f"The requested equipment ({code}) specifically; "
                                              "a substitute model does not count."},
            "accepts_assignment": {"type": ["boolean", "null"]},
            "serves_address": {"type": ["boolean", "null"]},
            "delivery_window_days": {"type": ["integer", "null"]},
            "disqualify_reason": {"type": "string", "description": "Empty if qualified; else the concrete reason."},
            "other_notes": {"type": "string", "description": "Anything material the schema doesn't cover."},
            **CONFIDENCE_FIELD,
        },
        "required": ["answered_meaningfully", "disqualify_reason", "confidence"],
    }
    return CallGoal(CallType.SUPPLIER_QUALIFICATION, sc.supplier.name, sc.supplier.phone, prompt, schema)


def supplier_confirmation_goal(case: Case, sc: SupplierContact) -> CallGoal:
    order = case.pcp_track.order
    code = order.hcpcs_code if order else case.equipment_hcpcs
    prompt = f"""You are calling {sc.supplier.name} to CONFIRM an order and delivery.
You previously qualified them. Facts you may share:
- Patient: {case.patient.name}, {case.patient.address}
- Equipment: {case.equipment_desc}, billing code {code}
- A signed Standard Written Order from {case.pcp.doctor} is on file and will be sent to them.
- Patient has Original Medicare Part B; this is capped-rental DME so the supplier
  bills Medicare monthly and the patient owes ~20% coinsurance per rental month.

Your goals: (1) confirm they will fulfill this order, (2) get a concrete delivery
date, (3) get a concrete commitment for the next step (e.g. when they'll call to
schedule the delivery window) WITH a due time.
{BASE_RULES}"""
    schema = {
        "type": "object",
        "properties": {
            "answered_meaningfully": {"type": "boolean"},
            "order_confirmed": {"type": "boolean"},
            "delivery_in_days": {"type": ["integer", "null"], "description": "Committed delivery, business days from now."},
            "next_step_promise": {"type": "string", "description": "What they committed to do next, verbatim-ish."},
            "next_step_due_hours": {"type": ["integer", "null"], "description": "Hours from now the promise is due."},
            "other_notes": {"type": "string"},
            **CONFIDENCE_FIELD,
        },
        "required": ["answered_meaningfully", "order_confirmed", "confidence"],
    }
    return CallGoal(CallType.SUPPLIER_CONFIRMATION, sc.supplier.name, sc.supplier.phone, prompt, schema)


def supplier_chase_goal(case: Case, sc: SupplierContact, broken_promise: str) -> CallGoal:
    prompt = f"""You are calling {sc.supplier.name} because they confirmed an order for
{case.patient.name} ({case.equipment_desc}, {case.equipment_hcpcs}) but MISSED a commitment:
"{broken_promise}". Your goal: find out status, re-pin a concrete delivery date
and next step with a due time — or determine they can't be relied on.
{BASE_RULES}"""
    schema = {
        "type": "object",
        "properties": {
            "answered_meaningfully": {"type": "boolean"},
            "still_committed": {"type": ["boolean", "null"]},
            "new_delivery_in_days": {"type": ["integer", "null"]},
            "next_step_promise": {"type": "string"},
            "next_step_due_hours": {"type": ["integer", "null"]},
            "other_notes": {"type": "string"},
            **CONFIDENCE_FIELD,
        },
        "required": ["answered_meaningfully", "confidence"],
    }
    return CallGoal(CallType.SUPPLIER_CHASE, sc.supplier.name, sc.supplier.phone, prompt, schema)
