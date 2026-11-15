"""Safe word setup in Telegram: a few commands, each one tap or one line.

    /safeword pink flamingo   set (or /safeword off to clear; /safeword alone shows the status)
    /trusted                  get an invitation link to forward to one person
    t.me/<bot>?start=trust_X  the invited person opens it and presses Start
    /accept, /decline         their answer; the bot can only message them because they started it
    /leave                    stop being anyone's trusted contact
    /location                 a button that shares the current location (or share a live one)
    /forgetlocation           delete the stored location now

Telegram bots cannot start a conversation with someone who has never started the bot, which is
why the contact is added through a link they open themselves: that tap is both their consent and
what lets the bot message them later.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta

from imbusy.config import Settings
from imbusy.excuse.schema import Language
from imbusy.safety.safeword import SafeWordError, parse_safe_word
from imbusy.store.locations import LocationStore, SharedLocation, location_key
from imbusy.store.sqlite import TrustRepository, UserRepository
from imbusy.telegram.client import TelegramClient, TelegramError
from imbusy.telegram.intents import INVITE_PREFIX, Classified, Intent
from imbusy.telegram.models import KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove

logger = logging.getLogger(__name__)

SAFETY_INTENTS = frozenset(
    {
        Intent.SAFEWORD,
        Intent.TRUSTED,
        Intent.INVITE_OPENED,
        Intent.ACCEPT,
        Intent.DECLINE,
        Intent.LEAVE,
        Intent.LOCATION_REQUEST,
        Intent.LOCATION,
        Intent.FORGET_LOCATION,
    }
)

NOT_EMERGENCY = {
    Language.EN_US: (
        "This is not an emergency service. It sends one Telegram message to one person you chose, "
        "and only if the call is actually going and the words are recognised. If you are in danger, "
        "call the local emergency number (112 in Europe, 911 in the US)."
    ),
    Language.ES_ES: (
        "Esto no es un servicio de emergencias. Envía un mensaje de Telegram a una persona que tú "
        "eliges, y solo si la llamada está en marcha y se reconocen las palabras. Si estás en "
        "peligro, llama al 112."
    ),
}

TEXTS: dict[str, dict[Language, str]] = {
    "safeword_help": {
        Language.EN_US: (
            "Choose a safe word: two to four words you would never say by accident, for example "
            "/safeword pink flamingo. If you say it during one of my calls, the caller keeps talking "
            "as if nothing happened, wraps up soon after, and I send your trusted contact a message "
            "with your last shared location.\n\n{status}\n\n{not_emergency}"
        ),
        Language.ES_ES: (
            "Elige una palabra de seguridad: de dos a cuatro palabras que nunca dirías sin querer, por "
            "ejemplo /safeword flamenco rosa. Si la dices durante una de mis llamadas, quien llama sigue "
            "hablando como si nada, cuelga poco después, y envío a tu contacto de confianza un mensaje "
            "con la última ubicación que hayas compartido.\n\n{status}\n\n{not_emergency}"
        ),
    },
    "status": {
        Language.EN_US: "Safe word: {word}. Trusted contact: {contact}. Location: {location}.",
        Language.ES_ES: "Palabra de seguridad: {word}. Contacto de confianza: {contact}. Ubicación: {location}.",
    },
    "status_set": {Language.EN_US: "set", Language.ES_ES: "puesta"},
    "status_unset": {Language.EN_US: "not set", Language.ES_ES: "sin poner"},
    "status_none": {
        Language.EN_US: "none yet (/trusted)",
        Language.ES_ES: "ninguno todavía (/trusted)",
    },
    "status_location_none": {
        Language.EN_US: "none shared (/location)",
        Language.ES_ES: "ninguna compartida (/location)",
    },
    "status_location_set": {
        Language.EN_US: "shared at {time}, kept until {until}",
        Language.ES_ES: "compartida a las {time}, se guarda hasta las {until}",
    },
    "safeword_saved": {
        Language.EN_US: (
            "Safe word saved. I will not repeat it back here, so delete your message if someone might see "
            "this chat.\n\n{next}\n\n{not_emergency}"
        ),
        Language.ES_ES: (
            "Palabra guardada. No la repito aquí, así que borra tu mensaje si alguien puede ver este chat."
            "\n\n{next}\n\n{not_emergency}"
        ),
    },
    "next_contact": {
        Language.EN_US: "Next: /trusted to invite the person who should get the message.",
        Language.ES_ES: "Ahora: /trusted para invitar a la persona que debe recibir el aviso.",
    },
    "next_location": {
        Language.EN_US: "Before a risky moment, use /location so I know where you are.",
        Language.ES_ES: "Antes de un momento delicado, usa /location para que sepa dónde estás.",
    },
    "safeword_off": {
        Language.EN_US: "Safe word removed. Calls will not listen for it any more.",
        Language.ES_ES: "Palabra de seguridad quitada. Las llamadas ya no la escuchan.",
    },
    "too_few_words": {
        Language.EN_US: "Use at least two words, so it cannot come up in normal conversation.",
        Language.ES_ES: "Usa al menos dos palabras, para que no salga en una conversación normal.",
    },
    "too_many_words": {
        Language.EN_US: "Four words at most, so it is easy to say under pressure.",
        Language.ES_ES: "Cuatro palabras como mucho, para que sea fácil de decir con prisa.",
    },
    "too_short": {
        Language.EN_US: "Too short: use at least eight letters in total.",
        Language.ES_ES: "Demasiado corta: usa al menos ocho letras en total.",
    },
    "too_long": {
        Language.EN_US: "Too long: keep it under sixty letters.",
        Language.ES_ES: "Demasiado larga: menos de sesenta letras.",
    },
    "invite": {
        Language.EN_US: (
            "Forward the link below to the one person who should be told if you say your safe word. "
            "When they open it and press Start, they will be asked to accept. The link works for "
            "{hours} hours."
        ),
        Language.ES_ES: (
            "Reenvía el enlace de abajo a la persona a la que hay que avisar si dices tu palabra de "
            "seguridad. Cuando lo abra y pulse Iniciar, le pediré que acepte. El enlace vale {hours} horas."
        ),
    },
    "invite_link": {
        Language.EN_US: "{name} would like you to be their trusted contact on I'm Busy: {url}",
        Language.ES_ES: "{name} quiere que seas su contacto de confianza en I'm Busy: {url}",
    },
    "invite_consent": {
        Language.EN_US: (
            "{name} asked you to be their trusted contact. If {name} says their safe word during an "
            "I'm Busy call, I will send you one message with the time and their last shared location. "
            "That is all: I will not message you otherwise and you can stop at any time with /leave.\n\n"
            "This is not an emergency service. Tap /accept to agree, or /decline."
        ),
        Language.ES_ES: (
            "{name} te ha pedido que seas su contacto de confianza. Si {name} dice su palabra de "
            "seguridad durante una llamada de I'm Busy, te enviaré un mensaje con la hora y su última "
            "ubicación compartida. Nada más: no te escribiré por otra cosa y puedes dejarlo cuando "
            "quieras con /leave.\n\nEsto no es un servicio de emergencias. Pulsa /accept para aceptar, "
            "o /decline."
        ),
    },
    "invite_invalid": {
        Language.EN_US: "That invitation link has expired or was already used. Ask for a new one.",
        Language.ES_ES: "Ese enlace ha caducado o ya se usó. Pide uno nuevo.",
    },
    "accepted_contact": {
        Language.EN_US: "Done. You are {name}'s trusted contact. /leave stops it.",
        Language.ES_ES: "Hecho. Eres el contacto de confianza de {name}. /leave lo deja.",
    },
    "accepted_user": {
        Language.EN_US: "{contact} accepted. They will get the message if you say your safe word.",
        Language.ES_ES: "{contact} ha aceptado. Recibirá el aviso si dices tu palabra de seguridad.",
    },
    "nothing_pending": {
        Language.EN_US: "There is no invitation waiting for you.",
        Language.ES_ES: "No tienes ninguna invitación pendiente.",
    },
    "declined_contact": {
        Language.EN_US: "Declined. I will not message you about it.",
        Language.ES_ES: "Rechazado. No te escribiré por esto.",
    },
    "declined_user": {
        Language.EN_US: "The person you invited declined. You can invite someone else with /trusted.",
        Language.ES_ES: "La persona que invitaste ha rechazado. Puedes invitar a otra con /trusted.",
    },
    "left_contact": {
        Language.EN_US: "You are no longer anyone's trusted contact.",
        Language.ES_ES: "Ya no eres contacto de confianza de nadie.",
    },
    "left_user": {
        Language.EN_US: "{contact} stopped being your trusted contact. Invite someone with /trusted.",
        Language.ES_ES: "{contact} ha dejado de ser tu contacto de confianza. Invita a otra persona con /trusted.",
    },
    "location_request": {
        Language.EN_US: (
            "Tap the button to share where you are now. For a moving location, use the attachment menu: "
            "Location, then Share live location; I keep the latest point. Either way I keep it for "
            "{minutes} minutes, use it only if you say your safe word, and /forgetlocation deletes it."
        ),
        Language.ES_ES: (
            "Pulsa el botón para compartir dónde estás. Si te vas a mover, usa el clip: Ubicación, luego "
            "Compartir ubicación en tiempo real; guardo el último punto. En los dos casos la guardo "
            "{minutes} minutos, solo la uso si dices tu palabra de seguridad, y /forgetlocation la borra."
        ),
    },
    "location_button": {
        Language.EN_US: "Share my location",
        Language.ES_ES: "Compartir mi ubicación",
    },
    "location_saved": {
        Language.EN_US: "Got it. I will forget it at {until} unless you share a newer one.",
        Language.ES_ES: "Recibida. La olvidaré a las {until} salvo que compartas otra más reciente.",
    },
    "location_forgotten": {
        Language.EN_US: "Location deleted.",
        Language.ES_ES: "Ubicación borrada.",
    },
}


def text_for(key: str, language: Language, **values: object) -> str:
    return TEXTS[key][language].format(**values)


class SafetyBot:
    def __init__(
        self,
        *,
        telegram: TelegramClient,
        users: UserRepository,
        trust: TrustRepository,
        locations: LocationStore,
        settings: Settings,
    ) -> None:
        self._tg = telegram
        self._users = users
        self._trust = trust
        self._locations = locations
        self._settings = settings

    async def handle(self, classified: Classified, message: Message, language: Language) -> None:
        user_id = classified.user_id
        assert user_id is not None
        chat_id = classified.chat_id
        match classified.intent:
            case Intent.SAFEWORD:
                await self.on_safeword(chat_id, user_id, classified.argument, language)
            case Intent.TRUSTED:
                await self.on_trusted(chat_id, user_id, language)
            case Intent.INVITE_OPENED:
                await self.on_invite_opened(
                    chat_id, user_id, classified.argument or "", message, language
                )
            case Intent.ACCEPT:
                await self.on_accept(chat_id, user_id, language)
            case Intent.DECLINE:
                await self.on_decline(chat_id, user_id, language)
            case Intent.LEAVE:
                await self.on_leave(chat_id, user_id, language)
            case Intent.LOCATION_REQUEST:
                await self.on_location_request(chat_id, language)
            case Intent.LOCATION:
                await self.on_location(chat_id, user_id, message, language)
            case Intent.FORGET_LOCATION:
                await self._locations.delete(location_key(user_id))
                await self._tg.send_message(chat_id, text_for("location_forgotten", language))

    def _local(self, moment: datetime) -> str:
        from zoneinfo import ZoneInfo

        return moment.astimezone(ZoneInfo(self._settings.default_timezone)).strftime("%H:%M")

    async def status_line(self, user_id: int, language: Language) -> str:
        user = await self._users.get(user_id)
        contact = await self._trust.contact_for(user_id)
        location = await self._locations.get(location_key(user_id))
        word = text_for("status_set" if user and user.safe_word else "status_unset", language)
        contact_label = contact.contact_name if contact else text_for("status_none", language)
        if location is None:
            location_label = text_for("status_location_none", language)
        else:
            until = location.captured_at + timedelta(seconds=self._settings.location_ttl_seconds)
            location_label = text_for(
                "status_location_set",
                language,
                time=self._local(location.captured_at),
                until=self._local(until),
            )
        return text_for(
            "status", language, word=word, contact=contact_label, location=location_label
        )

    async def on_safeword(
        self, chat_id: int, user_id: int, argument: str | None, language: Language
    ) -> None:
        if argument is None:
            status = await self.status_line(user_id, language)
            await self._tg.send_message(
                chat_id,
                text_for(
                    "safeword_help", language, status=status, not_emergency=NOT_EMERGENCY[language]
                ),
            )
            return
        if argument.strip().lower() in {"off", "quitar", "borrar"}:
            await self._users.set_safe_word(user_id, None)
            await self._tg.send_message(chat_id, text_for("safeword_off", language))
            return
        try:
            phrase = parse_safe_word(argument)
        except SafeWordError as exc:
            await self._tg.send_message(chat_id, text_for(exc.reason, language))
            return
        await self._users.set_safe_word(user_id, phrase)
        contact = await self._trust.contact_for(user_id)
        next_step = text_for("next_location" if contact else "next_contact", language)
        await self._tg.send_message(
            chat_id,
            text_for(
                "safeword_saved", language, next=next_step, not_emergency=NOT_EMERGENCY[language]
            ),
        )

    async def on_trusted(self, chat_id: int, user_id: int, language: Language) -> None:
        now = datetime.now(UTC)
        token = secrets.token_urlsafe(24)  # 32 chars of A-Z a-z 0-9 _ -, as deep links allow
        await self._trust.create_invite(
            token, user_id, now, now + timedelta(hours=self._settings.invite_ttl_hours)
        )
        username = await self._tg.bot_username()
        url = f"https://t.me/{username}?start={INVITE_PREFIX}{token}"
        user = await self._users.get(user_id)
        name = (user.display_name if user else None) or "A friend"
        await self._tg.send_message(
            chat_id, text_for("invite", language, hours=self._settings.invite_ttl_hours)
        )
        # A separate message so it can be forwarded on its own.
        await self._tg.send_message(chat_id, text_for("invite_link", language, name=name, url=url))

    async def on_invite_opened(
        self, chat_id: int, contact_id: int, token: str, message: Message, language: Language
    ) -> None:
        name = message.from_.first_name if message.from_ else "?"
        invite = await self._trust.open_invite(token, contact_id, chat_id, name, datetime.now(UTC))
        if invite is None:
            await self._tg.send_message(chat_id, text_for("invite_invalid", language))
            return
        inviter = await self._users.get(invite.inviter_id)
        inviter_name = (inviter.display_name if inviter else None) or "?"
        await self._tg.send_message(
            chat_id, text_for("invite_consent", language, name=inviter_name)
        )

    async def on_accept(self, chat_id: int, contact_id: int, language: Language) -> None:
        invite = await self._trust.accept(contact_id, datetime.now(UTC))
        if invite is None:
            await self._tg.send_message(chat_id, text_for("nothing_pending", language))
            return
        inviter = await self._users.get(invite.inviter_id)
        inviter_name = (inviter.display_name if inviter else None) or "?"
        await self._tg.send_message(
            chat_id, text_for("accepted_contact", language, name=inviter_name)
        )
        if inviter is not None:
            inviter_lang = inviter.language or language
            await self._notify(
                inviter.chat_id,
                text_for("accepted_user", inviter_lang, contact=invite.contact_name or "?")
                + "\n\n"
                + text_for("next_location", inviter_lang),
            )

    async def on_decline(self, chat_id: int, contact_id: int, language: Language) -> None:
        inviters = await self._trust.decline(contact_id)
        if not inviters:
            await self._tg.send_message(chat_id, text_for("nothing_pending", language))
            return
        await self._tg.send_message(chat_id, text_for("declined_contact", language))
        for inviter_id in inviters:
            inviter = await self._users.get(inviter_id)
            if inviter is not None:
                await self._notify(
                    inviter.chat_id, text_for("declined_user", inviter.language or language)
                )

    async def on_leave(self, chat_id: int, contact_id: int, language: Language) -> None:
        user = await self._users.get(contact_id)
        name = (user.display_name if user else None) or "?"
        for inviter_id in await self._trust.leave(contact_id):
            inviter = await self._users.get(inviter_id)
            if inviter is not None:
                await self._notify(
                    inviter.chat_id,
                    text_for("left_user", inviter.language or language, contact=name),
                )
        await self._tg.send_message(chat_id, text_for("left_contact", language))

    async def on_location_request(self, chat_id: int, language: Language) -> None:
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=text_for("location_button", language), request_location=True)]
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        minutes = self._settings.location_ttl_seconds // 60
        await self._tg.send_message(
            chat_id, text_for("location_request", language, minutes=minutes), keyboard
        )

    async def on_location(
        self, chat_id: int, user_id: int, message: Message, language: Language
    ) -> None:
        """Store a one-off location or the latest point of a live one, with the TTL from settings.

        Live updates arrive as edits of the original message: they are stored silently, and only
        the first share gets a reply.
        """
        assert message.location is not None
        captured = datetime.fromtimestamp(message.edit_date or message.date, UTC)
        location = SharedLocation(
            latitude=message.location.latitude,
            longitude=message.location.longitude,
            accuracy_m=message.location.horizontal_accuracy,
            captured_at=captured,
            live=message.location.live_period is not None,
        )
        ttl = self._settings.location_ttl_seconds
        await self._locations.put(location_key(user_id), location, ttl)
        if message.edit_date is None:
            until = self._local(datetime.now(UTC) + timedelta(seconds=ttl))
            await self._tg.send_message(
                chat_id, text_for("location_saved", language, until=until), ReplyKeyboardRemove()
            )

    async def forget_everything(self, user_id: int) -> None:
        """Part of /forget: location, invitations, the contact, and being someone's contact."""
        await self._locations.delete(location_key(user_id))
        await self._trust.remove_contact(user_id)
        await self._trust.leave(user_id)

    async def _notify(self, chat_id: int, text: str) -> None:
        try:
            await self._tg.send_message(chat_id, text)
        except TelegramError as exc:
            logger.warning("telegram notify failed: %s", exc.error_code)
