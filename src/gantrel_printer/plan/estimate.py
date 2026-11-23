"""Gantrel's own time and material estimate for an oriented part, and a reader for slicer G-code headers.

The estimate is a model, not a slice. It splits the part into walls, top and bottom skins, infill and
supports by volume, turns each into path length (volume / (layer height x line width)), and divides by a
speed limited three ways: the speed set for that feature, the volumetric flow cap, and acceleration
over a typical move. Then it adds the slowdown for layers shorter than the material's minimum layer time.
It reports a low and a high figure. A slicer's number is better; parse_slicer_gcode reads it.
"""

from __future__ import annotations

import math
import re

import numpy as np

from .. import materials
from ..slicing import PrinterProfile, pack_material
from . import geometry
from .mesh import Mesh

FLOW_MARGIN = 0.9            # plan at 90 percent of the volumetric flow cap
SKIN_NORMAL_Z = math.sin(math.radians(45))   # facets flatter than 45 degrees are covered by top and bottom skins
SUPPORT_FILL = 0.15          # support body density, a typical slicer default
SUPPORT_INTERFACE_LAYERS = 2
SEGMENT_MM = {"wall": 20.0, "skin": 10.0, "infill": 10.0, "support": 10.0}   # typical straight move per feature
TRAVEL_FRACTION = 0.10       # travel and retraction add about this share of extrusion time
LAYER_CHANGE_S = 0.5         # z move, wipe and seam per layer
LOW_FACTOR, HIGH_FACTOR = 0.85, 1.25   # the band around the central figure
DEFAULT_LINE_WIDTH = 1.125   # x nozzle, PrusaSlicer's and Orca's usual default (0.45 mm on a 0.4 mm nozzle)
PROFILE_LAYERS = 400         # at most this many heights are sampled for the per-layer pass


def line_width(s: dict, key: str = "line_width_mm") -> float:
    return float(s.get(key) or s.get("line_width_mm") or DEFAULT_LINE_WIDTH * float(s.get("nozzle_mm", 0.4)))


def flow_limit(printer: PrinterProfile, fil: materials.Filament) -> dict:
    """The volumetric flow (mm3/s) the plan may use, and where it came from.

    A profile built from a printer pack uses the pack's own max_flow for the filament, as is.
    When the printer's vendor ships an OrcaSlicer preset for this filament family on this printer
    (data/printer_material_flow.json), its max volumetric speed is used as is: the vendor tuned it on
    that hotend. Otherwise Gantrel's rule: FLOW_MARGIN x the lower of the hotend and the material column.
    """
    pm = pack_material(printer, fil.name)
    if pm is not None and pm.max_flow:
        # A printer pack's own number for this filament on this printer, used as is like a vendor preset.
        return {"mm3s": float(pm.max_flow), "source": f"{printer.source}: {pm.label}"}
    v = materials.vendor_flow(printer.key, fil.name)
    if v:
        return {"mm3s": float(v["mm3s"]), "source": f"vendor preset {v['preset']}", "preset": v["preset"],
                "url": v["url"]}
    hot, mat = printer.max_flow, fil.max_flow(printer.high_flow)
    return {"mm3s": FLOW_MARGIN * min(hot, mat),
            "source": f"Gantrel default: {FLOW_MARGIN} x min(hotend {hot}, {fil.name} {mat})"
                      + ("" if printer.is_cited("max_flow") else ", hotend value is Gantrel's default"),
            "hotend_mm3s": hot, "material_mm3s": mat}


def flow_cap(printer: PrinterProfile, fil: materials.Filament) -> float:
    return flow_limit(printer, fil)["mm3s"]


def flow_of(lh: float, lw: float, speed: float) -> float:
    return lh * lw * speed


def move_speed(v: float, accel: float, seg_mm: float) -> float:
    """Average speed over a move of seg_mm that starts and ends at rest (trapezoid, or triangle when short)."""
    if v <= 0:
        return 0.0
    if accel <= 0:
        return v
    if seg_mm >= v * v / accel:
        t = seg_mm / v + v / accel
    else:
        t = 2 * math.sqrt(seg_mm / accel)
    return seg_mm / t


def supports_on(s: dict) -> bool:
    v = s.get("supports", True)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower().split(",")[0] not in ("off", "none", "no", "false", "0", "")


