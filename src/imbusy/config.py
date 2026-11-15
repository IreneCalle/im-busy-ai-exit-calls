"""Settings loaded from the environment (and `.env` in development)."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class TtsProvider(StrEnum):
    """Values accepted by the `ttsProvider` attribute of `<ConversationRelay>`."""

    GOOGLE = "Google"
    AMAZON = "Amazon"
    ELEVENLABS = "ElevenLabs"


class Settings(BaseSettings):
    """All runtime configuration. Optional groups unlock features when complete."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    # Anthropic
    anthropic_api_key: SecretStr | None = None
    llm_fast_model: str = "claude-haiku-5-5"
    llm_strong_model: str = "claude-sonnet-5-5"
    llm_max_retries: int = Field(default=2, ge=0, le=10)

    # App
    app_env: AppEnv = AppEnv.DEVELOPMENT
    log_level: str = "INFO"
    public_base_url: str | None = None
    database_path: Path = Path("./data/imbusy.sqlite3")
    redis_url: str | None = None
    web_dist: Path | None = Field(default=None, alias="IMBUSY_WEB_DIST")
    # Telegram does not report the user's timezone; "at 18:30" is read in this zone.
    default_timezone: str = "Europe/Madrid"

    # Demo
    demo_enabled: bool = True
    demo_max_sessions_per_hour: int = Field(default=10, ge=0)

    # Twilio
    twilio_account_sid: str | None = None
    twilio_auth_token: SecretStr | None = None
    twilio_from_number: str | None = None
    twilio_validate_signatures: bool = True
    twilio_tts_provider: TtsProvider = TtsProvider.GOOGLE
    twilio_ring_timeout_seconds: int = Field(default=25, ge=5, le=600)

    # Telegram
    telegram_bot_token: SecretStr | None = None
    telegram_webhook_secret: SecretStr | None = None

    # Limits
    max_calls_per_user_per_day: int = Field(default=5, ge=0)
    max_call_delay_seconds: int = Field(default=900, ge=0)
    default_call_delay_seconds: int = Field(default=20, ge=0)

    # Safe word
    # How long a shared location is kept before it is forgotten on its own.
    location_ttl_seconds: int = Field(default=3600, ge=60, le=24 * 3600)
    # How long a trusted-contact invitation link stays valid.
    invite_ttl_hours: int = Field(default=24, ge=1, le=7 * 24)

    @field_validator("public_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not value.startswith("https://"):
            raise ValueError("PUBLIC_BASE_URL must start with https://")
        return value.rstrip("/")

    @field_validator("telegram_webhook_secret")
    @classmethod
    def _check_secret_charset(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or value.get_secret_value() == "":
            return None
        raw = value.get_secret_value()
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
        if not 1 <= len(raw) <= 256 or not set(raw) <= allowed:
            raise ValueError("TELEGRAM_WEBHOOK_SECRET must be 1-256 chars of A-Z a-z 0-9 _ -")
        return value

    @property
    def llm_enabled(self) -> bool:
        return (
            self.anthropic_api_key is not None and self.anthropic_api_key.get_secret_value() != ""
        )

    @property
    def twilio_enabled(self) -> bool:
        return bool(
            self.twilio_account_sid
            and self.twilio_auth_token
            and self.twilio_auth_token.get_secret_value()
            and self.twilio_from_number
        )

    @property
    def telegram_enabled(self) -> bool:
        return bool(
            self.telegram_bot_token
            and self.telegram_bot_token.get_secret_value()
            and self.telegram_webhook_secret
        )

    @property
    def real_calls_enabled(self) -> bool:
        """Real calls need Twilio, Telegram and a public URL together."""
        return self.twilio_enabled and self.telegram_enabled and self.public_base_url is not None

    @property
    def public_wss_base(self) -> str | None:
        """`wss://` origin derived from PUBLIC_BASE_URL, as written into the TwiML."""
        if self.public_base_url is None:
            return None
        return "wss://" + self.public_base_url.removeprefix("https://")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Tests construct `Settings(...)` directly instead."""
    return Settings()
