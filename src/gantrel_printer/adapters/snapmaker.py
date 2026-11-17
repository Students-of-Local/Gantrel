"""Snapmaker 2.0 (A150, A250, A350) through the HTTP API on port 8080 that Snapmaker Luban uses.

Pairing asks the printer for a token (POST /api/v1/connect). The touchscreen then asks the person to
accept the connection; until they do, /api/v1/status answers 204. The accepted token is the secret
kept in the keyring and sent only to the printer.

Reference: Snapmaker Luban (AGPL-3.0, https://github.com/Snapmaker/Luban), read for the API only, and
the Snapmaker forum notes on the same API (https://forum.snapmaker.com/t/status-rest-api/27206).

The J1 and the Artisan may speak only Snapmaker's SACP protocol (TCP 8888) on newer firmware, which
this adapter does not speak; with them it is untested.
"""

from __future__ import annotations

import time
import urllib.parse
from pathlib import Path

from .. import net
from ..errors import AuthFailed, NotFound, ProtocolError, Unsupported
from ..model import Camera, Status
from .base import Adapter

STATE_MAP = {"IDLE": "idle", "RUNNING": "printing", "PAUSED": "paused", "STOPPED": "cancelled",
             "FINISHED": "complete", "COMPLETED": "complete"}
APPROVE = "Accept the connection request on the printer's touchscreen, then pair again"


class SnapmakerAdapter(Adapter):
    type_name = "snapmaker"
    label = "Snapmaker 2.0"
    default_port = 8080
    needs_secret = False  # the token comes from the printer during pair, after the person accepts it
    pairs_by_approval = True
    approval_seconds = 120.0
    approval_prompt = "Look at the Snapmaker's touchscreen now and accept the connection request from Gantrel."
    verifies_remote = False  # the HTTP API lists no files
    network_start = False  # Luban starts only a file it just sent with prepare_print; never by name

    @property
    def base(self) -> str:
        return f"http://{self.cfg.host}:{self.port}"

    def _form(self, path: str, fields: dict | None = None) -> bytes:
        body = urllib.parse.urlencode({"token": self.secret or "", **(fields or {})}).encode()
        _, _, data = net.request("POST", self.base + path, body=body, what=self.cfg.name,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
        return data

    def _status_raw(self, token: str) -> tuple[int, dict]:
        code, _, data = net.request("GET", f"{self.base}/api/v1/status?" + urllib.parse.urlencode({"token": token}),
                                    what=self.cfg.name)
        return code, (net.parse_json(data, self.cfg.name) if code == 200 and data.strip() else {})

    # ------------------------------------------------------------ contract

    @classmethod
    def probe(cls, host, port=None, timeout=2.0):
        """GET /api/v1/status with no token. Snapmaker answers 401; PrusaLink and OctoPrint also have /api/version."""
        port = port or cls.default_port
        base = f"http://{host}:{port}"
        try:
            net.request("GET", f"{base}/api/v1/status", timeout=timeout)
            return None
        except AuthFailed:
            pass
        except Exception:
            return None
        try:
            net.request("GET", f"{base}/api/version", timeout=timeout)
            return None
        except NotFound:
            return {"type": cls.type_name, "host": host, "port": port,
                    "detail": "Snapmaker HTTP API; pair, then accept the request on the touchscreen"}
        except Exception:
            return None

    def pair(self):
        """Ask for a token, wait for the person to accept it on the touchscreen, keep it as the secret."""
        _, _, data = net.request("POST", f"{self.base}/api/v1/connect", body=b"", what=self.cfg.name,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
        token = net.parse_json(data, self.cfg.name).get("token")
        if not token:
            raise ProtocolError(f"{self.cfg.name} sent no token")
        deadline = time.monotonic() + self.approval_seconds
        while True:
            code, _ = self._status_raw(token)
            if code == 200:
                break
            if time.monotonic() > deadline:
                raise AuthFailed(f"nobody accepted the connection on {self.cfg.name}'s touchscreen", APPROVE)
            time.sleep(1)
        self.secret = str(token)
        st = self.status()
        return {"state": st.state}

    def status(self) -> Status:
        code, s = self._status_raw(self.secret or "")
        if code == 204:
            raise AuthFailed(f"{self.cfg.name} is waiting for the person to accept Gantrel", APPROVE)
        state = STATE_MAP.get(str(s.get("status", "")).upper(), "busy")
        busy = state in ("printing", "paused")
        prog = s.get("progress")
        return Status(
            printer=self.cfg.name, type=self.type_name, state=state, file=str(s.get("fileName") or "") if busy else "",
            progress=float(prog) if busy and isinstance(prog, (int, float)) else None,
            nozzle_c=s.get("nozzleTemperature"), nozzle_target_c=s.get("nozzleTargetTemperature"),
            bed_c=s.get("heatedBedTemperature"), bed_target_c=s.get("heatedBedTargetTemperature"),
            elapsed_s=s.get("elapsedTime") if busy else None, remaining_s=s.get("remainingTime") if busy else None,
            message="filament ran out" if s.get("isFilamentOut") else "",
        )

    def upload(self, local_path: Path, remote_name: str) -> str:
        # /api/v1/upload stores the file in the touchscreen's file list. It does not load or start it
        # (that is /api/v1/prepare_print, which this adapter never calls).
        body, ctype = net.multipart({"token": self.secret or ""}, "file", remote_name, Path(local_path).read_bytes())
        net.request("POST", f"{self.base}/api/v1/upload", body=body, timeout=300,
                    headers={"Content-Type": ctype}, what=self.cfg.name)
        return remote_name

    def _start(self, remote_name: str) -> None:
        raise Unsupported(f"{self.cfg.name} can't start a stored file over the network",
                          self.start_by_hand(remote_name))

    def _resume(self) -> None:
        self._form("/api/v1/resume_print")

    def pause(self) -> None:
        self._form("/api/v1/pause_print")

    def cancel(self) -> None:
        self._form("/api/v1/stop_print")

    def emergency_stop(self) -> str:
        self._form("/api/v1/execute_code", {"code": "M112"})
        return ("Sent M112 emergency stop: the heaters and motors are off. Restart the printer from its "
                "touchscreen or power switch before using it again")

    def start_by_hand(self, remote_name: str = "") -> str:
        what = f"Start {remote_name}" if remote_name else "Start or resume it"
        return f"{what} on the Snapmaker's touchscreen (Files)."

    def camera(self) -> Camera:
        # The Snapmaker 2.0 HTTP API has no camera.
        return Camera(self.cfg.camera_url, self.cfg.stream_url)
