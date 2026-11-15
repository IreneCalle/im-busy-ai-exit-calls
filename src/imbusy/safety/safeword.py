"""Safe word: deterministic matching of a short phrase against what the user said.

No model is involved: the decision cannot be argued with, and it costs nothing per turn.

Matching works on normalised text (lowercase, accents and punctuation removed, whitespace
collapsed) and tolerates the slips speech-to-text makes in practice: one wrong letter, a plural,
or a word split in two ("flamingo" heard as "flaming go"). It compares the phrase with every run of
transcript words of a similar length, with spaces removed, and allows an edit distance of 1, or 2
for phrases of 16 letters or more. That keeps near-misses that differ in two letters ("pink
flamenco" against "pink flamingo") from firing.

A phrase must be two to four words with at least eight letters in total, so a word that comes up
in ordinary conversation ("yes", "help") cannot be chosen by accident.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MIN_WORDS = 2
MAX_WORDS = 4
MIN_LETTERS = 8
MAX_LETTERS = 60


class SafeWordError(ValueError):
    """The phrase cannot be used; `reason` is a stable code the bot and the page translate."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def normalise(text: str) -> list[str]:
    """Lowercase, strip accents and punctuation, split into words."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", without_marks.replace("ß", "ss")).split()


def parse_safe_word(raw: str) -> str:
    """Validate a phrase chosen by the user and return its normalised form ("pink flamingo")."""
    words = normalise(raw)
    letters = sum(len(w) for w in words)
    if len(words) < MIN_WORDS:
        raise SafeWordError("too_few_words")
    if len(words) > MAX_WORDS:
        raise SafeWordError("too_many_words")
    if letters < MIN_LETTERS:
        raise SafeWordError("too_short")
    if letters > MAX_LETTERS:
        raise SafeWordError("too_long")
    return " ".join(words)


def edit_distance(a: str, b: str, limit: int) -> int:
    """Levenshtein distance, stopping early once it exceeds `limit` (returns limit + 1)."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def allowed_slips(letters: int) -> int:
    return 2 if letters >= 16 else 1


@dataclass(frozen=True)
class Match:
    start: int  # index of the first matched transcript word
    end: int  # index after the last matched transcript word


@dataclass(frozen=True)
class SafeWordMatcher:
    """Matches one normalised phrase (as returned by `parse_safe_word`)."""

    phrase: str

    def find(self, transcript: str) -> Match | None:
        words = normalise(transcript)
        target = self.phrase.replace(" ", "")
        size = len(self.phrase.split())
        limit = allowed_slips(len(target))
        for width in (size, size + 1, size - 1):
            if width < 1:
                continue
            for start in range(0, len(words) - width + 1):
                candidate = "".join(words[start : start + width])
                if edit_distance(candidate, target, limit) <= limit:
                    return Match(start, start + width)
        return None

    def heard(self, transcript: str) -> bool:
        return self.find(transcript) is not None

    def redact(self, transcript: str) -> str:
        """The transcript without the matched words, for the model's context.

        The caller model never sees the safe word, so it cannot react to it or repeat it.
        """
        match = self.find(transcript)
        if match is None:
            return transcript
        words = normalise(transcript)
        rest = words[: match.start] + words[match.end :]
        return " ".join(rest) if rest else "[inaudible]"
