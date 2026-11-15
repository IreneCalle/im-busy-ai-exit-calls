"""ConversationRelay WebSocket protocol and the loop that connects it to a CallSession.

Message shapes follow https://www.twilio.com/docs/voice/conversationrelay/websocket-messages.
The browser demo speaks the same protocol (a subset), so one `RelayHandler` serves both:
Twilio does STT/TTS on a phone call; the demo page does it with the Web Speech API.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic.alias_generators import to_camel

from imbusy.call.latency import LatencyRecorder
from imbusy.call.session import CallSession, CallState

logger = logging.getLogger(__name__)


class _Msg(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


# ---------------------------------------------------------------------------
# Twilio -> server
# ---------------------------------------------------------------------------


class SetupMessage(_Msg):
    """First message after the socket opens."""

    type: Literal["setup"]
    session_id: str
    call_sid: str
    account_sid: str | None = None
    parent_call_sid: str | None = None
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    forwarded_from: str | None = None
    caller_name: str | None = None
    direction: str | None = None
    call_type: str | None = None
    call_status: str | None = None
    custom_parameters: dict[str, str] = {}


class PromptMessage(_Msg):
    """Transcribed caller speech. With partialPrompts off, each message has last=true."""

    type: Literal["prompt"]
    voice_prompt: str
    lang: str | None = None
    last: bool = True


class DtmfMessage(_Msg):
    type: Literal["dtmf"]
    digit: str


class InterruptMessage(_Msg):
    """The caller spoke over TTS. `utterance_until_interrupt` is what they actually heard."""

    type: Literal["interrupt"]
    utterance_until_interrupt: str = ""
    duration_until_interrupt_ms: int | None = None


class InfoMessage(_Msg):
    """Event notifications subscribed via the `events` attribute (e.g. configUpdated)."""

    type: Literal["info"]
    name: str | None = None
    value: dict[str, object] | None = None


class ErrorMessage(_Msg):
    type: Literal["error"]
    description: str


class MetricMessage(_Msg):
    """Demo page only: latency it measured from end of speech to first synthesized audio."""

    type: Literal["metric"]
    turn: int
    first_audio_ms: float


InboundMessage = Annotated[
    SetupMessage
    | PromptMessage
    | DtmfMessage
    | InterruptMessage
    | InfoMessage
    | ErrorMessage
    | MetricMessage,
    Field(discriminator="type"),
]
_inbound_adapter: TypeAdapter[InboundMessage] = TypeAdapter(InboundMessage)


def parse_inbound(raw: str | bytes) -> InboundMessage:
    """Validate one JSON frame from Twilio (or the demo page)."""
    return _inbound_adapter.validate_json(raw)


# ---------------------------------------------------------------------------
# server -> Twilio
# ---------------------------------------------------------------------------


class TextToken(_Msg):
    """Text to speak. Stream tokens as they arrive; mark the final one with last=true."""

    type: Literal["text"] = "text"
    token: str
    last: bool = False
    interruptible: bool | None = None
    preemptible: bool | None = None
    lang: str | None = None


class EndSession(_Msg):
    """Ends the ConversationRelay session; Twilio then requests the <Connect action> URL."""

    type: Literal["end"] = "end"
    handoff_data: str | None = None


class LanguageSwitch(_Msg):
    type: Literal["language"] = "language"
    tts_language: str | None = None
    transcription_language: str | None = None


class SafeWordPreview(_Msg):
    """Demo page only: what the trusted contact would have received. Nothing is sent anywhere.

    Twilio never sees this frame; on a phone call the alert goes out through Telegram instead.
    """

    type: Literal["safeword"] = "safeword"
    text: str
    latitude: float | None = None
    longitude: float | None = None
    map_url: str | None = None


OutboundMessage = TextToken | EndSession | LanguageSwitch | SafeWordPreview


def dump_outbound(message: OutboundMessage) -> str:
    """Serialise with Twilio's camelCase names and without null fields."""
    return message.model_dump_json(by_alias=True, exclude_none=True)


# ---------------------------------------------------------------------------
# Transport and handler
# ---------------------------------------------------------------------------


class RelayTransport(Protocol):
    """A ConversationRelay peer: Twilio's socket or the demo page's socket."""

    async def receive(self) -> InboundMessage:
        """Next inbound message; raises `RelayClosed` when the peer disconnects."""
        ...

    async def send(self, message: OutboundMessage) -> None: ...

    async def close(self) -> None: ...


