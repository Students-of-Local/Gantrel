"""Creality stock firmware (K1, K2, Ender-3 V3, Hi): local WebSocket on 9999 plus HTTP upload.

This is the protocol Creality Print uses on the LAN. Creality does not document it and it may change
with firmware. Field names and state codes follow crlancli (MIT, https://github.com/axellse/crlancli)
and community notes. Record the firmware version whenever this is checked on a real printer.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path

from .. import net
from ..errors import ProtocolError, Unreachable
from ..model import Camera, Status
from .base import Adapter

# Creality "state" codes. Unverified on hardware; see module docstring.
STATE_MAP = {0: "idle", 1: "printing", 2: "complete", 3: "error", 4: "cancelled", 5: "paused"}
GCODE_DIR = "/usr/data/printer_data/gcodes"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class CrealityAdapter(Adapter):
    type_name = "creality"
    label = "Creality stock firmware"
    default_port = 9999
    needs_secret = False
    verifies_remote = False  # no file info call known on the stock socket; names carry the content hash

    @property
    def ws_url(self) -> str:
        return f"ws://{self.cfg.host}:{self.port}"

    @property
    def http_base(self) -> str:
        return f"http://{self.cfg.host}:{self.cfg.extra.get('http_port', 80)}"

    def _session(self, send: list[dict], wait: float = 1.5, need: str | None = "state") -> dict:
        """Open the socket, send messages, merge the JSON pushes the printer sends back."""
        from websockets.exceptions import WebSocketException
        from websockets.sync.client import connect

        merged: dict = {}
        try:
            with connect(self.ws_url, open_timeout=5, close_timeout=2, max_size=4 * 1024 * 1024) as ws:
                for msg in send:
                    ws.send(json.dumps(msg))
                deadline = time.monotonic() + wait
                while time.monotonic() < deadline:
                    try:
                        raw = ws.recv(timeout=max(0.05, deadline - time.monotonic()))
                    except TimeoutError:
                        break
                    if isinstance(raw, bytes) or raw in ("ok", "heart_beat"):
                        continue
                    try:
                        data = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(data, dict):
                        merged.update(data)
                        if need and need in merged and not send[1:]:
                            break
        except (OSError, TimeoutError, WebSocketException) as err:
            raise Unreachable(f"{self.cfg.name} did not answer on {self.ws_url} ({err.__class__.__name__})",
                              "Check it is on and on the same network; stock firmware listens on port 9999") from None
        return merged

    def _set(self, params: dict) -> None:
        self._session([{"method": "set", "params": params}], wait=0.5, need=None)

    @classmethod
    def probe(cls, host, port=None, timeout=2.0):
        from websockets.sync.client import connect

        port = port or cls.default_port
        try:
            with connect(f"ws://{host}:{port}", open_timeout=timeout, close_timeout=1) as ws:
                ws.send(json.dumps({"method": "get", "params": {"ReqPrinterPara": 1}}))
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    raw = ws.recv(timeout=max(0.05, deadline - time.monotonic()))
                    if isinstance(raw, str) and raw.startswith("{"):
                        data = json.loads(raw)
                        if "state" in data or "model" in data or "nozzleTemp" in data:
                            return {"type": cls.type_name, "host": host, "port": port,
                                    "detail": f"Creality {data.get('model', '')} firmware {data.get('modelVersion', '')}".strip()}
        except Exception:
            return None
        return None

    def pair(self):
        data = self._session([{"method": "get", "params": {"ReqPrinterPara": 1}}])
        st = self._to_status(data)
        return {"state": st.state, "model": data.get("model", ""), "firmware": data.get("modelVersion", "")}

    def _to_status(self, d: dict) -> Status:
        if "state" not in d:
            raise ProtocolError(f"{self.cfg.name} did not report a state",
                                "The firmware may have changed; record the version and report it")
        try:
            state = STATE_MAP.get(int(d["state"]), "busy")
        except (TypeError, ValueError):
            state = "busy"
        if d.get("err") and isinstance(d["err"], dict) and d["err"].get("errcode"):
            state = "error"
        prog = _num(d.get("printProgress"))
        name = str(d.get("printFileName", "") or "")
        return Status(
            printer=self.cfg.name, type=self.type_name, state=state, file=name.rsplit("/", 1)[-1],
            progress=prog / 100 if prog is not None else None,
            nozzle_c=_num(d.get("nozzleTemp")), nozzle_target_c=_num(d.get("targetNozzleTemp")),
            bed_c=_num(d.get("bedTemp0")), bed_target_c=_num(d.get("targetBedTemp0")),
            chamber_c=_num(d.get("boxTemp")), elapsed_s=_num(d.get("printJobTime")),
            remaining_s=_num(d.get("printLeftTime")), layer=d.get("layer"), total_layers=d.get("TotalLayer"),
            extra={"firmware": d.get("modelVersion", ""), "model": d.get("model", "")},
        )

    def status(self) -> Status:
        return self._to_status(self._session([{"method": "get", "params": {"ReqPrinterPara": 1}}]))

    def upload(self, local_path: Path, remote_name: str) -> str:
        body, ctype = net.multipart({}, "file", remote_name, Path(local_path).read_bytes())
        net.request("POST", f"{self.http_base}/upload/{urllib.parse.quote(remote_name)}", body=body,
                    headers={"Content-Type": ctype}, timeout=300, what=self.cfg.name)
        return remote_name

    def _start(self, remote_name: str) -> None:
        self._set({"opGcodeFile": f"printprt:{GCODE_DIR}/{remote_name}"})

    def _resume(self) -> None:
        self._set({"pause": 0})

    def pause(self) -> None:
        self._set({"pause": 1})

    def cancel(self) -> None:
        self._set({"stop": 1})

    def emergency_stop(self) -> str:
        self._set({"gcodeCmd": "M112"})
        return ("Sent M112 emergency stop over the Creality socket. Klipper shuts down; restart it from the "
                "printer screen before printing again")

    def camera(self) -> Camera:
        if self.cfg.camera_url:
            return Camera(self.cfg.camera_url, self.cfg.stream_url)
        # K1 and Ender-3 V3 stock firmware serve MJPEG here. The K2 serves only WebRTC on port 8000;
        # relay it with go2rtc and pair with --camera-url.
        return Camera(f"http://{self.cfg.host}:8080/?action=snapshot",
                      self.cfg.stream_url or f"http://{self.cfg.host}:8080/?action=stream")
