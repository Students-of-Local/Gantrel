"""Bambu Lab printers in LAN mode: MQTT over TLS on 8883, implicit FTPS on 990, camera on 6000 or 322.

Protocol references: ha-bambulab (MIT, https://github.com/greghesp/ha-bambulab) and bambulabs_api
(MIT, https://github.com/acse-ci223/bambulabs_api). Written from those notes, not copied.

The user is always "bblp" and the password is the printer's LAN access code (kept in the keyring).
Bambu printers use a certificate from Bambu's own CA, so this pins the certificate fingerprint the
first time it pairs (trust on first use) and refuses to send the access code if it ever changes.

Newer firmware only allows third-party control in LAN-only mode with Developer Mode on. When the
printer refuses, the error says so.
"""

from __future__ import annotations

import ftplib
import hashlib
import json
import socket
import ssl
import struct
import threading
import time
from pathlib import Path

from ..errors import AuthFailed, ProtocolError, Refused, Unreachable, Unsupported
from ..model import Camera, Status
from .base import Adapter, write_image

STATE_MAP = {"IDLE": "idle", "RUNNING": "printing", "PREPARE": "printing", "SLICING": "printing",
             "PAUSE": "paused", "FINISH": "complete", "FAILED": "error"}
USER = "bblp"
RTSP_MODELS = ("bambu-x1", "bambu-x1c", "bambu-x1e", "bambu-h2d", "bambu-h2s", "bambu-p2s")
# print_error codes Bambu firmware sets when a person stops a print (screen, app or a stop command).
# From ha-bambulab's error table (MIT): 0300400C "The task was canceled", 0500400E "Printing was cancelled".
USER_CANCEL_ERRORS = (0x0300400C, 0x0500400E)
DEV_MODE_HINT = ("On newer firmware turn on LAN-only mode and Developer Mode on the printer, then pair again "
                 "with the access code shown there")


class _PinMismatch(ConnectionError):
    """The certificate on this connection is not the pinned one. Nothing secret was sent."""

    def __init__(self, got: str):
        super().__init__("TLS certificate does not match the pin")
        self.got = got


class _PinnedSocket(ssl.SSLSocket):
    """Checks the peer certificate against the context's pin as part of every handshake.

    Both wrap_socket(do_handshake_on_connect=True) and an explicit do_handshake() (paho, ftplib's
    data channel) come through here, so the check runs on the very connection that then carries the
    access code or a command, before either is sent.
    """

    def do_handshake(self, *args, **kwargs):
        super().do_handshake(*args, **kwargs)
        fp = hashlib.sha256(self.getpeercert(binary_form=True) or b"").hexdigest()
        ctx = self.context
        ctx.seen = fp
        if ctx.pin and fp != ctx.pin:
            raise _PinMismatch(fp)


class _PinnedContext(ssl.SSLContext):
    sslsocket_class = _PinnedSocket
    pin: str = ""
    seen: str = ""


def _pinned_ctx(pin: str) -> _PinnedContext:
    ctx = _PinnedContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # Bambu's own CA; identity is the pinned fingerprint instead
    ctx.pin = pin
    return ctx


class _ImplicitFTPS(ftplib.FTP_TLS):
    """FTPS with TLS from the first byte (port 990) and TLS session reuse on the data channel."""

    def connect(self, host="", port=0, timeout=-999, source_address=None):
        self.host, self.port = host, port
        if timeout != -999:
            self.timeout = timeout
        raw = socket.create_connection((host, port), self.timeout)
        self.af = raw.family
        self.sock = self.context.wrap_socket(raw, server_hostname=host)
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


