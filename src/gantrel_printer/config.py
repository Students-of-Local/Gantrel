"""Printer config and secrets.

The config file holds only non-secret facts (type, host, port, model, camera URLs, Bambu serial,
TLS fingerprint). API keys and access codes live in the OS keyring under the service
"gantrel-printer", never in the file.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit

from .errors import ConfigError

KEYRING_SERVICE = "gantrel-printer"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def home() -> Path:
    """Root for config and state. GANTREL_PRINTER_HOME overrides it (tests, portable installs)."""
    override = os.environ.get("GANTREL_PRINTER_HOME")
    if override:
        return Path(override)
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "gantrel-printer"


def state_dir() -> Path:
    override = os.environ.get("GANTREL_PRINTER_HOME")
    if override:
        d = Path(override) / "state"
    else:
        d = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "gantrel-printer"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def config_path() -> Path:
    return home() / "printers.json"


@dataclass
class PrinterConfig:
    name: str
    type: str
    host: str
    port: int | None = None
    model: str = ""          # profile key, for example creality-k2 (see slicing.PRINTERS)
    camera_url: str = ""     # snapshot URL override, for cameras the printer API doesn't list
    stream_url: str = ""     # live stream URL override; must not contain a secret
    serial: str = ""         # Bambu Lab only
    tls_fingerprint: str = ""  # Bambu Lab only, pinned at pair time
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if not NAME_RE.match(self.name):
            raise ConfigError(f"printer name {self.name!r} is not allowed",
                              "Use 1 to 32 lowercase letters, digits, - or _, like k2 or prusa-mk4")
        for url in (self.camera_url, self.stream_url):
            if url and (urlsplit(url).username or urlsplit(url).password):
                raise ConfigError("camera URLs in the config must not hold a user or password",
                                  "Use a URL without credentials")


def load_all() -> dict[str, PrinterConfig]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except ValueError:
        raise ConfigError(f"{path} is not valid JSON", "Fix or delete it, then pair again") from None
    known = {f.name for f in fields(PrinterConfig)}
    return {n: PrinterConfig(**{k: v for k, v in d.items() if k in known}) for n, d in raw.get("printers", {}).items()}


def save_all(printers: dict[str, PrinterConfig]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    data = {"printers": {n: asdict(p) for n, p in sorted(printers.items())}}
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def get(name: str) -> PrinterConfig:
    printers = load_all()
    if name not in printers:
        known = ", ".join(sorted(printers)) or "none yet"
        raise ConfigError(f"no printer named {name!r} (paired: {known})",
                          "Run gantrel-printer list, or pair it with gantrel-printer pair")
    return printers[name]


# ---------------------------------------------------------------- secrets


def get_secret(name: str) -> str | None:
    import keyring
    import keyring.errors

    try:
        return keyring.get_password(KEYRING_SERVICE, name)
    except keyring.errors.KeyringError as err:
        raise ConfigError(f"could not read the OS keyring ({err.__class__.__name__})",
                          "Unlock the keyring, or set one up (Secret Service on Linux, Keychain on macOS)") from None


def set_secret(name: str, secret: str) -> None:
    import keyring
    import keyring.errors

    try:
        keyring.set_password(KEYRING_SERVICE, name, secret)
    except keyring.errors.KeyringError as err:
        raise ConfigError(f"could not save to the OS keyring ({err.__class__.__name__})",
                          "Unlock the keyring, or set one up; secrets are never written to plain config") from None


def delete_secret(name: str) -> None:
    import keyring
    import keyring.errors

    try:
        keyring.delete_password(KEYRING_SERVICE, name)
    except keyring.errors.PasswordDeleteError:
        pass
    except keyring.errors.KeyringError:
        pass


# ---------------------------------------------------------------- watch settings (vision guard, notices)

NOTIFY_KEY = "_notify_url"  # keyring name; printer names can't start with "_", so it never collides
FONSVIVA_KEY = "_fonsviva_key"  # Gantrel's Fonsviva app key, sent as the bearer to the vision model


def watch_settings_path() -> Path:
    return home() / "watch.json"


def load_watch_settings() -> dict:
    """{"vision": {...}, "notify": {...}}. Holds no secrets: the notice URL lives in the OS keyring."""
    path = watch_settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError:
        raise ConfigError(f"{path} is not valid JSON", "Fix or delete it, then run gantrel-printer watch-config") from None
    return data if isinstance(data, dict) else {}


def save_watch_settings(data: dict) -> None:
    path = watch_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
