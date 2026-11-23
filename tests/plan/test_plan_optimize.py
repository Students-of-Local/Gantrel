"""The optimizer passes its own check for every printer, filament and goal, or refuses with the reason."""

import numpy as np
import pytest

import shapes
from gantrel_printer import materials, slicing
from gantrel_printer.plan import check as C
from gantrel_printer.plan import optimize

FDM = [p for p in slicing.PRINTERS.values() if not p.resin]
CUBE = shapes.cube()


@pytest.mark.parametrize("printer", FDM, ids=lambda p: p.key)
def test_sweep_every_material_and_goal(printer):
    for fam in materials.FAMILIES:
        fil = materials.get(fam)
        for goal in C.GOALS:
            try:
                p = optimize.plan(CUBE, printer, fam, goal)
            except optimize.PlanRefused as err:
                fails = err.result["check"]["fails"]
                assert fails, (printer.key, fam, goal)
                assert all(f in optimize.REFUSE or "temperature within" in f for f in fails), fails
                # The refusal is for a real reason, worked out from the table and the profile.
                cannot = ((fil.needs_enclosure and not printer.enclosed) or (fil.abrasive and not printer.hardened_nozzle)
                          or (fil.flexible and not printer.direct_drive) or fil.nozzle_min_c > printer.hotend_max_c
                          or fil.bed_min_c > printer.bed_max_c)
                assert cannot, (printer.key, fam, goal, fails)
                continue
            assert p["check"]["ok"], (printer.key, fam, goal, p["check"]["fails"])
            again = C.check(p["settings"], printer, fil, None, goal)
            assert again["ok"], again["fails"]
            s = p["settings"]
            assert s["outer_wall_speed_mms"] <= C.OUTER_WALL_CAP[goal]
            assert s["infill_speed_mms"] <= printer.max_speed
            if fam in slicing.MATERIALS:
                assert p["baseline"]["available"] and "seconds_saved" in p["baseline"]


def test_known_refusals():
    cases = [("generic-marlin", "ABS", "enclosure"), ("bambu-a1", "PLA-CF", "hardened nozzle"),
             ("generic-marlin", "TPU", "direct drive for flexible"),
             ("bambu-x1c", "PC", "nozzle temperature within the hotend")]
    for key, fam, why in cases:
        with pytest.raises(optimize.PlanRefused) as err:
            optimize.plan(CUBE, slicing.PRINTERS[key], fam)
        assert why in err.value.result["check"]["fails"]
        assert err.value.code == "plan_refused"


def test_speed_is_faster_than_quality_and_quality_is_finer():
    k2 = slicing.PRINTERS["creality-k2"]
    m = shapes.mushroom()
    q = optimize.plan(m, k2, "PLA", "quality")
    s = optimize.plan(m, k2, "PLA", "speed")
    assert s["estimate"]["seconds"] < q["estimate"]["seconds"]
    assert q["settings"]["layer_height_mm"] < s["settings"]["layer_height_mm"]
    assert q["settings"]["outer_wall_speed_mms"] <= C.OUTER_WALL_CAP["quality"]


def test_k2_plus_uses_high_flow_and_heats_the_chamber():
    plus = slicing.PRINTERS["creality-k2-plus"]
    p = optimize.plan(CUBE, plus, "ABS", "speed")
    abs_ = materials.get("ABS")
    assert p["estimate"]["flow_cap_mm3s"] == pytest.approx(0.9 * min(40, abs_.max_flow_high_flow_mm3s))
    assert 0 < p["settings"]["chamber_c"] <= abs_.max_chamber_c
    assert p["printer_limits"]["max_flow"]["source"] == "cited"
    other = optimize.plan(CUBE, slicing.PRINTERS["bambu-a1"], "PLA")
    assert other["printer_limits"]["max_flow"]["source"] == "Gantrel default, not a spec"


def test_fine_layers_for_shallow_slopes_and_supports_for_overhangs():
    import numpy as np
    from gantrel_printer.plan.mesh import Mesh
    # A wedge: a 30 x 20 base rising to 6 mm, so most of its surface is a shallow slope.
    v = np.array([[0, 0, 0], [30, 0, 0], [30, 20, 0], [0, 20, 0], [30, 0, 6], [30, 20, 6]], float)
    f = [[0, 2, 1], [0, 3, 2], [0, 1, 4], [3, 5, 2], [1, 2, 5], [1, 5, 4], [0, 4, 5], [0, 5, 3]]
    wedge = Mesh(v[np.array(f)], name="wedge")
    assert wedge.watertight()["watertight"] and wedge.volume > 0
    assert optimize.shallow_fraction(wedge) > optimize.SHALLOW_AREA_FRACTION
    s = optimize.settings_for("quality", slicing.PRINTERS["creality-k2"], materials.get("PLA"), wedge)
    assert s["layer_height_mm"] == optimize.FINE_LAYER
    # The mushroom forced upright needs supports, and flat overhangs get normal supports.
    from gantrel_printer.plan import orient
    c = orient.evaluate(shapes.mushroom(), np.array([0.0, 0.0, -1.0]), slicing.PRINTERS["creality-k2"],
                        materials.get("PLA"))
    s = optimize.settings_for("quality", slicing.PRINTERS["creality-k2"], materials.get("PLA"), c.mesh, c)
    assert s["supports"] == "normal" and s["support_interface_layers"] == 2


def test_pin_plan_warns_on_layer_time_and_gets_a_brim():
    k2 = slicing.PRINTERS["creality-k2"]
    p = optimize.plan(shapes.pin(), k2, "PLA", "quality")
    assert "minimum layer time" in p["check"]["warns"]
    assert p["settings"]["brim_mm"] == 0                       # its 20 x 20 base holds it
    bare = optimize.plan(shapes.boxes((0, 3, 0, 3, 0, 40)), k2, "PETG", "quality")
    if bare["orientations"][0]["height_mm"] > 30:              # if it stands, it needs a brim
        assert bare["settings"]["brim_mm"] == optimize.BRIM_MM
    tall = optimize.settings_for("quality", k2, materials.get("PLA"), None,
                                 optimize.orient.evaluate(shapes.boxes((0, 3, 0, 3, 0, 40)),
                                                          np.array([0.0, 0.0, -1.0]), k2, materials.get("PLA")))
    assert tall["brim_mm"] == optimize.BRIM_MM
    lying = optimize.plan(shapes.pin(), k2, "PLA", "quality", load_axis="z")
    assert lying["orientations"][0]["height_mm"] == pytest.approx(20)
    assert lying["settings"]["perimeters"] == optimize.WALLS["quality"] + 1


def test_resin_and_unknown_goal_are_refused():
    from gantrel_printer.errors import ConfigError
    with pytest.raises(ConfigError, match="resin"):
        optimize.plan(CUBE, slicing.PRINTERS["elegoo-saturn-4-ultra"], "PLA")
    with pytest.raises(ValueError):
        optimize.plan(CUBE, slicing.PRINTERS["creality-k2"], "PLA", "fastest")
