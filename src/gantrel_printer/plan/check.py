"""Check a set of print settings against the printer, the filament and (optionally) the part.

Every rule returns its name, pass, warn, fail or skip, the formula it used, the numbers it put in,
and what to change. A fail means the print is likely to fail or damage something; a warn means it
will likely print but worse than it could. Every limit is a named constant below.

A printer limit can fail a rule only when the printer's profile cites it (PrinterProfile.cited). Past a limit
that is Gantrel's default, not a spec, the rule warns and names the assumption, so the person checks their
printer instead of being told it cannot print something it can.
"""

from __future__ import annotations

from .. import materials
from ..slicing import PrinterProfile
from .estimate import DEFAULT_LINE_WIDTH, flow_limit, supports_on

GOALS = ("quality", "balanced", "speed")

LAYER_MIN_FRAC, LAYER_MAX_FRAC = 0.25, 0.75          # layer height / nozzle: outside this the line will not form
LAYER_WARN_MIN_FRAC, LAYER_WARN_MAX_FRAC = 0.3, 0.7  # common slicer presets stay inside this
FIRST_LAYER_MIN_FRAC = 0.5                           # first layer / nozzle: thick enough to forgive an uneven bed
FIRST_LAYER_MAX_FRAC = 0.75
LINE_MIN_FRAC, LINE_MAX_FRAC = 0.9, 1.6              # line width / nozzle
LINE_WARN_MIN_FRAC, LINE_WARN_MAX_FRAC = 1.0, 1.5
MIN_WALLS = 2
# Outer wall speed cap (mm/s) per goal. quality: slicing.py's own outer wall cap. balanced: Bambu Studio's and
# Creality's 0.20 mm Standard presets. speed: Bambu Studio's 0.20 mm Standard preset for high flow printers.
OUTER_WALL_CAP = {"quality": 150, "balanced": 200, "speed": 350}
RETRACT_DIRECT = (0.2, 2.0)                          # mm, direct drive: PrusaSlicer and Orca presets sit inside this
RETRACT_BOWDEN = (2.0, 7.0)                          # mm, Bowden: the tube's slack needs more
OPEN_LID_BELOW_C = 50                                # materials that soften below this want an enclosed printer's lid open
BRIDGE_WARN_MM = 25.0                                # Gantrel's own cautious limit for an unsupported bridge, not a cited one
THIN_WALL_LINES = 1.0                                # a wall thinner than this many line widths may vanish when sliced
SPEED_KEYS = ("outer_wall_speed_mms", "inner_wall_speed_mms", "infill_speed_mms", "first_layer_speed_mms",
              "top_speed_mms", "support_speed_mms", "travel_speed_mms")
# What line width each extruding speed prints at, for the flow rule.
FEATURE_WIDTH = {"outer_wall_speed_mms": "outer_line_width_mm", "inner_wall_speed_mms": "line_width_mm",
                 "infill_speed_mms": "line_width_mm", "top_speed_mms": "line_width_mm",
                 "support_speed_mms": "line_width_mm", "first_layer_speed_mms": "first_layer_line_width_mm"}
REQUIRED = ("nozzle_c", "bed_c", "layer_height_mm", "first_layer_height_mm", "perimeters", "outer_wall_speed_mms",
            "inner_wall_speed_mms", "infill_speed_mms", "first_layer_speed_mms")


def _r(rule, status, formula, values, fix=""):
    return {"rule": rule, "status": status, "formula": formula, "values": values, "fix": fix if status != "pass" else ""}


def _limit(ok: bool, cited: bool) -> str:
    """pass, or fail past a cited printer limit, or warn past one of Gantrel's defaults."""
    return "pass" if ok else ("fail" if cited else "warn")


def _width(s: dict, key: str, nozzle: float) -> float:
    return float(s.get(key) or s.get("line_width_mm") or DEFAULT_LINE_WIDTH * nozzle)


