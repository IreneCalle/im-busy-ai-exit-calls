"""Telegram webhook -> compile -> dial -> status callbacks -> ConversationRelay socket.

Everything Twilio and Telegram would send is replayed from recorded-shape fixtures; everything we
would send to them is captured by fakes (an httpx MockTransport for Telegram, a recording dialer
for Twilio). Signatures are computed with Twilio's own RequestValidator.
"""

from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from twilio.request_validator import RequestValidator

from imbusy.call.dialer import PlacedCall, TwilioCallStatus
from imbusy.config import Settings
from imbusy.excuse.schema import ExcuseDraft
from imbusy.llm import FakeLLM
from imbusy.main import create_app
from imbusy.store.models import CallOutcome
from imbusy.telegram.client import TelegramClient

FIXTURES = Path(__file__).parent / "fixtures"
UPDATES = json.loads((FIXTURES / "telegram_updates.json").read_text(encoding="utf-8"))
TWILIO = json.loads((FIXTURES / "twilio_requests.json").read_text(encoding="utf-8"))
BASE = "https://example.trycloudflare.com"
SECRET = "test_secret-1"
AUTH_TOKEN = "test-auth-token"
CALL_SID = TWILIO["status_no_answer"]["CallSid"]


@dataclass
class RecordingDialer:
    placed: list[dict[str, Any]] = field(default_factory=list)
    hung_up: list[str] = field(default_factory=list)

    async def place(self, *, to: str, twiml: str, call_id: UUID) -> PlacedCall:
        self.placed.append({"to": to, "twiml": twiml, "call_id": call_id})
        return PlacedCall(call_sid=CALL_SID, status=TwilioCallStatus.QUEUED)

    async def hang_up(self, call_sid: str) -> None:
        self.hung_up.append(call_sid)


@dataclass
class Harness:
    client: TestClient
    dialer: RecordingDialer
    telegram_requests: list[dict[str, Any]]
    llm: FakeLLM

    def webhook(self, name: str, secret: str = SECRET) -> int:
        response = self.client.post(
            "/telegram/webhook",
            json=UPDATES[name],
            headers={"X-Telegram-Bot-Api-Secret-Token": secret},
        )
        return response.status_code

    def sent_texts(self) -> list[str]:
        return [r["text"] for r in self.telegram_requests if r["method"] == "sendMessage"]

    def twilio_post(
        self, path: str, form: dict[str, str], query: str = "", sign: bool = True
    ) -> httpx.Response:
        url = f"{BASE}{path}" + (f"?{query}" if query else "")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if sign:
            headers["X-Twilio-Signature"] = RequestValidator(AUTH_TOKEN).compute_signature(
                url, form
            )
        target = path + (f"?{query}" if query else "")
        return self.client.post(target, content=urlencode(form), headers=headers)


