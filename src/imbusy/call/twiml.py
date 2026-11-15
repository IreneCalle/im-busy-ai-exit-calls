"""TwiML for the outbound call: <Connect><ConversationRelay> with the script's greeting and voice.

The document is passed inline to `calls.create(twiml=...)`, which Twilio caps at 4000 characters.
"""

from __future__ import annotations

from dataclasses import dataclass
from xml.sax.saxutils import quoteattr

from imbusy.call.voices import VoiceSpec

TWIML_MAX_CHARS = 4000

# Public paths Twilio calls back on; the routes in imbusy.api.twilio_routes serve them.
RELAY_PATH = "/twilio/relay"
STATUS_PATH = "/twilio/status"
CONNECT_ACTION_PATH = "/twilio/connect-action"


@dataclass(frozen=True)
class RelayTwimlParams:
    relay_url: str  # wss://.../twilio/relay
    action_url: str  # https://.../twilio/connect-action
    call_id: str
    welcome_greeting: str
    language: str
    voice: VoiceSpec
    hints: tuple[str, ...] = ()


def _attrs(pairs: dict[str, str]) -> str:
    return " ".join(f"{name}={quoteattr(value)}" for name, value in pairs.items())


def build_relay_twiml(params: RelayTwimlParams) -> str:
    """Render the TwiML. Raises `ValueError` if it would exceed Twilio's inline limit."""
    if not params.relay_url.startswith("wss://"):
        raise ValueError("ConversationRelay url must start with wss://")
    relay_attrs: dict[str, str] = {
        "url": params.relay_url,
        "welcomeGreeting": params.welcome_greeting,
        "welcomeGreetingInterruptible": "speech",
        "language": params.language,
        "ttsProvider": params.voice.provider.value,
        "voice": params.voice.voice,
        "interruptible": "speech",
        "dtmfDetection": "false",
    }
    if params.hints:
        relay_attrs["hints"] = ",".join(params.hints)
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"<Connect {_attrs({'action': params.action_url})}>"
        f"<ConversationRelay {_attrs(relay_attrs)}>"
        f"<Parameter {_attrs({'name': 'call_id', 'value': params.call_id})}/>"
        "</ConversationRelay>"
        "</Connect>"
        "</Response>"
    )
    if len(twiml) > TWIML_MAX_CHARS:
        raise ValueError(f"TwiML is {len(twiml)} chars; Twilio accepts at most {TWIML_MAX_CHARS}")
    return twiml


def hangup_twiml() -> str:
    """Returned from the <Connect action> callback after the session ends."""
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
