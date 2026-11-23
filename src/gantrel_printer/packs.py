"""Printer packs: a shareable, reviewable setup for one printer model.

A pack is data only: the printer's size and limits, its start and end G-code, and the filament
settings that worked on it. It never holds code, and never anything that identifies the person's
machine or network (no host, IP address, port override, serial, access code, TLS pin, MAC address,
Wi-Fi name, user name, file path, credential URL, email or camera URL). A pack never pairs with or
contacts a printer; network pairing stays separate.

The format is JSON, "format": "gantrel-pack/1", and the schema below is strict: unknown keys are
refused at every level. check() runs the schema, the physical and materials.json ranges, the
filament rules from plan/check.py against the pack's own printer, a privacy scan of every string
and the G-code policy. It returns refusals (the pack is invalid), findings (shown; a person must
accept them) and notes (shown only).

The sha256 of the file's bytes is the pack's identity: `pack show` prints it, and `pack import`
installs only the file whose hash starts with the 12 hex digits the person read.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import ipaddress
import json
import math
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import config, materials, slicing
from .errors import ConfigError, SafetyError

FORMAT = "gantrel-pack/1"
MAX_FILE = 256 * 1024       # bytes
MAX_STRING = 4096           # characters, any string except the G-code blocks
MAX_GCODE = 8 * 1024        # characters, start_gcode and end_gcode each
MAX_GCODE_LINE = 200        # characters, any one line of them
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
SEMVER_RE = re.compile(r"^(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})(?:-[0-9A-Za-z.-]{1,32})?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PORT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
ORCA_RE = re.compile(r"^[A-Za-z0-9 ()._+@-]{1,128}$")
FLAVORS = ("klipper", "marlin", "marlin2", "reprapfirmware", "repetier", "smoothie")
FILAMENT_MM = (1.75, 2.85, 3.0)
CAL_TESTS = ("first-layer", "temp-tower", "retraction", "flow")
PROOF_KINDS = ("print", "calibration", "slice")
MULTILINE = ("notes", "start_gcode", "end_gcode", "result", "note")
# The printer limits a pack carries. A profile built from a pack cites each one it holds, with the pack as the
# source: plan/check.py then fails a rule past them, as it does past a vendor's cited limit. top_speed is optional
# (an author may not know it); without it the profile has none and the speed rules fall back to max_speed.
LIMITS = ("hotend_max_c", "bed_max_c", "max_flow", "high_flow", "hardened_nozzle", "max_accel", "chamber_heater")
OPTIONAL_LIMITS = ("top_speed",)


# ---------------------------------------------------------------- report


@dataclass
class Item:
    level: str              # refusal, finding or note
    path: str               # field path, like printer.start_gcode or materials[0].nozzle_c
    reason: str
    line: int | None = None  # 1-based line inside a multi-line string
    text: str = ""          # the line (private data in it masked)

    def to_dict(self) -> dict:
        return {"level": self.level, "path": self.path, "line": self.line, "text": self.text, "reason": self.reason}

    def where(self) -> str:
        return self.path + (f" line {self.line}" if self.line else "")


@dataclass
class Report:
    sha256: str = ""
    items: list[Item] = field(default_factory=list)

    def add(self, level: str, path: str, reason: str, line: int | None = None, text: str = "") -> None:
        self.items.append(Item(level, path, reason, line, shown(text)))

    def of(self, level: str) -> list[Item]:
        return [i for i in self.items if i.level == level]

    @property
    def refusals(self) -> list[Item]:
        return self.of("refusal")

    @property
    def findings(self) -> list[Item]:
        return self.of("finding")

    @property
    def notes(self) -> list[Item]:
        return self.of("note")

    @property
    def private(self) -> list[Item]:
        """Refusals for private data. A pack with any is never shown: its strings are withheld."""
        return [i for i in self.refusals if PRIVATE_WHY in i.reason]

    @property
    def result(self) -> str:
        return "refused" if self.refusals else ("findings" if self.findings else "pass")

    def to_dict(self) -> dict:
        return {"result": self.result, "sha256": self.sha256, "accept": self.sha256[:12],
                "refusals": [i.to_dict() for i in self.refusals], "findings": [i.to_dict() for i in self.findings],
                "notes": [i.to_dict() for i in self.notes]}


def shown(s: str, limit: int | None = 160) -> str:
    """Text safe to print on a terminal: control, format and line separator characters escaped, long text cut
    (never with limit None)."""
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        out.append(ch.encode("unicode_escape").decode("ascii") if cat[0] == "C" or cat in ("Zl", "Zp") else ch)
    t = "".join(out)
    return t if limit is None or len(t) <= limit else t[:limit - 3] + "..."


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- schema


@dataclass(frozen=True)
class S:
    """One field's rule. kind: str, int, num, bool, enum, date, list, obj, pair, map."""
    kind: str
    lo: float | None = None
    hi: float | None = None
    max_len: int = MAX_STRING
    min_len: int = 0
    pattern: re.Pattern | None = None
    choices: tuple = ()
    item: "S | None" = None
    fields: tuple = ()              # (name, S, required)
    max_items: int = 0
    min_items: int = 0
    ordered: bool = False           # pair: first <= second
    what: str = ""


def _o(*fields) -> S:
    return S("obj", fields=tuple(fields))


def _req(name, spec):
    return (name, spec, True)


def _opt(name, spec):
    return (name, spec, False)


def _adapter_names() -> tuple[str, ...]:
    from .adapters import ADAPTERS
    return tuple(ADAPTERS)


TEMP_NOZZLE = S("int", 150, 500, what="C")
TEMP_BED = S("int", 0, 200, what="C")
CALIBRATED = _o(_req("test", S("enum", choices=CAL_TESTS)), _req("date", S("date")),
                _req("result", S("str", max_len=1000, min_len=1)))
MATERIAL = _o(
    _req("family", S("enum", choices=tuple(materials.FAMILIES))),
    _req("brand", S("str", max_len=64, min_len=1)),
    _req("name", S("str", max_len=80, min_len=1)),
    _req("nozzle_c", TEMP_NOZZLE), _req("first_layer_nozzle_c", TEMP_NOZZLE),
    _req("bed_c", TEMP_BED), _req("first_layer_bed_c", TEMP_BED),
    _req("fan", S("pair", item=S("int", 0, 100, what="percent"), ordered=True)),
    _req("max_flow", S("num", 0.5, 200, what="mm3/s")),
    _req("retraction_mm", S("num", 0, 15, what="mm")),
    _req("retraction_speed", S("num", 5, 150, what="mm/s")),
    _opt("pressure_advance", S("num", 0, 2)),
    _opt("flow_ratio", S("num", 0.5, 1.5)),
    _req("calibrated", S("list", item=CALIBRATED, max_items=50)),
)


