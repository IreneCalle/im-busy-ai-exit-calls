"""CallLauncher: what happens when a scheduled excuse fires. Builds the TwiML and dials."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

from imbusy.call.dialer import Dialer
from imbusy.call.twiml import (
    CONNECT_ACTION_PATH,
    RELAY_PATH,
    RelayTwimlParams,
    build_relay_twiml,
)
from imbusy.call.voices import VoiceCatalog
from imbusy.excuse.schema import CompiledExcuse
from imbusy.safety.guard import DestinationGuard, GuardViolation
from imbusy.store.models import CallOutcome
from imbusy.store.sqlite import CallRepository, UserRepository

logger = logging.getLogger(__name__)

OutcomeNotifier = Callable[[UUID, CallOutcome], Awaitable[None]]


class CallLauncher:
    """`CallRunner` for the scheduler: re-checks the record, resolves the number, dials."""

    def __init__(
        self,
        *,
        dialer: Dialer,
        users: UserRepository,
        calls: CallRepository,
        voices: VoiceCatalog,
        guard: DestinationGuard,
        public_base_url: str,
    ) -> None:
        self._dialer = dialer
        self._users = users
        self._calls = calls
        self._voices = voices
        self._guard = guard
        self._base = public_base_url.rstrip("/")
        self.notify: OutcomeNotifier | None = None

    def twiml_for(self, excuse: CompiledExcuse) -> str:
        script = excuse.script
        return build_relay_twiml(
            RelayTwimlParams(
                relay_url="wss://" + self._base.removeprefix("https://") + RELAY_PATH,
                action_url=self._base + CONNECT_ACTION_PATH,
                call_id=str(excuse.call_id),
                welcome_greeting=script.opening_line,
                language=script.language.value,
                voice=self._voices.get(script.voice_id),
            )
        )

    async def __call__(self, excuse: CompiledExcuse) -> None:
        record = await self._calls.get(excuse.call_id)
        if record is None or record.outcome is not CallOutcome.SCHEDULED:
            return
        user = await self._users.get(record.telegram_user_id)
        now = datetime.now(UTC)
        try:
            if user is None:
                raise GuardViolation("unknown_user", "user no longer exists")
            destination = self._guard.destination_for(user)
        except GuardViolation as violation:
            logger.warning("call blocked: %s", violation.code)
            await self._calls.set_outcome(excuse.call_id, CallOutcome.FAILED, now, terminal=True)
            return
        await self._calls.set_outcome(excuse.call_id, CallOutcome.DIALING, now)
        try:
            placed = await self._dialer.place(
                to=destination, twiml=self.twiml_for(excuse), call_id=excuse.call_id
            )
        except Exception:
            logger.exception("dial failed", extra={"call_id": str(excuse.call_id)})
            await self._calls.set_outcome(excuse.call_id, CallOutcome.FAILED, now, terminal=True)
            if self.notify is not None:
                await self.notify(excuse.call_id, CallOutcome.FAILED)
            return
        await self._calls.set_sid(excuse.call_id, placed.call_sid)
