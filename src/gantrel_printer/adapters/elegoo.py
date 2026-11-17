"""Elegoo printers that speak SDCP v3: Centauri Carbon (FDM) and the Saturn and Mars resin printers.

SDCP is JSON over a WebSocket on port 3030 (ws://<ip>:3030/websocket). UDP 3000 answers the
discovery message "M99999" with the printer's MainboardID, which every request is addressed to.
Files go up over HTTP on port 80 (POST /uploadFile/upload, 1 MiB chunks, whole-file MD5 on each).
The camera is MJPEG at http://<ip>:3031/video.

References: Elegoo's SDCP spec (https://github.com/cbd-tech/SDCP), elegoo-homeassistant (MIT,
https://github.com/danielcherubini/elegoo-homeassistant) and the Centauri Carbon protocol notes in
pycentauri (https://github.com/bjan/pycentauri/blob/main/docs/PROTOCOL.md). Written from those
notes, not copied.

Those notes warn that the Centauri Carbon firmware can crash, killing a running print, when it gets
a Cmd it doesn't know or a payload shaped differently from Elegoo's own client. This adapter sends
only the Cmds those notes list as confirmed (0, 128, 129, 130, 131, 258) with the same payloads.
"""

from __future__ import annotations

import hashlib
import json
import socket
import time
import uuid
from pathlib import Path

from .. import net, slicing
from ..errors import ProtocolError, Refused, Unreachable
from ..model import Camera, Status
from .base import Adapter, write_image

# PrintInfo.Status codes (pycentauri notes, from Elegoo's elegoo-link SDK). 2 to 4 and 23 to 26 are resin.
PRINTING = {1, 2, 3, 4, 10, 11, 12, 13, 15, 16, 17, 18, 23, 24}
STATE_MAP = {0: "idle", 5: "busy", 6: "paused", 7: "busy", 8: "cancelled", 9: "complete", 14: "error",
             25: "error", 26: "paused"}
# 19 to 22 are "... completed" steps (leveling, preheat, homing, resonance) that also show between prints.
SETTLED = {19, 20, 21, 22}
CMD_STATUS, CMD_START, CMD_PAUSE, CMD_STOP, CMD_RESUME, CMD_FILES = 0, 128, 129, 130, 131, 258
CHUNK = 1024 * 1024
RESIN_EXTS = (".ctb", ".goo")


def _temp(v):
    """Temps come as a number, or on older firmware as [target, actual]."""
    if isinstance(v, (list, tuple)) and len(v) == 2:
        return float(v[1]), float(v[0])
    if isinstance(v, (int, float)):
        return float(v), None
    return None, None


