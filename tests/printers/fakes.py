"""Fake printers for the contract suite: one shared printer state, one front end per protocol.

Each front end speaks its protocol over real sockets on 127.0.0.1 (HTTP, WebSocket, MQTT over TLS,
implicit FTPS, the Bambu JPEG camera socket, Elegoo's UDP hello, FlashForge's TCP G-code port), or
over a pseudo terminal for Marlin on USB serial, so adapters are tested through their real I/O code.
"""

from __future__ import annotations

import asyncio
import datetime
import email.parser
import email.policy
import hashlib
import json
import os
import re
import select
import socket
import socketserver
import ssl
import struct
import threading
import time
import tty
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 64 + b"\xff\xd9"


class Brain:
    """The printer: what it holds and what it was told to do."""

    def __init__(self, secret: str | None = None):
        self.secret = secret
        self.state = "idle"  # idle printing paused complete cancelled error
        self.running_macro = False  # Klipper busy outside a print job (Moonraker fake only)
        self.files: dict[str, bytes] = {}
        self.mtimes: dict[str, float] = {}
        self.current = ""
        self.events: list[str] = []
        # Loaded filament (Moonraker fake only): Klipper's "box" object on a Creality K-series with a CFS,
        # and Spoolman behind Moonraker: {"connected": bool, "spool_id": int | None, "spools": {id: spool}}.
        self.box: dict | bytes | None = None
        self.spoolman: dict | None = None
        self.proxied: list[dict] = []  # what the Spoolman proxy was asked for
        self.lock = threading.Lock()

    def put(self, name: str, data: bytes) -> None:
        """Store a file the way a printer does: replacing any file of that name, with a new mtime."""
        with self.lock:
            self.files[name] = data
            self.mtimes[name] = time.time() + len(self.mtimes) * 0.001

    def info(self, name: str) -> dict | None:
        if name not in self.files:
            return None
        return {"size": len(self.files[name]), "mtime": self.mtimes[name]}

    def start(self, name: str) -> bool:
        with self.lock:
            if name not in self.files or self.state in ("printing", "paused", "error"):
                return False
            self.state, self.current = "printing", name
            self.events.append(f"start:{name}")
            return True

    def act(self, what: str) -> None:
        with self.lock:
            self.events.append(what)
            if what == "pause" and self.state == "printing":
                self.state = "paused"
            elif what == "resume" and self.state == "paused":
                self.state = "printing"
            elif what == "cancel" and self.state in ("printing", "paused"):
                self.state = "cancelled"
            elif what == "estop":
                self.state = "error"

    @property
    def started(self) -> bool:
        return any(e.startswith("start:") for e in self.events)


def parse_multipart(body: bytes, ctype: str) -> dict:
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        f"Content-Type: {ctype}\r\n\r\n".encode() + body)
    out = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        out[name] = (part.get_filename(), part.get_payload(decode=True))
    return out


class _Handler(BaseHTTPRequestHandler):
    brain: Brain
    routes: object

    def log_message(self, *a):
        pass

    def _send(self, code: int, data=None, ctype="application/json", headers: dict | None = None):
        body = b"" if data is None else (data if isinstance(data, bytes) else json.dumps(data).encode())
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if body:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _authed(self) -> bool:
        return not self.brain.secret or self.headers.get("X-Api-Key") == self.brain.secret

    def do_GET(self):
        self.routes(self, "GET")

    def do_POST(self):
        self.routes(self, "POST")

    def do_PUT(self):
        self.routes(self, "PUT")

    def do_DELETE(self):
        self.routes(self, "DELETE")


def serve_http(brain: Brain, routes, **attrs) -> ThreadingHTTPServer:
    handler = type("H", (_Handler,), {"brain": brain, "routes": staticmethod(routes), **attrs})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------- Moonraker

MOON_STATE = {"idle": "standby", "printing": "printing", "paused": "paused", "complete": "complete",
              "cancelled": "cancelled", "error": "error"}


def moonraker_routes(h: _Handler, method: str):
    b = h.brain
    url = urllib.parse.urlsplit(h.path)
    q = urllib.parse.parse_qs(url.query, keep_blank_values=True)
    if url.path == "/webcam/" and method == "GET":
        return h._send(200, JPEG, "image/jpeg")
    if method == "GET" and url.path == "/access/info":
        return h._send(200, {"result": {"default_source": "moonraker", "available_sources": ["moonraker"]}})
    if not h._authed():
        return h._send(401, {"error": {"message": "Unauthorized"}})
    if method == "GET" and url.path == "/server/info":
        return h._send(200, {"result": {"klippy_state": "shutdown" if b.state == "error" else "ready",
                                        "moonraker_version": "fake"}})
    if method == "GET" and url.path == "/printer/objects/list":
        return h._send(200, {"result": {"objects": ["print_stats", "virtual_sdcard", "extruder", "heater_bed",
                                                    "temperature_sensor chamber_temp", "webhooks"]
                                        + (["box"] if b.box is not None else [])}})
    if method == "GET" and url.path == "/printer/objects/query" and "box" in q:
        if isinstance(b.box, bytes):  # a raw reply, for size and shape tests
            return h._send(200, b.box)
        return h._send(200, {"result": {"eventtime": 1.0, "status": {} if b.box is None else {"box": b.box}}})
    if url.path.startswith("/server/spoolman/"):
        return spoolman_routes(h, method, url.path)
    if method == "GET" and url.path == "/printer/objects/query":
        st = {"print_stats": {"state": MOON_STATE[b.state], "filename": b.current, "print_duration": 12.0,
                              "message": "", "info": {"current_layer": 1, "total_layer": 10}},
              "virtual_sdcard": {"progress": 0.1 if b.state in ("printing", "paused") else 0.0},
              "extruder": {"temperature": 215.0, "target": 215.0}, "heater_bed": {"temperature": 60.0, "target": 60.0},
              "temperature_sensor chamber_temp": {"temperature": 30.0},
              "webhooks": {"state": "shutdown" if b.state == "error" else "ready", "state_message": "fake"},
              "idle_timeout": {"state": "Printing" if b.running_macro or b.state == "printing" else "Idle"}}
        return h._send(200, {"result": {"status": {k: v for k, v in st.items() if k in q}}})
    if method == "GET" and url.path == "/server/webcams/list":
        return h._send(200, {"result": {"webcams": [{"name": "cam", "enabled": True,
                                                     "snapshot_url": "/webcam/?action=snapshot",
                                                     "stream_url": "/webcam/?action=stream"}]}})
    if method == "GET" and url.path == "/server/files/metadata":
        info = b.info(q.get("filename", [""])[0])
        if info is None:
            return h._send(404, {"error": {"message": "Metadata not available"}})
        return h._send(200, {"result": {"size": info["size"], "modified": info["mtime"]}})
    if method == "POST" and url.path == "/server/files/upload":
        parts = parse_multipart(h._body(), h.headers["Content-Type"])
        name, data = parts["file"]
        b.put(name, data)
        return h._send(201, {"item": {"path": name, "root": "gcodes"}, "action": "create_file"})
    if method == "POST" and url.path == "/printer/print/start":
        ok = b.start(q.get("filename", [""])[0])
        return h._send(200 if ok else 400, {"result": "ok"} if ok else {"error": {"message": "cannot start"}})
    for path, act in (("/printer/print/pause", "pause"), ("/printer/print/resume", "resume"),
                      ("/printer/print/cancel", "cancel"), ("/printer/emergency_stop", "estop")):
        if method == "POST" and url.path == path:
            b.act(act)
            return h._send(200, {"result": "ok"})
    h._send(404, {"error": {"message": "not found"}})


def spoolman_routes(h: _Handler, method: str, path: str):
    """Moonraker's Spoolman endpoints. 404 when Spoolman is not set up, like Moonraker without the component."""
    sm = h.brain.spoolman
    if sm is None:
        return h._send(404, {"error": {"message": "Not Found"}})
    if method == "GET" and path == "/server/spoolman/status":
        return h._send(200, {"result": {"spoolman_connected": sm["connected"], "pending_reports": [],
                                        "spool_id": sm.get("spool_id")}})
    if method == "GET" and path == "/server/spoolman/spool_id":
        return h._send(200, {"result": {"spool_id": sm.get("spool_id")}})
    if method == "POST" and path == "/server/spoolman/proxy":
        req = json.loads(h._body() or b"{}")
        h.brain.proxied.append(req)
        m = re.fullmatch(r"/v1/spool/(\d+)", req.get("path", ""))
        spool = sm["spools"].get(int(m.group(1))) if m and req.get("request_method") == "GET" else None
        if spool is None:
            return h._send(200, {"result": {"response": None, "error": {"status_code": 404, "message": "no spool"}}})
        return h._send(200, {"result": {"response": spool, "error": None}})
    h._send(404, {"error": {"message": "not found"}})


