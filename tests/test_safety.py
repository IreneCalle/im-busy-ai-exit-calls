"""Design limits: destination, rate limits, script lint."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest

from imbusy.call.voices import CATALOG, VoiceCatalog
from imbusy.config import Settings, TtsProvider
from imbusy.excuse.schema import CallScript, ExcuseDraft
from imbusy.safety.guard import DestinationGuard, GuardViolation, ScriptLinter, SlidingWindowLimiter
from imbusy.store.models import UserRecord

NOW = datetime(2026, 11, 13, 18, 0, tzinfo=UTC)


def test_destination_is_always_the_users_verified_number() -> None:
    user = UserRecord(
        telegram_user_id=1,
        chat_id=1,
        phone_e164="+34600111222",
        phone_verified_at=NOW,
        created_at=NOW,
    )
    assert DestinationGuard().destination_for(user) == "+34600111222"


def test_unverified_user_cannot_be_called() -> None:
    user = UserRecord(telegram_user_id=1, chat_id=1, created_at=NOW)
    with pytest.raises(GuardViolation) as info:
        DestinationGuard().destination_for(user)
    assert info.value.code == "unverified"


def test_sliding_window_limits_and_recovers() -> None:
    limiter = SlidingWindowLimiter(limit=2, window=timedelta(hours=1))
    assert limiter.allow("1.2.3.4", NOW)
    assert limiter.allow("1.2.3.4", NOW + timedelta(minutes=1))
    assert not limiter.allow("1.2.3.4", NOW + timedelta(minutes=2))
    assert limiter.allow("5.6.7.8", NOW + timedelta(minutes=2))
    assert limiter.allow("1.2.3.4", NOW + timedelta(hours=1, seconds=1))


def test_linter_passes_ordinary_script(script: CallScript) -> None:
    assert ScriptLinter().lint(script) == []


def test_linter_flags_authority_impersonation(script: CallScript) -> None:
    bad = script.model_copy(update={"caller_name": "Policía Nacional"})
    issues = ScriptLinter().lint(bad)
    assert [i.field for i in issues] == ["caller_name"]


def test_linter_matches_whole_words_only(script: CallScript) -> None:
    """'robotizado' is not 'robot'."""
    fine = script.model_copy(
        update={"situation": "El aspirador robotizado se ha atascado en la escalera."}
    )
    assert ScriptLinter().lint(fine) == []


# --- design limits that make misuse structurally hard -------------------------------------------------------------------------------


def test_there_is_no_way_to_pass_a_destination_number() -> None:
    """The only number ever dialled is derived from the user record."""
    assert list(inspect.signature(DestinationGuard.destination_for).parameters) == ["self", "user"]
    assert not any("phone" in name or "number" in name for name in ExcuseDraft.model_fields)
    assert not any("phone" in name or "number" in name for name in CallScript.model_fields)


def test_voices_come_only_from_the_stock_catalogue(script: CallScript) -> None:
    """No field anywhere takes a voice sample or a voice id from the model or the user."""
    assert not any("voice" in name for name in ExcuseDraft.model_fields)
    catalog = VoiceCatalog(TtsProvider.GOOGLE)
    assert catalog.get(script.voice_id) in CATALOG
    with pytest.raises(KeyError):
        catalog.get("uploaded-sample")


def test_daily_call_limit_is_on_by_default() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert 1 <= settings.max_calls_per_user_per_day <= 10
    assert settings.demo_max_sessions_per_hour >= 1