def printer_spec() -> S:
    return _o(
        _req("model", S("str", max_len=80, min_len=1)),
        _req("adapter", S("enum", choices=_adapter_names())),
        _req("bed", S("pair", item=S("int", 50, 2000, what="mm"))),
        _req("height", S("int", 50, 2000, what="mm")),
        _req("nozzle", S("num", 0.1, 2.0, what="mm")),
        _req("flavor", S("enum", choices=FLAVORS)),
        _req("max_speed", S("int", 10, 2000, what="mm/s")),
        _req("accel", S("int", 100, 100000, what="mm/s2")),
        _req("max_accel", S("int", 100, 100000, what="mm/s2")),
        _opt("top_speed", S("int", 10, 2000, what="mm/s")),
        _req("hotend_max_c", S("int", 150, 500, what="C")),
        _req("bed_max_c", S("int", 0, 200, what="C")),
        _req("max_flow", S("num", 1, 200, what="mm3/s")),
        _req("high_flow", S("bool")), _req("hardened_nozzle", S("bool")), _req("enclosed", S("bool")),
        _req("chamber_heater", S("bool")), _req("direct_drive", S("bool")),
        _req("filament_mm", S("enum", choices=FILAMENT_MM)),
        _req("start_gcode", S("str", max_len=MAX_GCODE)),
        _req("end_gcode", S("str", max_len=MAX_GCODE)),
        _opt("orca_machine", S("str", pattern=ORCA_RE, max_len=128)),
        _opt("default_ports", S("map", item=S("int", 1, 65535), pattern=PORT_NAME_RE, max_items=8)),
    )


def pack_spec() -> S:
    return _o(
        _req("format", S("enum", choices=(FORMAT,))),
        _req("id", S("str", pattern=ID_RE, max_len=64, what="3 to 64 lowercase letters, digits and hyphens")),
        _req("name", S("str", max_len=120, min_len=1)),
        _req("version", S("str", pattern=SEMVER_RE, max_len=64, what="a version like 1.0.0")),
        _req("author", S("str", max_len=64)),
        _req("created", S("date")),
        _req("notes", S("str", max_len=2000)),
        _req("printer", printer_spec()),
        _req("materials", S("list", item=MATERIAL, min_items=1, max_items=32)),
        _req("proof", S("list", item=_o(_req("kind", S("enum", choices=PROOF_KINDS)), _req("date", S("date")),
                                        _req("note", S("str", max_len=1000, min_len=1))), max_items=50)),
    )


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _validate(v, spec: S, path: str, rep: Report) -> bool:
    def bad(reason: str) -> bool:
        rep.add("refusal", path, reason)
        return False

    k = spec.kind
    if k == "obj":
        if not isinstance(v, dict):
            return bad("must be an object")
        ok = True
        names = {f[0] for f in spec.fields}
        for key in v:
            if key not in names:
                rep.add("refusal", f"{path}.{key}" if path else key,
                        f"unknown key; {FORMAT} packs hold only their listed fields")
                ok = False
        for name, sub, required in spec.fields:
            p = f"{path}.{name}" if path else name
            if name not in v:
                if required:
                    rep.add("refusal", p, "missing")
                    ok = False
                continue
            ok = _validate(v[name], sub, p, rep) and ok
        return ok
    if k == "list":
        if not isinstance(v, list):
            return bad("must be a list")
        if not spec.min_items <= len(v) <= spec.max_items:
            return bad(f"must hold {spec.min_items} to {spec.max_items} entries, has {len(v)}")
        ok = True
        for i, x in enumerate(v):
            ok = _validate(x, spec.item, f"{path}[{i}]", rep) and ok
        return ok
    if k == "map":
        if not isinstance(v, dict):
            return bad("must be an object")
        if len(v) > spec.max_items:
            return bad(f"must hold at most {spec.max_items} entries")
        ok = True
        for key, x in v.items():
            if not spec.pattern.match(key):
                rep.add("refusal", f"{path}.{key}", "service name must be 1 to 32 lowercase letters, digits, - or _")
                ok = False
            ok = _validate(x, spec.item, f"{path}.{key}", rep) and ok
        return ok
    if k == "pair":
        if not isinstance(v, list) or len(v) != 2:
            return bad("must be a list of two numbers")
        ok = all([_validate(v[0], spec.item, f"{path}[0]", rep), _validate(v[1], spec.item, f"{path}[1]", rep)])
        if ok and spec.ordered and v[0] > v[1]:
            return bad("the first value must not be above the second")
        return ok
    if k == "bool":
        return True if isinstance(v, bool) else bad("must be true or false")
    if k == "enum":
        if isinstance(v, bool) or not isinstance(v, (str, int, float)) or v not in spec.choices:
            return bad("must be one of: " + ", ".join(str(c) for c in spec.choices))
        return True
    if k in ("int", "num"):
        if not _is_num(v) or (k == "int" and not isinstance(v, int)):
            return bad("must be a whole number" if k == "int" else "must be a number")
        if not spec.lo <= v <= spec.hi:
            return bad(f"{v:g} is outside {spec.lo:g} to {spec.hi:g}" + (f" {spec.what}" if spec.what else ""))
        return True
    if k == "date":
        if not isinstance(v, str) or not DATE_RE.match(v):
            return bad("must be a date like 2026-11-22")
        try:
            _dt.date.fromisoformat(v)
        except ValueError:
            return bad("is not a real date")
        return True
    if k == "str":
        if not isinstance(v, str):
            return bad("must be text")
        if len(v) > spec.max_len:
            return bad(f"is {len(v)} characters; the limit is {spec.max_len}")
        if len(v) < spec.min_len:
            return bad("must not be empty")
        if spec.pattern and not spec.pattern.match(v):
            return bad("must be " + (spec.what or "plain letters, digits, spaces and ()._+@-"))
        return True
    raise AssertionError(k)


# ---------------------------------------------------------------- privacy


PRIVATE_WHY = "packs must not identify a person, machine or network"
IPV4_RE = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?!\.?\d)")
IPV6_RE = re.compile(r"(?i)(?<![0-9a-f:])(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}(?![0-9a-f:])")
MAC_RE = re.compile(r"(?i)(?<![0-9a-f:-])[0-9a-f]{2}([:-])(?:[0-9a-f]{2}\1){4}[0-9a-f]{2}(?![0-9a-f:-])")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
CRED_URL_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@]+@")
HOME_RE = re.compile(r"(?i)(?:/home/|/Users/|[a-z]:[\\/]+Users\b)")
LOCAL_HOST_RE = re.compile(r"(?i)\b(?:localhost|[a-z0-9-]+\.(?:local|lan|home|internal|localdomain|ts\.net))\b")
CAMERA_URL_RE = re.compile(r"(?i)\b(?:rtsps?|rtmp|webrtc)://\S+|\bhttps?://\S*(?:webcam|snapshot|stream|mjpe?g|camera|"
                           r"\?action=)\S*")