# ---------------------------------------------------------------- OctoPrint

OCTO_TEXT = {"idle": "Operational", "printing": "Printing", "paused": "Paused", "complete": "Operational",
             "cancelled": "Operational", "error": "Error"}


def octoprint_routes(h: _Handler, method: str):
    b = h.brain
    url = urllib.parse.urlsplit(h.path)
    if method == "GET" and url.path == "/":
        return h._send(200, b"<html><title>OctoPrint</title></html>", "text/html")
    if url.path == "/webcam/" and method == "GET":
        return h._send(200, JPEG, "image/jpeg")
    if not h._authed():
        return h._send(403, {"error": "Forbidden"})
    if method == "GET" and url.path == "/api/version":
        return h._send(200, {"api": "0.1", "server": "1.10.3", "text": "OctoPrint 1.10.3"})
    if method == "GET" and url.path == "/api/job":
        busy = b.state in ("printing", "paused")
        return h._send(200, {"state": OCTO_TEXT[b.state], "job": {"file": {"name": b.current or None}},
                             "progress": {"completion": 10.0 if busy else None, "printTime": 12 if busy else None,
                                          "printTimeLeft": 600 if busy else None}})
    if method == "GET" and url.path == "/api/printer":
        flags = {"operational": b.state != "error", "printing": b.state == "printing", "paused": b.state == "paused",
                 "pausing": False, "cancelling": False, "error": b.state == "error",
                 "ready": b.state in ("idle", "complete", "cancelled"), "closedOrError": b.state == "error"}
        return h._send(200, {"state": {"text": OCTO_TEXT[b.state], "flags": flags},
                             "temperature": {"tool0": {"actual": 215.0, "target": 215.0},
                                             "bed": {"actual": 60.0, "target": 60.0}}})
    if method == "GET" and url.path == "/api/settings":
        return h._send(200, {"webcam": {"snapshotUrl": "/webcam/?action=snapshot", "streamUrl": "/webcam/?action=stream"}})
    if method == "POST" and url.path == "/api/files/local":
        parts = parse_multipart(h._body(), h.headers["Content-Type"])
        name, data = parts["file"]
        b.put(name, data)
        if parts.get("print", (None, b"false"))[1] == b"true":
            b.start(name)  # what real OctoPrint does; the contract test must never see this
        return h._send(201, {"done": True, "files": {"local": {"name": name}}})
    if method == "GET" and url.path.startswith("/api/files/local/"):
        info = b.info(urllib.parse.unquote(url.path.rsplit("/", 1)[1]))
        if info is None:
            return h._send(404, {"error": "File not found"})
        return h._send(200, {"size": info["size"], "date": int(info["mtime"] * 1000)})
    if method == "POST" and url.path.startswith("/api/files/local/"):
        cmd = json.loads(h._body() or b"{}")
        name = urllib.parse.unquote(url.path.rsplit("/", 1)[1])
        if cmd.get("command") == "select" and cmd.get("print"):
            ok = b.start(name)
            return h._send(204 if ok else 409)
        return h._send(204)
    if method == "POST" and url.path == "/api/job":
        cmd = json.loads(h._body() or b"{}")
        if cmd.get("command") == "pause":
            b.act("pause" if cmd.get("action") == "pause" else "resume")
        elif cmd.get("command") == "cancel":
            b.act("cancel")
        return h._send(204)
    if method == "POST" and url.path == "/api/printer/command":
        cmd = json.loads(h._body() or b"{}")
        if "M112" in cmd.get("commands", []):
            b.act("estop")
        return h._send(204)
    h._send(404, {"error": "not found"})


# ---------------------------------------------------------------- PrusaLink

PRUSA_STATE = {"idle": "IDLE", "printing": "PRINTING", "paused": "PAUSED", "complete": "FINISHED",
               "cancelled": "STOPPED", "error": "ERROR"}


def prusalink_routes(h: _Handler, method: str):
    b = h.brain
    url = urllib.parse.urlsplit(h.path)
    digest = getattr(h, "digest_users", None)
    if digest is not None:
        # Newer Buddy firmware: digest auth (user maker) on every call, /api/version included.
        if not digest_ok(h, digest, "Printer API"):
            return digest_challenge(h, "Printer API")
    elif getattr(h, "key_challenges_digest", False) and not h._authed():
        # PrusaLink on an MK3 Pi: takes an API key, yet answers every unauthenticated call,
        # /api/version included, with a digest challenge.
        return digest_challenge(h, "Printer API")
    elif not (method == "GET" and url.path == "/api/version") and not h._authed():
        return h._send(401, {"title": "Unauthorized"})
    if method == "GET" and url.path == "/api/version":
        return h._send(200, {"api": "2.0.0", "server": "2.1.2", "text": "PrusaLink"})
    busy = b.state in ("printing", "paused")
    if method == "GET" and url.path == "/api/v1/status":
        st = {"printer": {"state": PRUSA_STATE[b.state], "temp_nozzle": 215.0, "target_nozzle": 215.0,
                          "temp_bed": 60.0, "target_bed": 60.0}}
        if busy:
            st["job"] = {"id": 7, "progress": 10, "time_printing": 12, "time_remaining": 600}
        return h._send(200, st)
    if method == "GET" and url.path == "/api/v1/job":
        if not busy:
            return h._send(204)
        return h._send(200, {"id": 7, "state": PRUSA_STATE[b.state],
                             "file": {"name": b.current, "display_name": b.current}})
    if method == "GET" and url.path == "/api/v1/info":
        return h._send(200, {"serial": "FAKE-MK4", "hostname": "fake-prusa"})
    if method == "GET" and url.path == "/api/v1/cameras/snap":
        return h._send(200, JPEG, "image/jpeg")
    if url.path.startswith("/api/v1/files/usb/"):
        name = urllib.parse.unquote(url.path.rsplit("/", 1)[1])
        if method == "GET":
            info = b.info(name)
            if info is None:
                return h._send(404, {"title": "Not found"})
            return h._send(200, {"name": name, "size": info["size"], "m_timestamp": int(info["mtime"] * 1000)})
        if method == "PUT":
            if name in b.files and h.headers.get("Overwrite") != "?1":
                return h._send(409, {"title": "File exists"})
            b.put(name, h._body())
            if h.headers.get("Print-After-Upload") == "?1":
                b.start(name)
            return h._send(201)
        if method == "POST":
            return h._send(204 if b.start(name) else 409)
    if url.path.startswith("/api/v1/job/7"):
        if not busy:
            return h._send(409, {"title": "Not printing"})
        if method == "PUT" and url.path.endswith("/pause"):
            b.act("pause")
            return h._send(204)
        if method == "PUT" and url.path.endswith("/resume"):
            b.act("resume")
            return h._send(204)
        if method == "DELETE":
            b.act("cancel")
            return h._send(204)
    h._send(404, {"title": "Not found"})


# ---------------------------------------------------------------- Creality stock (WebSocket 9999 + HTTP)

CREALITY_STATE = {"idle": 0, "printing": 1, "complete": 2, "error": 3, "cancelled": 4, "paused": 5}


