"""Every check rule, once passing and once failing (or warning)."""

import dataclasses

import pytest

from gantrel_printer import materials, slicing
from gantrel_printer.errors import ConfigError
from gantrel_printer.plan import check as C
from gantrel_printer.plan import optimize

A1 = slicing.PRINTERS["bambu-a1"]          # open, direct drive
X1C = slicing.PRINTERS["bambu-x1c"]        # enclosed, no chamber heater
K2 = slicing.PRINTERS["creality-k2"]
K2P = slicing.PRINTERS["creality-k2-plus"]
MARLIN = slicing.PRINTERS["generic-marlin"]  # open, Bowden
PLA = materials.get("PLA")
PART = {"fits": True, "size_mm": [20, 20, 20], "bed_mm": [256, 256], "max_height_mm": 256, "overhang_mm2": 0.0,
        "bridges": [], "thinnest_wall_mm": 5.0, "smallest_layer_seconds": 20.0, "layers_below_min_layer_time": 0}


def good(printer=A1, fil=PLA, goal="balanced"):
    return optimize.settings_for(goal, printer, fil)


def rule(result, name):
    hits = [r for r in result["rules"] if r["rule"] == name]
    assert len(hits) == 1, (name, [r["rule"] for r in result["rules"]])
    return hits[0]


def status(s, name, printer=A1, fil=PLA, part=PART, goal="balanced"):
    return rule(C.check(s, printer, fil, part, goal), name)["status"]


def test_planned_settings_pass_everything_on_an_open_printer():
    r = C.check(good(), A1, PLA, PART, "balanced")
    assert r["ok"] and r["fails"] == [] and r["warns"] == []
    for x in r["rules"]:
        assert x["formula"] and isinstance(x["values"], dict)


@pytest.mark.parametrize("name,change,bad", [
    ("nozzle temperature in the PLA range", {"nozzle_c": 180}, "fail"),
    ("first layer nozzle temperature in the PLA range", {"nozzle_first_layer_c": 250}, "fail"),
    ("bed temperature in the PLA range", {"bed_c": 30}, "fail"),
    ("first layer bed temperature in the PLA range", {"bed_first_layer_c": 70}, "fail"),
    ("layer height fits the nozzle", {"layer_height_mm": 0.35}, "fail"),
    ("layer height fits the nozzle", {"layer_height_mm": 0.29}, "warn"),
    ("first layer height", {"first_layer_height_mm": 0.1}, "fail"),
    ("first layer height", {"first_layer_height_mm": 0.35}, "fail"),
    ("wall count", {"perimeters": 1}, "warn"),
    ("line width fits the nozzle", {"line_width_mm": 0.3}, "fail"),
    ("line width fits the nozzle", {"line_width_mm": 0.62}, "warn"),
    ("outer line width fits the nozzle", {"outer_line_width_mm": 0.7}, "fail"),
    ("first layer line width fits the nozzle", {"first_layer_line_width_mm": 0.3}, "fail"),
    ("flow at infill", {"infill_speed_mms": 240}, "fail"),
    ("flow at outer wall", {"outer_wall_speed_mms": 200}, "fail"),
    ("flow at first layer", {"first_layer_speed_mms": 200}, "fail"),
    ("travel speed within the printer", {"travel_speed_mms": 600}, "warn"),      # the A1 cites 500 mm/s
    ("infill speed within the printer", {"infill_speed_mms": 600}, "warn"),
    ("acceleration within the printer", {"accel_mms2": 50000}, "warn"),
    ("part fan in the PLA range", {"fan_percent": [10, 50]}, "warn"),
    ("retraction for direct drive", {"retract_mm": 3.0}, "fail"),
    ("retraction for direct drive", {"retract_mm": 0.1}, "warn"),
    ("chamber temperature", {"chamber_c": 60}, "fail"),
])
def test_settings_rule_passes_then_breaks(name, change, bad):
    s = good()
    if name == "chamber temperature":
        assert all(r["rule"] != name for r in C.check(s, A1, PLA, PART)["rules"])   # no chamber asked: no rule
        assert status(dict(s, chamber_c=40), name, printer=K2P) == "pass"
    else:
        assert status(s, name) == "pass"
    assert status(dict(s, **change), name, printer=K2P if name == "chamber temperature" else A1) == bad


