"""Guards that run before a call is placed. Each one is a hard stop, not a model judgement.

- `DestinationGuard`: the only number ever dialled is the requester's own verified number.
- `CallRateLimiter`: per-user daily cap on real calls; per-IP hourly cap on demo sessions.
- `ScriptLinter`: deterministic checks on compiled scripts (authority words, AI self-disclosure,
  lengths) as a second line behind the compiler prompt.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from imbusy.excuse.schema import CallScript, Language
from imbusy.store.models import UserRecord


class GuardViolation(Exception):
    """A call must not be placed. `code` is stable and logged; `message` is safe to show."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class DestinationGuard:
    """Resolves the destination from the user record; there is no parameter for a target number."""

    def destination_for(self, user: UserRecord) -> str:
        """The user's verified E.164 number, or `GuardViolation("unverified", ...)`."""
        if not user.is_verified or user.phone_e164 is None:
            raise GuardViolation("unverified", "Share your number with /start first.")
        return user.phone_e164


@dataclass
class SlidingWindowLimiter:
    """In-process sliding window keyed by string (user id or IP)."""

    limit: int
    window: timedelta

    def __post_init__(self) -> None:
        self._hits: dict[str, deque[datetime]] = {}

    def allow(self, key: str, now: datetime) -> bool:
        """Record a hit and return True if `key` is still within its limit."""
        hits = self._hits.setdefault(key, deque())
        cutoff = now - self.window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


# Words that would make the caller an authority or reveal the trick. Matched on whole words,
# case-insensitive, in the spoken fields of a script.
_BLOCKLIST: dict[Language, tuple[str, ...]] = {
    Language.ES_ES: ("policía", "guardia civil", "112", "juzgado", "inteligencia artificial", "robot"),
    Language.EN_US: ("police", "911", "sheriff", "artificial intelligence", "robot"),
}  # fmt: skip


@dataclass(frozen=True)
class LintIssue:
    field: str
    reason: str


class ScriptLinter:
    def lint(self, script: CallScript) -> list[LintIssue]:
        """Return issues found in the spoken fields; empty means the script may be used."""
        issues: list[LintIssue] = []
        words = _BLOCKLIST[script.language]
        for field_name in ("caller_name", "relationship", "opening_line", "situation", "ask"):
            value = str(getattr(script, field_name)).lower()
            for word in words:
                if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", value):
                    issues.append(LintIssue(field_name, f"contains '{word}'"))
        if len(script.opening_line.split()) > 25:
            issues.append(LintIssue("opening_line", "longer than 25 words"))
        return issues