class FakeCreality:
    def __init__(self, brain: Brain):
        self.brain = brain
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()
        self.port = 0

        async def handler(ws):
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("method") == "get":
                    await ws.send(json.dumps(self.report()))
                elif msg.get("method") == "set":
                    self.on_set(msg.get("params", {}))
                    await ws.send("ok")

        async def main():
            from websockets.asyncio.server import serve

            self.server = await serve(handler, "127.0.0.1", 0)
            self.port = self.server.sockets[0].getsockname()[1]
            ready.set()
            await self.server.serve_forever()

        threading.Thread(target=self.loop.run_until_complete, args=(main(),), daemon=True).start()
        ready.wait(5)
        self.http = serve_http(brain, self.http_routes)

    def report(self) -> dict:
        b = self.brain
        busy = b.state in ("printing", "paused")
        return {"state": CREALITY_STATE[b.state], "printProgress": 10 if busy else 0, "nozzleTemp": "215.000000",
                "targetNozzleTemp": 215, "bedTemp0": "60.000000", "targetBedTemp0": 60, "boxTemp": 30,
                "printFileName": f"/usr/data/printer_data/gcodes/{b.current}" if b.current else "",
                "printJobTime": 12, "printLeftTime": 600, "layer": 1, "TotalLayer": 10, "model": "FAKE",
                "modelVersion": "fake 1.0", "err": {"errcode": 0}}

    def on_set(self, params: dict):
        b = self.brain
        if "opGcodeFile" in params:
            b.start(params["opGcodeFile"].rsplit("/", 1)[-1])
        elif params.get("pause") == 1:
            b.act("pause")
        elif params.get("pause") == 0:
            b.act("resume")
        elif params.get("stop") == 1:
            b.act("cancel")
        elif params.get("gcodeCmd") == "M112":
            b.act("estop")

    @staticmethod
    def http_routes(h: _Handler, method: str):
        url = urllib.parse.urlsplit(h.path)
        if method == "POST" and url.path.startswith("/upload/"):
            parts = parse_multipart(h._body(), h.headers["Content-Type"])
            name, data = parts["file"]
            h.brain.put(name, data)
            return h._send(200, {"code": 200})
        if method == "GET" and url.path == "/":
            return h._send(200, JPEG, "image/jpeg")
        h._send(404, {})

    def close(self):
        self.http.shutdown()
        self.loop.call_soon_threadsafe(self.server.close)


# ---------------------------------------------------------------- Bambu Lab (MQTT TLS, FTPS, camera)

# Real firmware keeps gcode_state FAILED after a person stops a print, until the next print starts.
BAMBU_STATE = {"idle": "IDLE", "printing": "RUNNING", "paused": "PAUSE", "complete": "FINISH",
               "cancelled": "FAILED", "error": "FAILED"}
BAMBU_USER_CANCEL = 0x0300400C  # print_error after a stop on the screen, in the app or over MQTT


def _tls_server_ctx(cert: str, key: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    return ctx


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c:
            raise ConnectionError
        buf += c
    return buf


def _mqtt_packet(ptype: int, payload: bytes) -> bytes:
    n, rl = len(payload), bytearray()
    while True:
        byte, n = n % 128, n // 128
        rl.append(byte | (0x80 if n else 0))
        if not n:
            break
    return bytes([ptype]) + bytes(rl) + payload


class FakeBambu:
    def __init__(self, brain: Brain, serial: str, cert: str, key: str, alt: tuple[str, str] | None = None):
        self.brain, self.serial = brain, serial
        self.ctx = _tls_server_ctx(cert, key)
        # With alt, connections alternate between the main and the alternate certificate, like an
        # interceptor that passes some connections through to the real printer.
        self.ctxs = [self.ctx] + ([_tls_server_ctx(*alt)] if alt else [])
        self.connections = 0
        self.logins: list[tuple[str, int]] = []  # (service, index of the certificate it arrived on)
        self.ignored: list[dict] = []
        # Report overrides for tests: print_error (None: the firmware's own value) and the HMS list.
        self.print_error: int | None = None
        self.hms: list[dict] = []
        # Loaded filament for the report: the print.ams object and the external spool, when set.
        self.ams: object = None
        self.vt_tray: object = None
        self.mqtt = self._listen(self._mqtt_client)
        self.ftp = self._listen(self._ftp_client)
        self.cam = self._listen(self._cam_client)

    def _listen(self, fn) -> int:
        srv = socket.create_server(("127.0.0.1", 0))

        def loop():
            while True:
                try:
                    raw, _ = srv.accept()
                except OSError:
                    return
                threading.Thread(target=self._wrap, args=(raw, fn), daemon=True).start()

        threading.Thread(target=loop, daemon=True).start()
        return srv.getsockname()[1]

    def _wrap(self, raw, fn):
        idx = self.connections % len(self.ctxs)
        self.connections += 1
        try:
            with self.ctxs[idx].wrap_socket(raw, server_side=True) as tls:
                tls.cert_index = idx
                fn(tls)
        except (OSError, ssl.SSLError, ConnectionError):
            pass

    def report(self) -> dict:
        b = self.brain
        busy = b.state in ("printing", "paused")
        return {"print": {"gcode_state": BAMBU_STATE[b.state], "mc_percent": 10 if busy else 0,
                          "mc_remaining_time": 10, "nozzle_temper": 215.0, "nozzle_target_temper": 215.0,
                          "bed_temper": 60.0, "bed_target_temper": 60.0, "chamber_temper": 30.0,
                          "subtask_name": b.current, "layer_num": 1, "total_layer_num": 10,
                          "print_error": self._print_error(), "hms": self.hms, "command": "push_status",
                          **({"ams": self.ams} if self.ams is not None else {}),
                          **({"vt_tray": self.vt_tray} if self.vt_tray is not None else {})}}

    def _print_error(self) -> int:
        if self.print_error is not None:
            return self.print_error
        return {"cancelled": BAMBU_USER_CANCEL, "error": 0x0500C011}.get(self.brain.state, 0)

    # MQTT 3.1.1: just enough of a broker for one printer.
    def _mqtt_client(self, s):
        subscribed = False
        while True:
            head = _recv_exact(s, 1)[0]
            mult, length = 1, 0
            while True:
                byte = _recv_exact(s, 1)[0]
                length += (byte & 0x7F) * mult
                mult *= 128
                if not byte & 0x80:
                    break
            body = _recv_exact(s, length) if length else b""
            ptype = head >> 4
            if ptype == 1:  # CONNECT
                i = 2 + struct.unpack(">H", body[:2])[0] + 1  # protocol name, level
                flags = body[i]
                i += 3
                fields = []
                while i < len(body):
                    n = struct.unpack(">H", body[i:i + 2])[0]
                    fields.append(body[i + 2:i + 2 + n])
                    i += 2 + n
                user = fields[1].decode() if flags & 0x80 and len(fields) > 1 else None
                pw = fields[2].decode() if flags & 0x40 and len(fields) > 2 else None
                if pw is not None:
                    self.logins.append(("mqtt", s.cert_index))
                ok = user == "bblp" and pw == self.brain.secret
                s.sendall(_mqtt_packet(0x20, b"\x00" + (b"\x00" if ok else b"\x05")))
                if not ok:
                    return
            elif ptype == 8:  # SUBSCRIBE
                pid = body[:2]
                n = struct.unpack(">H", body[2:4])[0]
                subscribed = body[4:4 + n].decode() == f"device/{self.serial}/report"
                s.sendall(_mqtt_packet(0x90, pid + b"\x00"))
            elif ptype == 3:  # PUBLISH, QoS 0
                n = struct.unpack(">H", body[:2])[0]
                topic = body[2:2 + n].decode()
                payload = json.loads(body[2 + n:])
                if topic != f"device/{self.serial}/request":
                    continue
                if "pushing" in payload:
                    if subscribed:
                        t = f"device/{self.serial}/report".encode()
                        s.sendall(_mqtt_packet(0x30, struct.pack(">H", len(t)) + t + json.dumps(self.report()).encode()))
                    continue
                cmd = payload.get("print", {})
                c = cmd.get("command")
                if c in ("gcode_file", "project_file"):
                    name = cmd.get("param", "").rsplit("/", 1)[-1] if c == "gcode_file" else cmd["url"].rsplit("/", 1)[-1]
                    self.brain.start(name)
                elif c in ("pause", "resume"):
                    self.brain.act(c)
                elif c == "stop":
                    self.brain.act("cancel" if self.brain.state in ("printing", "paused") else "stop")
                elif c == "gcode_line" and "M104 S0" in cmd.get("param", ""):
                    self.brain.act("estop")
                else:
                    self.ignored.append(cmd)
            elif ptype == 12:  # PINGREQ
                s.sendall(b"\xd0\x00")
            elif ptype == 14:  # DISCONNECT
                return

    # Implicit FTPS: USER PASS PBSZ PROT TYPE PASV STOR QUIT.
    def _ftp_client(self, s):
        f = s.makefile("rb")

        def say(line):
            s.sendall((line + "\r\n").encode())

        say("220 fake bambu ftps")
        user = ok = None
        data_srv = None
        while True:
            line = f.readline().decode().strip()
            if not line:
                return
            cmd, _, arg = line.partition(" ")
            cmd = cmd.upper()
            if cmd == "USER":
                user = arg
                say("331 password")
            elif cmd == "PASS":
                self.logins.append(("ftp", s.cert_index))
                ok = user == "bblp" and arg == self.brain.secret
                say("230 ok" if ok else "530 login incorrect")
            elif cmd in ("PBSZ", "PROT", "TYPE"):
                say("200 ok")
            elif not ok:
                say("530 log in first")
            elif cmd == "PASV":
                data_srv = socket.create_server(("127.0.0.1", 0))
                p = data_srv.getsockname()[1]
                say(f"227 Entering Passive Mode (127,0,0,1,{p >> 8},{p & 255})")
            elif cmd == "STOR" and data_srv:
                say("150 ok")
                raw, _ = data_srv.accept()
                with self.ctx.wrap_socket(raw, server_side=True) as d:
                    buf = b""
                    while True:
                        try:
                            chunk = d.recv(65536)
                        except ssl.SSLError:
                            break
                        if not chunk:
                            break
                        buf += chunk
                    try:
                        d.unwrap()
                    except (ssl.SSLError, OSError):
                        pass
                data_srv.close()
                self.brain.put(arg, buf)
                say("226 done")
            elif cmd == "SIZE":
                info = self.brain.info(arg)
                say(f"213 {info['size']}" if info else "550 not found")
            elif cmd == "MDTM":
                info = self.brain.info(arg)
                say(f"213 {int(info['mtime'] * 1000)}" if info else "550 not found")
            elif cmd == "QUIT":
                say("221 bye")
                return
            else:
                say("502 not implemented")

    def _cam_client(self, s):
        auth = _recv_exact(s, 80)
        self.logins.append(("camera", s.cert_index))
        user = auth[16:48].rstrip(b"\0").decode()
        code = auth[48:80].rstrip(b"\0").decode()
        if user != "bblp" or code != self.brain.secret:
            return
        s.sendall(struct.pack("<IIII", len(JPEG), 0, 1, 0) + JPEG)


# ---------------------------------------------------------------- shared helpers for the newer fakes

def digest_challenge(h: _Handler, realm: str) -> None:
    nonce = uuid.uuid4().hex
    h.server.nonces = getattr(h.server, "nonces", set()) | {nonce}
    body = b'{"message": "Authorization required"}'
    h.send_response(401)
    h.send_header("WWW-Authenticate", f'Digest realm="{realm}", nonce="{nonce}", qop="auth", algorithm=MD5')
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


def digest_ok(h: _Handler, users: dict, realm: str) -> bool:
    """Check an RFC 7616 MD5 digest answer (qop auth) the way the printer does."""
    auth = h.headers.get("Authorization", "")
    if not auth.startswith("Digest "):
        return False
    p = {k: q or u for k, q, u in re.findall(r'(\w+)=(?:"([^"]*)"|([^\s,]*))', auth[7:])}
    h.digest_seen = getattr(h, "digest_seen", []) + [p.get("username")]
    password = users.get(p.get("username"))
    if password is None or p.get("realm") != realm or p.get("nonce") not in getattr(h.server, "nonces", set()):
        return False

    def md5(x: str) -> str:
        return hashlib.md5(x.encode()).hexdigest()

    ha1, ha2 = md5(f"{p['username']}:{realm}:{password}"), md5(f"{h.command}:{p.get('uri', '')}")
    if p.get("qop"):
        want = md5(f"{ha1}:{p['nonce']}:{p.get('nc')}:{p.get('cnonce')}:{p['qop']}:{ha2}")
    else:
        want = md5(f"{ha1}:{p['nonce']}:{ha2}")
    return p.get("response") == want


def send_mjpeg(h: _Handler, frames: int = 2) -> None:
    """A camera stream: multipart/x-mixed-replace, a couple of frames, then the connection closes."""
    h.send_response(200)
    h.send_header("Content-Type", "multipart/x-mixed-replace;boundary=frame")
    h.end_headers()
    for _ in range(frames):
        h.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(JPEG)).encode()
                      + b"\r\n\r\n" + JPEG + b"\r\n")


