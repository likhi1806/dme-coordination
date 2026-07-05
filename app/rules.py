"""S1 — Coverage & authorization: a deterministic rules module, NOT an LLM.

Coverage policy is published, versioned, and testable — exactly the kind of
logic that should never be delegated to a model. In production this module
grows a real eligibility check (HETS 270/271 via a clearinghouse) and a
maintained HCPCS prior-auth list; the shape stays the same."""
from __future__ import annotations

from app.models import Case, ChecklistItem

# HCPCS codes on Medicare's required-prior-authorization list (subset relevant
# to mobility). Standard manual wheelchairs (K0001) are NOT on it; most power
# wheelchairs (K0813+) are.
PRIOR_AUTH_REQUIRED = {"K0813", "K0814", "K0815", "K0816", "K0820", "K0856", "K0861"}

COVERED_MOBILITY_CODES = {"K0001", "K0002", "K0003", "K0004", "K0005", "K0006", "K0007"}


def run_coverage_check(case: Case) -> list[ChecklistItem]:
    items: list[ChecklistItem] = []

    covered = case.equipment_hcpcs in COVERED_MOBILITY_CODES
    items.append(ChecklistItem(
        requirement="Equipment covered under Part B as DME",
        status="met" if covered else "blocked",
        detail=f"{case.equipment_hcpcs} ({case.equipment_desc}) — manual wheelchair benefit, "
               "prescribed for in-home use.",
    ))

    items.append(ChecklistItem(
        requirement="Face-to-face visit with prescriber within 6 months",
        status="met",
        detail="PCP visit completed 3 days before intake; mobility need noted in chart.",
    ))

    items.append(ChecklistItem(
        requirement="Standard Written Order (SWO) signed before delivery",
        status="pending",
        detail="Verbal order in chart is NOT sufficient. Written order must be obtained "
               "from the PCP before any supplier delivers.",
    ))

    needs_pa = case.equipment_hcpcs in PRIOR_AUTH_REQUIRED
    items.append(ChecklistItem(
        requirement="Prior authorization",
        status="pending" if needs_pa else "met",
        detail=("Required for this code — must be submitted before delivery."
                if needs_pa else
                f"{case.equipment_hcpcs} is not on Medicare's required prior-authorization "
                "list (power wheelchairs are; standard manual is not). Checked, not skipped."),
    ))

    items.append(ChecklistItem(
        requirement="Supplier enrolled in Medicare and accepting assignment",
        status="pending",
        detail="Verified per-supplier during qualification calls.",
    ))

    items.append(ChecklistItem(
        requirement="Patient cost share understood",
        status="pending",
        detail="Standard manual wheelchairs are capped-rental DME: Medicare pays the supplier "
               "~80% of the approved amount each month (up to 13 months, after any remaining "
               "Part B deductible); the patient owes ~20% coinsurance per rental month and owns "
               "the chair after month 13. No supplemental plan, so she pays that coinsurance out "
               "of pocket. Supplier bills Medicare directly. Must be explained on a patient call.",
    ))

    return items


def validate_written_order(case: Case) -> tuple[bool, str]:
    """The wrong-code gate: catch claim-poisoning mistakes BEFORE handoff."""
    order = case.pcp_track.order
    if order is None:
        return False, "No order on file."
    if order.hcpcs_code != case.equipment_hcpcs:
        return False, (f"Order specifies {order.hcpcs_code} ({order.description}) but the case "
                       f"requires {case.equipment_hcpcs} ({case.equipment_desc}). A claim billed "
                       "against a mismatched order will be denied — bouncing back to PCP.")
    if not order.signed_by:
        return False, "Order is unsigned."
    return True, "Order matches required equipment and is signed."
