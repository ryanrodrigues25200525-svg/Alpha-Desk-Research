"""The package version has one source, alpha_desk_research.__version__, which the build reads."""

import re
from pathlib import Path

import pytest

import alpha_desk_research

PYPROJECT = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")


@pytest.mark.unit
def test_the_build_reads_the_version_from_the_package():
    assert re.search(r'^dynamic = \["version"\]', PYPROJECT, re.M)
    assert 'version = {attr = "alpha_desk_research.__version__"}' in PYPROJECT
    assert not re.search(r'^version = "', PYPROJECT, re.M)
    assert re.fullmatch(r"\d+\.\d+\.\d+(\.dev\d+|rc\d+)?", alpha_desk_research.__version__)