def serve_jpeg() -> ThreadingHTTPServer:
    """A stand-alone JPEG camera at /cam.jpg (for printers whose API lists no camera)."""

    def routes(h, method):
        if urllib.parse.urlsplit(h.path).path == "/cam.jpg":
            return h._send(200, JPEG, "image/jpeg")
        h._send(404, {})

    return serve_http(Brain(), routes)


def _form(h: _Handler, body: bytes | None = None) -> dict:
    data = h._body() if body is None else body
    return {k: v[0] for k, v in urllib.parse.parse_qs(data.decode(), keep_blank_values=True).items()}


# ---------------------------------------------------------------- Elegoo SDCP v3 (WebSocket, UDP, HTTP)

ELEGOO_CODE = {"idle": 0, "printing": 13, "paused": 6, "cancelled": 8, "complete": 9, "error": 14}


class FakeElegoo:
    MB = "ELEGOOFAKE000001"

    def __init__(self, brain: Brain, resin: bool = False):
        self.brain, self.resin = brain, resin
        self.starts: list[dict] = []
        self.cmds: list[int] = []
        self.uploads: dict[str, bytearray] = {}
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        async def handler(ws):
            if ws.request.path != "/websocket":
                await ws.close()
                return
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                d = msg.get("Data") if isinstance(msg, dict) else None
                if not isinstance(d, dict) or d.get("MainboardID") != self.MB:
                    continue  # the real board ignores requests addressed to another board
                cmd = d.get("Cmd")
                self.cmds.append(cmd)
                reply = self.on_cmd(cmd, d.get("Data") or {})
                if reply is None:
                    continue  # unknown Cmd: the real firmware may crash; the fake just goes quiet
                await ws.send(json.dumps({"Id": "fake", "Topic": f"sdcp/response/{self.MB}", "Data": {
                    "Cmd": cmd, "Data": reply, "RequestID": d.get("RequestID"), "MainboardID": self.MB,
                    "TimeStamp": int(time.time())}}))
                if cmd == 0:
                    await ws.send(json.dumps({"Id": "fake", "Topic": f"sdcp/status/{self.MB}",
                                              "Status": self.status(), "MainboardID": self.MB,
                                              "TimeStamp": int(time.time())}))

        async def main():
            from websockets.asyncio.server import serve

            self.server = await serve(handler, "127.0.0.1", 0)
            self.port = self.server.sockets[0].getsockname()[1]
            ready.set()
            await self.server.serve_forever()

        threading.Thread(target=self.loop.run_until_complete, args=(main(),), daemon=True).start()
        ready.wait(5)
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.udp_port = self.udp.getsockname()[1]
        threading.Thread(target=self._udp_loop, daemon=True).start()
        self.http = serve_http(brain, self.http_routes)

    def _udp_loop(self):
        while True:
            try:
                data, addr = self.udp.recvfrom(1024)
            except OSError:
                return
            if data.strip() == b"M99999":
                name = "Saturn 4 Ultra" if self.resin else "Centauri Carbon"
                self.udp.sendto(json.dumps({"Id": "fake", "Data": {
                    "Name": "fake-elegoo", "MachineName": name, "BrandName": "ELEGOO", "MainboardIP": "127.0.0.1",
                    "MainboardID": self.MB, "ProtocolVersion": "V3.0.0", "FirmwareVersion": "V1.1.29"}}).encode(), addr)

    def status(self) -> dict:
        b = self.brain
        busy = b.state in ("printing", "paused")
        st = {"CurrentStatus": [1 if busy else 0],
              "PrintInfo": {"Status": ELEGOO_CODE[b.state], "CurrentLayer": 1 if busy else 0,
                            "TotalLayer": 10 if busy else 0, "CurrentTicks": 12, "TotalTicks": 612,
                            "Filename": f"/local/{b.current}" if busy else "", "Progress": 10 if busy else 0}}
        if not self.resin:
            st.update({"TempOfNozzle": 215.0, "TempTargetNozzle": 215.0, "TempOfHotbed": 60.0,
                       "TempTargetHotbed": 60.0, "TempOfBox": 30.0})
        return st

    def on_cmd(self, cmd, data: dict):
        b = self.brain
        if cmd == 0:
            return {"Ack": 0}
        if cmd == 128:
            self.starts.append(data)
            return {"Ack": 0 if b.start(str(data.get("Filename", "")).removeprefix("/local/")) else 1}
        if cmd in (129, 130, 131):
            b.act({129: "pause", 130: "cancel", 131: "resume"}[cmd])
            return {"Ack": 0}
        if cmd == 258:
            return {"Ack": 0, "FileList": [{"name": f"/local/{n}", "FileSize": len(d), "CreateTime": b.mtimes[n],
                                            "TotalLayers": 10, "type": 1} for n, d in b.files.items()]}
        return None

    def http_routes(self, h: _Handler, method: str):
        url = urllib.parse.urlsplit(h.path)
        if method == "POST" and url.path == "/uploadFile/upload":
            parts = parse_multipart(h._body(), h.headers["Content-Type"])
            name, chunk = parts["File"]
            uid, offset = parts["Uuid"][1].decode(), int(parts["Offset"][1])
            total, md5 = int(parts["TotalSize"][1]), parts["S-File-MD5"][1].decode()
            buf = self.uploads.setdefault(uid, bytearray())
            if offset != len(buf):
                return h._send(200, {"code": "000004", "messages": "bad offset"})
            buf += chunk
            if len(buf) >= total:
                del self.uploads[uid]
                if hashlib.md5(bytes(buf)).hexdigest() != md5:
                    return h._send(200, {"code": "000003", "messages": "md5 mismatch"})
                self.brain.put(name, bytes(buf))
            return h._send(200, {"code": "000000", "messages": None, "data": None, "success": True})
        if method == "GET" and url.path == "/video" and not self.resin:
            return send_mjpeg(h)
        h._send(404, {})

    def close(self):
        self.http.shutdown()
        self.udp.close()

        async def stop():
            self.server.close()
            await self.server.wait_closed()

        try:
            asyncio.run_coroutine_threadsafe(stop(), self.loop).result(5)
        except Exception:
            pass


