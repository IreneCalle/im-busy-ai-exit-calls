"""Short-lived storage for compiled call scripts, keyed by call id.

A script lives from compilation until shortly after its call: TTL = delay + 10 minutes. Redis in
docker compose; an in-process dict when REDIS_URL is empty (demo, tests, single-process dev).
"""

from __future__ import annotations

import time
from typing import Any, Protocol
from uuid import UUID

from imbusy.excuse.schema import CallScript

SCRIPT_GRACE_SECONDS = 600
KEY_PREFIX = "imbusy:script:"


def script_ttl(delay_seconds: int) -> int:
    return delay_seconds + SCRIPT_GRACE_SECONDS


class ScriptStore(Protocol):
    async def put(self, call_id: UUID, script: CallScript, ttl_seconds: int) -> None: ...

    async def get(self, call_id: UUID) -> CallScript | None: ...

    async def delete(self, call_id: UUID) -> None: ...

    async def aclose(self) -> None: ...


class MemoryScriptStore:
    """Dict with expiry checked on read. Not shared across processes."""

    def __init__(self) -> None:
        self._items: dict[UUID, tuple[float, CallScript]] = {}

    async def put(self, call_id: UUID, script: CallScript, ttl_seconds: int) -> None:
        now = time.monotonic()
        for key in [k for k, (expires, _) in self._items.items() if expires <= now]:
            del self._items[key]
        self._items[call_id] = (now + ttl_seconds, script)

    async def get(self, call_id: UUID) -> CallScript | None:
        item = self._items.get(call_id)
        if item is None:
            return None
        expires_at, script = item
        if time.monotonic() >= expires_at:
            del self._items[call_id]
            return None
        return script

    async def delete(self, call_id: UUID) -> None:
        self._items.pop(call_id, None)

    async def aclose(self) -> None:
        self._items.clear()


class RedisScriptStore:
    """`redis.asyncio` store: SET key json EX ttl / GET / DEL."""

    def __init__(self, url: str, client: Any | None = None) -> None:
        if client is None:
            import redis.asyncio as redis

            client = redis.from_url(url, decode_responses=True)
        self._redis = client

    async def put(self, call_id: UUID, script: CallScript, ttl_seconds: int) -> None:
        await self._redis.set(
            KEY_PREFIX + str(call_id), script.model_dump_json(), ex=max(1, ttl_seconds)
        )

    async def get(self, call_id: UUID) -> CallScript | None:
        raw = await self._redis.get(KEY_PREFIX + str(call_id))
        return CallScript.model_validate_json(raw) if raw else None

    async def delete(self, call_id: UUID) -> None:
        await self._redis.delete(KEY_PREFIX + str(call_id))

    async def aclose(self) -> None:
        await self._redis.aclose()


def build_script_store(redis_url: str | None) -> ScriptStore:
    """Redis when configured, memory otherwise."""
    if redis_url:
        return RedisScriptStore(redis_url)
    return MemoryScriptStore()
