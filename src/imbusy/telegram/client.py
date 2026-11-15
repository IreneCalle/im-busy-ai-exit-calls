"""Minimal async Bot API client over httpx: setWebhook, deleteWebhook, sendMessage.

Requests go to https://api.telegram.org/bot<token>/<method> as JSON; responses are
{"ok": true, "result": ...} or {"ok": false, "error_code": ..., "description": ...}.
"""

from __future__ import annotations

from typing import Any

import httpx

from imbusy.telegram.models import ReplyMarkup

API_BASE = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4096


class TelegramError(Exception):
    def __init__(self, error_code: int | None, description: str) -> None:
        super().__init__(f"{error_code}: {description}")
        self.error_code = error_code
        self.description = description


class TelegramClient:
    def __init__(self, token: str, http: httpx.AsyncClient | None = None) -> None:
        self._token = token
        self._http = http or httpx.AsyncClient(timeout=10.0)
        self._username: str | None = None

    def method_url(self, method: str) -> str:
        return f"{API_BASE}/bot{self._token}/{method}"

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        """POST a method and return `result`; raises `TelegramError` when ok is false."""
        try:
            response = await self._http.post(self.method_url(method), json=payload)
        except httpx.HTTPError as exc:
            # The exception text can contain the URL, which contains the token.
            raise TelegramError(None, f"{method}: {type(exc).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            raise TelegramError(response.status_code, f"{method}: non-JSON response") from None
        if not body.get("ok"):
            raise TelegramError(body.get("error_code"), str(body.get("description", "unknown")))
        return body.get("result")

    async def set_webhook(self, url: str, secret_token: str) -> None:
        """Register the webhook with `allowed_updates=["message"]` and `drop_pending_updates=True`."""
        await self.call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                # Live location updates arrive as edits of the original location message.
                "allowed_updates": ["message", "edited_message"],
                "drop_pending_updates": True,
            },
        )

    async def delete_webhook(self) -> None:
        await self.call("deleteWebhook", {"drop_pending_updates": True})

    async def send_message(
        self, chat_id: int, text: str, reply_markup: ReplyMarkup | None = None
    ) -> None:
        """Send plain text (no parse_mode, so user-provided names need no escaping)."""
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:MAX_MESSAGE_CHARS]}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup.model_dump(exclude_none=True)
        await self.call("sendMessage", payload)

    async def send_location(
        self, chat_id: int, latitude: float, longitude: float, accuracy_m: float | None = None
    ) -> None:
        """A map pin in the chat (sendLocation); accuracy is capped at the API's 1500 m."""
        payload: dict[str, Any] = {"chat_id": chat_id, "latitude": latitude, "longitude": longitude}
        if accuracy_m is not None:
            payload["horizontal_accuracy"] = min(accuracy_m, 1500.0)
        await self.call("sendLocation", payload)

    async def bot_username(self) -> str:
        """The bot's @username from getMe, cached; needed to build t.me invitation links."""
        if self._username is None:
            me = await self.call("getMe", {})
            self._username = str(me["username"])
        return self._username

    async def aclose(self) -> None:
        await self._http.aclose()
