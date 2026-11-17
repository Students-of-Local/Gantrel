"""Confirm-to-start safety (docs/PRINTERS.md, Safety rules).

No agent or script can start or resume a print. `gantrel-printer confirm` shows the job and makes
a person type the printer's name at a real terminal (/dev/tty, with stdin and stdout both TTYs).
Only then does it mint a confirm token that is:

- single use (a failed or mismatched use also voids it),
- short lived (TOKEN_TTL_S),
- bound to one action, one printer, one job and the SHA-256 of that job's file.

Adapter.start and Adapter.resume refuse without a token that passes consume_token().

Limit: this stops an agent that follows instructions, and stops a prompt-injected or confused
agent from starting a print by itself, because an agent's shell has no TTY. A hostile program
running as the same user can fake a terminal or edit these files; the in-app confirm sheet is
the stronger boundary planned for later.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config
from .errors import SafetyError

TOKEN_TTL_S = 10 * 60
ACTIONS = ("start", "resume")


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- jobs


@dataclass
class Job:
    id: str
    printer: str
    local_path: str
    sha256: str
    remote_name: str
    material: str = ""
    est_seconds: float | None = None
    created: float = 0.0


def _jobs_dir() -> Path:
    d = config.state_dir() / "jobs"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _write_private(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def new_job(printer: str, local_path: str | Path, remote_name: str, material: str = "",
            est_seconds: float | None = None) -> Job:
    local = Path(local_path).resolve()
    job = Job(id=secrets.token_hex(4), printer=printer, local_path=str(local), sha256=file_sha256(local),
              remote_name=remote_name, material=material, est_seconds=est_seconds, created=time.time())
    _write_private(_jobs_dir() / f"{job.id}.json", asdict(job))
    return job


def load_job(job_id: str) -> Job:
    if not job_id.isalnum():
        raise SafetyError(f"{job_id!r} is not a job id", "Job ids are the short codes upload prints")
    path = _jobs_dir() / f"{job_id}.json"
    if not path.exists():
        raise SafetyError(f"no job {job_id!r}", "Run gantrel-printer jobs to see uploaded jobs")
    return Job(**json.loads(path.read_text()))


def list_jobs() -> list[Job]:
    jobs = [Job(**json.loads(p.read_text())) for p in _jobs_dir().glob("*.json")]
    return sorted(jobs, key=lambda j: j.created, reverse=True)


def verify_job_file(job: Job) -> None:
    """The local file must still be the one that was uploaded and confirmed."""
    try:
        now = file_sha256(job.local_path)
    except OSError:
        raise SafetyError(f"the job's file {job.local_path} is gone",
                          "Slice and upload again; a print only starts from a file you can inspect") from None
    if now != job.sha256:
        raise SafetyError("the job's file changed after upload", "Upload it again and confirm the new file")


# ---------------------------------------------------------------- tokens


def _tokens_dir() -> Path:
    d = config.state_dir() / "tokens"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _token_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint_token(action: str, printer: str, job_id: str, file_hash: str) -> str:
    """Only confirm_interactive() calls this, after a person typed the printer name at a TTY."""
    if action not in ACTIONS:
        raise ValueError(action)
    token = secrets.token_urlsafe(18)
    record = {"action": action, "printer": printer, "job": job_id, "sha256": file_hash,
              "expires": time.time() + TOKEN_TTL_S}
    _write_private(_tokens_dir() / f"{_token_key(token)}.json", record)
    return token


def consume_token(token: str | None, action: str, printer: str, job_id: str, file_hash: str) -> None:
    """Raise SafetyError unless token is a fresh, unused token for exactly this action.

    Any use, right or wrong, voids the token, so a token can never be tried twice.
    """
    if not token:
        raise SafetyError(f"{action} needs a confirm token",
                          "A person must run gantrel-printer confirm at a terminal; agents can't start prints")
    key = _token_key(token)
    path = _tokens_dir() / f"{key}.json"
    if not path.exists():
        raise SafetyError("that confirm token is not valid", "Run gantrel-printer confirm again")
    used = _tokens_dir() / f"{key}.used"
    try:
        fd = os.open(used, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except FileExistsError:
        raise SafetyError("that confirm token was already used", "Each token works once; confirm again") from None
    record = json.loads(path.read_text())
    if time.time() > record["expires"]:
        raise SafetyError("that confirm token expired", f"Tokens last {TOKEN_TTL_S // 60} minutes; confirm again")
    want = {"action": action, "printer": printer, "job": job_id, "sha256": file_hash}
    for k, v in want.items():
        if record.get(k) != v:
            raise SafetyError(f"that confirm token is for a different {'file' if k == 'sha256' else k}",
                              "The token is now void; confirm this exact job again")


# ---------------------------------------------------------------- the human step


def require_tty() -> None:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise SafetyError("confirm must be run by a person at a terminal",
                          "Agents: give the person this exact command to run themselves")


class _Tty:
    """The controlling terminal, read and written directly (it is not seekable, so no buffered r+ file)."""

    def __init__(self, fd: int):
        self.fd = fd

    def write(self, text: str) -> None:
        os.write(self.fd, text.encode("utf-8"))

    def flush(self) -> None:
        pass

    def readline(self, limit: int = 256) -> str:
        buf = b""
        while len(buf) < limit:
            ch = os.read(self.fd, 1)
            if not ch or ch == b"\n":
                break
            buf += ch
        return buf.decode("utf-8", "replace")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        os.close(self.fd)


def open_tty() -> _Tty:
    try:
        return _Tty(os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY))
    except OSError:
        raise SafetyError("no terminal is attached (/dev/tty is not available)",
                          "Run confirm yourself in a terminal window") from None


def confirm_interactive(action: str, printer: str, job_id: str, file_hash: str, lines: list[str]) -> str:
    """Show lines on the terminal, make the person type the printer name, then mint a token."""
    require_tty()
    with open_tty() as tty:
        tty.write("\n".join(lines) + "\n\n")
        tty.write(f"To {action} this print, type the printer name ({printer}) and press Enter.\n"
                  "Anything else cancels: ")
        tty.flush()
        answer = tty.readline().strip()
        if answer != printer:
            raise SafetyError("not confirmed", "Nothing was started")
    return mint_token(action, printer, job_id, file_hash)
