"""Creality stock firmware (K1, K2, Ender-3 V3, Hi): local WebSocket on 9999 plus HTTP upload.

This is the protocol Creality Print uses on the LAN. Creality does not document it and it may change
with firmware. Field names and state codes follow crlancli (MIT, https://github.com/axellse/crlancli)
and community notes. Record the firmware version whenever this is checked on a real printer.
"""

from __future__ import annotations

import functools
import importlib.resources
import json
import re
import time
import urllib.parse
from pathlib import Path

from .. import net
from ..errors import ProtocolError, Unreachable, Unsupported
from ..model import Camera, Slot, Status, bounded, clip
from .base import Adapter, filament_json

# Creality "state" codes. Unverified on hardware; see module docstring.
STATE_MAP = {0: "idle", 1: "printing", 2: "complete", 3: "error", 4: "cancelled", 5: "paused"}
GCODE_DIR = "/usr/data/printer_data/gcodes"


# ---------------------------------------------------------------- CFS (Creality Filament System)
#
# The K-series reports the CFS through Klipper's "box" object, read with Moonraker's
# /printer/objects/query?box. Shape, checked against one real K2 read on 2026-11-22: box.state is
# "connect" when a CFS is attached; units T1 to T4 each have state ("connect" when present) and arrays
# indexed A to D: material_type (a 6-character Creality code, "-1" when empty), color_value (hex, the
# last 6 characters are the color), remain_len (percent left, as a string), plus dry_and_humidity and
# temperature for the unit. Creality's firmware takes material_type and color_value from the spool's
# RFID text (https://github.com/CrealityOfficial/Hi_Klipper/blob/646b6aaaa4dc1fe9c4f773f1a425ea4666e5ed9b/klippy/extras/external_material_wrapper.py).
#
# Codes map to families through data/creality_materials.json. Creality Print
# (https://github.com/CrealityOfficial/CrealityPrint at 6262848e302b8c0d593f2b341cd01310aa2f58a1)
# publishes only 5-character filament_id values in its profiles and downloads its full material list
# from Creality's cloud, so the table lists only codes proven on a real printer. Any other code is
# reported as unknown, never guessed.

CFS_UNITS_MAX = 16
CFS_SLOT_LETTERS = "ABCD"
CFS_HINT = "The printer's firmware may have changed; record its version and report it"
MOONRAKER_PORT = 7125


@functools.cache
def creality_materials() -> dict[str, dict]:
    """Creality material code to {family, brand, name, nozzle_min_c, nozzle_max_c}."""
    raw = importlib.resources.files("gantrel_printer").joinpath("data", "creality_materials.json").read_text()
    return {m["code"]: m for m in json.loads(raw)["materials"]}


def _empty_code(v) -> bool:
    return v is None or v == -1 or (isinstance(v, str) and v.strip() in ("", "-1"))


def _cfs_color(v) -> str:
    """color_value is hex with the color in its last 6 characters ("0ffffff" is white)."""
    if not isinstance(v, str) or not 6 <= len(v.strip()) <= 8:
        return ""
    return v.strip()[-6:]


def external_slot(source: str) -> Slot:
    return Slot("ext", False, source=source,
                extra={"note": "the printer does not say what is on the external spool holder"})


def cfs_slots(reply: dict, what: str) -> list[Slot]:
    """Slots from a Moonraker reply to /printer/objects/query?box on a Creality K-series printer.

    No box object, or a box that is not connected, means one external spool holder ("ext") whose
    material the printer does not report.
    """
    try:
        return _cfs_slots(reply, what)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        raise ProtocolError(f"{what} sent CFS filament data in a shape Gantrel can't read", CFS_HINT) from None