class ElegooAdapter(Adapter):
    type_name = "elegoo"
    label = "Elegoo (SDCP)"
    default_port = 3030
    needs_secret = False  # SDCP v3 on the Centauri Carbon and Saturn/Mars has no LAN password
    udp_port = 3000
    accepts_resin = True

    @property
    def ws_url(self) -> str:
        return f"ws://{self.cfg.host}:{self.port}/websocket"

    @property
    def http_base(self) -> str:
        return f"http://{self.cfg.host}:{self.cfg.extra.get('http_port', 80)}"

    @property
    def resin(self) -> bool:
        p = slicing.PRINTERS.get(self.cfg.model)
        return bool(p and p.resin)

    # ------------------------------------------------------------ UDP discovery

    @classmethod
    def udp_hello(cls, host: str, timeout: float = 2.0, port: int | None = None) -> dict | None:
        """Send M99999 to one address and return the Data of its reply, or None."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(timeout)
                s.sendto(b"M99999", (host, port or cls.udp_port))
                data, _ = s.recvfrom(8192)
        except OSError:
            return None
        try:
            reply = json.loads(data)
        except ValueError:
            return None
        d = reply.get("Data") if isinstance(reply, dict) else None
        return d if isinstance(d, dict) and d.get("MainboardID") else None

    def mainboard_id(self) -> str:
        if self.cfg.serial:
            return self.cfg.serial
        hello = self.udp_hello(self.cfg.host, 3.0, self.cfg.extra.get("udp_port"))
        if not hello:
            raise Unreachable(f"{self.cfg.name} did not answer Elegoo discovery on UDP {self.udp_port}",
                              "Check the printer is on and on the same network")
        self.cfg.serial = str(hello["MainboardID"])
        return self.cfg.serial

    # ------------------------------------------------------------ SDCP over WebSocket

    def _sdcp(self, cmd: int, data: dict | None = None, *, want_status: bool = False,
              ws_url: str | None = None, timeout: float = 6.0) -> dict:
        """Send one Cmd. Return the response's Data.Data, or the status push when want_status."""
        from websockets.exceptions import WebSocketException
        from websockets.sync.client import connect

        mb = self.mainboard_id()
        rid = uuid.uuid4().hex[:16]
        msg = {"Id": mb, "Topic": f"sdcp/request/{mb}",
               "Data": {"Cmd": cmd, "Data": data or {}, "RequestID": rid, "MainboardID": mb,
                        "TimeStamp": int(time.time() * 1000), "From": 1}}
        ack = None
        try:
            with connect(ws_url or self.ws_url, open_timeout=5, close_timeout=2, max_size=4 * 1024 * 1024) as ws:
                ws.send(json.dumps(msg))
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        raw = ws.recv(timeout=max(0.05, deadline - time.monotonic()))
                    except TimeoutError:
                        break
                    try:
                        reply = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(reply, dict):
                        continue
                    topic = str(reply.get("Topic", ""))
                    if want_status and topic.startswith("sdcp/status/") and isinstance(reply.get("Status"), dict):
                        return reply["Status"]
                    body = reply.get("Data")
                    if topic.startswith("sdcp/response/") and isinstance(body, dict) and body.get("RequestID") == rid:
                        ack = body.get("Data") if isinstance(body.get("Data"), dict) else {}
                        if not want_status:
                            break
        except (OSError, TimeoutError, WebSocketException) as err:
            raise Unreachable(f"{self.cfg.name} did not answer on SDCP port {self.port} ({err.__class__.__name__})",
                              "Check the printer is on and on the same network") from None
        if want_status:
            raise ProtocolError(f"{self.cfg.name} sent no SDCP status")
        if ack is None:
            raise ProtocolError(f"{self.cfg.name} did not answer SDCP command {cmd}")
        if ack.get("Ack", 0) != 0:
            raise Refused(f"{self.cfg.name} refused SDCP command {cmd} (Ack {ack.get('Ack')})")
        return ack

    # ------------------------------------------------------------ contract

    @classmethod
    def probe(cls, host, port=None, timeout=2.0):
        """UDP M99999 must answer with a MainboardID, and that board must answer SDCP status on port."""
        port = port or cls.default_port
        hello = cls.udp_hello(host, timeout)
        if not hello:
            return None
        from ..config import PrinterConfig

        a = cls(PrinterConfig(name="probe", type=cls.type_name, host=host, port=port,
                              serial=str(hello["MainboardID"])))
        try:
            a._sdcp(CMD_STATUS, want_status=True, timeout=timeout)
        except Exception:
            return None
        name = hello.get("MachineName") or hello.get("Name") or "printer"
        return {"type": cls.type_name, "host": host, "port": port, "serial": str(hello["MainboardID"]),
                "detail": f"Elegoo {name} (SDCP), firmware {hello.get('FirmwareVersion', '?')}"}

    def pair(self):
        mb = self.mainboard_id()
        st = self.status()
        return {"state": st.state, "mainboard_id": mb}

    def status(self) -> Status:
        s = self._sdcp(CMD_STATUS, want_status=True)
        pi = s.get("PrintInfo") or {}
        code = pi.get("Status", 0)
        current = s.get("CurrentStatus") or s.get("CurrenStatus") or []
        if code in PRINTING:
            state = "printing"
        elif code in SETTLED:
            state = "idle" if 0 in current else "busy"
        else:
            state = STATE_MAP.get(code, "busy")
        if state == "idle" and any(c in (2, 3, 4) for c in current):
            state = "busy"  # file transfer, exposure test or self-check
        nozzle, nozzle_t = _temp(s.get("TempOfNozzle"))
        bed, bed_t = _temp(s.get("TempOfHotbed"))
        box, _ = _temp(s.get("TempOfBox"))
        if s.get("TempTargetNozzle") is not None:
            nozzle_t = s.get("TempTargetNozzle")
        if s.get("TempTargetHotbed") is not None:
            bed_t = s.get("TempTargetHotbed")
        layer, total = pi.get("CurrentLayer"), pi.get("TotalLayer")
        progress = None
        if isinstance(pi.get("Progress"), (int, float)):
            progress = pi["Progress"] / 100
        elif isinstance(layer, int) and isinstance(total, int) and total > 0:
            progress = layer / total
        elapsed = remaining = None
        if not self.resin and isinstance(pi.get("CurrentTicks"), (int, float)):
            elapsed = float(pi["CurrentTicks"])
            if isinstance(pi.get("TotalTicks"), (int, float)):
                remaining = max(0.0, float(pi["TotalTicks"]) - elapsed)
        return Status(
            printer=self.cfg.name, type=self.type_name, state=state,
            file=str(pi.get("Filename") or "").rsplit("/", 1)[-1], progress=progress if state != "idle" else None,
            nozzle_c=nozzle, nozzle_target_c=nozzle_t, bed_c=bed, bed_target_c=bed_t, chamber_c=box,
            elapsed_s=elapsed if state in ("printing", "paused") else None,
            remaining_s=remaining if state in ("printing", "paused") else None,
            layer=layer if state in ("printing", "paused") else None,
            total_layers=total if state in ("printing", "paused") else None,
            message=f"status code {code}" if state in ("error", "busy") else "",
        )

    def _check_kind(self, remote_name: str) -> None:
        if not self.cfg.model:
            return
        is_resin_file = remote_name.lower().endswith(RESIN_EXTS)
        if self.resin and not is_resin_file:
            raise Refused(f"{self.cfg.name} is a resin printer and takes only .ctb or .goo files",
                          "Slice for it in CHITUBOX or VoxelDance Tango, then upload that file")
        if not self.resin and is_resin_file:
            raise Refused(f"{self.cfg.name} is an FDM printer; .ctb and .goo files are for resin printers")

    def upload(self, local_path: Path, remote_name: str) -> str:
        self._check_kind(remote_name)
        data = Path(local_path).read_bytes()
        md5 = hashlib.md5(data).hexdigest()  # the printer's integrity check, not a security measure
        uid = uuid.uuid4().hex
        offset = 0
        while True:
            chunk = data[offset:offset + CHUNK]
            body, ctype = net.multipart({"Check": "1", "S-File-MD5": md5, "Offset": str(offset), "Uuid": uid,
                                             "TotalSize": str(len(data))}, "File", remote_name, chunk)
            _, _, reply = net.request("POST", f"{self.http_base}/uploadFile/upload", body=body, timeout=120,
                                      headers={"Content-Type": ctype}, what=self.cfg.name)
            r = net.parse_json(reply, self.cfg.name)
            if str(r.get("code")) != "000000":
                raise Refused(f"{self.cfg.name} refused the upload (code {r.get('code')})",
                              "Check there is free space on the printer, then upload again")
            offset += len(chunk)
            if offset >= len(data):
                break
        return remote_name

    def remote_info(self, remote_name: str) -> dict | None:
        files = self._sdcp(CMD_FILES, {"Url": "/local"}).get("FileList") or []
        for f in files:
            if str(f.get("name", "")).rsplit("/", 1)[-1] == remote_name:
                return {"size": f.get("FileSize"), "mtime": f.get("CreateTime")}
        return None

    def _start(self, remote_name: str) -> None:
        if self.resin:
            self._sdcp(CMD_START, {"Filename": f"/local/{remote_name}", "StartLayer": 0})
        else:
            self._sdcp(CMD_START, {"Filename": f"/local/{remote_name}", "StartLayer": 0, "Calibration_switch": 0,
                                   "PrintPlatformType": 0, "Tlp_Switch": 0})

    def _resume(self) -> None:
        self._sdcp(CMD_RESUME)

    def pause(self) -> None:
        self._sdcp(CMD_PAUSE)

    def cancel(self) -> None:
        self._sdcp(CMD_STOP)

    def emergency_stop(self) -> str:
        self._sdcp(CMD_STOP)
        return ("Elegoo printers have no network emergency stop. Sent stop, which ends the print. "
                "For a true emergency stop switch the printer off at its power switch")

    def web_ui(self) -> str:
        return f"http://{self.cfg.host}/ (the printer's web page)"

    def camera(self) -> Camera:
        if self.cfg.camera_url:
            return Camera(self.cfg.camera_url, self.cfg.stream_url)
        url = f"http://{self.cfg.host}:{self.cfg.extra.get('camera_port', 3031)}/video"
        return Camera(url, self.cfg.stream_url or url)

    def snapshot(self, out: Path) -> Path:
        if self.cfg.camera_url:
            return super().snapshot(out)
        if self.resin:
            raise ProtocolError(f"{self.cfg.name} is a resin printer with no camera",
                                "Pair again with --camera-url if you added one")
        return write_image(net.mjpeg_frame(self.camera().snapshot_url, what=f"{self.cfg.name}'s camera"), out)

