"""gantrel-printer calibrate: first-layer, temp-tower, retraction and flow prints for every filament printer."""

from __future__ import annotations

import json
import re

import pytest

from gantrel_printer import calibrate, cli, safety, slicing
from gantrel_printer.errors import ConfigError, Unsupported

import fakes

FDM = [k for k, p in slicing.PRINTERS.items() if not p.resin]
KLIPPER = [k for k, p in slicing.PRINTERS.items() if p.flavor == "klipper"]
NUM = r"(-?\d+(?:\.\d+)?)"


def sections(gcode: str) -> dict[str, list[str]]:
    out, cur = {"head": [], "start": [], "body": [], "end": []}, "head"
    for ln in gcode.splitlines():
        if ln.strip() in ("; START", "; BODY", "; END"):
            cur = ln.strip()[2:].lower()
            continue
        out[cur].append(ln)
    return out


def words(line: str) -> dict[str, float]:
    code = line.split(";")[0]
    return {m.group(1): float(m.group(2)) for m in re.finditer(r"\b([XYZEFS])" + NUM, code)}


def moves(lines: list[str]):
    """Absolute X, Y, Z of every G0/G1 move (start and body run in G90)."""
    x = y = z = None
    for ln in lines:
        cmd = ln.split(";")[0].split()
        if not cmd or cmd[0] not in ("G0", "G1"):
            continue
        w = words(ln)
        x, y, z = w.get("X", x), w.get("Y", y), w.get("Z", z)
        yield x, y, z, w


def temps(lines: list[str], codes=("M104", "M109")) -> list[float]:
    found = []
    for ln in lines:
        cmd = ln.split(";")[0].split()
        if cmd and cmd[0] in codes:
            found.append(words(ln)["S"])
        found += [float(v) for v in re.findall(r"EXTRUDER_TEMP=" + NUM, ln)] if "M104" in codes else []
    return found


def bands(gcode: str) -> list[dict]:
    out = []
    for ln in gcode.splitlines():
        if ln.startswith("; GANTREL_BAND "):
            fields = dict(re.findall(r"(\w+)=(\S+)", ln))
            out.append({"index": int(fields["index"]), "value": float(fields["value"]), "unit": fields["unit"]})
    return out


@pytest.mark.parametrize("model", FDM)
@pytest.mark.parametrize("kind", calibrate.KINDS)
def test_every_kind_stays_on_the_bed_and_in_the_temperature_range(model, kind):
    printer = slicing.PRINTERS[model]
    for mat in slicing.MATERIALS.values():
        try:
            cal = calibrate.make(kind, printer, mat)
        except ConfigError:
            assert (mat.needs_enclosure and not printer.enclosed) or (mat.name == "TPU" and not printer.direct_drive)
            continue
        sec = sections(cal.gcode)
        bw, bd = printer.bed
        n = 0
        for x, y, z, _ in moves(sec["start"] + sec["body"]):
            if x is not None:
                assert 0 <= x <= bw, (model, kind, x)
            if y is not None:
                assert 0 <= y <= bd, (model, kind, y)
            if z is not None:
                assert 0 < z <= printer.height
            n += 1
        assert n > 100
        lo, hi = mat.nozzle_range
        body_temps = temps(sec["body"])
        start_temps = [t for t in temps(sec["start"]) if t != 150]  # 150: the start G-code's standby preheat
        for t in body_temps + start_temps:
            assert lo <= t <= hi, (model, kind, mat.name, t)
        assert start_temps, "the start G-code heats the nozzle to a print temperature"
        beds = temps(sec["start"], ("M140", "M190")) + [float(v) for v in
                                                         re.findall(r"BED_TEMP=" + NUM, "\n".join(sec["start"]))]
        assert beds and all(b == mat.bed_first for b in beds)
        assert all(t == 0 for t in temps(sec["end"]))
        # The slicing profile's own retract or a band's value, never firmware retraction.
        cmds = {ln.split(";")[0].split()[0] for ln in sec["body"] if ln.split(";")[0].strip()}
        assert not cmds & {"G10", "G11"}
        assert "M83" in cmds


def test_every_material_range_holds_its_own_defaults():
    for m in slicing.MATERIALS.values():
        lo, hi = m.nozzle_range
        assert lo < hi and lo <= m.nozzle <= hi and lo <= m.nozzle_first <= hi


