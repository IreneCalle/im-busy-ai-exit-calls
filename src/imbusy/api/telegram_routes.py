"""POST /telegram/webhook."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, Response
from pydantic import ValidationError

from imbusy.api.deps import Services, services_from_request
from imbusy.api.security import telegram_secret_ok
from imbusy.telegram.models import Update

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram", tags=["telegram"])


@router.post("/webhook", status_code=200)
async def telegram_webhook(
    request: Request,
    background: BackgroundTasks,
    services: Services = Depends(services_from_request),
    secret: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
) -> Response:
    """Verify the secret header, parse the `Update`, acknowledge at once, handle in background.

    Telegram retries deliveries that do not get a 2xx quickly, and compiling an excuse takes
    seconds, so the work happens after the response. A missing or wrong secret gets 401 and no
    processing; a body that is not a valid `Update` gets 200 (logged) so Telegram stops retrying.
    """
    expected = services.settings.telegram_webhook_secret
    if expected is None or not telegram_secret_ok(secret, expected.get_secret_value()):
        raise HTTPException(status_code=401, detail="bad secret")
    try:
        update = Update.model_validate_json(await request.body())
    except ValidationError:
        logger.warning("telegram: unparseable update ignored")
        return Response(status_code=200)
    if services.bot is not None:
        background.add_task(services.bot.handle, update)
    return Response(status_code=200)