# ---------------------------------------------------------------- Duet: RepRapFirmware rr_ API and DSF

DUET_STATE = {"idle": "idle", "printing": "processing", "paused": "paused", "cancelled": "idle",
              "complete": "idle", "error": "halted"}


class FakeDuet:
    def __init__(self, brain: Brain, dsf: bool = False):
        self.brain, self.dsf = brain, dsf
        self.sessions: set[str] = set()
        self.codes: list[str] = []
        self.http = serve_http(brain, self.routes)
        self.port = self.http.server_address[1]

    def model(self) -> dict:
        b = self.brain
        busy = b.state in ("printing", "paused")
        return {
            "boards": [{"name": "Duet 3 MB6HC", "firmwareVersion": "3.5.2"}],
            "state": {"status": DUET_STATE[b.state], "displayMessage": ""},
            "job": {"file": {"fileName": f"0:/gcodes/{b.current}", "size": 1000, "numLayers": 10} if busy
                    else {"fileName": None}, "filePosition": 100 if busy else 0, "duration": 12 if busy else None,
                    "layer": 1 if busy else None, "timesLeft": {"slicer": 600, "file": 610}},
            "heat": {"heaters": [{"current": 60.0, "active": 60.0, "state": "active"},
                                 {"current": 215.0, "active": 215.0, "state": "active"}],
                     "bedHeaters": [0, -1], "chamberHeaters": [-1]},
            "tools": [{"number": 0, "heaters": [1]}],
        }

    def gcode(self, code: str) -> None:
        b = self.brain
        c = code.strip()
        self.codes.append(c)
        m = re.match(r'M32\s+"0:/gcodes/([^"]+)"', c)
        if m:
            b.start(m.group(1))
        elif c == "M24":
            b.act("resume")
        elif c == "M25":
            b.act("pause")
        elif c == "M0" and b.state == "paused":
            b.act("cancel")  # RRF: M0 while paused cancels the print
        elif c == "M112":
            b.act("estop")

    def file_info(self, path: str) -> dict | None:
        name = path.removeprefix("0:/gcodes/")
        info = self.brain.info(name)
        if info is None:
            return None
        stamp = datetime.datetime.fromtimestamp(info["mtime"]).isoformat(timespec="microseconds")
        return {"size": info["size"], "lastModified": stamp, "fileName": path}

    def _session_ok(self, h) -> bool:
        if self.dsf and not self.brain.secret:
            return True  # DSF with no password needs no session key
        return h.headers.get("X-Session-Key") in self.sessions

    def routes(self, h: _Handler, method: str):
        url = urllib.parse.urlsplit(h.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query, keep_blank_values=True).items()}
        if url.path == "/cam.jpg":
            return h._send(200, JPEG, "image/jpeg")
        if self.dsf:
            return self.dsf_routes(h, method, url, q)
        if url.path == "/rr_connect":
            if self.brain.secret and q.get("password") != self.brain.secret:
                return h._send(200, {"err": 1})
            key = str(len(self.sessions) + 1000)
            self.sessions.add(key)
            return h._send(200, {"err": 0, "sessionTimeout": 8000, "boardType": "duet3mb6hc", "sessionKey": int(key),
                                 "apiLevel": 1})
        if not url.path.startswith("/rr_"):
            # A standalone RepRapFirmware board serves Duet Web Control for any other path, with 200.
            return h._send(200, b"<!DOCTYPE html><html><title>Duet Web Control</title></html>", "text/html")
        if not self._session_ok(h):
            return h._send(401, {})
        if url.path == "/rr_disconnect":
            self.sessions.discard(h.headers.get("X-Session-Key"))
            return h._send(200, {"err": 0})
        if url.path == "/rr_model":
            return h._send(200, {"key": q.get("key"), "flags": q.get("flags"), "result": self.model().get(q.get("key"))})
        if url.path == "/rr_gcode":
            self.gcode(q.get("gcode", ""))
            return h._send(200, {"buff": 255})
        if url.path == "/rr_upload" and method == "POST":
            self.brain.put(q["name"].removeprefix("0:/gcodes/"), h._body())
            return h._send(200, {"err": 0})
        if url.path == "/rr_fileinfo":
            info = self.file_info(q.get("name", ""))
            return h._send(200, {"err": 0, **info} if info else {"err": 1})
        h._send(404, {})

    def dsf_routes(self, h, method, url, q):
        if url.path == "/machine/connect":
            if self.brain.secret and q.get("password") != self.brain.secret:
                return h._send(403, {})
            key = uuid.uuid4().hex
            self.sessions.add(key)
            return h._send(200, {"sessionKey": key})
        if not url.path.startswith("/machine/"):
            return h._send(404, {})
        if not self._session_ok(h):
            return h._send(403, {})
        if url.path == "/machine/disconnect":
            self.sessions.discard(h.headers.get("X-Session-Key"))
            return h._send(204)
        if url.path == "/machine/status":
            return h._send(200, self.model())
        if url.path == "/machine/code" and method == "POST":
            self.gcode(h._body().decode())
            return h._send(200, b"", "text/plain")
        if url.path.startswith("/machine/file/") and method == "PUT":
            path = urllib.parse.unquote(url.path.removeprefix("/machine/file/"))
            self.brain.put(path.removeprefix("0:/gcodes/"), h._body())
            return h._send(201)
        if url.path.startswith("/machine/fileinfo/"):
            info = self.file_info(urllib.parse.unquote(url.path.removeprefix("/machine/fileinfo/")))
            return h._send(200, info) if info else h._send(404, {})
        h._send(404, {})

    def close(self):
        self.http.shutdown()


# ---------------------------------------------------------------- FlashForge Adventurer 5M (HTTP 8898, TCP 8899)

FF_STATE = {"idle": "ready", "printing": "printing", "paused": "paused", "cancelled": "cancel",
            "complete": "completed", "error": "error"}
FF_LEGACY = {"idle": "READY", "printing": "BUILDING_FROM_SD", "paused": "PAUSED", "cancelled": "READY",
             "complete": "BUILDING_COMPLETED", "error": "BUSY"}