PORT_URL_RE = re.compile(r"(?i)\bhttps?://[^\s/:@]+:\d+")
SERIAL_RE = re.compile(r"(?<![A-Za-z0-9_])[0-9A-Z]{12,20}(?![A-Za-z0-9_])")
HEX_RE = re.compile(r"(?<![A-Za-z0-9_])[0-9a-fA-F]{20,}(?![A-Za-z0-9_])")
TOKEN_RE = re.compile(r"(?<![A-Za-z0-9+_=-])[A-Za-z0-9+_=-]{20,}(?![A-Za-z0-9+_=-])")
CODE8_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{8}(?![A-Za-z0-9])")
CODE_WORD_RE = re.compile(r"(?i)\b(?:access|code|passcode|pin)\b")
KV_PATTERNS = (
    (re.compile(r"(?i)\b(?:password|passwd|pass|api[_ -]?key|token|secret|access[_ -]?code|check[_ -]?code)\s*[:=]\s*\S+"),
     "a password, key or code"),
    (re.compile(r"(?i)\b(?:user(?:name)?|login)\s*[:=]\s*\S+"), "a user name"),
    (re.compile(r"(?i)\b(?:ssid|wi-?fi(?: name| network)?|wlan)\s*[:=]\s*\S+"), "a Wi-Fi name"),
    (re.compile(r"(?i)\bserial(?:\s*(?:number|no\.?|#))?\s*[:=]\s*\S+"), "a serial number"),
)


def _ipv4(m) -> bool:
    return all(0 <= int(g) <= 255 for g in m.groups())


def _ipv6(m) -> bool:
    s = m.group(0)
    if not re.search(r"[0-9a-fA-F]", s) or ("::" not in s and s.count(":") < 4):
        return False
    try:
        ipaddress.IPv6Address(s)
        return True
    except ValueError:
        return False


def _serial(m) -> bool:
    s = m.group(0)
    return sum(c.isdigit() for c in s) >= 3 and any(c.isalpha() for c in s)


def _token(m) -> bool:
    s = m.group(0)
    return any(c.isupper() for c in s) and any(c.islower() for c in s) and any(c.isdigit() for c in s)


PRIVACY = (
    (MAC_RE, None, "a MAC address"),
    (IPV4_RE, _ipv4, "an IP address"),
    (IPV6_RE, _ipv6, "an IPv6 address"),
    (CRED_URL_RE, None, "a URL with a user or password in it"),
    (EMAIL_RE, None, "an email address"),
    (HOME_RE, None, "a home folder path (it names the person's user account)"),
    (LOCAL_HOST_RE, None, "a local network host name"),
    (CAMERA_URL_RE, None, "a camera or stream URL"),
    (PORT_URL_RE, None, "a URL with a port number (a service on someone's network)"),
    (SERIAL_RE, _serial, "something shaped like a printer serial number"),
    (HEX_RE, None, "a long hex string (a key, token, fingerprint or hash)"),
    (TOKEN_RE, _token, "something shaped like a key or token"),
)


def _mask(line: str, spans: list[tuple[int, int]]) -> str:
    for a, b in sorted(spans, reverse=True):
        line = line[:a] + "[hidden]" + line[b:]
    return line


def scan_string(s: str, path: str, rep: Report, multiline: bool) -> None:
    """Privacy scan and character rules for one string."""
    lines = s.split("\n")
    for n, line in enumerate(lines, 1):
        ln = n if len(lines) > 1 else None
        for i, ch in enumerate(line):
            cat = unicodedata.category(ch)
            if (cat[0] == "C" and ch != "\t") or cat in ("Zl", "Zp"):
                rep.add("refusal", path, f"holds an invisible or control character (U+{ord(ch):04X}); packs must "
                                         "read the same to a person as to a machine", ln, line)
                break
        hits: list[tuple[int, int, str]] = []
        for rx, ok, why in PRIVACY:
            for m in rx.finditer(line):
                if ok is None or ok(m):
                    if not any(a <= m.start() < b or m.start() <= a < m.end() for a, b, _ in hits):
                        hits.append((m.start(), m.end(), why))
        for m in CODE8_RE.finditer(line):
            tok = m.group(0)
            near = line[max(0, m.start() - 30):m.end() + 30]
            if any(c.isdigit() for c in tok) and CODE_WORD_RE.search(near):
                hits.append((m.start(), m.end(), "an 8 character code next to the word code or access"))
        for rx, why in KV_PATTERNS:
            for m in rx.finditer(line):
                hits.append((m.start(), m.end(), why))
        for why in dict.fromkeys(h[2] for h in hits):
            rep.add("refusal", path, f"holds {why}; {PRIVATE_WHY}", ln,
                    _mask(line, [(a, b) for a, b, _ in hits]))
    if not multiline and len(lines) > 1:
        rep.add("refusal", path, "must be one line")
    if "\r" in s:
        rep.add("refusal", path, "uses carriage returns; use plain line feeds")


def privacy_scan(raw, rep: Report, path: str = "") -> None:
    """Walk every string in the parsed JSON (keys too), whatever the schema says."""
    if isinstance(raw, dict):
        for k, v in raw.items():
            p = f"{path}.{k}" if path else str(k)
            scan_string(str(k), f"{p} (key)", rep, False)
            privacy_scan(v, rep, p)
    elif isinstance(raw, list):
        for i, v in enumerate(raw):
            privacy_scan(v, rep, f"{path}[{i}]")
    elif isinstance(raw, str):
        last = path.rsplit(".", 1)[-1]
        scan_string(raw, path, rep, last in MULTILINE)


# ---------------------------------------------------------------- G-code policy


ALLOWED_G = {0, 1, 2, 3, 4, 28, 29, 90, 91, 92}
ALLOWED_M = {82, 83, 84, 104, 105, 106, 107, 109, 140, 190, 141, 191, 204, 205, 220, 221, 400, 117, 73}
REFUSED_M = {
    500: "saves settings to the printer's EEPROM", 501: "loads settings from the EEPROM",
    502: "resets settings to factory defaults", 504: "checks the EEPROM", 997: "starts a firmware update",
    999: "restarts the firmware", 303: "runs a PID autotune, heating with no one watching",
    92: "changes steps per mm", 201: "changes the maximum acceleration", 203: "changes the maximum feed rate",
    206: "changes the home offsets", 301: "changes the hotend PID", 304: "changes the bed PID",
    851: "changes the probe offset", 906: "changes stepper motor current", 913: "changes the stepper hybrid threshold",
    302: "allows cold extrusion, which grinds filament and can damage the extruder",
}
REFUSED_WORDS = {
    "SAVE_CONFIG": "writes printer.cfg and restarts Klipper", "FIRMWARE_RESTART": "restarts the firmware",
    "RESTART": "restarts Klipper", "RUN_SHELL_COMMAND": "runs a program on the printer's computer",
    "SET_KINEMATIC_POSITION": "tells the printer it is somewhere it may not be, which can crash the head",
    "SDCARD_PRINT_FILE": "starts printing a file", "SET_STEPPER_ENABLE": "turns motors on or off by hand",
}
ALLOWED_MACROS = ("START_PRINT", "END_PRINT", "PRINT_START", "PRINT_END", "BED_MESH_CALIBRATE", "CLEAN_NOZZLE",
                  "PAUSE", "RESUME", "CANCEL_PRINT")