def test_hotend_and_bed_heater_limits():
    """Past a cited limit the rule fails; past one of Gantrel's defaults it warns and says to check."""
    s = good()
    assert status(s, "nozzle temperature within the hotend") == "pass"
    assert A1.is_cited("hotend_max_c") and A1.is_cited("bed_max_c")
    cold = dataclasses.replace(A1, hotend_max_c=210)
    assert status(s, "nozzle temperature within the hotend", printer=cold) == "fail"
    r = rule(C.check(s, cold, PLA, PART), "nozzle temperature within the hotend")
    assert r["values"]["cited"] is True and "limited to 210 C" in r["fix"]
    guess = dataclasses.replace(A1, hotend_max_c=210, cited=())
    r = rule(C.check(s, guess, PLA, PART), "nozzle temperature within the hotend")
    assert r["status"] == "warn" and r["values"]["cited"] is False
    assert "Gantrel's default hotend max of 210 C, not a printer spec" in r["fix"]
    assert status(s, "bed temperature within the bed heater") == "pass"
    assert status(s, "bed temperature within the bed heater", printer=dataclasses.replace(A1, bed_max_c=50)) == "fail"
    assert status(s, "bed temperature within the bed heater",
                  printer=dataclasses.replace(A1, bed_max_c=50, cited=())) == "warn"


def test_outer_wall_cap_depends_on_the_goal():
    s = dict(good(), outer_wall_speed_mms=180)
    assert status(s, "outer wall speed for quality", goal="quality") == "fail"
    assert status(s, "outer wall speed for balanced", goal="balanced") == "pass"
    assert status(dict(s, outer_wall_speed_mms=220), "outer wall speed for balanced", goal="balanced") == "warn"
    assert status(dict(s, outer_wall_speed_mms=220), "outer wall speed for speed", goal="speed") == "pass"


def test_enclosure_rules():
    abs_ = materials.get("ABS")
    s = good(X1C, abs_)
    assert status(s, "enclosure", printer=X1C, fil=abs_) == "pass"
    assert status(s, "enclosure", printer=A1, fil=abs_) == "fail"
    assert status(good(X1C), "enclosure heat", printer=X1C) == "warn"
    assert all(r["rule"] != "enclosure heat" for r in C.check(good(), A1, PLA, PART)["rules"])


def test_chamber_without_a_heater_warns():
    abs_ = materials.get("ABS")
    s = dict(good(X1C, abs_), chamber_c=45)
    assert status(s, "chamber temperature", printer=X1C, fil=abs_) == "warn"
    assert status(s, "chamber temperature", printer=K2P, fil=abs_) == "pass"


def test_hardened_nozzle():
    cf = materials.get("PLA-CF")
    s = good(K2, cf)
    assert status(s, "hardened nozzle", printer=K2, fil=cf) == "pass"
    assert status(s, "hardened nozzle", printer=A1, fil=cf) == "fail"            # cited: stainless steel
    assert status(s, "hardened nozzle", printer=X1C, fil=cf) == "pass"           # cited: hardened steel
    assert status(s, "hardened nozzle", printer=MARLIN, fil=cf) == "warn"        # Gantrel's brass default


def test_flexible_needs_direct_drive():
    tpu = materials.get("TPU")
    s = good(A1, tpu)
    assert status(s, "direct drive for flexible", fil=tpu) == "pass"
    assert status(s, "direct drive for flexible", printer=MARLIN, fil=tpu) == "fail"


def test_bowden_retraction():
    s = good(MARLIN)
    assert status(s, "retraction for Bowden", printer=MARLIN) == "pass"
    assert status(dict(s, retract_mm=0.8), "retraction for Bowden", printer=MARLIN) == "warn"
    assert status(dict(s, retract_mm=8), "retraction for Bowden", printer=MARLIN) == "fail"