class FakeFlashForge:
    SERIAL = "SNFAKE5M01"

    def __init__(self, brain: Brain):
        self.brain = brain
        self.upload_headers: list[dict] = []
        self.http = serve_http(brain, self.routes)
        self.port = self.http.server_address[1]
        fake = self

        class Legacy(socketserver.StreamRequestHandler):
            def handle(self):
                while True:
                    line = self.rfile.readline(4096)
                    if not line:
                        return
                    text = line.decode("utf-8", "replace").strip()
                    if not text.startswith("~"):
                        self.wfile.write(b"CMD ? Received.\r\nok\r\n")  # it never speaks HTTP
                        continue
                    self.wfile.write(fake.legacy_reply(text[1:]).encode())

        self.tcp = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Legacy)
        self.tcp.daemon_threads = True
        self.legacy_port = self.tcp.server_address[1]
        threading.Thread(target=self.tcp.serve_forever, daemon=True).start()

    def legacy_reply(self, cmd: str) -> str:
        b = self.brain
        word = cmd.split()[0] if cmd else ""
        busy = b.state in ("printing", "paused")
        body = {
            "M115": "Machine Type: Flashforge Adventurer 5M\r\nMachine Name: fake-5m\r\nFirmware: v3.1.3\r\n"
                    f"SN: {self.SERIAL}\r\nX: 220 Y: 220 Z: 220\r\nTool Count: 1",
            "M119": f"Endstop: X-max:0 Y-max:0 Z-min:0\r\nMachineStatus: {FF_LEGACY[b.state]}\r\nMoveMode: READY\r\n"
                    f"Status: S:0 L:0 J:0 F:0\r\nLED: 1\r\nCurrentFile: {b.current if busy else ''}",
            "M105": "T0:215.0/215.0 T1:0.0/0.0 B:60.0/60.0",
            "M27": "SD printing byte 10/100\r\nLayer: 1/10" if busy else "SD printing byte 0/100\r\nLayer: 0/0",
            "M601": "Control Success.", "M602": "Control Release.",
        }.get(word, "")
        if word in ("M25", "M24", "M26"):
            b.act({"M25": "pause", "M24": "resume", "M26": "cancel"}[word])
        return f"CMD {word} Received.\r\n" + (body + "\r\n" if body else "") + "ok\r\n"

    def detail(self) -> dict:
        b = self.brain
        busy = b.state in ("printing", "paused")
        return {"status": FF_STATE[b.state], "printFileName": b.current if busy else "",
                "printProgress": 0.1 if busy else 0.0, "rightTemp": 215.0, "rightTargetTemp": 215.0,
                "platTemp": 60.0, "platTargetTemp": 60.0, "chamberTemp": 30.0, "printLayer": 1 if busy else 0,
                "targetPrintLayer": 10 if busy else 0, "printDuration": 12, "estimatedTime": 600,
                "cameraStreamUrl": f"http://127.0.0.1:{self.port}/?action=stream", "errorCode": "",
                "firmwareVersion": "3.1.3", "name": "Adventurer 5M"}

    def _creds_ok(self, serial, code) -> bool:
        return serial == self.SERIAL and code == self.brain.secret

    def routes(self, h: _Handler, method: str):
        b = self.brain
        url = urllib.parse.urlsplit(h.path)
        if method == "GET" and url.path == "/" and "action=stream" in url.query:
            return send_mjpeg(h)
        if method != "POST":
            return h._send(404, {})
        if url.path == "/uploadGcode":
            if not self._creds_ok(h.headers.get("serialNumber"), h.headers.get("checkCode")):
                return h._send(200, {"code": 1, "message": "check code error"})
            self.upload_headers.append({k.lower(): v for k, v in h.headers.items()})
            name, data = parse_multipart(h._body(), h.headers["Content-Type"])["gcodeFile"]
            b.put(name, data)
            if h.headers.get("printNow") == "true":
                b.start(name)
            return h._send(200, {"code": 0, "message": "Success"})
        try:
            body = json.loads(h._body() or b"{}")
        except ValueError:
            return h._send(400, {})
        if url.path not in ("/detail", "/control", "/printGcode", "/gcodeList"):
            return h._send(404, {})
        if not self._creds_ok(body.get("serialNumber"), body.get("checkCode")):
            return h._send(200, {"code": 1, "message": "check code error"})
        if url.path == "/detail":
            return h._send(200, {"code": 0, "message": "Success", "detail": self.detail()})
        if url.path == "/gcodeList":
            return h._send(200, {"code": 0, "message": "Success", "gcodeList": list(b.files)})
        if url.path == "/printGcode":
            ok = b.start(str(body.get("fileName", "")))
            return h._send(200, {"code": 0 if ok else 2, "message": "Success" if ok else "busy"})
        action = ((body.get("payload") or {}).get("args") or {}).get("action")
        if action in ("pause", "continue", "cancel"):
            b.act({"pause": "pause", "continue": "resume", "cancel": "cancel"}[action])
            return h._send(200, {"code": 0, "message": "Success"})
        h._send(200, {"code": 3, "message": "unknown"})

    def close(self):
        self.http.shutdown()
        self.tcp.shutdown()
        self.tcp.server_close()


# ---------------------------------------------------------------- Snapmaker 2.0 (HTTP 8080, touchscreen approval)

SNAP_STATE = {"idle": "IDLE", "printing": "RUNNING", "paused": "PAUSED", "cancelled": "STOPPED",
              "complete": "FINISHED", "error": "UNKNOWN"}


