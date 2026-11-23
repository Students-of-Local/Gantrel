"""Pick settings for a part: quality first, then as fast as the printer and filament allow under it.

The goal sets the quality floor (layer height, outer wall speed, walls). Everything that does not
show on the surface (inner walls, infill, supports) then runs at the highest speed the flow cap,
the printer's top speed and acceleration allow. The result always passes check(); a printer and
filament pair that cannot pass is refused with the rules that failed. Only a cited printer limit refuses:
past one of Gantrel's defaults the plan carries a warning instead.
"""

from __future__ import annotations

import math


from .. import materials, slicing
from ..errors import ConfigError
from ..slicing import PrinterProfile
from . import check as checks
from . import estimate as est
from . import orient
from .mesh import Mesh, fits_planner

LAYER_HEIGHT = {"quality": 0.16, "balanced": 0.20, "speed": 0.28}   # mm on a 0.4 mm nozzle
FINE_LAYER = 0.12              # quality goal on parts with many shallow slopes, where layer steps show
SHALLOW_AREA_FRACTION = 0.10   # share of the surface that must be shallow slopes to choose FINE_LAYER
FIRST_LAYER_FRAC = 0.5         # first layer / nozzle (0.2 mm on 0.4)
LINE = {"line_width_mm": 1.125, "outer_line_width_mm": 1.05, "first_layer_line_width_mm": 1.25}   # x nozzle
WALLS = {"quality": 3, "balanced": 3, "speed": 2}
INFILL = {"quality": 15, "balanced": 15, "speed": 10}
LOADED_EXTRA_WALLS = 1         # a part with a stated load direction gets one more wall
LOADED_INFILL = 25
TOP_MM, BOTTOM_MM = 0.8, 0.6   # solid skin thickness; never fewer than 4 top and 3 bottom layers
FIRST_LAYER_SPEED = 30         # mm/s, as slicing.py
CHAMBER_C = 45                 # a heated chamber's target for materials that need one, as slicing.py for ABS and ASA
RETRACT_DEFAULT = {True: 0.8, False: 4.0}   # direct drive, Bowden: when slicing.py has no value
BRIM_ASPECT = 3.0              # height / smallest footprint side above this gets a brim
BRIM_MM = 5
FLAT_OVERHANG_Z = -0.99        # overhangs this flat suit grid supports; anything else gets tree supports
# Rules a plan cannot fix by changing settings: the printer and filament do not go together. The hardened nozzle
# rule, like the hotend and bed rules below, fails only on a limit the printer's profile cites; past one of
# Gantrel's defaults it warns, so the plan goes ahead and tells the person what to check.
FIT_RULE = "part fits the printer"
REFUSE = ("enclosure", "hardened nozzle", "direct drive for flexible", FIT_RULE)
TOO_BIG, PAIRING = "part_too_big", "printer_filament"   # why a plan was refused (PlanRefused.reasons)


class PlanRefused(ConfigError):
    """A plan that cannot go ahead. reasons holds TOO_BIG when the part fits the printer in no orientation
    tried (another filament will not help), PAIRING when the printer and filament do not go together."""
    code = "plan_refused"

    def __init__(self, message: str, hint: str, result: dict, reasons: list[str] | None = None):
        super().__init__(message, hint)
        self.result = result
        self.reasons = reasons or [PAIRING]


