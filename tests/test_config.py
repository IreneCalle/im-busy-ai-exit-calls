"""Settings: which modes turn on with which variables."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from imbusy.config import Settings


def test_demo_only_with_no_keys(settings: Settings) -> None:
    """With nothing configured, real calls are off and the LLM is reported as missing."""
    assert not settings.llm_enabled
    assert not settings.real_calls_enabled


def test_real_calls_need_twilio_telegram_and_public_url(real_call_settings: Settings) -> None:
    assert real_call_settings.real_calls_enabled
    no_url = real_call_settings.model_copy(update={"public_base_url": None})
    assert not no_url.real_calls_enabled


def test_public_url_must_be_https_and_loses_trailing_slash() -> None:
    s = Settings(_env_file=None, public_base_url="https://abc.ngrok-free.app/")  # type: ignore[call-arg]
    assert s.public_base_url == "https://abc.ngrok-free.app"
    assert s.public_wss_base == "wss://abc.ngrok-free.app"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, public_base_url="http://abc.ngrok-free.app")  # type: ignore[call-arg]


def test_telegram_secret_charset_is_enforced() -> None:
    """Telegram accepts only A-Z a-z 0-9 _ - in secret_token; fail at startup, not at setWebhook."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, telegram_webhook_secret="has space")  # type: ignore[call-arg]
