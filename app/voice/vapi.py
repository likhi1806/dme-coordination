"""EXPERIMENTAL, UNEXERCISED real-call path via Vapi (set VOICE_PROVIDER=vapi).

The load-bearing claim is the TelephonyAdapter SEAM, not this implementation:
because the orchestrator only knows `place_call(goal) -> CallResult`, a real
telephony provider drops in here without touching the coordination logic. This
file sketches that drop-in against Vapi; it has NOT been run against the live API.

Known caveats (documented, not hidden):
  * Run with MAX_SUPPLIERS_TO_CONTACT=1 — the parallel fan-out would otherwise
    ring the single VAPI_TARGET_NUMBER several times at once.
  * on_turn streaming isn't wired (production: Vapi webhooks, not end-of-call poll).
  * world_effects (e.g. the inbound written order) aren't produced on this path;
    it's for exercising a single live call, not a full simulated multi-day case.
Requires VAPI_API_KEY, VAPI_PHONE_NUMBER_ID, VAPI_TARGET_NUMBER (E.164).
"""
from __future__ import annotations

import asyncio

import httpx

from app import config
from app.agents.goals import CallGoal
from app.models import TranscriptTurn
from app.voice.telephony import CallResult, TelephonyAdapter

VAPI_BASE = "https://api.vapi.ai"


class VapiTelephony(TelephonyAdapter):
    async def place_call(self, goal: CallGoal, on_turn=None) -> CallResult:
        # on_turn unsupported here (transcript arrives at call end via polling;
        # production would stream it from Vapi webhooks).
        headers = {"Authorization": f"Bearer {config.VAPI_API_KEY}"}
        # The simulation strips/interprets the [END_CALL] control token; a real
        # TTS voice would SPEAK it. Remove that instruction and let Vapi's
        # endCallPhrases end the call naturally.
        system = goal.agent_system_prompt.replace(
            "end your final message with the token [END_CALL].",
            "end the call by saying a natural goodbye (say \"goodbye\").")
        payload = {
            "phoneNumberId": config.VAPI_PHONE_NUMBER_ID,
            "customer": {"number": config.VAPI_TARGET_NUMBER},
            "assistant": {
                "model": {"provider": "anthropic", "model": config.ANTHROPIC_MODEL,
                          "messages": [{"role": "system", "content": system}]},
                "firstMessageMode": "assistant-waits-for-user",
                "endCallPhrases": ["goodbye", "bye now"],
                "maxDurationSeconds": 240,
            },
        }
        # One failed call must not crash the fan-out — mirror a no-answer so the
        # FSM's retry/escalation policy handles it like any other unreachable call.
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(f"{VAPI_BASE}/call", json=payload, headers=headers)
                r.raise_for_status()
                call_id = r.json()["id"]
                for _ in range(120):  # poll to end (production: Vapi webhooks)
                    await asyncio.sleep(3)
                    s = await client.get(f"{VAPI_BASE}/call/{call_id}", headers=headers)
                    data = s.json()
                    if data.get("status") == "ended":
                        return self._to_result(goal, data)
            return CallResult(answered=False)
        except (httpx.HTTPError, KeyError, ValueError):
            return CallResult(answered=False)

    def _to_result(self, goal: CallGoal, data: dict) -> CallResult:
        if data.get("endedReason") in ("customer-did-not-answer", "no-answer", "busy"):
            return CallResult(answered=False)
        turns = []
        for m in (data.get("artifact", {}) or {}).get("messages", []):
            if m.get("role") == "bot":
                turns.append(TranscriptTurn(speaker="agent", text=m.get("message", "")))
            elif m.get("role") == "user":
                turns.append(TranscriptTurn(speaker=goal.counterparty_name, text=m.get("message", "")))
        return CallResult(answered=bool(turns), transcript=turns)
