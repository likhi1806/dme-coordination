"""End-to-end orchestration test with a STUBBED voice engine — no LLM, no network.

This is the proof of the architecture thesis: because the FSM owns control flow
and LLMs only produce typed CallOutcomes, the entire multi-day workflow —
retries, stalls, wrong-code bounce, ghost detection, failover — is verifiable
deterministically in milliseconds.

Run:  .venv/bin/python -m pytest tests/ -q     (or: python tests/test_flow.py)
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bootstrap import eleanor_case, world_bridge
from app.clock import SimClock
from app.models import CasePhase, PCPOrderState, SupplierState
from app.orchestrator import Orchestrator
from app.scheduler import Scheduler
from app.store import Store
from app.voice.offline import SCRIPT_HARD, ScriptedEngine


async def run_case(script=None):
    # fixed start → deterministic dates in assertions/output
    store, clock, sched = Store(), SimClock(datetime(2026, 7, 4, 9, 0)), Scheduler()
    orch = Orchestrator(store, clock, sched, ScriptedEngine(script), eleanor_case(),
                        world_bridge=world_bridge)
    orch.start()
    await orch.run_to_completion()
    return orch


def test_full_flow():
    orch = asyncio.run(run_case())
    case, store = orch.case, orch.store
    by_name = {s.supplier.name: s for s in case.suppliers}

    # Case completes.
    assert case.phase == CasePhase.DONE, case.phase

    # Geo prefilter excluded far suppliers without a single call.
    assert by_name["Badger State Home Medical"].state == SupplierState.OUT_OF_AREA
    assert by_name["Central Illinois Medical Depot"].state == SupplierState.OUT_OF_AREA

    # Failure modes all exercised:
    assert by_name["Lakeshore Home Health Equipment"].state == SupplierState.DISQUALIFIED
    assert by_name["Prairie DME Partners"].state == SupplierState.DISQUALIFIED
    assert by_name["Windy City Medical Supply"].state == SupplierState.QUALIFIED  # after 2 no-answers
    assert by_name["Windy City Medical Supply"].attempts == 3
    assert by_name["Chicago Mobility Solutions"].state == SupplierState.GHOSTED   # confirmed then vanished

    # Failover landed on the reliable backup.
    matched = case.supplier_by_id(case.matched_supplier_id)
    assert matched and matched.supplier.name == "Great Lakes Medical Equipment"

    # PCP path: stall detected, wrong code bounced, corrected order validated.
    assert case.pcp_track.state == PCPOrderState.VALID
    assert case.pcp_track.order.hcpcs_code == "K0001"
    assert case.pcp_track.stall_count >= 1
    assert any("NO RECORD" in e.message for e in case.events)          # fell-in-a-hole
    assert any("INVALID" in e.message for e in case.events)            # wrong-code gate
    assert any("GHOSTED" in e.message for e in case.events)            # ghost detection

    # Ghosting produced an advocate escalation.
    assert any("ghosted" in e.reason.lower() for e in store.escalations)

    # Patient was kept in the loop, incl. cost explanation.
    assert any(c.requirement.startswith("Patient cost share") and c.status == "met"
               for c in case.checklist)

    # Multi-day workflow: sim time actually advanced days, not seconds.
    assert (orch.now() - case.events[0].at).days >= 3

    print(f"PASS — phase={case.phase.value}, calls={len(store.calls)}, "
          f"escalations={len(store.escalations)}, "
          f"sim span={(orch.now() - case.events[0].at)}")


def test_hard_mode_degrades_gracefully():
    """Adversarial pack: every supplier fails. The case must NOT succeed —
    it must park itself in NEEDS_HUMAN with the advocate queue populated,
    proving the FSM handles failure compositions beyond the demo script."""
    orch = asyncio.run(run_case(SCRIPT_HARD))
    case, store = orch.case, orch.store
    by_name = {s.supplier.name: s for s in case.suppliers}

    assert case.phase == CasePhase.NEEDS_HUMAN, case.phase
    # The PCP path still succeeded — failure is isolated to the supplier surface.
    assert case.pcp_track.state == PCPOrderState.VALID
    # Every contacted supplier ended in a terminal failure state.
    assert by_name["Windy City Medical Supply"].disqualify_reason.startswith("Unreachable")
    assert by_name["Great Lakes Medical Equipment"].state == SupplierState.GHOSTED
    assert by_name["South Side Medical Supply Co"].disqualify_reason == "Failed to confirm order"
    assert not case.qualified_ranked()
    # Advocate queue has the terminal escalation + the ghost + unreachable patient.
    reasons = " | ".join(e.reason for e in store.escalations)
    assert "No qualified suppliers remain" in reasons
    assert "ghosted" in reasons.lower()
    assert "unreachable" in reasons.lower()

    print(f"PASS (hard) — phase={case.phase.value}, calls={len(store.calls)}, "
          f"escalations={len(store.escalations)}")


def test_other_case_prior_auth_fails_closed():
    """The engine is case-agnostic: swap the equipment and the same code runs.
    A power wheelchair (prior-auth required, not on the covered manual list)
    must fail CLOSED at the coverage gate — parked in NEEDS_HUMAN before a
    single call is placed, not silently sailed through."""
    case = eleanor_case()
    case.equipment_hcpcs = "K0856"
    case.equipment_desc = "Power wheelchair"

    store, clock, sched = Store(), SimClock(datetime(2026, 7, 4, 9, 0)), Scheduler()
    orch = Orchestrator(store, clock, sched, ScriptedEngine(), case,
                        world_bridge=world_bridge)
    orch.start()

    assert case.phase == CasePhase.NEEDS_HUMAN, case.phase
    assert len(store.calls) == 0                       # no calls before coverage clears
    assert any("Coverage check failed" in e.reason for e in store.escalations)
    print("PASS (other case) — K0856 power wheelchair fails closed to NEEDS_HUMAN, 0 calls")


if __name__ == "__main__":
    test_full_flow()
    test_hard_mode_degrades_gracefully()
    test_other_case_prior_auth_fails_closed()
    print("\n--- timeline ---")
    orch = asyncio.run(run_case())
    for e in orch.case.events:
        print(f"[{e.at:%b %d %H:%M}] {e.kind:<10} {e.message}")
