"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from imbusy import __version__
from imbusy.api import demo_routes, telegram_routes, twilio_routes
from imbusy.api.deps import build_services, close_services
from imbusy.call.dialer import Dialer
from imbusy.config import Settings, get_settings
from imbusy.llm import LLMClient
from imbusy.telegram.client import TelegramClient

DEFAULT_WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"


def create_app(
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    *,
    dialer: Dialer | None = None,
    telegram: TelegramClient | None = None,
) -> FastAPI:
    """Build the app. Routers for real calls are mounted only when Twilio, Telegram and
    PUBLIC_BASE_URL are all configured; the demo router when DEMO_ENABLED is true.

    Also mounts the built demo page (web/dist) at / when present, and GET /healthz which reports
    which modes are enabled without revealing any configuration values.
    """
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.services = await build_services(settings, llm, dialer=dialer, telegram=telegram)
        try:
            yield
        finally:
            await close_services(app.state.services)

    app = FastAPI(title="I'm Busy", version=__version__, lifespan=lifespan, docs_url=None)

    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        services = request.app.state.services
        return {
            "ok": True,
            "demo": settings.demo_enabled,
            "model": "fixture" if services.fixture_mode else "live",
            "real_calls": settings.real_calls_enabled,
        }

    if settings.demo_enabled:
        app.include_router(demo_routes.router)
    if settings.real_calls_enabled:
        app.include_router(telegram_routes.router)
        app.include_router(twilio_routes.router)

    web_dist = settings.web_dist or DEFAULT_WEB_DIST
    if settings.demo_enabled and (web_dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    return app