@pytest.fixture
def harness(tmp_path: Path, real_call_settings: Settings, draft: ExcuseDraft) -> Iterator[Harness]:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        requests.append({"method": method, **json.loads(request.content)})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(requests)}})

    telegram = TelegramClient(
        "123:test", http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    dialer = RecordingDialer()
    llm = FakeLLM(
        structured_responses=[draft.model_copy(update={"delay_seconds": 0})],
        text_replies=["Es que me he dejado las llaves dentro.", "Gracias, cariño. Un beso."],
    )
    settings = real_call_settings.model_copy(
        update={"database_path": tmp_path / "db.sqlite3", "web_dist": tmp_path / "none"}
    )
    app = create_app(settings, llm, dialer=dialer, telegram=telegram)
    with TestClient(app) as client:
        yield Harness(client, dialer, requests, llm)


def wait_for(predicate: Any, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def onboard_and_order(h: Harness) -> UUID:
    assert h.webhook("start") == 200
    assert h.webhook("own_contact") == 200
    assert h.webhook("excuse") == 200
    wait_for(lambda: h.dialer.placed)
    call_id: UUID = h.dialer.placed[0]["call_id"]
    return call_id


def test_bad_secret_is_401_and_not_processed(harness: Harness) -> None:
    assert harness.webhook("start", secret="wrong") == 401
    assert harness.telegram_requests == []


def test_start_sends_request_contact_keyboard(harness: Harness) -> None:
    harness.webhook("start")
    request = harness.telegram_requests[0]
    assert request["chat_id"] == 4242
    button = request["reply_markup"]["keyboard"][0][0]
    assert button == {"text": "Compartir mi número", "request_contact": True}


def test_start_shows_the_acceptable_use_notice_before_anything_else(harness: Harness) -> None:
    """Sharing the number is the user's way of agreeing; the notice comes with the button."""
    harness.webhook("start")
    text = harness.telegram_requests[0]["text"]
    assert "Al seguir, aceptas" in text
    assert "solo llamo a tu propio teléfono" in text
    assert "nunca la identidad de una persona o una organización reales" in text
    assert "no es un servicio de emergencias" in text


def test_foreign_contact_is_refused(harness: Harness) -> None:
    harness.webhook("start")
    harness.webhook("foreign_contact")
    assert "propio número" in harness.sent_texts()[-1]
    harness.webhook("excuse")
    assert "necesito tu número" in harness.sent_texts()[-1]
    assert harness.dialer.placed == []


def test_duplicate_update_is_processed_once(harness: Harness) -> None:
    harness.webhook("start")
    harness.webhook("start")
    assert len(harness.telegram_requests) == 1


def test_excuse_dials_only_the_verified_number(harness: Harness) -> None:
    onboard_and_order(harness)
    placed = harness.dialer.placed[0]
    assert placed["to"] == "+34600111222"
    relay = ET.fromstring(placed["twiml"]).find("Connect/ConversationRelay")
    assert relay is not None
    assert relay.get("url") == "wss://example.trycloudflare.com/twilio/relay"
    assert (relay.get("welcomeGreeting") or "").startswith("¿Hola? Oye, soy Carmen")
    assert relay.get("language") == "es-ES"
    assert any("Tía Carmen te llama" in text for text in harness.sent_texts())


def test_status_callback_no_answer_notifies_user(harness: Harness) -> None:
    call_id = onboard_and_order(harness)
    query = f"call_id={call_id}"
    ringing = harness.twilio_post("/twilio/status", TWILIO["status_ringing"], query)
    assert ringing.status_code == 204
    response = harness.twilio_post("/twilio/status", TWILIO["status_no_answer"], query)
    assert response.status_code == 204
    assert "No lo cogiste" in harness.sent_texts()[-1]


def test_status_callback_with_bad_signature_is_403(harness: Harness) -> None:
    response = harness.twilio_post(
        "/twilio/status", TWILIO["status_ringing"], "call_id=x", sign=False
    )
    assert response.status_code == 403
    tampered = dict(TWILIO["status_no_answer"])
    url = f"{BASE}/twilio/status?call_id=x"
    signature = RequestValidator(AUTH_TOKEN).compute_signature(url, TWILIO["status_ringing"])
    response = harness.client.post(
        "/twilio/status?call_id=x",
        content=urlencode(tampered),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Twilio-Signature": signature,
        },
    )
    assert response.status_code == 403


def test_connect_action_returns_hangup(harness: Harness) -> None:
    response = harness.twilio_post("/twilio/connect-action", TWILIO["connect_action"])
    assert response.status_code == 200
    assert ET.fromstring(response.text).find("Hangup") is not None


def relay_headers(signature: str | None) -> dict[str, str]:
    return {"x-twilio-signature": signature} if signature else {}


def test_relay_handshake_without_signature_is_rejected(harness: Harness) -> None:
    with (
        pytest.raises(WebSocketDisconnect),
        harness.client.websocket_connect("/twilio/relay") as ws,
    ):
        ws.receive_json()


def test_relay_call_with_signed_handshake(harness: Harness) -> None:
    call_id = onboard_and_order(harness)
    signature = RequestValidator(AUTH_TOKEN).compute_signature(
        "wss://example.trycloudflare.com/twilio/relay", {}
    )
    setup = {**TWILIO["relay_setup"], "customParameters": {"call_id": str(call_id)}}
    with harness.client.websocket_connect("/twilio/relay", headers=relay_headers(signature)) as ws:
        ws.send_json(setup)
        ws.send_json({"type": "prompt", "voicePrompt": "¿Sí? Dime.", "lang": "es-ES", "last": True})
        tokens = []
        while True:
            frame = ws.receive_json()
            tokens.append(frame["token"])
            if frame["last"]:
                break
        assert "".join(tokens) == "Es que me he dejado las llaves dentro."
    wait_for(lambda: _outcome(harness, call_id) is CallOutcome.IN_PROGRESS)


def test_relay_rejects_wrong_call_sid(harness: Harness) -> None:
    call_id = onboard_and_order(harness)
    wait_for(lambda: _sid_set(harness))
    signature = RequestValidator(AUTH_TOKEN).compute_signature(
        "wss://example.trycloudflare.com/twilio/relay", {}
    )
    setup = {
        **TWILIO["relay_setup"],
        "callSid": "CA-someone-else",
        "customParameters": {"call_id": str(call_id)},
    }
    with harness.client.websocket_connect("/twilio/relay", headers=relay_headers(signature)) as ws:
        ws.send_json(setup)
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_cancel_after_dial_hangs_up(harness: Harness) -> None:
    onboard_and_order(harness)
    wait_for(lambda: _sid_set(harness))
    harness.webhook("cancel")
    assert harness.dialer.hung_up == [CALL_SID]
    assert harness.sent_texts()[-1] == "Anulada."


def _outcome(h: Harness, call_id: UUID) -> CallOutcome | None:
    record = h.client.portal.call(h.client.app.state.services.calls.get, call_id)  # type: ignore[union-attr,attr-defined]
    return None if record is None else record.outcome


def _sid_set(h: Harness) -> bool:
    call_id = h.dialer.placed[0]["call_id"]
    record = h.client.portal.call(h.client.app.state.services.calls.get, call_id)  # type: ignore[union-attr,attr-defined]
    return record is not None and record.twilio_call_sid == CALL_SID


def test_daily_limit_and_one_call_at_a_time(harness: Harness) -> None:
    """A second excuse while a call is pending is refused; once it completes, the daily cap applies."""
    settings = harness.client.app.state.services.settings  # type: ignore[attr-defined]
    settings.max_calls_per_user_per_day = 1
    call_id = onboard_and_order(harness)
    harness.webhook("excuse_again")
    assert "Ya hay una llamada en marcha" in harness.sent_texts()[-1]
    completed = {**TWILIO["status_no_answer"], "CallStatus": "completed"}
    assert harness.twilio_post("/twilio/status", completed, f"call_id={call_id}").status_code == 204
    harness.webhook("excuse_third")
    assert "Ya van 1 llamadas" in harness.sent_texts()[-1]
    assert len(harness.dialer.placed) == 1