def check(s: dict, printer: PrinterProfile, fil: materials.Filament, part: dict | None = None,
          goal: str = "balanced") -> dict:
    """All rules. part (from the planner) adds fit, overhang, bridge, thin wall and layer time rules."""
    if goal not in GOALS:
        raise ValueError(f"unknown goal {goal!r}")
    missing = [k for k in REQUIRED if k not in s]
    if missing:
        from ..errors import ConfigError
        raise ConfigError(f"the settings are missing {', '.join(missing)}",
                          "Use the settings JSON from gantrel-printer slice --json or plan --json")
    n = float(s.get("nozzle_mm") or printer.nozzle)
    out = []
    out += _temps(s, printer, fil)
    out += _layers(s, n)
    out += _widths(s, n)
    out += _flow_and_speed(s, printer, fil, n, goal)
    out += _material_fit(s, printer, fil)
    out += _cooling(s, fil, part)
    out += _part(s, n, part)
    fails = [r["rule"] for r in out if r["status"] == "fail"]
    warns = [r["rule"] for r in out if r["status"] == "warn"]
    return {"ok": not fails, "fails": fails, "warns": warns, "goal": goal, "rules": out}


def _temps(s, printer, fil):
    out = []
    for key, label in (("nozzle_c", "nozzle"), ("nozzle_first_layer_c", "first layer nozzle")):
        if key not in s:
            continue
        t = float(s[key])
        ok = fil.nozzle_min_c <= t <= fil.nozzle_max_c
        out.append(_r(f"{label} temperature in the {fil.name} range", "pass" if ok else "fail",
                      "nozzle_min <= T <= nozzle_max", {"T": t, "nozzle_min": fil.nozzle_min_c, "nozzle_max": fil.nozzle_max_c},
                      f"Set {key} between {fil.nozzle_min_c} and {fil.nozzle_max_c} C"))
        cited = printer.is_cited("hotend_max_c")
        ok = t <= printer.hotend_max_c
        out.append(_r(f"{label} temperature within the hotend", _limit(ok, cited), "T <= hotend_max",
                      {"T": t, "hotend_max": printer.hotend_max_c, "cited": cited},
                      f"{printer.label}'s hotend is limited to {printer.hotend_max_c} C: lower {key} to "
                      f"{printer.hotend_max_c} C or less, or print {fil.name} on a hotter printer" if cited else
                      f"{t:g} C is over Gantrel's default hotend max of {printer.hotend_max_c} C, not a printer spec: "
                      f"check that your {printer.label}'s hotend reaches {t:g} C before printing {fil.name}"))
    for key, label in (("bed_c", "bed"), ("bed_first_layer_c", "first layer bed")):
        if key not in s:
            continue
        t = float(s[key])
        ok = fil.bed_min_c <= t <= fil.bed_max_c
        out.append(_r(f"{label} temperature in the {fil.name} range", "pass" if ok else "fail",
                      "bed_min <= T <= bed_max", {"T": t, "bed_min": fil.bed_min_c, "bed_max": fil.bed_max_c},
                      f"Set {key} between {fil.bed_min_c} and {fil.bed_max_c} C"))
        cited = printer.is_cited("bed_max_c")
        ok = t <= printer.bed_max_c
        out.append(_r(f"{label} temperature within the bed heater", _limit(ok, cited), "T <= bed_heater_max",
                      {"T": t, "bed_heater_max": printer.bed_max_c, "cited": cited},
                      f"{printer.label}'s bed reaches {printer.bed_max_c} C: lower {key} to {printer.bed_max_c} C "
                      "or less" if cited else
                      f"{t:g} C is over Gantrel's default bed max of {printer.bed_max_c} C, not a printer spec: "
                      f"check that your {printer.label}'s bed reaches {t:g} C"))
    return out


