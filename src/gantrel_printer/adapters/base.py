"""The adapter contract (docs/PRINTERS.md, Adapter contract).

Every printer type subclasses Adapter. start() and resume() are final here and need a confirm token;
they exist for the app's confirm sheet and are not reachable from the CLI, which never starts or
resumes a print (see safety.py for what that does and does not protect against).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path

from .. import net, safety
from ..config import PrinterConfig
from ..errors import PrinterError, ProtocolError, Refused, SafetyError, Unsupported
from ..model import READY_STATES, Camera, Status


class Adapter(ABC):
    type_name: str = ""
    label: str = ""
    default_port: int = 80
    needs_secret: bool = False
    # An optional secret: pair asks for it in a terminal and takes an empty answer as "none".
    asks_secret: bool = False
    secret_label: str = "API key"
    # Honest record of what has been checked. "fake" means only against the fake in tests/printers.
    proven: str = "fake"

    def __init__(self, cfg: PrinterConfig, secret: str | None = None):
        self.cfg = cfg
        self.secret = secret

    @property
    def port(self) -> int:
        return self.cfg.port or self.default_port

    # ------------------------------------------------------------ discovery and pairing

    @classmethod
    @abstractmethod
    def probe(cls, host: str, port: int | None = None, timeout: float = 2.0) -> dict | None:
        """Read-only check: is a printer of this type at host:port? Return facts or None."""

    def pair(self) -> dict:
        """Check the address and credentials with read-only calls. Return facts to show the person.

        Adapters that pin something at pair time (Bambu's TLS fingerprint) set it on self.cfg.
        """
        st = self.status()
        return {"state": st.state}

    # ------------------------------------------------------------ status

    @abstractmethod
    def status(self) -> Status:
        """Read-only status."""

    def watch(self, interval: float = 5.0, count: int | None = None) -> Iterator[Status]:
        """Status stream. Polls status(); adapters with push APIs may override."""
        n = 0
        while count is None or n < count:
            yield self.status()
            n += 1
            if count is None or n < count:
                time.sleep(interval)

    # ------------------------------------------------------------ files and jobs

    @abstractmethod
    def upload(self, local_path: Path, remote_name: str) -> str:
        """Send a file. Must never start it. Return the name the printer knows it by."""

    # Can this adapter read back a file's size and time on the printer? If not, start relies on the
    # content-hash file name alone (Creality stock firmware has no file info call we know of).
    verifies_remote: bool = True

    def identity(self) -> str:
        """Which physical printer this is: type, host, port, serial, TLS pin. Tokens bind to this."""
        c = self.cfg
        return "|".join((c.type, c.host.lower(), str(self.port), c.serial, c.tls_fingerprint))

    def remote_info(self, remote_name: str) -> dict | None:
        """{'size': int, 'mtime': float | None} for a file on the printer, or None if it isn't there."""
        raise Unsupported(f"{self.label} can't report file details")

    def check_remote(self, job: safety.Job) -> None:
        if not self.verifies_remote:
            return
        info = self.remote_info(job.remote_name)
        if info is None:
            raise SafetyError(f"{job.remote_name} is no longer on {self.cfg.name}", "Upload and confirm again")
        if job.remote_size is None or info.get("size") != job.remote_size or (
                job.remote_mtime is not None and info.get("mtime") != job.remote_mtime):
            raise SafetyError(f"{job.remote_name} on {self.cfg.name} changed after it was uploaded",
                              "Something replaced the file on the printer. Upload and confirm again")

    def start(self, job: safety.Job, token: str | None) -> None:
        # The token is spent first, so any refused or mismatched use below voids it: a file changed and
        # then restored, or a printer name pointed away and back, can never reuse a confirm.
        safety.consume_token(token, "start", self.cfg.name, job.id, job.sha256, self.identity())
        if job.printer != self.cfg.name:
            raise SafetyError(f"job {job.id} is for printer {job.printer}, not {self.cfg.name}")
        if job.identity != self.identity():
            raise SafetyError(f"printer name {self.cfg.name} now points at a different printer than job {job.id}",
                              "Upload and confirm again for this printer")
        safety.verify_job_file(job)
        self.check_remote(job)
        st = self.status()
        if st.state not in READY_STATES:
            raise Refused(f"{self.cfg.name} is {st.state}, so it can't start a new print",
                          "Wait until it is idle, then confirm again")
        self._start(job.remote_name)

    def resume(self, token: str | None) -> None:
        # Any refusal before the binding check still spends the token, like start.
        try:
            st = self.status()
            if st.state != "paused":
                raise Refused(f"{self.cfg.name} is {st.state}, not paused")
            job_id, sha = resume_binding(self, st)
        except PrinterError:
            safety.void_token(token)
            raise
        safety.consume_token(token, "resume", self.cfg.name, job_id, sha, self.identity())
        self._resume()

    @abstractmethod
    def _start(self, remote_name: str) -> None: ...

    @abstractmethod
    def _resume(self) -> None: ...

    @abstractmethod
    def pause(self) -> None:
        """Safe action: no confirm needed."""

    @abstractmethod
    def cancel(self) -> None: ...

    @abstractmethod
    def emergency_stop(self) -> str:
        """Strongest stop the printer offers over the network. Return what it did, plainly."""

    # ------------------------------------------------------------ camera

    @abstractmethod
    def camera(self) -> Camera:
        """Where the camera is. Snapshot and stream URLs; stream_url never holds a secret."""

    def snapshot(self, out: Path) -> Path:
        """Save one camera frame. Default: GET the snapshot URL and check it is an image."""
        cam = self.camera()
        if not cam.snapshot_url:
            raise ProtocolError(f"{self.cfg.name} has no camera snapshot URL",
                                "Pair again with --camera-url pointing at a JPEG snapshot URL")
        # Printer credentials go only to the printer itself, never to a separate camera or relay.
        same = net.same_origin(cam.snapshot_url, "http", self.cfg.host, self.port)
        _, _, data = net.request("GET", cam.snapshot_url, headers=self.camera_headers() if same else {},
                                 timeout=15, what=f"{self.cfg.name}'s camera")
        return write_image(data, out)

    def camera_headers(self) -> dict:
        return {}

    # ------------------------------------------------------------ helpers

    def web_ui(self) -> str:
        """The printer's own web page, if it has one."""
        return ""

    def start_by_hand(self, remote_name: str = "") -> str:
        """Where a person starts or resumes a print. Gantrel's tools never do it."""
        what = f"Start {remote_name}" if remote_name else "Start or resume it"
        ui = self.web_ui()
        return f"{what} on the printer's screen" + (f" or in its web page at {ui}" if ui else "") + "."

    def describe(self) -> str:
        return f"{self.cfg.name}: {self.label} at {self.cfg.host}:{self.port}"


def resume_binding(adapter: Adapter, st: Status) -> tuple[str, str]:
    """What a resume token binds to: one print, the Gantrel job record the printer is paused on.

    Returns ("resume:<job id>", the job's file SHA-256). A print Gantrel did not upload has no record,
    so it fails closed. A file deleted from the printer and uploaded again gets a new job record, so a
    resume confirmed for the old print never resumes the new one, even though the names match.
    """
    job = safety.job_for_print(adapter.cfg.name, adapter.identity(), st.file)
    return "resume:" + job.id, job.sha256


def write_image(data: bytes, out: Path) -> Path:
    kind = net.is_image(data)
    if kind is None:
        raise ProtocolError("the camera did not send an image",
                            "Check the camera URL; some cameras need a relay such as go2rtc")
    out = Path(out)
    if kind == "png" and out.suffix.lower() in (".jpg", ".jpeg"):
        out = out.with_suffix(".png")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out