class FakeSnapmaker:
    def __init__(self, brain: Brain, approve: bool = True):
        self.brain, self.approve = brain, approve
        self.tokens: dict[str, bool] = {}  # token -> accepted on the touchscreen
        if brain.secret:
            self.tokens[brain.secret] = True
        self.http = serve_http(brain, self.routes)
        self.port = self.http.server_address[1]

    def routes(self, h: _Handler, method: str):
        b = self.brain
        url = urllib.parse.urlsplit(h.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path == "/cam.jpg":
            return h._send(200, JPEG, "image/jpeg")
        if method == "POST" and url.path == "/api/v1/connect":
            token = uuid.uuid4().hex
            self.tokens[token] = False
            return h._send(200, {"token": token, "readonly": False})
        if not url.path.startswith("/api/v1/"):
            return h._send(404, {})
        if method == "POST" and url.path == "/api/v1/upload":
            parts = parse_multipart(h._body(), h.headers["Content-Type"])
            token = parts.get("token", (None, b""))[1].decode()
            if not self.tokens.get(token):
                return h._send(401, {})
            name, data = parts["file"]
            b.put(name, data)
            return h._send(200, {})
        form = q if method == "GET" else _form(h)
        token = form.get("token")
        if token not in self.tokens:
            return h._send(401, {})
        if not self.tokens[token]:
            if self.approve:
                self.tokens[token] = True  # the person taps Yes; this poll still sees "waiting"
            return h._send(204)
        if method == "GET" and url.path == "/api/v1/status":
            busy = b.state in ("printing", "paused")
            return h._send(200, {"status": SNAP_STATE[b.state], "x": 0, "y": 0, "z": 0, "offsetX": 0,
                                 "nozzleTemperature": 215.0, "nozzleTargetTemperature": 215.0,
                                 "heatedBedTemperature": 60.0, "heatedBedTargetTemperature": 60.0,
                                 "isFilamentOut": False, "workSpeed": 1500, "printStatus": "",
                                 "fileName": b.current if busy else "", "progress": 0.1 if busy else 0,
                                 "elapsedTime": 12, "remainingTime": 600, "toolHead": "TOOLHEAD_3DPRINTING_1"})
        acts = {"/api/v1/pause_print": "pause", "/api/v1/resume_print": "resume", "/api/v1/stop_print": "cancel"}
        if method == "POST" and url.path in acts:
            b.act(acts[url.path])
            return h._send(200, {})
        if method == "POST" and url.path == "/api/v1/execute_code":
            if form.get("code", "").strip() == "M112":
                b.act("estop")
            return h._send(200, {})
        h._send(404, {})

    def close(self):
        self.http.shutdown()


# ---------------------------------------------------------------- UltiMaker /api/v1 (digest auth, screen approval)

class FakeUltimaker:
    REALM = "Jedi-API"

    def __init__(self, brain: Brain, approve: bool = True, deny: bool = False):
        self.brain, self.approve, self.deny = brain, approve, deny
        self.creds: dict[str, str] = {}
        self.approved: set[str] = set()
        self.digest_users_seen: list = []
        if brain.secret:
            self.creds["fakeid"] = brain.secret
            self.approved.add("fakeid")
        self.http = serve_http(brain, self.routes)
        self.port = self.http.server_address[1]

    def users(self) -> dict:
        return {i: k for i, k in self.creds.items() if i in self.approved}

    def job(self) -> dict | None:
        b = self.brain
        js = {"printing": "printing", "paused": "paused", "cancelled": "wait_cleanup", "complete": "wait_cleanup"}
        if b.state not in js or not b.current:
            return None
        return {"name": b.current, "state": js[b.state], "progress": 0.1, "time_elapsed": 12, "time_total": 612,
                "result": "Aborted" if b.state == "cancelled" else ("Finished" if b.state == "complete" else "")}

    def routes(self, h: _Handler, method: str):
        b = self.brain
        url = urllib.parse.urlsplit(h.path)
        if url.path == "/" and "action=snapshot" in url.query:
            return h._send(200, JPEG, "image/jpeg")
        if method == "GET" and url.path == "/api/v1/system":
            return h._send(200, {"guid": "fake-guid-0001", "variant": "Ultimaker S5", "firmware": "5.8.1",
                                 "name": "fake-s5", "platform": "Linux"})
        if method == "POST" and url.path == "/api/v1/auth/request":
            ident, key = uuid.uuid4().hex, uuid.uuid4().hex
            self.creds[ident] = key
            return h._send(200, {"id": ident, "key": key})
        if method == "GET" and url.path.startswith("/api/v1/auth/check/"):
            ident = url.path.rsplit("/", 1)[1]
            if self.deny:
                return h._send(200, {"message": "unauthorized"})
            if ident in self.approved:
                return h._send(200, {"message": "authorized"})
            if self.approve and ident in self.creds:
                self.approved.add(ident)  # the person taps Allow; this poll still says "unknown"
            return h._send(200, {"message": "unknown"})
        if method == "GET" and url.path == "/api/v1/printer":
            return h._send(200, {"status": "error" if b.state == "error" else
                                 ("printing" if b.state in ("printing", "paused") else "idle"),
                                 "heads": [{"extruders": [{"hotend": {"temperature": {"current": 215.0, "target": 215.0}}}]}],
                                 "bed": {"temperature": {"current": 60.0, "target": 60.0}}})
        if method == "GET" and url.path == "/api/v1/print_job":
            job = self.job()
            return h._send(200, job) if job else h._send(404, {"message": "No print job"})
        needs_auth = url.path == "/api/v1/auth/verify" or method in ("PUT", "POST")
        if needs_auth and not digest_ok(h, self.users(), self.REALM):
            return digest_challenge(h, self.REALM)
        if url.path == "/api/v1/auth/verify":
            return h._send(200, {"message": "ok"})
        if method == "PUT" and url.path == "/api/v1/print_job/state":
            target = json.loads(h._body() or b"{}").get("target")
            b.act({"pause": "pause", "print": "resume", "abort": "cancel"}.get(target, "unknown"))
            return h._send(204)
        if method == "POST" and url.path == "/api/v1/print_job":
            parts = parse_multipart(h._body(), h.headers["Content-Type"])
            name, data = parts["file"]
            b.put(name, data)
            b.start(name)  # the real printer starts at once, which is why Gantrel never calls this
            return h._send(201, {})
        h._send(404, {})

    def close(self):
        self.http.shutdown()


# ---------------------------------------------------------------- Repetier-Server (REST, x-api-key)

class FakeRepetier:
    SLUG = "Fake_Printer"
    GLOBAL_KEY = "repetier-global-key-from-info"

    def __init__(self, brain: Brain, printers: int = 1):
        self.brain, self.printers = brain, printers
        self.ids: dict[str, int] = {}
        self.sent: list[str] = []
        self.http = serve_http(brain, self.routes)
        self.port = self.http.server_address[1]

    def _printer_list(self) -> list:
        b = self.brain
        busy = b.state in ("printing", "paused")
        me = {"slug": self.SLUG, "name": "Fake Printer", "online": 1, "active": True,
              "job": b.current if busy else "none", "paused": b.state == "paused", "done": 10.0 if busy else 0.0,
              "printedTimeComp": 12}
        others = [{"slug": f"Other_{i}", "name": f"Other {i}", "online": 1, "active": True, "job": "none"}
                  for i in range(1, self.printers)]
        return [me, *others]

    def _id(self, name: str) -> int:
        return self.ids.setdefault(name, len(self.ids) + 1)

    def routes(self, h: _Handler, method: str):
        b = self.brain
        url = urllib.parse.urlsplit(h.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path == "/printer/info":
            # The real server's info call carries the global API key; adapters must never keep it.
            return h._send(200, {"name": "Repetier-Server", "version": "1.4.10", "apikey": self.GLOBAL_KEY,
                                 "printers": [{"slug": p["slug"], "name": p["name"]} for p in self._printer_list()]})
        if url.path == "/fake/cam.jpg":
            return h._send(200, JPEG, "image/jpeg")
        if h.headers.get("x-api-key") != b.secret:
            return h._send(200, {"error": "Authorization required"})
        if url.path == "/printer/list":
            return h._send(200, {"data": self._printer_list()})
        if url.path == f"/printer/model/{self.SLUG}" and method == "POST" and q.get("a") == "upload":
            _, data = parse_multipart(h._body(), h.headers["Content-Type"])["filename"]
            b.put(q["name"], data)
            self._id(q["name"])
            return h._send(200, {})
        if url.path != f"/printer/api/{self.SLUG}":
            return h._send(404, {"error": "unknown printer"})
        a, data = q.get("a"), json.loads(q.get("data") or "{}")
        if a == "stateList":
            return h._send(200, {self.SLUG: {"extruder": [{"tempRead": 215.0, "tempSet": 215.0}],
                                             "heatedBeds": [{"tempRead": 60.0, "tempSet": 60.0}],
                                             "layer": 1 if b.state in ("printing", "paused") else 0}})
        if a == "listModels":
            return h._send(200, {"data": [{"id": self._id(n), "name": n, "length": len(d),
                                           "created": int(b.mtimes[n] * 1000)} for n, d in b.files.items()]})
        if a == "copyModel":
            name = next((n for n, i in self.ids.items() if i == data.get("id")), "")
            ok = data.get("autostart") and b.start(name)
            return h._send(200, {} if ok else {"error": "printer busy"})
        if a == "continueJob":
            b.act("resume")
            return h._send(200, {})
        if a == "stopJob":
            b.act("cancel")
            return h._send(200, {})
        if a == "send":
            cmd = str(data.get("cmd", ""))
            self.sent.append(cmd)
            if cmd == "@pause":
                b.act("pause")
            elif cmd == "M112":
                b.act("estop")
            return h._send(200, {})
        if a == "getPrinterConfig":
            return h._send(200, {"general": {"name": "Fake Printer"},
                                 "webcam": {"staticUrl": "/fake/cam.jpg", "dynamicUrl": "/fake/stream"}})
        h._send(200, {"error": f"unknown action {a}"})

    def close(self):
        self.http.shutdown()


# ---------------------------------------------------------------- Marlin on USB serial (a pty pair)

def marlin_checksum(line: str) -> int:
    cs = 0
    for c in line.encode():
        cs ^= c
    return cs


class FakeMarlin:
    """Marlin 2.1 on a pseudo terminal. Line numbers and checksums, SD card commands, M112 halt."""

    def __init__(self, brain: Brain, sd: bool = True, corrupt_line: int | None = None):
        self.brain, self.sd, self.corrupt_line = brain, sd, corrupt_line
        # heating: inside a blocking M109/M190. Commands queue with no ok, and the board prints only
        # busy and temperature lines, except M112, which the emergency parser runs at once.
        self.heating = False
        self.queued: list[str] = []
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)
        self.path = os.ttyname(self.slave)
        self.line_no = 0
        self.saving: tuple[str, bytearray] | None = None
        self.selected = ""
        self.halted = False
        self.resends = 0
        self.received: list[str] = []
        self.stop = False
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._busy_loop, daemon=True).start()

    def _busy_loop(self):
        while not self.stop:
            time.sleep(0.3)
            if self.heating and not self.halted:
                self.say("echo:busy: processing", " T:180.00 /215.00 B:60.00 /60.00 @:127 B@:0")

    def finish_heating(self) -> None:
        self.heating = False
        queued, self.queued = self.queued, []
        for line in queued:
            self.handle(line)

    def say(self, *lines: str) -> None:
        os.write(self.master, "".join(f"{ln}\n" for ln in lines).encode())

    def _loop(self):
        buf = b""
        while not self.stop:
            try:
                r, _, _ = select.select([self.master], [], [], 0.1)
                if not r:
                    continue
                data = os.read(self.master, 4096)
            except OSError:
                return
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self.handle(line.decode("utf-8", "replace").strip())

    def handle(self, line: str) -> None:
        if not line:
            return
        self.received.append(line)
        if self.halted:
            return self.say("Error:Printer halted. kill() called!")
        if line.split()[0].upper() == "M112":
            self.halted = True
            self.brain.act("estop")
            return self.say("Error:Printer halted. kill() called!")
        if self.heating:
            self.received.pop()
            self.queued.append(line)
            return
        m = re.match(r"N(\d+)\s+(.*)\*(\d+)$", line)
        if m:
            n, cmd = int(m.group(1)), m.group(2)
            corrupt = self.corrupt_line == n
            if corrupt:
                self.corrupt_line = None  # one bad line on the wire, once
            if corrupt or marlin_checksum(line[:line.rindex("*")]) != int(m.group(3)) or n != self.line_no + 1:
                self.resends += 1
                return self.say(f"Error:checksum mismatch, Last Line: {self.line_no}", f"Resend: {self.line_no + 1}",
                                "ok")
            self.line_no = n
            line = cmd.strip()
        if self.saving is not None:
            name, data = self.saving
            if line.split()[0].upper() == "M29":
                self.saving = None
                self.brain.put(name, bytes(data))
                return self.say("Done saving file.", "ok")
            data += (line + "\r\n").encode()
            return self.say("ok")
        self.command(line)

    def command(self, line: str) -> None:
        b = self.brain
        word, _, arg = line.partition(" ")
        word = word.upper()
        busy = b.state in ("printing", "paused")
        sd_cmds = ("M20", "M21", "M23", "M24", "M25", "M27", "M28", "M524")
        if word in sd_cmds and not self.sd:
            return self.say(f'echo:Unknown command: "{line}"', "ok")
        if word == "M105":
            return self.say("ok T:215.00 /215.00 B:60.00 /60.00 @:0 B@:0")
        if word == "M115":
            return self.say("FIRMWARE_NAME:Marlin 2.1.2.1 (Sep 12 2023 12:00:00) SOURCE_CODE_URL:github.com/MarlinFirmware/"
                            "Marlin PROTOCOL_VERSION:1.0 MACHINE_TYPE:Fake Ender-3 EXTRUDER_COUNT:1 UUID:cede2a2f",
                            "Cap:SERIAL_XON_XOFF:0", f"Cap:SDCARD:{1 if self.sd else 0}", "Cap:AUTOREPORT_TEMP:1", "ok")
        if word == "M110":
            self.line_no = int(arg.strip().lstrip("N") or 0)
            return self.say("ok")
        if word in ("M21", "M28") and busy:
            b.act("cancel")  # real Marlin: remounting or writing the card mid-print kills the print
            self.selected = ""
        if word == "M21":
            return self.say("echo:SD card ok", "ok")
        if word == "M20":
            return self.say("Begin file list", *[f"{n} {len(d)}" for n, d in b.files.items()], "End file list", "ok")
        if word == "M28":
            self.saving = (arg.strip().upper(), bytearray())
            return self.say(f"Writing to file: {arg.strip()}", "ok")
        if word == "M23":
            name = arg.strip().upper()
            if name not in b.files:
                return self.say(f"open failed, File: {name}.", "ok")
            self.selected = name
            return self.say(f"echo:Now fresh file: {name}", f"File opened: {name} Size: {len(b.files[name])}",
                            "File selected", "ok")
        if word == "M24":
            if b.state == "paused":
                b.act("resume")
            elif self.selected:
                b.start(self.selected)
            return self.say("ok")
        if word == "M25":
            b.act("pause")
            return self.say("ok")
        if word == "M27" and arg.strip().upper() == "C":
            return self.say(f"Current file: {b.current} {b.current}" if busy else "(no file)", "ok")
        if word == "M27":
            # Marlin 2.1: paused still reports "SD printing byte", since the file stays open.
            return self.say("SD printing byte 10/100" if busy else "Not SD printing", "ok")
        if word == "M524":
            b.act("cancel")
            self.selected = ""
            return self.say("ok")
        if word == "M112":
            b.act("estop")
            self.halted = True
            return self.say("Error:Printer halted. kill() called!")
        self.say(f'echo:Unknown command: "{line}"', "ok")

    def close(self):
        self.stop = True
        time.sleep(0.15)
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


