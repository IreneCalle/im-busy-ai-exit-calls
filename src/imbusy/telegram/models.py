"""The subset of Bot API types this bot reads and writes (Bot API 10.x).

Unknown fields are ignored: Telegram adds fields without notice.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _TgModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class User(_TgModel):
    id: int
    is_bot: bool = False
    first_name: str
    language_code: str | None = None


class Chat(_TgModel):
    id: int
    type: str  # "private" | "group" | "supergroup" | "channel"


class Contact(_TgModel):
    phone_number: str
    first_name: str
    last_name: str | None = None
    user_id: int | None = None


class Location(_TgModel):
    latitude: float
    longitude: float
    horizontal_accuracy: float | None = None
    live_period: int | None = None  # seconds from the message date; only for live locations


class Message(_TgModel):
    message_id: int
    from_: User | None = Field(default=None, alias="from")
    chat: Chat
    date: int
    text: str | None = None
    contact: Contact | None = None
    location: Location | None = None
    edit_date: int | None = None


class Update(_TgModel):
    update_id: int
    message: Message | None = None
    edited_message: Message | None = None


class KeyboardButton(_TgModel):
    text: str
    request_contact: bool | None = None
    request_location: bool | None = None


class ReplyKeyboardMarkup(_TgModel):
    keyboard: list[list[KeyboardButton]]
    resize_keyboard: bool | None = None
    one_time_keyboard: bool | None = None
    input_field_placeholder: str | None = None


class ReplyKeyboardRemove(_TgModel):
    remove_keyboard: bool = True


ReplyMarkup = ReplyKeyboardMarkup | ReplyKeyboardRemove


def is_own_contact(message: Message) -> bool:
    """True only when the shared contact is the sender's own Telegram account.

    A `request_contact` button shares the user's own number with `user_id` equal to the sender.
    Contacts forwarded or typed in from the address book either lack `user_id` or carry someone
    else's, so they are rejected. This is what keeps the bot from ever calling a third party.
    """
    if message.contact is None or message.from_ is None:
        return False
    return message.contact.user_id is not None and message.contact.user_id == message.from_.id


def normalise_phone(raw: str) -> str:
    """E.164 from Telegram's `phone_number`, which may omit the leading '+'.

    Raises `ValueError` for anything that is not 8-15 digits after stripping separators.
    """
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not 8 <= len(digits) <= 15:
        raise ValueError("not a plausible E.164 number")
    return "+" + digits
