# DME Back-End Coordination Engine

Automates the care-advocate coordination loop for Medicare DME: supplier
qualification calls, PCP written-order chasing, coverage checks, ghost detection
with automatic failover, and patient status callbacks.

**Architecture in one line:** a deterministic state machine owns the workflow;
LLMs only (1) conduct the phone conversations and (2) extract typed outcomes
from transcripts — they never choose what happens next.

---

## ▶ Run it (≈60 seconds, no API key needed)

```bash
make setup       # create venv + install deps  (Python 3.9+; uses uv if present)
make dashboard   # then open http://localhost:8000  →  press "▶ Run Eleanor's case"
```

With **no API key** the system runs in **offline mode** — scripted but realistic
conversations drive the *exact same* state machine, timers, and failure handling.
So it works on a fresh clone with zero config. (Add a key for real LLM calls —
see [Level 2](#level-2--real-llm-conversations-optional).)

All entry points:

| Command | What it does |
|---|---|
| `make dashboard` | Live dashboard at **http://localhost:8000** — the main way to watch a case |
| `make demo` | Same case in the terminal; streams a timestamped timeline + a summary line |
| `make demo-hard` | Adversarial run: every supplier fails → case escalates to a human |
| `make test` | Zero-LLM end-to-end tests (happy + hard mode) — ~1s, no network, no key |
| `make evals` | Extraction-accuracy evals (needs an LLM key — see Level 2) |
| `make help` | List targets |

---

## 👀 What you'll see — and where to check it

Open the dashboard and press **▶ Run Eleanor's case**. The screen answers
three questions at a glance:

- **Top band — "what's happening *now*":** while a call is live, a card streams
  the actual conversation turn-by-turn; between calls it shows the simulated
  clock fast-forwarding to the next scheduled step.
- **Center — the timeline ("what it has *done*"):** every action as it happens,
  color-coded (calls / doctor's office / suppliers / matching / patient /
  needs-a-human). **Click any blue call** to open its transcript **plus the
  structured data extracted from it and a confidence score.**
- **Left — the case:** patient/coverage facts, the coverage **checklist** (turns
  green as each Medicare requirement is met), and **Promises → Timers** (every
  commitment becomes a timer — watch one go overdue).
- **Right — suppliers & escalations:** the supplier board
  (QUALIFIED / DISQUALIFIED / GHOSTED / CONFIRMED, each with *why*) and the
  **escalation queue** (anything handed to a human, with full context).

**Three moments worth watching for (the demo's point):**
1. **Ghost → failover** — a supplier confirms, its callback-promise timer
   expires, the system chases it, then auto-fails-over to the backup.
   *A ghost is just an expired promise.*
2. **Wrong-code bounce** — the doctor's office sends an order coded **K0006**;
   the validation gate catches the mismatch and bounces it back for a corrected
   **K0001** before it can poison the insurance claim.
3. **Hard mode** — flip the **Storyline** dropdown to *"hard mode"* and rerun:
   every supplier fails a different way and the case parks safely in
   **NEEDS_HUMAN** with the advocate queue populated. It never fails silently.

**In the terminal** (`make demo`): the same case prints a timestamped timeline
as it runs and ends with a summary — final phase, calls placed, escalations,
matched supplier, and (in LLM mode) the estimated cost. Add `make demo-hard` for
the adversarial version.

---

## Level 2 — real LLM conversations (optional)

```bash
cp .env.example .env       # add a key (see below), then:
make demo                  # each call is now a live LLM⇄LLM conversation
make evals                 # + randomized personas scored against known ground truth
```

`.env` accepts `ANTHROPIC_API_KEY` **or** `OPENAI_API_KEY`. For an
OpenAI-compatible **gateway**, also set `OPENAI_BASE_URL` and `OPENAI_MODEL`;
no code change needed. Our agent
then runs its production prompts against an LLM persona with a hidden scenario
script (`data/scenarios.yaml`) — unscripted phrasing, real extraction.

## Level 3 — a real phone call (Vapi, experimental & unexercised)

`app/voice/vapi.py` sketches a real-PSTN implementation of the same
`TelephonyAdapter` interface (`VOICE_PROVIDER=vapi`, `VAPI_*` keys,
`MAX_SUPPLIERS_TO_CONTACT=1`). **The claim here is the adapter seam, not
this implementation** — the orchestrator only sees `place_call(goal) ->
CallResult`, so a real provider drops in without touching coordination logic.
It has *not* been run against the live Vapi API; treat it as the drop-in point,
not a tested feature.

---

## Verification status (what has actually run)

| Component | Status |
|---|---|
| FSM / orchestration (`tests/test_flow.py`, happy + hard) | ✅ run, passing |
| Voice threading + extraction contract (`tests/test_voice.py`) | ✅ run, passing (fake LLM) |
| Offline demo + dashboard (`make demo` / `make dashboard`) | ✅ run |
| Live LLM simulated calls (`VOICE_PROVIDER=simulated`) | ✅ verified (gpt-4o-mini via an OpenAI-compatible gateway): full case → `DONE`, 20 calls, ~$0.014; the confidence gate escalated one 0.60-confidence extraction to a human, as designed |
| Eval harness (`make evals`) | ✅ verified: 100% field accuracy over 10 personas, ~$0.01 |
| Vapi real call (`VOICE_PROVIDER=vapi`) | ⚠️ unexercised — adapter seam only |

## What's mocked (explicitly)

| Mock | Real-world counterpart |
|---|---|
| LLM persona on the far side of calls | The human supplier rep / front desk / patient |
| `SimClock` (days → seconds) | Real time + durable timers (Temporal / job queue) |
| `scenarios.yaml` scripted failures | Actual flaky humans (scripted so the demo deterministically shows every failure mode) |
| Inbound `inbound_order` event | Fax/portal intake of the signed written order |
| Coverage rules module | HETS 270/271 eligibility + maintained HCPCS policy tables |
| In-memory `Store` | Postgres (interface already isolates it) |

## Docs

- [`docs/WRITEUP.md`](docs/WRITEUP.md) — sequencing / stack / cut list / what's next (the 1–2 page writeup)
- [`docs/DIAGRAMS.md`](docs/DIAGRAMS.md) — architecture, class, state-machine & sequence diagrams (Mermaid)
- [`docs/DESIGN.md`](docs/DESIGN.md) — architecture & tradeoffs in depth

## Code tour — if you only read three things

1. **`app/orchestrator.py`** — the state machine: promises become timers, a
   ghost is an expired promise, escalation is a first-class outcome.
2. **`tests/test_flow.py`** — the whole multi-day workflow (retries, stalls,
   wrong-code bounce, ghost→failover, hard mode) proven **with no LLM in the loop.**
3. **`app/voice/engine.py` + `app/agents/goals.py`** — the two-pass call
   pipeline (talk, then extract to a typed schema with a confidence gate).

## How it works (60 seconds)

```
Orchestrator (deterministic FSM)  ← the only thing that decides what happens next
  ├─ Coverage rules (S1): Part B / K0001 / prior-auth checklist — pure code
  ├─ Scheduler: every promise from every call becomes a timer;
  │             ghost detection = "timer fired, state didn't advance"
  ├─ Match gate: written order VALID  ∧  ≥1 supplier QUALIFIED
  └─ Escalation queue: low confidence / repeated stalls → a human, with context

Every outbound interaction is one pipeline:
  CallGoal → conversation (LLM talker) → transcript
           → extraction (LLM parser, schema-forced, temp 0) → typed CallOutcome
           → confidence < 0.7 ? escalate : apply to FSM
```
LLMs talk and parse; they never steer — which is why the whole workflow is
testable with no model in the loop (`make test`).

## Repo map
```
app/orchestrator.py   # shared plumbing + run loop; composes the four flows
app/flows/            # one module per surface: supplier, pcp, matching, patient
app/agents/           # call prompts + outcome schemas, per counterparty
app/voice/            # TelephonyAdapter: simulated (LLM⇄LLM), offline, Vapi
app/rules.py          # deterministic Medicare coverage checklist (S1)
app/scheduler.py      # promise → timer; ghost = expired promise
data/scenarios*.yaml  # hidden world scripts (only the simulation reads them)
evals/run_evals.py    # extraction accuracy vs persona ground truth
tests/                # test_flow.py (FSM, no LLM) · test_voice.py (call contract)
```
