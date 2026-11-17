"""Marlin printers on a USB serial port, through pyserial (BSD-3-Clause).

The printer's "host" is the device path (/dev/ttyUSB0, /dev/ttyACM0, or a /dev/serial/by-id/ link)
and its "port" is the baud rate. Status comes from M105 (temperatures) and M27 (SD progress).

Uploads go to the printer's SD card with M28/M29, only when the board reports SDCARD:1 in M115, under
an 8.3 name made from the content hash. Gantrel never streams a print from the host, so a board
without SD support gets a clear refusal instead. Marlin saves each line without its comment, so the
copy on the card is the G-code with comments and blank lines removed.

Opening the port can restart boards with Arduino style auto-reset, which would stop a print that is
running. This adapter turns off hang-up-on-close (HUPCL) so later opens leave the board alone; pair
while the printer is idle.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from .. import config
from ..errors import ProtocolError, Refused, Unreachable, Unsupported
from ..model import Camera, Status
from .base import Adapter

DEFAULT_BAUD = 115200
TEMP_RE = re.compile(r"\bT0?:\s*(-?[\d.]+)\s*/\s*(-?[\d.]+)")
BED_RE = re.compile(r"\bB:\s*(-?[\d.]+)\s*/\s*(-?[\d.]+)")
CHAMBER_RE = re.compile(r"\bC:\s*(-?[\d.]+)\s*/\s*(-?[\d.]+)")
ALIVE_TEMP_RE = re.compile(r"\s*T\d?:\s*-?\d")  # temperature report or autoreport line
HEAT_WAIT = 900.0  # seconds a busy board may take to finish a blocking command before we give up
SD_RE = re.compile(r"SD printing byte\s+(\d+)\s*/\s*(\d+)")
SHA_RE = re.compile(r"-([0-9a-f]{12})\.[A-Za-z0-9]+$")
NO_SD = ("this printer's board does not report SD card support (M115 has no Cap:SDCARD:1), and Gantrel "
         "never streams a print over USB")
NO_SD_HINT = "Copy the file to the printer's SD card or USB stick by hand, or run OctoPrint and pair that instead"


class Halted(Refused):
    """Marlin said 'Printer halted' (after M112 or a fault)."""

    def __init__(self, line: str):
        super().__init__(f"the printer has halted ({line[:80]})", "Reset or power cycle the printer")


def checksum(line: str) -> int:
    cs = 0
    for b in line.encode():
        cs ^= b
    return cs


def sd_name(remote_name: str) -> str:
    """8.3 name for the SD card, taken from the content hash in the remote name."""
    m = SHA_RE.search(remote_name)
    digest = m.group(1) if m else hashlib.sha256(remote_name.encode()).hexdigest()
    return digest[:8].upper() + ".GCO"


def card_lines(data: bytes) -> list[str]:
    """The lines Marlin keeps on the card: no comments, no blank lines, no trailing spaces."""
    out = []
    for raw in data.decode("utf-8", "replace").splitlines():
        line = raw.split(";", 1)[0].strip()
        if line:
            out.append(line)
    return out


class _Link:
    """One open serial connection that speaks Marlin's line protocol."""

    def __init__(self, path: str, baud: int, what: str, timeout: float = 8.0, handshake: bool = True):
        try:
            import serial  # pyserial
        except ImportError:  # pragma: no cover - pyserial is a dependency
            raise Unsupported("pyserial is not installed", "Install gantrel-printer with its dependencies") from None
        self.what = what
        try:
            self.s = serial.Serial(path, baud, timeout=0.2, write_timeout=5, exclusive=True)
        except (serial.SerialException, OSError, ValueError) as err:
            busy = "busy" in str(err).lower() or "lock" in str(err).lower()
            raise Unreachable(f"could not open {path} for {what}",
                              "Close other software using the port (OctoPrint, Cura, Pronterface)" if busy
                              else "Check the USB cable and the device path (ls /dev/serial/by-id/)") from None
        self._keep_dtr()
        self.buf = b""
        if not handshake:
            return
        try:
            self._handshake(timeout)
        except BaseException:
            self.close()
            raise

    def _keep_dtr(self) -> None:
        try:
            import termios

            attrs = termios.tcgetattr(self.s.fd)
            attrs[2] &= ~termios.HUPCL
            termios.tcsetattr(self.s.fd, termios.TCSANOW, attrs)
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.s.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def readline(self, deadline: float) -> str | None:
        while b"\n" not in self.buf:
            if time.monotonic() > deadline:
                return None
            chunk = self.s.read(1)
            if chunk:
                self.buf += chunk + self.s.read(self.s.in_waiting)
            if len(self.buf) > 65536:
                raise ProtocolError(f"{self.what} sent a line that never ended")
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode("utf-8", "replace").strip()

    def write(self, line: str) -> None:
        self.s.write((line + "\n").encode())
        self.s.flush()

    def _handshake(self, timeout: float) -> None:
        """Boards that reset on open print a banner first. Ask M105 until one 'ok' comes back.

        A board busy with a blocking command (M109, M190, G29) answers nothing but busy and
        temperature lines until that command ends. Those prove it is alive: stop asking and wait for
        the ok of the M105 already queued, for up to HEAT_WAIT seconds.
        """
        deadline = time.monotonic() + timeout
        alive = False
        while time.monotonic() < deadline:
            if not alive:
                self.write("M105")
            step = min(deadline, time.monotonic() + 1.0)
            while True:
                line = self.readline(step)
                if line is None:
                    break
                if is_halted(line):
                    raise Halted(line)
                if line.startswith("ok"):
                    self._drain()
                    return
                if line.startswith("echo:busy") or ALIVE_TEMP_RE.match(line):
                    if not alive:
                        alive = True
                        deadline = time.monotonic() + HEAT_WAIT
                    step = min(deadline, time.monotonic() + 1.0)
        raise Unreachable(f"{self.what} did not answer M105 on the serial port",
                          "Check the baud rate (pair with --port 250000 for some boards) and that it runs Marlin")

    def _drain(self) -> None:
        end = time.monotonic() + 0.1
        while self.readline(end) is not None:
            pass

    def cmd(self, line: str, timeout: float = 10.0) -> list[str]:
        """Send one command; return the lines before 'ok', plus the ok line's own text."""
        self.write(line)
        out = []
        deadline = time.monotonic() + timeout
        while True:
            got = self.readline(deadline)
            if got is None:
                raise Unreachable(f"{self.what} stopped answering on the serial port")
            if is_halted(got):
                raise Halted(got)
            if got.startswith("echo:busy"):
                deadline = time.monotonic() + timeout
                continue
            if got.startswith("ok"):
                out.append(got)
                return out
            if got.startswith("Error:") and "Unknown command" in got:
                raise Refused(f"{self.what} does not know {line.split()[0]}")
            out.append(got)


