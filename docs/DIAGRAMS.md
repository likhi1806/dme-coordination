# Diagrams

Visual companion to [DESIGN.md](DESIGN.md). All diagrams are Mermaid (GitHub
renders them inline). They reflect the actual code in `app/`.

---

## 1. System architecture — FSM at the center, LLM only at the edges

The one idea the whole system is built on: **deterministic control flow owns the
workflow; the LLM is confined to the two jobs code can't do** — holding a phone
conversation and turning it into structured facts.

```mermaid
flowchart TB
  subgraph API["Interface layer — FastAPI (app/main.py)"]
    DASH["Dashboard SPA (app/static)"]
    ROUTES["REST: /cases /timeline /calls /escalations"]
  end

  subgraph CTRL["CONTROL PLANE — deterministic, testable, auditable"]
    ORCH["Orchestrator (app/orchestrator.py)<br/>composes app/flows/: supplier · pcp · matching · patient"]
    RULES["rules.py — coverage checklist S1"]
    SCHED["Scheduler — promise & SLA timers (app/scheduler.py)"]
    CLOCK["Clock / SimClock (app/clock.py)"]
    STORE["Store — cases, calls, escalations + event log (app/store.py)"]
    ESC["Escalation queue -> care advocate"]
  end

  subgraph EDGE["EDGE — probabilistic, the ONLY place an LLM runs"]
    ENGINE["VoiceEngine — call pipeline (app/voice/engine.py)"]
    TEL["TelephonyAdapter (interface)"]
    SIM["SimulatedTelephony — LLM vs LLM"]
    OFF["ScriptedEngine — offline, no key"]
    VAPI["VapiTelephony — real PSTN (experimental)"]
    LLM["LLMProvider (interface)"]
    PROV["Anthropic · OpenAI · any OpenAI-compatible gateway"]
  end

  DASH --> ROUTES --> ORCH
  ORCH --> RULES
  ORCH --> SCHED
  SCHED --> CLOCK
  ORCH --> STORE
  ORCH --> ESC
  ORCH -->|"place_call(goal)"| ENGINE
  ENGINE --> TEL
  TEL --> SIM & OFF & VAPI
  ENGINE -->|"extract(transcript, schema)"| LLM
  SIM --> LLM
  LLM --> PROV
  ENGINE -->|"typed CallOutcome + confidence"| ORCH
```

---

## 2. Class diagram — domain model + services

The domain models (`app/models.py`) are the shared type system; the service
classes wire the edges to the FSM. `<<interface>>` marks the swap seams.

```mermaid
classDiagram
  class Case {
    +str id
    +CasePhase phase
    +str equipment_hcpcs
    +PCPOrderTrack pcp_track
    +List~SupplierContact~ suppliers
    +List~Promise~ promises
    +List~Event~ events
    +List~ChecklistItem~ checklist
    +qualified_ranked()
  }
  class SupplierContact {
    +Supplier supplier
    +SupplierState state
    +int attempts
    +bool accepts_assignment
    +int delivery_window_days
  }
  class PCPOrderTrack {
    +PCPOrderState state
    +datetime promised_by
    +int stall_count
    +WrittenOrder order
  }
  class Promise {
    +str who
    +str what
    +datetime due_at
    +bool fulfilled
  }
  class CallRecord {
    +CallType call_type
    +bool answered
    +List~TranscriptTurn~ transcript
    +dict outcome
    +float confidence
  }
  class Escalation {
    +str reason
    +str context
    +bool resolved
  }
  class Event {
    +datetime at
    +str kind
    +str message
  }

  Case "1" --> "*" SupplierContact
  Case "1" --> "1" PCPOrderTrack
  Case "1" --> "*" Promise
  Case "1" --> "*" Event
  Case "1" --> "*" ChecklistItem
  SupplierContact --> Supplier
  PCPOrderTrack --> WrittenOrder

  class Orchestrator {
    +Case case
    +start()
    +run_to_completion()
    +place_call(goal)
    +add_promise() add a timer
    +outcome_ok() confidence gate
    +escalate()
  }
  class SupplierFlow {
    <<mixin app/flows/supplier.py>>
    +h_qualify_suppliers() fan-out
    +qualify_one() gates
  }
  class PCPFlow {
    <<mixin app/flows/pcp.py>>
    +h_request_pcp_order()
    +h_pcp_sla_check() nudge
    +h_inbound_order() validate
  }
  class MatchFlow {
    <<mixin app/flows/matching.py>>
    +check_match_gate()
    +h_confirm_supplier()
    +transmit_swo()
    +h_supplier_promise_check() ghost
  }
  class PatientFlow {
    <<mixin app/flows/patient.py>>
    +h_patient_callback()
  }
  SupplierFlow <|-- Orchestrator
  PCPFlow <|-- Orchestrator
  MatchFlow <|-- Orchestrator
  PatientFlow <|-- Orchestrator
  class VoiceEngine {
    +place_call(goal, now)
  }
  class TelephonyAdapter {
    <<interface>>
    +place_call(goal)
  }
  class LLMProvider {
    <<interface>>
    +chat()
    +extract()
  }
  class Clock {
    <<interface>>
    +now()
  }
  class Store
  class Scheduler
  class CallGoal {
    <<built by app/agents/ per counterparty>>
    +CallType call_type
    +str agent_system_prompt
    +dict outcome_schema
  }

  Orchestrator --> Store
  Orchestrator --> Scheduler
  Orchestrator --> Clock
  Orchestrator --> VoiceEngine
  Orchestrator --> Case
  Orchestrator ..> CallGoal
  VoiceEngine --> TelephonyAdapter
  VoiceEngine --> LLMProvider
  VoiceEngine <|.. ScriptedEngine : offline stand-in (no LLM)
  TelephonyAdapter <|-- SimulatedTelephony
  TelephonyAdapter <|-- VapiTelephony
  LLMProvider <|-- AnthropicProvider
  LLMProvider <|-- OpenAIProvider
  Clock <|-- SimClock
  Clock <|-- RealClock
```

