# DESIGN — DME Coordination Engine

## 0. Architecture thesis

> **Deterministic state machine owns control flow. LLMs live only at the edges.**

LLMs do exactly two jobs here, the two jobs code can't do:
1. **Conduct phone conversations** (goal-directed, handles messy humans).
2. **Extract structured outcomes** from those conversations (transcript → typed verdict).

Everything else — what to do next, when to retry, when a promise is broken, when to escalate — is explicit, inspectable, testable Python. A workflow that can stall for a week on a missed callback cannot be steered by "the model felt like it." Timers, gates, and transitions are code; language is the model's job.

```
┌──────────────────────────────────────────────────────────────┐
│                        Orchestrator (loop)                   │
│   pulls due work from Scheduler → dispatches actions →       │
│   applies typed CallOutcome events → advances state machines │
└──────┬────────────────┬──────────────────┬───────────────────┘
       │                │                  │
┌──────▼─────┐   ┌──────▼──────┐   ┌───────▼────────┐
│ Case FSM   │   │ Scheduler   │   │ Escalation Q   │
│ + supplier │   │ (SimClock,  │   │ (advocate      │
│ sub-FSMs   │   │  SLA timers)│   │  inbox)        │
└──────┬─────┘   └─────────────┘   └────────────────┘
       │ actions ("call supplier X with goal G")
┌──────▼───────────────────────────────────────────────┐
│              VoiceEngine (TelephonyAdapter)          │
│  ┌──────────────────────┐   ┌─────────────────────┐  │
│  │ SimulatedTelephony   │   │ VapiTelephony       │  │
│  │ LLM ⇄ LLM roleplay,  │   │ real outbound call, │  │
│  │ scenario injection   │   │ flag-gated          │  │
│  └──────────────────────┘   └─────────────────────┘  │
│        transcript → Extractor (LLM, JSON schema)     │
│        → typed CallOutcome (+ confidence)            │
└──────────────────────────────────────────────────────┘
       │ LLMProvider interface
┌──────▼───────────────┐
│ Anthropic │ OpenAI   │   (env-selected, one interface)
└──────────────────────┘
```

## 1. Domain model (Pydantic)

- `Case` — patient, equipment (HCPCS), PCP, coverage checklist, current phase, event log.
- `SupplierContact` — directory row + sub-FSM state + qualification facts + promises.
- `PCPOrderTrack` — sub-FSM state, promised-by, nudge count, received order payload.
- `CallRecord` — who/why/transcript/outcome/confidence/timestamps.
- `CallOutcome` — the typed result of every call (per call-goal schema: e.g. `SupplierQualification{accepting_medicare, k0001_in_stock, delivery_window_days, accepts_assignment, serves_address, callback_promised_at}`).
- `Escalation` — reason, context bundle, recommended action, resolution.
- `Promise` — anything a counterparty committed to, with a due time. **Promises are the unit of chasing.**

## 2. State machines

### Case (top level)
```
INTAKE_COMPLETE
  → COVERAGE_CHECK            (deterministic, instant)
  → COORDINATING              (S2 + S3 run in parallel)
  → READY_TO_MATCH            (order RECEIVED+VALID ∧ ≥1 supplier QUALIFIED)
  → MATCHED                   (delivery confirmed w/ date)
  → DELIVERY_SCHEDULED
  → DONE
any state → NEEDS_HUMAN       (escalation; resumable)
```

### Supplier sub-FSM (one per directory row engaged)
```
NOT_CONTACTED → CALLING → NO_ANSWER (retry ≤3, backoff)
                        → DISQUALIFIED(reason)
                        → QUALIFIED (ranked)
QUALIFIED → CONFIRMING → CONFIRMED(delivery_date, SLA timer)
CONFIRMED → GHOSTED (SLA breach → 1 chase → failover to next QUALIFIED)
```

### PCP order sub-FSM
```
NOT_REQUESTED → REQUESTED(promised_by) → RECEIVED → VALID | INVALID(reason → re-request)
REQUESTED --promised_by passes--> STALLED → nudge call → REQUESTED'
STALLED ×2 → escalate
"never got it" on nudge → re-send path, log, shorter SLA
```

### Patient track
Milestone-triggered callbacks (coverage confirmed / matched / delivery date), each explains status + next step + cost share. `UNREACHABLE ×2 → escalate`.

## 3. The call pipeline (core primitive)

Every outbound interaction is the same shape:

```
CallGoal (typed) ──> VoiceEngine.place_call(contact, goal)
                       └─ conversation loop (agent LLM ⇄ counterparty)
                     ──> Transcript
                     ──> Extractor.extract(transcript, goal.outcome_schema)
                     ──> CallOutcome (typed, + confidence 0–1)
confidence < 0.7 ──> Escalation instead of state transition
```