@pytest.mark.parametrize("name,change,bad", [
    ("part fits the printer", {"fits": False}, "fail"),
    ("supports for overhangs", {"overhang_mm2": 120.0}, "fail"),
    ("thin walls", {"thinnest_wall_mm": 0.3}, "warn"),
    ("minimum layer time", {"smallest_layer_seconds": 2.0}, "warn"),
])
def test_part_rule_passes_then_breaks(name, change, bad):
    s = dict(good(), supports="off")
    assert status(s, name) == "pass"
    assert status(s, name, part=dict(PART, **change)) == bad


def test_supports_on_clear_the_overhang_rule_and_bridges():
    part = dict(PART, overhang_mm2=120.0, bridges=[{"z_mm": 20, "span_mm": 30.0, "area_mm2": 300}])
    assert status(dict(good(), supports="tree"), "supports for overhangs", part=part) == "pass"
    assert status(dict(good(), supports="tree"), "bridge length", part=part) == "pass"
    assert status(dict(good(), supports="off"), "bridge length", part=part) == "warn"
    short = dict(PART, bridges=[{"z_mm": 20, "span_mm": 10.0, "area_mm2": 100}])
    assert status(dict(good(), supports="off"), "bridge length", part=short) == "pass"


def test_without_a_part_the_part_rules_are_skipped():
    r = C.check(good(), A1, PLA, None)
    assert rule(r, "part checks")["status"] == "skip"
    assert all(x["rule"] != "minimum layer time" for x in r["rules"])


def test_missing_settings_and_bad_goal():
    with pytest.raises(ConfigError, match="missing"):
        C.check({"nozzle_c": 200}, A1, PLA)
    with pytest.raises(ValueError):
        C.check(good(), A1, PLA, None, "fastest")


def test_slicing_defaults_fail_the_flow_rule():
    """slicing.py asks a 12 mm3/s hotend for 27 mm3/s of PLA infill: the check says so."""
    s = slicing.settings(A1, slicing.material("PLA"), "standard")
    r = C.check(s, A1, PLA, None, "balanced")
    assert "flow at infill" in r["fails"]
    v = rule(r, "flow at infill")["values"]
    assert v["flow_mm3s"] == pytest.approx(0.2 * 0.45 * 250)


def test_every_limit_is_a_named_constant():
    for name in ("LAYER_MIN_FRAC", "LAYER_MAX_FRAC", "FIRST_LAYER_MIN_FRAC", "FIRST_LAYER_MAX_FRAC", "LINE_MIN_FRAC",
                 "LINE_MAX_FRAC", "MIN_WALLS", "OUTER_WALL_CAP", "RETRACT_DIRECT", "RETRACT_BOWDEN",
                 "OPEN_LID_BELOW_C", "BRIDGE_WARN_MM", "THIN_WALL_LINES"):
        assert hasattr(C, name)


def test_flow_rule_says_which_source_it_used():
    k2 = rule(C.check(good(K2), K2, PLA, PART), "flow at infill")["values"]
    assert k2["cap_mm3s"] == 14.0 and k2["cap_source"] == "vendor preset Generic PLA @K2-all"
    gk = slicing.PRINTERS["generic-klipper"]
    other = rule(C.check(good(gk), gk, PLA, PART), "flow at infill")["values"]
    assert other["cap_mm3s"] == pytest.approx(10.8) and other["cap_source"].startswith("Gantrel default")


# Speeds and accelerations from the vendors' own slicer presets (resolved through their inherits chains).
# Bambu Studio / OrcaSlicer at a31c95e: BBL/process/0.20mm Standard @BBL X1C.json and @BBL A1.json.
BAMBU_X1C_STANDARD = {"travel_speed_mms": 500, "infill_speed_mms": 270, "inner_wall_speed_mms": 300,
                      "outer_wall_speed_mms": 200, "top_speed_mms": 200, "accel_mms2": 10000}
BAMBU_A1_STANDARD = {"travel_speed_mms": 700, "infill_speed_mms": 270, "inner_wall_speed_mms": 300,
                     "outer_wall_speed_mms": 200, "top_speed_mms": 200, "accel_mms2": 6000}
