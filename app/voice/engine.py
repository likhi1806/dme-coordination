"""VoiceEngine: the call pipeline. Every outbound interaction is the same shape:

    CallGoal -> telephony (conversation) -> transcript
             -> extractor (LLM, schema-constrained, temp 0) -> typed outcome

Talker and parser are SEPARATE LLM passes on purpose: different objectives,
re-runnable extraction over stored transcripts, and a confidence gate so the
FSM never acts on a guess."""
from __future__ import annotations

from datetime import datetime

from app.agents.goals import CallGoal
from app.llm.provider import LLMProvider
from app.models import CallRecord
from app.voice.telephony import CallResult, TelephonyAdapter

EXTRACTOR_SYSTEM = """You are a meticulous analyst extracting structured facts from a
phone call transcript. Rules:
- Extract ONLY what was actually said. Never infer beyond the words.
- If a later statement corrects an earlier one, the correction wins.
- Use null for anything not established on the call.
- Set confidence low (<0.7) if the call was ambiguous, contradictory, or cut short."""


class VoiceEngine:
    def __init__(self, telephony: TelephonyAdapter, llm: LLMProvider):
        self.telephony = telephony
        self.llm = llm

    async def place_call(self, goal: CallGoal, now: datetime,
                         on_turn=None, turn_delay: float = 0.0) -> tuple[CallRecord, CallResult]:
        # turn_delay is only meaningful for the offline scripted engine; live
        # LLM/telephony calls have natural latency between turns.
        result = await self.telephony.place_call(goal, on_turn=on_turn)

        record = CallRecord(
            call_type=goal.call_type,
            counterparty=goal.counterparty_name,
            phone=goal.phone,
            answered=result.answered,
            transcript=result.transcript,
            at=now,
        )
        if not result.answered:
            return record, result

        text = "\n".join(f"{t.speaker}: {t.text}" for t in result.transcript)
        outcome = await self.llm.extract(
            EXTRACTOR_SYSTEM,
            f"Call purpose: {goal.call_type.value}\n\nTranscript:\n{text}",
            goal.outcome_schema,
        )
        record.outcome = outcome
        record.confidence = float(outcome.get("confidence", 0.0))
        return record, result