def _layers(s, n):
    lh, flh = float(s["layer_height_mm"]), float(s["first_layer_height_mm"])
    f = lh / n
    st = "fail" if not LAYER_MIN_FRAC <= f <= LAYER_MAX_FRAC else (
        "warn" if not LAYER_WARN_MIN_FRAC <= f <= LAYER_WARN_MAX_FRAC else "pass")
    out = [_r("layer height fits the nozzle", st, f"{LAYER_MIN_FRAC} <= layer / nozzle <= {LAYER_MAX_FRAC}",
              {"layer_mm": lh, "nozzle_mm": n, "ratio": round(f, 3)},
              f"Use a layer height between {LAYER_WARN_MIN_FRAC * n:.2f} and {LAYER_WARN_MAX_FRAC * n:.2f} mm")]
    ok = FIRST_LAYER_MIN_FRAC * n - 1e-9 <= flh <= FIRST_LAYER_MAX_FRAC * n + 1e-9
    out.append(_r("first layer height", "pass" if ok else "fail",
                  f"{FIRST_LAYER_MIN_FRAC} x nozzle <= first layer <= {FIRST_LAYER_MAX_FRAC} x nozzle",
                  {"first_layer_mm": flh, "nozzle_mm": n},
                  f"Use a first layer between {FIRST_LAYER_MIN_FRAC * n:.2f} and {FIRST_LAYER_MAX_FRAC * n:.2f} mm"))
    walls = int(s["perimeters"])
    out.append(_r("wall count", "pass" if walls >= MIN_WALLS else "warn", f"walls >= {MIN_WALLS}",
                  {"walls": walls}, f"Use at least {MIN_WALLS} walls"))
    return out


def _widths(s, n):
    out = []
    for key in ("line_width_mm", "outer_line_width_mm", "first_layer_line_width_mm"):
        w = _width(s, key, n)
        f = w / n
        st = "fail" if not LINE_MIN_FRAC <= f <= LINE_MAX_FRAC else (
            "warn" if not LINE_WARN_MIN_FRAC <= f <= LINE_WARN_MAX_FRAC else "pass")
        out.append(_r(f"{key.replace('_mm', '').replace('_', ' ')} fits the nozzle", st,
                      f"{LINE_MIN_FRAC} <= width / nozzle <= {LINE_MAX_FRAC}",
                      {"width_mm": w, "nozzle_mm": n, "ratio": round(f, 3), "given": key in s},
                      f"Use a width between {LINE_WARN_MIN_FRAC * n:.2f} and {LINE_WARN_MAX_FRAC * n:.2f} mm"))
    return out


