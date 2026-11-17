"""Klipper through Moonraker's HTTP API (https://moonraker.readthedocs.io/en/latest/web_api/).

Covers stock Klipper, the Creality K2 family (Moonraker on 7125, the web UI on 4408) and rooted K1s.
Moonraker is GPL-3.0; this only calls its network API.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path

from .. import net
from ..errors import AuthFailed, NotFound, ProtocolError
from ..model import Camera, Status
from .base import Adapter

STATE_MAP = {"standby": "idle", "printing": "printing", "paused": "paused", "complete": "complete",
             "cancelled": "cancelled", "error": "error"}
OBJECTS = ("print_stats", "virtual_sdcard", "display_status", "extruder", "heater_bed", "webhooks", "idle_timeout")


class MoonrakerAdapter(Adapter):
    type_name = "moonraker"
    label = "Klipper (Moonraker)"
    default_port = 7125
    needs_secret = False  # optional API key, only when Moonraker's authorization requires it
    secret_label = "Moonraker API key (leave empty if your printer doesn't ask for one)"
    proven = "real hardware: Creality K2 (read-only status and camera via go2rtc), 2026-11-16"

    @property
    def base(self) -> str:
        return f"http://{self.cfg.host}:{self.port}"

    def _headers(self) -> dict:
        return {"X-Api-Key": self.secret} if self.secret else {}

    def _get(self, path: str) -> dict:
        reply = net.get_json(self.base + path, headers=self._headers(), what=self.cfg.name)
        if "result" not in reply:
            raise ProtocolError(f"{self.cfg.name} sent a Moonraker reply without a result")
        return reply["result"]

    def _post(self, path: str) -> None:
        net.send_json("POST", self.base + path, headers=self._headers(), what=self.cfg.name)

    @classmethod
    def probe(cls, host, port=None, timeout=2.0):
        port = port or cls.default_port
        try:
            reply = net.get_json(f"http://{host}:{port}/server/info", timeout=timeout)
        except AuthFailed:
            # Authorization is on. /access/info answers without a key and only Moonraker serves it.
            try:
                acc = net.get_json(f"http://{host}:{port}/access/info", timeout=timeout).get("result", {})
            except Exception:
                return None
            if isinstance(acc, dict) and "available_sources" in acc:
                return {"type": cls.type_name, "host": host, "port": port,
                        "detail": "Moonraker (needs an API key to pair)"}
            return None
        except Exception:
            return None
        result = reply.get("result")
        if isinstance(result, dict) and "klippy_state" in result:
            return {"type": cls.type_name, "host": host, "port": port,
                    "detail": f"Klipper {result.get('klippy_state')}, Moonraker {result.get('moonraker_version', '')}".strip()}
        return None

    def pair(self):
        info = self._get("/server/info")
        st = self.status()
        return {"state": st.state, "klippy": info.get("klippy_state", ""),
                "moonraker": info.get("moonraker_version", "")}

    def _chamber_object(self) -> str | None:
        try:
            objs = self._get("/printer/objects/list").get("objects", [])
        except Exception:
            return None
        for o in objs:
            if o.split(" ", 1)[0] in ("temperature_sensor", "heater_generic") and "chamber" in o:
                return o
        return None

    def status(self) -> Status:
        objs = list(OBJECTS)
        chamber = self._chamber_object()
        if chamber:
            objs.append(chamber)
        q = "&".join(urllib.parse.quote(o) for o in objs)
        st = self._get(f"/printer/objects/query?{q}").get("status", {})
        ps = st.get("print_stats", {})
        hooks = st.get("webhooks", {})
        state = STATE_MAP.get(ps.get("state", ""), "busy")
        message = ps.get("message", "") or ""
        if state in ("idle", "complete", "cancelled") and st.get("idle_timeout", {}).get("state") == "Printing":
            # A macro or gcode is running outside a print job (homing, leveling, a preheat, or a job
            # another app is starting). Seen live on a Creality K2. Not ready for a new print.
            state = "busy"
            message = message or "the printer is running commands (homing, leveling or heating)"
        if hooks.get("state") in ("shutdown", "error"):
            state = "error"
            message = hooks.get("state_message", "") or message
        progress = st.get("virtual_sdcard", {}).get("progress")
        if progress is None:
            progress = st.get("display_status", {}).get("progress")
        info = ps.get("info") or {}
        ext, bed = st.get("extruder", {}), st.get("heater_bed", {})
        return Status(
            printer=self.cfg.name, type=self.type_name, state=state, file=ps.get("filename", "") or "",
            progress=progress if state in ("printing", "paused") else (1.0 if state == "complete" else progress),
            nozzle_c=ext.get("temperature"), nozzle_target_c=ext.get("target"),
            bed_c=bed.get("temperature"), bed_target_c=bed.get("target"),
            chamber_c=st.get(chamber, {}).get("temperature") if chamber else None,
            elapsed_s=ps.get("print_duration"), layer=info.get("current_layer"),
            total_layers=info.get("total_layer"), message=message,
        )

    def upload(self, local_path: Path, remote_name: str) -> str:
        body, ctype = net.multipart({"root": "gcodes"}, "file", remote_name, Path(local_path).read_bytes())
        _, _, data = net.request("POST", f"{self.base}/server/files/upload", body=body, timeout=300,
                                 headers={"Content-Type": ctype, **self._headers()}, what=self.cfg.name)
        reply = net.parse_json(data, self.cfg.name)
        item = reply.get("item") or reply.get("result", {}).get("item") or {}
        return item.get("path", remote_name)

    def remote_info(self, remote_name: str) -> dict | None:
        try:
            meta = self._get("/server/files/metadata?filename=" + urllib.parse.quote(remote_name))
        except NotFound:
            return None
        return {"size": meta.get("size"), "mtime": meta.get("modified")}

    def _start(self, remote_name: str) -> None:
        self._post("/printer/print/start?filename=" + urllib.parse.quote(remote_name))

    def _resume(self) -> None:
        self._post("/printer/print/resume")

    def pause(self) -> None:
        self._post("/printer/print/pause")

    def cancel(self) -> None:
        self._post("/printer/print/cancel")

    def emergency_stop(self) -> str:
        self._post("/printer/emergency_stop")
        return ("Sent Klipper emergency stop (M112): heaters and motors are off and Klipper is shut down. "
                "Restart the firmware from the printer screen or the web UI before using it again")

    def camera(self) -> Camera:
        if self.cfg.camera_url:
            return Camera(self.cfg.camera_url, self.cfg.stream_url)
        try:
            cams = self._get("/server/webcams/list").get("webcams", [])
        except Exception:
            cams = []
        web = f"http://{self.cfg.host}" + ("" if self.port == 7125 else f":{self.port}")
        for cam in cams:
            if not cam.get("enabled", True):
                continue
            snap = urllib.parse.urljoin(web + "/", cam.get("snapshot_url", "") or "")
            stream = urllib.parse.urljoin(web + "/", cam.get("stream_url", "") or "")
            if cam.get("snapshot_url"):
                return Camera(snap, self.cfg.stream_url or stream)
        return Camera("", self.cfg.stream_url)

    def camera_headers(self) -> dict:
        return {}
