"""TwiML for the outbound call."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from imbusy.call.twiml import RelayTwimlParams, build_relay_twiml, hangup_twiml
from imbusy.call.voices import VoiceCatalog
from imbusy.config import TtsProvider
from imbusy.excuse.schema import Language, VoiceGender


def _params(greeting: str = "¿Mari? Oye, soy Carmen.") -> RelayTwimlParams:
    voice = VoiceCatalog(TtsProvider.GOOGLE).pick(Language.ES_ES, VoiceGender.FEMALE)
    return RelayTwimlParams(
        relay_url="wss://example.trycloudflare.com/twilio/relay",
        action_url="https://example.trycloudflare.com/twilio/connect-action",
        call_id="6f1c",
        welcome_greeting=greeting,
        language="es-ES",
        voice=voice,
    )


def test_twiml_structure_and_attributes() -> None:
    root = ET.fromstring(build_relay_twiml(_params()))
    connect = root.find("Connect")
    assert connect is not None
    assert connect.get("action") == "https://example.trycloudflare.com/twilio/connect-action"
    relay = connect.find("ConversationRelay")
    assert relay is not None
    assert relay.get("url") == "wss://example.trycloudflare.com/twilio/relay"
    assert relay.get("language") == "es-ES"
    assert relay.get("ttsProvider") == "Google"
    assert relay.get("voice") == "es-ES-Chirp3-HD-Aoede"
    assert relay.get("interruptible") == "speech"
    param = relay.find("Parameter")
    assert param is not None
    assert (param.get("name"), param.get("value")) == ("call_id", "6f1c")


def test_greeting_is_escaped() -> None:
    """Quotes and ampersands from the model cannot break the XML."""
    tricky = 'Soy "Carmen" & te llamo <ya>'
    root = ET.fromstring(build_relay_twiml(_params(tricky)))
    relay = root.find("Connect/ConversationRelay")
    assert relay is not None
    assert relay.get("welcomeGreeting") == tricky


def test_rejects_non_wss_url() -> None:
    params = _params()
    bad = RelayTwimlParams(**{**params.__dict__, "relay_url": "https://x/twilio/relay"})
    with pytest.raises(ValueError):
        build_relay_twiml(bad)


def test_rejects_twiml_over_4000_chars() -> None:
    with pytest.raises(ValueError):
        build_relay_twiml(_params("a" * 4000))


def test_hangup_twiml() -> None:
    assert ET.fromstring(hangup_twiml()).find("Hangup") is not None
