"""Contracts between the excuse compiler, the store and the call session.

`ExcuseDraft` is what the model returns: flat, simple types that structured outputs handle well.
`CallScript` is what the rest of the system uses: validated, bounded, with a catalog voice chosen
in code. The model never picks a voice id or a phone number.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
SpokenLine = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]


class Language(StrEnum):
    """Call languages supported in the MVP (BCP-47, as ConversationRelay expects)."""

    ES_ES = "es-ES"
    EN_US = "en-US"


class Register(StrEnum):
    """How the caller speaks to the user."""

    FAMILY = "family"
    WORK = "work"
    FORMAL = "formal"


class VoiceGender(StrEnum):
    FEMALE = "female"
    MALE = "male"


class RefusalKind(StrEnum):
    """Why the compiler declined to build a script."""

    IMPERSONATES_AUTHORITY = "impersonates_authority"  # police, emergency services, courts
    IMPERSONATES_REAL_PERSON = "impersonates_real_person"  # a named public figure
    TARGETS_THIRD_PARTY = "targets_third_party"  # asks to call or deceive someone else
    HARMFUL = "harmful"
    NOT_AN_EXCUSE = "not_an_excuse"  # the message is not a request for a call


class ExcuseDraft(BaseModel):
    """Raw structured output of the compiler model. Field descriptions are part of the prompt."""

    model_config = ConfigDict(extra="forbid")

    refusal: RefusalKind | None = Field(
        description="Set only if the request must not become a call; otherwise null."
    )
    language: Language = Field(
        description="Language the user wrote in, or asked the call to be in."
    )
    caller_name: str = Field(
        description="How the caller is named, e.g. 'Tía Carmen', 'Marta from work'."
    )
    relationship: str = Field(description="Relationship to the user, e.g. 'aunt', 'manager'.")
    caller_gender: VoiceGender = Field(description="Picks a matching catalog voice.")
    speech_register: Register
    user_alias: str | None = Field(
        description="How the caller addresses the user (nickname or first name), null if unknown."
    )
    situation: str = Field(
        description="What is going on, in one or two sentences, in the call language."
    )
    ask: str = Field(description="The concrete thing the caller asks the user to do.")
    urgency: int = Field(description="1 (mild) to 5 (drop everything).")
    facts: list[str] = Field(
        description="Two to four small invented specifics (times, places, names) to stay consistent."
    )
    opening_line: str = Field(
        description="First sentence the caller says when the user picks up. Under 20 words."
    )
    delay_seconds: int | None = Field(
        description="Seconds until the call if the user said when ('in 2 min' = 120), else null."
    )


class CallScript(BaseModel):
    """Everything the call session needs. Immutable once compiled."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    caller_name: ShortText
    relationship: ShortText
    speech_register: Register
    language: Language
    voice_id: str  # key into imbusy.call.voices.CATALOG
    user_alias: ShortText | None = None
    opening_line: SpokenLine
    situation: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ]
    ask: ShortText
    urgency: Annotated[int, Field(ge=1, le=5)]
    facts: Annotated[list[ShortText], Field(max_length=6)] = []
    forbidden: list[str] = []
    max_turns: Annotated[int, Field(ge=3, le=8)] = 5


class ExcuseRequest(BaseModel):
    """A user's free-text message, as received from Telegram or the demo page."""

    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]
    source: Literal["telegram", "demo"]
    preferred_language: Language | None = None
    received_at: datetime
    timezone: str | None = None  # IANA name; the browser sends its own, Telegram uses the default


class CompiledExcuse(BaseModel):
    """Compiler output handed to the scheduler: the script plus when to ring."""

    call_id: UUID = Field(default_factory=uuid4)
    script: CallScript
    delay_seconds: Annotated[int, Field(ge=0)]
    prompt_version: int


class CompileRefused(BaseModel):
    """Compiler outcome when the request is declined; `message` is shown to the user."""

    kind: RefusalKind
    message: str


# Rules the caller persona must always follow, regardless of what the excuse says.
# Rendered into the system prompt; checked again by imbusy.safety.guard on compiled scripts.
BASE_FORBIDDEN: tuple[str, ...] = (
    "Never say or imply you are an AI, a bot, a recording or a service.",
    "Never claim to be police, emergency services, a court, a bank or any authority.",
    "Never give specific medical diagnoses, medication names or doses.",
    "Never mention money transfers, codes, passwords or personal data.",
    "Never name real public figures.",
)
