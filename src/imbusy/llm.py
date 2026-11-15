"""Thin LLM client: the only module that talks to the Anthropic API.

Two call shapes cover the product:
- `structured`: one request, validated Pydantic output (the excuse compiler).
- `stream_text`: token stream for a spoken turn (the call session).

Every call is logged with tokens, cost and latency. `FakeLLM` replaces the real client in tests.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Generic, Literal, Protocol, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from imbusy.config import Settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class ModelRole(StrEnum):
    """Which configured model a call uses. Resolved to a model id by the client."""

    FAST = "fast"
    STRONG = "strong"


class Effort(StrEnum):
    """`output_config.effort` levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class StopReason(StrEnum):
    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    REFUSAL = "refusal"
    OTHER = "other"


class ChatMessage(BaseModel):
    """One conversation message. The call transcript is kept in this shape."""

    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float


# List prices for prompts up to 100K tokens; checked against the Anthropic pricing docs.
PRICES: dict[str, ModelPrice] = {
    "claude-haiku-5-5": ModelPrice(input=0.10, output=0.50, cache_read=0.01),
    "claude-sonnet-5-5": ModelPrice(input=2.00, output=10.00, cache_read=0.10),
}


@dataclass
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    latency_ms: float = 0.0
    time_to_first_token_ms: float | None = None

    @property
    def cost_usd(self) -> float | None:
        price = PRICES.get(self.model)
        if price is None:
            return None
        return (
            self.input_tokens * price.input
            + self.output_tokens * price.output
            + self.cache_read_input_tokens * price.cache_read
        ) / 1_000_000


@dataclass
class StructuredResult(Generic[T]):
    value: T | None
    stop_reason: StopReason
    usage: Usage


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class StreamEnd:
    stop_reason: StopReason
    usage: Usage


TextEvent = TextDelta | StreamEnd


class LLMError(Exception):
    """Non-retryable failure after the SDK's own retries are exhausted."""


class LLMClient(Protocol):
    """What the rest of the code depends on. Implemented by `AnthropicLLM` and `FakeLLM`."""

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
        """Return `output_type` parsed from the model's JSON output."""
        ...

    def stream_text(
        self,
        *,
        role: ModelRole,
        system: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 300,
        effort: Effort = Effort.LOW,
    ) -> AsyncIterator[TextEvent]:
        """Yield `TextDelta`s as they arrive, then exactly one `StreamEnd`."""
        ...


def log_usage(kind: str, usage: Usage, stop_reason: StopReason) -> None:
    """One structured log line per LLM call; the latency eval also reads these."""
    logger.info(
        "llm_call",
        extra={
            "kind": kind,
            "model": usage.model,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens,
            "cost_usd": usage.cost_usd,
            "latency_ms": round(usage.latency_ms, 1),
            "ttft_ms": None
            if usage.time_to_first_token_ms is None
            else round(usage.time_to_first_token_ms, 1),
            "stop_reason": stop_reason.value,
        },
    )


class AnthropicLLM:
    """`LLMClient` backed by the official `anthropic` SDK (2.x, async client).

    Request shapes (verified against the SDK docs, see docs/providers.md):

    - `structured` on the strong model (Sonnet 5.5):
      `client.beta.messages.parse(model=..., max_tokens=..., system=..., messages=...,
      output_format=OutputModel, output_config={"effort": "low"},
      betas=["server-side-fallback-2026-07-01"], fallbacks="default")`
      then `response.parsed_output`. Thinking cannot be disabled on Sonnet 5.5; effort is the lever.
      Check `response.stop_reason == "refusal"` before reading the output.
    - `stream_text` on the fast model (Haiku 5.5):
      `async with client.messages.stream(model=..., max_tokens=..., system=..., messages=...,
      thinking={"type": "disabled"}, output_config={"effort": "low"}) as stream:`
      iterate `stream.text_stream`, then `await stream.get_final_message()` for usage.
      Disabled thinking is accepted on Haiku 5.5 at effort low/medium/high and removes the
      thinking pause before the first spoken token. Haiku has no server-side fallback.

    No `temperature`/`top_p`/`top_k`: API version 2026-10-21 rejects them.
    """

    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(
        self,
        *,
        api_key: str | None,
        fast_model: str,
        strong_model: str,
        max_retries: int = 2,
        timeout_seconds: float = 30.0,
        use_fallbacks: bool = True,
        client: Any | None = None,
    ) -> None:
        if client is None:
            import anthropic

            client = anthropic.AsyncAnthropic(
                api_key=api_key, max_retries=max_retries, timeout=timeout_seconds
            )
        self._client = client
        self._fast_model = fast_model
        self._strong_model = strong_model
        self._use_fallbacks = use_fallbacks

    def model_for(self, role: ModelRole) -> str:
        """Resolve a role to the configured model id."""
        return self._fast_model if role is ModelRole.FAST else self._strong_model

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
        import anthropic
        from pydantic import ValidationError

        model = self.model_for(role)
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [m.model_dump() for m in messages],
            "output_format": output_type,
            "output_config": {"effort": effort.value},
        }
        started = time.perf_counter()
        try:
            if self._use_fallbacks and _supports_server_fallback(model):
                response = await self._client.beta.messages.parse(
                    **params, betas=[self.FALLBACK_BETA], fallbacks="default"
                )
            else:
                response = await self._client.messages.parse(**params)
        except ValidationError as exc:
            raise LLMError(f"structured output did not validate: {exc}") from exc
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            raise LLMError(str(exc)) from exc
        stop = map_stop_reason(response.stop_reason)
        usage = _usage_from(model, response.usage, time.perf_counter() - started)
        value = response.parsed_output if stop is not StopReason.REFUSAL else None
        log_usage("structured", usage, stop)
        return StructuredResult(value=value, stop_reason=stop, usage=usage)

    async def stream_text(
        self,
        *,
        role: ModelRole,
        system: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 300,
        effort: Effort = Effort.LOW,
    ) -> AsyncIterator[TextEvent]:
        import anthropic

        model = self.model_for(role)
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [m.model_dump() for m in messages],
            "output_config": {"effort": effort.value},
        }
        if _accepts_disabled_thinking(model):
            params["thinking"] = {"type": "disabled"}
        started = time.perf_counter()
        ttft: float | None = None
        try:
            async with self._client.messages.stream(**params) as stream:
                async for text in stream.text_stream:
                    if not text:
                        continue
                    if ttft is None:
                        ttft = (time.perf_counter() - started) * 1000
                    yield TextDelta(text)
                final = await stream.get_final_message()
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            raise LLMError(str(exc)) from exc
        stop = map_stop_reason(final.stop_reason)
        usage = _usage_from(model, final.usage, time.perf_counter() - started)
        usage.time_to_first_token_ms = ttft
        log_usage("stream_text", usage, stop)
        yield StreamEnd(stop, usage)