class RelayClosed(Exception):
    """The peer closed the socket (caller hung up or the tab closed)."""


class QueueTransport:
    """In-memory transport for tests, the terminal `talk` command and the latency harness."""

    def __init__(self) -> None:
        self.inbound: asyncio.Queue[InboundMessage | None] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        self.sent: list[OutboundMessage] = []
        self.closed = False

    async def receive(self) -> InboundMessage:
        message = await self.inbound.get()
        if message is None:
            raise RelayClosed
        return message

    async def send(self, message: OutboundMessage) -> None:
        self.sent.append(message)
        await self.outbound.put(message)

    async def close(self) -> None:
        self.closed = True

    def push(self, message: InboundMessage | None) -> None:
        """Queue an inbound message; None simulates the peer hanging up."""
        self.inbound.put_nowait(message)

    async def next_reply(self, within: float = 5.0) -> str:
        """Collect text tokens until one with last=true; returns the joined text."""
        parts: list[str] = []
        while True:
            message = await asyncio.wait_for(self.outbound.get(), within)
            if isinstance(message, TextToken):
                parts.append(message.token)
                if message.last:
                    return "".join(parts)
            elif isinstance(message, EndSession):
                return "".join(parts)


def estimated_playback_seconds(
    text: str, chars_per_second: float = 14.0, padding: float = 0.8
) -> float:
    """Rough spoken duration of `text`; used to delay `end` until the farewell has been heard."""
    return len(text) / chars_per_second + padding


SessionResolver = Callable[[SetupMessage], Awaitable[CallSession | None]]
FinishCallback = Callable[[CallSession, LatencyRecorder], Awaitable[None]]


