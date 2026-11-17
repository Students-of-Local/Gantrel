"""The adapter contract (docs/PRINTERS.md, Adapter contract).

Every printer type subclasses Adapter. start() and resume() are final here: they check the confirm
token and the printer's state before calling the adapter's _start() or _resume(), so no adapter
can start or resume a print without a person's confirm.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path

from .. import net, safety
from ..config import PrinterConfig
from ..errors import ProtocolError, Refused, SafetyError
from ..model import READY_STATES, Camera, Status


class Adapter(ABC):
    type_name: str = ""
    label: str = ""
    default_port: int = 80
    needs_secret: bool = False
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

    def start(self, job: safety.Job, token: str | None) -> None:
        if job.printer != self.cfg.name:
            raise SafetyError(f"job {job.id} is for printer {job.printer}, not {self.cfg.name}")
        safety.verify_job_file(job)
        safety.consume_token(token, "start", self.cfg.name, job.id, job.sha256)
        st = self.status()
        if st.state not in READY_STATES:
            raise Refused(f"{self.cfg.name} is {st.state}, so it can't start a new print",
                          "Wait until it is idle, then confirm again")
        self._start(job.remote_name)

    def resume(self, token: str | None) -> None:
        st = self.status()
        if st.state != "paused":
            raise Refused(f"{self.cfg.name} is {st.state}, not paused")
        safety.consume_token(token, "resume", self.cfg.name, resume_job_id(st), "")
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
        _, _, data = net.request("GET", cam.snapshot_url, headers=self.camera_headers(), timeout=15,
                                 what=f"{self.cfg.name}'s camera")
        return write_image(data, out)

    def camera_headers(self) -> dict:
        return {}

    # ------------------------------------------------------------ helpers

    def describe(self) -> str:
        return f"{self.cfg.name}: {self.label} at {self.cfg.host}:{self.port}"


def resume_job_id(st: Status) -> str:
    """Resume tokens bind to the printer and the file it is paused on."""
    return "resume:" + (st.file or "")


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
