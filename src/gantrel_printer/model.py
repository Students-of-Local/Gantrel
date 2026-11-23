"""Shared data shapes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

# Normalized states every adapter maps to.
STATES = ("idle", "printing", "paused", "complete", "cancelled", "error", "busy", "offline")
# States in which a new print may start.
READY_STATES = ("idle", "complete", "cancelled")


@dataclass
class Status:
    printer: str
    type: str
    state: str
    file: str = ""
    progress: float | None = None  # 0.0 to 1.0
    nozzle_c: float | None = None
    nozzle_target_c: float | None = None
    bed_c: float | None = None
    bed_target_c: float | None = None
    chamber_c: float | None = None
    elapsed_s: float | None = None
    remaining_s: float | None = None
    layer: int | None = None
    total_layers: int | None = None
    message: str = ""
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.state not in STATES:
            raise ValueError(f"unknown state {self.state!r}")

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        parts = [f"{self.printer} ({self.type}): {self.state}"]
        if self.file:
            parts.append(f"file {self.file}")
        if self.progress is not None:
            parts.append(f"{self.progress * 100:.0f}%")
        if self.layer is not None and self.total_layers:
            parts.append(f"layer {self.layer}/{self.total_layers}")
        temps = []
        for label, cur, tgt in (("nozzle", self.nozzle_c, self.nozzle_target_c),
                                ("bed", self.bed_c, self.bed_target_c),
                                ("chamber", self.chamber_c, None)):
            if cur is not None:
                temps.append(f"{label} {cur:.0f}C" + (f"/{tgt:.0f}C" if tgt else ""))
        if temps:
            parts.append(", ".join(temps))
        if self.remaining_s:
            parts.append(f"{fmt_duration(self.remaining_s)} left")
        if self.message:
            parts.append(f"message: {self.message}")
        return printable("; ".join(parts))


@dataclass
class Camera:
    snapshot_url: str = ""
    stream_url: str = ""  # never contains a secret


# Material families a slot reports. "other" is a material the printer names that is not one of these;
# "unknown" means the printer did not say (or sent a code Gantrel has no entry for).
FAMILIES = ("PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC", "PLA-CF", "PETG-CF", "PVA", "HIPS", "other", "unknown")
# Names printers use for a family. Nylon variants (PA6, PA-CF) are the PA family; the slot's material
# field keeps the printer's own word, so an agent still sees "PA-CF".
_FAMILY_ALIASES = {
    "PLA+": "PLA", "PLA-PLUS": "PLA", "PLA-BASIC": "PLA", "PLA-MATTE": "PLA", "PLA-SILK": "PLA",
    "PLA-HS": "PLA", "HYPER-PLA": "PLA", "PLACF": "PLA-CF", "PETGCF": "PETG-CF", "PET-G": "PETG",
    "TPU-95A": "TPU", "TPU-90A": "TPU", "TPU-85A": "TPU", "TPU-AMS": "TPU",
    "PA6": "PA", "PA12": "PA", "PAHT": "PA", "PA-CF": "PA", "PA6-CF": "PA", "PA12-CF": "PA", "PAHT-CF": "PA",
    "PA6-GF": "PA", "NYLON": "PA",
}
TEXT_MAX = 64  # every string a printer sends is cut to this
# A loaded-filament reply is a few kilobytes; refuse anything far bigger before parsing it.
FILAMENT_REPLY_MAX = 1024 * 1024


def clip(value, n: int = TEXT_MAX) -> str:
    """A printer's string, made printable and cut to n characters. Non-strings become ""."""
    if not isinstance(value, str):
        return ""
    return printable(value.strip())[:n]


def hex_color(value) -> str:
    """'#rrggbb' from 'rrggbb' or '#rrggbb', or "" when it is not a 6-digit hex color."""
    if not isinstance(value, str):
        return ""
    v = value.strip().lstrip("#")
    if len(v) != 6 or any(c not in "0123456789abcdefABCDEF" for c in v):
        return ""
    return "#" + v.lower()


def bounded(value, lo: float, hi: float) -> float | None:
    """A number (or numeric string) inside [lo, hi], else None. Booleans, NaN and infinities are None."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, str) and len(value) > 32:
        return None
    try:
        n = float(value)
    except ValueError:
        return None
    if n != n or not lo <= n <= hi:
        return None
    return n


def material_family(text) -> str:
    """The family for a printer's material word: 'PLA', 'PETG-CF', 'other', or 'unknown' when blank."""
    t = clip(text).upper().replace("_", "-").replace(" ", "-")
    if not t or t in ("-1", "UNKNOWN", "NONE", "?"):
        return "unknown"
    if t in FAMILIES[:-2]:
        return t
    return _FAMILY_ALIASES.get(t, "other")


@dataclass
class Slot:
    """One place filament feeds from: a CFS or AMS slot, or the external spool holder ("ext").

    Every field comes from the printer, so __post_init__ cleans it: strings cut to 64 printable
    characters, colors checked as hex, numbers kept only inside a sane range.
    """

    slot: str
    empty: bool
    family: str = "unknown"
    material: str = ""  # the printer's own word or code for the material, such as "PLA" or "008001"
    brand: str = ""
    name: str = ""  # product name, such as "PLA Basic"
    color: str = ""  # "#rrggbb" or ""
    percent_left: float | None = None
    grams_left: float | None = None
    nozzle_min_c: float | None = None
    nozzle_max_c: float | None = None
    source: str = ""  # which API field this came from
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        self.slot = clip(self.slot, 16) or "?"
        self.empty = bool(self.empty)
        self.family = self.family if self.family in FAMILIES else "unknown"
        for name in ("material", "brand", "name", "source"):
            setattr(self, name, clip(getattr(self, name)))
        self.color = hex_color(self.color)
        self.percent_left = bounded(self.percent_left, 0, 100)
        self.grams_left = bounded(self.grams_left, 0, 100_000)
        self.nozzle_min_c = bounded(self.nozzle_min_c, 0, 500)
        self.nozzle_max_c = bounded(self.nozzle_max_c, 0, 500)
        if None in (self.nozzle_min_c, self.nozzle_max_c) or self.nozzle_min_c > self.nozzle_max_c:
            self.nozzle_min_c = self.nozzle_max_c = None
        extra = {}
        for k, v in list((self.extra or {}).items())[:8]:
            if not isinstance(k, str):
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                if bounded(v, -1e12, 1e12) is not None:  # NaN and infinity would break --json
                    extra[clip(k, 32)] = v
            else:
                extra[clip(k, 32)] = clip(v)
        self.extra = extra

    def to_dict(self) -> dict:
        return asdict(self)


def printable(text: str) -> str:
    """Replace control and other non-printable characters (escape codes, newlines) with '?'."""
    return "".join(c if c.isprintable() else "?" for c in str(text))


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"
