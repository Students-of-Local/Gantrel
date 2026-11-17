"""Duet boards running RepRapFirmware: the standalone rr_ HTTP API, and DSF's /machine API on SBC setups.

References: RepRapFirmware HTTP requests (https://github.com/Duet3D/RepRapFirmware/wiki/HTTP-requests)
and the DSF REST API (https://github.com/Duet3D/DuetSoftwareFramework, DuetWebServer). RRF and DSF are
GPL-3.0; this only calls their network APIs.

The board password is optional (RRF's default "reprap" means none). It is sent only to the board,
in rr_connect or /machine/connect, never logged. Status comes from the object model. Pause is M25;
cancel pauses first and then sends M0, which RRF treats as "cancel the paused print"; emergency stop
is M112.
"""

from __future__ import annotations

import datetime
import time
import urllib.parse
from pathlib import Path

from .. import net
from ..errors import AuthFailed, NotFound, ProtocolError, Refused
from ..model import Camera, Status
from .base import Adapter

STATE_MAP = {"idle": "idle", "processing": "printing", "resuming": "printing", "paused": "paused",
             "pausing": "busy", "cancelling": "busy", "simulating": "busy", "busy": "busy", "changingTool": "busy",
             "starting": "busy", "updating": "busy", "halted": "error", "off": "offline", "disconnected": "offline"}


