"""Telegram Bot API client.

A separate session without impersonation: spoofing the TLS fingerprint is only
needed for HLTV, and Telegram should be approached as an ordinary client.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Mapping, Optional

from curl_cffi.requests import AsyncSession

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
API = API_BASE + "/bot{token}/{method}"

# What Telegram tolerates across DIFFERENT chats, near enough: about thirty
# a second. Held a little under it — the cost of being wrong is a 429 and a
# delayed notification, and nothing here is worth the last few percent.
CALLS_PER_SECOND = 25.0


class TelegramError(RuntimeError):
    def __init__(self, message: str, *, retry_after: Optional[float] = None,
                 fatal: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.fatal = fatal


class Telegram:
    def __init__(self, token: str, proxies: Optional[Mapping[str, str]] = None):
        self._token = token
        # Telegram gets its own proxy setting in case api.telegram.org is in
        # NO_PROXY while HLTV is not, or the other way round.
        self._proxies = dict(proxies or {})
        self._session: Optional[AsyncSession] = None
        # The rate limit lives HERE, at the single door out to Telegram,
        # rather than in each of the things that write through it. There are
        # four of them — the event queue, the live score card, command replies
        # and button acknowledgements — and they share one budget of about
        # thirty calls a second. While each held its own limiter, every one of
        # them was within its own rules and together they could still go over.
        self._gate = asyncio.Lock()
        self._last_call = 0.0

    async def _ensure(self) -> AsyncSession:
        if self._session is None:
            self._session = AsyncSession(proxies=self._proxies)
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _pace(self) -> None:
        """Hold the shared rate. The lock covers the bookkeeping only, never
        the request itself — `getUpdates` hangs for twenty-five seconds, and
        holding the gate across that would stop everything else dead."""
        async with self._gate:
            wait = self._last_call + 1.0 / CALLS_PER_SECOND - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    async def _call(self, method: str, payload: Dict[str, Any], *, timeout: int = 30) -> Any:
        session = await self._ensure()
        await self._pace()
        url = API.format(token=self._token, method=method)
        try:
            response = await session.post(url, json=payload, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - network
            raise TelegramError(f"{type(exc).__name__}: {exc}") from exc

        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001 - invalid response
            raise TelegramError(f"HTTP {response.status_code}, body is not JSON") from exc

        if data.get("ok"):
            return data["result"]

        description = data.get("description", "")
        # An edit with the same text is not an error but a normal outcome: the
        # score may not have changed between two updates.
        if "message is not modified" in description.lower():
            return None
        retry_after = (data.get("parameters") or {}).get("retry_after")
        # 400 and 403 will not resolve themselves: a wrong chat_id, the bot
        # blocked, broken markup. Retrying them is pointless.
        fatal = response.status_code in (400, 401, 403) and retry_after is None
        raise TelegramError(f"Telegram {response.status_code}: {description}",
                            retry_after=retry_after, fatal=fatal)

    async def send_message(self, chat_id: str, text: str,
                           reply_markup: Optional[Dict[str, Any]] = None) -> int:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = await self._call("sendMessage", payload)
        return int(result["message_id"])

    async def edit_message_text(self, chat_id: str, message_id: int, text: str,
                                reply_markup: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self._call("editMessageText", payload)

    async def delete_message(self, chat_id: str, message_id: int) -> None:
        """Used to move the live card down: the old copy is removed and a new
        one sent below whatever arrived.

        Goes through `_call` like everything else, so it draws on the same
        global rate budget — a delete is a Telegram call and pretending
        otherwise is how the budget gets overspent.
        """
        await self._call("deleteMessage",
                         {"chat_id": chat_id, "message_id": message_id})

    async def answer_callback_query(self, callback_id: str, text: str = "") -> None:
        """The mandatory answer to a button press.

        Without it Telegram keeps a spinner on the button until it times out,
        and the person concludes the bot has hung.
        """
        await self._call("answerCallbackQuery",
                         {"callback_query_id": callback_id, "text": text[:200]})

    async def set_my_commands(self, commands: List[Dict[str, str]],
                              scope: Optional[Dict[str, Any]] = None) -> None:
        """Register the list Telegram offers when you type "/" in the chat.

        It is what fills the Menu button next to the input field. The list
        replaces whatever was registered before for that scope, so it is
        enough to send the current one on every start.

        Without a scope the list is the default one, shown to everybody. With
        `{"type": "chat", "chat_id": ...}` it applies to one chat only and
        takes precedence there — which is how a command meant for the owner
        stays out of everyone else's hint list.
        """
        payload: Dict[str, Any] = {"commands": commands}
        if scope is not None:
            payload["scope"] = scope
        await self._call("setMyCommands", payload)

    async def get_updates(self, offset: Optional[int], timeout: int = 25) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {"timeout": timeout,
                                   "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return await self._call("getUpdates", payload, timeout=timeout + 15)
