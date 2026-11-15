"""The message a trusted contact receives when the user says their safe word.

It says who sent it, when, and where (the last location the user shared, if it is still within
its time limit), and that it is an automatic message, not an emergency service.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from imbusy.excuse.schema import Language
from imbusy.store.locations import LocationStore, SharedLocation, location_key
from imbusy.store.sqlite import TrustRepository, UserRepository
from imbusy.telegram.client import TelegramClient, TelegramError

logger = logging.getLogger(__name__)

TEXTS: dict[Language, dict[str, str]] = {
    Language.EN_US: {
        "head": "{name} said their safe word during a call at {time} ({zone}). They set you up as the person to tell.",
        "where": "Last location they shared, at {loc_time}{accuracy}: {url}",
        "accuracy": ", accurate to about {meters} m",
        "nowhere": "They had not shared a recent location with me.",
        "foot": (
            "This is an automatic message from the I'm Busy bot. It is not an emergency service and nobody "
            "else has been told. If you are worried, call {name}. If you think they are in danger, call the "
            "local emergency number (112 in Europe, 911 in the US)."
        ),
    },
    Language.ES_ES: {
        "head": "{name} ha dicho su palabra de seguridad durante una llamada a las {time} ({zone}). Te eligió como la persona a quien avisar.",
        "where": "Última ubicación que compartió, a las {loc_time}{accuracy}: {url}",
        "accuracy": ", con un margen de unos {meters} m",
        "nowhere": "No me había compartido ninguna ubicación reciente.",
        "foot": (
            "Este es un mensaje automático del bot I'm Busy. No es un servicio de emergencias y no se ha "
            "avisado a nadie más. Si te preocupa, llama a {name}. Si crees que está en peligro, llama al "
            "número de emergencias (112)."
        ),
    },
}


@dataclass(frozen=True)
class Alert:
    text: str
    location: SharedLocation | None


def map_url(location: SharedLocation) -> str:
    lat, lon = round(location.latitude, 5), round(location.longitude, 5)
    return f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=17/{lat}/{lon}"


def _zone(name: str | None) -> ZoneInfo | None:
    try:
        return ZoneInfo(name) if name else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def compose_alert(
    *,
    name: str,
    language: Language,
    triggered_at: datetime,
    timezone: str | None,
    location: SharedLocation | None,
) -> Alert:
    """Pure: the alert text in the user's language, times shown in their zone."""
    texts = TEXTS[language]
    zone = _zone(timezone)
    local = triggered_at.astimezone(zone) if zone else triggered_at
    zone_label = timezone if zone else "UTC"
    parts = [texts["head"].format(name=name, time=local.strftime("%H:%M"), zone=zone_label)]
    if location is not None:
        captured = location.captured_at.astimezone(zone) if zone else location.captured_at
        accuracy = (
            texts["accuracy"].format(meters=round(location.accuracy_m))
            if location.accuracy_m is not None
            else ""
        )
        parts.append(
            texts["where"].format(
                loc_time=captured.strftime("%H:%M"), accuracy=accuracy, url=map_url(location)
            )
        )
    else:
        parts.append(texts["nowhere"])
    parts.append(texts["foot"].format(name=name))
    return Alert(text="\n\n".join(parts), location=location)


class SafeWordAlerter:
    """Sends the alert to the user's accepted trusted contact over Telegram."""

    def __init__(
        self,
        *,
        telegram: TelegramClient,
        users: UserRepository,
        trust: TrustRepository,
        locations: LocationStore,
        default_timezone: str,
    ) -> None:
        self._telegram = telegram
        self._users = users
        self._trust = trust
        self._locations = locations
        self._timezone = default_timezone

    async def alert(self, telegram_user_id: int, triggered_at: datetime) -> bool:
        """Send the alert; False when there is no trusted contact or Telegram refuses."""
        contact = await self._trust.contact_for(telegram_user_id)
        user = await self._users.get(telegram_user_id)
        if contact is None or user is None:
            logger.warning("safe word heard but no trusted contact is set")
            return False
        location = await self._locations.get(location_key(telegram_user_id))
        alert = compose_alert(
            name=user.display_name or "Your contact",
            language=user.language or Language.EN_US,
            triggered_at=triggered_at,
            timezone=self._timezone,
            location=location,
        )
        try:
            await self._telegram.send_message(contact.contact_chat_id, alert.text)
            if location is not None:
                await self._telegram.send_location(
                    contact.contact_chat_id,
                    location.latitude,
                    location.longitude,
                    location.accuracy_m,
                )
        except TelegramError as exc:
            # 403 here usually means the contact blocked the bot after accepting.
            logger.warning("safe word alert not delivered: %s", exc.error_code)
            return False
        logger.info("safe word alert delivered")
        return True