class DuetAdapter(Adapter):
    type_name = "duet"
    label = "Duet (RepRapFirmware)"
    default_port = 80
    needs_secret = False  # optional board password
    asks_secret = True
    secret_label = "Duet board password (leave empty if it has none)"

    @property
    def base(self) -> str:
        return f"http://{self.cfg.host}:{self.port}"

    @property
    def api(self) -> str:
        """'rr' (standalone board) or 'dsf' (Duet with a single-board computer). Detected once."""
        if self.cfg.extra.get("api") not in ("rr", "dsf"):
            self.cfg.extra["api"] = self._detect()
        return self.cfg.extra["api"]

    def _detect(self) -> str:
        try:
            _, _, data = net.request("GET", f"{self.base}/machine/connect?"
                                     + urllib.parse.urlencode({"password": self.secret or ""}), what=self.cfg.name)
        except AuthFailed:
            return "dsf"  # DSF answered and refused the password; the call that follows reports it
        except (NotFound, Refused, ProtocolError):
            return "rr"
        # A standalone board's web server answers unknown paths with 200 and its web page, so only a
        # JSON reply that holds a sessionKey proves DSF is there.
        try:
            reply = net.parse_json(data, self.cfg.name) if data.strip() else None
        except Exception:
            reply = None
        key = reply.get("sessionKey") if isinstance(reply, dict) else None
        if key is None:
            return "rr"
        try:
            net.request("GET", f"{self.base}/machine/disconnect", what=self.cfg.name,
                        headers={"X-Session-Key": str(key)})
        except Exception:
            pass
        return "dsf"

    # ------------------------------------------------------------ sessions

    def _session(self, work):
        """Log in, run work(headers), log out. Every call is one short session."""
        if self.api == "dsf":
            headers = {}
            try:
                _, _, data = net.request("GET", f"{self.base}/machine/connect?"
                                         + urllib.parse.urlencode({"password": self.secret or ""}), what=self.cfg.name)
            except AuthFailed:
                raise AuthFailed(f"{self.cfg.name} refused the board password",
                                 "Pair again with the password set in config.g (M551)") from None
            key = net.parse_json(data, self.cfg.name).get("sessionKey") if data.strip() else None
            if key:
                headers["X-Session-Key"] = str(key)
            try:
                return work(headers)
            finally:
                try:
                    net.request("GET", f"{self.base}/machine/disconnect", headers=headers, what=self.cfg.name)
                except Exception:
                    pass
        stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        r = net.get_json(f"{self.base}/rr_connect?" + urllib.parse.urlencode({"password": self.secret or "", "time": stamp}),
                         what=self.cfg.name)
        err = r.get("err")
        if err == 1:
            raise AuthFailed(f"{self.cfg.name} refused the board password",
                             "Pair again with the password set in config.g (M551)")
        if err == 2:
            raise Refused(f"{self.cfg.name} has no free web sessions", "Close a Duet Web Control tab and try again")
        if err not in (0, None):
            raise ProtocolError(f"{self.cfg.name} sent rr_connect error {err}")
        headers = {"X-Session-Key": str(r["sessionKey"])} if r.get("sessionKey") is not None else {}
        try:
            return work(headers)
        finally:
            try:
                net.request("GET", f"{self.base}/rr_disconnect", headers=headers, what=self.cfg.name)
            except Exception:
                pass

    def _model(self, h: dict) -> dict:
        if self.api == "dsf":
            return net.get_json(f"{self.base}/machine/status", headers=h, what=self.cfg.name)
        out = {}
        for key in ("state", "job", "heat", "tools"):
            r = net.get_json(f"{self.base}/rr_model?" + urllib.parse.urlencode({"key": key, "flags": "d99vn"}),
                             headers=h, what=self.cfg.name)
            out[key] = r.get("result")
        return out

    def _gcode(self, code: str) -> None:
        def work(h):
            if self.api == "dsf":
                net.request("POST", f"{self.base}/machine/code", body=code.encode(), what=self.cfg.name,
                            headers={"Content-Type": "text/plain", **h})
            else:
                net.get_json(f"{self.base}/rr_gcode?" + urllib.parse.urlencode({"gcode": code}), headers=h,
                             what=self.cfg.name)
        self._session(work)

    # ------------------------------------------------------------ contract

    @classmethod
    def probe(cls, host, port=None, timeout=2.0):
        port = port or cls.default_port
        base = f"http://{host}:{port}"
        try:
            st = net.get_json(f"{base}/machine/status", timeout=timeout)
            if isinstance(st.get("boards"), list) and isinstance(st.get("state"), dict):
                return {"type": cls.type_name, "host": host, "port": port, "detail": "Duet with DSF (SBC)"}
        except AuthFailed:
            pass
        except Exception:
            pass
        try:
            stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            r = net.get_json(f"{base}/rr_connect?" + urllib.parse.urlencode({"password": "", "time": stamp}),
                             timeout=timeout)
        except Exception:
            return None
        if r.get("err") in (0, 1, 2) and ("boardType" in r or "sessionTimeout" in r or r.get("err") == 1):
            if r.get("err") == 0:
                try:
                    net.request("GET", f"{base}/rr_disconnect", timeout=timeout,
                                headers={"X-Session-Key": str(r["sessionKey"])} if r.get("sessionKey") else {})
                except Exception:
                    pass
            needs = " (needs the board password to pair)" if r.get("err") == 1 else ""
            return {"type": cls.type_name, "host": host, "port": port,
                    "detail": f"Duet {r.get('boardType', '')}".strip() + needs}
        return None

    def pair(self):
        st = self.status()
        return {"state": st.state, "api": self.api}

    def status(self) -> Status:
        m = self._session(self._model)
        state_obj = m.get("state") or {}
        job = m.get("job") or {}
        heat = m.get("heat") or {}
        heaters = heat.get("heaters") or []
        state = STATE_MAP.get(str(state_obj.get("status", "")), "busy")

        def heater(i):
            if isinstance(i, int) and 0 <= i < len(heaters) and isinstance(heaters[i], dict):
                return heaters[i].get("current"), heaters[i].get("active")
            return None, None

        beds = heat.get("bedHeaters") or []
        bed, bed_t = heater(beds[0] if beds else None)
        tools = m.get("tools") or []
        tool_heaters = (tools[0] or {}).get("heaters") or [] if tools else []
        nozzle, nozzle_t = heater(tool_heaters[0] if tool_heaters else None)
        chambers = heat.get("chamberHeaters") or []
        chamber, _ = heater(chambers[0] if chambers else None)
        f = job.get("file") or {}
        size, pos = f.get("size"), job.get("filePosition")
        busy = state in ("printing", "paused")
        progress = pos / size if busy and isinstance(pos, (int, float)) and size else None
        left = (job.get("timesLeft") or {}).get("slicer") or (job.get("timesLeft") or {}).get("file")
        return Status(
            printer=self.cfg.name, type=self.type_name, state=state,
            file=str(f.get("fileName") or "").rsplit("/", 1)[-1] if busy else "", progress=progress,
            nozzle_c=nozzle, nozzle_target_c=nozzle_t, bed_c=bed, bed_target_c=bed_t, chamber_c=chamber,
            elapsed_s=job.get("duration") if busy else None, remaining_s=left if busy else None,
            layer=job.get("layer") if busy else None, total_layers=f.get("numLayers") if busy else None,
            message=str(state_obj.get("displayMessage") or ""),
        )

    def upload(self, local_path: Path, remote_name: str) -> str:
        data = Path(local_path).read_bytes()
        path = f"0:/gcodes/{remote_name}"

        def work(h):
            if self.api == "dsf":
                net.request("PUT", f"{self.base}/machine/file/{urllib.parse.quote(path, safe='')}", body=data,
                            timeout=300, headers={"Content-Type": "application/octet-stream", **h}, what=self.cfg.name)
                return
            stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            _, _, reply = net.request("POST", f"{self.base}/rr_upload?" + urllib.parse.urlencode({"name": path, "time": stamp}),
                                      body=data, timeout=300,
                                      headers={"Content-Type": "application/octet-stream", **h}, what=self.cfg.name)
            if net.parse_json(reply, self.cfg.name).get("err") != 0:
                raise Refused(f"{self.cfg.name} refused the upload", "Check the SD card has free space")
        self._session(work)
        return remote_name

    def remote_info(self, remote_name: str) -> dict | None:
        path = f"0:/gcodes/{remote_name}"

        def work(h):
            if self.api == "dsf":
                try:
                    info = net.get_json(f"{self.base}/machine/fileinfo/{urllib.parse.quote(path, safe='')}",
                                        headers=h, what=self.cfg.name)
                except NotFound:
                    return None
            else:
                info = net.get_json(f"{self.base}/rr_fileinfo?" + urllib.parse.urlencode({"name": path}),
                                    headers=h, what=self.cfg.name)
                if info.get("err") != 0:
                    return None
            return {"size": info.get("size"), "mtime": info.get("lastModified")}
        return self._session(work)

    def _start(self, remote_name: str) -> None:
        self._gcode(f'M32 "0:/gcodes/{remote_name}"')

    def _resume(self) -> None:
        self._gcode("M24")

    def pause(self) -> None:
        self._gcode("M25")

    def cancel(self) -> None:
        st = self.status()
        if st.state == "printing":
            self._gcode("M25")
            deadline = time.monotonic() + 60
            while self.status().state != "paused":
                if time.monotonic() > deadline:
                    raise Refused(f"{self.cfg.name} did not pause within 60 s, so the print was not cancelled",
                                  "Cancel it on the printer's screen or in Duet Web Control")
                time.sleep(1)
        self._gcode("M0")

    def emergency_stop(self) -> str:
        self._gcode("M112")
        return ("Sent M112 emergency stop: RepRapFirmware turned the heaters and motors off and reset the board. "
                "Check the printer before using it again")

    def web_ui(self) -> str:
        return f"{self.base}/ (Duet Web Control)"

    def camera(self) -> Camera:
        # The Duet object model has no camera; Duet Web Control's webcam is a browser setting.
        return Camera(self.cfg.camera_url, self.cfg.stream_url)
