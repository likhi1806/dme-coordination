"""Shared building blocks for call goals.

A CallGoal is everything the voice engine needs to run one call: the agent's
system prompt (the talker) and the outcome schema (the parser). These prompts
are the PRODUCTION prompts — the simulation swaps the human on the other end,
not our agent."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models import CallType

BASE_RULES = """
Hard rules:
- You are a care coordination agent calling on behalf of a patient. Be brief,
  warm, and professional — this is a phone call, keep each turn to 1-3 sentences.
- NEVER invent clinical information. You only know what's in your briefing.
- If the other party commits to anything, always ask "by when?" so there is a
  concrete date/time attached. Never accept a vague promise.
- Refer to equipment by both plain name and billing code whenever confirming
  what will be ordered/delivered — claims are paid against codes.
- When you have everything you need (or the call is clearly unproductive),
  politely wrap up and end your final message with the token [END_CALL].
"""

CONFIDENCE_FIELD = {
    "confidence": {"type": "number",
                   "description": "0-1: how confident you are that the extracted fields "
                                  "faithfully reflect the transcript. Below 0.7 means a "
                                  "human should review."}
}


@dataclass
class CallGoal:
    call_type: CallType
    counterparty_name: str
    phone: str
    agent_system_prompt: str
    outcome_schema: dict[str, Any]
    opening_context: str = ""   # optional extra context line for logs
    extra: dict[str, Any] = field(default_factory=dict)
