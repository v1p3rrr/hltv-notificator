"""TLS compatibility options must reach curl after Chrome impersonation."""

import asyncio
import time

import pytest
from curl_cffi import AsyncCurl, Curl, CurlOpt
from curl_cffi.requests import AsyncSession

from hltv_notify.config import Config, HARD_MIN_REQUEST_INTERVAL_SECONDS
from hltv_notify.http import HltvHttp, SourceUnavailable
from hltv_notify.proxy import ProxySettings


def test_real_session_applies_tls_groups_after_impersonation(monkeypatch):
    """Exercise the real session and native setters; stop before network I/O."""
    events = []
    native_setopt = Curl.setopt
    native_impersonate = Curl.impersonate

    def setopt(curl, option, value):
        events.append((option, value))
        return native_setopt(curl, option, value)

    def impersonate(curl, profile, *args, **kwargs):
        events.append(("impersonate", profile))
        return native_impersonate(curl, profile, *args, **kwargs)

    def stop_before_transfer(async_curl, curl):
        raise RuntimeError("fixture stopped before transfer")

    monkeypatch.setattr(Curl, "setopt", setopt)
    monkeypatch.setattr(Curl, "impersonate", impersonate)
    monkeypatch.setattr(AsyncCurl, "add_handle", stop_before_transfer)
    proxy = "http://fixture-proxy.invalid:8080"
    config = Config(impersonate="chrome", http_retries=1,
                    proxy=ProxySettings.from_env({"HTTPS_PROXY": proxy}))

    async def run():
        client = HltvHttp(config)
        client._last_request_at = time.monotonic() - HARD_MIN_REQUEST_INTERVAL_SECONDS
        try:
            session = await client._ensure_session()
            assert isinstance(session, AsyncSession)
            assert await client._ensure_session() is session
            assert session.impersonate == config.impersonate
            assert session.verify is True
            with pytest.raises(SourceUnavailable, match="fixture stopped before transfer"):
                await client.get_text("https://www.hltv.org/team/12857/forze-reload")
        finally:
            await client.close()
        assert client._session is None

    asyncio.run(run())
    profile = next(index for index, event in enumerate(events) if event[0] == "impersonate")
    curves = events.index((CurlOpt.SSL_EC_CURVES, b"X25519:P-256:P-384"))
    assert curves > profile
    assert (CurlOpt.PROXY, proxy) in events
    assert (CurlOpt.SSL_VERIFYPEER, 0) not in events
    assert (CurlOpt.SSL_VERIFYHOST, 0) not in events
