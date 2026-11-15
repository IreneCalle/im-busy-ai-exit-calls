"""Script store and SQLite repositories."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from imbusy.excuse.schema import CallScript, Language
from imbusy.store.locations import MemoryLocationStore, RedisLocationStore, SharedLocation
from imbusy.store.models import CallOutcome, CallRecord
from imbusy.store.scripts import (
    KEY_PREFIX,
    MemoryScriptStore,
    RedisScriptStore,
)
from imbusy.store.sqlite import CallRepository, Database, TrustRepository, UserRepository

NOW = datetime(2026, 11, 13, 18, 0, tzinfo=UTC)


async def test_memory_store_round_trip_and_delete(script: CallScript) -> None:
    store = MemoryScriptStore()
    call_id = uuid4()
    await store.put(call_id, script, ttl_seconds=60)
    assert await store.get(call_id) == script
    await store.delete(call_id)
    assert await store.get(call_id) is None


async def test_memory_store_expires(script: CallScript) -> None:
    store = MemoryScriptStore()
    call_id = uuid4()
    await store.put(call_id, script, ttl_seconds=0)
    assert await store.get(call_id) is None


class FakeRedis:
    """The three commands RedisScriptStore uses, with the redis-py asyncio signatures."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.data[key] = value
        if ex is not None:
            self.ttl[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def delete(self, key: str) -> int:
        return 1 if self.data.pop(key, None) is not None else 0

    async def aclose(self) -> None:
        return None


async def test_redis_store_round_trip_with_ttl(script: CallScript) -> None:
    fake = FakeRedis()
    store = RedisScriptStore("redis://unused", client=fake)
    call_id = uuid4()
    await store.put(call_id, script, ttl_seconds=720)
    assert fake.ttl[KEY_PREFIX + str(call_id)] == 720
    assert await store.get(call_id) == script
    await store.delete(call_id)
    assert await store.get(call_id) is None


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    database = Database(":memory:")
    await database.open()
    yield database
    await database.close()


def _record(user_id: int, **overrides: Any) -> CallRecord:
    data: dict[str, Any] = {
        "call_id": uuid4(),
        "telegram_user_id": user_id,
        "caller_name": "Tía Carmen",
        "language": Language.ES_ES,
        "scheduled_for": NOW,
        "prompt_version": 1,
        "created_at": NOW,
    }
    data.update(overrides)
    return CallRecord(**data)


async def test_users_upsert_verify_and_forget(db: Database) -> None:
    users, calls = UserRepository(db), CallRepository(db)
    user = await users.upsert_chat(42, 42, NOW, Language.ES_ES)
    assert not user.is_verified
    await users.upsert_chat(42, 43, NOW)
    refreshed = await users.get(42)
    assert refreshed is not None
    assert refreshed.chat_id == 43
    assert refreshed.language is Language.ES_ES
    await users.set_verified_phone(42, "+34600111222", NOW)
    verified = await users.get(42)
    assert verified is not None and verified.is_verified
    await calls.create(_record(42))
    await users.forget(42)
    assert await users.get(42) is None
    assert await calls.count_since(42, NOW - timedelta(days=1)) == 0


async def test_calls_lifecycle_and_daily_count(db: Database) -> None:
    users, calls = UserRepository(db), CallRepository(db)
    await users.upsert_chat(7, 7, NOW)
    first = _record(7, created_at=NOW - timedelta(hours=30))
    second = _record(7, created_at=NOW - timedelta(hours=1))
    cancelled = _record(7, created_at=NOW, outcome=CallOutcome.CANCELLED)
    for record in (first, second, cancelled):
        await calls.create(record)
    assert await calls.count_since(7, NOW - timedelta(hours=24)) == 1
    pending = await calls.latest_pending(7)
    assert pending is not None and pending.call_id == second.call_id
    await calls.set_sid(second.call_id, "CA123")
    await calls.set_outcome(second.call_id, CallOutcome.COMPLETED, NOW, terminal=True)
    await calls.set_stats(second.call_id, 5, 412.0)
    stored = await calls.get(second.call_id)
    assert stored is not None
    assert (stored.twilio_call_sid, stored.outcome, stored.turns) == (
        "CA123",
        CallOutcome.COMPLETED,
        5,
    )
    assert stored.ended_at == NOW
    latest = await calls.latest_pending(7)
    assert latest is not None and latest.call_id == first.call_id


async def test_update_dedup(db: Database) -> None:
    calls = CallRepository(db)
    assert await calls.mark_update_processed(1000, NOW)
    assert not await calls.mark_update_processed(1000, NOW)
    assert await calls.mark_update_processed(1001, NOW)


# --- safe word: location TTL and the trusted-contact consent flow --------------------------------


async def test_location_is_forgotten_after_its_ttl_and_on_request() -> None:
    now = [1000.0]
    store = MemoryLocationStore(clock=lambda: now[0])
    here = SharedLocation(latitude=40.4, longitude=-3.7, captured_at=NOW)
    await store.put("tg:1", here, ttl_seconds=3600)
    now[0] += 3599
    assert await store.get("tg:1") == here
    now[0] += 2
    assert await store.get("tg:1") is None
    await store.put("tg:1", here, ttl_seconds=3600)
    await store.delete("tg:1")
    assert await store.get("tg:1") is None


async def test_redis_location_store_sets_an_expiry() -> None:
    fake = FakeRedis()
    store = RedisLocationStore("redis://unused", client=fake)
    await store.put(
        "tg:1", SharedLocation(latitude=1, longitude=2, captured_at=NOW), ttl_seconds=900
    )
    assert fake.ttl["imbusy:location:tg:1"] == 900
    assert (await store.get("tg:1")) is not None


def test_location_repr_never_shows_coordinates() -> None:
    assert "40.4" not in repr(SharedLocation(latitude=40.4, longitude=-3.7, captured_at=NOW))


async def test_safe_word_is_stored_and_hidden_from_repr(db: Database) -> None:
    users = UserRepository(db)
    await users.upsert_chat(1, 1, NOW, display_name="Mari")
    await users.set_safe_word(1, "pink flamingo")
    user = await users.get(1)
    assert user is not None and user.safe_word == "pink flamingo" and user.display_name == "Mari"
    assert "flamingo" not in repr(user)
    await users.set_safe_word(1, None)
    cleared = await users.get(1)
    assert cleared is not None and cleared.safe_word is None


async def test_invite_must_be_opened_by_someone_else_before_it_expires(db: Database) -> None:
    users, trust = UserRepository(db), TrustRepository(db)
    await users.upsert_chat(1, 1, NOW, display_name="Mari")
    await trust.create_invite("tok", 1, NOW, NOW + timedelta(hours=24))
    assert await trust.open_invite("tok", 1, 1, "Mari", NOW) is None  # their own link
    assert await trust.open_invite("nope", 2, 2, "Ana", NOW) is None
    assert await trust.open_invite("tok", 2, 2, "Ana", NOW + timedelta(hours=25)) is None
    opened = await trust.open_invite("tok", 2, 20, "Ana", NOW)
    assert opened is not None and opened.inviter_id == 1


async def test_accept_makes_one_contact_and_replaces_the_previous_one(db: Database) -> None:
    users, trust = UserRepository(db), TrustRepository(db)
    await users.upsert_chat(1, 1, NOW)
    for contact_id, name in [(2, "Ana"), (3, "Luis")]:
        await trust.create_invite(f"tok{contact_id}", 1, NOW, NOW + timedelta(hours=24))
        await trust.open_invite(f"tok{contact_id}", contact_id, contact_id * 10, name, NOW)
        accepted = await trust.accept(contact_id, NOW)
        assert accepted is not None
    contact = await trust.contact_for(1)
    assert contact is not None
    assert (contact.contact_user_id, contact.contact_chat_id, contact.contact_name) == (
        3,
        30,
        "Luis",
    )
    assert await trust.accept(3, NOW) is None  # nothing pending any more


async def test_decline_leave_and_remove(db: Database) -> None:
    users, trust = UserRepository(db), TrustRepository(db)
    await users.upsert_chat(1, 1, NOW)
    await trust.create_invite("a", 1, NOW, NOW + timedelta(hours=1))
    await trust.open_invite("a", 2, 20, "Ana", NOW)
    assert await trust.decline(2) == [1]
    assert await trust.accept(2, NOW) is None
    await trust.create_invite("b", 1, NOW, NOW + timedelta(hours=1))
    await trust.open_invite("b", 2, 20, "Ana", NOW)
    await trust.accept(2, NOW)
    assert await trust.leave(2) == [1]
    assert await trust.contact_for(1) is None
    await trust.create_invite("c", 1, NOW, NOW + timedelta(hours=1))
    await trust.open_invite("c", 2, 20, "Ana", NOW)
    await trust.accept(2, NOW)
    removed = await trust.remove_contact(1)
    assert removed is not None and removed.contact_name == "Ana"
    assert await trust.contact_for(1) is None


async def test_old_database_gains_the_new_user_columns(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE users (telegram_user_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, "
        "phone_e164 TEXT, phone_verified_at TEXT, language TEXT, created_at TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO users VALUES (1, 1, NULL, NULL, NULL, ?)", (NOW.isoformat(),))
    conn.commit()
    conn.close()
    database = Database(path)
    await database.open()
    try:
        await UserRepository(database).set_safe_word(1, "pink flamingo")
        user = await UserRepository(database).get(1)
        assert user is not None and user.safe_word == "pink flamingo"
    finally:
        await database.close()
