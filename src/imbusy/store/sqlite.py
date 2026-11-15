"""SQLite repositories (stdlib `sqlite3`, run in a worker thread; volumes here are tiny)."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID

from imbusy.excuse.schema import Language
from imbusy.store.models import CallOutcome, CallRecord, Invite, TrustedContact, UserRecord

R = TypeVar("R")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_user_id INTEGER PRIMARY KEY,
    chat_id          INTEGER NOT NULL,
    phone_e164       TEXT,
    phone_verified_at TEXT,
    language         TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calls (
    call_id          TEXT PRIMARY KEY,
    telegram_user_id INTEGER NOT NULL REFERENCES users(telegram_user_id) ON DELETE CASCADE,
    caller_name      TEXT NOT NULL,
    language         TEXT NOT NULL,
    scheduled_for    TEXT NOT NULL,
    twilio_call_sid  TEXT UNIQUE,
    outcome          TEXT NOT NULL,
    turns            INTEGER NOT NULL DEFAULT 0,
    first_token_p50_ms REAL,
    prompt_version   INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    ended_at         TEXT
);

CREATE INDEX IF NOT EXISTS calls_by_user_time ON calls (telegram_user_id, created_at);

CREATE TABLE IF NOT EXISTS processed_updates (
    update_id   INTEGER PRIMARY KEY,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trusted_contacts (
    user_id          INTEGER PRIMARY KEY REFERENCES users(telegram_user_id) ON DELETE CASCADE,
    contact_user_id  INTEGER NOT NULL,
    contact_chat_id  INTEGER NOT NULL,
    contact_name     TEXT NOT NULL,
    accepted_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS trusted_by_contact ON trusted_contacts (contact_user_id);

CREATE TABLE IF NOT EXISTS invites (
    token            TEXT PRIMARY KEY,
    inviter_id       INTEGER NOT NULL REFERENCES users(telegram_user_id) ON DELETE CASCADE,
    created_at       TEXT NOT NULL,
    expires_at       TEXT NOT NULL,
    contact_user_id  INTEGER,
    contact_chat_id  INTEGER,
    contact_name     TEXT
);
"""

# Columns added after the first release; ALTER TABLE keeps existing databases working.
USER_COLUMNS = {"display_name": "TEXT", "safe_word": "TEXT"}

PENDING_OUTCOMES = (CallOutcome.SCHEDULED, CallOutcome.DIALING, CallOutcome.IN_PROGRESS)


def _ts(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Database:
    """Owns the SQLite file and applies the schema on open. `":memory:"` works for tests."""

    def __init__(self, path: Path | str) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    async def open(self) -> None:
        """Create the parent directory and tables; enable foreign keys."""
        await asyncio.to_thread(self._open_sync)

    def _open_sync(self) -> None:
        if str(self.path) != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        for column, kind in USER_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} {kind}")
        conn.commit()
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await asyncio.to_thread(conn.close)

    async def run(self, fn: Callable[[sqlite3.Connection], R]) -> R:
        """Run `fn` with the connection in a worker thread, serialised, committing on success."""
        if self._conn is None:
            raise RuntimeError("database is not open")
        conn = self._conn

        def call() -> R:
            with self._lock:
                try:
                    result = fn(conn)
                    conn.commit()
                    return result
                except BaseException:
                    conn.rollback()
                    raise

        return await asyncio.to_thread(call)


def _user(row: sqlite3.Row | None) -> UserRecord | None:
    if row is None:
        return None
    return UserRecord(
        telegram_user_id=row["telegram_user_id"],
        chat_id=row["chat_id"],
        phone_e164=row["phone_e164"],
        phone_verified_at=_dt(row["phone_verified_at"]),
        language=Language(row["language"]) if row["language"] else None,
        display_name=row["display_name"],
        safe_word=row["safe_word"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _call(row: sqlite3.Row | None) -> CallRecord | None:
    if row is None:
        return None
    data: dict[str, Any] = dict(row)
    data["scheduled_for"] = datetime.fromisoformat(data["scheduled_for"])
    data["created_at"] = datetime.fromisoformat(data["created_at"])
    data["ended_at"] = _dt(data["ended_at"])
    return CallRecord.model_validate(data)


class UserRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, telegram_user_id: int) -> UserRecord | None:
        return await self._db.run(
            lambda c: _user(
                c.execute(
                    "SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
                ).fetchone()
            )
        )

    async def upsert_chat(
        self,
        telegram_user_id: int,
        chat_id: int,
        now: datetime,
        language: Language | None = None,
        display_name: str | None = None,
    ) -> UserRecord:
        """Create the user on first contact or refresh the chat id, language and name."""

        def op(c: sqlite3.Connection) -> UserRecord:
            c.execute(
                """
                INSERT INTO users (telegram_user_id, chat_id, language, display_name, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (telegram_user_id) DO UPDATE SET
                    chat_id = excluded.chat_id,
                    language = COALESCE(excluded.language, users.language),
                    display_name = COALESCE(excluded.display_name, users.display_name)
                """,
                (
                    telegram_user_id,
                    chat_id,
                    language.value if language else None,
                    display_name,
                    _ts(now),
                ),
            )
            row = c.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
            ).fetchone()
            user = _user(row)
            assert user is not None
            return user

        return await self._db.run(op)

    async def set_verified_phone(
        self, telegram_user_id: int, phone_e164: str, now: datetime
    ) -> None:
        await self._db.run(
            lambda c: c.execute(
                "UPDATE users SET phone_e164 = ?, phone_verified_at = ? WHERE telegram_user_id = ?",
                (phone_e164, _ts(now), telegram_user_id),
            )
        )

    async def set_safe_word(self, telegram_user_id: int, phrase: str | None) -> None:
        """Store the normalised safe word, or clear it with None."""
        await self._db.run(
            lambda c: c.execute(
                "UPDATE users SET safe_word = ? WHERE telegram_user_id = ?",
                (phrase, telegram_user_id),
            )
        )

    async def forget(self, telegram_user_id: int) -> None:
        """Delete the user and, by cascade, their call records."""
        await self._db.run(
            lambda c: c.execute("DELETE FROM users WHERE telegram_user_id = ?", (telegram_user_id,))
        )


class CallRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, record: CallRecord) -> None:
        await self._db.run(
            lambda c: c.execute(
                """
                INSERT INTO calls (call_id, telegram_user_id, caller_name, language, scheduled_for,
                    twilio_call_sid, outcome, turns, first_token_p50_ms, prompt_version, created_at,
                    ended_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record.call_id),
                    record.telegram_user_id,
                    record.caller_name,
                    record.language.value,
                    _ts(record.scheduled_for),
                    record.twilio_call_sid,
                    record.outcome.value,
                    record.turns,
                    record.first_token_p50_ms,
                    record.prompt_version,
                    _ts(record.created_at),
                    _ts(record.ended_at),
                ),
            )
        )

    async def get(self, call_id: UUID) -> CallRecord | None:
        return await self._db.run(
            lambda c: _call(
                c.execute("SELECT * FROM calls WHERE call_id = ?", (str(call_id),)).fetchone()
            )
        )

    async def latest_pending(self, telegram_user_id: int) -> CallRecord | None:
        """Most recent call still SCHEDULED, DIALING or IN_PROGRESS, for /cancel."""
        marks = ",".join("?" for _ in PENDING_OUTCOMES)
        return await self._db.run(
            lambda c: _call(
                c.execute(
                    f"SELECT * FROM calls WHERE telegram_user_id = ? AND outcome IN ({marks}) "
                    "ORDER BY created_at DESC LIMIT 1",
                    (telegram_user_id, *(o.value for o in PENDING_OUTCOMES)),
                ).fetchone()
            )
        )

    async def set_sid(self, call_id: UUID, call_sid: str) -> None:
        await self._db.run(
            lambda c: c.execute(
                "UPDATE calls SET twilio_call_sid = ? WHERE call_id = ?", (call_sid, str(call_id))
            )
        )

    async def set_outcome(
        self, call_id: UUID, outcome: CallOutcome, now: datetime, *, terminal: bool = False
    ) -> None:
        await self._db.run(
            lambda c: c.execute(
                "UPDATE calls SET outcome = ?, ended_at = COALESCE(?, ended_at) WHERE call_id = ?",
                (outcome.value, _ts(now) if terminal else None, str(call_id)),
            )
        )

    async def set_stats(self, call_id: UUID, turns: int, first_token_p50_ms: float | None) -> None:
        await self._db.run(
            lambda c: c.execute(
                "UPDATE calls SET turns = ?, first_token_p50_ms = ? WHERE call_id = ?",
                (turns, first_token_p50_ms, str(call_id)),
            )
        )

    async def count_since(self, telegram_user_id: int, since: datetime) -> int:
        """Calls created by the user since `since`, for the daily limit (cancelled ones excluded)."""

        def op(c: sqlite3.Connection) -> int:
            row = c.execute(
                "SELECT COUNT(*) FROM calls WHERE telegram_user_id = ? AND created_at >= ? "
                "AND outcome != ?",
                (telegram_user_id, _ts(since), CallOutcome.CANCELLED.value),
            ).fetchone()
            return int(row[0])

        return await self._db.run(op)

    async def mark_update_processed(self, update_id: int, now: datetime) -> bool:
        """Insert the Telegram update id; False if it was already processed."""

        def op(c: sqlite3.Connection) -> bool:
            cursor = c.execute(
                "INSERT OR IGNORE INTO processed_updates (update_id, received_at) VALUES (?, ?)",
                (update_id, _ts(now)),
            )
            return cursor.rowcount == 1

        return await self._db.run(op)


def _contact(row: sqlite3.Row | None) -> TrustedContact | None:
    if row is None:
        return None
    data: dict[str, Any] = dict(row)
    data["accepted_at"] = datetime.fromisoformat(data["accepted_at"])
    return TrustedContact.model_validate(data)


def _invite(row: sqlite3.Row | None) -> Invite | None:
    if row is None:
        return None
    data: dict[str, Any] = dict(row)
    data["created_at"] = datetime.fromisoformat(data["created_at"])
    data["expires_at"] = datetime.fromisoformat(data["expires_at"])
    return Invite.model_validate(data)


class TrustRepository:
    """Invitations and the accepted trusted contact (one per user)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create_invite(
        self, token: str, inviter_id: int, now: datetime, expires_at: datetime
    ) -> None:
        await self._db.run(
            lambda c: c.execute(
                "INSERT INTO invites (token, inviter_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, inviter_id, _ts(now), _ts(expires_at)),
            )
        )

    async def open_invite(
        self,
        token: str,
        contact_user_id: int,
        contact_chat_id: int,
        contact_name: str,
        now: datetime,
    ) -> Invite | None:
        """Record who opened the link. None if the token is unknown, expired or their own."""

        def op(c: sqlite3.Connection) -> Invite | None:
            invite = _invite(
                c.execute("SELECT * FROM invites WHERE token = ?", (token,)).fetchone()
            )
            if invite is None or invite.expires_at <= now or invite.inviter_id == contact_user_id:
                return None
            c.execute(
                "UPDATE invites SET contact_user_id = ?, contact_chat_id = ?, contact_name = ? WHERE token = ?",
                (contact_user_id, contact_chat_id, contact_name, token),
            )
            return invite.model_copy(
                update={
                    "contact_user_id": contact_user_id,
                    "contact_chat_id": contact_chat_id,
                    "contact_name": contact_name,
                }
            )

        return await self._db.run(op)

    async def accept(self, contact_user_id: int, now: datetime) -> Invite | None:
        """Accept the most recent unexpired invite this person opened; replaces any earlier contact."""

        def op(c: sqlite3.Connection) -> Invite | None:
            invite = _invite(
                c.execute(
                    "SELECT * FROM invites WHERE contact_user_id = ? AND expires_at > ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (contact_user_id, _ts(now)),
                ).fetchone()
            )
            if invite is None or invite.contact_chat_id is None or invite.contact_name is None:
                return None
            c.execute(
                """
                INSERT INTO trusted_contacts (user_id, contact_user_id, contact_chat_id, contact_name, accepted_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (user_id) DO UPDATE SET
                    contact_user_id = excluded.contact_user_id,
                    contact_chat_id = excluded.contact_chat_id,
                    contact_name = excluded.contact_name,
                    accepted_at = excluded.accepted_at
                """,
                (
                    invite.inviter_id,
                    contact_user_id,
                    invite.contact_chat_id,
                    invite.contact_name,
                    _ts(now),
                ),
            )
            c.execute("DELETE FROM invites WHERE inviter_id = ?", (invite.inviter_id,))
            return invite

        return await self._db.run(op)

    async def decline(self, contact_user_id: int) -> list[int]:
        """Drop every invite this person opened; returns the inviters to tell."""

        def op(c: sqlite3.Connection) -> list[int]:
            rows = c.execute(
                "SELECT DISTINCT inviter_id FROM invites WHERE contact_user_id = ?",
                (contact_user_id,),
            ).fetchall()
            c.execute("DELETE FROM invites WHERE contact_user_id = ?", (contact_user_id,))
            return [int(r[0]) for r in rows]

        return await self._db.run(op)

    async def contact_for(self, user_id: int) -> TrustedContact | None:
        return await self._db.run(
            lambda c: _contact(
                c.execute("SELECT * FROM trusted_contacts WHERE user_id = ?", (user_id,)).fetchone()
            )
        )

    async def leave(self, contact_user_id: int) -> list[int]:
        """Stop being anyone's trusted contact; returns the users who lost their contact."""

        def op(c: sqlite3.Connection) -> list[int]:
            rows = c.execute(
                "SELECT user_id FROM trusted_contacts WHERE contact_user_id = ?", (contact_user_id,)
            ).fetchall()
            c.execute("DELETE FROM trusted_contacts WHERE contact_user_id = ?", (contact_user_id,))
            c.execute("DELETE FROM invites WHERE contact_user_id = ?", (contact_user_id,))
            return [int(r[0]) for r in rows]

        return await self._db.run(op)

    async def remove_contact(self, user_id: int) -> TrustedContact | None:
        """The user drops their own trusted contact; returns who it was so they can be told."""

        def op(c: sqlite3.Connection) -> TrustedContact | None:
            contact = _contact(
                c.execute("SELECT * FROM trusted_contacts WHERE user_id = ?", (user_id,)).fetchone()
            )
            c.execute("DELETE FROM trusted_contacts WHERE user_id = ?", (user_id,))
            c.execute("DELETE FROM invites WHERE inviter_id = ?", (user_id,))
            return contact

        return await self._db.run(op)
