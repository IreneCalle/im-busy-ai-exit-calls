"""ExcuseCompiler: one strong-model call that turns "aunt Carmen, locked out, in 2 min" into a CallScript."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from imbusy.call.voices import VoiceCatalog
from imbusy.excuse.schema import (
    BASE_FORBIDDEN,
    CallScript,
    CompiledExcuse,
    CompileRefused,
    ExcuseDraft,
    ExcuseRequest,
    Language,
    RefusalKind,
)
from imbusy.llm import ChatMessage, Effort, LLMClient, ModelRole, StopReason
from imbusy.prompts import Prompt, load_prompt
from imbusy.safety.acceptable_use import screen_excuse, screen_script
from imbusy.safety.guard import ScriptLinter

logger = logging.getLogger(__name__)

OPENING_MAX_WORDS = 20
MAX_FACTS = 4

# Shown to the user when the compiler declines. Keyed by language because the bot replies in kind.
REFUSAL_MESSAGES: dict[Language, dict[RefusalKind, str]] = {
    Language.EN_US: {
        RefusalKind.IMPERSONATES_AUTHORITY: (
            "I can't play the police, a court, a bank, a hospital, a company's support line or any "
            "other organisation. A relative, a friend or a colleague by first name works just as well."
        ),
        RefusalKind.IMPERSONATES_REAL_PERSON: (
            "I only play made-up callers: a first name or a relationship, like 'aunt Carmen' or "
            "'my manager Dana'. No full names, no public figures, and no sounding like someone real."
        ),
        RefusalKind.TARGETS_THIRD_PARTY: (
            "I only ever call you, on your own verified number, and I don't take part in anything "
            "that involves someone else's phone, money, codes or passwords."
        ),
        RefusalKind.HARMFUL: (
            "I can't make that call. I'm for harmless excuses that get you out of somewhere, not for "
            "pressuring, scaring or deceiving anyone."
        ),
        RefusalKind.NOT_AN_EXCUSE: (
            "Tell me who should call and why, e.g. 'aunt Carmen, she locked herself out, in 2 min'."
        ),
    },
    Language.ES_ES: {
        RefusalKind.IMPERSONATES_AUTHORITY: (
            "No puedo hacerme pasar por la policía, un juzgado, un banco, un hospital, el servicio de "
            "atención de una empresa ni ninguna otra organización. Un familiar, una amiga o alguien "
            "del trabajo, por su nombre de pila, funciona igual de bien."
        ),
        RefusalKind.IMPERSONATES_REAL_PERSON: (
            "Solo hago de personas inventadas: un nombre de pila o un parentesco, como 'tía Carmen' "
            "o 'mi jefa Dana'. Nada de nombres completos, personas conocidas ni imitar a alguien real."
        ),
        RefusalKind.TARGETS_THIRD_PARTY: (
            "Solo te llamo a ti, a tu número verificado, y no participo en nada que tenga que ver con "
            "el teléfono, el dinero, los códigos o las contraseñas de otra persona."
        ),
        RefusalKind.HARMFUL: (
            "Esa llamada no la puedo hacer. Sirvo para excusas inofensivas que te saquen de un sitio, "
            "no para presionar, asustar ni engañar a nadie."
        ),
        RefusalKind.NOT_AN_EXCUSE: (
            "Dime quién te llama y por qué, por ejemplo: 'tía Carmen, se ha dejado las llaves dentro, en 2 min'."
        ),
    },
}


class ExcuseCompiler:
    """Compiles excuses with the strong model and finalises them deterministically."""

    def __init__(
        self,
        llm: LLMClient,
        voices: VoiceCatalog,
        *,
        default_delay_seconds: int,
        max_delay_seconds: int,
        default_timezone: str = "Europe/Madrid",
        prompt: Prompt | None = None,
        linter: ScriptLinter | None = None,
    ) -> None:
        self._llm = llm
        self._voices = voices
        self._default_delay = default_delay_seconds
        self._max_delay = max_delay_seconds
        self._default_tz = default_timezone
        self._prompt = prompt or load_prompt("imbusy.excuse", "compiler.md")
        self._linter = linter or ScriptLinter()

    @property
    def prompt_version(self) -> int:
        return self._prompt.version

    def system_prompt(self, now: datetime) -> str:
        """Render the compiler system prompt. `now` lets the model resolve 'at 18:30'."""
        return self._prompt.render(now=now.strftime("%A %d %B %Y, %H:%M (UTC%z)"))

    def local_now(self, request: ExcuseRequest) -> datetime:
        """The request time in the user's zone (request.timezone, else the configured default)."""
        for name in (request.timezone, self._default_tz):
            if not name:
                continue
            try:
                return request.received_at.astimezone(ZoneInfo(name))
            except (ZoneInfoNotFoundError, ValueError):
                logger.warning("unknown timezone %r", name)
        return request.received_at

    async def compile(self, request: ExcuseRequest) -> CompiledExcuse | CompileRefused:
        """Screen the message, run the model, then `finalise` its draft.

        Requests that break the acceptable-use rules are refused here, before any model call.
        Refusals by the API map to `HARMFUL`. `LLMError` propagates: the caller decides how to
        tell the user the service is down.
        """
        language = request.preferred_language or guess_language(request.text)
        screening = screen_excuse(request.text)
        if screening is not None:
            logger.info("excuse refused before the model", extra={"rule": screening.rule})
            return refused(screening.kind, language)
        content = request.text
        if request.preferred_language is not None:
            content += f"\n\n(Call language: {request.preferred_language.value})"
        result = await self._llm.structured(
            role=ModelRole.STRONG,
            system=self.system_prompt(self.local_now(request)),
            messages=[ChatMessage(role="user", content=content)],
            output_type=ExcuseDraft,
            max_tokens=4000,
            effort=Effort.LOW,
        )
        if result.stop_reason is StopReason.REFUSAL or result.value is None:
            return refused(RefusalKind.HARMFUL, language)
        return self.finalise(result.value, request)

    def finalise(
        self, draft: ExcuseDraft, request: ExcuseRequest
    ) -> CompiledExcuse | CompileRefused:
        """Deterministic post-processing of a draft (pure; unit-tested without a model).

        - honour `draft.refusal`;
        - pick a catalog voice from language and gender;
        - clamp urgency to 1..5 and delay to 0..max (null -> default);
        - append BASE_FORBIDDEN to `forbidden`;
        - trim the opening line to one sentence if the model wrote more;
        - check who the caller is (no organisation, no full name) and lint the spoken fields;
          either failing is a refusal.
        """
        language = request.preferred_language or draft.language
        if draft.refusal is not None:
            return refused(draft.refusal, language)
        voice = self._voices.pick(language, draft.caller_gender)
        facts = [clip(f, 200) for f in draft.facts if f.strip()][:MAX_FACTS]
        alias = (
            clip(draft.user_alias, 200) if draft.user_alias and draft.user_alias.strip() else None
        )
        try:
            script = CallScript(
                caller_name=clip(draft.caller_name, 200),
                relationship=clip(draft.relationship, 200),
                speech_register=draft.speech_register,
                language=language,
                voice_id=voice.id,
                user_alias=alias,
                opening_line=trim_opening(draft.opening_line),
                situation=clip(draft.situation, 500),
                ask=clip(draft.ask, 200),
                urgency=max(1, min(5, draft.urgency)),
                facts=facts,
                forbidden=forbidden_rules(),
            )
        except ValidationError as exc:
            logger.info("draft rejected: %s", exc.errors(include_url=False))
            return refused(RefusalKind.NOT_AN_EXCUSE, language)
        screening = screen_script(script)
        if screening is not None:
            logger.info("script refused", extra={"rule": screening.rule})
            return refused(screening.kind, language)
        issues = self._linter.lint(script)
        if issues:
            logger.info("script failed lint: %s", issues)
            return refused(RefusalKind.HARMFUL, language)
        delay = clamp_delay(
            draft.delay_seconds, default=self._default_delay, maximum=self._max_delay
        )
        return CompiledExcuse(
            script=script, delay_seconds=delay, prompt_version=self.prompt_version
        )


