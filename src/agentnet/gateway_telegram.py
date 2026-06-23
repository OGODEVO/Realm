"""Telegram Bot API adapter for Realm gateways."""

from __future__ import annotations

import json
from typing import Any

import httpx

TELEGRAM_TEXT_LIMIT = 4096


class TelegramAPIError(RuntimeError):
    """Safe Telegram API error that never includes the bot token URL."""

    def __init__(self, action: str, status_code: int, detail: str) -> None:
        self.action = action
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Telegram API {action} failed with HTTP {status_code}: {detail}")


class TelegramPollingConflict(TelegramAPIError):
    """Raised when another getUpdates poller is already using this bot token."""


class TelegramClient:
    def __init__(self, token: str, *, timeout: float = 30.0) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=timeout + 10.0, write=10.0, pool=10.0)
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get_updates(self, *, offset: int | None, timeout: int = 30) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": json.dumps(["message"]),
        }
        if offset is not None:
            params["offset"] = offset
        try:
            response = await self._client.get(f"{self.base_url}/getUpdates", params=params)
        except httpx.ReadTimeout:
            # Telegram long polling naturally returns no data for long windows.
            # Treat client-side read timeouts as an empty poll, not an error.
            return []
        _raise_for_telegram_status(response, action="getUpdates")
        data = response.json()
        if not data.get("ok"):
            raise TelegramAPIError("getUpdates", 200, _telegram_error_detail(data))
        result = data.get("result")
        return result if isinstance(result, list) else []

    async def send_message(self, chat_id: int, text: str) -> int | None:
        first_message_id: int | None = None
        for chunk in split_telegram_text(text):
            response = await self._client.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                },
            )
            _raise_for_telegram_status(response, action="sendMessage")
            data = response.json()
            if not data.get("ok"):
                raise TelegramAPIError("sendMessage", 200, _telegram_error_detail(data))
            result = data.get("result") if isinstance(data, dict) else None
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if first_message_id is None and isinstance(message_id, int):
                first_message_id = message_id
        return first_message_id

    async def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        response = await self._client.post(
            f"{self.base_url}/editMessageText",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": split_telegram_text(text)[0],
                "disable_web_page_preview": True,
            },
        )
        if response.status_code == 400 and "message is not modified" in response.text.lower():
            return
        _raise_for_telegram_status(response, action="editMessageText")
        data = response.json()
        if not data.get("ok"):
            raise TelegramAPIError("editMessageText", 200, _telegram_error_detail(data))


def split_telegram_text(text: str) -> list[str]:
    value = str(text or "").strip() or "(empty)"
    if len(value) <= TELEGRAM_TEXT_LIMIT:
        return [value]
    chunks: list[str] = []
    remaining = value
    while remaining:
        chunks.append(remaining[:TELEGRAM_TEXT_LIMIT])
        remaining = remaining[TELEGRAM_TEXT_LIMIT:]
    return chunks


def _raise_for_telegram_status(response: httpx.Response, *, action: str) -> None:
    if response.status_code < 400:
        return
    detail = _telegram_response_detail(response)
    if response.status_code == 409 and action == "getUpdates":
        raise TelegramPollingConflict(
            action,
            response.status_code,
            "another gateway or bot client is already polling getUpdates for this bot token",
        )
    raise TelegramAPIError(action, response.status_code, detail)


def _telegram_response_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return _shorten(response.text)
    return _telegram_error_detail(data)


def _telegram_error_detail(data: Any) -> str:
    if isinstance(data, dict):
        description = data.get("description")
        if isinstance(description, str) and description.strip():
            return _shorten(description)
    return _shorten(json.dumps(data, ensure_ascii=False, default=str))


def _shorten(value: str, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
