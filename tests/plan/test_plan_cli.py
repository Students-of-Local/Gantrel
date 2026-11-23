"""gantrel-printer plan and check, end to end through main(). No printer is contacted."""

import json

import pytest

import shapes
from gantrel_printer import cli
from gantrel_printer.plan import mesh


@pytest.fixture
def stl(tmp_path):
    def make(m, name):
        return str(mesh.write_stl(tmp_path / name, m))
    return make


def run(capsys, *argv):
    rc = cli.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_plan_json(capsys, stl, tmp_path):
    out_stl = tmp_path / "turned.stl"
    rc, out, _ = run(capsys, "plan", stl(shapes.mushroom(), "m.stl"), "--printer", "creality-k2", "--material", "PETG",
                     "--goal", "balanced", "--json", "--write-oriented", str(out_stl))
    assert rc == 0
    d = json.loads(out)
    assert d["check"]["ok"] and d["settings"]["supports"] == "off" and len(d["orientations"]) == 3
    assert d["estimate"]["seconds"] > 0 and d["estimate"]["filament_g"] > 0 and "slicing.py" in d["baseline"]["settings"]
    assert d["next"].startswith("Tell the person")
    turned = mesh.load(out_stl)
    lo, hi = turned.bbox
    assert lo[2] == pytest.approx(0, abs=1e-4) and hi[2] == pytest.approx(25, abs=1e-4)


def test_plan_text(capsys, stl):
    rc, out, _ = run(capsys, "plan", stl(shapes.pin(), "pin.stl"), "--printer", "creality-k2", "--material", "PLA",
                     "--load-axis", "z")
    assert rc == 0
    for line in ("Orientation:", "Supports:", "Time:", "Filament:", "Settings:", "Check: pass", "NEXT:"):
        assert line in out
    assert "layers run along the load" in out


def test_plan_refused(capsys, stl):
    rc, out, _ = run(capsys, "plan", stl(shapes.cube(), "c.stl"), "--printer", "generic-marlin", "--material", "ABS",
                     "--json")
    assert rc == 1
    d = json.loads(out)
    assert d["refused"] and "enclosure" in d["check"]["fails"]


def test_plan_bad_file(capsys, tmp_path):
    bad = tmp_path / "bad.stl"
    bad.write_bytes(b"garbage")
    rc, _, err = run(capsys, "plan", str(bad), "--printer", "creality-k2", "--material", "PLA")
    assert rc == 1 and "ERROR (mesh)" in err


def test_check_round_trip(capsys, stl, tmp_path):
    model = stl(shapes.cube(), "c.stl")
    rc, out, _ = run(capsys, "plan", model, "--printer", "bambu-a1", "--material", "PLA", "--json")
    assert rc == 0
    plan_json = tmp_path / "plan.json"
    plan_json.write_text(out)
    rc, out, _ = run(capsys, "check", str(plan_json), "--printer", "bambu-a1", "--material", "PLA", "--model", model)
    assert rc == 0 and "pass" in out
    s = json.loads(plan_json.read_text())["settings"]
    s["infill_speed_mms"] = 249
    s["nozzle_c"] = 310                      # over PLA's range and over the A1's cited 300 C hotend
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(s))
    rc, out, _ = run(capsys, "check", str(bad), "--printer", "bambu-a1", "--material", "PLA", "--json")
    assert rc == 1
    d = json.loads(out)
    assert {"flow at infill", "nozzle temperature in the PLA range", "nozzle temperature within the hotend"} <= set(d["fails"])


def test_check_bad_input(capsys, tmp_path):
    p = tmp_path / "x.json"
    p.write_text("{not json")
    rc, _, err = run(capsys, "check", str(p), "--printer", "bambu-a1", "--material", "PLA")
    assert rc == 1 and "not valid JSON" in err
    p.write_text("[1, 2]")
    rc, _, err = run(capsys, "check", str(p), "--printer", "bambu-a1", "--material", "PLA")
    assert rc == 1 and "settings object" in err
    rc, _, err = run(capsys, "check", str(tmp_path / "none.json"), "--printer", "bambu-a1", "--material", "PLA")
    assert rc == 1 and "no file" in err


def test_planner_has_no_network_or_printer_code():
    import pathlib
    src = pathlib.Path(cli.__file__).parent / "plan"
    for f in src.glob("*.py"):
        text = f.read_text()
        for word in ("socket", "urllib", "http", "requests", "adapters", "subprocess"):
            assert f"import {word}" not in text and f"from {word}" not in text and f"from ..{word}" not in text, (f, word)