def _mm(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def too_big(cand: orient.Candidate, printer: PrinterProfile, tried: int) -> str:
    """The plain sentence for a part that fits no orientation: its size and the printer's build volume."""
    w, d = cand.footprint_mm
    return (f"the part is {_mm(w)} x {_mm(d)} x {_mm(cand.height_mm)} mm and does not fit the {printer.label}'s "
            f"{_mm(printer.bed[0])} x {_mm(printer.bed[1])} mm bed and {_mm(printer.height)} mm height in any of "
            f"the {tried} orientations Gantrel tried")


def _k(printer: PrinterProfile) -> float:
    return printer.nozzle / 0.4


def shallow_fraction(m: Mesh) -> float:
    nz = m.normals[:, 2]
    lo, hi = orient.SHALLOW_SLOPE_Z
    sel = (nz > lo) & (nz < hi)
    return float(m.areas[sel].sum() / max(m.surface_area, 1e-9))


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def settings_for(goal: str, printer: PrinterProfile, fil: materials.Filament, m: Mesh | None = None,
                 cand: orient.Candidate | None = None, load_axis: str | None = None,
                 turned: Mesh | None = None) -> dict:
    """Settings for this goal. m is the part as it sits on the bed (it decides the fine layer height).
    turned is cand's part on the bed when the caller already has it (cand.mesh rebuilds it)."""
    k = _k(printer)
    n = printer.nozzle
    lh = LAYER_HEIGHT[goal]
    if goal == "quality" and m is not None and shallow_fraction(m) > SHALLOW_AREA_FRACTION:
        lh = FINE_LAYER
    lh = round(lh * k, 3)
    flh = round(FIRST_LAYER_FRAC * n, 3)
    widths = {key: round(f * n, 3) for key, f in LINE.items()}
    walls = WALLS[goal] + (LOADED_EXTRA_WALLS if load_axis else 0)
    infill = LOADED_INFILL if load_axis else INFILL[goal]
    base = slicing.pack_material(printer, fil.name) or slicing.MATERIALS.get(fil.name)
    hot_hi = min(fil.nozzle_max_c, printer.hotend_max_c)
    bed_hi = min(fil.bed_max_c, printer.bed_max_c)
    mid_n = 5 * round((fil.nozzle_min_c + fil.nozzle_max_c) / 10)
    mid_b = 5 * round((fil.bed_min_c + fil.bed_max_c) / 10)
    nozzle = _clamp(base.nozzle if base else mid_n, fil.nozzle_min_c, hot_hi)
    nozzle_first = _clamp(base.nozzle_first if base else nozzle + 5, fil.nozzle_min_c, hot_hi)
    bed = _clamp(base.bed if base else mid_b, fil.bed_min_c, bed_hi)
    bed_first = _clamp(base.bed_first if base else bed, fil.bed_min_c, bed_hi)
    fan = [_clamp(base.fan_min if base else fil.fan_min_pct, fil.fan_min_pct, fil.fan_max_pct),
           _clamp(base.fan_max if base else fil.fan_max_pct, fil.fan_min_pct, fil.fan_max_pct)]
    chamber = min(fil.max_chamber_c, base.chamber if base and base.chamber else CHAMBER_C) \
        if fil.needs_enclosure and printer.chamber_heater else 0
    cap = est.flow_cap(printer, fil)

    def fast(h: float, w: float, limit: float) -> int:
        return int(max(1, min(limit, math.floor(cap / (h * w)))))

    outer_cap = min(printer.max_speed, checks.OUTER_WALL_CAP[goal])
    retract = base.retract_mm if base else RETRACT_DEFAULT[printer.direct_drive]
    lo, hi = checks.RETRACT_DIRECT if printer.direct_drive else checks.RETRACT_BOWDEN
    retract = _clamp(retract, 0.0 if fil.flexible else lo, hi)
    supports = "off"
    if cand is not None and cand.needs_support:
        turned = turned if turned is not None else cand.mesh
        flat = turned is not None and _flat_overhangs(turned, fil)
        supports = "normal" if flat else "tree"
    brim = 0
    if cand is not None and min(cand.footprint_mm) > 0 and cand.height_mm / min(cand.footprint_mm) > BRIM_ASPECT:
        brim = BRIM_MM
    return {
        "printer": printer.label, "material": fil.name, "goal": goal, "nozzle_mm": n,
        "layer_height_mm": lh, "first_layer_height_mm": flh, **widths,
        "perimeters": walls, "top_layers": max(4, math.ceil(TOP_MM / lh - 1e-9)),
        "bottom_layers": max(3, math.ceil(BOTTOM_MM / lh - 1e-9)), "infill_percent": infill,
        "infill_pattern": "gyroid",
        "nozzle_c": nozzle, "nozzle_first_layer_c": nozzle_first, "bed_c": bed, "bed_first_layer_c": bed_first,
        "chamber_c": chamber, "fan_percent": fan,
        "outer_wall_speed_mms": fast(lh, widths["outer_line_width_mm"], outer_cap),
        "top_speed_mms": fast(lh, widths["line_width_mm"], outer_cap),
        "inner_wall_speed_mms": fast(lh, widths["line_width_mm"], printer.max_speed),
        "infill_speed_mms": fast(lh, widths["line_width_mm"], printer.max_speed),
        "support_speed_mms": fast(lh, widths["line_width_mm"], printer.max_speed),
        "first_layer_speed_mms": fast(flh, widths["first_layer_line_width_mm"], min(FIRST_LAYER_SPEED, printer.max_speed)),
        "travel_speed_mms": printer.max_speed, "accel_mms2": printer.accel, "retract_mm": retract,
        "supports": supports, "support_interface_layers": est.SUPPORT_INTERFACE_LAYERS if supports != "off" else 0,
        "brim_mm": brim, "bed_mm": list(printer.bed), "max_height_mm": printer.height,
        "filament_mm": printer.filament_mm,
    }


def _flat_overhangs(m: Mesh, fil: materials.Filament) -> bool:
    nz = m.normals[:, 2]
    over = nz < -math.sin(math.radians(fil.max_overhang_deg))
    zlow = m.tris[:, :, 2].min(axis=1)
    over &= zlow > orient.BED_CONTACT_MM
    if not over.any():
        return False
    a = m.areas
    return float(a[over & (nz < FLAT_OVERHANG_Z)].sum()) >= 0.5 * float(a[over].sum())


def part_facts(cand: orient.Candidate, e: dict, printer: PrinterProfile, thin: float | None) -> dict:
    return {"fits": cand.fits, "size_mm": [round(v, 2) for v in (*cand.footprint_mm, cand.height_mm)],
            "bed_mm": list(printer.bed), "max_height_mm": printer.height,
            "overhang_mm2": cand.overhang_mm2, "bridges": cand.bridges, "thinnest_wall_mm": thin,
            "smallest_layer_seconds": e["smallest_layer_seconds"],
            "layers_below_min_layer_time": e["layers_below_min_layer_time"]}


def printer_limits(printer: PrinterProfile) -> dict:
    keys = ("hotend_max_c", "bed_max_c", "max_flow", "high_flow", "hardened_nozzle", "max_accel", "top_speed",
            "chamber_heater")
    return {k: {"value": getattr(printer, k), "source": "cited" if printer.is_cited(k) else "Gantrel default, not a spec"}
            for k in keys} | ({"sources": printer.limits_source} if printer.limits_source else {})


def baseline(cand: orient.Candidate, printer: PrinterProfile, fil: materials.Filament, profile,
             turned: Mesh | None = None) -> dict:
    """slicing.py's standard settings for the same orientation, for the time saved figure."""
    if fil.name not in slicing.MATERIALS:
        return {"available": False, "why": f"slicing.py has no defaults for {fil.name}"}
    s = slicing.settings(printer, slicing.MATERIALS[fil.name], "standard", force=True)
    s["supports"] = "auto" if cand.needs_support else "off"
    e = est.estimate(turned if turned is not None else cand.mesh, s, printer, fil, cand.support_volume_mm3,
                     cand.support_contact_mm2, profile)
    c = checks.check(s, printer, fil, None, "balanced")
    out = {"available": True, "settings": "slicing.py standard", "seconds": e["seconds"],
           "check_fails": c["fails"], "check_warns": c["warns"]}
    over = [r for r in c["rules"] if r["rule"].startswith("flow at") and r["status"] == "fail"]
    if over:
        worst = max(over, key=lambda r: r["values"]["flow_mm3s"])["values"]
        out["note"] = (f"slicing.py's standard speeds ask for up to {worst['flow_mm3s']} mm3/s, over the "
                       f"{worst['cap_mm3s']} mm3/s cap ({worst['cap_source']}). OrcaSlicer slows those moves to its filament "
                       "preset's limit; PrusaSlicer, as slicing.py calls it, sets no volumetric limit, so those moves "
                       "would under-extrude. The time above assumes the capped flow")
    return out


def scoring(m: Mesh, ranked: list[orient.Candidate]) -> dict:
    """How many triangles the orientations were scored on, and a sentence saying so when it was a copy."""
    total = len(m.tris)
    scored = min((c.scored_on for c in ranked if c.scored_on), default=total)
    out = {"triangles": total, "scored_on_triangles": scored, "simplified": scored < total}
    if scored < total:
        out["note"] = (f"orientation was scored on {scored} of {total} triangles (a simplified copy) for supports "
                       f"and time; the best {orient.FINALISTS} were measured again on the full mesh, and fit, size, "
                       "bed contact, overhang, volume and the estimate come from the full mesh")
    return out


def plan(m: Mesh, printer: PrinterProfile, material: str, goal: str = "quality", load_axis: str | None = None) -> dict:
    """Orientation, supports, settings, estimate and check for one part on one printer and filament."""
    fits_planner(m)
    if printer.resin:
        raise ConfigError(f"Gantrel does not plan resin prints like the {printer.label}",
                          "Plan and slice it in CHITUBOX or VOXELDANCE TANGO")
    if goal not in checks.GOALS:
        raise ValueError(f"unknown goal {goal!r}; use one of {', '.join(checks.GOALS)}")
    fil = materials.get(material)
    time_fn = None
    if goal != "quality":
        def time_fn(c: orient.Candidate, turned: Mesh) -> float:
            s = settings_for(goal, printer, fil, turned, c, load_axis, turned)
            prof = est.layer_profile(turned, s["layer_height_mm"], s["first_layer_height_mm"], RANK_COLUMNS)
            return float(est.estimate(turned, s, printer, fil, c.support_volume_mm3, c.support_contact_mm2,
                                      prof)["seconds"])
    ranked = orient.rank(m, printer, fil, goal, load_axis, time_fn)
    best = ranked[0]
    placed = best.mesh
    s = settings_for(goal, printer, fil, placed, best, load_axis, placed)
    profile = est.layer_profile(placed, s["layer_height_mm"], s["first_layer_height_mm"])
    e = est.estimate(placed, s, printer, fil, best.support_volume_mm3, best.support_contact_mm2, profile)
    thin = orient.thinnest_wall(placed)
    facts = part_facts(best, e, printer, thin)
    result = checks.check(s, printer, fil, facts, goal)
    out = {
        "part": m.facts() | {"name": m.name, "warnings": m.warnings},
        "printer": printer.key, "printer_label": printer.label, "printer_limits": printer_limits(printer),
        "material": fil.name, "material_sources": fil.source_urls(), "goal": goal, "load_axis": load_axis,
        "orientations": [c.summary() for c in ranked[:3]],
        "orientation_scoring": scoring(m, ranked),
        "settings": s, "estimate": e, "check": result,
        "thin_wall_note": None if thin is not None else
        f"thin wall check skipped: more than {orient.THIN_MAX_TRIANGLES} triangles or no wall found",
    }
    refused = [r for r in result["rules"] if r["status"] == "fail" and r["rule"] in REFUSE]
    temp_refused = [r for r in result["rules"] if r["status"] == "fail" and r["rule"].endswith(
        ("within the hotend", "within the bed heater", f"in the {fil.name} range")) and "temperature" in r["rule"]]
    fit = [r for r in refused if r["rule"] == FIT_RULE]
    pairing = [r for r in refused if r["rule"] != FIT_RULE] + temp_refused
    if fit or pairing:
        said, fixes, reasons = [], [], []
        if fit:   # a bigger part than the printer builds: no filament or setting fixes that
            said.append(too_big(best, printer, len(ranked)))
            fixes.append(fit[0]["fix"])
            reasons.append(TOO_BIG)
        if pairing:
            said.append(f"{fil.name} on {printer.label} cannot pass: " + "; ".join(r["rule"] for r in pairing))
            fixes.append(pairing[0]["fix"])
            reasons.append(PAIRING)
        raise PlanRefused("; and ".join(said), ". ".join(fixes), out, reasons)
    if not result["ok"]:   # a planner bug: the optimizer must never emit settings that fail
        raise AssertionError(f"plan failed its own check: {result['fails']}")
    b = baseline(best, printer, fil, profile, placed)
    if b.get("available"):
        b["seconds_saved"] = b["seconds"] - e["seconds"]
    out["baseline"] = b
    return out


RANK_COLUMNS = 1024   # fewer columns for the quick estimate used to rank orientations
