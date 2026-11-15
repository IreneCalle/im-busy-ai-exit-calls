"""TwilioDialer request shape and CallScheduler timing, with a fake Twilio client."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from imbusy.call.dialer import CallScheduler, TwilioCallStatus, TwilioDialer
from imbusy.excuse.schema import CallScript, CompiledExcuse


class FakeCalls:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.updated: list[tuple[str, dict[str, Any]]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.created.append(kwargs)
        return SimpleNamespace(sid="CA123", status="queued")

    def __call__(self, sid: str) -> SimpleNamespace:
        return SimpleNamespace(update=lambda **kw: self.updated.append((sid, kw)))


def make_dialer() -> tuple[TwilioDialer, FakeCalls]:
    calls = FakeCalls()
    dialer = TwilioDialer(
        account_sid="AC1",
        auth_token="t",
        from_number="+15005550006",
        public_base_url="https://x.trycloudflare.com/",
        ring_timeout_seconds=25,
        client=SimpleNamespace(calls=calls),
    )
    return dialer, calls


async def test_place_sends_inline_twiml_and_status_callback() -> None:
    dialer, calls = make_dialer()
    call_id = uuid4()
    placed = await dialer.place(to="+34600111222", twiml="<Response/>", call_id=call_id)
    assert placed.call_sid == "CA123"
    assert placed.status is TwilioCallStatus.QUEUED
    sent = calls.created[0]
    assert sent["to"] == "+34600111222"
    assert sent["from_"] == "+15005550006"
    assert sent["twiml"] == "<Response/>"
    assert sent["status_callback"] == f"https://x.trycloudflare.com/twilio/status?call_id={call_id}"
    assert sent["status_callback_event"] == ["initiated", "ringing", "answered", "completed"]
    assert sent["timeout"] == 25


async def test_hang_up_completes_the_call() -> None:
    dialer, calls = make_dialer()
    await dialer.hang_up("CA9")
    assert calls.updated == [("CA9", {"status": "completed"})]


def _excuse(script: CallScript) -> CompiledExcuse:
    return CompiledExcuse(script=script, delay_seconds=0, prompt_version=1)


async def test_scheduler_fires_after_delay_and_can_cancel(script: CallScript) -> None:
    fired: list[CompiledExcuse] = []

    async def run(excuse: CompiledExcuse) -> None:
        fired.append(excuse)

    scheduler = CallScheduler()
    soon, later = _excuse(script), _excuse(script)
    now = datetime.now(UTC)
    scheduler.schedule(soon, now + timedelta(milliseconds=50), run)
    scheduler.schedule(later, now + timedelta(seconds=5), run)
    assert set(scheduler.pending()) == {soon.call_id, later.call_id}
    assert scheduler.cancel(later.call_id)
    assert not scheduler.cancel(later.call_id)
    await asyncio.sleep(0.15)
    assert fired == [soon]
    assert scheduler.pending() == []
    await scheduler.shutdown()


async def test_rescheduling_replaces_pending_call(script: CallScript) -> None:
    fired: list[datetime] = []

    async def run(excuse: CompiledExcuse) -> None:
        fired.append(datetime.now(UTC))

    scheduler = CallScheduler()
    excuse = _excuse(script)
    scheduler.schedule(excuse, datetime.now(UTC) + timedelta(seconds=5), run)
    scheduler.schedule(excuse, datetime.now(UTC), run)
    await asyncio.sleep(0.05)
    assert len(fired) == 1
    assert scheduler.pending() == []
