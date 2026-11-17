import subprocess
import sys
from pathlib import Path

import keyring
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from memkeyring import MemoryKeyring  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test gets its own config and state folder and an empty in-memory keyring."""
    home = tmp_path / "gp-home"
    monkeypatch.setenv("GANTREL_PRINTER_HOME", str(home))
    MemoryKeyring.store = {}
    keyring.set_keyring(MemoryKeyring())
    return home


@pytest.fixture(scope="session")
def tls_cert(tmp_path_factory):
    d = tmp_path_factory.mktemp("tls")
    cert, key = d / "cert.pem", d / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=FAKEBAMBU",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    return str(cert), str(key)


@pytest.fixture(scope="session")
def tls_cert2(tmp_path_factory):
    """A second, different certificate (an impostor's)."""
    d = tmp_path_factory.mktemp("tls2")
    cert, key = d / "cert.pem", d / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=IMPOSTOR",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    return str(cert), str(key)
