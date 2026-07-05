"""TelephonyAdapter: the seam between coordination logic and the phone network.

SimulatedTelephony runs the call as an LLM⇄LLM conversation:
  - our agent side uses the REAL production prompt (from CallGoal)
  - the counterparty is an LLM persona with a hidden scenario script
The simulation replaces the human, not our agent. Swap in VapiTelephony
(voice/vapi.py) and the rest of the system does not change."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app import config
from app.agents.goals import CallGoal
from app.llm.provider import LLMProvider
from app.models import TranscriptTurn
from app.voice.personas import ScenarioWorld


@dataclass
class CallResult:
    answered: bool
    transcript: list[TranscriptTurn] = field(default_factory=list)
    world_effects: list[dict[str, Any]] = field(default_factory=list)


class TelephonyAdapter:
    async def place_call(self, goal: CallGoal, on_turn: Callable | None = None) -> CallResult:
        """on_turn(TranscriptTurn): optional live-progress callback, invoked as
        each conversation turn happens (drives the dashboard's live-call view)."""
        raise NotImplementedError


class SimulatedTelephony(TelephonyAdapter):
    def __init__(self, llm: LLMProvider, world: ScenarioWorld):
        self.llm = llm
        self.world = world

    async def place_call(self, goal: CallGoal, on_turn: Callable | None = None) -> CallResult:
        name = goal.counterparty_name
        attempt = self.world.next_attempt(name)

        if not self.world.answers(name, attempt):
            # Ring... ring... nothing. Exactly what production would report.
            return CallResult(answered=False,
                              world_effects=self.world.world_effects(name, attempt))

        persona_system = (
            self.world.persona(name, attempt)
            + "\nYou are answering a PHONE CALL. Speak naturally, 1-3 short sentences "
              "per turn. Never break character, never mention being an AI or a scenario. "
              "Start by answering the phone with a greeting."
        )

        transcript: list[TranscriptTurn] = []

        def emit(turn: TranscriptTurn) -> None:
            transcript.append(turn)
            if on_turn:
                on_turn(turn)

        def as_messages(for_side: str) -> list[dict[str, str]]:
            """Same transcript, two viewpoints: each LLM sees its own lines as
            'assistant' and the other party's as 'user'. Both the Anthropic and
            OpenAI Messages APIs require the first message to be role 'user', so
            when this side spoke first (the counterparty answers the phone), we
            seed the view with the same '(phone rings)' user turn that started
            the call — otherwise the request 400s on an assistant-first message."""
            out = []
            for t in transcript:
                mine = (t.speaker == "agent") == (for_side == "agent")
                out.append({"role": "assistant" if mine else "user", "content": t.text})
            if out and out[0]["role"] == "assistant":
                out.insert(0, {"role": "user", "content": "(phone rings — answer it)"})
            return out

        # Counterparty answers the phone first.
        opening = await self.llm.chat(persona_system,
                                      [{"role": "user", "content": "(phone rings — answer it)"}])
        emit(TranscriptTurn(speaker=name, text=opening))

        for _ in range(config.MAX_CONVERSATION_TURNS):
            agent_line = await self.llm.chat(goal.agent_system_prompt, as_messages("agent"))
            ended = "[END_CALL]" in agent_line
            emit(TranscriptTurn(speaker="agent",
                                text=agent_line.replace("[END_CALL]", "").strip()))
            if ended:
                break
            reply = await self.llm.chat(persona_system, as_messages("counterparty"))
            emit(TranscriptTurn(speaker=name, text=reply))

        return CallResult(answered=True, transcript=transcript,
                          world_effects=self.world.world_effects(name, attempt))


def get_telephony(llm: LLMProvider, world: ScenarioWorld) -> TelephonyAdapter:
    if config.VOICE_PROVIDER == "vapi":
        from app.voice.vapi import VapiTelephony
        return VapiTelephony()
    return SimulatedTelephony(llm, world)
