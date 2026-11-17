"""UltiMaker UM3, S3, S5 and S7 through the printer's local /api/v1.

Pairing asks the printer for credentials (POST /api/v1/auth/request). The printer's screen asks the
person to allow Gantrel; /api/v1/auth/check/<id> says "authorized" once they do. Write calls use HTTP
digest auth with the id as the user and the key as the password. The key is kept in the keyring and
digest answers go only to the printer's own address.

The printer's API has no upload that only stores a file: POST /api/v1/print_job starts printing as
soon as the file arrives. Gantrel never starts a print, so this adapter refuses to upload. Copy the
file to a USB stick, or send it from UltiMaker Cura, and start it on the printer.

References: the unofficial API notes at https://gist.github.com/SimonIT/ea672554e9d642b517202125b10d3b37
and Cura's UM3NetworkPrinting plugin (LGPL-3.0, https://github.com/Ultimaker/Cura), read for the API only.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path

from .. import net
from ..errors import AuthFailed, NotFound, ProtocolError, Unsupported
from ..model import Camera, Status
from .base import Adapter

JOB_STATE = {"printing": "printing", "pre_print": "printing", "resuming": "printing", "pausing": "busy",
             "paused": "paused", "post_print": "busy", "wait_user_action": "paused"}
PRINTER_STATE = {"idle": "idle", "printing": "printing", "error": "error", "maintenance": "busy",
                 "booting": "busy"}
APPROVE = "Allow Gantrel on the printer's screen when it asks, then pair again"
NO_UPLOAD = ("UltiMaker's network API starts printing as soon as it receives a file, and Gantrel never starts "
             "a print, so it does not upload to UltiMaker printers")
NO_UPLOAD_HINT = "Copy the file to a USB stick, or send it from UltiMaker Cura, and start it on the printer"


class UltimakerAdapter(Adapter):
    type_name = "ultimaker"
    label = "UltiMaker (UM3 and S series)"
    default_port = 80
    needs_secret = False  # credentials come from the printer during pair, after the person allows them
    pairs_by_approval = True
    approval_seconds = 120.0
    approval_prompt = "Look at the UltiMaker's screen now and allow the request from Gantrel."
    verifies_remote = False
    uploads = False
    network_start = False

    @property
    def base(self) -> str:
        return f"http://{self.cfg.host}:{self.port}"

    def _digest(self) -> tuple[str, str]:
        return str(self.cfg.extra.get("auth_id", "")), self.secret or ""

    def _get(self, path: str, digest: bool = False):
        _, _, data = net.request("GET", self.base + path, what=self.cfg.name,
                                 digest=self._digest() if digest else None)
        try:
            return json.loads(data)
        except ValueError:
            raise ProtocolError(f"{self.cfg.name} sent a reply that is not JSON") from None

    def _put_state(self, target: str) -> None:
        net.request("PUT", f"{self.base}/api/v1/print_job/state", body=json.dumps({"target": target}).encode(),
                    headers={"Content-Type": "application/json"}, what=self.cfg.name, digest=self._digest())

    def _verify(self) -> None:
        if not self.cfg.extra.get("auth_id") or not self.secret:
            raise AuthFailed(f"{self.cfg.name} has no saved UltiMaker credentials", APPROVE)
        self._get("/api/v1/auth/verify", digest=True)

    # ------------------------------------------------------------ contract

    @classmethod
    def probe(cls, host, port=None, timeout=2.0):
        port = port or cls.default_port
        try:
            s = net.get_json(f"http://{host}:{port}/api/v1/system", timeout=timeout)
        except Exception:
            return None
        if "guid" in s and ("variant" in s or "firmware" in s):
            return {"type": cls.type_name, "host": host, "port": port,
                    "detail": f"{s.get('variant', 'UltiMaker')} firmware {s.get('firmware', '?')}; pair, then allow it on the screen"}
        return None

    def pair(self):
        body = urllib.parse.urlencode({"application": "Gantrel", "user": "gantrel"}).encode()
        _, _, data = net.request("POST", f"{self.base}/api/v1/auth/request", body=body, what=self.cfg.name,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
        r = net.parse_json(data, self.cfg.name)
        if not r.get("id") or not r.get("key"):
            raise ProtocolError(f"{self.cfg.name} sent no credentials")
        deadline = time.monotonic() + self.approval_seconds
        while True:
            answer = net.get_json(f"{self.base}/api/v1/auth/check/{urllib.parse.quote(str(r['id']))}",
                                  what=self.cfg.name).get("message")
            if answer == "authorized":
                break
            if answer == "unauthorized" or time.monotonic() > deadline:
                raise AuthFailed(f"the request was not allowed on {self.cfg.name}'s screen", APPROVE)
            time.sleep(1)
        self.cfg.extra["auth_id"] = str(r["id"])
        self.secret = str(r["key"])
        st = self.status()
        return {"state": st.state}

    def status(self) -> Status:
        p = self._get("/api/v1/printer")
        self._verify()  # read calls need no auth on UltiMaker; this catches a broken pairing early
        try:
            job = self._get("/api/v1/print_job")
        except NotFound:
            job = {}
        state = PRINTER_STATE.get(str(p.get("status", "")), "busy")
        js = str(job.get("state", ""))
        if js == "wait_cleanup":
            state = "cancelled" if str(job.get("result", "")).lower() == "aborted" else "complete"
        elif js in JOB_STATE:
            state = JOB_STATE[js]
        heads = p.get("heads") or [{}]
        ext = ((heads[0] or {}).get("extruders") or [{}])[0] or {}
        hot = (ext.get("hotend") or {}).get("temperature") or {}
        bed = (p.get("bed") or {}).get("temperature") or {}
        busy = state in ("printing", "paused")
        total, done = job.get("time_total"), job.get("time_elapsed")
        return Status(
            printer=self.cfg.name, type=self.type_name, state=state, file=str(job.get("name") or "") if busy else "",
            progress=job.get("progress") if busy else None, nozzle_c=hot.get("current"),
            nozzle_target_c=hot.get("target"), bed_c=bed.get("current"), bed_target_c=bed.get("target"),
            elapsed_s=done if busy else None,
            remaining_s=max(0, total - done) if busy and isinstance(total, (int, float)) and isinstance(done, (int, float)) else None,
            message="waiting for someone at the printer" if js == "wait_user_action" else "",
        )

    def upload(self, local_path: Path, remote_name: str) -> str:
        raise Unsupported(NO_UPLOAD, NO_UPLOAD_HINT)

    def _start(self, remote_name: str) -> None:
        raise Unsupported(NO_UPLOAD, NO_UPLOAD_HINT)

    def _resume(self) -> None:
        self._put_state("print")

    def pause(self) -> None:
        self._put_state("pause")

    def cancel(self) -> None:
        self._put_state("abort")

    def emergency_stop(self) -> str:
        self._put_state("abort")
        return ("UltiMaker printers have no network emergency stop. Sent abort, which stops the print and cools "
                "down. For a true emergency stop switch the printer off at its power switch")

    def start_by_hand(self, remote_name: str = "") -> str:
        what = f"Start {remote_name}" if remote_name else "Start or resume it"
        return f"{what} on the printer's touchscreen, from a USB stick or from UltiMaker Cura."

    def camera(self) -> Camera:
        if self.cfg.camera_url:
            return Camera(self.cfg.camera_url, self.cfg.stream_url)
        cam = f"http://{self.cfg.host}:{self.cfg.extra.get('camera_port', 8080)}"
        return Camera(f"{cam}/?action=snapshot", self.cfg.stream_url or f"{cam}/?action=stream")
