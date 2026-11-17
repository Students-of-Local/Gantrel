"""Job records and confirm tokens, kept for the app's confirm sheet. Not wired to anything yet.

Gantrel's tools never start or resume a print: the CLI has no start, resume or confirm command, and
a person starts every print on the printer's screen or its own web UI. Nothing in the CLI mints a
token, so Adapter.start and Adapter.resume are unreachable from it today.

This is not a security boundary. An agent with shell access can reach a printer's API directly
(curl to Moonraker, for example), and a terminal prompt can be faked with a pseudo-terminal. The
real boundary comes later, with Local Studio capability grants and the in-app confirm sheet, which
will be the only caller of mint_token().

What is here for that sheet:

- tokens: single use, short lived (TOKEN_TTL_S), bound to one action, one job record, the SHA-256 of
  that job's file, and the printer's identity (type, host, port, serial, TLS pin). Adapter.start and
  Adapter.resume spend the token before any other check, so a failed or mismatched use voids it;
- start tokens bind to the job the person confirmed; resume tokens bind to the job record of the
  print the printer is paused on (job_for_print), and a print with no Gantrel job record can't be
  resumed with a token at all;
- jobs: the file on the printer is named with its content hash, uploads never replace a file, and
  Adapter.start re-checks the printer's copy (size and modification time).

What a token can't tell apart: the same job record printed twice inside one token's lifetime. The
person confirmed that exact file on that exact printer, and the token still works only once.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
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
    identity: str = ""                # printer identity at upload (Adapter.identity())
    remote_size: int | None = None    # the printer's copy, recorded right after upload
    remote_mtime: float | None = None


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
            est_seconds: float | None = None, identity: str = "", sha256: str | None = None,
            remote_size: int | None = None, remote_mtime: float | None = None) -> Job:
    local = Path(local_path).resolve()
    job = Job(id=secrets.token_hex(4), printer=printer, local_path=str(local), sha256=sha256 or file_sha256(local),
              remote_name=remote_name, material=material, est_seconds=est_seconds, created=time.time(),
              identity=identity, remote_size=remote_size, remote_mtime=remote_mtime)
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


def job_for_print(printer: str, identity: str, reported_file: str) -> Job:
    """The job record of the print a printer reports, matched by the content-hash name on the printer.

    Raises SafetyError when Gantrel has no record of it (a print started from elsewhere): fail closed.
    """
    name = (reported_file or "").replace("\\", "/").rsplit("/", 1)[-1]
    matches = [j for j in list_jobs() if name and j.printer == printer and j.identity == identity
               and j.remote_name == name]
    if not matches:
        raise SafetyError(f"Gantrel has no job record for the print on {printer} ({name or 'no file reported'})",
                          "Only prints uploaded with Gantrel can be resumed from the app; resume it on the printer")
    return matches[0]  # newest: a file uploaded again after a delete gets a new record


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


def mint_token(action: str, printer: str, job_id: str, file_hash: str, identity: str) -> str:
    """For the app's confirm sheet, after a person confirms there. Nothing calls this yet."""
    if action not in ACTIONS:
        raise ValueError(action)
    if not identity:
        raise ValueError("a token must be bound to a printer identity")
    token = secrets.token_urlsafe(18)
    record = {"action": action, "printer": printer, "job": job_id, "sha256": file_hash, "identity": identity,
              "expires": time.time() + TOKEN_TTL_S}
    _write_private(_tokens_dir() / f"{_token_key(token)}.json", record)
    return token


def void_token(token: str | None) -> None:
    """Spend a token without using it, for a request refused before its binding could be checked."""
    if not token:
        return
    key = _token_key(token)
    if (_tokens_dir() / f"{key}.json").exists():
        try:
            os.close(os.open(_tokens_dir() / f"{key}.used", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        except FileExistsError:
            pass


def consume_token(token: str | None, action: str, printer: str, job_id: str, file_hash: str,
                  identity: str) -> None:
    """Raise SafetyError unless token is a fresh, unused token for exactly this action.

    Any use, right or wrong, voids the token, so a token can never be tried twice.
    """
    if not token:
        raise SafetyError(f"{action} needs a confirm token",
                          "Only the app's confirm sheet makes tokens, and it doesn't exist yet")
    key = _token_key(token)
    path = _tokens_dir() / f"{key}.json"
    if not path.exists():
        raise SafetyError("that confirm token is not valid", "Confirm again")
    used = _tokens_dir() / f"{key}.used"
    try:
        fd = os.open(used, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except FileExistsError:
        raise SafetyError("that confirm token was already used", "Each token works once; confirm again") from None
    record = json.loads(path.read_text())
    if time.time() > record["expires"]:
        raise SafetyError("that confirm token expired", f"Tokens last {TOKEN_TTL_S // 60} minutes; confirm again")
    want = {"action": action, "printer": printer, "job": job_id, "sha256": file_hash, "identity": identity}
    names = {"sha256": "file", "identity": "printer (the name now points at another device)"}
    for k, v in want.items():
        if record.get(k) != v:
            raise SafetyError(f"that confirm token is for a different {names.get(k, k)}",
                              "The token is now void; confirm this exact job again")


# ---------------------------------------------------------------- unpair


def forget_printer(printer: str) -> int:
    """Delete a printer's job records and tokens (unpair). Returns how many files went."""
    n = 0
    for d in (_jobs_dir(), _tokens_dir()):
        for p in d.glob("*.json"):
            try:
                if json.loads(p.read_text()).get("printer") == printer:
                    p.unlink()
                    n += 1
            except (OSError, ValueError):
                continue
    return n
