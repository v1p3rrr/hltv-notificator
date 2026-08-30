"""Proxying from the standard environment variables.

What is checked here is exactly what the hand-written parsing exists for
instead of trusting libcurl: uppercase `HTTP_PROXY`, the `NO_PROXY` bypass
(subnets included), and the fact that on a bypass the proxy is disabled
EXPLICITLY rather than "left unset".
"""

import pytest

from hltv_notify.config import Config
from hltv_notify.proxy import ProxySettings

HLTV = "https://www.hltv.org/team/12857/forze-reload"
TELEGRAM = "https://api.telegram.org/bot123/sendMessage"


def settings(**env):
    return ProxySettings.from_env(env)


# ---------------------------------------------------------------- reading


def test_no_env_means_no_change():
    """With no variables the dict is empty — behaviour exactly as before."""
    s = settings()
    assert s.configured is False
    assert s.for_url(HLTV) == {}


def test_uppercase_http_proxy_is_honoured():
    """libcurl itself ignores uppercase HTTP_PROXY, and that is exactly how it
    is written in compose — this case is why the parsing is our own."""
    s = settings(HTTP_PROXY="http://10.0.0.1:20171")
    assert s.for_url("http://www.hltv.org/") == {"all": "http://10.0.0.1:20171"}


def test_lowercase_wins_over_uppercase():
    s = settings(http_proxy="http://lower:1", HTTP_PROXY="http://upper:2")
    assert s.for_url("http://x/") == {"all": "http://lower:1"}


def test_blank_value_is_not_a_setting():
    assert settings(HTTPS_PROXY="   ").configured is False


# ---------------------------------------------------------------- selection


def test_https_uses_https_proxy():
    s = settings(HTTP_PROXY="http://p:20171", HTTPS_PROXY="http://p:20172",
                 ALL_PROXY="socks5h://p:20170")
    assert s.for_url(HLTV) == {"all": "http://p:20172"}


def test_all_proxy_is_the_fallback():
    s = settings(ALL_PROXY="socks5h://p:20170")
    assert s.for_url(HLTV) == {"all": "socks5h://p:20170"}
    assert s.for_url("http://www.hltv.org/") == {"all": "socks5h://p:20170"}


# ---------------------------------------------------------------- NO_PROXY


def test_bypass_disables_proxy_explicitly():
    """An empty string, not an empty dict: a dict would mean "do not set
    CURLOPT_PROXY", and libcurl would pick the environment variable up itself —
    the bypass would not work."""
    s = settings(ALL_PROXY="socks5h://p:20170", NO_PROXY="api.telegram.org")
    assert s.for_url(TELEGRAM) == {"all": ""}
    assert s.for_url(HLTV) == {"all": "socks5h://p:20170"}


@pytest.mark.parametrize("entry, host, expected", [
    ("hltv.org", "www.hltv.org", True),          # a subdomain
    ("hltv.org", "hltv.org", True),              # the domain itself
    (".hltv.org", "www.hltv.org", True),         # a leading dot
    ("hltv.org", "nothltv.org", False),          # not a suffix on a dot boundary
    ("localhost", "localhost", True),
    ("127.0.0.1", "127.0.0.1", True),
    ("192.168.1.0/24", "192.168.1.15", True),    # a subnet
    ("192.168.1.0/24", "192.168.2.15", False),
    ("10.0.0.0/24", "10.0.0.7", True),
    ("example.com:8080", "example.com", True),   # an entry carrying a port
    ("*", "anything-at-all", True),
])
def test_no_proxy_matching(entry, host, expected):
    s = settings(ALL_PROXY="socks5h://p:1", NO_PROXY=entry)
    assert s.bypassed(host) is expected


def test_no_proxy_list_separators_and_spaces():
    s = settings(ALL_PROXY="socks5h://p:1",
                 NO_PROXY="localhost, 127.0.0.1 ,192.168.1.0/24;10.0.0.0/24")
    assert s.bypassed("127.0.0.1") is True
    assert s.bypassed("10.0.0.9") is True
    assert s.bypassed("www.hltv.org") is False


def test_broken_no_proxy_entry_is_ignored_not_fatal():
    s = settings(ALL_PROXY="socks5h://p:1", NO_PROXY="not/a/network/at/all")
    assert s.bypassed("www.hltv.org") is False


def test_no_proxy_alone_changes_nothing():
    """NO_PROXY without a proxy must not touch the requests."""
    assert settings(NO_PROXY="*").for_url(HLTV) == {}


# ---------------------------------------------------------------- logging


def test_describe_hides_the_password():
    s = settings(ALL_PROXY="socks5h://user:secret@p:1080")
    text = s.describe()
    assert "secret" not in text
    assert "p:1080" in text


def test_describe_without_credentials():
    assert "10.0.0.1:20171" in settings(HTTP_PROXY="http://10.0.0.1:20171").describe()


# ---------------------------------------------------------------- config


def test_config_reads_the_environment(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks5h://192.168.1.9:20170")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    config = Config()
    assert config.proxies_for(HLTV) == {"all": "socks5h://192.168.1.9:20170"}
    assert config.proxies_for("http://127.0.0.1:8080/health") == {"all": ""}


def test_config_without_proxy_env(monkeypatch):
    for name in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy",
                 "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    assert Config().proxies_for(HLTV) == {}


# ---------------------------------------------------------------- per direction


def test_no_proxy_splits_the_directions():
    """The only way to split the directions: HLTV through the proxy, Telegram
    direct (or the other way round). There are deliberately no separate
    variables for this."""
    from hltv_notify.notify.telegram import API_BASE
    from hltv_notify.sources.scorebot import SCOREBOT_BASE

    s = settings(ALL_PROXY="socks5h://p:20170", NO_PROXY="api.telegram.org")
    assert s.for_url(HLTV) == {"all": "socks5h://p:20170"}
    assert s.for_url(SCOREBOT_BASE) == {"all": "socks5h://p:20170"}
    assert s.for_url(API_BASE) == {"all": ""}


def test_feed_and_its_warmup_are_decided_separately():
    """The feed client talks to TWO hosts: the feed itself and the match page
    for the warm-up. An exception may cover only one of them."""
    from hltv_notify.sources.scorebot import SCOREBOT_BASE

    s = settings(ALL_PROXY="socks5h://p:1", NO_PROXY="scorebot-lb.hltv.org")
    assert s.for_url(SCOREBOT_BASE) == {"all": ""}
    assert s.for_url("https://www.hltv.org/matches/1/x") == {"all": "socks5h://p:1"}


# ---------------------------------------------------------------- where we may go


@pytest.mark.parametrize("url, ok", [
    ("https://www.hltv.org/team/12857/forze-reload", True),
    ("https://scorebot-lb.hltv.org/socket.io/?EIO=3", True),
    # userinfo: the string STARTS correctly but the host is foreign
    ("https://www.hltv.org@10.0.0.1:8080/matches/1/x", False),
    ("https://www.hltv.org@192.168.1.1/matches/1/x", False),
    # a domain continuation — no at-sign needed at all
    ("https://www.hltv.org.evil.example/matches/1/x", False),
    ("https://evil.example/matches/1/x", False),
    ("http://www.hltv.org/team/1/x", False),          # the scheme must be https
    ("https://127.0.0.1:8080/", False),
    ("", False),
])
def test_only_hltv_hosts_are_reachable(url, ok):
    from hltv_notify.config import url_allowed

    assert url_allowed(url) is ok