# ---------------------------------------------------------------- watch: camera, vision model, notice target


class FakeFrameCamera:
    """A camera at /cam.jpg serving the given image files in turn; the last one repeats."""

    def __init__(self, paths):
        self.frames = [open(p, "rb").read() for p in paths]
        self.served = 0
        cam = self

        def routes(h, method):
            if urllib.parse.urlsplit(h.path).path != "/cam.jpg":
                return h._send(404, {})
            data = cam.frames[min(cam.served, len(cam.frames) - 1)]
            cam.served += 1
            return h._send(200, data, "image/jpeg")

        self.srv = serve_http(Brain(), routes)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/cam.jpg"

    def close(self):
        self.srv.shutdown()


FONSVIVA_TEST_KEY = "fv-test-app-key-0123456789"


class FakeVision:
    """Stands in for Fonsviva's OpenAI-compatible /v1 API, answering from a script of reply texts.

    Every request must carry `Authorization: Bearer <key>` (Gantrel's Fonsviva app key), or it gets
    401, as Fonsviva answers an unknown app; key=None accepts any request. Replies carry an
    X-Fonsviva-Machine header and a served model name, so tests can check where a verdict says it ran.
    The last reply repeats. reject_json_mode answers 400 to a request carrying response_format, as
    LM Studio does for json_object.
    """

    def __init__(self, replies, reject_json_mode=False, key: str | None = FONSVIVA_TEST_KEY):
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.auth: list[str] = []
        self.reject_json_mode = reject_json_mode
        self.key = key
        fv = self

        def routes(h, method):
            fv.auth.append(h.headers.get("Authorization", ""))
            if fv.key and h.headers.get("Authorization") != "Bearer " + fv.key:
                return h._send(401, {"error": {"message": "unknown app key"}})
            if method == "GET" and urllib.parse.urlsplit(h.path).path == "/v1/models":
                return h._send(200, {"object": "list", "data": [{"id": "fake-vl", "object": "model"}]})
            if method != "POST" or urllib.parse.urlsplit(h.path).path != "/v1/chat/completions":
                return h._send(404, {})
            body = json.loads(h._body())
            if fv.reject_json_mode and "response_format" in body:
                return h._send(400, {"error": "'response_format.type' must be 'json_schema' or 'text'"})
            fv.requests.append(body)
            text = fv.replies[min(len(fv.requests) - 1, len(fv.replies) - 1)]
            return h._send(200, {"id": "x", "object": "chat.completion", "model": f"served/{body.get('model')}",
                                 "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                              "finish_reason": "stop"}]},
                           headers={"X-Fonsviva-Machine": "fake-fonsviva-host"})

        self.srv = serve_http(Brain(), routes)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"

    def images(self) -> list[bytes]:
        import base64
        out = []
        for r in self.requests:
            for part in r["messages"][0]["content"]:
                if part["type"] == "image_url":
                    out.append(base64.b64decode(part["image_url"]["url"].split(",", 1)[1]))
        return out

    def close(self):
        self.srv.shutdown()


class FakeHook:
    """A notice target (ntfy topic or JSON webhook) that records every request and answers "ok".

    status other than 200 answers that code with the request path echoed in the body, as many servers
    do on an error page.
    """

    def __init__(self, status: int = 200):
        self.got: list[dict] = []
        self.port = 0
        fh = self

        def routes(h, method):
            fh.got.append({"method": method, "path": h.path, "headers": dict(h.headers), "body": h._body()})
            if status != 200:
                return h._send(status, f"no route for {h.path}".encode(), "text/plain")
            return h._send(200, b"ok", "text/plain")

        self.srv = serve_http(Brain(), routes)
        self.port = self.srv.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.srv.shutdown()
