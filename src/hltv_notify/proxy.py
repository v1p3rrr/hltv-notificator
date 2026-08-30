"""Proxy support through the standard environment variables.

`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY` — the same names curl,
requests and nearly everything else understands. Schemes: `http://`,
`https://`, `socks5://`, `socks5h://` (with `socks5h` the proxy resolves the
name, not us — usually what you want when getting out of a closed network).

Why we parse them ourselves instead of relying on libcurl:

* `curl_cffi` does not read the environment at all. The session has a
  `trust_env` field, but it has no effect on proxy selection — checked against
  the 0.16.2 sources;
* libcurl underneath does read them, but by its own rules: it deliberately
  ignores `HTTP_PROXY` in UPPERCASE (a CGI legacy, where that variable comes
  from the client). And uppercase is exactly how people write it in
  `compose.yaml`;
* CIDR support (`10.0.0.0/8`) in `NO_PROXY` depends on the libcurl version.

So the decision is made here and handed to the session as an explicit dict.
With no proxy configured the dict is empty and everything behaves as before.
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
    """First non-empty value. Lowercase wins — same as curl."""
    for name in names:
        value = env.get(name)
        if value and value.strip():
            return value.strip()
    return ""


def _split_list(raw: str) -> List[str]:
    return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]


def _strip_port(entry: str) -> str:
    """`example.com:8080` -> `example.com`. IPv6 literals are left alone."""
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

    if "/" in entry:  # subnet: 192.168.1.0/24
        try:
            network = ipaddress.ip_network(entry, strict=False)
            return ipaddress.ip_address(host.strip("[]")) in network
        except ValueError:
            return False

    return host == entry or host.endswith("." + entry)


@dataclass(frozen=True)
class ProxySettings:
    """What was read from the environment. Empty strings mean "not set"."""

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
        """The `proxies` dict for curl_cffi, for one specific address.

        `{}` means no proxy is configured and we behave exactly as before.
        `{"all": ""}` means the address matched `NO_PROXY`: the empty string
        EXPLICITLY disables the proxy in libcurl, because otherwise it would
        pick the environment variable up on its own and the bypass would not
        take effect.
        """
        if not self.configured:
            return {}
        parts = urlparse(url)
        if self.bypassed(parts.hostname or ""):
            return {"all": ""}
        scheme_proxy = self.https if parts.scheme == "https" else self.http
        return {"all": scheme_proxy or self.all or ""}

    def describe(self) -> str:
        """A line for the startup log. Passwords in the URL are not shown."""
        if not self.configured:
            return "no proxy configured"
        parts = []
        for label, value in (("http", self.http), ("https", self.https),
                             ("all", self.all)):
            if value:
                parts.append(f"{label}={_safe(value)}")
        if self.no_proxy:
            parts.append(f"bypass: {self.no_proxy}")
        return "proxy: " + ", ".join(parts)


def _safe(url: str) -> str:
    """Hide credentials: `socks5h://user:pass@host:1080` -> `socks5h://***@host:1080`."""
    head, sep, tail = url.rpartition("@")
    if not sep:
        return url
    scheme, delim, _ = head.partition("://")
    return f"{scheme}{delim}***@{tail}" if delim else f"***@{tail}"
