"""Telegram update parsing, intent classification and the bot flow."""

from __future__ import annotations

import pytest

from imbusy.telegram.intents import Intent, classify
from imbusy.telegram.models import Update, is_own_contact, normalise_phone


def _update(message: dict[str, object]) -> Update:
    base = {"message_id": 1, "date": 1763050000, "chat": {"id": 42, "type": "private"}}
    return Update.model_validate({"update_id": 1000, "message": {**base, **message}})


SENDER = {"id": 42, "is_bot": False, "first_name": "Mari"}


def test_own_contact_is_accepted() -> None:
    update = _update(
        {
            "from": SENDER,
            "contact": {"phone_number": "34600111222", "first_name": "Mari", "user_id": 42},
        }
    )
    assert update.message is not None
    assert is_own_contact(update.message)
    classified = classify(update)
    assert classified is not None and classified.intent is Intent.OWN_CONTACT


@pytest.mark.parametrize("user_id", [None, 7])
def test_someone_elses_contact_is_rejected(user_id: int | None) -> None:
    contact: dict[str, object] = {"phone_number": "+34600999888", "first_name": "Other"}
    if user_id is not None:
        contact["user_id"] = user_id
    update = _update({"from": SENDER, "contact": contact})
    assert update.message is not None
    assert not is_own_contact(update.message)
    classified = classify(update)
    assert classified is not None and classified.intent is Intent.FOREIGN_CONTACT


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("/start", Intent.START),
        ("/cancel@ImBusyBot", Intent.CANCEL),
        ("/forget", Intent.FORGET),
        ("/whatever", Intent.HELP),
        ("tía Carmen, llaves, en 2 min", Intent.EXCUSE),
    ],
)
def test_text_intents(text: str, intent: Intent) -> None:
    classified = classify(_update({"from": SENDER, "text": text}))
    assert classified is not None and classified.intent is intent


def test_group_chats_are_ignored() -> None:
    update = Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 0,
                "chat": {"id": -5, "type": "group"},
                "from": SENDER,
                "text": "tía Carmen",
            },
        }
    )
    classified = classify(update)
    assert classified is not None and classified.intent is Intent.IGNORE


@pytest.mark.parametrize(
    ("raw", "expected"), [("34600111222", "+34600111222"), ("+1 (415) 555-0100", "+14155550100")]
)
def test_normalise_phone(raw: str, expected: str) -> None:
    assert normalise_phone(raw) == expected


def test_normalise_phone_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        normalise_phone("12")


@pytest.mark.parametrize(
    ("text", "intent", "argument"),
    [
        ("/safeword pink flamingo", Intent.SAFEWORD, "pink flamingo"),
        ("/safeword", Intent.SAFEWORD, None),
        ("/start trust_Ab-9_x", Intent.INVITE_OPENED, "Ab-9_x"),
        ("/start", Intent.START, None),
        ("/accept", Intent.ACCEPT, None),
        ("/leave", Intent.LEAVE, None),
        ("/forgetlocation", Intent.FORGET_LOCATION, None),
    ],
)
def test_safe_word_commands(text: str, intent: Intent, argument: str | None) -> None:
    classified = classify(_update({"from": SENDER, "text": text}))
    assert classified is not None
    assert (classified.intent, classified.argument) == (intent, argument)


def test_live_location_edits_are_locations_and_other_edits_are_ignored() -> None:
    base = {
        "message_id": 9,
        "date": 1,
        "edit_date": 2,
        "chat": {"id": 42, "type": "private"},
        "from": SENDER,
    }
    live = Update.model_validate(
        {
            "update_id": 1,
            "edited_message": {
                **base,
                "location": {"latitude": 1.0, "longitude": 2.0, "live_period": 900},
            },
        }
    )
    text = Update.model_validate(
        {"update_id": 2, "edited_message": {**base, "text": "/safeword x y"}}
    )
    assert (c := classify(live)) is not None and c.intent is Intent.LOCATION
    assert (c := classify(text)) is not None and c.intent is Intent.IGNORE