def is_halted(line: str) -> bool:
    return "Printer halted" in line or "kill() called" in line


class SerialAdapter(Adapter):
    type_name = "serial"
    label = "Marlin over USB serial"
    default_port = DEFAULT_BAUD  # the "port" of a serial printer is its baud rate
    needs_secret = False

    def _open(self) -> _Link:
        return _Link(self.cfg.host, self.port, self.cfg.name)

    # A tiny local note so status can tell "paused by Gantrel" from "printing": Marlin 2.1 reports the
    # same "SD printing byte" line for both. It holds the SD position at pause time and nothing secret.
    @property
    def _marker(self) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", self.cfg.name)
        return config.state_dir() / f"serial-paused-{safe}.json"

    def _clear_marker(self) -> None:
        try:
            self._marker.unlink()
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------ contract

    @classmethod
    def probe(cls, host, port=None, timeout=2.0):
        """Opens only an explicit /dev/ path. discover never opens serial ports on its own."""
        if not str(host).startswith("/dev/"):
            return None
        try:
            with _Link(host, port or DEFAULT_BAUD, host, timeout=max(timeout, 3.0)) as link:
                info = " ".join(link.cmd("M115", timeout=timeout))
        except Exception:
            return None
        m = re.search(r"FIRMWARE_NAME:\s*(.+?)(?:\s+\w+:|$)", info)
        if not m or "marlin" not in m.group(1).lower():
            return None
        sd = "SD card" if "Cap:SDCARD:1" in info else "no SD card support (status only)"
        mt = re.search(r"MACHINE_TYPE:\s*(.+?)(?:\s+[A-Z_]+:|$)", info)
        return {"type": cls.type_name, "host": host, "port": port or DEFAULT_BAUD,
                "detail": f"{(mt.group(1) if mt else 'Marlin printer').strip()}, {m.group(1).strip()}, {sd}"}

    def pair(self):
        with self._open() as link:
            info = " ".join(link.cmd("M115"))
        st = self.status()
        return {"state": st.state, "sd_card": "Cap:SDCARD:1" in info}

    def status(self) -> Status:
        try:
            with self._open() as link:
                temps = " ".join(link.cmd("M105"))
                m27 = " ".join(link.cmd("M27"))
                cur = " ".join(link.cmd("M27 C")) if "Not SD printing" in m27 or SD_RE.search(m27) else ""
        except Halted as err:
            self._clear_marker()
            return Status(printer=self.cfg.name, type=self.type_name, state="error",
                          message=f"{err}. Reset or power cycle the printer")
        t, b, c = TEMP_RE.search(temps), BED_RE.search(temps), CHAMBER_RE.search(temps)
        sd = SD_RE.search(m27)
        f = re.search(r"Current file:\s*([^\s(]\S*)", cur)
        file = f.group(1) if f else ""
        state, progress, note = "idle", None, ""
        if sd and int(sd.group(2)):
            pos, total = int(sd.group(1)), int(sd.group(2))
            progress = pos / total
            state = "printing"
            try:
                marked = json.loads(self._marker.read_text())
            except (OSError, ValueError):
                marked = None
            if marked and marked.get("file") == file and marked.get("pos") == pos:
                state = "paused"
            elif marked:
                self._clear_marker()  # it moved on: someone resumed it at the printer
        elif file:
            state, note = "paused", "a file is open on the SD card but not printing"
        else:
            self._clear_marker()
        busy = state in ("printing", "paused")
        return Status(
            printer=self.cfg.name, type=self.type_name, state=state, file=file if busy else "",
            progress=progress if busy else None,
            nozzle_c=float(t.group(1)) if t else None, nozzle_target_c=float(t.group(2)) if t else None,
            bed_c=float(b.group(1)) if b else None, bed_target_c=float(b.group(2)) if b else None,
            chamber_c=float(c.group(1)) if c else None, message=note,
        )

    def _listing(self, link: _Link) -> dict[str, int | None]:
        files = {}
        for line in link.cmd("M20", timeout=30):
            if line.startswith(("Begin file list", "End file list", "ok", "echo:")):
                continue
            parts = line.split()
            if parts:
                size = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
                files[parts[0].upper()] = size
        return files

    def upload(self, local_path: Path, remote_name: str) -> str:
        name = sd_name(remote_name)
        lines = card_lines(Path(local_path).read_bytes())
        if not lines:
            raise Refused(f"{Path(local_path).name} has no G-code lines")
        with self._open() as link:
            if "Cap:SDCARD:1" not in " ".join(link.cmd("M115")):
                raise Unsupported(NO_SD, NO_SD_HINT)
            # M21 or M28 during an SD print can stop or corrupt it, so look before touching the card.
            # Marlin reports "SD printing byte" while printing and while paused (the file stays open).
            m27 = " ".join(link.cmd("M27"))
            if SD_RE.search(m27):
                raise Refused(f"{self.cfg.name} is printing from its SD card (or paused mid-print)",
                              "Upload when the printer is idle; the file is kept on this computer")
            link.cmd("M21")  # mount the card
            if name in self._listing(link):
                raise Refused(f"{name} is already on {self.cfg.name}'s SD card; uploads never replace a file",
                              "Delete it on the printer first, or use the job that uploaded it")
            link.cmd("M110 N0")
            got = link.cmd(f"M28 {name}")
            if any("open failed" in g.lower() for g in got):
                raise Refused(f"{self.cfg.name} could not create {name} on its SD card", "Check the card")
            self._send_numbered(link, lines + ["M29"])
        return name

    def _send_numbered(self, link: _Link, lines: list[str]) -> None:
        """Send N-numbered, checksummed lines; go back when Marlin asks for a resend."""
        i, resends = 0, 0
        while i < len(lines):
            n = i + 1
            body = f"N{n} {lines[i]}"
            link.write(f"{body}*{checksum(body)}")
            deadline = time.monotonic() + 15
            want = None
            while True:
                got = link.readline(deadline)
                if got is None:
                    raise Unreachable(f"{self.cfg.name} stopped answering during the upload",
                                      "The file on the SD card is incomplete; delete it on the printer")
                if is_halted(got):
                    raise Refused(f"{self.cfg.name} halted during the upload")
                r = re.match(r"(?:Resend|rs):?\s*N?(\d+)", got)
                if r:
                    want = int(r.group(1))
                    continue
                if got.startswith("ok"):
                    break
            if want is not None:
                resends += 1
                if resends > 50:
                    raise ProtocolError(f"{self.cfg.name} kept asking for resends; the USB link is unreliable")
                i = max(0, want - 1)
            else:
                i += 1

    def remote_info(self, remote_name: str) -> dict | None:
        name = sd_name(remote_name) if not remote_name.upper().endswith(".GCO") else remote_name.upper()
        with self._open() as link:
            files = self._listing(link)
        if name not in files:
            return None
        return {"size": files[name], "mtime": None}

    def _start(self, remote_name: str) -> None:
        with self._open() as link:
            link.cmd(f"M23 {remote_name}")
            link.cmd("M24")
        self._clear_marker()

    def _resume(self) -> None:
        with self._open() as link:
            link.cmd("M24")
        self._clear_marker()

    def pause(self) -> None:
        with self._open() as link:
            link.cmd("M25")
            sd = SD_RE.search(" ".join(link.cmd("M27")))
            cur = re.search(r"Current file:\s*([^\s(]\S*)", " ".join(link.cmd("M27 C")))
        if sd:
            self._marker.parent.mkdir(parents=True, exist_ok=True)
            self._marker.write_text(json.dumps({"file": cur.group(1) if cur else "", "pos": int(sd.group(1))}))

    def cancel(self) -> None:
        with self._open() as link:
            link.cmd("M524")  # abort the SD print (Marlin 2)
        self._clear_marker()

    def emergency_stop(self) -> str:
        # No handshake: a board inside M109/M190/G29 sends no ok for minutes. Marlin's emergency
        # parser acts on M112 the moment it arrives, so send it first and read afterward.
        with _Link(self.cfg.host, self.port, self.cfg.name, handshake=False) as link:
            link.write("M112")
            end = time.monotonic() + 1.0
            while link.readline(end) is not None:
                pass
        self._clear_marker()
        return ("Sent M112 emergency stop: Marlin turned the heaters and motors off and halted. Press reset or "
                "power cycle the printer before using it again")

    def start_by_hand(self, remote_name: str = "") -> str:
        what = f"Start {remote_name}" if remote_name else "Start or resume it"
        return f"{what} on the printer's screen (Print from SD, or Resume)."

    def identity(self) -> str:
        c = self.cfg
        return "|".join((c.type, c.host, str(self.port), c.serial, c.tls_fingerprint))

    def camera(self) -> Camera:
        # A USB printer has no network camera; --camera-url can point at one.
        return Camera(self.cfg.camera_url, self.cfg.stream_url)
