"""A one-off fixture collector for docs/recon/fixtures.

It also verifies the premise of the chosen HTML path: an ordinary HTTP client
gets a 403 where curl_cffi with a browser TLS fingerprint gets a 200. Requests
are strictly sequential, with a pause between them.
"""

import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from curl_cffi import requests

FIXTURES = Path(__file__).resolve().parent.parent / "docs" / "recon" / "fixtures"
IMPERSONATE = "chrome"
PAUSE_SECONDS = 8

PAGES = [
    ("team-12857-forze-reload", "https://www.hltv.org/team/12857/forze-reload"),
    ("match-2397053-live", "https://www.hltv.org/matches/2397053/color-vs-forze-reload-gluck-moscow-cyber-games-2026-closed-qualifier"),
    ("match-2397340-upcoming", "https://www.hltv.org/matches/2397340/ex-rustec-vs-forze-reload-kibertochka-season-2"),
    ("match-2397047-finished", "https://www.hltv.org/matches/2397047/forze-reload-vs-black-phoenix-gluck-moscow-cyber-games-2026-closed-qualifier"),
]


def plain_urllib(url: str) -> str:
    """A control request without fingerprint spoofing."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return f"{resp.status} ({len(resp.read())} bytes)"
    except urllib.error.HTTPError as exc:
        return f"{exc.code}"
    except Exception as exc:  # noqa: BLE001 - a control measurement, the fact is what matters
        return f"error: {type(exc).__name__}"


def main() -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    print(f"{'fixture':<26} {'urllib':<10} {'curl_cffi':<10} saved")
    for name, url in PAGES:
        control = plain_urllib(url)
        time.sleep(PAUSE_SECONDS)
        resp = requests.get(url, impersonate=IMPERSONATE, timeout=30)
        saved = "-"
        if resp.status_code == 200:
            path = FIXTURES / f"{name}.html"
            path.write_text(resp.text, encoding="utf-8")
            saved = f"{path.name} ({len(resp.text)} bytes)"
        print(f"{name:<26} {control:<10} {resp.status_code:<10} {saved}")
        time.sleep(PAUSE_SECONDS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
