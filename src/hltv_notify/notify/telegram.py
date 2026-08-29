"""Клиент Telegram Bot API.

Отдельная сессия без impersonation: подмена TLS-фингерпринта нужна только
для HLTV, а к Telegram надо ходить как обычный клиент.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from curl_cffi.requests import AsyncSession

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(RuntimeError):
    def __init__(self, message: str, *, retry_after: Optional[float] = None,
                 fatal: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.fatal = fatal


class Telegram:
    def __init__(self, token: str):
        self._token = token
        self._session: Optional[AsyncSession] = None

    async def _ensure(self) -> AsyncSession:
        if self._session is None:
            self._session = AsyncSession()
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _call(self, method: str, payload: Dict[str, Any], *, timeout: int = 30) -> Any:
        session = await self._ensure()
        url = API.format(token=self._token, method=method)
        try:
            response = await session.post(url, json=payload, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - сеть
            raise TelegramError(f"{type(exc).__name__}: {exc}") from exc

        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001 - невалидный ответ
            raise TelegramError(f"HTTP {response.status_code}, тело не JSON") from exc

        if data.get("ok"):
            return data["result"]

        description = data.get("description", "")
        # Правка тем же текстом — не ошибка, а нормальный исход: счёт мог не
        # измениться между обновлениями.
        if "message is not modified" in description.lower():
            return None
        retry_after = (data.get("parameters") or {}).get("retry_after")
        # 400 и 403 сами не пройдут: неверный chat_id, бот заблокирован,
        # сломанная разметка. Повторять их бессмысленно.
        fatal = response.status_code in (400, 401, 403) and retry_after is None
        raise TelegramError(f"Telegram {response.status_code}: {description}",
                            retry_after=retry_after, fatal=fatal)

    async def send_message(self, chat_id: str, text: str) -> int:
        result = await self._call("sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        return int(result["message_id"])

    async def edit_message_text(self, chat_id: str, message_id: int, text: str) -> None:
        await self._call("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })

    async def get_updates(self, offset: Optional[int], timeout: int = 25) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return await self._call("getUpdates", payload, timeout=timeout + 15)
