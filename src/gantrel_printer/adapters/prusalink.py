"""PrusaLink local API v1 (https://github.com/prusa3d/Prusa-Link-Web/blob/master/spec/openapi.yaml).

GPL-3.0; API only. Older firmware authenticates with an API key (X-Api-Key). Newer firmware (MK4, XL,
MINI, Core One) uses HTTP digest auth with the user "maker" and the password the printer shows in
Settings, Network, PrusaLink. The adapter sends the key header and answers a digest challenge with the
same secret, so either works. Digest answers go only to the printer's own address (net.request).
PrusaConnect is cloud and comes after v0.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path

from .. import net
from ..errors import AuthFailed, NotFound
from ..model import Camera, Status
from .base import Adapter, write_image

STATE_MAP = {"IDLE": "idle", "READY": "idle", "PRINTING": "printing", "PAUSED": "paused",
             "FINISHED": "complete", "STOPPED": "cancelled", "ERROR": "error", "ATTENTION": "error",
             "BUSY": "busy"}


class PrusaLinkAdapter(Adapter):
    type_name = "prusalink"
    label = "PrusaLink"
    default_port = 80
    needs_secret = True
    secret_label = "PrusaLink password or API key (printer menu: Settings, Network, PrusaLink)"

    @property
    def base(self) -> str:
        return f"http://{self.cfg.host}:{self.port}"

    def _h(self) -> dict:
        return {"X-Api-Key": self.secret or ""}

    def _digest(self) -> tuple[str, str]:
        return str(self.cfg.extra.get("user") or "maker"), self.secret or ""

    def _req(self, method: str, path: str, **kw):
        """Send the secret one way only: as an API key, or as a digest password, never both.

        The first call finds which one the printer takes and records it, so a digest password is
        never also sent in the clear as X-Api-Key.
        """
        extra = kw.pop("headers", {})
        scheme = self.cfg.extra.get("auth")
        if scheme == "digest":
            return net.request(method, self.base + path, headers=extra, digest=self._digest(),
                               what=self.cfg.name, **kw)
        if scheme == "key":
            return net.request(method, self.base + path, headers={**extra, **self._h()}, what=self.cfg.name, **kw)
        try:
            # Ask with neither first: the printer's challenge says which scheme it wants.
            realm = net.challenge(self.base + "/api/version", 3.0)
        except Exception:
            realm = ""
        self.cfg.extra["auth"] = "digest" if realm.lower().startswith("digest") else "key"
        return self._req(method, path, headers=extra, **kw)

    def _get(self, path: str) -> dict:
        _, _, data = self._req("GET", path)
        return net.parse_json(data, self.cfg.name)

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
        except AuthFailed:
            # Newer firmware asks for digest auth even here; its realm names the Prusa printer API.
            realm = net.challenge(f"http://{host}:{port}/api/version", timeout)
            if realm.lower().startswith("digest") and 'realm="printer api"' in realm.lower():
                return {"type": cls.type_name, "host": host, "port": port,
                        "detail": "PrusaLink (password shown in Settings, Network, PrusaLink)"}
            return None
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
        self._req("PUT", f"/api/v1/files/usb/{urllib.parse.quote(remote_name)}",
                  body=Path(local_path).read_bytes(), timeout=300,
                  headers={"Content-Type": "application/octet-stream", "Print-After-Upload": "?0", "Overwrite": "?0"})
        return remote_name

    def remote_info(self, remote_name: str) -> dict | None:
        try:
            f = self._get("/api/v1/files/usb/" + urllib.parse.quote(remote_name))
        except NotFound:
            return None
        return {"size": f.get("size"), "mtime": f.get("m_timestamp")}

    def _start(self, remote_name: str) -> None:
        self._req("POST", f"/api/v1/files/usb/{urllib.parse.quote(remote_name)}", body=b"")

    def _resume(self) -> None:
        self._req("PUT", f"/api/v1/job/{self._job_id()}/resume", body=b"")

    def pause(self) -> None:
        self._req("PUT", f"/api/v1/job/{self._job_id()}/pause", body=b"")

    def cancel(self) -> None:
        self._req("DELETE", f"/api/v1/job/{self._job_id()}")

    def emergency_stop(self) -> str:
        self.cancel()
        return ("PrusaLink has no network emergency stop, so the job was stopped (the printer parks and "
                "turns its heaters off). For a true emergency stop press the reset button on the printer")

    def web_ui(self) -> str:
        return f"{self.base}/ (PrusaLink)"

    def camera(self) -> Camera:
        return Camera(self.cfg.camera_url or f"{self.base}/api/v1/cameras/snap", self.cfg.stream_url)

    def camera_headers(self) -> dict:
        return self._h()

    def snapshot(self, out: Path) -> Path:
        cam = self.camera()
        if self.cfg.camera_url or not cam.snapshot_url.startswith(self.base + "/"):
            return super().snapshot(out)  # another host never gets the key or a digest answer
        _, _, data = self._req("GET", cam.snapshot_url[len(self.base):], timeout=15)
        return write_image(data, out)
