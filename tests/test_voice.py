"""Voice-layer smoke test — the test that WOULD have caught the assistant-first
400 bug without a real API key.

A fake LLM stands in for the provider and asserts the actual provider contract:
every chat() request must start with a role='user' message (both Anthropic and
OpenAI reject an assistant-first message with a 400). We run the real
SimulatedTelephony conversation loop + the real VoiceEngine extraction pass
through it, proving the message-threading is correct by construction.

Run:  .venv/bin/python tests/test_voice.py
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.goals import supplier_qualification_goal
from app.bootstrap import eleanor_case
from app.models import Supplier, SupplierContact
from app.voice.engine import VoiceEngine
from app.voice.telephony import SimulatedTelephony


class ContractCheckingLLM:
    """Stand-in provider that enforces the real API contract, so we can exercise
    the conversation/extraction threading with no network or key."""
    def __init__(self):
        self.chat_calls = 0

    async def chat(self, system, messages):
        assert messages, "chat() called with no messages"
        assert messages[0]["role"] == "user", (
            f"first message must be role 'user' (provider returns 400 otherwise); "
            f"got {messages[0]['role']}. This is the assistant-first bug.")
        # roles must also alternate user/assistant for the Anthropic contract
        for a, b in zip(messages, messages[1:]):
            assert a["role"] != b["role"], "messages must alternate user/assistant roles"
        self.chat_calls += 1
        # Counterparty turns end the call quickly; agent wraps up with the token.
        if "care coordination agent" in system:
            return "Thanks, that's everything I needed. [END_CALL]"
        return "Yes, we accept Medicare, K0001 is in stock, we take assignment, "\
               "we deliver to Chicago, about 5 business days."

    async def extract(self, system, text, schema):
        return {"answered_meaningfully": True, "accepting_medicare": True,
                "k0001_in_stock": True, "accepts_assignment": True, "serves_address": True,
                "delivery_window_days": 5, "disqualify_reason": "", "confidence": 0.95}


class OneShotWorld:
    def next_attempt(self, name): return 1
    def answers(self, name, attempt): return True
    def persona(self, name, attempt):
        return "You are a friendly DME supplier rep. Answer the phone naturally."
    def world_effects(self, name, attempt): return []


def test_simulated_call_respects_provider_contract():
    llm = ContractCheckingLLM()
    engine = VoiceEngine(SimulatedTelephony(llm, OneShotWorld()), llm)
    case = eleanor_case()
    sc = SupplierContact(supplier=Supplier(name="Test DME", phone="(555) 000-0000",
                                           address="Chicago, IL"))
    goal = supplier_qualification_goal(case, sc)

    record, result = asyncio.run(engine.place_call(goal, now=datetime(2026, 7, 4, 9)))

    assert record.answered
    assert llm.chat_calls >= 2, "expected at least an agent turn and a counterparty reply"
    assert len(record.transcript) >= 2
    # The [END_CALL] control token must never leak into the stored transcript.
    assert all("[END_CALL]" not in t.text for t in record.transcript)
    assert record.outcome["confidence"] == 0.95
    print(f"PASS — {llm.chat_calls} contract-checked LLM turns, "
          f"{len(record.transcript)} transcript lines, extraction ok")


if __name__ == "__main__":
    test_simulated_call_respects_provider_contract()
