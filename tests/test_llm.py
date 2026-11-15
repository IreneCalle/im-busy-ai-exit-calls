"""The LLM client contract, exercised through FakeLLM, plus cost accounting."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from imbusy.excuse.schema import ExcuseDraft
from imbusy.llm import (
    AnthropicLLM,
    ChatMessage,
    FakeLLM,
    LLMError,
    ModelRole,
    StopReason,
    StreamEnd,
    TextDelta,
    Usage,
)


async def test_fake_stream_yields_deltas_then_one_end() -> None:
    llm = FakeLLM(text_replies=["Oye, que me ha pasado una cosa."])
    events = [
        e
        async for e in llm.stream_text(
            role=ModelRole.FAST, system="s", messages=[ChatMessage(role="user", content="¿Sí?")]
        )
    ]
    deltas = [e for e in events if isinstance(e, TextDelta)]
    assert "".join(d.text for d in deltas) == "Oye, que me ha pasado una cosa."
    assert isinstance(events[-1], StreamEnd)
    assert events[-1].stop_reason is StopReason.END_TURN
    assert sum(isinstance(e, StreamEnd) for e in events) == 1


async def test_fake_stream_refusal_has_no_text() -> None:
    llm = FakeLLM(text_replies=[FakeLLM.REFUSAL])
    events = [e async for e in llm.stream_text(role=ModelRole.FAST, system="s", messages=[])]
    assert len(events) == 1
    assert isinstance(events[0], StreamEnd)
    assert events[0].stop_reason is StopReason.REFUSAL


async def test_fake_structured_returns_queued_object_and_records_call(draft: ExcuseDraft) -> None:
    llm = FakeLLM(structured_responses=[draft])
    result = await llm.structured(
        role=ModelRole.STRONG, system="sys", messages=[], output_type=ExcuseDraft
    )
    assert result.value == draft
    assert llm.calls[0].kind == "structured"
    assert llm.calls[0].role is ModelRole.STRONG


async def test_fake_structured_none_means_refusal() -> None:
    llm = FakeLLM(structured_responses=[None])
    result = await llm.structured(
        role=ModelRole.STRONG, system="sys", messages=[], output_type=ExcuseDraft
    )
    assert result.value is None
    assert result.stop_reason is StopReason.REFUSAL


def test_cost_uses_list_prices() -> None:
    usage = Usage(model="claude-haiku-5-5", input_tokens=1_000_000, output_tokens=1_000_000)
    assert usage.cost_usd == pytest.approx(0.60)
    assert Usage(model="unknown").cost_usd is None


class _FakeStream:
    def __init__(self, texts: list[str], stop_reason: str) -> None:
        self._stop = stop_reason
        self.text_stream = self._gen(texts)

    @staticmethod
    async def _gen(texts: list[str]) -> AsyncIterator[str]:
        for text in texts:
            yield text

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get_final_message(self) -> SimpleNamespace:
        return SimpleNamespace(
            stop_reason=self._stop,
            usage=SimpleNamespace(input_tokens=1200, output_tokens=40, cache_read_input_tokens=0),
        )


class _FakeAnthropic:
    """Records request kwargs; mimics `AsyncAnthropic` for the two calls AnthropicLLM makes."""

    def __init__(self, *, texts: list[str] | None = None, stop_reason: str = "end_turn",
                 parsed: object | None = None) -> None:  # fmt: skip
        self.requests: list[tuple[str, dict[str, object]]] = []
        outer = self

        class Messages:
            def stream(self, **kwargs: object) -> _FakeStream:
                outer.requests.append(("stream", kwargs))
                return _FakeStream(texts or [], stop_reason)

            async def parse(self, **kwargs: object) -> SimpleNamespace:
                outer.requests.append(("parse", kwargs))
                return outer._parsed_response()

        class BetaMessages:
            async def parse(self, **kwargs: object) -> SimpleNamespace:
                outer.requests.append(("beta.parse", kwargs))
                return outer._parsed_response()

        self.messages = Messages()
        self.beta = SimpleNamespace(messages=BetaMessages())
        self._parsed = parsed
        self._stop = stop_reason

    def _parsed_response(self) -> SimpleNamespace:
        return SimpleNamespace(
            stop_reason=self._stop,
            parsed_output=self._parsed,
            usage=SimpleNamespace(input_tokens=2000, output_tokens=400, cache_read_input_tokens=0),
        )


def _llm(fake: _FakeAnthropic) -> AnthropicLLM:
    return AnthropicLLM(
        api_key=None, fast_model="claude-haiku-5-5", strong_model="claude-sonnet-5-5", client=fake
    )


async def test_anthropic_client_maps_refusal_stop_reason(draft: ExcuseDraft) -> None:
    """AnthropicLLM returns StopReason.REFUSAL (not an exception) when the API stops with 'refusal'."""
    fake = _FakeAnthropic(stop_reason="refusal", parsed=draft)
    result = await _llm(fake).structured(
        role=ModelRole.STRONG, system="s", messages=[], output_type=ExcuseDraft
    )
    assert result.stop_reason is StopReason.REFUSAL
    assert result.value is None


async def test_strong_model_uses_server_side_fallback(draft: ExcuseDraft) -> None:
    fake = _FakeAnthropic(parsed=draft)
    result = await _llm(fake).structured(
        role=ModelRole.STRONG, system="s", messages=[], output_type=ExcuseDraft
    )
    assert result.value == draft
    kind, kwargs = fake.requests[0]
    assert kind == "beta.parse"
    assert kwargs["model"] == "claude-sonnet-5-5"
    assert kwargs["fallbacks"] == "default"
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert kwargs["output_format"] is ExcuseDraft
    assert kwargs["output_config"] == {"effort": "low"}
    assert result.usage.cost_usd == pytest.approx((2000 * 2 + 400 * 10) / 1_000_000)


async def test_anthropic_client_sends_no_sampling_parameters(draft: ExcuseDraft) -> None:
    """Requests never include temperature/top_p/top_k (rejected by API version 2026-10-21)."""
    fake = _FakeAnthropic(texts=["Hola"], parsed=draft)
    llm = _llm(fake)
    await llm.structured(role=ModelRole.STRONG, system="s", messages=[], output_type=ExcuseDraft)
    [_ async for _ in llm.stream_text(role=ModelRole.FAST, system="s", messages=[])]
    for _, kwargs in fake.requests:
        assert not {"temperature", "top_p", "top_k"} & set(kwargs)


async def test_anthropic_stream_disables_thinking_on_fast_model() -> None:
    """stream_text sends thinking={'type': 'disabled'} with effort low for the fast model."""
    fake = _FakeAnthropic(texts=["Oye, ", "", "que me ha pasado algo."])
    events = [
        e
        async for e in _llm(fake).stream_text(
            role=ModelRole.FAST, system="s", messages=[ChatMessage(role="user", content="¿Sí?")]
        )
    ]
    kind, kwargs = fake.requests[0]
    assert kind == "stream"
    assert kwargs["thinking"] == {"type": "disabled"}
    assert kwargs["output_config"] == {"effort": "low"}
    assert kwargs["messages"] == [{"role": "user", "content": "¿Sí?"}]
    texts = [e.text for e in events if isinstance(e, TextDelta)]
    assert texts == ["Oye, ", "que me ha pasado algo."]
    end = events[-1]
    assert isinstance(end, StreamEnd) and end.usage.time_to_first_token_ms is not None


async def test_api_errors_become_llm_error() -> None:
    import anthropic
    import httpx2

    class Failing(_FakeAnthropic):
        def __init__(self) -> None:
            super().__init__()

            class Messages:
                def stream(self, **kwargs: object) -> object:
                    raise anthropic.APIConnectionError(
                        request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
                    )

            self.messages = Messages()  # type: ignore[assignment]

    with pytest.raises(LLMError):
        [_ async for _ in _llm(Failing()).stream_text(role=ModelRole.FAST, system="s", messages=[])]
