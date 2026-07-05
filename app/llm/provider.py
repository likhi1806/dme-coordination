"""LLM provider abstraction: two methods, two vendors, one interface.

chat()    -> free-text conversation turn (the "talker")
extract() -> schema-constrained JSON     (the "parser", temperature 0)

Deliberately NOT a framework (LangChain etc.): for two methods, owning ~100
lines beats importing a dependency tree. Provider selected via env."""
from __future__ import annotations

import json
from typing import Any

from app import config

# --- usage/cost tracking (unit economics are part of the product story) -----
# Rough $/M-token prices for the default models; good enough for an estimate.
PRICING = {"anthropic": {"in": 3.00, "out": 15.00},
           "openai": {"in": 0.15, "out": 0.60}}

USAGE = {"input_tokens": 0, "output_tokens": 0, "llm_calls": 0, "vendor": config.LLM_PROVIDER}


def record_usage(vendor: str, input_tokens: int, output_tokens: int) -> None:
    USAGE["input_tokens"] += input_tokens
    USAGE["output_tokens"] += output_tokens
    USAGE["llm_calls"] += 1
    USAGE["vendor"] = vendor


def estimated_cost_usd() -> float:
    p = PRICING.get(USAGE["vendor"], PRICING["anthropic"])
    return (USAGE["input_tokens"] * p["in"] + USAGE["output_tokens"] * p["out"]) / 1e6


def reset_usage() -> None:
    USAGE.update(input_tokens=0, output_tokens=0, llm_calls=0)


class LLMProvider:
    async def chat(self, system: str, messages: list[dict[str, str]]) -> str:
        """messages: [{"role": "user"|"assistant", "content": str}]"""
        raise NotImplementedError

    async def extract(self, system: str, text: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Force a JSON object matching `schema` out of the model (via tool-use)."""
        raise NotImplementedError


class AnthropicProvider(LLMProvider):
    def __init__(self, model: str = config.ANTHROPIC_MODEL):
        self.model = model
        self._client = None

    @property
    def client(self):
        # Lazy init: the dashboard must be able to render (and the zero-LLM test
        # must run) without an API key configured; fail on first call, not import.
        if self._client is None:
            from anthropic import AsyncAnthropic
            self._client = AsyncAnthropic()
        return self._client

    async def chat(self, system: str, messages: list[dict[str, str]]) -> str:
        resp = await self.client.messages.create(
            model=self.model, max_tokens=400, system=system, messages=messages,
        )
        record_usage("anthropic", resp.usage.input_tokens, resp.usage.output_tokens)
        return resp.content[0].text.strip()

    async def extract(self, system: str, text: str, schema: dict[str, Any]) -> dict[str, Any]:
        # Tool-use as structured output: the model MUST call the tool, and the
        # API enforces the input schema. Cheapest reliable structured output.
        resp = await self.client.messages.create(
            model=self.model, max_tokens=800, system=system,
            messages=[{"role": "user", "content": text}],
            tools=[{"name": "record_outcome",
                    "description": "Record the structured outcome extracted from the transcript.",
                    "input_schema": schema}],
            tool_choice={"type": "tool", "name": "record_outcome"},
        )
        record_usage("anthropic", resp.usage.input_tokens, resp.usage.output_tokens)
        for block in resp.content:
            if block.type == "tool_use":
                return dict(block.input)
        raise RuntimeError("extraction produced no tool call")


class OpenAIProvider(LLMProvider):
    def __init__(self, model: str = config.OPENAI_MODEL):
        self.model = model
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI()
        return self._client

    async def chat(self, system: str, messages: list[dict[str, str]]) -> str:
        resp = await self.client.chat.completions.create(
            model=self.model, max_tokens=400,
            messages=[{"role": "system", "content": system}, *messages],
        )
        record_usage("openai", resp.usage.prompt_tokens, resp.usage.completion_tokens)
        return resp.choices[0].message.content.strip()

    async def extract(self, system: str, text: str, schema: dict[str, Any]) -> dict[str, Any]:
        resp = await self.client.chat.completions.create(
            model=self.model, max_tokens=800, temperature=0,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": text}],
            tools=[{"type": "function",
                    "function": {"name": "record_outcome",
                                 "description": "Record the structured outcome.",
                                 "parameters": schema}}],
            tool_choice={"type": "function", "function": {"name": "record_outcome"}},
        )
        record_usage("openai", resp.usage.prompt_tokens, resp.usage.completion_tokens)
        call = resp.choices[0].message.tool_calls[0]
        return json.loads(call.function.arguments)


def get_provider() -> LLMProvider:
    if config.LLM_PROVIDER == "openai":
        return OpenAIProvider()
    return AnthropicProvider()
