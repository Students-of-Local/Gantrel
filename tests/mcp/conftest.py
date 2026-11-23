import sys
from pathlib import Path

import keyring
import pytest

TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS / "printers"))
sys.path.insert(0, str(TESTS / "plan"))
sys.path.insert(0, str(Path(__file__).parent))

from memkeyring import MemoryKeyring  # noqa: E402

LS_ENV = ("LOCAL_STUDIO_ASK", "LOCAL_STUDIO_ASK_TOKEN", "LOCAL_STUDIO_ASK_PID", "LOCAL_STUDIO_THREAD",
          "LOCAL_STUDIO_CALLER", "LOCAL_STUDIO_CALLER_NAME")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Own config and state folder, an empty in-memory keyring, and no Local Studio variables."""
    home = tmp_path / "gp-home"
    monkeypatch.setenv("GANTREL_PRINTER_HOME", str(home))
    MemoryKeyring.store = {}
    keyring.set_keyring(MemoryKeyring())
    for name in LS_ENV:
        monkeypatch.delenv(name, raising=False)
    return home
