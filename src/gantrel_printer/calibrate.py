"""Calibration prints: first layer, temperature tower, retraction tower and flow patches.

Each one is plain G-code made here from the printer's slicing profile (bed size, nozzle, filament
diameter, start and end G-code) and the material's defaults, so no slicer has to be installed and
every value is known. The shapes are simple on purpose: flat squares, two hollow pillars, flat
patches. Every band or patch is listed in the G-code as a "; GANTREL_BAND" comment, so the file
says what each part tests.

Making a calibration print never sends or starts anything. Upload is a separate, explicit step.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError, Unsupported
from .model import fmt_duration
from .slicing import Material, PrinterProfile, settings

KINDS = ("first-layer", "temp-tower", "retraction", "flow")

DEFAULT_START = ("M140 S{first_layer_bed_temperature[0]}\\nM104 S150\\nG28\\nM190 S{first_layer_bed_temperature[0]}\\n"
                 "M109 S{first_layer_temperature[0]}\\nG92 E0\\nG1 Z0.3 F3000\\nG1 X5 Y20 F6000\\n"
                 "G1 X5 Y120 E10 F1200\\nG92 E0")
DEFAULT_END = "M104 S0\\nM140 S0\\nG91\\nG1 Z10 F600\\nG90\\nM107\\nM84"

LAYER = 0.2           # every calibration print uses 0.2 mm layers
BAND_MM = 5.0         # each tower band is 5 mm tall
TEMP_STEP = 5         # temp-tower: degrees C per band
FLOWS = (90, 95, 100, 105, 110)  # flow patches, percent
TRAVEL_RETRACT_MIN_MM = 2.0      # shorter moves don't retract


@dataclass
class Band:
    index: int
    value: float
    unit: str
    where: str            # Z range for towers, position for patches

    def label(self) -> str:
        v = f"{self.value:g}"
        return f"{v} {self.unit}" if self.unit != "%" else f"{v}%"


@dataclass
class Calibration:
    kind: str
    gcode: str
    bands: list[Band]
    summary: str
    look_for: str
    change: str
    est_seconds: float
    extra: dict = field(default_factory=dict)


def retract_steps(printer: PrinterProfile) -> list[float]:
    """Retraction distances to try: 0.2 to 2.0 mm on direct drive, 1.0 to 6.0 mm on Bowden."""
    if printer.direct_drive:
        return [round(0.2 * i, 1) for i in range(1, 11)]
    return [round(1.0 + 0.5 * i, 1) for i in range(0, 11)]


def temp_steps(mat: Material) -> list[int]:
    """Hottest at the bottom (it sticks best), down to the coolest, TEMP_STEP apart."""
    lo, hi = mat.nozzle_range
    return list(range(hi, lo - 1, -TEMP_STEP))


class _Writer:
    """Absolute XYZ, relative E (M83). Tracks position, filament and time."""

    def __init__(self, printer: PrinterProfile, s: dict):
        self.p, self.s = printer, s
        self.lines: list[str] = []
        self.x = self.y = None
        self.z = 0.0
        self.width = round(printer.nozzle * 1.125, 3)     # 0.45 mm lines from a 0.4 mm nozzle
        self.fil_area = math.pi * (printer.filament_mm / 2) ** 2
        self.retract = s["retract_mm"]
        self.seconds = 0.0
        self.travel_f = min(s["travel_speed_mms"], 150) * 60

    def add(self, line: str) -> None:
        self.lines.append(line)

    def e_for(self, length: float, height: float, flow: float) -> float:
        # Slic3r's extrusion model: a rectangle with round sides.
        area = (self.width - height) * height + math.pi * (height / 2) ** 2
        return length * area * flow / self.fil_area

    def layer(self, z: float) -> None:
        self.z = round(z, 3)
        self.add(f"G1 Z{self.z:.3f} F600")
        self.seconds += 0.5

    def travel(self, x: float, y: float) -> None:
        dist = math.hypot(x - self.x, y - self.y) if self.x is not None else 99.0
        do_retract = dist >= TRAVEL_RETRACT_MIN_MM and self.retract > 0
        if do_retract:
            self.add(f"G1 E-{self.retract:.2f} F2400")
        self.add(f"G0 X{x:.3f} Y{y:.3f} F{self.travel_f:.0f}")
        if do_retract:
            self.add(f"G1 E{self.retract:.2f} F2400")
        self.seconds += dist / (self.travel_f / 60) + (0.2 if do_retract else 0)
        self.x, self.y = x, y

    def line_to(self, x: float, y: float, speed: float, flow: float = 1.0) -> None:
        dist = math.hypot(x - self.x, y - self.y)
        e = self.e_for(dist, LAYER, flow)
        self.add(f"G1 X{x:.3f} Y{y:.3f} E{e:.5f} F{speed * 60:.0f}")
        self.seconds += dist / speed
        self.x, self.y = x, y

    def loops(self, x0: float, y0: float, x1: float, y1: float, n: int, speed: float, flow: float = 1.0) -> None:
        """n perimeters inside the rectangle (x0, y0)-(x1, y1), outermost first."""
        for i in range(n):
            d = self.width * (i + 0.5)
            a, b, c, e = x0 + d, y0 + d, x1 - d, y1 - d
            self.travel(a, b)
            for px, py in ((c, b), (c, e), (a, e), (a, b)):
                self.line_to(px, py, speed, flow)

    def fill(self, x0: float, y0: float, x1: float, y1: float, speed: float, flow: float = 1.0,
             vertical: bool = False) -> None:
        """Solid zigzag fill of the rectangle, lines one width apart."""
        w = self.width
        if vertical:
            xs = _steps(x0 + w / 2, x1 - w / 2, w)
            self.travel(xs[0], y0 + w / 2)
            for i, x in enumerate(xs):
                ya, yb = (y0 + w / 2, y1 - w / 2) if i % 2 == 0 else (y1 - w / 2, y0 + w / 2)
                if i:
                    self.line_to(x, ya, speed, flow)
                self.line_to(x, yb, speed, flow)
        else:
            ys = _steps(y0 + w / 2, y1 - w / 2, w)
            self.travel(x0 + w / 2, ys[0])
            for i, y in enumerate(ys):
                xa, xb = (x0 + w / 2, x1 - w / 2) if i % 2 == 0 else (x1 - w / 2, x0 + w / 2)
                if i:
                    self.line_to(xa, y, speed, flow)
                self.line_to(xb, y, speed, flow)


def _steps(a: float, b: float, step: float) -> list[float]:
    n = max(1, int((b - a) / step) + 1)
    return [a + i * step for i in range(n)]


def _expand(template: str, s: dict, nozzle_first: int) -> list[str]:
    text = (template.replace("{first_layer_temperature[0]}", str(nozzle_first))
            .replace("{first_layer_bed_temperature[0]}", str(s["bed_first_layer_c"]))
            .replace("{chamber_temperature[0]}", str(s["chamber_c"])))
    return text.replace("\\n", "\n").splitlines()


def _fan(w: _Writer, percent: int) -> None:
    w.add(f"M106 S{round(255 * percent / 100)}" if percent else "M107")


def make(kind: str, printer: PrinterProfile, mat: Material, force: bool = False) -> Calibration:
    if kind not in KINDS:
        raise ConfigError(f"unknown calibration {kind!r}", f"Use one of: {', '.join(KINDS)}")
    if printer.resin:
        raise Unsupported(f"Gantrel makes calibration prints only for filament printers, not the {printer.label}",
                          "Use the resin maker's exposure test (for example the Elegoo or CHITUBOX test file)")
    s = settings(printer, mat, "standard", force=force)
    w = _Writer(printer, s)
    bw, bd = printer.bed
    cx, cy = bw / 2, bd / 2
    first_speed = s["first_layer_speed_mms"]
    wall_speed = min(s["outer_wall_speed_mms"], 40)    # small parts: slow enough to cool each layer
    fill_speed = min(s["infill_speed_mms"], 60)
    fan = s["fan_percent"][1]
    nozzle_first = s["nozzle_first_layer_c"]
    bands: list[Band] = []

    if kind == "temp-tower":
        temps = temp_steps(mat)
        nozzle_first = temps[0]

    start = _expand(printer.start_gcode or DEFAULT_START, s, nozzle_first)
    w.add("; START")
    w.lines += start
    w.add("; BODY")
    w.add("G90")
    w.add("M83")
    w.add("G92 E0")
    w.add("M107")

    if kind == "first-layer":
        size = min(30.0, bw / 8, bd / 8)
        inset = max(20.0, size)
        spots = [("center", cx, cy), ("front left", inset, inset), ("front right", bw - inset, inset),
                 ("back left", inset, bd - inset), ("back right", bw - inset, bd - inset)]
        w.layer(LAYER)
        for i, (name, x, y) in enumerate(spots, 1):
            x0, y0, x1, y1 = x - size / 2, y - size / 2, x + size / 2, y + size / 2
            w.add(f"; GANTREL_BAND index={i} value={size:g} unit=mm where={name}")
            w.loops(x0, y0, x1, y1, 2, first_speed)
            m = 2 * w.width
            w.fill(x0 + m, y0 + m, x1 - m, y1 - m, first_speed)
            bands.append(Band(i, size, "mm square", name))
        summary = (f"Five {size:g} mm squares, one layer ({LAYER} mm), at the center and near each corner of the "
                   f"{bw} x {bd} mm bed.")
        look_for = ("Look at each square. Good: flat, smooth, lines fused together with no gaps. Too high: round "
                    "separate lines with gaps between them, or a square that peels off. Too low: rough ridges, "
                    "thin see-through spots, or plastic piled up at the edges.")
        if "bambu" in printer.key:
            change = ("Bambu printers set the nozzle height themselves: if a square is off, clean the plate with "
                      "dish soap and water and run Calibration, Auto bed leveling on the printer's screen")
        elif printer.flavor == "klipper":
            change = ("Change the Z offset: too high, lower it 0.02 to 0.05 mm (closer to the bed); too low, raise it. "
                      "If squares differ from each other, run the bed mesh (BED_MESH_CALIBRATE) and print again")
        else:
            change = ("Change the Z offset (live adjust Z or babystep on the printer's screen): too high, lower it "
                      "0.02 to 0.05 mm; too low, raise it. If squares differ from each other, level the bed or run "
                      "its auto bed leveling and print again")
    else:
        # Towers and patches stand on a two layer solid base.
        if kind == "flow":
            patch, gap = 20.0, 5.0
            n = len(FLOWS)
            total = n * patch + (n - 1) * gap
            x_start = cx - total / 2
            spots = [(x_start + i * (patch + gap), cy - patch / 2) for i in range(n)]
            layers = 5  # 1 mm tall: three solid layers under a top surface the eye can judge
            for layer in range(layers):
                z = LAYER * (layer + 1)
                w.layer(z)
                if layer == 1:
                    _fan(w, fan)
                speed = first_speed if layer == 0 else fill_speed
                for i, (x0, y0) in enumerate(spots):
                    flow = FLOWS[i] / 100
                    if layer == 0:
                        w.add(f"; GANTREL_BAND index={i + 1} value={FLOWS[i]} unit=% where=patch {i + 1} from "
                              f"the left, X {x0:.1f} to {x0 + patch:.1f}")
                    w.loops(x0, y0, x0 + patch, y0 + patch, 1, speed if layer == 0 else wall_speed, flow)
                    m = w.width
                    w.fill(x0 + m, y0 + m, x0 + patch - m, y0 + patch - m, speed, flow, vertical=layer % 2 == 1)
                if layer == 0:
                    # A small tab in front of the first patch marks the left end.
                    x0, y0 = spots[0]
                    w.loops(x0, y0 - 6, x0 + 5, y0 - 1, 2, first_speed)
            bands = [Band(i + 1, f, "%", f"patch {i + 1} from the left") for i, f in enumerate(FLOWS)]
            summary = (f"Five {patch:g} mm flat patches in a row, {layers * LAYER:g} mm tall, printed at "
                       f"{', '.join(f'{f}%' for f in FLOWS)} flow from left to right. A small tab marks the left "
                       f"({FLOWS[0]}%) patch.")
            look_for = ("Look at the top of each patch in good light. Too little flow: gaps or grooves between "
                        "lines. Too much: ridges, rough bumps or a raised edge. Pick the smoothest patch with no gaps")
            change = ("Multiply your flow (extrusion multiplier) by that patch's percent: for example 105% means "
                      "flow 1.00 becomes 1.05. If two look the same, choose the lower one")
        else:
            pillar, gap = 15.0, 30.0
            ax0 = cx - gap / 2 - pillar
            bx0 = cx + gap / 2
            y0 = cy - pillar / 2
            base = (ax0 - 4, y0 - 4, bx0 + pillar + 4, y0 + pillar + 4)
            if kind == "temp-tower":
                values, unit = temps, "C"
                w.add(f"M109 S{values[0]}")
            else:
                values, unit = retract_steps(printer), "mm"
            for layer in range(2):
                w.layer(LAYER * (layer + 1))
                w.loops(*base, 1, first_speed)
                m = w.width
                w.fill(base[0] + m, base[1] + m, base[2] - m, base[3] - m, first_speed, vertical=layer == 1)
            _fan(w, fan)
            z = 2 * LAYER
            per_band = round(BAND_MM / LAYER)
            for i, v in enumerate(values):
                z0 = z
                where = f"Z {z0:.1f} to {z0 + BAND_MM:.1f} mm"
                w.add(f"; GANTREL_BAND index={i + 1} value={v:g} unit={unit} where={where}")
                if kind == "temp-tower":
                    w.add(f"M104 S{v}")
                else:
                    w.retract = v
                bands.append(Band(i + 1, v, unit, where))
                for _ in range(per_band):
                    z = round(z + LAYER, 3)
                    w.layer(z)
                    w.loops(ax0, y0, ax0 + pillar, y0 + pillar, 2, wall_speed)
                    w.loops(bx0, y0, bx0 + pillar, y0 + pillar, 2, wall_speed)
            if z > printer.height:
                raise ConfigError(f"the {kind} is {z:.0f} mm tall and the {printer.label} prints only "
                                  f"{printer.height} mm", "Use a printer with more height")
            if kind == "temp-tower":
                summary = (f"Two hollow {pillar:g} mm pillars, {len(values)} bands of {BAND_MM:g} mm, from "
                           f"{values[0]} C at the bottom down to {values[-1]} C at the top, {TEMP_STEP} C per band, "
                           f"on a {2 * LAYER:g} mm base.")
                look_for = ("Look at each band: smooth walls, sharp corners, little stringing between the pillars, "
                            "and layers that don't split when you squeeze or try to snap the top. Too hot: strings, "
                            "blobs and droopy corners. Too cold: rough or dull walls and weak, splitting layers")
                change = (f"Set the nozzle temperature to the best band's value (Gantrel's {mat.name} default is "
                          f"{mat.nozzle} C; in a slicer it is the filament's nozzle temperature). Band 1 is the "
                          "bottom one")
            else:
                summary = (f"Two hollow {pillar:g} mm pillars, {len(values)} bands of {BAND_MM:g} mm. Every travel "
                           f"between them retracts {values[0]:g} mm at the bottom, up to {values[-1]:g} mm at the "
                           f"top, at {s['nozzle_c']} C.")
                look_for = ("Look between the pillars, from the bottom up. Find the lowest band with no strings or "
                            "fine hairs between them")
                change = (f"Set the retraction length to that band's value (Gantrel's {mat.name} default is "
                          f"{mat.retract_mm:g} mm; in a slicer it is retraction length). If every band strings, "
                          "print a temp-tower first: the nozzle may be too hot")

    w.add("; END")
    w.lines += _expand(printer.end_gcode or DEFAULT_END, s, nozzle_first)
    est = w.seconds + 240  # plus heating and homing
    head = [f"; generated by gantrel-printer calibrate {kind}",
            f"; printer_model = {printer.key}",
            f"; filament_type = {mat.name}",
            f"; nozzle_diameter = {printer.nozzle}",
            f"; layer_height = {LAYER}",
            f"; retract_length = {s['retract_mm']}",
            f"; estimated printing time (normal mode) = {_hms(est)}"]
    gcode = "\n".join(head + w.lines) + "\n"
    return Calibration(kind, gcode, bands, summary, look_for, change, est,
                       extra={"nozzle_c": s["nozzle_c"], "bed_c": s["bed_c"]})


def _hms(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return (f"{h}h " if h else "") + f"{m}m {s}s"


def file_name(kind: str, printer: PrinterProfile, mat: Material) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", f"calibrate-{kind}_{printer.key}_{mat.name}") + ".gcode"


def save(cal: Calibration, out_dir: Path, name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    path.write_text(cal.gcode)
    return path


def describe(cal: Calibration) -> list[str]:
    lines = [cal.summary]
    if cal.kind != "first-layer":
        lines.append("Bands, first is the " + ("left patch:" if cal.kind == "flow" else "bottom:"))
        lines += [f"  {b.index}. {b.label()}  ({b.where})" for b in cal.bands]
    lines.append(f"Estimated time: {fmt_duration(cal.est_seconds)}")
    return lines