@pytest.mark.parametrize("model", ["creality-k2", "generic-marlin", "bambu-a1-mini", "ultimaker-s5"])
@pytest.mark.parametrize("mat", ["PLA", "PETG"])
def test_temp_tower_bands_step_as_stated(model, mat):
    printer, m = slicing.PRINTERS[model], slicing.material(mat)
    cal = calibrate.make("temp-tower", printer, m)
    lo, hi = m.nozzle_range
    stated = [b.value for b in cal.bands]
    assert stated == list(range(hi, lo - 1, -calibrate.TEMP_STEP))
    assert f"from {hi} C at the bottom down to {stated[-1]} C at the top, 5 C per band" in cal.summary
    assert [b["value"] for b in bands(cal.gcode)] == stated
    # Each band sets its temperature and then prints exactly BAND_MM of layers, starting where stated.
    body = sections(cal.gcode)["body"]
    marks = [i for i, ln in enumerate(body) if ln.startswith("; GANTREL_BAND")]
    for n, (band, at) in enumerate(zip(cal.bands, marks)):
        nxt = marks[n + 1] if n + 1 < len(marks) else len(body)
        chunk = body[at:nxt]
        assert words(chunk[1]) == {"S": band.value} and chunk[1].startswith("M104")
        zs = sorted({w["Z"] for *_, w in moves(chunk) if "Z" in w})
        z0 = float(re.search(r"Z " + NUM, band.where).group(1))
        assert zs[0] == pytest.approx(z0 + calibrate.LAYER)
        assert zs[-1] == pytest.approx(z0 + calibrate.BAND_MM)
        assert len(zs) == round(calibrate.BAND_MM / calibrate.LAYER)
        assert not [t for t in temps(chunk) if t != band.value]


@pytest.mark.parametrize("model", ["creality-k2", "generic-klipper", "generic-marlin"])
def test_retraction_tower_retracts_each_bands_distance(model):
    printer = slicing.PRINTERS[model]
    cal = calibrate.make("retraction", printer, slicing.material("PLA"))
    want = [round(0.2 * i, 1) for i in range(1, 11)] if printer.direct_drive else \
        [round(1.0 + 0.5 * i, 1) for i in range(11)]
    assert [b.value for b in cal.bands] == want
    body = sections(cal.gcode)["body"]
    marks = [i for i, ln in enumerate(body) if ln.startswith("; GANTREL_BAND")]
    for n, (band, at) in enumerate(zip(cal.bands, marks)):
        chunk = body[at:marks[n + 1] if n + 1 < len(marks) else len(body)]
        retracts = [w["E"] for *_, w in moves(chunk) if set(w) <= {"E", "F"} and w.get("E", 0) < 0]
        assert retracts and all(r == pytest.approx(-band.value) for r in retracts)
        assert len(retracts) >= round(calibrate.BAND_MM / calibrate.LAYER)  # travels between the pillars


def test_flow_patches_extrude_their_stated_percent():
    cal = calibrate.make("flow", slicing.PRINTERS["creality-k2"], slicing.material("PLA"))
    assert [b.value for b in cal.bands] == list(calibrate.FLOWS)
    # E per mm of the first fill line of each patch on layer 1 scales with the patch's flow.
    body = sections(cal.gcode)["body"]
    marks = [i for i, ln in enumerate(body) if ln.startswith("; GANTREL_BAND")]
    ratios = []
    for n, at in enumerate(marks):
        last = None
        for x, y, z, w in moves(body[at:marks[n + 1] if n + 1 < len(marks) else at + 400]):
            if "E" in w and "X" in w and last is not None and w["E"] > 0:
                dist = ((x - last[0]) ** 2 + (y - last[1]) ** 2) ** 0.5
                if dist > 5:
                    ratios.append(w["E"] / dist)
                    break
            last = (x, y)
    assert len(ratios) == len(calibrate.FLOWS)
    for r, f in zip(ratios, calibrate.FLOWS):
        assert r / ratios[2] == pytest.approx(f / 100, rel=0.01)


def test_first_layer_is_one_layer_at_first_layer_settings():
    printer, m = slicing.PRINTERS["generic-marlin"], slicing.material("PLA")
    cal = calibrate.make("first-layer", printer, m)
    body = sections(cal.gcode)["body"]
    assert {w["Z"] for *_, w in moves(body) if "Z" in w} == {calibrate.LAYER}
    assert len(cal.bands) == 5
    assert f"M109 S{m.nozzle_first}" in sections(cal.gcode)["start"]


def test_resin_printers_get_no_calibration_print():
    with pytest.raises(Unsupported, match="filament printers"):
        calibrate.make("flow", slicing.PRINTERS["elegoo-saturn-4-ultra"], slicing.material("PLA"))


# ---------------------------------------------------------------- the CLI