PLACEHOLDERS = (
    "first_layer_temperature", "first_layer_bed_temperature", "temperature", "bed_temperature",
    "chamber_temperature", "overall_chamber_temperature", "nozzle_diameter", "layer_height", "first_layer_height",
    "initial_layer_print_height", "initial_extruder", "current_extruder", "initial_tool", "filament_type",
    "filament_diameter", "nozzle_temperature", "nozzle_temperature_initial_layer", "bed_temperature_initial_layer",
    "bed_temperature_initial_layer_single", "hot_plate_temp", "hot_plate_temp_initial_layer", "textured_plate_temp",
    "textured_plate_temp_initial_layer", "print_bed_max", "print_bed_min", "first_layer_print_min",
    "first_layer_print_max", "first_layer_print_size", "max_layer_z", "layer_num", "layer_z", "total_layer_count",
    "travel_speed", "retract_length", "retraction_length",
)
PLACEHOLDER_RE = re.compile(r"^\s*([a-z_]+)\s*(?:\[\s*(\d+|initial_extruder|current_extruder|initial_tool)\s*\])?\s*$")
HOTEND_KEYS = {"EXTRUDER_TEMP", "EXTRUDER_TEMPERATURE", "EXTRUDER", "HOTEND_TEMP", "HOTEND", "NOZZLE_TEMP",
               "NOZZLE", "TOOL_TEMP"}
BED_KEYS = {"BED_TEMP", "BED_TEMPERATURE", "BED", "HEATER_BED"}
MARK = "\x00"   # stands in for a slicer placeholder: never a literal
GCODE_LINE_RE = re.compile(r"^\s*(?:N\d+\s*)?([GMTgmt])\s*(\d+)(?:\.\d+)?(.*?)(?:\*\d+)?\s*$")


def _strip_comment(line: str) -> str:
    cut = [i for i in (line.find(";"), line.find("#")) if i >= 0]
    return line[:min(cut)] if cut else line


def _placeholders(body: str) -> tuple[str, list[str]]:
    """Replace known slicer placeholders with MARK. Return the line and the unknown ones."""
    unknown = []

    def sub(m):
        inner = m.group(1)
        pm = PLACEHOLDER_RE.match(inner)
        if not pm or pm.group(1) not in PLACEHOLDERS:
            unknown.append(m.group(0))
        return MARK

    body = re.sub(r"\{([^{}]*)\}", sub, body)

    def legacy(m):
        if m.group(1) in PLACEHOLDERS:
            return MARK
        unknown.append(m.group(0))
        return MARK

    body = re.sub(r"\[([a-z_]+)\]", legacy, body)
    if "{" in body or "}" in body:
        unknown.append("{")
    return body, unknown


def _num(v: str) -> float | None:
    try:
        x = float(v)
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def _refused_anywhere(body: str) -> str:
    """A refused command anywhere on the line, not only first (display text after M117 and M118 is skipped)."""
    up = body.upper()
    if re.match(r"^\s*(?:N\d+\s*)?M\s*0*11[78](?!\d)", up):
        return ""
    for w, why in REFUSED_WORDS.items():
        if re.search(rf"(?<![A-Z0-9_]){w}(?![A-Z0-9_])", up):
            return f"{w} {why}"
    for m in re.finditer(r"(?<![A-Z0-9_])M\s*0*(\d+)(?![\d.])", up):
        if int(m.group(1)) in REFUSED_M:
            return f"M{int(m.group(1))} {REFUSED_M[int(m.group(1))]}"
    return ""


def gcode_policy(text: str, path: str, hotend_max: float | None, bed_max: float | None, rep: Report) -> None:
    for n, raw in enumerate(text.split("\n"), 1):
        if any(ord(c) > 126 or (ord(c) < 32 and c != "\t") for c in raw):
            rep.add("refusal", path, "G-code must be plain printable ASCII", n, raw)
            continue
        if len(raw) > MAX_GCODE_LINE:
            # pack show prints every line whole; a short limit keeps each one readable in a review.
            rep.add("refusal", path, f"is {len(raw)} characters; a G-code line may be at most {MAX_GCODE_LINE}", n,
                    raw)
            continue
        if "\\" in raw:
            # Gantrel hands G-code to the slicer with \n escapes, so a backslash here could start a line the
            # policy never read (G28\nSAVE_CONFIG is one line here and two in the sliced file).
            rep.add("refusal", path, "holds a backslash; the slicer can read one as a line break, so a pack's "
                                     "G-code must not use any", n, raw)
            continue
        body = _strip_comment(raw).strip()
        if not body:
            continue
        if re.search(r"\{[^{}]*[\"'][^{}]*\}", body):
            rep.add("refusal", path, "a slicer expression with quotes builds text when the file is sliced, which could "
                                     "spell out a refused command", n, raw)
            continue
        body, unknown = _placeholders(body)
        hidden = _refused_anywhere(body)
        if hidden:
            rep.add("refusal", path, hidden, n, raw)
            continue
        temps: list[tuple[str, float]] = []   # (heater, value)
        m = GCODE_LINE_RE.match(body)
        finding = None
        if m:
            letter, num, rest = m.group(1).upper(), int(m.group(2)), m.group(3).upper()
            params = dict((k, v) for k, v in re.findall(r"([A-Z])\s*(-?\d+(?:\.\d+)?|" + MARK + ")", rest))
            cmd = f"{letter}{num}"
            if letter == "M" and num in REFUSED_M:
                rep.add("refusal", path, f"{cmd} {REFUSED_M[num]}", n, raw)
                continue
            if cmd == "M211" and _num(params.get("S", "1")) == 0:
                rep.add("refusal", path, "M211 S0 turns the soft endstops off", n, raw)
                continue
            if letter == "T":
                if num > 15:
                    finding = f"{cmd} picks a tool past T15"
            elif not ((letter == "G" and num in ALLOWED_G) or (letter == "M" and num in ALLOWED_M)):
                finding = f"{cmd} is not on Gantrel's allowed list"
            elif cmd == "G92" and set(re.findall(r"[A-Z]", rest)) != {"E"}:
                # Only G92 E<n> (resetting the extruder) passes. With an axis, or with none (Klipper then sets every
                # axis to 0), it tells the printer the head is somewhere it is not, as SET_KINEMATIC_POSITION does.
                finding = ("G92 other than G92 E tells the printer the head is somewhere it may not be, so a later move "
                           "can drive the nozzle into the bed or the frame")
            heater = {104: "hotend", 109: "hotend", 140: "bed", 190: "bed"}.get(num) if letter == "M" else None
            if heater:
                for key in ("S", "R"):
                    v = _num(params.get(key, ""))
                    if v is not None:
                        temps.append((heater, v))
        else:
            tokens = body.split()
            word = tokens[0].upper()
            if MARK in word or not re.match(r"^[A-Z_][A-Z0-9_]*$", word):
                finding = "is not a G-code command Gantrel recognizes"
            elif word in REFUSED_WORDS:
                rep.add("refusal", path, f"{word} {REFUSED_WORDS[word]}", n, raw)
                continue
            elif word not in ALLOWED_MACROS:
                finding = f"{word} is a printer macro or command not on Gantrel's allowed list"
            kv = {}
            for t in tokens[1:]:
                if "=" in t:
                    k, v = t.split("=", 1)
                    kv[k.upper()] = v
            for k, v in kv.items():
                x = _num(v)
                if x is None:
                    continue
                if k in HOTEND_KEYS:
                    temps.append(("hotend", x))
                elif k in BED_KEYS:
                    temps.append(("bed", x))
            if word == "SET_HEATER_TEMPERATURE" and _num(kv.get("TARGET", "")) is not None:
                h = kv.get("HEATER", "").lower()
                if h.startswith("extruder"):
                    temps.append(("hotend", _num(kv["TARGET"])))
                elif h == "heater_bed":
                    temps.append(("bed", _num(kv["TARGET"])))
        over = [(h, v) for h, v in temps
                if (h == "hotend" and hotend_max is not None and v > hotend_max)
                or (h == "bed" and bed_max is not None and v > bed_max)]
        if over:
            h, v = over[0]
            lim = hotend_max if h == "hotend" else bed_max
            rep.add("refusal", path, f"sets the {h} to {v:g} C, above this pack's {h}_max_c of {lim:g} C", n, raw)
            continue
        if unknown:
            rep.add("finding", path, f"uses slicer text {', '.join(sorted(set(unknown)))} that is not in Gantrel's "
                                     "known placeholder list; read what the slicer makes of it", n, raw)
        if finding:
            rep.add("finding", path, finding + "; read what it does on this printer before accepting", n, raw)


