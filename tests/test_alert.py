"""The message a trusted contact receives: who, when, where, and that it is not an emergency service."""

from __future__ import annotations

from datetime import UTC, datetime

from imbusy.excuse.schema import Language
from imbusy.safety.trusted_contact import compose_alert, map_url
from imbusy.store.locations import SharedLocation

AT = datetime(2026, 11, 13, 21, 42, tzinfo=UTC)
LOCATION = SharedLocation(
    latitude=40.416775,
    longitude=-3.70379,
    accuracy_m=18.4,
    captured_at=datetime(2026, 11, 13, 21, 30, tzinfo=UTC),
)


def test_alert_says_who_when_where_and_that_it_is_not_an_emergency_service() -> None:
    alert = compose_alert(
        name="Mari",
        language=Language.EN_US,
        triggered_at=AT,
        timezone="Europe/Madrid",
        location=LOCATION,
    )
    assert alert.text.startswith(
        "Mari said their safe word during a call at 22:42 (Europe/Madrid)."
    )
    assert "at 22:30, accurate to about 18 m" in alert.text
    assert map_url(LOCATION) in alert.text
    assert "not an emergency service" in alert.text
    assert "112" in alert.text


def test_alert_without_location_says_so() -> None:
    alert = compose_alert(
        name="Mari", language=Language.ES_ES, triggered_at=AT, timezone=None, location=None
    )
    assert "No me había compartido ninguna ubicación reciente." in alert.text
    assert "(UTC)" in alert.text
    assert "No es un servicio de emergencias" in alert.text
    assert alert.location is None


def test_map_link_rounds_coordinates() -> None:
    assert (
        map_url(LOCATION)
        == "https://www.openstreetmap.org/?mlat=40.41678&mlon=-3.70379#map=17/40.41678/-3.70379"
    )