def refused(kind: RefusalKind, language: Language) -> CompileRefused:
    return CompileRefused(kind=kind, message=REFUSAL_MESSAGES[language][kind])


def clamp_delay(value: int | None, *, default: int, maximum: int) -> int:
    """Delay to apply: `default` when unspecified, never negative, never above `maximum`."""
    if value is None:
        return default
    return max(0, min(value, maximum))


def forbidden_rules(extra: list[str] | None = None) -> list[str]:
    """Base safety rules plus any script-specific ones, without duplicates, order preserved."""
    seen: dict[str, None] = dict.fromkeys(BASE_FORBIDDEN)
    for rule in extra or []:
        seen.setdefault(rule, None)
    return list(seen)


def clip(text: str, limit: int) -> str:
    """Strip and cut to `limit` characters on a word boundary when possible."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut or text[:limit]


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def trim_opening(line: str, max_words: int = OPENING_MAX_WORDS) -> str:
    """Keep the opening short: whole sentences while under `max_words`, at least the first one.

    A first sentence that is itself too long is cut to `max_words + 5` words.
    """
    line = " ".join(line.split())
    sentences = [s for s in _SENTENCE_END.split(line) if s]
    if not sentences:
        return line
    kept = [sentences[0]]
    for sentence in sentences[1:]:
        if len(" ".join([*kept, sentence]).split()) > max_words:
            break
        kept.append(sentence)
    words = " ".join(kept).split()
    if len(words) > max_words + 5:
        return " ".join(words[: max_words + 5])
    return " ".join(kept)


_SPANISH_HINTS = re.compile(
    r"[ñáéíóú¿¡]|\b(que|tía|tío|mamá|papá|del|curro|en \d+ ?min|llama|tiene|jefa?)\b",
    re.IGNORECASE,
)


def guess_language(text: str) -> Language:
    """Cheap fallback used only to word refusal messages when the model gave no draft."""
    return Language.ES_ES if _SPANISH_HINTS.search(text) else Language.EN_US


__all__ = [
    "REFUSAL_MESSAGES",
    "ExcuseCompiler",
    "clamp_delay",
    "clip",
    "forbidden_rules",
    "guess_language",
    "trim_opening",
]
