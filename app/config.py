"""Central config. Everything env-driven, nothing hidden."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# --- LLM provider selection -------------------------------------------------
# "anthropic" | "openai". Defaults to whichever key is present (anthropic wins).
LLM_PROVIDER = os.getenv(
    "LLM_PROVIDER",
    "anthropic" if os.getenv("ANTHROPIC_API_KEY") else "openai",
)
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# --- Voice / telephony -------------------------------------------------------
# "simulated" (LLM⇄LLM calls) | "offline" (scripted, zero-key) | "vapi" (real call)
# Default is chosen by what can actually run: no LLM key on a fresh clone →
# offline mode, so `make demo` works out of the box instead of crashing mid-run.
HAS_LLM_KEY = bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY"))
VOICE_PROVIDER = os.getenv("VOICE_PROVIDER", "simulated" if HAS_LLM_KEY else "offline")
VAPI_API_KEY = os.getenv("VAPI_API_KEY", "")
VAPI_PHONE_NUMBER_ID = os.getenv("VAPI_PHONE_NUMBER_ID", "")
# In vapi mode every counterparty rings THIS number (you roleplay the supplier).
VAPI_TARGET_NUMBER = os.getenv("VAPI_TARGET_NUMBER", "")

# --- Workflow policy knobs ---------------------------------------------------
MAX_SUPPLIERS_TO_CONTACT = int(os.getenv("MAX_SUPPLIERS_TO_CONTACT", "6"))
NO_ANSWER_MAX_RETRIES = 3
NO_ANSWER_RETRY_HOURS = 4          # backoff between redial attempts
PCP_PROMISE_GRACE_HOURS = 6        # grace after a promised-by before we call it stalled
PCP_MAX_STALLS_BEFORE_ESCALATION = 3
CONFIDENCE_THRESHOLD = 0.7         # extraction confidence below this -> escalation
MAX_CONVERSATION_TURNS = 8         # per side, per simulated call

# Naive geo prefilter: cities we consider deliverable to a Chicago patient.
# Production: geocode + drive-time radius. This is deliberately simple.
SERVICE_AREA_CITIES = {"chicago", "evanston", "skokie", "oak park", "berwyn", "cicero"}
