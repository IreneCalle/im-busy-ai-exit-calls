"""Twilio endpoints.

    POST /twilio/status          call status callback (StatusCallback), ?call_id=...
    POST /twilio/connect-action  <Connect action> after the relay session ends -> <Hangup/>
    WS   /twilio/relay           ConversationRelay WebSocket

All three verify X-Twilio-Signature against the public URL (see imbusy.api.security).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from urllib.parse import parse_qsl
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from imbusy.api.deps import Services, services_from_request, services_from_websocket
from imbusy.api.security import public_url
from imbusy.call.dialer import TwilioCallStatus
from imbusy.call.latency import LatencyKind, LatencyRecorder, percentile
from imbusy.call.relay import (
    InboundMessage,
    OutboundMessage,
    RelayClosed,
    RelayHandler,
    SetupMessage,
    dump_outbound,
    parse_inbound,
)
from imbusy.call.session import CallSession
from imbusy.call.twiml import CONNECT_ACTION_PATH, RELAY_PATH, STATUS_PATH, hangup_twiml
from imbusy.safety.safeword import SafeWordMatcher
from imbusy.store.models import CallOutcome

logger = logging.getLogger(__name__)

router = APIRouter(tags=["twilio"])

STATUS_TO_OUTCOME: dict[TwilioCallStatus, CallOutcome] = {
    TwilioCallStatus.IN_PROGRESS: CallOutcome.IN_PROGRESS,
    TwilioCallStatus.ANSWERED: CallOutcome.IN_PROGRESS,
    TwilioCallStatus.COMPLETED: CallOutcome.COMPLETED,
    TwilioCallStatus.BUSY: CallOutcome.BUSY,
    TwilioCallStatus.NO_ANSWER: CallOutcome.NO_ANSWER,
    TwilioCallStatus.FAILED: CallOutcome.FAILED,
    TwilioCallStatus.CANCELED: CallOutcome.CANCELLED,
}
TERMINAL_OUTCOMES = frozenset(
    {
        CallOutcome.COMPLETED,
        CallOutcome.BUSY,
        CallOutcome.NO_ANSWER,
        CallOutcome.FAILED,
        CallOutcome.CANCELLED,
    }
)


async def _verified_form(request: Request, services: Services, path: str) -> dict[str, str]:
    # Twilio posts application/x-www-form-urlencoded; parsed here to avoid python-multipart.
    form = dict(parse_qsl((await request.body()).decode("utf-8"), keep_blank_values=True))
    settings = services.settings
    if settings.twilio_validate_signatures:
        assert services.twilio_signatures is not None and settings.public_base_url is not None
        url = public_url(settings.public_base_url, path, request.url.query)
        signature = request.headers.get("X-Twilio-Signature")
        if not services.twilio_signatures.check_webhook(url, form, signature):
            raise HTTPException(status_code=403, detail="bad signature")
    return form


def _call_id(raw: str | None) -> UUID | None:
    try:
        return UUID(raw) if raw else None
    except ValueError:
        return None


@router.post(STATUS_PATH)
async def call_status(
    request: Request, services: Services = Depends(services_from_request)
) -> Response:
    """Record CallStatus transitions; on no-answer/busy/failed tell the user on Telegram."""
    form = await _verified_form(request, services, STATUS_PATH)
    call_id = _call_id(request.query_params.get("call_id"))
    try:
        status = TwilioCallStatus(form.get("CallStatus", ""))
    except ValueError:
        return Response(status_code=204)
    outcome = STATUS_TO_OUTCOME.get(status)
    if call_id is None or outcome is None:
        return Response(status_code=204)
    record = await services.calls.get(call_id)
    if record is None or record.outcome in TERMINAL_OUTCOMES:
        return Response(status_code=204)
    terminal = outcome in TERMINAL_OUTCOMES
    await services.calls.set_outcome(call_id, outcome, datetime.now(UTC), terminal=terminal)
    if terminal:
        await services.scripts.delete(call_id)
    if services.bot is not None and outcome in (
        CallOutcome.NO_ANSWER,
        CallOutcome.BUSY,
        CallOutcome.FAILED,
    ):
        await services.bot.notify_call_outcome(call_id, outcome)
    return Response(status_code=204)


@router.post(CONNECT_ACTION_PATH)
async def connect_action(
    request: Request, services: Services = Depends(services_from_request)
) -> Response:
    """Receives SessionStatus/HandoffData/ErrorCode when the relay session ends; returns <Hangup/>."""
    form = await _verified_form(request, services, CONNECT_ACTION_PATH)
    logger.info(
        "relay session ended",
        extra={
            "session_status": form.get("SessionStatus"),
            "error_code": form.get("ErrorCode"),
            "call_sid": form.get("CallSid"),
        },
    )
    return Response(content=hangup_twiml(), media_type="application/xml")


class StarletteRelayTransport:
    """`RelayTransport` over a Starlette WebSocket (used by both the Twilio and demo endpoints)."""

    def __init__(self, websocket: WebSocket) -> None:
        self._ws = websocket

    async def receive(self) -> InboundMessage:
        """Receive text and parse it; maps WebSocketDisconnect to `RelayClosed`.

        Frames that do not parse are logged and skipped rather than ending the call.
        """
        while True:
            try:
                raw = await self._ws.receive_text()
            except (WebSocketDisconnect, RuntimeError):
                raise RelayClosed from None
            try:
                return parse_inbound(raw)
            except ValueError:
                logger.warning("relay: unparseable frame skipped")

    async def send(self, message: OutboundMessage) -> None:
        if self._ws.client_state is WebSocketState.CONNECTED:
            await self._ws.send_text(dump_outbound(message))

    async def close(self) -> None:
        if self._ws.client_state is WebSocketState.CONNECTED:
            await self._ws.close()


@router.websocket(RELAY_PATH)
async def relay_socket(websocket: WebSocket) -> None:
    """Validate the handshake signature before accepting; reject with 1008 when invalid."""
    services = services_from_websocket(websocket)
    settings = services.settings
    if settings.twilio_validate_signatures:
        assert services.twilio_signatures is not None and settings.public_wss_base is not None
        expected = settings.public_wss_base + RELAY_PATH
        signature = websocket.headers.get("x-twilio-signature")
        if not services.twilio_signatures.check_websocket(expected, signature):
            logger.warning("relay: handshake signature rejected")
            await websocket.close(code=1008)
            return
    await websocket.accept()

    async def resolve(setup: SetupMessage) -> CallSession | None:
        call_id = _call_id(setup.custom_parameters.get("call_id"))
        if call_id is None:
            return None
        record = await services.calls.get(call_id)
        if record is None or (record.twilio_call_sid and record.twilio_call_sid != setup.call_sid):
            return None
        script = await services.scripts.get(call_id)
        if script is None:
            return None
        await services.calls.set_outcome(call_id, CallOutcome.IN_PROGRESS, datetime.now(UTC))
        user = await services.users.get(record.telegram_user_id)
        matcher = SafeWordMatcher(user.safe_word) if user and user.safe_word else None
        alerter = services.alerter
        user_id = record.telegram_user_id

        async def alert() -> None:
            if alerter is not None:
                await alerter.alert(user_id, datetime.now(UTC))

        return CallSession(
            call_id=str(call_id),
            script=script,
            llm=services.llm,
            safe_word=matcher,
            on_safe_word=alert if matcher else None,
        )

    async def on_finish(session: CallSession, latency: LatencyRecorder) -> None:
        call_id = UUID(session.call_id)
        server = [
            s.milliseconds for s in latency.samples if s.kind is LatencyKind.SERVER_FIRST_TOKEN
        ]
        p50 = percentile(server, 50) if server else None
        await services.calls.set_stats(call_id, session.turn_index, p50)
        logger.info(
            "call finished",
            extra={"call_id": session.call_id, "turns": session.turn_index, "reason": session.end_reason,
                   "first_token_ms": json.dumps([round(v) for v in server])},
        )  # fmt: skip

    handler = RelayHandler(
        StarletteRelayTransport(websocket), resolve, send_greeting=False, on_finish=on_finish
    )
    await handler.run()
