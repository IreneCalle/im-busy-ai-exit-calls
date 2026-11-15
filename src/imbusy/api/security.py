"""Request authenticity checks for Telegram and Twilio.

Behind a tunnel or proxy the app sees http://localhost:8000/..., but Twilio signs the public URL
it called. Signatures are therefore always checked against URLs rebuilt from PUBLIC_BASE_URL, never
against `request.url`.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping

from twilio.request_validator import RequestValidator


def telegram_secret_ok(header_value: str | None, expected: str) -> bool:
    """Constant-time check of X-Telegram-Bot-Api-Secret-Token."""
    if header_value is None:
        return False
    return hmac.compare_digest(header_value.encode(), expected.encode())


def public_url(public_base: str, path: str, query: str = "") -> str:
    """The URL Twilio called: public origin + path + raw query string (not re-encoded)."""
    url = public_base.rstrip("/") + path
    return f"{url}?{query}" if query else url


class TwilioSignatureChecker:
    """Wraps `twilio.request_validator.RequestValidator`."""

    def __init__(self, auth_token: str) -> None:
        self._validator = RequestValidator(auth_token)

    def check_webhook(self, url: str, form: Mapping[str, str], signature: str | None) -> bool:
        """POST webhook: signature over the full public URL plus every form parameter."""
        if not signature:
            return False
        return bool(self._validator.validate(url, dict(form), signature))

    def check_websocket(self, wss_url: str, signature: str | None) -> bool:
        """ConversationRelay handshake: the header arrives lowercase (`x-twilio-signature`) and is
        computed over the `wss://` URL exactly as written in the TwiML, with no body parameters.
        """
        if not signature:
            return False
        return bool(self._validator.validate(wss_url, {}, signature))