def _flow_and_speed(s, printer, fil, n, goal):
    out = []
    lh, flh = float(s["layer_height_mm"]), float(s["first_layer_height_mm"])
    limit = flow_limit(printer, fil)
    cap = limit["mm3s"]
    for key, wkey in FEATURE_WIDTH.items():
        if key not in s:
            continue
        v = float(s[key])
        h = flh if key == "first_layer_speed_mms" else lh
        w = _width(s, wkey, n)
        flow = h * w * v
        ok = flow <= cap + 1e-9
        out.append(_r(f"flow at {key.replace('_speed_mms', '').replace('_', ' ')}", "pass" if ok else "fail",
                      "layer x width x speed <= flow cap",
                      {"flow_mm3s": round(flow, 2), "layer_mm": h, "width_mm": w, "speed_mms": v,
                       "cap_mm3s": round(cap, 2), "cap_source": limit["source"]},
                      f"Lower {key} to {int(cap / (h * w))} mm/s or less: {flow:.1f} mm3/s is over the "
                      f"{cap:.1f} mm3/s cap ({limit['source']}), so the part would under-extrude"))
    # Speed and acceleration never fail: the printer's firmware caps every move at its own top speed and
    # acceleration, so a higher setting runs at the cap (Bambu Studio's own A1 preset asks for 700 mm/s travel on a
    # 500 mm/s toolhead). Past a cited limit the warning says the move runs slower than set; past Gantrel's default
    # cap it says the cap is a default, not a spec. Flow, which does spoil a print, has its own rule above.
    top_cited = printer.is_cited("top_speed") and printer.top_speed > 0
    top = printer.top_speed if top_cited else printer.max_speed
    for key in SPEED_KEYS:
        if key not in s:
            continue
        v = float(s[key])
        ok = v <= top
        out.append(_r(f"{key.replace('_mms', '').replace('_', ' ')} within the printer", "pass" if ok else "warn",
                      "speed <= printer top speed" if top_cited else "speed <= Gantrel's default speed cap",
                      {"speed_mms": v, "top_speed_mms": top, "cited": top_cited},
                      f"{printer.label} moves at most {top} mm/s, so this runs at {top} mm/s, not {v:g}: set {key} "
                      f"to {top} or less so the time estimate holds" if top_cited else
                      f"{v:g} mm/s is over {top} mm/s, Gantrel's default speed cap for the {printer.label}, not a "
                      f"printer spec: check your printer's top speed, and lower {key} only if it is slower"))
    if "accel_mms2" in s:
        a = float(s["accel_mms2"])
        cited = printer.is_cited("max_accel")
        ok = a <= printer.max_accel
        out.append(_r("acceleration within the printer", "pass" if ok else "warn",
                      "accel <= printer max_accel" if cited else "accel <= Gantrel's default acceleration",
                      {"accel_mms2": a, "max_accel_mms2": printer.max_accel, "cited": cited},
                      f"{printer.label} accelerates at most {printer.max_accel} mm/s2, so moves run at that, not "
                      f"{a:g}: set accel_mms2 to {printer.max_accel} or less" if cited else
                      f"{a:g} mm/s2 is over {printer.max_accel} mm/s2, Gantrel's default acceleration for the "
                      f"{printer.label}, not a printer spec: check your printer's maximum acceleration"))
    v = float(s["outer_wall_speed_mms"])
    limit = OUTER_WALL_CAP[goal]
    st = "pass" if v <= limit else ("fail" if goal == "quality" else "warn")
    out.append(_r(f"outer wall speed for {goal}", st, f"outer wall <= {limit} mm/s for {goal}",
                  {"speed_mms": v, "cap_mms": limit},
                  f"Slow the outer wall to {limit} mm/s: it is the surface people see"))
    return out


def _material_fit(s, printer, fil):
    out = []
    if fil.needs_enclosure:
        out.append(_r("enclosure", "pass" if printer.enclosed else "fail", "material needs enclosure -> printer enclosed",
                      {"needs_enclosure": True, "enclosed": printer.enclosed},
                      f"{fil.name} warps and cracks in open air: use an enclosed printer, or PLA or PETG"))
    elif printer.enclosed and fil.max_chamber_c < OPEN_LID_BELOW_C:
        out.append(_r("enclosure heat", "warn", f"max chamber < {OPEN_LID_BELOW_C} C on an enclosed printer",
                      {"max_chamber_c": fil.max_chamber_c, "enclosed": True},
                      f"Open the lid or door: {fil.name} softens at {fil.max_chamber_c} C and can clog in a warm chamber"))
    ch = float(s.get("chamber_c") or 0)
    if ch:
        st = "fail" if ch > fil.max_chamber_c else ("warn" if not printer.chamber_heater else "pass")
        out.append(_r("chamber temperature", st, "chamber <= material max chamber; heater present to reach it",
                      {"chamber_c": ch, "max_chamber_c": fil.max_chamber_c, "chamber_heater": printer.chamber_heater},
                      f"Keep the chamber at or under {fil.max_chamber_c} C" if ch > fil.max_chamber_c
                      else f"{printer.label} has no chamber heater: the chamber only warms from the bed"))
    if fil.abrasive:
        cited = printer.is_cited("hardened_nozzle")
        out.append(_r("hardened nozzle", _limit(printer.hardened_nozzle, cited),
                      "abrasive material -> hardened nozzle",
                      {"abrasive": True, "hardened_nozzle": printer.hardened_nozzle, "cited": cited},
                      f"{fil.name} wears a brass or stainless nozzle out within hours, and {printer.label}'s "
                      "stock nozzle is not hardened: fit a hardened steel nozzle" if cited else
                      "Gantrel assumes a brass nozzle by default, not a printer spec: check that your "
                      f"{printer.label} has a hardened steel nozzle, because {fil.name} wears brass out within hours"))
    if fil.flexible:
        out.append(_r("direct drive for flexible", "pass" if printer.direct_drive else "fail",
                      "flexible material -> direct drive", {"flexible": True, "direct_drive": printer.direct_drive},
                      f"{fil.name} buckles in a Bowden tube and jams: use a direct drive printer"))
    if "retract_mm" in s:
        r = float(s["retract_mm"])
        lo, hi = RETRACT_DIRECT if printer.direct_drive else RETRACT_BOWDEN
        kind = "direct drive" if printer.direct_drive else "Bowden"
        st = "fail" if r > hi else ("warn" if r < lo and not fil.flexible else "pass")
        out.append(_r(f"retraction for {kind}", st, f"{lo} <= retract <= {hi} mm",
                      {"retract_mm": r, "min_mm": lo, "max_mm": hi},
                      f"Set retract_mm between {lo} and {hi} mm" + (": longer grinds and clogs" if r > hi else "")))
    return out


