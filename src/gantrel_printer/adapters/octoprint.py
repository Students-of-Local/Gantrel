"""OctoPrint REST API (https://docs.octoprint.org/en/master/api/). AGPL-3.0; API only.

Pair with an application key the person creates or approves in OctoPrint (Settings, Application Keys).
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path

from .. import net
from ..errors import Refused
from ..model import Camera, Status
from .base import Adapter


def _state(flags: dict, text: str) -> str:
    if flags.get("error") or (flags.get("closedOrError") and "error" in text.lower()):
        return "error"
    if flags.get("cancelling"):
        return "busy"
    if flags.get("paused") or flags.get("pausing"):
        return "paused"
    if flags.get("printing"):
        return "printing"
    if flags.get("closedOrError"):
        return "offline"
    if flags.get("ready") or flags.get("operational"):
        return "idle"
    return "busy"


class OctoPrintAdapter(Adapter):
    type_name = "octoprint"
    label = "OctoPrint"
    default_port = 80
    needs_secret = True
    secret_label = "OctoPrint application key"

    @property
    def base(self) -> str:
        return f"http://{self.cfg.host}:{self.port}"

    def _h(self) -> dict:
        return {"X-Api-Key": self.secret or ""}

    def _get(self, path: str) -> dict:
        return net.get_json(self.base + path, headers=self._h(), what=self.cfg.name)

    def _post(self, path: str, payload: dict) -> None:
        net.send_json("POST", self.base + path, payload, headers=self._h(), what=self.cfg.name)

    @classmethod
    def probe(cls, host, port=None, timeout=2.0):
        port = port or cls.default_port
        try:
            _, headers, data = net.request("GET", f"http://{host}:{port}/", timeout=timeout)
        except Exception:
            return None
        if b"OctoPrint" in data[:65536]:
            return {"type": cls.type_name, "host": host, "port": port, "detail": "OctoPrint web UI"}
        return None

    def pair(self):
        v = self._get("/api/version")
        st = self.status()
        return {"state": st.state, "octoprint": v.get("server", "")}

    def status(self) -> Status:
        job = self._get("/api/job")
        state_text = job.get("state", "")
        try:
            printer = self._get("/api/printer")
        except Refused:  # 409 when the printer is not connected to OctoPrint
            printer = {}
        flags = printer.get("state", {}).get("flags", {})
        if not flags:
            flags = {"closedOrError": state_text.lower().startswith(("offline", "closed", "error"))}
            low = state_text.lower()
            flags.update(printing=low == "printing", paused=low.startswith("paus"), operational=low == "operational")
        temps = printer.get("temperature", {})
        tool, bed, chamber = temps.get("tool0", {}), temps.get("bed", {}), temps.get("chamber") or {}
        prog = job.get("progress") or {}
        completion = prog.get("completion")
        return Status(
            printer=self.cfg.name, type=self.type_name, state=_state(flags, state_text),
            file=((job.get("job") or {}).get("file") or {}).get("name") or "",
            progress=completion / 100 if isinstance(completion, (int, float)) else None,
            nozzle_c=tool.get("actual"), nozzle_target_c=tool.get("target"),
            bed_c=bed.get("actual"), bed_target_c=bed.get("target"), chamber_c=chamber.get("actual"),
            elapsed_s=prog.get("printTime"), remaining_s=prog.get("printTimeLeft"),
            message=job.get("error", "") or "",
        )

    def upload(self, local_path: Path, remote_name: str) -> str:
        # select and print are false: uploading never starts a print.
        body, ctype = net.multipart({"select": "false", "print": "false"}, "file", remote_name,
                                    Path(local_path).read_bytes())
        net.request("POST", f"{self.base}/api/files/local", body=body, timeout=300,
                    headers={"Content-Type": ctype, **self._h()}, what=self.cfg.name)
        return remote_name

    def _start(self, remote_name: str) -> None:
        self._post("/api/files/local/" + urllib.parse.quote(remote_name), {"command": "select", "print": True})

    def _resume(self) -> None:
        self._post("/api/job", {"command": "pause", "action": "resume"})

    def pause(self) -> None:
        self._post("/api/job", {"command": "pause", "action": "pause"})

    def cancel(self) -> None:
        self._post("/api/job", {"command": "cancel"})

    def emergency_stop(self) -> str:
        self._post("/api/printer/command", {"commands": ["M112"]})
        return ("Sent M112 emergency stop through OctoPrint. Most firmware turns off heaters and motors and "
                "needs a power cycle or reset before printing again")

    def camera(self) -> Camera:
        if self.cfg.camera_url:
            return Camera(self.cfg.camera_url, self.cfg.stream_url)
        try:
            cam = self._get("/api/settings").get("webcam", {})
        except Exception:
            cam = {}
        web = f"http://{self.cfg.host}:{self.port}/"
        snap = cam.get("snapshotUrl") or ""
        stream = cam.get("streamUrl") or ""
        return Camera(urllib.parse.urljoin(web, snap) if snap else "",
                      self.cfg.stream_url or (urllib.parse.urljoin(web, stream) if stream else ""))

    def camera_headers(self) -> dict:
        return self._h()