def _cfs_slots(reply: dict, what: str) -> list[Slot]:
    result = reply.get("result")
    status = result.get("status") if isinstance(result, dict) else None
    if not isinstance(status, dict):
        raise ProtocolError(f"{what} sent a Moonraker reply without a status", CFS_HINT)
    box = status.get("box")
    if box is None:
        return [external_slot("moonraker: no box object")]
    if not isinstance(box, dict):
        raise ProtocolError(f"{what} sent CFS filament data in a shape Gantrel can't read", CFS_HINT)
    if box.get("state") != "connect":
        return [external_slot(f"moonraker box.state {clip(box.get('state'), 16) or 'missing'}")]
    units = sorted((int(m.group(1)), k) for k in box if isinstance(k, str) and (m := re.fullmatch(r"T(\d{1,2})", k)))
    table = creality_materials()
    slots: list[Slot] = []
    for _, key in units[:CFS_UNITS_MAX]:
        unit = box[key]
        if not isinstance(unit, dict):
            raise ProtocolError(f"{what} sent CFS unit {key} in a shape Gantrel can't read", CFS_HINT)
        if unit.get("state") != "connect":
            continue
        arrays = [unit.get(f) if unit.get(f) is not None else [] for f in ("material_type", "color_value", "remain_len")]
        if not all(isinstance(a, list) for a in arrays):
            raise ProtocolError(f"{what} sent CFS unit {key} in a shape Gantrel can't read", CFS_HINT)
        types, colors, remains = arrays
        extra = {"humidity": bounded(unit.get("dry_and_humidity"), 0, 100),
                 "temperature_c": bounded(unit.get("temperature"), -40, 150)}
        extra = {k: v for k, v in extra.items() if v is not None}
        for i, letter in enumerate(CFS_SLOT_LETTERS):
            src = f"moonraker box.{key}.material_type[{i}]"
            code = types[i] if i < len(types) else None
            if _empty_code(code):
                slots.append(Slot(f"{key}{letter}", True, source=src, extra=extra))
                continue
            code = clip(code, 16)  # a code that is not a string is kept as "" and reported unknown
            known = table.get(code, {})
            slots.append(Slot(
                f"{key}{letter}", False, family=known.get("family", "unknown"), material=code,
                brand=known.get("brand", ""), name=known.get("name", ""),
                color=_cfs_color(colors[i] if i < len(colors) else None),
                percent_left=remains[i] if i < len(remains) else None,
                nozzle_min_c=known.get("nozzle_min_c"), nozzle_max_c=known.get("nozzle_max_c"),
                source=src, extra=extra))
    return slots


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

    def start_by_hand(self, remote_name: str = "") -> str:
        what = f"Start {remote_name}" if remote_name else "Start or resume it"
        return f"{what} on the printer's touch screen (local files), or from Creality Print on the same network."

    def filaments(self) -> list[Slot]:
        """The CFS through the K2's own Moonraker (port 7125), read-only. The stock socket has no call for it."""
        port = int(self.cfg.extra.get("moonraker_port", MOONRAKER_PORT))
        try:
            _, _, data = net.request("GET", f"http://{self.cfg.host}:{port}/printer/objects/query?box",
                                     timeout=10, what=self.cfg.name)
        except Unreachable:
            raise Unsupported(f"{self.cfg.name} did not answer on port {port}, where Creality K2 printers report "
                              "their CFS, so it can't report loaded filament",
                              "On a K1, Ender-3 V3 or Hi, ask the person which filament is loaded") from None
        return cfs_slots(filament_json(data, self.cfg.name), self.cfg.name)

    def camera(self) -> Camera:
        if self.cfg.camera_url:
            return Camera(self.cfg.camera_url, self.cfg.stream_url)
        # K1 and Ender-3 V3 stock firmware serve MJPEG here. The K2 serves only WebRTC on port 8000;
        # relay it with go2rtc and pair with --camera-url.
        return Camera(f"http://{self.cfg.host}:8080/?action=snapshot",
                      self.cfg.stream_url or f"http://{self.cfg.host}:8080/?action=stream")
