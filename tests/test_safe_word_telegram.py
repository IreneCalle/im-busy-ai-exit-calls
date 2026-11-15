"""Safe word setup in Telegram and the alert during a real-call relay session.

Telegram is an `httpx.MockTransport` that records every Bot API request and answers like the real
API; updates come from tests/fixtures/telegram_safety_updates.json. No network.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from imbusy.call.dialer import PlacedCall, TwilioCallStatus
from imbusy.config import Settings
from imbusy.excuse.schema import ExcuseDraft
from imbusy.llm import FakeLLM
from imbusy.main import create_app
from imbusy.store.locations import location_key
from imbusy.telegram.client import TelegramClient

FIXTURES = Path(__file__).parent / "fixtures"
CALL_UPDATES = json.loads((FIXTURES / "telegram_updates.json").read_text(encoding="utf-8"))
SAFETY = json.loads((FIXTURES / "telegram_safety_updates.json").read_text(encoding="utf-8"))
TWILIO = json.loads((FIXTURES / "twilio_requests.json").read_text(encoding="utf-8"))
SECRET = "test_secret-1"
CALL_SID = TWILIO["relay_setup"]["callSid"]
MARI, ANA = 4242, 5151


@dataclass
class Dialer:
    placed: list[dict[str, Any]] = field(default_factory=list)

    async def place(self, *, to: str, twiml: str, call_id: UUID) -> PlacedCall:
        self.placed.append({"to": to, "call_id": call_id})
        return PlacedCall(call_sid=CALL_SID, status=TwilioCallStatus.QUEUED)

    async def hang_up(self, call_sid: str) -> None:
        return None


@dataclass
class Bot:
    client: TestClient
    sent: list[dict[str, Any]]
    dialer: Dialer

    def post(self, update: dict[str, Any]) -> None:
        response = self.client.post(
            "/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": SECRET}
        )
        assert response.status_code == 200

    def to(self, chat_id: int, method: str = "sendMessage") -> list[dict[str, Any]]:
        return [r for r in self.sent if r["method"] == method and r.get("chat_id") == chat_id]

    def last_text(self, chat_id: int) -> str:
        return str(self.to(chat_id)[-1]["text"])

    def services(self) -> Any:
        return self.client.app.state.services  # type: ignore[attr-defined]

    def run(self, coro: Any) -> Any:
        return self.client.portal.call(lambda: coro)  # type: ignore[union-attr]


@pytest.fixture
def bot(tmp_path: Path, real_call_settings: Settings, draft: ExcuseDraft) -> Iterator[Bot]:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        sent.append({"method": method, **body})
        if method == "getMe":
            me = {"id": 1, "is_bot": True, "first_name": "I'm Busy", "username": "ImBusyTestBot"}
            return httpx.Response(200, json={"ok": True, "result": me})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})

    telegram = TelegramClient(
        "123:test", http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    llm = FakeLLM(
        structured_responses=[draft.model_copy(update={"delay_seconds": 0})],
        text_replies=[
            "Es que me he dejado las llaves dentro.",
            "Bueno, que me quedo sin batería.",
            "Un beso.",
        ],
    )
    settings = real_call_settings.model_copy(
        update={"database_path": tmp_path / "db.sqlite3", "web_dist": tmp_path / "none"}
    )
    dialer = Dialer()
    with TestClient(create_app(settings, llm, dialer=dialer, telegram=telegram)) as client:
        yield Bot(client, sent, dialer)


def invite_token(bot: Bot) -> str:
    link = bot.last_text(MARI)
    return link.rsplit("start=trust_", 1)[1]


def connect_contact(bot: Bot) -> None:
    bot.post(SAFETY["trusted"])
    opened = json.loads(json.dumps(SAFETY["invite_opened"]))
    opened["message"]["text"] = f"/start trust_{invite_token(bot)}"
    bot.post(opened)
    bot.post(SAFETY["accept"])


def test_setting_the_safe_word_never_echoes_it(bot: Bot) -> None:
    bot.post(SAFETY["safeword_set"])
    reply = bot.last_text(MARI)
    assert "Palabra guardada" in reply
    assert "flamingo" not in reply.lower()
    assert "no es un servicio de emergencias" in reply.lower() and "112" in reply
    user = bot.run(bot.services().users.get(MARI))
    assert user.safe_word == "pink flamingo"


def test_a_single_word_is_refused_with_the_reason(bot: Bot) -> None:
    bot.post(SAFETY["safeword_bad"])
    assert "al menos dos palabras" in bot.last_text(MARI)
    assert bot.run(bot.services().users.get(MARI)).safe_word is None


def test_setup_message_says_it_is_not_an_emergency_service(bot: Bot) -> None:
    bot.post(SAFETY["safeword_status"])
    reply = bot.last_text(MARI)
    assert "no es un servicio de emergencias" in reply.lower()
    assert "Palabra de seguridad: sin poner" in reply


def test_invitation_is_a_deep_link_with_a_start_parameter(bot: Bot) -> None:
    bot.post(SAFETY["trusted"])
    assert [r["method"] for r in bot.sent][:2] == ["getMe", "sendMessage"]
    token = invite_token(bot)
    assert bot.last_text(MARI).startswith("Mari quiere que seas su contacto de confianza")
    # Deep-link parameters allow A-Z a-z 0-9 _ - and up to 64 characters.
    payload = "trust_" + token
    assert len(payload) <= 64
    assert all(ch.isalnum() or ch in "_-" for ch in payload)


def test_the_contact_consents_before_anything_can_be_sent_to_them(bot: Bot) -> None:
    bot.post(SAFETY["trusted"])
    opened = json.loads(json.dumps(SAFETY["invite_opened"]))
    opened["message"]["text"] = f"/start trust_{invite_token(bot)}"
    bot.post(opened)
    consent = bot.last_text(ANA)
    assert "Mari te ha pedido que seas su contacto de confianza" in consent
    assert "/accept" in consent and "no es un servicio de emergencias" in consent.lower()
    assert bot.run(bot.services().trust.contact_for(MARI)) is None
    bot.post(SAFETY["accept"])
    assert "Eres el contacto de confianza de Mari" in bot.last_text(ANA)
    assert "Ana ha aceptado" in bot.last_text(MARI)
    contact = bot.run(bot.services().trust.contact_for(MARI))
    assert (contact.contact_user_id, contact.contact_chat_id) == (ANA, ANA)


def test_an_unknown_invitation_is_refused(bot: Bot) -> None:
    bot.post(SAFETY["invite_opened"])
    assert "caducado" in bot.last_text(ANA)


def test_contact_can_leave_and_the_user_is_told(bot: Bot) -> None:
    connect_contact(bot)
    bot.post(SAFETY["leave"])
    assert bot.last_text(ANA) == "Ya no eres contacto de confianza de nadie."
    assert "Ana ha dejado de ser tu contacto de confianza" in bot.last_text(MARI)
    assert bot.run(bot.services().trust.contact_for(MARI)) is None


def test_one_off_and_live_locations_are_stored_with_a_ttl(bot: Bot) -> None:
    store = bot.services().locations
    bot.post(SAFETY["location_once"])
    assert "La olvidaré a las" in bot.last_text(MARI)
    first = bot.run(store.get(location_key(MARI)))
    assert (first.latitude, first.accuracy_m, first.live) == (40.416775, 18.4, False)
    bot.post(SAFETY["location_live_start"])
    replies_before = len(bot.to(MARI))
    bot.post(SAFETY["location_live_update"])
    assert len(bot.to(MARI)) == replies_before  # live updates are stored silently
    latest = bot.run(store.get(location_key(MARI)))
    assert (latest.latitude, latest.live) == (40.4201, True)


def test_edited_text_messages_are_ignored(bot: Bot) -> None:
    bot.post(SAFETY["safeword_set"])
    count = len(bot.sent)
    bot.post(SAFETY["text_edit"])
    assert len(bot.sent) == count
    assert bot.run(bot.services().users.get(MARI)).safe_word == "pink flamingo"


def test_forget_location_deletes_it_now(bot: Bot) -> None:
    bot.post(SAFETY["location_once"])
    bot.post(SAFETY["forget_location"])
    assert bot.last_text(MARI) == "Ubicación borrada."
    assert bot.run(bot.services().locations.get(location_key(MARI))) is None


def test_location_request_offers_a_request_location_button(bot: Bot) -> None:
    update = json.loads(json.dumps(SAFETY["forget_location"]))
    update["update_id"] += 100
    update["message"]["text"] = "/location"
    bot.post(update)
    request = bot.to(MARI)[-1]
    button = request["reply_markup"]["keyboard"][0][0]
    assert button == {"text": "Compartir mi ubicación", "request_location": True}
    assert "60 minutos" in request["text"]


def wait_for(predicate: Any, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def test_safe_word_on_the_phone_alerts_the_contact_and_the_call_goes_on(bot: Bot) -> None:
    for name in ("start", "own_contact"):
        bot.post(CALL_UPDATES[name])
    bot.post(SAFETY["safeword_set"])
    connect_contact(bot)
    bot.post(SAFETY["location_once"])
    bot.post(CALL_UPDATES["excuse"])
    wait_for(lambda: bot.dialer.placed)
    call_id = bot.dialer.placed[0]["call_id"]
    wait_for(lambda: bot.run(bot.services().calls.get(call_id)).twilio_call_sid == CALL_SID)
    signature = RequestValidator("test-auth-token").compute_signature(
        "wss://example.trycloudflare.com/twilio/relay", {}
    )
    ana_before = len(bot.to(ANA))
    mari_before = len(bot.to(MARI))
    setup = {**TWILIO["relay_setup"], "customParameters": {"call_id": str(call_id)}}
    with bot.client.websocket_connect(
        "/twilio/relay", headers={"x-twilio-signature": signature}
    ) as ws:
        ws.send_json(setup)
        ws.send_json({"type": "prompt", "voicePrompt": "¿Sí?", "last": True})
        reply = []
        while not (frame := ws.receive_json()).get("last"):
            reply.append(frame["token"])
        ws.send_json(
            {"type": "prompt", "voicePrompt": "Ay. Pink flamingo. ¿Y qué pasa?", "last": True}
        )
        tokens = []
        while True:
            frame = ws.receive_json()
            tokens.append(frame["token"])
            if frame["last"]:
                break
        assert "".join(tokens) == "Bueno, que me quedo sin batería."
        wait_for(lambda: len(bot.to(ANA)) > ana_before)
    alert = bot.to(ANA)[-1]["text"]
    assert alert.startswith("Mari ha dicho su palabra de seguridad durante una llamada")
    assert "openstreetmap.org/?mlat=40.41678&mlon=-3.70379" in alert
    assert "No es un servicio de emergencias" in alert
    pin = bot.to(ANA, "sendLocation")[-1]
    assert (pin["latitude"], pin["longitude"], pin["horizontal_accuracy"]) == (
        40.416775,
        -3.70379,
        18.4,
    )
    # Nothing shows up on the user's own screen while people may be watching.
    assert len(bot.to(MARI)) == mari_before


def test_forget_also_removes_the_location_and_the_trusted_contact(bot: Bot) -> None:
    connect_contact(bot)
    bot.post(SAFETY["location_once"])
    update = json.loads(json.dumps(SAFETY["forget_location"]))
    update["update_id"] += 200
    update["message"]["text"] = "/forget"
    bot.post(update)
    assert bot.run(bot.services().locations.get(location_key(MARI))) is None
    assert bot.run(bot.services().trust.contact_for(MARI)) is None