# ---------------------------------------------------------------- filament rules


def to_profile(pack: dict, sha: str = "", chosen: int | None = None) -> slicing.PrinterProfile:
    """A PrinterProfile from a checked pack. chosen puts that material first for its family."""
    p = pack["printer"]
    src = f"pack {pack['id']} {sha[:12]}" if sha else f"pack {pack['id']}"
    mats = [material_from(pack, i, src) for i in range(len(pack["materials"]))]
    if chosen is not None:
        mats.insert(0, mats.pop(chosen))
    cited = LIMITS + tuple(k for k in OPTIONAL_LIMITS if k in p)
    return slicing.PrinterProfile(
        key=f"pack:{pack['id']}", label=p["model"], bed=(p["bed"][0], p["bed"][1]), height=p["height"],
        flavor=p["flavor"], nozzle=p["nozzle"], max_speed=p["max_speed"], accel=p["accel"],
        direct_drive=p["direct_drive"], enclosed=p["enclosed"],
        # slicing.py passes G-code to the slicer with \n escapes, as its own profiles do
        start_gcode=p["start_gcode"].replace("\n", "\\n"), end_gcode=p["end_gcode"].replace("\n", "\\n"),
        orca_machine=p.get("orca_machine", ""), filament_mm=p["filament_mm"],
        hotend_max_c=p["hotend_max_c"], bed_max_c=p["bed_max_c"], max_flow=p["max_flow"], high_flow=p["high_flow"],
        hardened_nozzle=p["hardened_nozzle"], max_accel=p["max_accel"], chamber_heater=p["chamber_heater"],
        top_speed=p.get("top_speed", 0), cited=cited, limits_source=f"{src} (the pack author's values)", pack_materials=tuple(mats), source=src)


def material_from(pack: dict, i: int, src: str) -> slicing.Material:
    m = pack["materials"][i]
    fam = materials.get(m["family"])
    base = slicing.MATERIALS.get(fam.name)
    return slicing.Material(
        fam.name, m["nozzle_c"], m["first_layer_nozzle_c"], m["bed_c"], m["first_layer_bed_c"], m["fan"][0], m["fan"][1],
        max_speed=base.max_speed if base else pack["printer"]["max_speed"], chamber=base.chamber if base else 0,
        retract_mm=m["retraction_mm"], note=f"{m['brand']} {m['name']} from {src}.",
        nozzle_range=(fam.nozzle_min_c, min(fam.nozzle_max_c, pack["printer"]["hotend_max_c"])),
        label=f"{m['brand']} {m['name']}", max_flow=m["max_flow"], retract_speed=m["retraction_speed"],
        pressure_advance=m.get("pressure_advance"), flow_ratio=m.get("flow_ratio"))


def material_rules(pack: dict, rep: Report) -> None:
    """plan/check.py's temperature, enclosure, nozzle, flexible, fan and retraction rules, against the pack's printer."""
    from .plan import check as pcheck
    profile = to_profile(pack)
    p = pack["printer"]
    if p["accel"] > p["max_accel"]:
        rep.add("refusal", "printer.accel", f"accel {p['accel']} is above max_accel {p['max_accel']}")
    if "top_speed" in p and p["max_speed"] > p["top_speed"]:
        rep.add("refusal", "printer.max_speed", f"max_speed {p['max_speed']} is above top_speed {p['top_speed']}, "
                                                "the fastest this printer moves")
    names = {}
    for i, m in enumerate(pack["materials"]):
        path = f"materials[{i}]"
        key = (m["brand"].strip().lower(), m["name"].strip().lower())
        if key in names:
            rep.add("refusal", f"{path}.name", f"same brand and name as materials[{names[key]}]")
        names.setdefault(key, i)
        fil = materials.get(m["family"])
        s = {"nozzle_c": m["nozzle_c"], "nozzle_first_layer_c": m["first_layer_nozzle_c"], "bed_c": m["bed_c"],
             "bed_first_layer_c": m["first_layer_bed_c"], "fan_percent": list(m["fan"]),
             "retract_mm": m["retraction_mm"]}
        rules = pcheck._temps(s, profile, fil) + pcheck._material_fit(s, profile, fil) + pcheck._cooling(s, fil, None)
        for r in rules:
            vals = ", ".join(f"{k} {v}" for k, v in r["values"].items() if k != "cited")
            if r["status"] == "fail" or (r["status"] == "warn" and r["rule"].startswith("part fan")):
                rep.add("refusal", path, f"{r['rule']} fails ({r['formula']}; {vals}). {r['fix']}")
            elif r["status"] == "warn":
                rep.add("note", path, f"{r['rule']} ({vals}). {r['fix']}")
        if m["max_flow"] > p["max_flow"]:
            rep.add("refusal", f"{path}.max_flow",
                    f"{m['max_flow']:g} mm3/s is above the printer's max_flow of {p['max_flow']:g} mm3/s")


# ---------------------------------------------------------------- check


class _Dup(ValueError):
    pass


