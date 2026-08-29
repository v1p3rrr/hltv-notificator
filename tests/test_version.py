"""Версия читается из одного места, и CI читает её тем же способом.

Работа publish в ci.yml берёт номер регуляркой из исходника и сверяет его с
тегом vX.Y.Z. Если строку переформатировать, регулярка молча перестанет
совпадать, и образ уедет с чужим номером — этот тест ловит такое заранее.
"""

import re
from pathlib import Path

import hltv_notify

SOURCE = Path(__file__).resolve().parent.parent / "src" / "hltv_notify" / "__init__.py"
CI_PATTERN = r'__version__ = "([^"]+)"'


def test_version_is_readable_the_way_ci_reads_it():
    found = re.search(CI_PATTERN, SOURCE.read_text(encoding="utf-8"))
    assert found is not None, "CI не сможет вытащить версию этой регуляркой"
    assert found.group(1) == hltv_notify.__version__


def test_version_looks_like_a_release_number():
    assert re.fullmatch(r"\d+\.\d+\.\d+", hltv_notify.__version__)


def test_pyproject_version_matches_the_code():
    """Версия теперь в двух местах: в коде её читает CI, в pyproject — pip.
    Разъехавшись, они дадут пакет с одним номером и образ с другим."""
    project = Path(__file__).resolve().parent.parent / "pyproject.toml"
    found = re.search(r'^version = "([^"]+)"', project.read_text(encoding="utf-8"),
                      re.MULTILINE)
    assert found is not None
    assert found.group(1) == hltv_notify.__version__
