import re
from pathlib import Path

import pytest

import specto
from specto.cli import main


def test_version_flag_prints_the_name_and_number_and_stops(capsys):
    with pytest.raises(SystemExit) as cm:
        main(["--version"])
    assert cm.value.code == 0
    assert capsys.readouterr().out.strip() == f"specto {specto.__version__}"


def test_the_number_matches_pyproject():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    declared = re.search(r'^version = "([^"]+)"', text, re.M).group(1)
    assert specto.__version__ == declared
