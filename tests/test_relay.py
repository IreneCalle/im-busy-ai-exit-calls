"""ConversationRelay message parsing/serialisation and the relay loop."""

from __future__ import annotations

import asyncio
import json

import pytest

from imbusy.call.latency import LatencyKind, LatencyRecorder
from imbusy.call.relay import (
    EndSession,
    InterruptMessage,
    MetricMessage,
    PromptMessage,
    QueueTransport,
    RelayHandler,
    SetupMessage,
    TextToken,
    dump_outbound,
    parse_inbound,
)
from imbusy.call.session import CallSession, CallState
from imbusy.excuse.schema import CallScript
from imbusy.llm import FakeLLM, TextDelta


def test_parse_setup_with_custom_parameters() -> None:
    raw = json.dumps(
        {
            "type": "setup",
            "sessionId": "VX123",
            "callSid": "CA123",
            "from": "+15005550006",
            "to": "+34600000000",
            "direction": "outbound-api",
            "callStatus": "IN-PROGRESS",
            "customParameters": {"call_id": "6f1c"},
            "someFutureField": 1,
        }
    )
    message = parse_inbound(raw)
    assert isinstance(message, SetupMessage)
    assert message.call_sid == "CA123"
    assert message.from_ == "+15005550006"
    assert message.custom_parameters == {"call_id": "6f1c"}


def test_parse_prompt_and_interrupt() -> None:
    prompt = parse_inbound(
        '{"type":"prompt","voicePrompt":"¿Qué pasa?","lang":"es-ES","last":true}'
    )
    assert isinstance(prompt, PromptMessage)
    assert prompt.voice_prompt == "¿Qué pasa?"
    interrupt = parse_inbound(
        '{"type":"interrupt","utteranceUntilInterrupt":"¿Mari? Oye","durationUntilInterruptMs":460}'
    )
    assert isinstance(interrupt, InterruptMessage)
    assert interrupt.duration_until_interrupt_ms == 460


def test_unknown_type_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_inbound('{"type":"nonsense"}')


def test_outbound_uses_twilio_field_names_and_drops_nulls() -> None:
    assert json.loads(dump_outbound(TextToken(token="Hola", last=False))) == {
        "type": "text",
        "token": "Hola",
        "last": False,
    }
    assert json.loads(dump_outbound(EndSession(handoff_data='{"reason":"done"}'))) == {
        "type": "end",
        "handoffData": '{"reason":"done"}',
    }


SETUP = SetupMessage(
    type="setup", session_id="VX1", call_sid="CA1", custom_parameters={"call_id": "c1"}
)
REPLIES = [
    "Es que me he dejado las llaves dentro.",
    "Y el cerrajero no viene hasta las nueve.",
    "¿Me llevas tú?",
    "Gracias, un beso.",
]


def prompt(text: str) -> PromptMessage:
    return PromptMessage(type="prompt", voice_prompt=text, lang="es-ES", last=True)


def start(
    script: CallScript,
    llm: FakeLLM,
    *,
    send_greeting: bool = False,
    silence_timeout: float | None = None,
) -> tuple[QueueTransport, RelayHandler, asyncio.Task[None], list[CallSession]]:
    transport = QueueTransport()
    finished: list[CallSession] = []

    async def resolve(setup: SetupMessage) -> CallSession | None:
        return CallSession(call_id="c1", script=script, llm=llm)

    async def on_finish(session: CallSession, latency: LatencyRecorder) -> None:
        finished.append(session)

    handler = RelayHandler(
        transport,
        resolve,
        send_greeting=send_greeting,
        silence_timeout=silence_timeout,
        playback_scale=0.0,
        on_finish=on_finish,
    )
    transport.push(SETUP)
    task = asyncio.create_task(handler.run())
    return transport, handler, task, finished


async def test_handler_streams_tokens_and_marks_last(script: CallScript) -> None:
    """A prompt produces text frames whose concatenation is the reply, the final one with last=true."""
    transport, _, task, _ = start(script, FakeLLM(text_replies=list(REPLIES)))
    transport.push(prompt("¿Sí?"))
    assert await transport.next_reply() == REPLIES[0]
    tokens = [m for m in transport.sent if isinstance(m, TextToken)]
    assert len(tokens) == len(REPLIES[0].split())
    assert [t.last for t in tokens] == [False] * (len(tokens) - 1) + [True]
    transport.push(None)
    await asyncio.wait_for(task, 2)


async def test_demo_mode_sends_greeting_first(script: CallScript) -> None:
    transport, _, task, _ = start(script, FakeLLM(), send_greeting=True)
    assert await transport.next_reply() == script.opening_line
    transport.push(None)
    await asyncio.wait_for(task, 2)


async def test_full_call_ends_with_end_message(script: CallScript) -> None:
    transport, handler, task, finished = start(script, FakeLLM(text_replies=list(REPLIES)))
    for text, reply in zip(["¿Sí?", "¿Y eso?", "Vaya.", "Voy."], REPLIES, strict=True):
        transport.push(prompt(text))
        assert await transport.next_reply() == reply
    await asyncio.wait_for(task, 2)
    assert isinstance(transport.sent[-1], EndSession)
    assert json.loads(transport.sent[-1].handoff_data or "")["reason"] == "script-complete"
    assert finished and finished[0].end_reason == "script-complete"
    assert handler.session is not None and handler.session.state is CallState.ENDED


