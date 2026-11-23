import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """The planner never needs the person's paired printers: every test gets an empty config folder."""
    monkeypatch.setenv("GANTREL_PRINTER_HOME", str(tmp_path / "gp-home"))
    return tmp_path