def _pairs(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise _Dup(k)
        d[k] = v
    return d


def _const(name):
    raise ValueError(f"{name} is not allowed in a pack")


UNREADABLE = object()   # parse() could not read the file (JSON null parses to None, so None cannot say so)


def parse(data: bytes, rep: Report):
    """The JSON value, or UNREADABLE after adding a refusal."""
    if len(data) > MAX_FILE:
        rep.add("refusal", "(file)", f"is {len(data)} bytes; the limit is {MAX_FILE} (256 KiB)")
        return UNREADABLE
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        rep.add("refusal", "(file)", "is not UTF-8 text")
        return UNREADABLE
    try:
        raw = json.loads(text, object_pairs_hook=_pairs, parse_constant=_const)
    except _Dup as err:
        rep.add("refusal", str(err), "appears twice in one object")
        return UNREADABLE
    except RecursionError:
        rep.add("refusal", "(file)", "is nested too deeply")
        return UNREADABLE
    except ValueError as err:
        rep.add("refusal", "(file)", f"is not valid JSON ({str(err)[:120]})")
        return UNREADABLE
    return raw


def check_bytes(data: bytes) -> tuple[dict | None, Report]:
    """Check a pack file's bytes. The pack is returned only when the schema passed."""
    rep = Report(sha256=sha256(data))
    raw = parse(data, rep)
    if raw is UNREADABLE:
        return None, rep
    if not isinstance(raw, dict):
        rep.add("refusal", "(file)", "must hold one JSON object")
        return None, rep
    ok = _validate(raw, pack_spec(), "", rep)
    privacy_scan(raw, rep)
    if not ok:
        return None, rep
    p = raw["printer"]
    for key in ("start_gcode", "end_gcode"):
        gcode_policy(p[key], f"printer.{key}", p["hotend_max_c"], p["bed_max_c"], rep)
    material_rules(raw, rep)
    _mask_private_lines(rep)
    return raw, rep


def _mask_private_lines(rep: Report) -> None:
    """Every result on a line that holds private data shows the masked line, never the raw one."""
    masked = {(i.path, i.line): i.text for i in rep.private}
    for i in rep.items:
        if i.text and (i.path, i.line) in masked:
            i.text = masked[(i.path, i.line)]


def check_file(path: str | Path) -> tuple[dict | None, Report, bytes]:
    path = Path(path).expanduser()
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise ConfigError(f"no file at {path}") from None
    if not path.is_file():
        raise ConfigError(f"{path} is not a file")
    if size > MAX_FILE:
        rep = Report()
        rep.add("refusal", "(file)", f"is {size} bytes; the limit is {MAX_FILE} (256 KiB)")
        return None, rep, b""
    data = path.read_bytes()
    pack, rep = check_bytes(data)
    return pack, rep, data


def dumps(pack: dict) -> bytes:
    """The bytes export writes: sorted nowhere (field order is the schema's), two-space indent, ASCII only."""
    return (json.dumps(pack, indent=2, ensure_ascii=True) + "\n").encode("ascii")


# ---------------------------------------------------------------- export


def _paired_type_and_model(name: str) -> tuple[str, str] | None:
    """Only the adapter type and model of a paired printer. No other config field is read out, and the
    OS keyring is never touched: hosts, ports, serials, fingerprints and camera URLs stay where they are."""
    path = config.config_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except ValueError:
        raise ConfigError(f"{path} is not valid JSON", "Fix or delete it, then pair again") from None
    entry = raw.get("printers", {}).get(name) if isinstance(raw, dict) else None
    if not isinstance(entry, dict):
        return None
    return str(entry.get("type", "")), str(entry.get("model", ""))


DEFAULT_RETRACT_SPEED = 40   # mm/s: PrusaSlicer's default retract_speed; Gantrel's profiles do not set one


def export(printer: str, pack_id: str | None = None, name: str | None = None, author: str = "",
           notes: str = "", materials_wanted: list[str] | None = None, today: str | None = None) -> tuple[dict, list[str]]:
    """Build a pack from a paired printer (or a profile key). Return (pack, what was left out or changed, and why)."""
    from .adapters import ADAPTERS
    from .plan import estimate as est
    paired = _paired_type_and_model(printer)
    if paired:
        adapter, model = paired
        if not model:
            raise ConfigError(f"{printer} has no model set", "Pair it again with --model, then export")
    elif printer in slicing.PRINTERS:
        model = printer
        adapter = ""
    else:
        raise ConfigError(f"no paired printer or profile named {printer!r}",
                          "Run gantrel-printer list, or use a profile key from gantrel-printer profiles")
    prof = slicing.profile_for(model, adapter)
    if prof.resin:
        raise ConfigError(f"packs hold filament printers; {prof.label} is a resin printer")
    if not adapter:
        adapter = guess_adapter(model)
    if adapter not in ADAPTERS:
        raise ConfigError(f"unknown adapter type {adapter!r}")
    pack_id = pack_id or model
    if not ID_RE.match(pack_id):
        raise ConfigError(f"pack id {pack_id!r} is not allowed", "Use 3 to 64 lowercase letters, digits and hyphens")
    left_out = []
    wanted = [slicing.material(m).name for m in materials_wanted] if materials_wanted else list(slicing.MATERIALS)
    # top_speed goes in only when the profile has one (0 means no source gives it); a pack cites what it holds.
    top = {"top_speed": prof.top_speed} if prof.top_speed else {}
    uncited = [k for k in LIMITS + tuple(top) if not prof.is_cited(k)]
    auto = (f"Exported by gantrel-printer from its {prof.key} profile and its material defaults. "
            "Calibration: Gantrel does not record calibration results yet, so calibrated is empty.")
    if uncited:
        auto += " Gantrel defaults, not printer specs: " + ", ".join(uncited) + "."
    pack = {
        "format": FORMAT, "id": pack_id, "name": name or f"{prof.label} with Gantrel's defaults",
        "version": "1.0.0", "author": author, "created": today or _dt.date.today().isoformat(),
        "notes": (notes + "\n\n" + auto).strip() if notes else auto,
        "printer": {
            "model": prof.label, "adapter": adapter, "bed": list(prof.bed), "height": prof.height,
            "nozzle": prof.nozzle, "flavor": prof.flavor, "max_speed": prof.max_speed, "accel": prof.accel,
            "max_accel": prof.max_accel, **top, "hotend_max_c": prof.hotend_max_c, "bed_max_c": prof.bed_max_c,
            "max_flow": prof.max_flow, "high_flow": prof.high_flow, "hardened_nozzle": prof.hardened_nozzle,
            "enclosed": prof.enclosed, "chamber_heater": prof.chamber_heater, "direct_drive": prof.direct_drive,
            "filament_mm": prof.filament_mm, "start_gcode": prof.start_gcode.replace("\\n", "\n"),
            "end_gcode": prof.end_gcode.replace("\\n", "\n"),
            **({"orca_machine": prof.orca_machine} if prof.orca_machine else {}),
            **({"default_ports": {adapter: ADAPTERS[adapter].default_port}}
               if adapter != "serial" and getattr(ADAPTERS[adapter], "default_port", None) else {}),
        },
        "materials": [], "proof": [],
    }
    for fam in wanted:
        m = slicing.MATERIALS[fam]
        fil = materials.get(fam)
        # A pack's fan must sit inside the cited range; slicing.py's PETG default (30-60) does not, so the pack
        # gets it clamped into the range, as plan's optimizer does, and the export says so.
        fan = [min(max(v, fil.fan_min_pct), fil.fan_max_pct) for v in (m.fan_min, m.fan_max)]
        if fan != [m.fan_min, m.fan_max]:
            left_out.append(f"Changed {fam} fan {m.fan_min}-{m.fan_max}%: it is outside the cited {fil.fan_min_pct}-"
                            f"{fil.fan_max_pct}% range, so the pack uses {fan[0]}-{fan[1]}%, as plan does")
        entry = {"family": fam, "brand": "Generic", "name": fam, "nozzle_c": m.nozzle,
                 "first_layer_nozzle_c": m.nozzle_first, "bed_c": m.bed, "first_layer_bed_c": m.bed_first,
                 "fan": fan, "max_flow": round(est.flow_cap(prof, fil), 2),
                 "retraction_mm": m.retract_mm, "retraction_speed": DEFAULT_RETRACT_SPEED, "calibrated": []}
        trial = dict(pack, materials=[entry])
        rep = Report()
        material_rules(trial, rep)
        if rep.refusals:
            why = rep.refusals[0].reason
            if materials_wanted:
                raise ConfigError(f"{fam} cannot go in a pack for {prof.label}: {why}")
            left_out.append(f"Left out {fam}: {why}")
            continue
        pack["materials"].append(entry)
    if not pack["materials"]:
        raise ConfigError(f"no material Gantrel has defaults for passes the checks on {prof.label}")
    return pack, left_out


def guess_adapter(model: str) -> str:
    """The adapter type a profile key's printers use on stock firmware (used only when exporting a profile key)."""
    for prefix, kind in (("creality-k2-plus", "moonraker"), ("creality-", "creality"), ("bambu-", "bambu"),
                         ("prusa-", "prusalink"), ("generic-klipper", "moonraker"), ("generic-marlin", "serial"),
                         ("elegoo-", "elegoo"), ("flashforge-", "flashforge"), ("snapmaker-a", "snapmaker"),
                         ("snapmaker-j", "moonraker"), ("ultimaker-", "ultimaker")):
        if model.startswith(prefix):
            return kind
    return "serial"


# ---------------------------------------------------------------- installed packs


def packs_dir() -> Path:
    return config.home() / "packs"


def _check_id(pack_id: str) -> str:
    if not ID_RE.match(pack_id or ""):
        raise ConfigError(f"pack id {pack_id!r} is not allowed", "Use the id from gantrel-printer pack list")
    return pack_id


def _private_dir(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(d, 0o700)


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


SIDECAR = ".findings-accepted.json"   # <sha>.findings-accepted.json: the findings the person accepted


def _pack_files(d: Path) -> list[Path]:
    """The installed pack files in a pack's folder, never the findings-accepted sidecars beside them."""
    return sorted(p for p in d.glob("*.json") if not p.name.endswith(SIDECAR)) if d.is_dir() else []


def accept_matches(accept: str | None, sha: str) -> bool:
    a = (accept or "").strip().lower()
    return bool(re.fullmatch(r"[0-9a-f]{12}|[0-9a-f]{64}", a)) and sha.startswith(a)


def install(data: bytes, accept: str | None, accept_findings: bool = False, replace: bool = False) -> tuple[dict, Report, Path]:
    """Check these exact bytes and install them. Raises SafetyError on any refusal."""
    pack, rep = check_bytes(data)
    sha = rep.sha256
    if not accept:
        raise SafetyError("import needs --accept with the first 12 hex digits of the pack's sha256",
                          "Run gantrel-printer pack show <file>, read it with the person, and use the accept code it prints")
    if not accept_matches(accept, sha):
        raise SafetyError(f"--accept {accept!r} does not match this file (its sha256 starts {sha[:12]})",
                          "The file is not the one that was reviewed. Run pack show on it again and review it")
    if rep.refusals or pack is None:
        first = rep.refusals[0]
        raise SafetyError(f"the pack is refused: {first.where()}: {first.reason}"
                          + (f" (and {len(rep.refusals) - 1} more)" if len(rep.refusals) > 1 else ""),
                          "Run gantrel-printer pack check <file> to see every refusal")
    if rep.findings and not accept_findings:
        raise SafetyError(f"the pack has {len(rep.findings)} finding(s) that need the person's acceptance",
                          "Read every FINDING from pack show to the person; only with their yes, add --accept-findings")
    d = packs_dir() / _check_id(pack["id"])
    others = [p for p in _pack_files(d) if p.stem != sha]
    if others and not replace:
        raise SafetyError(f"pack {pack['id']} is already installed with a different file ({others[0].stem[:12]})",
                          "Ask the person whether to replace it; only with their yes, add --replace")
    _private_dir(packs_dir())
    _private_dir(d)
    dest = d / f"{sha}.json"
    _write_private(dest, data)
    accepted = d / f"{sha}{SIDECAR}"
    if rep.findings:
        _write_private(accepted, json.dumps([i.to_dict() for i in rep.findings], indent=2).encode() + b"\n")
    elif accepted.exists():
        accepted.unlink()
    for o in others:
        o.unlink()
        side = o.with_name(f"{o.stem}{SIDECAR}")
        if side.exists():
            side.unlink()
    return pack, rep, dest


def installed() -> list[dict]:
    rows = []
    root = packs_dir()
    for d in sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []:
        for f in _pack_files(d):
            try:
                pack, sha, rep = load(d.name)
                rows.append({"id": pack["id"], "name": pack["name"], "version": pack["version"],
                             "model": pack["printer"]["model"], "sha256": sha, "accept": sha[:12],
                             "materials": [f"{m['brand']} {m['name']} ({m['family']})" for m in pack["materials"]],
                             "findings_accepted": len(rep.findings), "ok": True})
            except (ConfigError, SafetyError) as err:
                rows.append({"id": d.name, "sha256": f.stem, "accept": f.stem[:12], "ok": False, "problem": str(err)})
            break
    return rows


def load(pack_id: str) -> tuple[dict, str, Report]:
    """An installed pack, re-checked: its bytes must still match its name, pass, and have any findings accepted."""
    d = packs_dir() / _check_id(pack_id)
    files = _pack_files(d)
    if not files:
        raise ConfigError(f"no pack {pack_id!r} is installed", "Run gantrel-printer pack list, or import one first")
    if len(files) > 1:
        raise SafetyError(f"pack {pack_id} has {len(files)} files installed", f"Run gantrel-printer pack remove {pack_id}"
                                                                               " and import it again")
    data = files[0].read_bytes()
    pack, rep = check_bytes(data)
    if rep.sha256 != files[0].stem:
        raise SafetyError(f"the installed file for pack {pack_id} changed after import",
                          f"Run gantrel-printer pack remove {pack_id} and import it again after review")
    if pack is None or rep.refusals:
        raise SafetyError(f"installed pack {pack_id} no longer passes its check: {rep.refusals[0].reason}",
                          f"Run gantrel-printer pack remove {pack_id}")
    if pack["id"] != pack_id:
        raise SafetyError(f"the file installed as {pack_id} says its id is {pack['id']}")
    if rep.findings:
        side = files[0].with_name(f"{rep.sha256}{SIDECAR}")
        accepted = json.loads(side.read_text()) if side.exists() else []
        if [i.to_dict() for i in rep.findings] != accepted:
            raise SafetyError(f"pack {pack_id} has findings no one accepted",
                              "Import it again with pack import, after the person reads its findings")
    return pack, rep.sha256, rep


def remove(pack_id: str) -> bool:
    d = packs_dir() / _check_id(pack_id)
    if not d.is_dir():
        return False
    for f in d.iterdir():
        f.unlink()
    d.rmdir()
    return True


def resolve(key: str, material: str | None = None) -> tuple[slicing.PrinterProfile, slicing.Material | None]:
    """pack:<id> -> the pack's PrinterProfile, and the pack's settings for this material (family or name)."""
    pack_id = key.split(":", 1)[1]
    pack, sha, _ = load(pack_id)
    if material is None:
        prof = to_profile(pack, sha)
        return prof, None
    want = material.strip().lower()
    by_name = [i for i, m in enumerate(pack["materials"])
               if want in (m["name"].strip().lower(), f"{m['brand']} {m['name']}".strip().lower())]
    try:
        fam = materials.get(material).name
    except ConfigError:
        fam = None
    by_family = [i for i, m in enumerate(pack["materials"]) if m["family"] == fam]
    have = ", ".join(f"{m['brand']} {m['name']} ({m['family']})" for m in pack["materials"])
    if by_name:
        i = by_name[0]
    elif len(by_family) == 1:
        i = by_family[0]
    elif by_family:
        raise ConfigError(f"pack {pack_id} has {len(by_family)} {fam} settings", f"Name one with --material: {have}")
    else:
        raise ConfigError(f"pack {pack_id} has no settings for {material!r}", f"It has: {have}")
    prof = to_profile(pack, sha, chosen=i)
    return prof, prof.pack_materials[0]


# ---------------------------------------------------------------- show


def show_lines(pack: dict | None, rep: Report, data: bytes) -> list[str]:
    sha = rep.sha256 or sha256(data)
    lines = []
    marks: dict[tuple[str, int | None], list[Item]] = {}
    for i in rep.items:
        marks.setdefault((i.path, i.line), []).append(i)
    verdict = {"pass": "PASS", "findings": f"{len(rep.findings)} FINDING(S) need the person's acceptance",
               "refused": f"REFUSED ({len(rep.refusals)} refusal(s), {len(rep.findings)} finding(s))"}[rep.result]
    if pack is None or rep.private:
        lines += [f"Pack file (sha256 {sha})", f"Result: {verdict}"]
        if pack is not None:
            lines.append("Contents withheld: the pack holds private data (the refusals below say where, with it "
                         "hidden). Nothing else in it is shown until that is taken out.")
    else:
        p = pack["printer"]
        yn = {True: "yes", False: "no"}
        top = f"{p['top_speed']} mm/s" if "top_speed" in p else "not given"
        lines += [
            f"Pack {pack['id']} {shown(pack['version'])}: {shown(pack['name'])}",
            f"  author: {shown(pack['author']) or '(none given)'}   created: {pack['created']}",
            f"  sha256: {sha}",
            f"  accept code (first 12 hex of the sha256): {sha[:12]}",
            f"  result: {verdict}",
            "Notes:",
        ] + [f"  {shown(n, 400)}" for n in pack["notes"].split("\n")] + [
            f"Printer: {shown(p['model'])} (adapter {p['adapter']}, {p['flavor']} G-code)",
            f"  bed {p['bed'][0]:g} x {p['bed'][1]:g} mm, height {p['height']:g} mm, nozzle {p['nozzle']:g} mm, "
            f"filament {p['filament_mm']:g} mm",
            f"  speed cap {p['max_speed']} mm/s (printer top speed {top}), accel {p['accel']} mm/s2 (max "
            f"{p['max_accel']}), hotend flow {p['max_flow']:g} mm3/s, high flow hotend {yn[p['high_flow']]}",
            f"  hotend max {p['hotend_max_c']} C, bed max {p['bed_max_c']} C, enclosed {yn[p['enclosed']]}, chamber "
            f"heater {yn[p['chamber_heater']]}, direct drive {yn[p['direct_drive']]}, hardened nozzle "
            f"{yn[p['hardened_nozzle']]}",
        ]
        if p.get("orca_machine"):
            lines.append(f"  OrcaSlicer machine profile: {shown(p['orca_machine'])}")
        if p.get("default_ports"):
            lines.append("  default ports: " + ", ".join(f"{k} {v}" for k, v in p["default_ports"].items()))
        lines.append("Materials:")
        for i, m in enumerate(pack["materials"]):
            extra = []
            if "pressure_advance" in m:
                extra.append(f"pressure advance {m['pressure_advance']:g}")
            if "flow_ratio" in m:
                extra.append(f"flow ratio {m['flow_ratio']:g}")
            lines.append(f"  {i + 1}. {m['family']}: {shown(m['brand'])} {shown(m['name'])}: nozzle {m['nozzle_c']} C "
                         f"(first layer {m['first_layer_nozzle_c']}), bed {m['bed_c']} C (first layer "
                         f"{m['first_layer_bed_c']}), fan {m['fan'][0]}-{m['fan'][1]}%, flow {m['max_flow']:g} mm3/s, "
                         f"retraction {m['retraction_mm']:g} mm at {m['retraction_speed']:g} mm/s"
                         + (", " + ", ".join(extra) if extra else ""))
            for c in m["calibrated"]:
                lines.append(f"     calibrated {c['test']} {c['date']}: {shown(c['result'], 300)}")
            if not m["calibrated"]:
                lines.append("     calibrated: nothing recorded")
            for it in rep.items:
                if it.path.startswith(f"materials[{i}]"):
                    lines.append(f"     <- {it.level.upper()} {it.where()}: {it.reason}")
        for key in ("start_gcode", "end_gcode"):
            path = f"printer.{key}"
            lines.append(f"{'Start' if key == 'start_gcode' else 'End'} G-code ({path}), every line:")
            for n, gl in enumerate(p[key].split("\n"), 1):
                lines.append(f"  {n:4}  {shown(gl, None)}")   # whole: the person accepts every character
                for it in marks.get((path, n), []) + (marks.get((path, None), []) if n == 1 else []):
                    lines.append(f"        <- {it.level.upper()}: {it.reason}")
        lines.append("Proof (what the author says was tested; shown, not checked):")
        lines += [f"  {pr['date']} {pr['kind']}: {shown(pr['note'], 300)}" for pr in pack["proof"]] or ["  none given"]
    shown_paths = {("printer.start_gcode",), ("printer.end_gcode",)}
    listed = pack is not None and not rep.private
    rest = [i for i in rep.items if not (listed and (i.path.startswith("materials[") or (i.path,) in shown_paths))]
    if rest:
        lines.append("Other results:")
        lines += [f"  {i.level.upper()} {i.where()}: {i.reason}" + (f"  [{i.text}]" if i.text else "") for i in rest]
    return lines
