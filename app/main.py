"""FastAPI surface: case state, timeline, transcripts, escalation queue,
and the dashboard. `POST /demo/start` runs the whole case as a background
task while the dashboard polls; `pace` controls real-seconds between steps
so a live audience can watch the case unfold."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from app import bootstrap, config
from app.llm import provider

app = FastAPI(title="DME Coordination Engine")

STATIC = Path(__file__).parent / "static"
DEFAULT_PACE = float(os.getenv("DEMO_STEP_DELAY", "0.9"))

_orch: Optional[object] = None
_task: Optional[asyncio.Task] = None


def orch():
    global _orch
    if _orch is None:
        _orch = bootstrap.build()
    return _orch


@app.post("/demo/start")
async def start_demo(pace: float = DEFAULT_PACE, mode: str = "happy"):
    global _task, _orch
    if _task and not _task.done():
        return {"status": "already running"}
    _orch = bootstrap.build(mode=mode)  # fresh world each run
    _orch.start()
    _task = asyncio.create_task(_orch.run_to_completion(step_delay=pace))
    return {"status": "started", "pace": pace, "mode": mode}


# Read endpoints are async so they share the event loop with the demo task —
# a sync def runs in a threadpool and can iterate store dicts while the demo
# task mutates them (RuntimeError: dict changed size), exactly during polling.
@app.get("/cases/current")
async def get_case():
    o = orch()
    c = o.case
    open_promises = [p for p in c.promises if not p.fulfilled]
    return {
        "id": c.id, "phase": c.phase,
        "patient": c.patient, "pcp": c.pcp,
        "equipment": c.equipment_desc, "hcpcs": c.equipment_hcpcs,
        "sim_time": o.clock.now().isoformat(),
        "checklist": c.checklist,
        "pcp_track": {
            "state": c.pcp_track.state,
            "promised_by": c.pcp_track.promised_by.isoformat() if c.pcp_track.promised_by else None,
            "stall_count": c.pcp_track.stall_count,
            "attempts": c.pcp_track.request_attempts,
            "order": c.pcp_track.order,
            "invalid_reason": c.pcp_track.invalid_reason,
            "call_ids": c.pcp_track.call_ids,
        },
        "suppliers": [
            {"name": s.supplier.name, "phone": s.supplier.phone,
             "address": s.supplier.address, "state": s.state,
             "attempts": s.attempts, "delivery_days": s.delivery_window_days,
             "reason": s.disqualify_reason,
             "medicare": s.accepting_medicare, "stock": s.k0001_in_stock,
             "assignment": s.accepts_assignment,
             "delivery_date": s.confirmed_delivery_date.isoformat() if s.confirmed_delivery_date else None,
             "matched": s.supplier.id == c.matched_supplier_id,
             "call_ids": s.call_ids}
            for s in c.suppliers
        ],
        "promises": [
            {"who": p.who, "what": p.what, "due_at": p.due_at.isoformat(),
             "fulfilled": p.fulfilled}
            for p in c.promises
        ],
        "stats": {
            "calls": len(o.store.calls),
            "answered": sum(1 for r in o.store.calls.values() if r.answered),
            "qualified": sum(1 for s in c.suppliers if s.state.value in ("QUALIFIED", "CONFIRMED")),
            "open_promises": len(open_promises),
            "escalations": len(o.store.escalations),
            "sim_days": max(0, (o.clock.now() - c.events[0].at).days) if c.events else 0,
            "llm_calls": provider.USAGE["llm_calls"],
            "llm_tokens": provider.USAGE["input_tokens"] + provider.USAGE["output_tokens"],
            "llm_cost_usd": round(provider.estimated_cost_usd(), 3),
        },
        "running": bool(_task and not _task.done()),
        "voice_provider": config.VOICE_PROVIDER,
        "active_calls": list(getattr(o, "active_calls", [])),
        "next_action": (lambda p: {"action": p[0].action, "due_at": p[0].due_at.isoformat()}
                        if p else None)(o.scheduler.pending()),
    }


@app.get("/cases/current/timeline")
async def timeline():
    return [{"at": e.at.isoformat(), "kind": e.kind, "message": e.message, "data": e.data}
            for e in orch().case.events]


@app.get("/calls/{call_id}")
async def call(call_id: str):
    rec = orch().store.get_call(call_id)
    if not rec:
        raise HTTPException(404)
    return rec


@app.get("/calls")
async def calls():
    return [{"id": r.id, "type": r.call_type, "counterparty": r.counterparty,
             "answered": r.answered, "confidence": r.confidence, "at": r.at.isoformat()}
            for r in orch().store.calls.values()]


@app.get("/escalations")
async def escalations():
    return orch().store.escalations


@app.post("/escalations/{esc_id}/resolve")
def resolve(esc_id: str, resolution: str = "Reviewed by advocate"):
    for e in orch().store.escalations:
        if e.id == esc_id:
            e.resolved, e.resolution = True, resolution
            return e
    raise HTTPException(404)


@app.get("/")
def dashboard():
    return FileResponse(STATIC / "dashboard.html")
