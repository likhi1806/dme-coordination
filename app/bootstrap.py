"""Wiring: build the whole system for Eleanor's case.

The world_bridge lives HERE (demo wiring), not in the orchestrator: it converts
simulated-scenario side effects into the same inbound events production would
receive from fax intake / supplier callbacks."""
from __future__ import annotations

import csv
from datetime import timedelta

from app import config
from app.clock import SimClock
from app.llm.provider import get_provider
from app.models import Case, Patient, PCP, Supplier, SupplierContact
from app.orchestrator import Orchestrator
from app.scheduler import ScheduledAction, Scheduler
from app.store import Store
from app.voice.engine import VoiceEngine
from app.voice.personas import ScenarioWorld
from app.voice.telephony import get_telephony


def load_suppliers() -> list[SupplierContact]:
    with open(config.DATA_DIR / "sample-supplier-directory.csv") as f:
        return [SupplierContact(supplier=Supplier(**row)) for row in csv.DictReader(f)]


def eleanor_case() -> Case:
    return Case(
        patient=Patient(name="Eleanor Martinez", age=72),
        pcp=PCP(doctor="Dr. Sarah Chen", practice="Sunrise Family Medicine",
                phone="(312) 555-0198"),
        suppliers=load_suppliers(),
    )


def world_bridge(effects, clock, scheduler: Scheduler) -> None:
    """Scenario effect -> inbound event (the shape a webhook would produce)."""
    for e in effects:
        if "deliver_order_after_hours" in e:
            scheduler.schedule(ScheduledAction(
                due_at=clock.now() + timedelta(hours=e["deliver_order_after_hours"]),
                action="inbound_order", data=dict(e["order"])))
        if "supplier_callback_after_hours" in e:
            scheduler.schedule(ScheduledAction(
                due_at=clock.now() + timedelta(hours=e["supplier_callback_after_hours"]),
                action="inbound_supplier_callback",
                data={"supplier_name": e["counterparty"]}))


def build(mode: str = "happy") -> Orchestrator:
    """mode: 'happy' (default demo) | 'hard' (adversarial pack — everything
    fails, case must degrade gracefully into the advocate queue)."""
    store = Store()
    clock = SimClock()
    scheduler = Scheduler()
    if config.VOICE_PROVIDER == "offline":
        # No-LLM fallback: scripted outcomes, same FSM. Demo survives a dead API.
        from app.voice.offline import SCRIPTS, ScriptedEngine
        engine = ScriptedEngine(SCRIPTS.get(mode))
    else:
        from app.llm.provider import reset_usage
        reset_usage()  # per-case cost tracking
        llm = get_provider()
        scenario_file = config.DATA_DIR / ("scenarios_hard.yaml" if mode == "hard"
                                           else "scenarios.yaml")
        world = ScenarioWorld(scenario_file)
        engine = VoiceEngine(get_telephony(llm, world), llm)
    return Orchestrator(store, clock, scheduler, engine, eleanor_case(),
                        world_bridge=world_bridge)