def layer_profile(m: Mesh, lh: float, first: float, columns: int = geometry.COLUMNS) -> tuple[np.ndarray, np.ndarray]:
    """Heights at the middle of each layer (thinned to PROFILE_LAYERS) and the part's cross-section there."""
    h = float(m.size[2])
    n = max(1, int(math.ceil((h - first) / lh)) + 1) if h > first else 1
    z = np.concatenate([[first / 2], first + (np.arange(1, n) - 0.5) * lh]) if n > 1 else np.array([first / 2])
    z = z[z < h]
    if len(z) == 0:
        z = np.array([h / 2])
    if len(z) > PROFILE_LAYERS:
        z = z[np.linspace(0, len(z) - 1, PROFILE_LAYERS).astype(int)]
    return z, geometry.layer_areas(m.tris, z, columns)


def layer_seconds(area: float, s: dict, printer: PrinterProfile, fil: materials.Filament) -> float:
    """Time for one layer with this cross-section. The outline is taken as a square of the same area
    (4 x sqrt(area)), which is the shortest outline a part of that area plausibly has, so this errs fast,
    and a layer flagged too fast here is at least that fast."""
    if area <= 0:
        return 0.0
    lh, lw = float(s["layer_height_mm"]), line_width(s)
    cap = flow_cap(printer, fil)
    perim = 4 * math.sqrt(area)
    walls = int(s.get("perimeters", 2))
    vw = min(float(s["outer_wall_speed_mms"]), cap / (lh * lw))
    vi = min(float(s["infill_speed_mms"]), cap / (lh * lw))
    accel = float(s.get("accel_mms2") or printer.accel)
    wall_path = walls * perim
    fill_area = max(0.0, area - wall_path * lw)
    fill_path = fill_area / lw * float(s.get("infill_percent", 15)) / 100
    return (wall_path / move_speed(vw, accel, min(SEGMENT_MM["wall"], perim / 4))
            + (fill_path / move_speed(vi, accel, SEGMENT_MM["infill"]) if fill_path else 0.0))


def estimate(m: Mesh, s: dict, printer: PrinterProfile, fil: materials.Filament,
             support_volume_mm3: float = 0.0, support_contact_mm2: float = 0.0,
             profile: tuple[np.ndarray, np.ndarray] | None = None) -> dict:
    """Time and material for the part as it sits on the bed (z up), with these settings."""
    lh, flh = float(s["layer_height_mm"]), float(s["first_layer_height_mm"])
    lw = line_width(s)
    walls = int(s["perimeters"])
    top, bottom = int(s["top_layers"]), int(s["bottom_layers"])
    infill = float(s["infill_percent"]) / 100
    nz, areas = m.normals[:, 2], m.areas
    vol = abs(m.volume)
    side = float(areas[np.abs(nz) < SKIN_NORMAL_Z].sum())
    up = float((areas * np.clip(nz, 0, None))[nz >= SKIN_NORMAL_Z].sum())
    down = float((areas * np.clip(-nz, 0, None))[nz <= -SKIN_NORMAL_Z].sum())
    shell = min(vol, side * walls * lw)
    skin = min(vol - shell, up * top * lh + down * bottom * lh)
    core = max(0.0, vol - shell - skin)
    fill = core * infill
    sup = 0.0
    if supports_on(s) and support_volume_mm3 > 0:
        sup = support_volume_mm3 * SUPPORT_FILL + support_contact_mm2 * SUPPORT_INTERFACE_LAYERS * lh
    parts = {"wall": shell, "skin": skin, "infill": fill, "support": sup}
    speed = {"wall": (float(s["outer_wall_speed_mms"]) + (walls - 1) * float(s["inner_wall_speed_mms"])) / max(walls, 1),
             "skin": float(s.get("top_speed_mms") or s["infill_speed_mms"]),
             "infill": float(s["infill_speed_mms"]),
             "support": float(s.get("support_speed_mms") or s["infill_speed_mms"])}
    cap = flow_cap(printer, fil)
    accel = float(s.get("accel_mms2") or printer.accel)
    seconds = {}
    flow_limited = []
    for k, v in parts.items():
        cap_speed = cap / (lh * lw)
        if speed[k] > cap_speed:
            flow_limited.append(k)
        vv = min(speed[k], cap_speed)
        seconds[k] = (v / (lh * lw)) / move_speed(vv, accel, SEGMENT_MM[k]) if v else 0.0
    z, layer_area = profile if profile is not None else layer_profile(m, lh, flh)
    h = float(m.size[2])
    layers = max(1, int(math.ceil(max(0.0, h - flh) / lh)) + 1)
    first_area = float(layer_area[0]) if len(layer_area) else 0.0
    first_s = first_area / lw / max(1e-6, float(s["first_layer_speed_mms"]))
    extrude = sum(seconds.values()) + first_s
    # Split the extrusion time across layers in proportion to each layer's own time (layer_seconds), then
    # slow every layer that is faster than the material's minimum layer time up to it.
    shape = np.array([layer_seconds(float(a), s, printer, fil) for a in layer_area])
    scale = layers / max(1, len(shape))
    per = extrude * shape / max(shape.sum() * scale, 1e-9) if shape.sum() > 0 else shape
    short = per < fil.min_layer_time_s
    pad = float(np.clip(fil.min_layer_time_s - per, 0, None).sum() * scale)
    total = extrude * (1 + TRAVEL_FRACTION) + layers * LAYER_CHANGE_S + pad
    extruded = sum(parts.values())
    fil_area = math.pi * (float(s.get("filament_mm", printer.filament_mm)) / 2) ** 2
    return {
        "source": "Gantrel's estimate from the mesh, not the slicer's",
        "seconds": round(total), "low_seconds": round(total * LOW_FACTOR), "high_seconds": round(total * HIGH_FACTOR),
        "layers": layers,
        "volume_mm3": {k: round(v, 1) for k, v in parts.items()} | {"total": round(extruded, 1)},
        "seconds_by_feature": {k: round(v, 1) for k, v in seconds.items()}
        | {"first_layer": round(first_s, 1), "min_layer_time_padding": round(pad, 1)},
        "filament_m": round(extruded / fil_area / 1000, 3),
        "filament_g": round(extruded / 1000 * fil.density_g_cm3, 2),
        "density_g_cm3": fil.density_g_cm3,
        "flow_cap_mm3s": round(cap, 2), "flow_cap_source": flow_limit(printer, fil)["source"],
        "flow_limited": flow_limited,
        "layers_below_min_layer_time": int(short.sum() * scale),
        "smallest_layer_mm2": round(float(layer_area.min()) if len(layer_area) else 0.0, 2),
        "smallest_layer_seconds": round(float(per.min()) if len(per) else 0.0, 2),
    }