@pytest.mark.parametrize("printer,speeds", [
    ("bambu-x1c", {"travel_speed_mms": 500, "accel_mms2": 10000}),      # Bambu Studio 0.20mm Standard @BBL X1C
    ("prusa-mk4", {"travel_speed_mms": 300, "accel_mms2": 4000}),       # PrusaSlicer 0.20mm SPEED @MK4IS 0.4
    ("generic-marlin", {"travel_speed_mms": 150, "accel_mms2": 500}),   # PrusaSlicer 0.20 mm NORMAL @CREALITY
])
def test_check_passes_vendor_travel_speeds(capsys, tmp_path, printer, speeds):
    from gantrel_printer import materials, slicing
    from gantrel_printer.plan import optimize
    s = optimize.settings_for("balanced", slicing.PRINTERS[printer], materials.get("PLA")) | speeds
    f = tmp_path / "s.json"
    f.write_text(json.dumps(s))
    rc, out, _ = run(capsys, "check", str(f), "--printer", printer, "--material", "PLA", "--json")
    d = json.loads(out)
    assert rc == 0 and d["ok"], d["fails"]


@pytest.mark.parametrize("make", [shapes.cube, shapes.l_bracket, shapes.mushroom, shapes.arch, shapes.pin,
                                  shapes.long_bar, shapes.cylinder], ids=lambda f: f.__name__)
@pytest.mark.parametrize("goal", ["quality", "speed"])
def test_plan_json_round_trips_through_check(capsys, stl, tmp_path, make, goal):
    """What plan passed, check passes, with the original file (turned the plan's way) or the turned file."""
    model = stl(make(), "part.stl")
    turned = tmp_path / "turned.stl"
    rc, out, _ = run(capsys, "plan", model, "--printer", "creality-k2", "--material", "PLA", "--goal", goal, "--json",
                     "--write-oriented", str(turned))
    assert rc == 0
    plan_json = tmp_path / "plan.json"
    plan_json.write_text(out)
    for path, how in ((model, "turned the plan's way"), (str(turned), "already turned")):
        rc, out, _ = run(capsys, "check", str(plan_json), "--printer", "creality-k2", "--material", "PLA",
                         "--model", path, "--json")
        d = json.loads(out)
        assert rc == 0 and d["ok"], (make.__name__, path, d["fails"])
        assert how in d["part_orientation"]


def test_check_settings_without_a_plan_read_the_model_as_drawn(capsys, stl, tmp_path):
    """Settings with no plan orientation: the part is checked as drawn, so the upright mushroom needs supports."""
    model = stl(shapes.mushroom(), "m.stl")
    rc, out, _ = run(capsys, "plan", model, "--printer", "creality-k2", "--material", "PLA", "--json")
    s = json.loads(out)["settings"]
    assert s["supports"] == "off"
    bare = tmp_path / "s.json"
    bare.write_text(json.dumps(s))
    rc, out, _ = run(capsys, "check", str(bare), "--printer", "creality-k2", "--material", "PLA", "--model", model)
    assert rc == 1 and "supports for overhangs" in out and "as drawn" in out
    bad = json.loads((tmp_path / "s.json").read_text())
    (tmp_path / "bad.json").write_text(json.dumps({"settings": bad, "orientations": [{"rotation_matrix": [[2, 0, 0],
                                                                                                        [0, 1, 0],
                                                                                                        [0, 0, 1]]}]}))
    rc, _, err = run(capsys, "check", str(tmp_path / "bad.json"), "--printer", "creality-k2", "--material", "PLA",
                     "--model", model)
    assert rc == 1 and "not a rotation" in err


def test_check_with_a_model_names_missing_keys_instead_of_crashing(capsys, stl, tmp_path):
    from gantrel_printer.plan import check as C
    model = stl(shapes.cube(), "c.stl")
    full = json.loads(run(capsys, "plan", model, "--printer", "bambu-a1", "--material", "PLA", "--json")[1])["settings"]
    only = tmp_path / "only.json"
    only.write_text(json.dumps({k: full[k] for k in C.REQUIRED}))
    rc, out, _ = run(capsys, "check", str(only), "--printer", "bambu-a1", "--material", "PLA")
    assert rc == 0, out                                      # no model: the required keys are enough
    rc, _, err = run(capsys, "check", str(only), "--printer", "bambu-a1", "--material", "PLA", "--model", model)
    assert rc == 1 and "ERROR (config)" in err and "Traceback" not in err
    assert "top_layers, bottom_layers, infill_percent" in err


@pytest.mark.parametrize("fam", ["PLA", "PLA-CF", "PETG", "PA-CF", "PC", "TPU", "PVA", "HIPS"])
def test_plan_next_line_only_sends_slice_what_slice_can_do(capsys, stl, fam):
    from gantrel_printer import slicing
    rc, out, _ = run(capsys, "plan", stl(shapes.cube(), "c.stl"), "--printer", "creality-k2-plus", "--material", fam,
                     "--json")
    assert rc == 0
    nxt = json.loads(out)["next"]
    if fam in slicing.MATERIALS:
        assert f"gantrel-printer slice <file> --printer <name> --material {fam}" in nxt
        slicing.material(fam)                                   # the command it names works
    else:
        assert "gantrel-printer slice <file>" not in nxt and "OrcaSlicer" in nxt and fam in nxt
