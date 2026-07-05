# Writeup — DME Back-End Coordination

A quick note on scope: the 3-hour box covered the core — the state machine,
the call pipeline, the scenario simulation, and passing end-to-end tests. The
dashboard, hard-mode scenario, evals, and cost tracking came after the box.
What has and hasn't actually executed is in the README's verification table;
the live-LLM path has run end to end (full case to `DONE` for ~$0.014,
extraction 100% on the eval personas).

**What it does.** Runs Eleanor's case end to end: coverage check, parallel
supplier qualification calls, chasing the PCP's written order (nudging when it
stalls), validating the order on arrival, confirming with the best supplier,
detecting ghosts and failing over to a backup, and calling the patient at each
milestone. A care advocate supervises through an escalation queue instead of
chasing anyone by hand.

**The design bet.** A deterministic state machine owns the workflow; the LLM
is confined to the two jobs software genuinely can't do — holding a phone
conversation with a messy human, and turning the transcript into structured
facts. Everything else (retries, SLA timers, the match gate, failover,
escalation policy) is plain, testable Python. Where a missed callback silently
costs a patient a week, I don't want a model deciding what happens next; I
want it reporting what happened, and code deciding.

The core primitive is the **promise**: any commitment made on any call gets a
due time and becomes a timer. A ghost is just an expired promise. All five
failure modes in the brief reduce to promise-tracking plus three policies:
retry, nudge, fail over.

## Sequencing — and how I decided

I ordered the build by the case's dependency graph rather than by the four
surfaces in the brief:

1. Domain model, state machines, coverage rules (S1) first — coverage gates
   everything and needs no phone calls, and the FSM is the skeleton.
2. The call pipeline second (goal → conversation → transcript → typed
   outcome), because supplier, PCP, and patient calls are all the same shape.
   Build the primitive once instead of three surfaces separately.
3. PCP chase (S2) and supplier research (S3) run concurrently — the written
   order is the days-long critical path, suppliers are parallelizable — and
   they join at the match gate.
4. Match/ghost/failover and patient callbacks (S4) last; they only matter once
   that join point exists.

## Technology & architecture

- **Python + FastAPI + Pydantic.** Pydantic is the contract
  between probabilistic output and deterministic state. Every extraction must
  validate into a typed object before it can touch the FSM.
- **Two-pass calls.** A conversation model pursues the call goal; a separate
  temperature-0 pass extracts schema-constrained JSON (forced tool-use) with a
  confidence score. Under 0.7 the system escalates instead of acting, and the
  stored transcript can be re-extracted without calling anyone back.
- **Voice behind an adapter.** `SimulatedTelephony` runs each call as
  LLM-vs-LLM — my production prompt against a persona with a hidden script the
  orchestrator never sees. A flag-gated Vapi adapter sketches the real-PSTN
  drop-in (unexercised; the seam is the claim, not that file).
- **Structure.** The four coordination surfaces live in `app/flows/`, call
  prompts per counterparty in `app/agents/`; the orchestrator composes them and
  owns the shared plumbing. The engine is case-parameterized — swap the
  equipment code and the same flows run (a test proves a prior-auth power
  wheelchair fails closed to a human before any call is placed).
- **Clock and scheduler are seams.** Multi-day timers fire in demo-seconds;
  the event-sourced state and small interfaces let Postgres and durable timers
  slot in without touching the flows.
- **Tested without LLMs.** `tests/test_flow.py` proves the whole multi-day
  workflow — happy, hard mode, alternate equipment — deterministically;
  `tests/test_voice.py` enforces the provider message contract offline.

## How AI is used inside the product

Exactly two places, both at the edges:

1. **Conversation.** Each call is a goal-directed dialogue: qualify this
   supplier, get the order re-sent, explain cost to the patient. The prompt
   carries hard rules — never invent clinical info; pin every commitment to a
   concrete time (that's what feeds the timers).
2. **Extraction.** A second pass reads the finished transcript into typed
   fields plus a confidence score. On the first real
   run, one terse supplier call extracted at 0.60 and the system escalated to
   a human rather than guessing — exactly the intended behavior.

The LLM never picks the next step, never schedules, never escalates on its
own. Its job is to convert unstructured speech into structured state, or admit
it can't. The simulated personas double as an eval set — their hidden facts are
ground-truth labels, so extraction accuracy is a measured number (100% across
10 randomized personas), not an impression. I used AI coding tools while
building, as the brief invites; the design calls above, the failure policies,
and what got cut were mine.

## The cut list

- **Real telephony as the default path.** The suppliers are fictional and a
  live call is demo risk; the adapter keeps the door open.
- **Persistence, auth, multi-case.** Skipped per the constraints; the `Store`
  interface and append-only event log make Postgres a swap, not a rewrite.
- **Real eligibility (HETS 270/271), fax intake, e-prescribe.** Stubbed as a
  rules module and generic inbound events, named so the seams are visible.
- **Voice-layer hardening** (STT/TTS, barge-in, voicemail detection) — not
  relevant until real telephony is the primary path.
- **Eval breadth.** The harness runs but covers supplier qualification only.

## What's next

**With one more day** (in priority order):
1. **Postgres behind `Store`** — a days-long workflow that loses state on
   restart isn't a product; durability unblocks everything else.
2. **One real call end to end** through the Vapi adapter (webhooks, plus an
   inbound callback) — retires the biggest unknown, the audio layer.
3. **Idempotent event intake** — webhooks re-deliver and retries re-fire; a
   dedup key per event so nobody gets double-dialed.
4. **Widen the evals** to PCP and patient call types, plus a
   confidence-calibration check for the 0.7 threshold.

**With two weeks:**
1. **Durable execution** (Temporal or an outbox + workers) — the promise
   primitive maps directly onto durable timers that survive deploys.
2. **Real integrations, in dependency order:** eligibility via a clearinghouse
   (270/271), e-fax intake for the written order, directory refresh.
3. **The advocate console as a real work queue:** multi-case view, "which case
   stalls next" prioritization, actionable escalations.
4. **Compliance before real patients:** BAA-covered endpoints, minimum PHI in
   prompts, consent + AI-disclosure on outbound calls, audit log.
5. **Evals in CI** so prompt changes can't silently regress extraction.

**Why this order:** durability first, because nothing matters if state dies;
then make one call real — the riskiest unproven layer; then widen (each
integration removes a mock); then harden for the humans and the regulators.
