"""Bot conversation: onboarding by contact share, excuse requests, cancel, forget.

/start          -> explain, show the "Share my number" button (request_contact)
contact (own)   -> store verified phone, confirm
contact (other) -> refuse: the bot only calls the sender's own number
free text       -> compile excuse -> schedule call -> "Carmen will call you in 2 min. /cancel"
/cancel         -> cancel the pending call (or hang up the one ringing)
/forget         -> delete the user's phone number, call history, safe word data
/safeword ...    -> see imbusy.telegram.safety_bot
/help           -> usage
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from imbusy.call.dialer import CallRunner, CallScheduler, Dialer
from imbusy.config import Settings
from imbusy.excuse.compiler import ExcuseCompiler
from imbusy.excuse.schema import CompileRefused, ExcuseRequest, Language
from imbusy.llm import LLMError
from imbusy.store.models import CallOutcome, CallRecord
from imbusy.store.scripts import ScriptStore, script_ttl
from imbusy.store.sqlite import CallRepository, UserRepository
from imbusy.telegram.client import TelegramClient, TelegramError
from imbusy.telegram.intents import Intent, classify, language_of
from imbusy.telegram.models import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    normalise_phone,
)
from imbusy.telegram.safety_bot import SAFETY_INTENTS, SafetyBot

logger = logging.getLogger(__name__)


TEXTS: dict[str, dict[Language, str]] = {
    "start": {
        Language.ES_ES: (
            "Hola. Me escribes una excusa y te llama alguien que la sostiene: tu tía, tu jefa, "
            "quien haga falta. La voz es generada por IA.\n\n"
            "Al seguir, aceptas esto: solo llamo a tu propio teléfono; me das un nombre de pila y "
            "una excusa, nunca la identidad de una persona o una organización reales; no es para "
            "engañar ni molestar a nadie; y no es un servicio de emergencias.\n\n"
            "Si te parece bien, comparte tu número con el botón de abajo."
        ),
        Language.EN_US: (
            "Hi. Text me an excuse and someone who backs it up will call you: your aunt, your "
            "boss, whoever it takes. The voice is AI-generated.\n\n"
            "By continuing you agree to this: I only call your own phone; you give me a first name "
            "and an excuse, never a real person's or organisation's identity; it is not for "
            "deceiving or bothering anyone; and it is not an emergency service.\n\n"
            "If that works for you, share your number with the button below."
        ),
    },
    "share_button": {Language.ES_ES: "Compartir mi número", Language.EN_US: "Share my number"},
    "verified": {
        Language.ES_ES: (
            "Listo, te llamaré al {phone}. Ahora escríbeme la excusa, por ejemplo: "
            "«tía Carmen, se ha dejado las llaves dentro, en 2 min»."
        ),
        Language.EN_US: (
            "Done, I'll call {phone}. Now text me the excuse, for example: "
            '"my manager Dana, the client moved the call to now, in 2 min".'
        ),
    },
    "foreign_contact": {
        Language.ES_ES: "Solo acepto tu propio número, compartido con el botón. No llamo a terceros.",
        Language.EN_US: "I only accept your own number, shared with the button. I never call anyone else.",
    },
    "need_contact": {
        Language.ES_ES: "Antes necesito tu número: pulsa /start y compártelo con el botón.",
        Language.EN_US: "I need your number first: tap /start and share it with the button.",
    },
    "scheduled": {
        Language.ES_ES: "{caller} te llama {when}. /cancel para anularlo.",
        Language.EN_US: "{caller} will call you {when}. /cancel to stop it.",
    },
    "limit": {
        Language.ES_ES: "Ya van {n} llamadas en 24 horas. Vuelve a intentarlo más tarde.",
        Language.EN_US: "That's {n} calls in 24 hours. Try again later.",
    },
    "busy": {
        Language.ES_ES: "Ya hay una llamada en marcha. /cancel si quieres anularla.",
        Language.EN_US: "A call is already on its way. /cancel if you want to stop it.",
    },
    "cancelled": {Language.ES_ES: "Anulada.", Language.EN_US: "Cancelled."},
    "nothing_to_cancel": {
        Language.ES_ES: "No hay ninguna llamada pendiente.",
        Language.EN_US: "There is no pending call.",
    },
    "forgotten": {
        Language.ES_ES: "He borrado tu número y tu historial.",
        Language.EN_US: "I deleted your number and your history.",
    },
    "unavailable": {
        Language.ES_ES: "Ahora mismo no puedo preparar la llamada. Prueba en un minuto.",
        Language.EN_US: "I can't set up the call right now. Try again in a minute.",
    },
    "help": {
        Language.ES_ES: (
            "Escríbeme quién te llama, por qué y cuándo: «Laura del curro, se ha quedado sin "
            "llaves, en 3 min». /cancel anula, /forget borra tus datos. /safeword prepara una "
            "palabra de seguridad que avisa a alguien de confianza."
        ),
        Language.EN_US: (
            'Tell me who calls, why and when: "my sister, her car will not start, '
            'in 5". /cancel stops a call, /forget deletes your data. /safeword sets up a word '
            "that quietly alerts someone you trust."
        ),
    },
    "outcome_no_answer": {
        Language.ES_ES: "No lo cogiste. Si aún la necesitas, mándame la excusa otra vez.",
        Language.EN_US: "You didn't pick up. If you still need it, send the excuse again.",
    },
    "outcome_busy": {
        Language.ES_ES: "Tu línea estaba ocupada.",
        Language.EN_US: "Your line was busy.",
    },
    "outcome_failed": {
        Language.ES_ES: "La llamada no se pudo hacer.",
        Language.EN_US: "The call could not be placed.",
    },
}


def text_for(key: str, language: Language, **values: object) -> str:
    return TEXTS[key][language].format(**values)


def describe_delay(seconds: int, language: Language) -> str:
    if seconds < 45:
        return "ahora mismo" if language is Language.ES_ES else "right now"
    minutes = max(1, round(seconds / 60))
    if language is Language.ES_ES:
        return f"en {minutes} min"
    return f"in {minutes} min"


class BotService:
    """Handles classified updates. Depends on the stores, compiler, scheduler and Telegram client."""

    def __init__(
        self,
        *,
        telegram: TelegramClient,
        users: UserRepository,
        calls: CallRepository,
        scripts: ScriptStore,
        compiler: ExcuseCompiler,
        scheduler: CallScheduler,
        runner: CallRunner,
        dialer: Dialer,
        settings: Settings,
        safety: SafetyBot,
    ) -> None:
        self._tg = telegram
        self._users = users
        self._calls = calls
        self._scripts = scripts
        self._compiler = compiler
        self._scheduler = scheduler
        self._runner = runner
        self._dialer = dialer
        self._settings = settings
        self._safety = safety

    async def handle(self, update: Update) -> None:
        """Process one update. Idempotent per `update_id` (Telegram redelivers on slow responses)."""
        now = datetime.now(UTC)
        if not await self._calls.mark_update_processed(update.update_id, now):
            return
        classified = classify(update)
        if classified is None or classified.intent is Intent.IGNORE or classified.user_id is None:
            return
        message = update.message or update.edited_message
        assert message is not None
        language = language_of(message)
        user_id, chat_id = classified.user_id, classified.chat_id
        name = message.from_.first_name if message.from_ else None
        await self._users.upsert_chat(user_id, chat_id, now, language, display_name=name)
        try:
            if classified.intent in SAFETY_INTENTS:
                await self._safety.handle(classified, message, language)
                return
            match classified.intent:
                case Intent.START:
                    await self.on_start(chat_id, language)
                case Intent.HELP:
                    await self._tg.send_message(chat_id, text_for("help", language))
                case Intent.OWN_CONTACT:
                    await self.on_own_contact(message, language)
                case Intent.FOREIGN_CONTACT:
                    await self._tg.send_message(chat_id, text_for("foreign_contact", language))
                case Intent.EXCUSE:
                    await self.on_excuse(chat_id, user_id, classified.text or "", language)
                case Intent.CANCEL:
                    await self.on_cancel(chat_id, user_id, language)
                case Intent.FORGET:
                    await self.on_forget(chat_id, user_id, language)
        except TelegramError as exc:
            logger.warning("telegram send failed: %s", exc.error_code)

    async def on_start(self, chat_id: int, language: Language) -> None:
        """Explain the product and send the request_contact keyboard."""
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=text_for("share_button", language), request_contact=True)]
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await self._tg.send_message(chat_id, text_for("start", language), keyboard)

    async def on_own_contact(self, message: Message, language: Language) -> None:
        """Normalise and store the verified number, remove the keyboard, confirm."""
        assert message.contact is not None and message.from_ is not None
        try:
            phone = normalise_phone(message.contact.phone_number)
        except ValueError:
            await self._tg.send_message(message.chat.id, text_for("foreign_contact", language))
            return
        await self._users.set_verified_phone(message.from_.id, phone, datetime.now(UTC))
        await self._tg.send_message(
            message.chat.id,
            text_for("verified", language, phone=phone),
            ReplyKeyboardRemove(),
        )

    async def on_excuse(self, chat_id: int, user_id: int, text: str, language: Language) -> None:
        """Check verification and limits, compile, schedule, confirm with the caller name and delay."""
        user = await self._users.get(user_id)
        if user is None or not user.is_verified:
            await self._tg.send_message(chat_id, text_for("need_contact", language))
            return
        now = datetime.now(UTC)
        if await self._calls.latest_pending(user_id) is not None:
            await self._tg.send_message(chat_id, text_for("busy", language))
            return
        used = await self._calls.count_since(user_id, now - timedelta(hours=24))
        if used >= self._settings.max_calls_per_user_per_day:
            await self._tg.send_message(chat_id, text_for("limit", language, n=used))
            return
        request = ExcuseRequest(
            text=text[:500],
            source="telegram",
            received_at=now,
            timezone=self._settings.default_timezone,
        )
        try:
            result = await self._compiler.compile(request)
        except LLMError:
            logger.exception("compile failed")
            await self._tg.send_message(chat_id, text_for("unavailable", language))
            return
        if isinstance(result, CompileRefused):
            await self._tg.send_message(chat_id, result.message)
            return
        fire_at = now + timedelta(seconds=result.delay_seconds)
        await self._scripts.put(result.call_id, result.script, script_ttl(result.delay_seconds))
        await self._calls.create(
            CallRecord(
                call_id=result.call_id,
                telegram_user_id=user_id,
                caller_name=result.script.caller_name,
                language=result.script.language,
                scheduled_for=fire_at,
                prompt_version=result.prompt_version,
                created_at=now,
            )
        )
        self._scheduler.schedule(result, fire_at, self._runner)
        when = describe_delay(result.delay_seconds, language)
        await self._tg.send_message(
            chat_id, text_for("scheduled", language, caller=result.script.caller_name, when=when)
        )

    async def on_cancel(self, chat_id: int, user_id: int, language: Language) -> None:
        pending = await self._calls.latest_pending(user_id)
        if pending is None:
            await self._tg.send_message(chat_id, text_for("nothing_to_cancel", language))
            return
        self._scheduler.cancel(pending.call_id)
        if pending.twilio_call_sid:
            try:
                await self._dialer.hang_up(pending.twilio_call_sid)
            except Exception:
                logger.exception("hang up failed")
        await self._calls.set_outcome(
            pending.call_id, CallOutcome.CANCELLED, datetime.now(UTC), terminal=True
        )
        await self._scripts.delete(pending.call_id)
        await self._tg.send_message(chat_id, text_for("cancelled", language))

    async def on_forget(self, chat_id: int, user_id: int, language: Language) -> None:
        pending = await self._calls.latest_pending(user_id)
        if pending is not None:
            self._scheduler.cancel(pending.call_id)
            await self._scripts.delete(pending.call_id)
        await self._safety.forget_everything(user_id)
        await self._users.forget(user_id)
        await self._tg.send_message(chat_id, text_for("forgotten", language), ReplyKeyboardRemove())

    async def notify_call_outcome(self, call_id: UUID, outcome: CallOutcome) -> None:
        """Tell the user when a call was not answered, was busy or failed."""
        key = {
            CallOutcome.NO_ANSWER: "outcome_no_answer",
            CallOutcome.BUSY: "outcome_busy",
            CallOutcome.FAILED: "outcome_failed",
        }.get(outcome)
        if key is None:
            return
        record = await self._calls.get(call_id)
        if record is None:
            return
        user = await self._users.get(record.telegram_user_id)
        if user is None:
            return
        language = user.language or record.language
        try:
            await self._tg.send_message(user.chat_id, text_for(key, language))
        except TelegramError as exc:
            logger.warning("telegram send failed: %s", exc.error_code)
