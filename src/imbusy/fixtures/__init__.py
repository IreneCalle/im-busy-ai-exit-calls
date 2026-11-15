"""Fixture mode: a scripted `LLMClient` that plays a full call with no model and no key.

Used when ANTHROPIC_API_KEY is unset so the browser demo works anywhere, and by tests and the
latency harness. It ignores what the user actually says: the call is scripted by state.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from functools import cache
from importlib import resources
from typing import Any, TypeVar

from pydantic import BaseModel

from imbusy.excuse.compiler import guess_language
from imbusy.excuse.schema import ExcuseDraft, Language
from imbusy.llm import (
    ChatMessage,
    Effort,
    ModelRole,
    StopReason,
    StreamEnd,
    StructuredResult,
    TextDelta,
    TextEvent,
    Usage,
)

T = TypeVar("T", bound=BaseModel)

FIXTURE_MODEL = "fixture"
_NUDGE_MARKER = "said nothing"
# Turn directives start with these words in turns.yaml; matched to pick the scripted line.
_DIRECTIVE_STATES = (
    ("They just picked up", "hook"),
    ("Make it clear this cannot wait", "escalate"),
    ("Ask them directly", "ask"),
    ("This is your last turn", "closing"),
    ("Keep exactly the same tone", "wrap_up"),
)


@cache
def demo_calls() -> dict[str, Any]:
    text = resources.files("imbusy.fixtures").joinpath("demo_calls.json").read_text("utf-8")
    data: dict[str, Any] = json.loads(text)
    return data


def fixture_call(language: Language) -> dict[str, Any]:
    call: dict[str, Any] = demo_calls()["calls"][language.value]
    return call


class ScriptedLLM:
    """`LLMClient` that returns the fixture draft and the scripted line for each turn."""

    def __init__(self, *, token_delay_seconds: float = 0.03) -> None:
        self._delay = token_delay_seconds

    async def structured(
        self,
        *,
        role: ModelRole,
        system: str,
        messages: Sequence[ChatMessage],
        output_type: type[T],
        max_tokens: int = 4000,
        effort: Effort = Effort.LOW,
    ) -> StructuredResult[T]:
        text = messages[-1].content if messages else ""
        language = Language.ES_ES
        for candidate in Language:
            if f"(Call language: {candidate.value})" in text:
                language = candidate
                break
        else:
            language = guess_language(text)
        draft = ExcuseDraft.model_validate(fixture_call(language)["draft"])
        if not issubclass(output_type, ExcuseDraft):
            raise TypeError("ScriptedLLM only produces ExcuseDraft")
        return StructuredResult(
            value=draft,  # type: ignore[arg-type]
            stop_reason=StopReason.END_TURN,
            usage=Usage(model=FIXTURE_MODEL),
        )

    async def stream_text(
        self,
        *,
        role: ModelRole,
        system: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 300,
        effort: Effort = Effort.LOW,
    ) -> AsyncIterator[TextEvent]:
        language = Language.EN_US if "American English" in system else Language.ES_ES
        lines = fixture_call(language)["lines"]
        key = "nudge" if _NUDGE_MARKER in system else _state_from_system(system)
        reply: str = lines[key]
        words = reply.split(" ")
        for index, word in enumerate(words):
            await asyncio.sleep(self._delay)
            yield TextDelta(word if index == 0 else " " + word)
        yield StreamEnd(StopReason.END_TURN, Usage(model=FIXTURE_MODEL, output_tokens=len(words)))


def _state_from_system(system: str) -> str:
    tail = system.rsplit("This turn:", 1)[-1]
    for marker, state in _DIRECTIVE_STATES:
        if marker in tail:
            return state
    return "ask"
