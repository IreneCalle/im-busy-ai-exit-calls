"""Browser demo: the same compiler and CallSession, with the browser doing speech.

    POST /demo/compile   {"text": "...", "language"?, "timezone"?, "safeWord"?, "name"?, "location"?}
    POST /demo/safeword  {"safeWord": "..."} -> whether the phrase is usable, and why not
    WS   /demo/ws?token=...                       ConversationRelay-shaped protocol

The page plays the role Twilio plays on a phone call: it sends `prompt` and `interrupt` frames and
speaks the `text` tokens it receives. One extra frame type lets the page report the latency it
measured: {"type": "metric", "turn": 2, "firstAudioMs": 812}. When the visitor set a safe word and
says it, the server sends one `safeword` frame with the alert the trusted contact would receive.
In the demo that alert is only shown on the page; nothing is sent to anyone.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from imbusy.api.deps import Services, services_from_request, services_from_websocket
from imbusy.api.twilio_routes import StarletteRelayTransport
from imbusy.call.latency import LatencyKind, LatencyRecorder
from imbusy.call.relay import RelayHandler, SafeWordPreview, SetupMessage
from imbusy.call.session import CallSession
from imbusy.excuse.schema import CallScript, CompileRefused, ExcuseRequest, Language, RefusalKind
from imbusy.llm import LLMError
from imbusy.safety.demo import DEMO_REASONS, DemoSafety
from imbusy.safety.safeword import SafeWordError, SafeWordMatcher, parse_safe_word
from imbusy.safety.trusted_contact import compose_alert, map_url
from imbusy.store.locations import SharedLocation
from imbusy.store.scripts import SCRIPT_GRACE_SECONDS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/demo", tags=["demo"])


class DemoLocation(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0)


class DemoCompileRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    text: str = Field(min_length=3, max_length=500)
    language: Language | None = None
    timezone: str | None = Field(default=None, max_length=64)
    safe_word: str | None = Field(default=None, max_length=200)
    name: str | None = Field(default=None, max_length=40)
    location: DemoLocation | None = None


class SafeWordCheckRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    safe_word: str = Field(max_length=200)


class _Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class DemoCompileResponse(_Camel):
    """Either a script with a one-use session token, or a refusal message."""

    status: Literal["ok", "refused"]
    mode: Literal["live", "fixture"]
    token: str | None = None
    script: CallScript | None = None
    refusal: RefusalKind | None = None
    message: str | None = None
    safe_word_armed: bool = False


class SafeWordCheckResponse(_Camel):
    ok: bool
    message: str | None = None


def client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/compile", response_model=DemoCompileResponse, response_model_by_alias=True)
async def demo_compile(
    body: DemoCompileRequest,
    request: Request,
    services: Services = Depends(services_from_request),
) -> DemoCompileResponse:
    """Rate-limit by client IP, compile, store the script under a one-use token (10 min)."""
    if not services.demo_limiter.allow(client_key(request), datetime.now(UTC)):
        raise HTTPException(status_code=429, detail="Too many demo calls. Try again later.")
    phrase: str | None = None
    if body.safe_word:
        try:
            phrase = parse_safe_word(body.safe_word)
        except SafeWordError as exc:
            raise HTTPException(status_code=422, detail=DEMO_REASONS[exc.reason]) from None
    excuse_request = ExcuseRequest(
        text=body.text,
        source="demo",
        preferred_language=body.language,
        received_at=datetime.now(UTC),
        timezone=body.timezone,
    )
    try:
        result = await services.compiler.compile(excuse_request)
    except LLMError as exc:
        logger.warning("demo compile failed: %s", exc)
        raise HTTPException(status_code=503, detail="The model is unavailable.") from None
    mode: Literal["live", "fixture"] = "fixture" if services.fixture_mode else "live"
    if isinstance(result, CompileRefused):
        return DemoCompileResponse(
            status="refused", mode=mode, refusal=result.kind, message=result.message
        )
    await services.scripts.put(result.call_id, result.script, SCRIPT_GRACE_SECONDS)
    if phrase is not None:
        location = None
        if body.location is not None:
            location = SharedLocation(
                latitude=body.location.latitude,
                longitude=body.location.longitude,
                accuracy_m=body.location.accuracy,
                captured_at=datetime.now(UTC),
            )
        name = (body.name or "").strip() or "Your friend"
        services.demo_safety.put(
            result.call_id, DemoSafety(phrase, name, location, body.timezone), SCRIPT_GRACE_SECONDS
        )
    return DemoCompileResponse(
        status="ok",
        mode=mode,
        token=str(result.call_id),
        script=result.script,
        safe_word_armed=phrase is not None,
    )


@router.post("/safeword", response_model=SafeWordCheckResponse, response_model_by_alias=True)
async def demo_check_safe_word(body: SafeWordCheckRequest) -> SafeWordCheckResponse:
    """Validate a phrase with the same rules the bot uses. Nothing is stored."""
    try:
        parse_safe_word(body.safe_word)
    except SafeWordError as exc:
        return SafeWordCheckResponse(ok=False, message=DEMO_REASONS[exc.reason])
    return SafeWordCheckResponse(ok=True)


def origin_allowed(origin: str | None, host: str | None) -> bool:
    """Browsers send Origin on WebSocket upgrades; it must match the host the page came from."""
    if origin is None:
        return True
    return host is not None and urlsplit(origin).netloc == host


@router.websocket("/ws")
async def demo_socket(websocket: WebSocket, token: str) -> None:
    """Check the Origin header and the token, then run a RelayHandler without a phone call.

    The token is single-use and maps to the stored script; the welcome greeting is sent as the
    first `text` frame because there is no TwiML here.
    """
    services = services_from_websocket(websocket)
    if not origin_allowed(websocket.headers.get("origin"), websocket.headers.get("host")):
        await websocket.close(code=1008)
        return
    try:
        call_id = UUID(token)
    except ValueError:
        await websocket.close(code=1008)
        return
    script = await services.scripts.get(call_id)
    if script is None:
        await websocket.close(code=1008)
        return
    await services.scripts.delete(call_id)
    safety = services.demo_safety.pop(call_id)
    await websocket.accept()
    transport = StarletteRelayTransport(websocket)

    async def show_alert() -> None:
        assert safety is not None
        alert = compose_alert(
            name=safety.name,
            language=script.language,
            triggered_at=datetime.now(UTC),
            timezone=safety.timezone,
            location=safety.location,
        )
        location = alert.location
        await transport.send(
            SafeWordPreview(
                text=alert.text,
                latitude=location.latitude if location else None,
                longitude=location.longitude if location else None,
                map_url=map_url(location) if location else None,
            )
        )

    async def resolve(setup: SetupMessage) -> CallSession | None:
        return CallSession(
            call_id=str(call_id),
            script=script,
            llm=services.llm,
            safe_word=SafeWordMatcher(safety.phrase) if safety else None,
            on_safe_word=show_alert if safety else None,
        )

    async def on_finish(session: CallSession, latency: LatencyRecorder) -> None:
        logger.info(
            "demo call finished",
            extra={
                "call_id": session.call_id,
                "turns": session.turn_index,
                "server_ms": [round(s.milliseconds) for s in latency.samples if s.kind is LatencyKind.SERVER_FIRST_TOKEN],
                "client_ms": [round(s.milliseconds) for s in latency.samples if s.kind is LatencyKind.CLIENT_FIRST_AUDIO],
            },
        )  # fmt: skip

    # The page decides when its own speech has finished, so no playback wait and no nudges:
    # a visitor typing their side of the call would otherwise be nudged mid-sentence.
    handler = RelayHandler(
        transport,
        resolve,
        send_greeting=True,
        silence_timeout=None,
        playback_scale=0.0,
        on_finish=on_finish,
    )
    setup = SetupMessage(type="setup", session_id=f"demo-{call_id}", call_sid="demo")
    await handler.run(setup)
    await StarletteRelayTransport(websocket).close()
