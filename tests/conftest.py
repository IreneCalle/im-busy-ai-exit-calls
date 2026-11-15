"""Shared fixtures. No test touches the network or needs a key."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from imbusy.config import Settings, TtsProvider
from imbusy.excuse.schema import (
    CallScript,
    ExcuseDraft,
    ExcuseRequest,
    Language,
    Register,
    VoiceGender,
)
from imbusy.llm import FakeLLM


@pytest.fixture(autouse=True)
def _no_env_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's .env and shell variables out of tests."""
    for name in (
        "ANTHROPIC_API_KEY",
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_FROM_NUMBER",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_WEBHOOK_SECRET",
        "PUBLIC_BASE_URL",
        "REDIS_URL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, twilio_tts_provider=TtsProvider.GOOGLE)  # type: ignore[call-arg]


@pytest.fixture
def real_call_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        anthropic_api_key="test-key",
        twilio_account_sid="AC" + "0" * 32,
        twilio_auth_token="test-auth-token",
        twilio_from_number="+15005550006",
        telegram_bot_token="123:test",
        telegram_webhook_secret="test_secret-1",
        public_base_url="https://example.trycloudflare.com",
    )


@pytest.fixture
def script() -> CallScript:
    return CallScript(
        caller_name="Tía Carmen",
        relationship="aunt",
        speech_register=Register.FAMILY,
        language=Language.ES_ES,
        voice_id="es-f-aoede",
        user_alias="Mari",
        opening_line="¿Mari? Oye, soy Carmen, perdona que te llame ahora, ¿tienes un segundo?",
        situation="Ha bajado a tirar la basura y se le ha cerrado la puerta con las llaves dentro.",
        ask="Que le lleve el juego de llaves de repuesto.",
        urgency=4,
        facts=[
            "las llaves están puestas por dentro",
            "el cerrajero no puede venir hasta las nueve",
            "tu prima Lucía está de viaje",
        ],
        forbidden=[],
    )


@pytest.fixture
def draft() -> ExcuseDraft:
    return ExcuseDraft(
        refusal=None,
        language=Language.ES_ES,
        caller_name="Tía Carmen",
        relationship="tía",
        caller_gender=VoiceGender.FEMALE,
        speech_register=Register.FAMILY,
        user_alias=None,
        situation="Se ha quedado fuera de casa sin llaves.",
        ask="Que le lleve las llaves de repuesto.",
        urgency=7,
        facts=["las llaves están dentro", "el cerrajero tarda dos horas"],
        opening_line="¿Hola? Oye, soy Carmen. ¿Tienes un minuto? Es que me ha pasado una cosa.",
        delay_seconds=120,
    )


@pytest.fixture
def excuse_request() -> ExcuseRequest:
    return ExcuseRequest(
        text="tía Carmen, se ha dejado las llaves dentro, en 2 min",
        source="telegram",
        received_at=datetime(2026, 11, 13, 17, 45, tzinfo=UTC),
    )


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()