def _cooling(s, fil, part):
    out = []
    if "fan_percent" in s:
        lo, hi = (float(v) for v in s["fan_percent"])
        ok = lo >= fil.fan_min_pct and hi <= fil.fan_max_pct
        out.append(_r(f"part fan in the {fil.name} range", "pass" if ok else "warn", "fan_min <= fan <= fan_max",
                      {"fan_percent": [lo, hi], "fan_min": fil.fan_min_pct, "fan_max": fil.fan_max_pct},
                      f"Set the fan between {fil.fan_min_pct} and {fil.fan_max_pct} percent"))
    if part and part.get("smallest_layer_seconds") is not None:
        t = float(part["smallest_layer_seconds"])
        ok = t >= fil.min_layer_time_s
        out.append(_r("minimum layer time", "pass" if ok else "warn", "fastest layer >= material min layer time",
                      {"fastest_layer_s": t, "min_layer_time_s": fil.min_layer_time_s,
                       "layers_below": part.get("layers_below_min_layer_time")},
                      "The smallest layers print too fast to cool and will sag: the slicer will slow them. "
                      "Print two copies, lay the part down, or add a cooling tower"))
    return out


def _part(s, n, part):
    if not part:
        return [_r("part checks", "skip", "needs --model", {}, "")]
    out = []
    ok = bool(part.get("fits", True))
    out.append(_r("part fits the printer", "pass" if ok else "fail", "footprint <= bed (either way round), height <= max",
                  {"size_mm": part.get("size_mm"), "bed_mm": part.get("bed_mm"), "max_height_mm": part.get("max_height_mm")},
                  "Scale the part down, split it, or use a bigger printer"))
    sup = supports_on(s)
    over = float(part.get("overhang_mm2", 0))
    st = "fail" if over > 0 and not sup else "pass"
    out.append(_r("supports for overhangs", st, "overhang area > 0 -> supports on",
                  {"overhang_mm2": round(over, 1), "supports": s.get("supports", "on")},
                  "Turn supports on, or turn the part so nothing hangs past the material's overhang angle"))
    thin = part.get("thinnest_wall_mm")
    if thin is not None:
        w = _width(s, "line_width_mm", n)
        ok = thin >= THIN_WALL_LINES * w
        out.append(_r("thin walls", "pass" if ok else "warn", f"thinnest wall >= {THIN_WALL_LINES} x line width",
                      {"thinnest_wall_mm": round(thin, 3), "line_width_mm": w},
                      "Some walls are thinner than one line and may not print: thicken them, or use a smaller nozzle"))
    spans = [b["span_mm"] for b in part.get("bridges", [])]
    if spans:
        worst = max(spans)
        st = "warn" if worst > BRIDGE_WARN_MM and not sup else "pass"
        out.append(_r("bridge length", st, f"longest bridge <= {BRIDGE_WARN_MM} mm without supports",
                      {"longest_bridge_mm": worst, "supports": s.get("supports", "on")},
                      "Long bridges sag: turn supports on or turn the part"))
    return out