---

## 3. Case state machine (top level)

The whole case as one FSM. `NEEDS_HUMAN` is reachable from any working state —
escalation is a first-class outcome, not an error path.

```mermaid
stateDiagram-v2
  [*] --> INTAKE_COMPLETE
  INTAKE_COMPLETE --> COVERAGE_CHECK
  COVERAGE_CHECK --> COORDINATING : checklist ok
  COVERAGE_CHECK --> NEEDS_HUMAN : not covered
  COORDINATING --> READY_TO_MATCH : order VALID and >=1 supplier QUALIFIED
  note right of COORDINATING : repeated PCP stalls / low confidence\nescalate to advocate (phase unchanged)
  READY_TO_MATCH --> MATCHED : supplier confirms order
  READY_TO_MATCH --> NEEDS_HUMAN : no qualified suppliers left
  MATCHED --> DELIVERY_SCHEDULED : supplier callback, window set
  MATCHED --> READY_TO_MATCH : supplier ghosts, failover
  DELIVERY_SCHEDULED --> DONE : patient informed
  DONE --> [*]
```

---

## 4. Supplier sub-state-machine (one per engaged supplier)

Note the two failure branches that matter most: retry-with-backoff on no-answer,
and `GHOSTED` — which is simply a confirmed supplier whose promise timer expired.

```mermaid
stateDiagram-v2
  [*] --> NOT_CONTACTED
  NOT_CONTACTED --> OUT_OF_AREA : geo prefilter, never called
  NOT_CONTACTED --> CALLING
  CALLING --> NO_ANSWER : no pickup
  NO_ANSWER --> CALLING : redial, backoff, up to 3
  NO_ANSWER --> DISQUALIFIED : unreachable after max retries
  CALLING --> DISQUALIFIED : fails a qualification gate
  CALLING --> QUALIFIED : Medicare + K0001 + area + assignment
  QUALIFIED --> CONFIRMING : chosen at match gate
  CONFIRMING --> CONFIRMED : order confirmed
  CONFIRMING --> DISQUALIFIED : declines to commit
  CONFIRMED --> GHOSTED : promise expired, no answer on chase
  GHOSTED --> [*] : failover to next candidate
  DISQUALIFIED --> [*]
```

---

## 5. PCP written-order sub-state-machine

The two brief failure modes live here: "we never got it" (STALLED → nudge) and
"wrong billing code" (RECEIVED → INVALID → correction call).

```mermaid
stateDiagram-v2
  [*] --> NOT_REQUESTED
  NOT_REQUESTED --> REQUESTED : call placed, promise recorded
  REQUESTED --> STALLED : promised_by passed, nothing arrived
  STALLED --> REQUESTED : nudge call, fresh promise
  note right of STALLED : 3 stalls → escalate to advocate
  REQUESTED --> RECEIVED : written order arrives
  RECEIVED --> VALID : code matches K0001
  RECEIVED --> INVALID : wrong code / unsigned
  INVALID --> REQUESTED : correction call
  VALID --> [*]
```

---

## 6. Sequence — the call pipeline (the core primitive)

Every outbound interaction is this exact shape. Talker and parser are separate
LLM passes; the confidence gate decides whether the FSM may act.

```mermaid
sequenceDiagram
  participant O as Orchestrator (FSM)
  participant E as VoiceEngine
  participant T as TelephonyAdapter
  participant C as Counterparty (LLM persona / real human)
  participant X as LLMProvider (extractor)

  O->>E: place_call(goal, now)
  E->>T: place_call(goal, on_turn)
  loop conversation turns
    T->>C: agent line (production prompt)
    C-->>T: reply
  end
  T-->>E: transcript
  E->>X: extract(transcript, outcome_schema)
  X-->>E: typed CallOutcome + confidence
  E-->>O: CallRecord
  alt confidence >= 0.7
    O->>O: apply typed outcome, advance FSM
  else confidence < 0.7
    O->>O: escalate to advocate, no state change
  end
```

---

## 7. Sequence — Eleanor's case end-to-end (the interesting path)

Shows the concurrency (S2 ∥ S3), the wrong-code bounce, and the
ghost-then-failover.

```mermaid
sequenceDiagram
  autonumber
  participant O as Orchestrator
  participant R as Rules (S1)
  participant S as Suppliers
  participant P as PCP office
  participant Pt as Patient

  O->>R: coverage check (K0001, Part B, no prior-auth)
  R-->>O: checklist — written order still needed

  par Supplier research (S3, parallel)
    O->>S: qualification fan-out
    S-->>O: some out-of-stock / not-taking, several QUALIFIED
  and PCP order chase (S2, critical path)
    O->>P: request Standard Written Order
    P-->>O: promised in 48h
    Note over O,P: promise becomes an SLA timer
    O->>P: nudge (stalled) — office had no record
    P-->>O: order arrives as K0006 (WRONG code)
    O->>P: correction call
    P-->>O: corrected K0001 order — VALID
  end

  O->>O: match gate opens (order VALID + supplier QUALIFIED)
  O->>S: confirm with best candidate
  S-->>O: confirmed, promises to call back
  O->>S: transmit signed written order (tracked as our own promise)
  Note over O,S: callback promise becomes an SLA timer
  O-->>S: timer expires, chase call
  S--xO: no answer — GHOSTED
  O->>O: failover to backup supplier
  O->>S: confirm with backup
  S-->>O: confirmed, delivery scheduled
  O->>Pt: status callback (cost = capped rental, ~20%/month)
  O->>O: phase DONE
```