# PrusaSlicer 2.9.7 PrusaResearch.ini "0.20mm SPEED @MK4IS 0.4" and Creality.ini "0.20 mm NORMAL (0.4 mm nozzle)
# @CREALITY" (the Ender-3 class printer generic-marlin stands for).
PRUSA_MK4IS_SPEED = {"travel_speed_mms": 300, "infill_speed_mms": 200, "inner_wall_speed_mms": 170,
                     "outer_wall_speed_mms": 170, "top_speed_mms": 100, "support_speed_mms": 120,
                     "first_layer_speed_mms": 40, "accel_mms2": 4000}
CREALITY_NORMAL = {"travel_speed_mms": 150, "infill_speed_mms": 50, "inner_wall_speed_mms": 40,
                   "outer_wall_speed_mms": 25, "top_speed_mms": 30, "support_speed_mms": 40,
                   "first_layer_speed_mms": 20, "accel_mms2": 500}
MOTION = ("speed within the printer", "acceleration within the printer")


def _motion(result):
    return [r for r in result["rules"] if r["rule"].endswith(MOTION)]


@pytest.mark.parametrize("key,preset", [("bambu-x1c", BAMBU_X1C_STANDARD), ("prusa-mk4", PRUSA_MK4IS_SPEED)])
def test_vendor_preset_speeds_pass_on_a_cited_top_speed(key, preset):
    printer = slicing.PRINTERS[key]
    assert printer.is_cited("top_speed") and printer.is_cited("max_accel")
    r = C.check(dict(good(printer), **preset), printer, PLA, PART, "speed")
    motion = _motion(r)
    assert motion and all(x["status"] == "pass" and x["values"]["cited"] for x in motion), \
        [(x["rule"], x["status"]) for x in motion]
    # The planned settings with only the vendor's travel speed pass the whole check.
    s = dict(good(printer), travel_speed_mms=preset["travel_speed_mms"])
    assert C.check(s, printer, PLA, PART)["ok"]


def test_speed_past_a_cited_top_speed_warns_that_the_printer_caps_it():
    """Bambu's own A1 preset travels at 700 mm/s on a 500 mm/s toolhead: the firmware caps it, nothing breaks."""
    r = C.check(dict(good(A1), **BAMBU_A1_STANDARD), A1, PLA, PART, "speed")
    travel = rule(r, "travel speed within the printer")
    assert travel["status"] == "warn" and travel["values"] == {"speed_mms": 700, "top_speed_mms": 500, "cited": True}
    assert "moves at most 500 mm/s" in travel["fix"]
    assert not [x for x in _motion(r) if x["status"] == "fail"]


def test_speed_past_gantrels_default_cap_warns_and_names_it():
    """generic-marlin at PrusaSlicer's normal Ender-3 speeds: travel 150 is over Gantrel's 100 mm/s cap only."""
    assert not MARLIN.is_cited("top_speed") and not MARLIN.is_cited("max_accel")
    s = dict(good(MARLIN), **CREALITY_NORMAL)
    r = C.check(s, MARLIN, PLA, PART)
    assert r["ok"], r["fails"]
    travel = rule(r, "travel speed within the printer")
    assert travel["status"] == "warn" and travel["values"]["cited"] is False
    assert "Gantrel's default speed cap" in travel["fix"] and "not a printer spec" in travel["fix"]
    fast = rule(C.check(dict(s, accel_mms2=3000), MARLIN, PLA, PART), "acceleration within the printer")
    assert fast["status"] == "warn" and "Gantrel's default acceleration" in fast["fix"]


def test_motion_rules_never_fail():
    for printer in (A1, X1C, K2, MARLIN, slicing.PRINTERS["generic-klipper"]):
        s = dict(good(printer), travel_speed_mms=5000, infill_speed_mms=5000, accel_mms2=10 ** 6)
        assert not [x["rule"] for x in _motion(C.check(s, printer, PLA, PART)) if x["status"] == "fail"]