class StallingLLM(FakeLLM):
    """First reply streams two words and then stalls, like a model mid-sentence; later replies are normal."""

    def __init__(self, later: list[str]) -> None:
        super().__init__(text_replies=later)
        self.stalled = asyncio.Event()
        self._first = True

    async def stream_text(self, **kwargs):  # type: ignore[no-untyped-def,override]
        if not self._first:
            async for event in super().stream_text(**kwargs):
                yield event
            return
        self._first = False
        yield TextDelta("uno")
        yield TextDelta(" dos")
        yield TextDelta(" tres")
        self.stalled.set()
        await asyncio.Event().wait()


async def test_interrupt_cancels_reply_in_flight(script: CallScript) -> None:
    """An interrupt mid-reply stops that turn and the transcript keeps only what was heard."""
    llm = StallingLLM([REPLIES[1]])
    transport, handler, task, _ = start(script, llm)
    transport.push(prompt("¿Sí?"))
    await asyncio.wait_for(llm.stalled.wait(), 2)
    transport.push(
        InterruptMessage(
            type="interrupt", utterance_until_interrupt="uno", duration_until_interrupt_ms=300
        )
    )
    transport.push(prompt("Perdona, ¿qué?"))
    assert await transport.next_reply() == "uno dos" + REPLIES[1]
    assert handler.session is not None
    assert handler.session.turns[1].caller_text == "uno"
    assert handler.session.turns[1].interrupted
    transport.push(None)
    await asyncio.wait_for(task, 2)


async def test_new_prompt_replaces_reply_in_flight(script: CallScript) -> None:
    """Speech without an interrupt frame (barge-in on a half-sent reply) still starts a fresh turn."""
    llm = StallingLLM([REPLIES[1]])
    transport, _, task, _ = start(script, llm)
    transport.push(prompt("¿Sí?"))
    await asyncio.wait_for(llm.stalled.wait(), 2)
    transport.push(prompt("Perdona, ¿qué?"))
    reply = await transport.next_reply()
    assert reply.endswith(REPLIES[1])
    assert "tres" not in reply
    transport.push(None)
    await asyncio.wait_for(task, 2)


async def test_unknown_session_closes_transport(script: CallScript) -> None:
    transport = QueueTransport()

    async def resolve(setup: SetupMessage) -> CallSession | None:
        return None

    transport.push(SETUP)
    await asyncio.wait_for(RelayHandler(transport, resolve, send_greeting=False).run(), 2)
    assert transport.closed
    assert transport.sent == []


async def test_first_message_must_be_setup(script: CallScript) -> None:
    transport = QueueTransport()

    async def resolve(setup: SetupMessage) -> CallSession | None:
        raise AssertionError("must not resolve")

    transport.push(prompt("hola"))
    await asyncio.wait_for(RelayHandler(transport, resolve, send_greeting=False).run(), 2)
    assert transport.closed


async def test_hangup_mid_call_ends_session(script: CallScript) -> None:
    transport, _, task, finished = start(script, FakeLLM(text_replies=list(REPLIES)))
    transport.push(prompt("¿Sí?"))
    await transport.next_reply()
    transport.push(None)
    await asyncio.wait_for(task, 2)
    assert finished[0].end_reason == "hangup"


async def test_silence_triggers_one_nudge(script: CallScript) -> None:
    llm = FakeLLM(text_replies=["¿Hola? ¿Me oyes?"])
    transport, handler, task, _ = start(script, llm, silence_timeout=0.05)
    assert await transport.next_reply(within=2) == "¿Hola? ¿Me oyes?"
    await asyncio.sleep(0.3)
    assert len(llm.calls) == 1
    assert handler.session is not None and handler.session.state is CallState.OPENING
    transport.push(None)
    await asyncio.wait_for(task, 2)


async def test_latency_recorded_per_turn(script: CallScript) -> None:
    llm = FakeLLM(text_replies=list(REPLIES))
    llm.first_token_delay_seconds = 0.05
    transport, handler, task, _ = start(script, llm)
    for text in ["a", "b"]:
        transport.push(prompt(text))
        await transport.next_reply()
    transport.push(MetricMessage(type="metric", turn=2, first_audio_ms=812.0))
    transport.push(None)
    await asyncio.wait_for(task, 2)
    assert handler.latency is not None
    server = [s for s in handler.latency.samples if s.kind is LatencyKind.SERVER_FIRST_TOKEN]
    assert [s.turn_index for s in server] == [2, 3]
    assert all(s.milliseconds >= 40 for s in server)
    client = [s for s in handler.latency.samples if s.kind is LatencyKind.CLIENT_FIRST_AUDIO]
    assert [(s.turn_index, s.milliseconds) for s in client] == [(2, 812.0)]


def test_metric_frame_parses() -> None:
    message = parse_inbound('{"type":"metric","turn":2,"firstAudioMs":640.5}')
    assert isinstance(message, MetricMessage)
    assert message.first_audio_ms == 640.5
