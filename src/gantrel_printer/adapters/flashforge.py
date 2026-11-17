"""FlashForge Adventurer 5M, 5M Pro and AD5X: the local HTTP API on port 8898, with the legacy TCP
G-code API on port 8899 as a read-only fallback for status (and for older FlashForge printers).

Every HTTP call carries the printer's serial number and its check code (the LAN code the printer's
network settings show once LAN-only mode is on). The check code is kept in the keyring and sent only
to the printer. Uploads send printNow "false", so they never start a print.

Reference: ff-5mp-api-ts (MIT, https://github.com/GhostTypes/ff-5mp-api-ts). Written from its
endpoint and field notes, not copied.
"""

from __future__ import annotations

import re
import socket
import urllib.parse
from pathlib import Path

from .. import net
from ..errors import AuthFailed, ProtocolError, Refused, Unreachable, Unsupported
from ..model import Camera, Status
from .base import Adapter, write_image

STATE_MAP = {"ready": "idle", "printing": "printing", "pause": "paused", "paused": "paused",
             "completed": "complete", "cancel": "cancelled", "error": "error", "busy": "busy"}
LEGACY_STATE = {"READY": "idle", "BUILDING_FROM_SD": "printing", "BUILDING_COMPLETED": "complete",
                "PAUSED": "paused", "BUSY": "busy"}
LEGACY_PORT = 8899
NOT_ON_LEGACY = "Pair it again with --port 8898 (the HTTP API on the Adventurer 5M series)"


