"""Outbound calls through Twilio's REST API, and delayed dialing.

`twilio.rest.Client.calls.create(to=..., from_=..., twiml=..., status_callback=...,
status_callback_event=[...], timeout=...)` is synchronous; it runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from imbusy.excuse.schema import CompiledExcuse

logger = logging.getLogger(__name__)


class TwilioCallStatus(StrEnum):
    """`CallStatus` values sent to the status callback (and returned by the Calls API)."""

    QUEUED = "queued"
    INITIATED = "initiated"
    RINGING = "ringing"
    IN_PROGRESS = "in-progress"
    ANSWERED = "answered"
    COMPLETED = "completed"
    BUSY = "busy"
    FAILED = "failed"
    NO_ANSWER = "no-answer"
    CANCELED = "canceled"


TERMINAL_STATUSES = frozenset(
    {
        TwilioCallStatus.COMPLETED,
        TwilioCallStatus.BUSY,
        TwilioCallStatus.FAILED,
        TwilioCallStatus.NO_ANSWER,
        TwilioCallStatus.CANCELED,
    }
)

STATUS_CALLBACK_EVENTS = ["initiated", "ringing", "answered", "completed"]


@dataclass(frozen=True)
class PlacedCall:
    call_sid: str
    status: TwilioCallStatus


class Dialer(Protocol):
    """Places a call. Real implementation: `TwilioDialer`; tests use a recording fake."""

    async def place(self, *, to: str, twiml: str, call_id: UUID) -> PlacedCall: ...

    async def hang_up(self, call_sid: str) -> None: ...


class TwilioDialer:
    """`Dialer` over the `twilio` helper library (9.x)."""

    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        from_number: str,
        public_base_url: str,
        ring_timeout_seconds: int,
        client: Any | None = None,
    ) -> None:
        if client is None:
            from twilio.rest import Client

            client = Client(account_sid, auth_token)
        self._client = client
        self._from = from_number
        self._base = public_base_url.rstrip("/")
        self._timeout = ring_timeout_seconds

    def status_callback_url(self, call_id: UUID) -> str:
        return f"{self._base}/twilio/status?call_id={call_id}"

    async def place(self, *, to: str, twiml: str, call_id: UUID) -> PlacedCall:
        """Create the call with inline TwiML and a status callback to /twilio/status?call_id=...

        `to` must already have passed `imbusy.safety.guard.DestinationGuard`.
        """
        call = await asyncio.to_thread(
            self._client.calls.create,
            to=to,
            from_=self._from,
            twiml=twiml,
            status_callback=self.status_callback_url(call_id),
            status_callback_event=STATUS_CALLBACK_EVENTS,
            status_callback_method="POST",
            timeout=self._timeout,
        )
        return PlacedCall(call_sid=str(call.sid), status=TwilioCallStatus(str(call.status)))

    async def hang_up(self, call_sid: str) -> None:
        """Force-complete a call, e.g. on /cancel while it rings."""
        await asyncio.to_thread(lambda: self._client.calls(call_sid).update(status="completed"))


class CallRunner(Protocol):
    async def __call__(self, excuse: CompiledExcuse) -> None: ...


class CallScheduler:
    """Fires compiled excuses after their delay. In-process: pending calls die with the process.

    That trade-off is deliberate for the MVP (delays are minutes, one instance); a durable queue is
    listed as a next step.
    """

    def __init__(self) -> None:
        self._tasks: dict[UUID, asyncio.Task[None]] = {}

    def schedule(self, excuse: CompiledExcuse, fire_at: datetime, run: CallRunner) -> None:
        """Run `run(excuse)` at `fire_at`. Replaces any pending call with the same id."""
        self.cancel(excuse.call_id)
        delay = max(0.0, (fire_at - datetime.now(UTC)).total_seconds())

        async def fire() -> None:
            await asyncio.sleep(delay)
            try:
                await run(excuse)
            except Exception:
                logger.exception("scheduled call failed", extra={"call_id": str(excuse.call_id)})

        task = asyncio.create_task(fire())
        self._tasks[excuse.call_id] = task
        task.add_done_callback(lambda done: self._forget(excuse.call_id, done))

    def _forget(self, call_id: UUID, task: asyncio.Task[None]) -> None:
        if self._tasks.get(call_id) is task:
            del self._tasks[call_id]

    def cancel(self, call_id: UUID) -> bool:
        """Cancel a pending call; returns False if it already fired or is unknown."""
        task = self._tasks.pop(call_id, None)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def pending(self) -> list[UUID]:
        return [call_id for call_id, task in self._tasks.items() if not task.done()]

    async def shutdown(self) -> None:
        """Cancel everything; called on app shutdown."""
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
