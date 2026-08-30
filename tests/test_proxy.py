"""Прокси из стандартных переменных окружения.

Здесь проверяется ровно то, ради чего разбор написан вручную вместо того,
чтобы довериться libcurl: верхний регистр `HTTP_PROXY`, обход по `NO_PROXY`
(включая подсети) и то, что при обходе прокси выключается ЯВНО, а не
«не задаётся».
"""

import pytest

from hltv_notify.config import Config
from hltv_notify.proxy import ProxySettings

HLTV = "https://www.hltv.org/team/12857/forze-reload"
TELEGRAM = "https://api.telegram.org/bot123/sendMessage"


def settings(**env):
    return ProxySettings.from_env(env)


# ---------------------------------------------------------------- чтение


def test_no_env_means_no_change():
    """Без переменных словарь пустой — поведение ровно как до прокси."""
    s = settings()
    assert s.configured is False
    assert s.for_url(HLTV) == {}


def test_uppercase_http_proxy_is_honoured():
    """libcurl сам верхний регистр HTTP_PROXY игнорирует, а в compose пишут
    именно так — ради этого случая разбор и свой."""
    s = settings(HTTP_PROXY="http://10.0.0.1:20171")
    assert s.for_url("http://www.hltv.org/") == {"all": "http://10.0.0.1:20171"}


def test_lowercase_wins_over_uppercase():
    s = settings(http_proxy="http://lower:1", HTTP_PROXY="http://upper:2")
    assert s.for_url("http://x/") == {"all": "http://lower:1"}


def test_blank_value_is_not_a_setting():
    assert settings(HTTPS_PROXY="   ").configured is False


# ---------------------------------------------------------------- выбор


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
    """Пустая строка, а не пустой словарь: словарь означал бы «не задавать
    CURLOPT_PROXY», и libcurl подхватил бы переменную окружения сам —
    обход бы не сработал."""
    s = settings(ALL_PROXY="socks5h://p:20170", NO_PROXY="api.telegram.org")
    assert s.for_url(TELEGRAM) == {"all": ""}
    assert s.for_url(HLTV) == {"all": "socks5h://p:20170"}


@pytest.mark.parametrize("entry, host, expected", [
    ("hltv.org", "www.hltv.org", True),          # поддомен
    ("hltv.org", "hltv.org", True),              # сам домен
    (".hltv.org", "www.hltv.org", True),         # ведущая точка
    ("hltv.org", "nothltv.org", False),          # не хвост по границе точки
    ("localhost", "localhost", True),
    ("127.0.0.1", "127.0.0.1", True),
    ("192.168.1.0/24", "192.168.1.15", True),    # подсеть
    ("192.168.1.0/24", "192.168.2.15", False),
    ("10.0.0.0/24", "10.0.0.7", True),
    ("example.com:8080", "example.com", True),   # запись с портом
    ("*", "что-угодно", True),
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
    s = settings(ALL_PROXY="socks5h://p:1", NO_PROXY="не/сеть/вовсе")
    assert s.bypassed("www.hltv.org") is False


def test_no_proxy_alone_changes_nothing():
    """NO_PROXY без прокси не должен трогать запросы."""
    assert settings(NO_PROXY="*").for_url(HLTV) == {}


# ---------------------------------------------------------------- лог


def test_describe_hides_the_password():
    s = settings(ALL_PROXY="socks5h://user:secret@p:1080")
    text = s.describe()
    assert "secret" not in text
    assert "p:1080" in text


def test_describe_without_credentials():
    assert "10.0.0.1:20171" in settings(HTTP_PROXY="http://10.0.0.1:20171").describe()


# ---------------------------------------------------------------- конфиг


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


# ---------------------------------------------------------------- по направлениям


def test_no_proxy_splits_the_directions():
    """Единственный способ развести направления: HLTV через прокси, Telegram
    напрямую (или наоборот). Отдельных переменных для этого нет намеренно."""
    from hltv_notify.notify.telegram import API_BASE
    from hltv_notify.sources.scorebot import SCOREBOT_BASE

    s = settings(ALL_PROXY="socks5h://p:20170", NO_PROXY="api.telegram.org")
    assert s.for_url(HLTV) == {"all": "socks5h://p:20170"}
    assert s.for_url(SCOREBOT_BASE) == {"all": "socks5h://p:20170"}
    assert s.for_url(API_BASE) == {"all": ""}


def test_feed_and_its_warmup_are_decided_separately():
    """Клиент фида ходит на ДВА хоста: сам фид и страницу матча для прогрева.
    Исключение может касаться только одного из них."""
    from hltv_notify.sources.scorebot import SCOREBOT_BASE

    s = settings(ALL_PROXY="socks5h://p:1", NO_PROXY="scorebot-lb.hltv.org")
    assert s.for_url(SCOREBOT_BASE) == {"all": ""}
    assert s.for_url("https://www.hltv.org/matches/1/x") == {"all": "socks5h://p:1"}
