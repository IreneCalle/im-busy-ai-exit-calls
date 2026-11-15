"""Acceptable use, checked in code before and after the model.

The product takes a name and an excuse. The caller is a first name or a relationship ("tía
Carmen", "my manager Dana") that the compiler invents a voice for from a stock catalogue. These
checks refuse, without asking the model, the requests that would turn it into something else:

- playing an identifiable real person: a full name, or asking to sound like someone real;
- playing an authority or an organisation: police, courts, banks, hospitals, a company's support
  line, a government office;
- calling or deceiving anyone other than the requester, or asking for money, codes or passwords;
- harassment.

They are deliberately simple and conservative: phrase lists and a full-name pattern, documented
in RESPONSIBLE_USE.md. The compiler prompt carries the same rules for what a list cannot catch
(a public figure named by first name only, say), and `screen_script` checks the model's output
again so a slip in the prompt cannot reach a phone.
"""

from __future__ import annotations

import itertools
import re
import unicodedata
from dataclasses import dataclass

from imbusy.excuse.schema import CallScript, RefusalKind


def fold(text: str) -> str:
    """Lowercase without accents, so 'Policía' and 'policia' match the same pattern."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


# Words that make "Tía Carmen" or "Señora Pilar" a relationship plus a first name, not a full name,
# and capitalised words that commonly open a message.
NOT_A_SURNAME = frozenset(
    {
        # Spanish
        "tia", "tio", "mama", "papa", "madre", "padre", "abuela", "abuelo", "yaya", "yayo",
        "hermana", "hermano", "prima", "primo", "hija", "hijo", "sobrina", "sobrino", "cunada",
        "cunado", "suegra", "suegro", "amiga", "amigo", "vecina", "vecino", "companera",
        "companero", "jefa", "jefe", "senora", "senor", "dona", "don", "mi", "el",
        "hola", "oye", "llama", "llamame", "que", "porfa",
        # English
        "aunt", "auntie", "uncle", "mum", "mom", "mother", "dad", "father", "grandma", "grandpa",
        "granny", "nan", "sister", "brother", "cousin", "daughter", "son", "niece", "nephew",
        "friend", "neighbour", "neighbor", "flatmate", "roommate", "colleague", "coworker",
        "boss", "manager", "my", "the", "hi", "hey", "hello", "please", "call", "tell",
    }
)  # fmt: skip
_PARTICLES = frozenset({"de", "del", "la", "las", "los", "van", "von", "da", "di", "y"})
_CAPITALISED = re.compile(r"^[A-ZÁÉÍÓÚÑÜ][a-záéíóúñü]+$")
_SEGMENTS = re.compile(r"[,.;:!?¿¡()\n]+")
_WORDS = re.compile(r"[^\W\d_]+")

AUTHORITIES = (
    r"police", r"policia", r"guardia civil", r"guardia urbana", r"mossos", r"ertzaintza",
    r"sheriff", r"fbi", r"court", r"juzgado", r"tribunal", r"judge", r"juez", r"fiscal",
    r"prosecutor", r"bank", r"banco", r"hospital", r"urgencias", r"ambulance", r"ambulancia",
    r"emergenc\w*", r"112", r"911", r"091", r"062", r"hacienda", r"agencia tributaria",
    r"tax office", r"irs", r"seguridad social", r"social security", r"ayuntamiento",
    r"city council", r"town hall", r"government", r"gobierno", r"ministry", r"ministerio",
    r"embassy", r"embajada", r"customer service", r"customer support", r"support line",
    r"helpdesk", r"help desk", r"atencion al cliente", r"servicio tecnico", r"soporte tecnico",
    r"call cent(?:er|re)", r"insurance", r"aseguradora",
)  # fmt: skip
_ANY_AUTHORITY = "(?:" + "|".join(AUTHORITIES) + ")"
_AUTHORITY_WORD = re.compile(rf"\b{_ANY_AUTHORITY}\b")
# The authority must be the caller, not just mentioned: "my mum is stuck at the bank" is fine,
# "a call from the bank" is not.
_AUTHORITY_CALLING = re.compile(
    rf"\b(?:call|calls|llamada|llamadas)\s+(?:from|de|del|de la|desde)\s+(?:the |el |la |los |las |my |mi )?{_ANY_AUTHORITY}\b"
    rf"|\b{_ANY_AUTHORITY}\s+(?:calling|calls|call|llama\w*)\b"
    rf"|\b(?:que me llam\w*|call me as|pretend(?:ing)? to be|as if (?:it|you) (?:were|was)|como si fuera)\s+(?:the |el |la |a |an |un |una )?{_ANY_AUTHORITY}\b"
)

_IMPERSONATION = re.compile(
    r"\bsound(?:s|ing)? (?:exactly |just )?like (?:my|his|her|the real|a real)\b"
    r"|\b(?:imitat|impersonat|mimic|imit|suplant)\w*"
    r"|\bclon\w*"
    r"|\b(?:voice|voz) (?:of|de) (?:my|mi|su|his|her)\b"
    r"|\b(?:real|actual|de verdad|verdader[oa]) (?:voice|voz)\b"
    r"|\bmy (?:real|actual) (?:boss|manager|mum|mom|dad|mother|father|sister|brother|partner|wife|husband)\b"
    r"|\bmi (?:jef[ea]|madre|padre|hermana|hermano|pareja|mujer|marido) (?:de verdad|real)\b"
    r"|\bhac\w* pasar por\b"
    r"|\bque suene (?:igual )?(?:que|como) (?:mi|el|la|a)\b"
)

_THIRD_PARTY = re.compile(
    r"\b(?:call|ring|phone|text|message)\s+(?:my|his|her|their|him|them)\b"
    r"|\bllam(?:a|ale|alo|ala|ar|en)\s+a\s+(?:mi|mis|su|sus|tu|el|la|los|las|este|esta)\b"
    r"|\b(?:a|para) mi ex\b|\bmy ex\b"
    r"|(?:\+|\b00)\d[\d\s-]{6,}\d|\b\d{3}[\s-]?\d{3}[\s-]?\d{3,4}\b"
)

_GAIN = re.compile(
    r"\b(?:money|dinero|pasta|cash|transfer\w*|bizum|paypal|wire|western union|gift ?cards?"
    r"|tarjetas? regalo|card number|numero de (?:la )?tarjeta|bank account|cuenta bancaria|iban"
    r"|password|contrasena|pin|verification code|codigo de verificacion|codigo sms|one[- ]time code"
    r"|crypto\w*|bitcoin)\b"
)

_HARASSMENT = re.compile(
    r"\b(?:harass\w*|acos\w*|amenaz\w*|threat\w*|intimid\w*|scare|asust\w*|insult\w*"
    r"|humiliat\w*|humill\w*|stalk\w*|every (?:hour|five minutes|day)|cada (?:hora|dia|cinco minutos)"
    r"|keep calling|sin parar|todo el rato|revenge|venganza)\b"
)


@dataclass(frozen=True)
class Screening:
    kind: RefusalKind
    rule: str  # stable id for logs and tests; never the user's text


def full_names(text: str) -> list[str]:
    """Pairs of capitalised words that are not a relationship plus a first name ("Carmen López")."""
    found: list[str] = []
    for segment in _SEGMENTS.split(text):
        words = [w for w in _WORDS.findall(segment) if fold(w) not in _PARTICLES]
        for first, last in itertools.pairwise(words):
            if not (_CAPITALISED.match(first) and _CAPITALISED.match(last)):
                continue
            if fold(first) in NOT_A_SURNAME or fold(last) in NOT_A_SURNAME:
                continue
            found.append(f"{first} {last}")
    return found


def screen_excuse(text: str) -> Screening | None:
    """Check the user's message before any model call. None means it may be compiled."""
    folded = fold(text)
    if _IMPERSONATION.search(folded):
        return Screening(RefusalKind.IMPERSONATES_REAL_PERSON, "impersonation_request")
    if full_names(text):
        return Screening(RefusalKind.IMPERSONATES_REAL_PERSON, "full_name")
    if _AUTHORITY_CALLING.search(folded):
        return Screening(RefusalKind.IMPERSONATES_AUTHORITY, "authority_caller")
    if _THIRD_PARTY.search(folded):
        return Screening(RefusalKind.TARGETS_THIRD_PARTY, "third_party")
    if _GAIN.search(folded):
        return Screening(RefusalKind.TARGETS_THIRD_PARTY, "money_or_codes")
    if _HARASSMENT.search(folded):
        return Screening(RefusalKind.HARMFUL, "harassment")
    return None


def screen_script(script: CallScript) -> Screening | None:
    """Check who the compiled caller is. The situation may mention a bank; the caller may not be one."""
    identity = f"{script.caller_name} {script.relationship}"
    if _AUTHORITY_WORD.search(fold(identity)):
        return Screening(RefusalKind.IMPERSONATES_AUTHORITY, "authority_caller")
    if full_names(script.caller_name):
        return Screening(RefusalKind.IMPERSONATES_REAL_PERSON, "full_name")
    if _GAIN.search(fold(f"{script.opening_line} {script.ask}")):
        return Screening(RefusalKind.TARGETS_THIRD_PARTY, "money_or_codes")
    return None