@pytest.fixture
def k2():
    brain = fakes.Brain(secret=None)
    srv = fakes.serve_http(brain, fakes.moonraker_routes)
    assert cli.main(["pair", "k2", "--type", "moonraker", "--host", "127.0.0.1", "--port",
                     str(srv.server_address[1]), "--model", "creality-k2"]) == 0
    yield brain
    srv.shutdown()


@pytest.mark.parametrize("kind", calibrate.KINDS)
def test_calibrate_saves_locally_and_sends_nothing(k2, tmp_path, capsys, kind):
    capsys.readouterr()
    assert cli.main(["calibrate", "k2", kind, "--out-dir", str(tmp_path / "cal")]) == 0
    text = capsys.readouterr().out
    path = tmp_path / "cal" / f"calibrate-{kind}_creality-k2_PLA.gcode"
    assert path.is_file() and str(path) in text
    assert "Nothing was sent to the printer." in text
    last = text.strip().splitlines()[-1]
    assert last.startswith("NEXT: ") and f"gantrel-printer upload {path} --printer k2" in last
    assert "When it is done:" in last
    assert k2.files == {} and not k2.started and k2.events == []
    assert safety.list_jobs() == []


def test_calibrate_next_line_says_what_to_look_for_and_what_to_change(k2, tmp_path, capsys):
    capsys.readouterr()
    assert cli.main(["calibrate", "k2", "temp-tower", "--out-dir", str(tmp_path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [b["value"] for b in data["bands"]] == list(range(230, 189, -5))
    assert "stringing" in data["next"] and "nozzle temperature" in data["next"]
    assert "Band 1 is the bottom" in data["next"]
    capsys.readouterr()
    assert cli.main(["calibrate", "k2", "first-layer", "--out-dir", str(tmp_path)]) == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert "Z offset" in last and "BED_MESH_CALIBRATE" in last


def test_calibrate_upload_sends_it_and_still_never_starts(k2, tmp_path, capsys):
    capsys.readouterr()
    assert cli.main(["calibrate", "k2", "retraction", "--out-dir", str(tmp_path), "--upload"]) == 0
    text = capsys.readouterr().out
    assert len(k2.files) == 1 and not k2.started
    remote = next(iter(k2.files))
    assert remote.startswith("calibrate-retraction_creality-k2_PLA-") and remote.endswith(".gcode")
    assert f"Uploaded to k2 as {remote}. It has NOT started." in text
    job = safety.list_jobs()[0]
    assert job.remote_name == remote and job.material == "PLA" and job.est_seconds
    last = text.strip().splitlines()[-1]
    assert last.startswith("NEXT: It has NOT started.")
    assert f"call print_start {job.id}" in last and f"({remote})" in last


def test_calibrate_takes_a_profile_key_without_a_paired_printer(tmp_path, capsys):
    assert cli.main(["calibrate", "bambu-a1-mini", "flow", "--out-dir", str(tmp_path)]) == 0
    assert (tmp_path / "calibrate-flow_bambu-a1-mini_PLA.gcode").is_file()
    assert "pair the printer, then gantrel-printer upload it" in capsys.readouterr().out
    # Upload needs a paired printer.
    assert cli.main(["calibrate", "bambu-a1-mini", "flow", "--out-dir", str(tmp_path), "--upload"]) == cli.EXIT_ERROR


def test_calibrate_refuses_a_material_the_printer_cant_print(tmp_path, capsys):
    assert cli.main(["calibrate", "generic-marlin", "temp-tower", "--material", "ABS",
                     "--out-dir", str(tmp_path)]) == cli.EXIT_ERROR
    assert "needs an enclosed printer" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_the_cli_still_cannot_start_a_print():
    from pathlib import Path

    import gantrel_printer.calibrate as mod

    src = Path(mod.__file__).read_text()
    assert ".start(" not in src and "mint_token" not in src and "upload" not in src.split('"""', 2)[2]


@pytest.mark.parametrize("kind", ["retraction", "flow"])
@pytest.mark.parametrize("mat", ["PLA", "PETG"])
def test_summary_temperature_is_the_one_the_body_prints_at(kind, mat):
    # Review r1 first-F1 (PR #7): the body heated to the first-layer temperature and never came down.
    m = slicing.material(mat)
    cal = calibrate.make(kind, slicing.PRINTERS["creality-k2"], m)
    temps = [int(x) for x in re.findall(r"^M10[49] S(\d+)", "\n".join(sections(cal.gcode)["body"]), re.M)]
    assert temps, "the body never sets the print temperature"
    stated = re.search(r"at (\d+) C", cal.summary)
    assert stated and int(stated.group(1)) == temps[-1]
