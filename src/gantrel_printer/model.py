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
