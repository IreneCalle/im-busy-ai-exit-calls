"""Service container built once at startup and stored on `app.state.services`."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import cast

from fastapi import Request, WebSocket

from imbusy.api.security import TwilioSignatureChecker
from imbusy.call.dialer import CallScheduler, Dialer, TwilioDialer
from imbusy.call.launcher import CallLauncher
from imbusy.call.voices import VoiceCatalog
from imbusy.config import Settings
from imbusy.excuse.compiler import ExcuseCompiler
from imbusy.fixtures import ScriptedLLM
from imbusy.llm import LLMClient, build_llm
from imbusy.safety.demo import DemoSafetyStore
from imbusy.safety.guard import DestinationGuard, ScriptLinter, SlidingWindowLimiter
from imbusy.safety.trusted_contact import SafeWordAlerter
from imbusy.store.locations import LocationStore, build_location_store
from imbusy.store.scripts import ScriptStore, build_script_store
from imbusy.store.sqlite import CallRepository, Database, TrustRepository, UserRepository
from imbusy.telegram.bot import BotService
from imbusy.telegram.client import TelegramClient
from imbusy.telegram.safety_bot import SafetyBot

logger = logging.getLogger(__name__)


@dataclass
class Services:
    """Everything the endpoints need. Optional members are None when their provider is not set up."""

    settings: Settings
    llm: LLMClient
    fixture_mode: bool
    voices: VoiceCatalog
    compiler: ExcuseCompiler
    scripts: ScriptStore
    db: Database
    users: UserRepository
    calls: CallRepository
    linter: ScriptLinter
    demo_limiter: SlidingWindowLimiter
    scheduler: CallScheduler
    trust: TrustRepository
    locations: LocationStore
    demo_safety: DemoSafetyStore
    # Real-call mode only
    dialer: Dialer | None = None
    telegram: TelegramClient | None = None
    bot: BotService | None = None
    launcher: CallLauncher | None = None
    twilio_signatures: TwilioSignatureChecker | None = None
    alerter: SafeWordAlerter | None = None


async def build_services(
    settings: Settings,
    llm: LLMClient | None = None,
    *,
    dialer: Dialer | None = None,
    telegram: TelegramClient | None = None,
) -> Services:
    """Wire the container. Tests pass fakes; production builds the real clients.

    Without ANTHROPIC_API_KEY the app runs in fixture mode: a scripted model that plays one
    prepared call per language, so the demo works with no key at all.
    """
    if llm is None:
        llm = build_llm(settings) if settings.llm_enabled else ScriptedLLM()
    fixture_mode = isinstance(llm, ScriptedLLM)
    voices = VoiceCatalog(settings.twilio_tts_provider)
    linter = ScriptLinter()
    compiler = ExcuseCompiler(
        llm,
        voices,
        default_delay_seconds=settings.default_call_delay_seconds,
        max_delay_seconds=settings.max_call_delay_seconds,
        default_timezone=settings.default_timezone,
        linter=linter,
    )
    db = Database(settings.database_path)
    await db.open()
    users, calls = UserRepository(db), CallRepository(db)
    services = Services(
        settings=settings,
        llm=llm,
        fixture_mode=fixture_mode,
        voices=voices,
        compiler=compiler,
        scripts=build_script_store(settings.redis_url),
        db=db,
        users=users,
        calls=calls,
        linter=linter,
        demo_limiter=SlidingWindowLimiter(
            limit=settings.demo_max_sessions_per_hour, window=timedelta(hours=1)
        ),
        scheduler=CallScheduler(),
        trust=TrustRepository(db),
        locations=build_location_store(settings.redis_url),
        demo_safety=DemoSafetyStore(),
    )
    if settings.real_calls_enabled:
        assert settings.twilio_auth_token is not None and settings.public_base_url is not None
        assert settings.telegram_bot_token is not None
        auth_token = settings.twilio_auth_token.get_secret_value()
        services.twilio_signatures = TwilioSignatureChecker(auth_token)
        services.dialer = dialer or TwilioDialer(
            account_sid=cast(str, settings.twilio_account_sid),
            auth_token=auth_token,
            from_number=cast(str, settings.twilio_from_number),
            public_base_url=settings.public_base_url,
            ring_timeout_seconds=settings.twilio_ring_timeout_seconds,
        )
        services.telegram = telegram or TelegramClient(
            settings.telegram_bot_token.get_secret_value()
        )
        services.launcher = CallLauncher(
            dialer=services.dialer,
            users=users,
            calls=calls,
            voices=voices,
            guard=DestinationGuard(),
            public_base_url=settings.public_base_url,
        )
        services.alerter = SafeWordAlerter(
            telegram=services.telegram,
            users=users,
            trust=services.trust,
            locations=services.locations,
            default_timezone=settings.default_timezone,
        )
        safety = SafetyBot(
            telegram=services.telegram,
            users=users,
            trust=services.trust,
            locations=services.locations,
            settings=settings,
        )
        services.bot = BotService(
            telegram=services.telegram,
            users=users,
            calls=calls,
            scripts=services.scripts,
            compiler=compiler,
            scheduler=services.scheduler,
            runner=services.launcher,
            dialer=services.dialer,
            settings=settings,
            safety=safety,
        )
        services.launcher.notify = services.bot.notify_call_outcome
    return services


async def close_services(services: Services) -> None:
    await services.scheduler.shutdown()
    await services.scripts.aclose()
    await services.locations.aclose()
    if services.telegram is not None:
        await services.telegram.aclose()
    await services.db.close()


def services_from_request(request: Request) -> Services:
    return cast(Services, request.app.state.services)


def services_from_websocket(websocket: WebSocket) -> Services:
    return cast(Services, websocket.app.state.services)
