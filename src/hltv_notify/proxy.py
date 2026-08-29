"""Прокси по стандартным переменным окружения.

`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY` — те же имена, что понимают
curl, requests и почти всё остальное. Схемы: `http://`, `https://`, `socks5://`,
`socks5h://` (у `socks5h` имя резолвит прокси, а не мы — для выхода из закрытой
сети это обычно то, что нужно).

Почему разбираем сами, а не полагаемся на libcurl:

* `curl_cffi` переменные окружения не читает вовсе. Поле `trust_env` у сессии
  есть, но на выбор прокси не влияет — проверено по исходникам 0.16.2;
* libcurl под ним читает их сам, но по своим правилам: `HTTP_PROXY` в ВЕРХНЕМ
  регистре он игнорирует намеренно (наследие CGI, где эта переменная приходит
  от клиента). В `compose.yaml` же пишут именно в верхнем;
* поддержка CIDR (`10.0.0.0/8`) в `NO_PROXY` зависит от версии libcurl.

Поэтому решение принимается здесь и передаётся в сессию явным словарём. Когда
прокси не задан, словарь пустой и всё работает ровно как раньше.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)


def _first(env: Mapping[str, str], *names: str) -> str:
    """Первое непустое значение. Нижний регистр важнее — так же у curl."""
    for name in names:
        value = env.get(name)
        if value and value.strip():
            return value.strip()
    return ""


def _split_list(raw: str) -> List[str]:
    return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]


def _strip_port(entry: str) -> str:
    """`example.com:8080` → `example.com`. IPv6-литералы не трогаем."""
    if entry.count(":") == 1:
        host, _, port = entry.partition(":")
        if port.isdigit():
            return host
    return entry


def _matches(host: str, entry: str) -> bool:
    if entry == "*":
        return True
    entry = _strip_port(entry.lower().strip(".")).strip("[]")
    if not entry:
        return False

    if "/" in entry:  # подсеть: 192.168.1.0/24
        try:
            network = ipaddress.ip_network(entry, strict=False)
            return ipaddress.ip_address(host.strip("[]")) in network
        except ValueError:
            return False

    return host == entry or host.endswith("." + entry)


@dataclass(frozen=True)
class ProxySettings:
    """Что прочитано из окружения. Пустые строки — «не задано»."""

    http: str = ""
    https: str = ""
    all: str = ""
    no_proxy: str = ""

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "ProxySettings":
        env = os.environ if env is None else env
        return cls(
            http=_first(env, "http_proxy", "HTTP_PROXY"),
            https=_first(env, "https_proxy", "HTTPS_PROXY"),
            all=_first(env, "all_proxy", "ALL_PROXY"),
            no_proxy=_first(env, "no_proxy", "NO_PROXY"),
        )

    @property
    def configured(self) -> bool:
        return bool(self.http or self.https or self.all)

    def bypassed(self, host: str) -> bool:
        host = (host or "").lower().rstrip(".")
        if not host:
            return False
        return any(_matches(host, entry) for entry in _split_list(self.no_proxy))

    def for_url(self, url: str) -> Dict[str, str]:
        """Словарь `proxies` для curl_cffi под конкретный адрес.

        `{}` — прокси не настроен, ведём себя как раньше. `{"all": ""}` — адрес
        попал в `NO_PROXY`: пустая строка ЯВНО выключает прокси в libcurl, иначе
        он подхватил бы переменную окружения сам и обход не сработал бы.
        """
        if not self.configured:
            return {}
        parts = urlparse(url)
        if self.bypassed(parts.hostname or ""):
            return {"all": ""}
        scheme_proxy = self.https if parts.scheme == "https" else self.http
        return {"all": scheme_proxy or self.all or ""}

    def describe(self) -> str:
        """Строка для лога при старте. Пароль в адресе не показываем."""
        if not self.configured:
            return "прокси не задан"
        parts = []
        for label, value in (("http", self.http), ("https", self.https),
                             ("all", self.all)):
            if value:
                parts.append(f"{label}={_safe(value)}")
        if self.no_proxy:
            parts.append(f"без прокси: {self.no_proxy}")
        return "прокси: " + ", ".join(parts)


def _safe(url: str) -> str:
    """Скрыть логин и пароль: `socks5h://user:pass@host:1080` → `socks5h://***@host:1080`."""
    head, sep, tail = url.rpartition("@")
    if not sep:
        return url
    scheme, delim, _ = head.partition("://")
    return f"{scheme}{delim}***@{tail}" if delim else f"***@{tail}"