# ---------------------------------------------------------------- slicer G-code

_GEN_RE = re.compile(r"^;\s*generated by\s+(\S+)\s+(\S+)", re.I | re.M)
_TOTAL_TIME_RE = re.compile(r"total estimated time\s*[:=]\s*([0-9dhms ]+)", re.I)
_NORMAL_TIME_RE = re.compile(r"^;\s*estimated printing time \(normal mode\)\s*=\s*([0-9dhms ]+)", re.I | re.M)
_FIRST_LAYER_RE = re.compile(r"^;\s*estimated first layer printing time \(normal mode\)\s*=\s*([0-9dhms ]+)", re.I | re.M)
_MODEL_TIME_RE = re.compile(r"model printing time\s*[:=]\s*([0-9dhms ]+)", re.I)
_USED_RE = re.compile(r"^;\s*(total )?filament used \[(mm|cm3|g)\]\s*=\s*([0-9., ]+)$", re.I | re.M)


def duration(text: str) -> float | None:
    total, found = 0.0, False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", text):
        total += float(num) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
        found = True
    return total if found else None


def parse_slicer_gcode(text: str) -> dict:
    """Time and filament from PrusaSlicer or OrcaSlicer comments. Empty dict when there are none.

    PrusaSlicer writes "; estimated printing time (normal mode) = 1h 2m 3s" and "; filament used [g] = ..."
    at the end. OrcaSlicer writes the same time line in its header block for most printers, and
    "; model printing time: ...; total estimated time: ..." for Bambu printers. Lists (one value per
    extruder) are summed.
    """
    out: dict = {}
    m = _GEN_RE.search(text)
    if m:
        out["slicer"], out["slicer_version"] = m.group(1), m.group(2)
    m = _TOTAL_TIME_RE.search(text) or _NORMAL_TIME_RE.search(text)
    if m:
        out["seconds"] = duration(m.group(1))
    m = _MODEL_TIME_RE.search(text)
    if m:
        out["model_seconds"] = duration(m.group(1))
    m = _FIRST_LAYER_RE.search(text)
    if m:
        out["first_layer_seconds"] = duration(m.group(1))
    for total, unit, vals in _USED_RE.findall(text):
        nums = [float(v) for v in vals.replace(" ", "").split(",") if v]
        key = {"mm": "filament_mm", "cm3": "filament_cm3", "g": "filament_g"}[unit.lower()]
        if total or key not in out:
            out[key] = round(sum(nums), 3)
    if out.get("seconds") is None:
        out.pop("seconds", None)
    if out and "slicer" not in out:
        out["slicer"] = "unknown"
    return out
