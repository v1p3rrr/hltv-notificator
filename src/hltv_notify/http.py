"""The single point of egress to the network.

Ordinary requests/httpx get a 403 where a browser gets data — the filtering is
by TLS fingerprint, not by headers (measured, see docs/recon/R3). Hence
curl_cffi with an impersonation profile from the very beginning.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

from curl_cffi.requests import AsyncSession

from .config import HARD_MIN_REQUEST_INTERVAL_SECONDS, Config, url_allowed

log = logging.getLogger(__name__)


class SourceRejected(RuntimeError):
    """403: the source did not accept the client. Not a network failure — back
    off for a long while."""


class SourceUnavailable(RuntimeError):
    """Timeout, 5xx, network. The ordinary reason to retry with backoff."""


class BlockedTarget(SourceRejected):
    """The address is not on the allow-list. Retrying is pointless — hence the
    inheritance.

    The last line of defence: match addresses come from the HLTV page, and if
    the parser ever again allows one to be steered at a foreign host, the
    request still will not leave. It also fires on records written to the
    database earlier.
    """


class HltvHttp:
    """Sequential requests with jitter and a rate ceiling.

    The ceiling is process-wide: by construction there are never parallel
    requests (see the "respect the source" rule in the spec).
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
        # A loop, not a single sleep: the timer can return control a few
        # milliseconds early, and the ceiling would then systematically fall
        # short. The promise "no more often than once every N seconds" has to
        # hold literally.
        while True:
            elapsed = time.monotonic() - self._last_request_at
            wait = HARD_MIN_REQUEST_INTERVAL_SECONDS - elapsed
            if wait <= 0:
                return
            log.debug("rate ceiling: waiting %.2fs", wait)
            await asyncio.sleep(wait)

    async def get_text(self, url: str, *, timeout: int = 30, exempt_from_ceiling: bool = False) -> str:
        """GET with retries. `exempt_from_ceiling` is for held connections
        (the live feed's long poll): that is one connection, not frequent
        polling."""
        if not url_allowed(url):
            log.error("request to a foreign address refused: %s", url)
            raise BlockedTarget(f"address is not on the allow-list: {url}")

        attempts = max(1, self._config.http_retries)
        last_error: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            async with self._lock:
                if not exempt_from_ceiling:
                    await self._respect_ceiling()
                session = await self._ensure_session()
                started = time.monotonic()
                try:
                    # The proxy is chosen per ADDRESS: that way NO_PROXY
                    # exceptions apply precisely, rather than "by the session's
                    # base host".
                    response = await session.get(
                        url, timeout=timeout, proxies=self._config.proxies_for(url))
                except Exception as exc:  # noqa: BLE001 - network, timeout, TLS
                    last_error = SourceUnavailable(f"{type(exc).__name__}: {exc}")
                    status: object = "-"
                else:
                    status = response.status_code
                    last_error = None
                finally:
                    self._last_request_at = time.monotonic()
                    duration = time.monotonic() - started

            log.info("GET %s -> %s in %.2fs (attempt %d/%d)", url, status, duration,
                     attempt, attempts)

            if last_error is None:
                if status == 403:
                    self.consecutive_failures += 1
                    raise SourceRejected(f"403 on {url}")
                if status == 429 or (isinstance(status, int) and status >= 500):
                    last_error = SourceUnavailable(f"HTTP {status} on {url}")
                else:
                    self.consecutive_failures = 0
                    return response.text

            if attempt < attempts:
                backoff = min(2 ** attempt, 30) * random.uniform(0.8, 1.2)
                log.warning("retrying in %.1fs: %s", backoff, last_error)
                await asyncio.sleep(backoff)

        self.consecutive_failures += 1
        raise last_error or SourceUnavailable(f"could not fetch {url}")


def jittered(interval: float, spread: float = 0.2) -> float:
    """An interval with jitter, so requests do not land exactly on a grid."""
    return interval * random.uniform(1 - spread, 1 + spread)
