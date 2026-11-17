"""PrusaLink local API v1 (https://github.com/prusa3d/Prusa-Link-Web/blob/master/spec/openapi.yaml).

GPL-3.0; API only. Authenticates with the printer's API key (X-Api-Key). PrusaConnect is cloud and
comes after v0.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path

from .. import net
from ..model import Camera, Status
from .base import Adapter

STATE_MAP = {"IDLE": "idle", "READY": "idle", "PRINTING": "printing", "PAUSED": "paused",
             "FINISHED": "complete", "STOPPED": "cancelled", "ERROR": "error", "ATTENTION": "error",
             "BUSY": "busy"}


class PrusaLinkAdapter(Adapter):
    type_name = "prusalink"
    label = "PrusaLink"
    default_port = 80
    needs_secret = True
    secret_label = "PrusaLink API key (printer menu: Settings, Network, PrusaLink)"

    @property
    def base(self) -> str:
        return f"http://{self.cfg.host}:{self.port}"

    def _h(self) -> dict:
        return {"X-Api-Key": self.secret or ""}

    def _get(self, path: str) -> dict:
        return net.get_json(self.base + path, headers=self._h(), what=self.cfg.name)

    def _job_id(self) -> int:
        job = self._get("/api/v1/status").get("job") or {}
        if "id" not in job:
            from ..errors import Refused

            raise Refused(f"{self.cfg.name} has no current job")
        return int(job["id"])

    @classmethod
    def probe(cls, host, port=None, timeout=2.0):
        port = port or cls.default_port
        try:
            v = net.get_json(f"http://{host}:{port}/api/version", timeout=timeout)
        except Exception:
            return None
        if "prusalink" in str(v.get("text", "")).lower() or "PrusaLink" in str(v.get("server", "")):
            return {"type": cls.type_name, "host": host, "port": port, "detail": f"PrusaLink {v.get('server', '')}"}
        return None

    def pair(self):
        st = self.status()
        info = self._get("/api/v1/info")
        return {"state": st.state, "serial": info.get("serial", ""), "hostname": info.get("hostname", "")}

    def status(self) -> Status:
        s = self._get("/api/v1/status")
        p, job = s.get("printer") or {}, s.get("job") or {}
        file = ""
        if job.get("id") is not None:
            try:
                j = self._get("/api/v1/job")
                file = (j.get("file") or {}).get("display_name") or (j.get("file") or {}).get("name") or ""
            except Exception:
                file = ""
        prog = job.get("progress")
        return Status(
            printer=self.cfg.name, type=self.type_name, state=STATE_MAP.get(p.get("state", ""), "busy"),
            file=file, progress=prog / 100 if isinstance(prog, (int, float)) else None,
            nozzle_c=p.get("temp_nozzle"), nozzle_target_c=p.get("target_nozzle"),
            bed_c=p.get("temp_bed"), bed_target_c=p.get("target_bed"),
            elapsed_s=job.get("time_printing"), remaining_s=job.get("time_remaining"),
        )

    def upload(self, local_path: Path, remote_name: str) -> str:
        # Print-After-Upload is explicitly off: uploading never starts a print.
        net.request("PUT", f"{self.base}/api/v1/files/usb/{urllib.parse.quote(remote_name)}",
                    body=Path(local_path).read_bytes(), timeout=300,
                    headers={"Content-Type": "application/octet-stream", "Print-After-Upload": "?0",
                             "Overwrite": "?1", **self._h()}, what=self.cfg.name)
        return remote_name

    def _start(self, remote_name: str) -> None:
        net.request("POST", f"{self.base}/api/v1/files/usb/{urllib.parse.quote(remote_name)}",
                    body=b"", headers=self._h(), what=self.cfg.name)

    def _resume(self) -> None:
        net.request("PUT", f"{self.base}/api/v1/job/{self._job_id()}/resume", body=b"", headers=self._h(),
                    what=self.cfg.name)

    def pause(self) -> None:
        net.request("PUT", f"{self.base}/api/v1/job/{self._job_id()}/pause", body=b"", headers=self._h(),
                    what=self.cfg.name)

    def cancel(self) -> None:
        net.request("DELETE", f"{self.base}/api/v1/job/{self._job_id()}", headers=self._h(), what=self.cfg.name)

    def emergency_stop(self) -> str:
        self.cancel()
        return ("PrusaLink has no network emergency stop, so the job was stopped (the printer parks and "
                "turns its heaters off). For a true emergency stop press the reset button on the printer")

    def camera(self) -> Camera:
        return Camera(self.cfg.camera_url or f"{self.base}/api/v1/cameras/snap", self.cfg.stream_url)

    def camera_headers(self) -> dict:
        return self._h()