class BambuAdapter(Adapter):
    type_name = "bambu"
    label = "Bambu Lab (LAN mode)"
    default_port = 8883
    needs_secret = True
    secret_label = "LAN access code (printer screen: Settings, Network)"

    @property
    def ftp_port(self) -> int:
        return int(self.cfg.extra.get("ftp_port", 990))

    @property
    def camera_port(self) -> int:
        return int(self.cfg.extra.get("camera_port", 6000))

    def _require(self):
        if not self.cfg.serial:
            raise ProtocolError(f"{self.cfg.name} has no serial number",
                                "Pair again with --serial (printer screen: Settings, Device)")
        if not self.secret:
            raise AuthFailed(f"{self.cfg.name} has no access code in the keyring", "Pair it again")

    def _pin_for(self, port: int) -> str:
        if port == self.port:
            return self.cfg.tls_fingerprint
        return self.cfg.extra.get("tls_pins", {}).get(str(port), "")

    def _ctx(self, port: int) -> _PinnedContext:
        pin = self._pin_for(port)
        if not pin:
            # Never send the access code to a service whose certificate wasn't pinned at pair.
            raise AuthFailed(f"{self.cfg.name} has no pinned TLS certificate for port {port}",
                             "Unpair and pair it again so every service's certificate is pinned")
        return _pinned_ctx(pin)

    def _handshake_fp(self, port: int) -> str:
        """TLS handshake only, no credentials sent: the certificate fingerprint a service shows."""
        ctx = _pinned_ctx("")
        try:
            with socket.create_connection((self.cfg.host, port), timeout=5) as raw:
                with ctx.wrap_socket(raw, server_hostname=self.cfg.host):
                    pass
        except (OSError, ssl.SSLError) as err:
            raise Unreachable(f"no TLS answer from {self.cfg.host}:{port} ({err.__class__.__name__})",
                              "Check the printer is on and in LAN mode") from None
        return ctx.seen

    def _pin_error(self, port: int) -> AuthFailed:
        return AuthFailed(f"{self.cfg.name}'s TLS certificate on port {port} changed",
                          "The access code was not sent. If you replaced the printer or its firmware reset, "
                          "unpair and pair it again; otherwise something else may be answering at that address")

    # ------------------------------------------------------------ MQTT

    def _mqtt(self, publish: list[dict], want_report: bool, wait: float = 6.0) -> dict:
        import paho.mqtt.client as mqtt

        self._require()
        ctx = self._ctx(self.port)
        report_topic = f"device/{self.cfg.serial}/report"
        req_topic = f"device/{self.cfg.serial}/request"
        connected, got = threading.Event(), threading.Event()
        state: dict = {"rc": None, "print": {}}

        def on_connect(client, userdata, flags, reason_code, properties):
            state["rc"] = reason_code
            connected.set()

        def on_message(client, userdata, msg):
            try:
                data = json.loads(msg.payload)
            except ValueError:
                return
            p = data.get("print") if isinstance(data, dict) else None
            if isinstance(p, dict):
                state["print"].update(p)
                if "gcode_state" in state["print"]:
                    got.set()

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"gantrel-{time.time_ns() % 10**9}",
                        protocol=mqtt.MQTTv311)
        c.username_pw_set(USER, self.secret)
        c.tls_set_context(ctx)
        c.on_connect, c.on_message = on_connect, on_message
        try:
            c.connect(self.cfg.host, self.port, keepalive=30)
        except _PinMismatch:
            raise self._pin_error(self.port) from None
        except (OSError, ssl.SSLError) as err:
            raise Unreachable(f"{self.cfg.name} did not answer on MQTT ({err.__class__.__name__})",
                              "Check the printer is on and in LAN mode") from None
        c.loop_start()
        try:
            if not connected.wait(10):
                raise Unreachable(f"{self.cfg.name} did not finish the MQTT login")
            if state["rc"] is None or state["rc"].is_failure:
                raise AuthFailed(f"{self.cfg.name} refused the access code ({state['rc']})", DEV_MODE_HINT)
            if want_report:
                c.subscribe(report_topic, qos=0)
            for msg in publish:
                c.publish(req_topic, json.dumps(msg), qos=0).wait_for_publish(5)
            if want_report and not got.wait(wait):
                raise ProtocolError(f"{self.cfg.name} sent no status report", DEV_MODE_HINT)
            if not want_report:
                time.sleep(0.2)
        finally:
            c.loop_stop()
            c.disconnect()
        return state["print"]

    def _command(self, cmd: dict) -> None:
        self._mqtt([{"print": {"sequence_id": str(int(time.time()) % 100000), **cmd}}], want_report=False)

    # ------------------------------------------------------------ contract

    @classmethod
    def probe(cls, host, port=None, timeout=2.0):
        """TLS MQTT that refuses an anonymous login looks like a Bambu printer. No credentials are sent."""
        port = port or cls.default_port
        try:
            with socket.create_connection((host, port), timeout=timeout) as raw:
                with _pinned_ctx("").wrap_socket(raw, server_hostname=host) as tls:
                    # MQTT 3.1.1 CONNECT, clean session, client id "gantrel-probe", no credentials.
                    cid = b"gantrel-probe"
                    var = b"\x00\x04MQTT\x04\x02\x00\x1e" + struct.pack(">H", len(cid)) + cid
                    tls.sendall(bytes([0x10, len(var)]) + var)
                    reply = tls.recv(4)
        except (OSError, ssl.SSLError):
            return None
        if len(reply) == 4 and reply[0] == 0x20:
            return {"type": cls.type_name, "host": host, "port": port,
                    "detail": "MQTT over TLS (likely Bambu Lab in LAN mode); pair needs --serial and the access code"}
        return None

    def pair(self):
        self._require()
        # Pin every service's certificate first, over connections that send no credentials. Each later
        # connection that carries the access code must show exactly its pinned certificate.
        self.cfg.tls_fingerprint = self._handshake_fp(self.port)
        pins = {str(self.port): self.cfg.tls_fingerprint, str(self.ftp_port): self._handshake_fp(self.ftp_port)}
        if self.cfg.model not in RTSP_MODELS:
            try:
                pins[str(self.camera_port)] = self._handshake_fp(self.camera_port)
            except Unreachable:
                pass  # no LAN liveview on 6000; snapshot will say to pair again once it is on
        self.cfg.extra["tls_pins"] = pins
        st = self.status()
        return {"state": st.state, "tls_fingerprint": self.cfg.tls_fingerprint[:16] + "..."}

    def status(self) -> Status:
        p = self._mqtt([{"pushing": {"sequence_id": "0", "command": "pushall"}}], want_report=True)
        state = _state(p)
        err = p.get("print_error") or 0
        message = ""
        if state == "cancelled":
            message = "stopped by a person"
        elif err:
            message = f"print_error {err}"
        elif state == "error" and _hms_errors(p):
            message = "HMS " + ", ".join(_hms_errors(p))
        rem = p.get("mc_remaining_time")
        return Status(
            printer=self.cfg.name, type=self.type_name, state=state,
            file=str(p.get("subtask_name") or p.get("gcode_file") or ""),
            progress=p["mc_percent"] / 100 if isinstance(p.get("mc_percent"), (int, float)) else None,
            nozzle_c=p.get("nozzle_temper"), nozzle_target_c=p.get("nozzle_target_temper"),
            bed_c=p.get("bed_temper"), bed_target_c=p.get("bed_target_temper"), chamber_c=p.get("chamber_temper"),
            remaining_s=rem * 60 if isinstance(rem, (int, float)) else None,
            layer=p.get("layer_num"), total_layers=p.get("total_layer_num"),
            message=message,
        )

    def _ftp(self, work):
        self._require()
        ctx = self._ctx(self.ftp_port)
        ftp = _ImplicitFTPS(context=ctx, timeout=30)
        try:
            ftp.connect(self.cfg.host, self.ftp_port)
            ftp.login(USER, self.secret)
            ftp.prot_p()
            out = work(ftp)
            ftp.quit()
        except _PinMismatch:
            raise self._pin_error(self.ftp_port) from None
        except ftplib.error_perm as err:
            if str(err).startswith("530"):
                raise AuthFailed(f"{self.cfg.name} refused the access code for file transfer", DEV_MODE_HINT) from None
            raise Refused(f"{self.cfg.name} refused it ({str(err)[:80]})") from None
        except (OSError, ssl.SSLError, ftplib.Error) as err:
            raise Unreachable(f"file transfer with {self.cfg.name} failed ({err.__class__.__name__})",
                              "Check the printer has an SD card or storage inserted") from None
        return out

    def upload(self, local_path: Path, remote_name: str) -> str:
        def work(ftp):
            with open(local_path, "rb") as fh:
                ftp.storbinary(f"STOR {remote_name}", fh)
        self._ftp(work)
        return remote_name

    def remote_info(self, remote_name: str) -> dict | None:
        def work(ftp):
            try:
                size = ftp.size(remote_name)
            except ftplib.error_perm:
                return None
            try:
                mtime = ftp.sendcmd(f"MDTM {remote_name}").split()[-1]
            except ftplib.error_perm:
                mtime = None
            return {"size": size, "mtime": mtime}
        return self._ftp(work)

    def _start(self, remote_name: str) -> None:
        if remote_name.lower().endswith(".3mf"):
            self._command({"command": "project_file", "param": "Metadata/plate_1.gcode",
                           "url": f"ftp:///{remote_name}", "subtask_name": remote_name, "use_ams": False,
                           "timelapse": False, "bed_leveling": True, "flow_cali": False, "vibration_cali": False,
                           "layer_inspect": False})
        else:
            self._command({"command": "gcode_file", "param": f"/sdcard/{remote_name}"})

    def _resume(self) -> None:
        self._command({"command": "resume"})

    def pause(self) -> None:
        self._command({"command": "pause"})

    def cancel(self) -> None:
        self._command({"command": "stop"})

    def emergency_stop(self) -> str:
        self._command({"command": "stop"})
        self._command({"command": "gcode_line", "param": "M104 S0\nM140 S0\n"})
        return ("Bambu printers have no network emergency stop. Sent stop and turned the nozzle and bed heaters "
                "off. For a true emergency stop switch the printer off at its power switch")

    def start_by_hand(self, remote_name: str = "") -> str:
        what = f"Start {remote_name}" if remote_name else "Start or resume it"
        return (f"{what} on the printer's screen (Print files), or from Bambu Studio's Device tab on the same "
                "network.")

    def camera(self) -> Camera:
        if self.cfg.camera_url:
            return Camera(self.cfg.camera_url, self.cfg.stream_url)
        if self.cfg.model in RTSP_MODELS:
            # The URL needs user bblp and the access code as password; it is never written out with them.
            return Camera("", self.cfg.stream_url or f"rtsps://{self.cfg.host}:322/streaming/live/1")
        return Camera(f"bambu-jpeg://{self.cfg.host}:{self.camera_port}", self.cfg.stream_url)

    def snapshot(self, out: Path) -> Path:
        cam = self.camera()
        if not cam.snapshot_url.startswith("bambu-jpeg://"):
            if cam.snapshot_url:
                return super().snapshot(out)
            raise Unsupported(f"{self.cfg.name} streams video over RTSPS only, so there is no direct snapshot",
                              "Turn on LAN Mode Liveview on the printer, relay it with go2rtc, and pair with "
                              "--camera-url set to go2rtc's frame.jpeg URL")
        self._require()
        ctx = self._ctx(self.camera_port)
        auth = struct.pack("<IIII", 0x40, 0x3000, 0, 0) + USER.encode().ljust(32, b"\0") \
            + self.secret.encode().ljust(32, b"\0")
        try:
            with socket.create_connection((self.cfg.host, self.camera_port), timeout=10) as raw:
                with ctx.wrap_socket(raw, server_hostname=self.cfg.host) as tls:
                    tls.sendall(auth)  # only after the handshake matched the pin
                    header = _recv_exact(tls, 16)
                    size = struct.unpack("<I", header[:4])[0]
                    if not 0 < size < 8 * 1024 * 1024:
                        raise ProtocolError(f"{self.cfg.name}'s camera sent a bad frame header")
                    data = _recv_exact(tls, size)
        except _PinMismatch:
            raise self._pin_error(self.camera_port) from None
        except (OSError, ssl.SSLError) as err:
            raise Unreachable(f"{self.cfg.name}'s camera did not answer ({err.__class__.__name__})",
                              "Check LAN Mode Liveview is on") from None
        return write_image(data, out)


def _hms_errors(p: dict) -> list[str]:
    """HMS entries the report lists, as attr-code hex. Any entry here means the printer flagged a fault."""
    out = []
    for h in p.get("hms") or []:
        if isinstance(h, dict):
            out.append(f"{int(h.get('attr', 0)):08X}-{int(h.get('code', 0)):08X}")
    return out


def _state(p: dict) -> str:
    """Bambu keeps gcode_state FAILED after a person stops a print, until the next print starts.

    That is a cancel, not a failure: FAILED with print_error 0 or a user cancel code, and no HMS fault,
    is reported as cancelled. A nonzero print_error or any HMS entry stays an error.
    """
    state = STATE_MAP.get(str(p.get("gcode_state", "")), "busy")
    err = p.get("print_error") or 0
    if state == "error" and err in (0, *USER_CANCEL_ERRORS) and not _hms_errors(p):
        return "cancelled"
    if err and err not in USER_CANCEL_ERRORS and state not in ("printing", "paused"):
        return "error"
    return state


def _recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ProtocolError("the camera closed the connection early")
        buf += chunk
    return bytes(buf)
