"""Единственная точка выхода в сеть.

Обычные requests/httpx получают 403 там, где браузер получает данные — отсев
идёт по TLS-фингерпринту, а не по заголовкам (замерено, см. docs/recon/R3).
Поэтому curl_cffi с профилем impersonation с самого начала.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

from curl_cffi.requests import AsyncSession

from .config import HARD_MIN_REQUEST_INTERVAL_SECONDS, Config

log = logging.getLogger(__name__)


class SourceRejected(RuntimeError):
    """403: источник не принял клиента. Не сетевой сбой — отступаем надолго."""


class SourceUnavailable(RuntimeError):
    """Таймаут, 5xx, сеть. Обычный повод для повтора с backoff."""


class HltvHttp:
    """Последовательные запросы с джиттером и потолком частоты.

    Потолок общий на весь процесс: параллельных запросов не бывает по
    построению (см. правило «уважай источник» в ТЗ).
    """

    def __init__(self, config: Config):
        self._config = config
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._session: Optional[AsyncSession] = None
        self.consecutive_failures = 0

    async def _ensure_session(self) -> AsyncSession:
        if self._session is None:
            self._session = AsyncSession(impersonate=self._config.impersonate)
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _respect_ceiling(self) -> None:
        # Цикл, а не одиночный sleep: таймер может вернуть управление на
        # несколько миллисекунд раньше срока, и потолок систематически
        # недотягивал бы. Обещание «не чаще, чем раз в N секунд» должно
        # выполняться буквально.
        while True:
            elapsed = time.monotonic() - self._last_request_at
            wait = HARD_MIN_REQUEST_INTERVAL_SECONDS - elapsed
            if wait <= 0:
                return
            log.debug("потолок частоты: ждём %.2fs", wait)
            await asyncio.sleep(wait)

    async def get_text(self, url: str, *, timeout: int = 30, exempt_from_ceiling: bool = False) -> str:
        """GET с повторами. `exempt_from_ceiling` — для удерживаемых соединений
        (long-poll живого фида): это не частый опрос, а одно соединение."""
        attempts = max(1, self._config.http_retries)
        last_error: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            async with self._lock:
                if not exempt_from_ceiling:
                    await self._respect_ceiling()
                session = await self._ensure_session()
                started = time.monotonic()
                try:
                    response = await session.get(url, timeout=timeout)
                except Exception as exc:  # noqa: BLE001 - сеть, таймаут, TLS
                    last_error = SourceUnavailable(f"{type(exc).__name__}: {exc}")
                    status: object = "-"
                else:
                    status = response.status_code
                    last_error = None
                finally:
                    self._last_request_at = time.monotonic()
                    duration = time.monotonic() - started

            log.info("GET %s -> %s за %.2fs (попытка %d/%d)", url, status, duration, attempt, attempts)

            if last_error is None:
                if status == 403:
                    self.consecutive_failures += 1
                    raise SourceRejected(f"403 на {url}")
                if status == 429 or (isinstance(status, int) and status >= 500):
                    last_error = SourceUnavailable(f"HTTP {status} на {url}")
                else:
                    self.consecutive_failures = 0
                    return response.text

            if attempt < attempts:
                backoff = min(2 ** attempt, 30) * random.uniform(0.8, 1.2)
                log.warning("повтор через %.1fs: %s", backoff, last_error)
                await asyncio.sleep(backoff)

        self.consecutive_failures += 1
        raise last_error or SourceUnavailable(f"не удалось получить {url}")


def jittered(interval: float, spread: float = 0.2) -> float:
    """Интервал с джиттером, чтобы запросы не шли ровно по сетке."""
    return interval * random.uniform(1 - spread, 1 + spread)