def map_stop_reason(raw: str | None) -> StopReason:
    if raw == "end_turn" or raw == "stop_sequence":
        return StopReason.END_TURN
    if raw == "max_tokens":
        return StopReason.MAX_TOKENS
    if raw == "refusal":
        return StopReason.REFUSAL
    return StopReason.OTHER


def _supports_server_fallback(model: str) -> bool:
    # Server-side fallbacks exist for the Sonnet/Opus 5.x line; Haiku has none.
    return not model.startswith("claude-haiku")


def _accepts_disabled_thinking(model: str) -> bool:
    # Haiku 5.5 accepts thinking=disabled at effort <= high; Sonnet 5.5 and Opus 5.5 reject it.
    return model.startswith("claude-haiku")


def _usage_from(model: str, raw: Any, elapsed_seconds: float) -> Usage:
    return Usage(
        model=model,
        input_tokens=getattr(raw, "input_tokens", 0) or 0,
        output_tokens=getattr(raw, "output_tokens", 0) or 0,
        cache_read_input_tokens=getattr(raw, "cache_read_input_tokens", 0) or 0,
        latency_ms=elapsed_seconds * 1000,
    )


@dataclass
class FakeCall:
    """What a `FakeLLM` was asked, for assertions in tests."""

    kind: Literal["structured", "stream_text"]
    role: ModelRole
    system: str
    messages: list[ChatMessage]
    output_type: type[BaseModel] | None = None


@dataclass
class FakeLLM:
    """Deterministic `LLMClient` for tests and offline development.

    Structured responses and text replies are consumed in order. Text replies are streamed
    word by word with an optional per-token delay so interruption and latency paths can be tested.
    A reply equal to `FakeLLM.REFUSAL` ends the stream with `StopReason.REFUSAL` and no text.
    """

    REFUSAL = "<<refusal>>"

    structured_responses: list[BaseModel | None] = field(default_factory=list)
    text_replies: list[str] = field(default_factory=list)
    token_delay_seconds: float = 0.0
    first_token_delay_seconds: float = 0.0
    model_name: str = "fake-model"
    calls: list[FakeCall] = field(default_factory=list)

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
        self.calls.append(
            FakeCall("structured", role, system, list(messages), output_type=output_type)
        )
        if not self.structured_responses:
            raise AssertionError("FakeLLM has no structured response left")
        response = self.structured_responses.pop(0)
        usage = Usage(model=self.model_name)
        if response is None:
            return StructuredResult(value=None, stop_reason=StopReason.REFUSAL, usage=usage)
        if not isinstance(response, output_type):
            raise AssertionError(
                f"FakeLLM queued {type(response).__name__}, caller expected {output_type.__name__}"
            )
        return StructuredResult(value=response, stop_reason=StopReason.END_TURN, usage=usage)

    async def stream_text(
        self,
        *,
        role: ModelRole,
        system: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 300,
        effort: Effort = Effort.LOW,
    ) -> AsyncIterator[TextEvent]:
        self.calls.append(FakeCall("stream_text", role, system, list(messages)))
        if not self.text_replies:
            raise AssertionError("FakeLLM has no text reply left")
        reply = self.text_replies.pop(0)
        started = time.perf_counter()
        if reply == self.REFUSAL:
            yield StreamEnd(StopReason.REFUSAL, Usage(model=self.model_name))
            return
        ttft: float | None = None
        words = reply.split(" ")
        for index, word in enumerate(words):
            await asyncio.sleep(
                self.first_token_delay_seconds if index == 0 else self.token_delay_seconds
            )
            if ttft is None:
                ttft = (time.perf_counter() - started) * 1000
            yield TextDelta(word if index == 0 else " " + word)
        usage = Usage(
            model=self.model_name,
            output_tokens=len(words),
            latency_ms=(time.perf_counter() - started) * 1000,
            time_to_first_token_ms=ttft,
        )
        yield StreamEnd(StopReason.END_TURN, usage)


def build_llm(settings: Settings) -> LLMClient:
    """Construct the real client from `Settings`; raises if no API key is configured."""
    if settings.anthropic_api_key is None or not settings.anthropic_api_key.get_secret_value():
        raise LLMError("ANTHROPIC_API_KEY is not set")
    return AnthropicLLM(
        api_key=settings.anthropic_api_key.get_secret_value(),
        fast_model=settings.llm_fast_model,
        strong_model=settings.llm_strong_model,
        max_retries=settings.llm_max_retries,
    )
