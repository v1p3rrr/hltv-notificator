"""The version lives in one place, and CI reads it the same way.

The publish job in ci.yml pulls the number out of the source with a regex and
checks it against the vX.Y.Z tag. Reformat the line and the regex silently
stops matching, and the image ships with somebody else's number — this test
catches that in advance.
"""

import re
from pathlib import Path

import hltv_notify

SOURCE = Path(__file__).resolve().parent.parent / "src" / "hltv_notify" / "__init__.py"
CI_PATTERN = r'__version__ = "([^"]+)"'


def test_version_is_readable_the_way_ci_reads_it():
    found = re.search(CI_PATTERN, SOURCE.read_text(encoding="utf-8"))
    assert found is not None, "CI would not be able to extract the version with this regex"
    assert found.group(1) == hltv_notify.__version__


def test_version_looks_like_a_release_number():
    assert re.fullmatch(r"\d+\.\d+\.\d+", hltv_notify.__version__)


def test_pyproject_version_matches_the_code():
    """The version now lives in two places: CI reads it from the code, pip from
    pyproject. Drift apart and you get a package with one number and an image
    with another."""
    project = Path(__file__).resolve().parent.parent / "pyproject.toml"
    found = re.search(r'^version = "([^"]+)"', project.read_text(encoding="utf-8"),
                      re.MULTILINE)
    assert found is not None
    assert found.group(1) == hltv_notify.__version__
