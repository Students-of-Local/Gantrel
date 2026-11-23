"""prints.log: one JSON line for every attempt to start or resume a print, so a person can review them.

It lives in Gantrel's config folder (config.home()), mode 0600. It never holds a token or a secret:
time, action, job id, printer, the first 12 hex of the file's SHA-256, the outcome, a short reason,
and the Local Studio caller when there is one. Read it with: gantrel-printer prints
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import config
from .model import printable

OUTCOMES = ("started", "declined", "timeout", "not_in_local_studio", "refused", "error")
FIELD_MAX = 200


def path() -> Path:
    return config.home() / "prints.log"


def _clean(value) -> str | None:
    if value is None:
        return None
    return printable(str(value))[:FIELD_MAX]


def append(action: str, outcome: str, job: str = "", printer: str = "", sha256: str = "", reason: str = "",
           caller: str | None = None) -> dict:
    if outcome not in OUTCOMES:
        raise ValueError(outcome)
    rec = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "action": action, "job": _clean(job),
           "printer": _clean(printer), "sha256": _clean(sha256)[:12], "outcome": outcome}
    if reason:
        rec["reason"] = _clean(reason)
    if caller:
        rec["caller"] = _clean(caller)
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (json.dumps(rec, sort_keys=True) + "\n").encode())
    finally:
        os.close(fd)
    return rec


def read() -> list[dict]:
    p = path()
    if not p.exists():
        return []
    rows = []
    for line in p.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            rows.append(rec)
    return rows
