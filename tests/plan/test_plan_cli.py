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
