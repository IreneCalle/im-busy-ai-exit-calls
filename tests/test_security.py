"""Webhook authenticity: Telegram secret header and Twilio signatures."""

from __future__ import annotations

import base64
import hashlib
import hmac

from imbusy.api.security import TwilioSignatureChecker, public_url, telegram_secret_ok

TOKEN = "test-auth-token"


def _sign(url: str, params: dict[str, str]) -> str:
    """Twilio's documented scheme: HMAC-SHA1(auth token, url + sorted key/value pairs), base64."""
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(
        hmac.new(TOKEN.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()


def test_telegram_secret() -> None:
    assert telegram_secret_ok("abc_DEF-1", "abc_DEF-1")
    assert not telegram_secret_ok("wrong", "abc_DEF-1")
    assert not telegram_secret_ok(None, "abc_DEF-1")


def test_public_url_keeps_raw_query() -> None:
    assert (
        public_url("https://x.trycloudflare.com/", "/twilio/status", "call_id=a%20b")
        == "https://x.trycloudflare.com/twilio/status?call_id=a%20b"
    )


def test_webhook_signature_round_trip() -> None:
    checker = TwilioSignatureChecker(TOKEN)
    url = "https://x.trycloudflare.com/twilio/status?call_id=1"
    form = {"CallSid": "CA1", "CallStatus": "ringing"}
    assert checker.check_webhook(url, form, _sign(url, form))
    assert not checker.check_webhook(url, {**form, "CallStatus": "completed"}, _sign(url, form))
    assert not checker.check_webhook(url, form, None)


def test_websocket_signature_uses_wss_url_and_no_params() -> None:
    checker = TwilioSignatureChecker(TOKEN)
    wss = "wss://x.trycloudflare.com/twilio/relay"
    assert checker.check_websocket(wss, _sign(wss, {}))
    https_variant = "https://x.trycloudflare.com/twilio/relay"
    assert not checker.check_websocket(wss, _sign(https_variant, {}))
