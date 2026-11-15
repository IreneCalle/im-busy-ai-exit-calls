"""Turning a Telegram update into an intent. Pure, so both bots share it and tests pin it."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from imbusy.excuse.schema import Language
from imbusy.telegram.models import Message, Update


class Intent(StrEnum):
    START = "start"
    HELP = "help"
    CANCEL = "cancel"
    FORGET = "forget"
    OWN_CONTACT = "own_contact"
    FOREIGN_CONTACT = "foreign_contact"
    EXCUSE = "excuse"
    # Safe word and trusted contact
    SAFEWORD = "safeword"
    TRUSTED = "trusted"
    INVITE_OPENED = "invite_opened"  # /start trust_<token> from the person being invited
    ACCEPT = "accept"
    DECLINE = "decline"
    LEAVE = "leave"
    LOCATION_REQUEST = "location_request"
    LOCATION = "location"  # a shared location, or a live location update
    FORGET_LOCATION = "forget_location"
    IGNORE = "ignore"  # non-private chats, edits of text, stickers, bots


@dataclass(frozen=True)
class Classified:
    intent: Intent
    chat_id: int
    user_id: int | None
    text: str | None = None
    argument: str | None = None


_COMMANDS = {
    "/start": Intent.START,
    "/help": Intent.HELP,
    "/cancel": Intent.CANCEL,
    "/forget": Intent.FORGET,
    "/safeword": Intent.SAFEWORD,
    "/trusted": Intent.TRUSTED,
    "/accept": Intent.ACCEPT,
    "/decline": Intent.DECLINE,
    "/leave": Intent.LEAVE,
    "/location": Intent.LOCATION_REQUEST,
    "/forgetlocation": Intent.FORGET_LOCATION,
}

INVITE_PREFIX = "trust_"


def classify(update: Update) -> Classified | None:
    """Map an update to an intent. Returns None for updates without a message.

    Edited messages matter only when they carry a location: that is how Telegram delivers the
    updates of a live location. Every other edit is ignored.
    """
    message: Message | None = update.message or update.edited_message
    if message is None:
        return None
    user_id = message.from_.id if message.from_ else None
    if message.chat.type != "private" or (message.from_ is not None and message.from_.is_bot):
        return Classified(Intent.IGNORE, message.chat.id, user_id)
    if message.location is not None:
        return Classified(Intent.LOCATION, message.chat.id, user_id)
    if update.message is None:
        return Classified(Intent.IGNORE, message.chat.id, user_id)
    if message.contact is not None:
        own = message.contact.user_id is not None and message.contact.user_id == user_id
        intent = Intent.OWN_CONTACT if own else Intent.FOREIGN_CONTACT
        return Classified(intent, message.chat.id, user_id)
    if message.text:
        text = message.text.strip()
        if text.startswith("/"):
            head, _, rest = text.partition(" ")
            command = head.split("@")[0].lower()
            argument = rest.strip() or None
            intent = _COMMANDS.get(command, Intent.HELP)
            if intent is Intent.START and argument and argument.startswith(INVITE_PREFIX):
                token = argument.removeprefix(INVITE_PREFIX)
                return Classified(Intent.INVITE_OPENED, message.chat.id, user_id, argument=token)
            return Classified(intent, message.chat.id, user_id, argument=argument)
        return Classified(Intent.EXCUSE, message.chat.id, user_id, text=text)
    return Classified(Intent.IGNORE, message.chat.id, user_id)


def language_of(message: Message) -> Language:
    code = (message.from_.language_code if message.from_ else None) or ""
    return Language.ES_ES if code.lower().startswith("es") else Language.EN_US
