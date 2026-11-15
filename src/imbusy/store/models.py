"""Stored records. Kept deliberately small: no transcripts of real calls are persisted."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field

from imbusy.excuse.schema import Language


class UserRecord(BaseModel):
    telegram_user_id: int
    chat_id: int
    phone_e164: str | None = None
    phone_verified_at: datetime | None = None
    language: Language | None = None
    display_name: str | None = None
    # Normalised safe word. Kept out of repr so it never reaches a log line by accident.
    safe_word: str | None = Field(default=None, repr=False)
    created_at: datetime

    @property
    def is_verified(self) -> bool:
        return self.phone_e164 is not None and self.phone_verified_at is not None


class TrustedContact(BaseModel):
    """Someone who opened the user's invitation link and accepted. One per user."""

    user_id: int
    contact_user_id: int
    contact_chat_id: int
    contact_name: str
    accepted_at: datetime


class Invite(BaseModel):
    """A trusted-contact invitation. The token travels in a t.me deep link."""

    token: str
    inviter_id: int
    created_at: datetime
    expires_at: datetime
    contact_user_id: int | None = None
    contact_chat_id: int | None = None
    contact_name: str | None = None


class CallOutcome(StrEnum):
    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"
    DIALING = "dialing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED = "failed"


class CallRecord(BaseModel):
    call_id: UUID
    telegram_user_id: int
    caller_name: str
    language: Language
    scheduled_for: datetime
    twilio_call_sid: str | None = None
    outcome: CallOutcome = CallOutcome.SCHEDULED
    turns: int = 0
    first_token_p50_ms: float | None = None
    prompt_version: int
    created_at: datetime
    ended_at: datetime | None = None
