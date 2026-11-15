"""Safe word in the browser demo: same matcher, same alert text, nothing sent anywhere.

The page sends the phrase, an optional first name and an optional browser location with the
excuse. They are kept in memory under the demo call token for as long as the script is, and are
gone when the call ends or the token expires.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from imbusy.store.locations import SharedLocation

# Shown by the page; the bot has its own wording in each language.
DEMO_REASONS = {
    "too_few_words": "Use at least two words, so it cannot come up in normal conversation.",
    "too_many_words": "Four words at most, so it is easy to say under pressure.",
    "too_short": "Too short: use at least eight letters in total.",
    "too_long": "Too long: keep it under sixty letters.",
}


@dataclass(frozen=True)
class DemoSafety:
    phrase: str  # normalised
    name: str
    location: SharedLocation | None
    timezone: str | None = None


class DemoSafetyStore:
    """In-memory, keyed by demo call token, with the same lifetime as the demo script."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._items: dict[UUID, tuple[float, DemoSafety]] = {}

    def put(self, token: UUID, safety: DemoSafety, ttl_seconds: int) -> None:
        now = self._clock()
        for key in [k for k, (expires, _) in self._items.items() if expires <= now]:
            del self._items[key]
        self._items[token] = (now + ttl_seconds, safety)

    def pop(self, token: UUID) -> DemoSafety | None:
        item = self._items.pop(token, None)
        if item is None or item[0] <= self._clock():
            return None
        return item[1]