class FlashForgeAdapter(Adapter):
    type_name = "flashforge"
    label = "FlashForge (Adventurer 5M series)"
    default_port = 8898
    needs_secret = True
    secret_label = "check code (printer screen: network settings with LAN-only mode on)"
    verifies_remote = False  # /gcodeList gives names but no sizes on the 5M and 5M Pro

    @property
    def base(self) -> str:
        return f"http://{self.cfg.host}:{self.port}"

    @property
    def legacy(self) -> bool:
        """True when paired on the TCP G-code API (port 8899, or detected at pair time)."""
        return self.port == LEGACY_PORT or self.cfg.extra.get("api") == "legacy"

    @property
    def legacy_port(self) -> int:
        return int(self.cfg.extra.get("legacy_port", LEGACY_PORT))

    def _creds(self) -> dict:
        if not self.cfg.serial:
            raise ProtocolError(f"{self.cfg.name} has no serial number",
                                "Pair again with --serial (printer screen: Settings, About)")
        return {"serialNumber": self.cfg.serial, "checkCode": self.secret or ""}

    def _post(self, path: str, payload: dict | None = None) -> dict:
        r = net.send_json("POST", self.base + path, {**self._creds(), **(payload or {})}, what=self.cfg.name)
        if r.get("code") != 0:
            msg = str(r.get("message", ""))[:120]
            if path == "/detail" or "check" in msg.lower() or "serial" in msg.lower():
                raise AuthFailed(f"{self.cfg.name} refused the serial number or check code ({msg})",
                                 "Pair again with the serial number and the check code the printer shows")
            raise Refused(f"{self.cfg.name} refused it ({msg})")
        return r

    def _job(self, action: str) -> None:
        if self.legacy:
            self._legacy({"pause": "M25", "continue": "M24", "cancel": "M26"}[action])
            return
        self._post("/control", {"payload": {"cmd": "jobCtl_cmd", "args": {"jobID": "", "action": action}}})

    # ------------------------------------------------------------ legacy TCP (port 8899)

    @staticmethod
    def _tcp(host: str, port: int, commands: list[str], timeout: float = 5.0) -> list[str]:
        replies = []
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            f = s.makefile("rb")
            for cmd in commands:
                s.sendall(f"~{cmd}\r\n".encode())
                lines = []
                while True:
                    line = f.readline(4096)
                    if not line:
                        raise ConnectionError("closed")
                    text = line.decode("utf-8", "replace").strip()
                    if text == "ok":
                        break
                    lines.append(text)
                    if len(lines) > 200:
                        raise ConnectionError("no ok")
                replies.append("\n".join(lines))
        return replies

    def _legacy(self, *commands: str) -> list[str]:
        port = self.port if self.legacy else self.legacy_port
        try:
            return self._tcp(self.cfg.host, port, ["M601 S1", *commands, "M602"])[1:-1]
        except (OSError, ConnectionError) as err:
            raise Unreachable(f"{self.cfg.name} did not answer on port {port} ({err.__class__.__name__})",
                              "Check the printer is on and on the same network") from None

    def _legacy_status(self, note: str = "") -> Status:
        m119, m105, m27 = self._legacy("M119", "M105", "M27")
        ms = re.search(r"MachineStatus:\s*(\S+)", m119)
        state = LEGACY_STATE.get(ms.group(1) if ms else "", "busy")
        cur = re.search(r"CurrentFile:\s*(\S*)", m119)
        t = re.search(r"T0?:\s*([\d.]+)\s*/\s*([\d.]+)", m105)
        b = re.search(r"B:\s*([\d.]+)\s*/\s*([\d.]+)", m105)
        sd = re.search(r"SD printing byte\s+(\d+)\s*/\s*(\d+)", m27)
        layer = re.search(r"Layer:\s*(\d+)\s*/\s*(\d+)", m27)
        busy = state in ("printing", "paused")
        progress = None
        if busy and sd and int(sd.group(2)):
            progress = int(sd.group(1)) / int(sd.group(2))
        return Status(
            printer=self.cfg.name, type=self.type_name, state=state, file=cur.group(1) if cur and busy else "",
            progress=progress, nozzle_c=float(t.group(1)) if t else None, nozzle_target_c=float(t.group(2)) if t else None,
            bed_c=float(b.group(1)) if b else None, bed_target_c=float(b.group(2)) if b else None,
            layer=int(layer.group(1)) if layer and busy else None,
            total_layers=int(layer.group(2)) if layer and busy else None, message=note,
        )

    # ------------------------------------------------------------ contract

    @classmethod
    def _legacy_m115(cls, host: str, port: int, timeout: float) -> str | None:
        try:
            (m115,) = cls._tcp(host, port, ["M115"], timeout)
        except (OSError, ConnectionError):
            return None
        mt = re.search(r"Machine Type:\s*(.+)", m115)
        return mt.group(1).strip() if "M115" in m115 and mt else None

    @classmethod
    def probe(cls, host, port=None, timeout=2.0):
        port = port or cls.default_port
        if port != LEGACY_PORT:
            try:
                r = net.send_json("POST", f"http://{host}:{port}/detail", {"serialNumber": "", "checkCode": ""},
                                  timeout=timeout)
            except Exception:
                r = None
            if r is not None:
                if "code" in r and "message" in r and set(r) <= {"code", "message", "detail"}:
                    return {"type": cls.type_name, "host": host, "port": port,
                            "detail": "FlashForge HTTP API; pair needs --serial and the check code"}
                return None
        machine = cls._legacy_m115(host, port, timeout)
        if machine:
            return {"type": cls.type_name, "host": host, "port": port,
                    "detail": f"{machine} (legacy G-code port; pair with --port 8898 for uploads)"}
        return None

    def pair(self):
        if not self.legacy:
            try:
                self._post("/detail")
            except (Unreachable, ProtocolError):
                if not self._legacy_m115(self.cfg.host, self.port, 5.0):
                    raise
                self.cfg.extra["api"] = "legacy"
        if self.legacy:
            st = self._legacy_status()
            return {"state": st.state, "api": "legacy TCP 8899 (status, pause and cancel only)"}
        d = self._post("/detail").get("detail") or {}
        st = self.status()
        return {"state": st.state, "name": d.get("name", ""), "firmware": d.get("firmwareVersion", "")}

    def status(self) -> Status:
        if self.legacy:
            return self._legacy_status()
        try:
            d = self._post("/detail").get("detail") or {}
        except Unreachable:
            return self._legacy_status(f"read over legacy port {self.legacy_port}; the HTTP API did not answer")
        state = STATE_MAP.get(str(d.get("status", "")).lower(), "busy")
        busy = state in ("printing", "paused")
        prog = d.get("printProgress")
        return Status(
            printer=self.cfg.name, type=self.type_name, state=state,
            file=str(d.get("printFileName") or "") if busy else "",
            progress=float(prog) if busy and isinstance(prog, (int, float)) else None,
            nozzle_c=d.get("rightTemp"), nozzle_target_c=d.get("rightTargetTemp"),
            bed_c=d.get("platTemp"), bed_target_c=d.get("platTargetTemp"), chamber_c=d.get("chamberTemp"),
            elapsed_s=d.get("printDuration") if busy else None, remaining_s=d.get("estimatedTime") if busy else None,
            layer=d.get("printLayer") if busy else None, total_layers=d.get("targetPrintLayer") if busy else None,
            message=f"error code {d['errorCode']}" if d.get("errorCode") not in (None, "", "0") else "",
        )

    def upload(self, local_path: Path, remote_name: str) -> str:
        if self.legacy:
            raise Unsupported(f"{self.cfg.name} is paired on the legacy port, which has no upload", NOT_ON_LEGACY)
        data = Path(local_path).read_bytes()
        creds = self._creds()
        body, ctype = net.multipart({}, "gcodeFile", remote_name, data)
        # printNow false: the printer stores the file and does not start it.
        headers = {"Content-Type": ctype, "serialNumber": creds["serialNumber"], "checkCode": creds["checkCode"],
                   "fileSize": str(len(data)), "printNow": "false", "levelingBeforePrint": "false",
                   "flowCalibration": "false", "useMatlStation": "false", "gcodeToolCnt": "0",
                   "materialMappings": "W10="}
        _, _, reply = net.request("POST", f"{self.base}/uploadGcode", body=body, headers=headers, timeout=300,
                                  what=self.cfg.name)
        r = net.parse_json(reply, self.cfg.name)
        if r.get("code") != 0:
            raise Refused(f"{self.cfg.name} refused the upload ({str(r.get('message', ''))[:120]})")
        return remote_name

    def _start(self, remote_name: str) -> None:
        if self.legacy:
            raise Unsupported(f"{self.cfg.name} is paired on the legacy port", NOT_ON_LEGACY)
        self._post("/printGcode", {"fileName": remote_name, "levelingBeforePrint": False})

    def _resume(self) -> None:
        self._job("continue")

    def pause(self) -> None:
        self._job("pause")

    def cancel(self) -> None:
        self._job("cancel")

    def emergency_stop(self) -> str:
        self._job("cancel")
        return ("FlashForge printers have no network emergency stop. Sent cancel, which stops the print and "
                "turns the heaters off. For a true emergency stop switch the printer off at its power switch")

    def start_by_hand(self, remote_name: str = "") -> str:
        what = f"Start {remote_name}" if remote_name else "Start or resume it"
        return f"{what} on the printer's touchscreen (Files, then the local files list)."

    def camera(self) -> Camera:
        if self.cfg.camera_url:
            return Camera(self.cfg.camera_url, self.cfg.stream_url)
        url = ""
        if not self.legacy:
            try:
                url = str((self._post("/detail").get("detail") or {}).get("cameraStreamUrl") or "")
            except Exception:
                url = ""
        if urllib.parse.urlsplit(url).hostname != self.cfg.host:
            url = ""  # only ever fetch the camera from the printer itself
        url = url or f"http://{self.cfg.host}:8080/?action=stream"
        return Camera(url, self.cfg.stream_url or url)

    def snapshot(self, out: Path) -> Path:
        if self.cfg.camera_url:
            return super().snapshot(out)
        return write_image(net.mjpeg_frame(self.camera().snapshot_url, what=f"{self.cfg.name}'s camera"), out)