class RelayHandler:
    """Pumps messages between a transport and a CallSession until the session ends.

    - on `setup`: resolve the session (the resolver checks call id and call SID), speak nothing
      on the phone (Twilio plays `welcomeGreeting`) or send the greeting as text in the demo;
    - on `prompt` with last=true: cancel any reply in flight, start a new reply task that streams
      `TextToken`s, record latency from receipt to first token sent;
    - on `interrupt`: cancel the reply task and tell the session what the user actually heard;
    - silence: if no prompt arrives within `silence_timeout` after the caller's line should have
      finished playing, ask the session for one nudge;
    - after the CLOSING turn: wait for the farewell to be played, then send `EndSession`;
    - on `error`: log it; ConversationRelay keeps the session open for non-fatal errors.
    """

    def __init__(
        self,
        transport: RelayTransport,
        resolve: SessionResolver,
        *,
        send_greeting: bool,
        silence_timeout: float | None = 6.0,
        playback_scale: float = 1.0,
        on_finish: FinishCallback | None = None,
    ) -> None:
        self._transport = transport
        self._resolve = resolve
        self._send_greeting = send_greeting
        self._silence_timeout = silence_timeout
        self._playback_scale = playback_scale
        self._on_finish = on_finish
        self._session: CallSession | None = None
        self._latency: LatencyRecorder | None = None
        self._reply_task: asyncio.Task[None] | None = None
        self._finished = asyncio.Event()
        self._silence_deadline: float | None = None
        self._nudged_this_turn = False

    @property
    def session(self) -> CallSession | None:
        return self._session

    @property
    def latency(self) -> LatencyRecorder | None:
        return self._latency

    async def run(self, setup: SetupMessage | None = None) -> None:
        """Main loop. Returns when the session ends or the peer disconnects."""
        try:
            first = setup or await self._transport.receive()
        except RelayClosed:
            return
        if not isinstance(first, SetupMessage):
            logger.warning("relay: first message was %s, not setup", first.type)
            await self._transport.close()
            return
        session = await self._resolve(first)
        if session is None:
            logger.warning("relay: no session for setup", extra={"call_sid": first.call_sid})
            await self._transport.close()
            return
        self._session = session
        self._latency = LatencyRecorder(session.call_id)
        greeting = session.opening_line()
        if self._send_greeting:
            await self._transport.send(TextToken(token=greeting, last=True))
        self._arm_silence(greeting)
        try:
            await self._loop(session)
        finally:
            await self._cancel_reply()
            # A safe word said in the last seconds must still reach the contact.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(session.wait_for_alerts(), timeout=15)
            if session.state is not CallState.ENDED:
                session.end("hangup")
            if self._on_finish is not None:
                await self._on_finish(session, self._latency)

    async def _loop(self, session: CallSession) -> None:
        receive: asyncio.Task[InboundMessage] | None = None
        finished = asyncio.ensure_future(self._finished.wait())
        try:
            while not self._finished.is_set():
                if receive is None:
                    receive = asyncio.ensure_future(self._transport.receive())
                timeout = self._seconds_to_silence()
                done, _ = await asyncio.wait(
                    {receive, finished}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if self._finished.is_set():
                    break
                if receive in done:
                    task, receive = receive, None
                    try:
                        message = task.result()
                    except RelayClosed:
                        session.end("hangup")
                        return
                    await self._dispatch(session, message)
                elif not done:
                    self._on_silence(session)
        finally:
            pending: list[asyncio.Future[Any]] = [finished]
            if receive is not None:
                pending.append(receive)
            for future in pending:
                if not future.done():
                    future.cancel()
                    with contextlib.suppress(asyncio.CancelledError, RelayClosed):
                        await future

    async def _dispatch(self, session: CallSession, message: InboundMessage) -> None:
        if isinstance(message, PromptMessage):
            if not message.last or not message.voice_prompt.strip():
                return
            if not session.accepts_input:
                return
            await self._cancel_reply()
            self._silence_deadline = None
            self._nudged_this_turn = False
            turn = session.turn_index + 1
            assert self._latency is not None
            self._latency.prompt_received(turn)
            self._reply_task = asyncio.create_task(
                self._reply(session.respond(message.voice_prompt), turn)
            )
        elif isinstance(message, InterruptMessage):
            if session.is_final_turn:
                return
            await self._cancel_reply()
            session.mark_interrupted(message.utterance_until_interrupt)
        elif isinstance(message, MetricMessage):
            if self._latency is not None:
                self._latency.client_reported(message.turn, message.first_audio_ms)
        elif isinstance(message, ErrorMessage):
            logger.warning("relay error from peer: %s", message.description)
        elif isinstance(message, SetupMessage):
            logger.warning("relay: duplicate setup ignored")

    async def _reply(
        self, chunks: AsyncGenerator[str, None], turn: int, *, nudge: bool = False
    ) -> None:
        """Stream one caller turn.

        One chunk is held back so the final token can carry last=true (Twilio requires a
        non-null token, so an empty closing token is avoided). Costs one inter-token gap.
        """
        assert self._session is not None and self._latency is not None
        session = self._session
        pending: str | None = None
        spoken: list[str] = []
        recorded = nudge
        try:
            async for chunk in chunks:
                if pending is not None:
                    await self._transport.send(TextToken(token=pending, last=False))
                    if not recorded:
                        self._latency.first_token_sent(turn)
                        recorded = True
                pending = chunk
                spoken.append(chunk)
        finally:
            await chunks.aclose()
        if pending is not None:
            await self._transport.send(TextToken(token=pending, last=True))
            if not recorded:
                self._latency.first_token_sent(turn)
        text = "".join(spoken)
        if session.is_final_turn:
            await asyncio.sleep(estimated_playback_seconds(text) * self._playback_scale)
            handoff = json.dumps({"reason": "script-complete", "call_id": session.call_id})
            await self._transport.send(EndSession(handoff_data=handoff))
            session.end("script-complete")
            self._finished.set()
        else:
            self._arm_silence(text)

    def _on_silence(self, session: CallSession) -> None:
        self._silence_deadline = None
        if self._nudged_this_turn or not session.accepts_input:
            return
        if self._reply_task is not None and not self._reply_task.done():
            return
        self._nudged_this_turn = True
        self._reply_task = asyncio.create_task(
            self._reply(session.nudge(), session.turn_index, nudge=True)
        )

    def _arm_silence(self, spoken_text: str) -> None:
        if self._silence_timeout is None:
            return
        playback = estimated_playback_seconds(spoken_text) * self._playback_scale
        self._silence_deadline = time.monotonic() + playback + self._silence_timeout

    def _seconds_to_silence(self) -> float | None:
        if self._silence_deadline is None:
            return None
        return max(0.0, self._silence_deadline - time.monotonic())

    async def _cancel_reply(self) -> None:
        task, self._reply_task = self._reply_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