- **Agent side:** system prompt = role + case facts + call goal + hard rules (never invent clinical info, always get a concrete date/time for any promise, confirm billing code by name).
- **Simulated counterparty:** system prompt = persona + **hidden scenario** (e.g. "you are out of stock on K0001, offer K0002 upgrade", "promise delivery Thursday then never answer again"). Scenarios come from `data/scenarios.yaml` so the demo is deterministic and failure modes are guaranteed to appear.
- **Extraction:** second LLM pass with a strict JSON schema (tool-use / structured output). Extraction is a *separate* call from conversation — a talker and a parser have different jobs and different prompts.

## 4. Scheduler & time

- `Clock` interface: `RealClock` and `SimClock` (demo: 1 real second ≈ hours; or fully event-stepped).
- Scheduler holds `(due_time, action)` items: retry-no-answer, nudge-PCP, SLA-check-on-promise, patient-callback. Orchestrator ticks: pop due items → dispatch.
- **Every promise made on any call auto-creates an SLA timer.** Ghost detection is just "timer fired and state didn't advance."

## 5. API surface (FastAPI)

| Route | Purpose |
|---|---|
| `POST /cases/{id}/run` | advance the case (or `demo.py` drives ticks) |
| `GET /cases/{id}` | full case state: phase, sub-FSMs, checklist |
| `GET /cases/{id}/timeline` | event log — the demo centerpiece |
| `GET /cases/{id}/calls/{call_id}` | transcript + extracted outcome |
| `GET /escalations` / `POST /escalations/{id}/resolve` | advocate queue |
| `GET /` | single-page dashboard (vanilla HTML/JS, polls state) |

Store: in-memory dict (per constraints). Interface named `Store` so Postgres slots in later.

## 6. Module layout

```
app/
  config.py            # env: LLM_PROVIDER, VOICE_PROVIDER, keys, policy knobs
  models.py            # all Pydantic domain models + enums
  store.py             # in-memory store (Store interface → Postgres later)
  clock.py             # Clock / SimClock (starts at real now; tests pin a date)
  scheduler.py         # due-work queue: promise → timer
  rules.py             # S1 coverage checklist (deterministic)
  bootstrap.py         # wiring: case, engine, world bridge, mode selection
  orchestrator.py      # shared plumbing + start + run loop; composes the flows
  flows/               # the four coordination surfaces, one module each
    supplier.py        #   S3: qualification fan-out, retry/backoff, gates
    pcp.py             #   S2: order request, SLA nudges, validate on arrival
    matching.py        #   S4: match gate, confirm, SWO transmit, ghost→failover
    patient.py         #   S5: milestone callbacks, bounded retries
  agents/              # call prompts + outcome schemas, per counterparty
    base.py            #   CallGoal, shared hard rules, confidence field
    supplier_goals.py · pcp_goals.py · patient_goals.py
    goals.py           #   facade (stable import surface)
  llm/provider.py      # LLMProvider ABC + Anthropic/OpenAI (+ gateway via env)
  voice/
    engine.py          # place_call pipeline (conversation + extraction)
    telephony.py       # TelephonyAdapter ABC + SimulatedTelephony (LLM⇄LLM)
    offline.py         # scripted no-LLM engine (tests + zero-key demo)
    vapi.py            # experimental real-call sketch (unexercised seam)
    personas.py        # scenario loading for the simulated world
  main.py              # FastAPI app; static/dashboard.html is the UI
demo.py                # terminal end-to-end run (--hard for the adversarial pack)
evals/run_evals.py     # extraction accuracy vs persona ground truth
tests/                 # test_flow.py (FSM, no LLM) · test_voice.py (msg contract)
data/
  sample-supplier-directory.csv
  scenarios.yaml       # hidden per-supplier scripts → deterministic demo
  scenarios_hard.yaml  # adversarial pack: everything fails, case escalates
```

## 7. Key tradeoffs

| Decision | Chosen | Rejected | Why (short) |
|---|---|---|---|
| Control flow | Explicit FSM | Autonomous agent loop | Auditability, testability, healthcare stakes; stalls need timers not vibes |
| Voice | Simulated LLM⇄LLM + Vapi flag | Full real telephony | 3h box; demo determinism; adapter keeps the door open |
| Extraction | Separate LLM pass, JSON schema | Parse during conversation | Separation of concerns; retryable; confidence-gated |
| Concurrency | asyncio fan-out for supplier calls | Sequential | Mirrors reality (parallel dialing) & demo speed |
| Persistence | In-memory + event log | SQLite/Postgres | Constraint says skip; `Store` interface preserves the swap |
| LLM | Provider ABC (Anthropic/OpenAI) | Single vendor | Cheap insurance + talking point; one file of cost |
| Demo | Scripted scenarios.yaml | Random failures | A demo that *guarantees* showing ghost-detection beats one that might |
