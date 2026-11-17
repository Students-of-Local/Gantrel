"""Fake printers for the contract suite: one shared printer state, one front end per protocol.

Each front end speaks its protocol over real sockets on 127.0.0.1 (HTTP, WebSocket, MQTT over TLS,
implicit FTPS, the Bambu JPEG camera socket), so adapters are tested through their real network code.
"""

from __future__ import annotations

import asyncio
import email.parser
import email.policy
import json
import socket
import ssl
import struct
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 64 + b"\xff\xd9"


class Brain:
    """The printer: what it holds and what it was told to do."""

    def __init__(self, secret: str | None = None):
        self.secret = secret
        self.state = "idle"  # idle printing paused complete cancelled error
        self.running_macro = False  # Klipper busy outside a print job (Moonraker fake only)
        self.files: dict[str, bytes] = {}
        self.current = ""
        self.events: list[str] = []
        self.lock = threading.Lock()

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

    def _send(self, code: int, data=None, ctype="application/json"):
        body = b"" if data is None else (data if isinstance(data, bytes) else json.dumps(data).encode())
        self.send_response(code)
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


def serve_http(brain: Brain, routes) -> ThreadingHTTPServer:
    handler = type("H", (_Handler,), {"brain": brain, "routes": staticmethod(routes)})
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
                                                    "temperature_sensor chamber_temp", "webhooks"]}})
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
    if method == "POST" and url.path == "/server/files/upload":
        parts = parse_multipart(h._body(), h.headers["Content-Type"])
        name, data = parts["file"]
        b.files[name] = data
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
        b.files[name] = data
        if parts.get("print", (None, b"false"))[1] == b"true":
            b.start(name)  # what real OctoPrint does; the contract test must never see this
        return h._send(201, {"done": True, "files": {"local": {"name": name}}})
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
    if method == "GET" and url.path == "/api/version":
        return h._send(200, {"api": "2.0.0", "server": "2.1.2", "text": "PrusaLink"})
    if not h._authed():
        return h._send(401, {"title": "Unauthorized"})
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
        if method == "PUT":
            b.files[name] = h._body()
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
            h.brain.files[name] = data
            return h._send(200, {"code": 200})
        if method == "GET" and url.path == "/":
            return h._send(200, JPEG, "image/jpeg")
        h._send(404, {})

    def close(self):
        self.http.shutdown()
        self.loop.call_soon_threadsafe(self.server.close)


# ---------------------------------------------------------------- Bambu Lab (MQTT TLS, FTPS, camera)

BAMBU_STATE = {"idle": "IDLE", "printing": "RUNNING", "paused": "PAUSE", "complete": "FINISH",
               "cancelled": "IDLE", "error": "FAILED"}


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
    def __init__(self, brain: Brain, serial: str, cert: str, key: str):
        self.brain, self.serial = brain, serial
        self.ctx = _tls_server_ctx(cert, key)
        self.ignored: list[dict] = []
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
        try:
            with self.ctx.wrap_socket(raw, server_side=True) as tls:
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
                          "print_error": 0, "command": "push_status"}}

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
                self.brain.files[arg] = buf
                say("226 done")
            elif cmd == "QUIT":
                say("221 bye")
                return
            else:
                say("502 not implemented")

    def _cam_client(self, s):
        auth = _recv_exact(s, 80)
        user = auth[16:48].rstrip(b"\0").decode()
        code = auth[48:80].rstrip(b"\0").decode()
        if user != "bblp" or code != self.brain.secret:
            return
        s.sendall(struct.pack("<IIII", len(JPEG), 0, 1, 0) + JPEG)
