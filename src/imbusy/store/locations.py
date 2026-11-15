"""Last shared location per user, kept only for a short, explicit time.

Locations never go to SQLite: they live in Redis with an expiry (or in process memory when there
is no Redis) and disappear on their own after `ttl_seconds`. They are read only when the safe word
fires, and the user can delete them at any time.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field

KEY_PREFIX = "imbusy:location:"


def location_key(telegram_user_id: int) -> str:
    """Store key for a Telegram user's location."""
    return f"tg:{telegram_user_id}"


class SharedLocation(BaseModel):
    latitude: float = Field(ge=-90, le=90, repr=False)
    longitude: float = Field(ge=-180, le=180, repr=False)
    accuracy_m: float | None = Field(default=None, ge=0, repr=False)
    captured_at: datetime
    live: bool = False


class LocationStore(Protocol):
    async def put(self, user_key: str, location: SharedLocation, ttl_seconds: int) -> None: ...

    async def get(self, user_key: str) -> SharedLocation | None: ...

    async def delete(self, user_key: str) -> None: ...

    async def aclose(self) -> None: ...


class MemoryLocationStore:
    """Dict with expiry checked on every access. `clock` is injectable for tests."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._items: dict[str, tuple[float, SharedLocation]] = {}

    def _sweep(self) -> None:
        now = self._clock()
        for key in [k for k, (expires, _) in self._items.items() if expires <= now]:
            del self._items[key]

    async def put(self, user_key: str, location: SharedLocation, ttl_seconds: int) -> None:
        self._sweep()
        self._items[user_key] = (self._clock() + ttl_seconds, location)

    async def get(self, user_key: str) -> SharedLocation | None:
        self._sweep()
        item = self._items.get(user_key)
        return item[1] if item else None

    async def delete(self, user_key: str) -> None:
        self._items.pop(user_key, None)

    async def aclose(self) -> None:
        self._items.clear()


class RedisLocationStore:
    """`SET key json EX ttl`, so Redis itself forgets the location."""

    def __init__(self, url: str, client: Any | None = None) -> None:
        if client is None:
            import redis.asyncio as redis

            client = redis.from_url(url, decode_responses=True)
        self._redis = client

    async def put(self, user_key: str, location: SharedLocation, ttl_seconds: int) -> None:
        await self._redis.set(
            KEY_PREFIX + user_key, location.model_dump_json(), ex=max(1, ttl_seconds)
        )

    async def get(self, user_key: str) -> SharedLocation | None:
        raw = await self._redis.get(KEY_PREFIX + user_key)
        return SharedLocation.model_validate_json(raw) if raw else None

    async def delete(self, user_key: str) -> None:
        await self._redis.delete(KEY_PREFIX + user_key)

    async def aclose(self) -> None:
        await self._redis.aclose()


def build_location_store(redis_url: str | None) -> LocationStore:
    if redis_url:
        return RedisLocationStore(redis_url)
    return MemoryLocationStore()
