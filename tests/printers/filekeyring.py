"""A keyring backend in a JSON file, so a test and a child process (watch --background) share secrets.

Used only by tests: the child gets PYTHON_KEYRING_BACKEND=filekeyring.FileKeyring and the file path in
GANTREL_TEST_KEYRING_FILE. Never the real OS keyring.
"""

import json
import os
from pathlib import Path

from keyring.backend import KeyringBackend


class FileKeyring(KeyringBackend):
    priority = 1

    def _path(self) -> Path:
        return Path(os.environ["GANTREL_TEST_KEYRING_FILE"])

    def _load(self) -> dict:
        try:
            return json.loads(self._path().read_text())
        except (OSError, ValueError):
            return {}

    def get_password(self, service, username):
        return self._load().get(f"{service}\n{username}")

    def set_password(self, service, username, password):
        data = self._load()
        data[f"{service}\n{username}"] = password
        self._path().write_text(json.dumps(data))

    def delete_password(self, service, username):
        data = self._load()
        data.pop(f"{service}\n{username}", None)
        self._path().write_text(json.dumps(data))
