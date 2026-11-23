"""gantrel-printer pack export, show, import, list, remove, and slice and plan with pack:<id>. No printer is contacted."""

import glob
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from gantrel_printer import cli, config, packs

ROOT = Path(__file__).resolve().parents[2]
STL = b"solid t\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 10 0 0\nvertex 0 10 0\nendloop\nendfacet\nendsolid t\n"
sys.path.insert(0, str(ROOT / "tests" / "plan"))
sys.path.insert(0, str(ROOT / "tests" / "printers"))
import fakes  # noqa: E402
import shapes  # noqa: E402
from gantrel_printer.plan import mesh  # noqa: E402

PRIVATE = {"host": "192.168.77.41", "port": 4409, "serial": "01P00A987654321", "camera_url": "http://192.168.77.41:8080/",
           "stream_url": "http://192.168.77.41:8080/stream", "tls_fingerprint": "ab" * 32}


def run(capsys, *argv):
    rc = cli.main([str(a) for a in argv])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


@pytest.fixture
def paired_k2():
    """A paired K2 in printers.json with every private field filled in, as pairing leaves it."""
    config.save_all({"donovans-k2": config.PrinterConfig(name="donovans-k2", type="creality", model="creality-k2",
                                                         extra={"has_secret": True}, **PRIVATE)})
    return "donovans-k2"


def export(capsys, tmp_path, printer, *extra, name="k2.json"):
    out = tmp_path / name
    rc, text, err = run(capsys, "pack", "export", printer, "--out", out, *extra)
    assert rc == 0, text + err
    return out, text


def accept_code(text):
    return re.search(r"accept code \(first 12 hex[^)]*\): ([0-9a-f]{12})", text).group(1)


def test_export_from_a_paired_printer_holds_nothing_private(capsys, tmp_path, paired_k2):
    out, text = export(capsys, tmp_path, paired_k2, "--id", "my-k2", "--author", "A Maker", "--material", "PLA",
                       "--material", "PETG")
    body = out.read_text()
    for v in [*map(str, PRIVATE.values()), "192.168.77.41", "donovans-k2", "has_secret", "4409"]:
        assert v not in body, v
    pack = json.loads(body)
    assert pack["printer"]["model"] == "Creality K2" and pack["printer"]["adapter"] == "creality"
    assert [m["family"] for m in pack["materials"]] == ["PLA", "PETG"]
    assert all(m["calibrated"] == [] for m in pack["materials"])
    assert "Gantrel does not record calibration results" in pack["notes"]
    sha = packs.sha256(out.read_bytes())
    assert f"sha256 {sha}" in text and accept_code(text) == sha[:12]


def test_export_carries_top_speed_only_when_the_profile_has_one(capsys, tmp_path):
    out, text = export(capsys, tmp_path, "creality-k2", "--material", "PLA")
    pack = json.loads(out.read_text())
    assert pack["printer"]["top_speed"] == 800 and "top_speed" not in pack["notes"]
    rc, shown_text, _ = run(capsys, "pack", "show", out)
    assert rc == 0 and "printer top speed 800 mm/s" in shown_text
    out, _ = export(capsys, tmp_path, "generic-klipper", "--material", "PLA", name="gk.json")
    pack = json.loads(out.read_text())
    assert "top_speed" not in pack["printer"] and "hotend_max_c" in pack["notes"]
    assert packs.to_profile(pack).top_speed == 0


def test_export_default_materials_leave_out_what_the_printer_cannot_take(capsys, tmp_path):
    out, text = export(capsys, tmp_path, "generic-marlin")
    fams = [m["family"] for m in json.loads(out.read_text())["materials"]]
    assert "ABS" not in fams and "TPU" not in fams and "PLA" in fams
    assert "Left out ABS" in text and "Left out TPU" in text
    rc, _, err = run(capsys, "pack", "export", "generic-marlin", "--material", "ABS", "--out", tmp_path / "x.json")
    assert rc == 1 and "cannot go in a pack" in err


def test_export_refuses_private_text_and_writes_nothing(capsys, tmp_path):
    rc, text, _ = run(capsys, "pack", "export", "creality-k2", "--author", "me@example.com", "--out", tmp_path / "x.json")
    assert rc == 1 and "REFUSAL author" in text and not (tmp_path / "x.json").exists()


def test_round_trip_export_check_show_import_slice_plan(capsys, tmp_path, paired_k2, fake_slicer, isolated):
    out, _ = export(capsys, tmp_path, paired_k2, "--id", "my-k2", "--material", "PLA")
    # The person tuned it: their own numbers, then the pack is checked again (the hash changes).
    pack = json.loads(out.read_text())
    m = pack["materials"][0]
    m.update(brand="Acme", name="Matte PLA", nozzle_c=223, first_layer_nozzle_c=226, bed_c=57, first_layer_bed_c=61,
             max_flow=15.5, retraction_mm=0.6, retraction_speed=35, flow_ratio=0.96,
             calibrated=[{"test": "temp-tower", "date": "2026-11-20", "result": "223 C band cleanest"}])
    pack["printer"]["max_speed"] = 180
    out.write_bytes(packs.dumps(pack))
    sha = packs.sha256(out.read_bytes())

    rc, text, _ = run(capsys, "pack", "check", out)
    assert rc == 0 and "PASS" in text and sha[:12] in text

    rc, text, _ = run(capsys, "pack", "show", out)
    assert rc == 0
    for want in (f"sha256: {sha}", f"accept code (first 12 hex of the sha256): {sha[:12]}", "Acme Matte PLA",
                 "nozzle 223 C", "calibrated temp-tower 2026-11-20: 223 C band cleanest", "START_PRINT EXTRUDER_TEMP=",
                 "End G-code (printer.end_gcode), every line:", "END_PRINT",
                 f"--accept {sha[:12]}"):
        assert want in text, want

    rc, _, err = run(capsys, "pack", "import", out)
    assert rc == 3 and "needs --accept" in err
    rc, _, err = run(capsys, "pack", "import", out, "--accept", "000000000000")
    assert rc == 3 and "does not match" in err
    rc, text, _ = run(capsys, "pack", "import", out, "--accept", sha[:12])
    assert rc == 0 and "Installed pack my-k2" in text
    d = isolated / "packs" / "my-k2"
    f = d / f"{sha}.json"
    assert f.read_bytes() == out.read_bytes()
    assert stat.S_IMODE(f.stat().st_mode) == 0o600 and stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE((isolated / "packs").stat().st_mode) == 0o700

    rc, text, _ = run(capsys, "pack", "list")
    assert rc == 0 and "my-k2" in text and sha[:12] in text and "Acme Matte PLA (PLA)" in text

    model = tmp_path / "part.stl"
    model.write_bytes(STL)
    rc, text, err = run(capsys, "slice", model, "--printer", "pack:my-k2", "--material", "PLA", "--json")
    assert rc == 0, err
    d = json.loads(text)
    s = d["settings"]
    assert (s["nozzle_c"], s["nozzle_first_layer_c"], s["bed_c"], s["bed_first_layer_c"]) == (223, 226, 57, 61)
    assert s["retract_mm"] == 0.6 and s["retract_speed_mms"] == 35 and s["flow_ratio"] == 0.96
    assert s["travel_speed_mms"] == 180 and s["source"] == f"pack my-k2 {sha[:12]}"
    argv = json.loads(fake_slicer.read_text())
    assert argv[argv.index("--temperature") + 1] == "223" and argv[argv.index("--retract-speed") + 1] == "35"
    assert argv[argv.index("--extrusion-multiplier") + 1] == "0.96"
    assert "<paired printer name>" in d["next"]
    # The sliced file uploads under its own name (no --name), as the slice NEXT line and the skill say.
    gcode = Path(d["gcode"])
    assert gcode.name == "part_pack_my-k2_PLA.gcode", gcode.name
    brain = fakes.Brain(secret=None)
    srv = fakes.serve_http(brain, fakes.moonraker_routes)
    try:
        rc, _, err = run(capsys, "pair", "fake-k2", "--type", "moonraker", "--host", "127.0.0.1", "--port",
                         srv.server_address[1], "--model", "creality-k2")
        assert rc == 0, err
        rc, text, err = run(capsys, "upload", gcode, "--printer", "fake-k2", "--json")
        assert rc == 0, err
    finally:
        srv.shutdown()
    remote = json.loads(text)["remote_name"]
    assert remote.startswith("part_pack_my-k2_PLA-") and brain.files[remote] == gcode.read_bytes()

    rc, text, _ = run(capsys, "slice", model, "--printer", "pack:my-k2", "--material", "Matte PLA")
    assert rc == 0 and f"settings from pack my-k2 {sha[:12]}" in text

    stl = mesh.write_stl(tmp_path / "cube.stl", shapes.cube())
    rc, text, err = run(capsys, "plan", stl, "--printer", "pack:my-k2", "--material", "PLA", "--goal", "balanced",
                        "--json")
    assert rc == 0, err
    p = json.loads(text)
    assert p["source"] == f"pack my-k2 {sha[:12]}" and p["settings"]["nozzle_c"] == 223
    assert p["estimate"]["flow_cap_mm3s"] == 15.5 and p["check"]["ok"]
    assert p["settings"]["inner_wall_speed_mms"] <= 180
    rc, text, _ = run(capsys, "plan", stl, "--printer", "pack:my-k2", "--material", "PLA")
    assert rc == 0 and f"settings from pack my-k2 {sha[:12]}" in text

    rc, _, err = run(capsys, "slice", model, "--printer", "pack:my-k2", "--material", "PETG")
    assert rc == 1 and "no settings for 'PETG'" in err

    rc, text, _ = run(capsys, "pack", "remove", "my-k2")
    assert rc == 0 and not (isolated / "packs" / "my-k2").exists()
    rc, _, err = run(capsys, "slice", model, "--printer", "pack:my-k2", "--material", "PLA")
    assert rc == 1 and "no pack 'my-k2' is installed" in err


def test_findings_need_accept_findings(capsys, tmp_path, good, write, fake_slicer):
    good["printer"]["start_gcode"] += "\nLOAD_MATERIAL"
    f = write(good)
    sha = packs.sha256(f.read_bytes())
    rc, text, _ = run(capsys, "pack", "check", f)
    assert rc == 0 and "FINDING printer.start_gcode line 8" in text
    rc, _, _ = run(capsys, "pack", "check", f, "--strict")
    assert rc == 1
    rc, text, _ = run(capsys, "pack", "show", f)
    assert "LOAD_MATERIAL" in text and "<- FINDING: LOAD_MATERIAL is a printer macro" in text
    assert f"--accept {sha[:12]} --accept-findings" in text
    rc, _, err = run(capsys, "pack", "import", f, "--accept", sha[:12])
    assert rc == 3 and "finding(s) that need the person's acceptance" in err
    rc, text, _ = run(capsys, "pack", "import", f, "--accept", sha[:12], "--accept-findings")
    assert rc == 0 and "1 finding(s) accepted" in text
    model = tmp_path / "part.stl"
    model.write_bytes(STL)
    assert run(capsys, "slice", model, "--printer", "pack:test-k2", "--material", "PLA")[0] == 0


def test_refused_pack_is_never_installed(capsys, good, write):
    good["printer"]["end_gcode"] = "M500"
    f = write(good)
    sha = packs.sha256(f.read_bytes())
    rc, text, _ = run(capsys, "pack", "show", f)
    assert rc == 1 and "<- REFUSAL: M500 saves settings" in text
    rc, _, err = run(capsys, "pack", "import", f, "--accept", sha[:12], "--accept-findings")
    assert rc == 3 and "refused" in err
    assert not (packs.packs_dir() / "test-k2").exists()


def test_import_never_overwrites_a_different_file_without_replace(capsys, good, write):
    f1 = write(good, "a.json")
    s1 = packs.sha256(f1.read_bytes())
    assert run(capsys, "pack", "import", f1, "--accept", s1[:12])[0] == 0
    assert run(capsys, "pack", "import", f1, "--accept", s1)[0] == 0   # the same file again, full hash: fine
    good["version"] = "1.3.0"
    f2 = write(good, "b.json")
    s2 = packs.sha256(f2.read_bytes())
    rc, _, err = run(capsys, "pack", "import", f2, "--accept", s2[:12])
    assert rc == 3 and "different file" in err
    rc, _, _ = run(capsys, "pack", "import", f2, "--accept", s2[:12], "--replace")
    assert rc == 0
    assert sorted(p.name for p in (packs.packs_dir() / "test-k2").iterdir()) == [f"{s2}.json"]


def test_tampered_install_is_refused(capsys, tmp_path, good, write):
    f = write(good)
    sha = packs.sha256(f.read_bytes())
    assert run(capsys, "pack", "import", f, "--accept", sha[:12])[0] == 0
    inst = packs.packs_dir() / "test-k2" / f"{sha}.json"
    inst.write_bytes(inst.read_bytes().replace(b"223", b"239"))
    model = tmp_path / "part.stl"
    model.write_bytes(STL)
    rc, _, err = run(capsys, "slice", model, "--printer", "pack:test-k2", "--material", "PLA")
    assert rc == 3 and "changed after import" in err
    rc, text, _ = run(capsys, "pack", "list")
    assert "PROBLEM" in text


def test_pack_ids_cannot_reach_outside_the_packs_folder(capsys):
    rc, _, err = run(capsys, "pack", "remove", "../../etc")
    assert rc == 1 and "not allowed" in err


def test_check_json(capsys, good, write):
    good["notes"] = "at 10.1.2.3"
    rc, text, _ = run(capsys, "pack", "check", write(good), "--json")
    d = json.loads(text)
    assert rc == 1 and not d["ok"]
    r = d["files"][0]["refusals"][0]
    assert r["path"] == "notes" and "IP address" in r["reason"] and "10.1.2.3" not in r["text"]


# ---------------------------------------------------------------- the community folder and CI


COMMUNITY = ROOT / "profiles" / "community"


def test_community_example_passes(capsys):
    files = sorted(COMMUNITY.glob("*.json"))
    assert files, "profiles/community has no pack"
    k2 = COMMUNITY / "creality-k2-pla.json"
    pack, rep, data = packs.check_file(k2)
    assert rep.result == "pass", rep.items
    assert pack["printer"]["model"] == "Creality K2" and [m["family"] for m in pack["materials"]] == ["PLA"]
    prof = packs.to_profile(pack)
    from gantrel_printer import slicing
    k2p = slicing.PRINTERS["creality-k2"]
    for f in ("bed", "height", "flavor", "max_speed", "accel", "max_accel", "top_speed", "hotend_max_c", "bed_max_c",
              "max_flow", "hardened_nozzle", "enclosed", "direct_drive", "chamber_heater", "start_gcode", "end_gcode",
              "orca_machine"):
        assert getattr(prof, f) == getattr(k2p, f), f
    # Everything the K2 profile cites, the pack cites too (its own values, from the pack).
    assert set(k2p.cited) <= set(prof.cited) and prof.limits_source.startswith("pack creality-k2-pla")
    pla = slicing.MATERIALS["PLA"]
    m = pack["materials"][0]
    assert (m["nozzle_c"], m["first_layer_nozzle_c"], m["bed_c"], m["first_layer_bed_c"], m["retraction_mm"]) == \
        (pla.nozzle, pla.nozzle_first, pla.bed, pla.bed_first, pla.retract_mm)
    assert data == packs.dumps(pack), "the example is written exactly as pack export writes"


def ci_command() -> str:
    wf = (ROOT / ".github" / "workflows" / "printers.yml").read_text()
    lines = [ln.strip() for ln in wf.splitlines() if "pack check" in ln]
    assert len(lines) == 1, lines
    return lines[0].removeprefix("- run:").strip()


def test_ci_step_command_passes_on_the_repo():
    cmd = ci_command()
    assert "profiles/community/*.json" in cmd
    argv = shlex.split(cmd)
    i = argv.index("gantrel-printer")
    rest = [x for a in argv[i + 1:] for x in (sorted(glob.glob(str(ROOT / a))) if "*" in a else [a])]
    env = {k: v for k, v in os.environ.items() if k != "GANTREL_PRINTER_HOME"}
    proc = subprocess.run([sys.executable, "-m", "gantrel_printer.cli", *rest], capture_output=True, text=True,
                          cwd=ROOT, env=env, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "creality-k2-pla.json: PASS" in proc.stdout


def test_ci_step_fails_on_a_refused_pack(tmp_path, good):
    good["notes"] = "my printer is 192.168.1.9"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(good))
    proc = subprocess.run([sys.executable, "-m", "gantrel_printer.cli", "pack", "check", str(bad)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1 and "REFUSED" in proc.stdout


# A literal backslash-n reads as one line to the policy, but the slicer turns it into a second command.
@pytest.mark.parametrize("field", ["start_gcode", "end_gcode"])
@pytest.mark.parametrize("line", ["G28\\nSAVE_CONFIG", "M117 a\\nM500", "G28\\nRUN_SHELL_COMMAND CMD=x"])
def test_backslash_in_gcode_is_refused_by_check_and_import(capsys, good, write, field, line):
    good["printer"][field] += "\n" + line
    n = good["printer"][field].count("\n") + 1
    f = write(good)
    sha = packs.sha256(f.read_bytes())
    rc, text, _ = run(capsys, "pack", "check", f)
    assert rc == 1 and f"REFUSAL printer.{field} line {n}: holds a backslash" in text, text
    rc, _, err = run(capsys, "pack", "import", f, "--accept", sha[:12], "--accept-findings")
    assert rc == 3 and "refused" in err and "backslash" in err
    assert not (packs.packs_dir() / "test-k2").exists()


def import_example(capsys) -> str:
    f = COMMUNITY / "creality-k2-pla.json"
    sha = packs.sha256(f.read_bytes())
    rc, _, err = run(capsys, "pack", "import", f, "--accept", sha[:12])
    assert rc == 0, err
    return sha


def test_material_max_flow_reaches_both_slicers(capsys, tmp_path, fake_slicer, fake_orca):
    import_example(capsys)
    model = tmp_path / "part.stl"
    model.write_bytes(STL)
    rc, text, err = run(capsys, "slice", model, "--printer", "pack:creality-k2-pla", "--material", "PLA", "--json")
    assert rc == 0, err
    assert json.loads(text)["settings"]["max_flow_mm3s"] == 14
    argv = json.loads(fake_slicer.read_text())
    assert float(argv[argv.index("--max-volumetric-speed") + 1]) == 14
    rc, text, err = run(capsys, "slice", model, "--printer", "pack:creality-k2-pla", "--material", "PLA",
                        "--slicer", "orcaslicer", "--json")
    assert rc == 0, err
    fil = json.loads(fake_orca.read_text())["filament"]
    assert [float(v) for v in fil["filament_max_volumetric_speed"]] == [14]
    # A built-in profile carries the flow cap the planner uses for it (here Creality's Generic PLA @K2-all).
    from gantrel_printer import materials, slicing
    from gantrel_printer.plan import estimate
    k2 = slicing.PRINTERS["creality-k2"]
    s = slicing.settings(k2, slicing.material("PLA"))
    assert s["max_flow_mm3s"] == estimate.flow_cap(k2, materials.get("PLA"))
    assert float(slicing.orca_overrides(s, k2)[2]["filament_max_volumetric_speed"][0]) == s["max_flow_mm3s"]


def test_orca_writes_the_packs_reviewed_start_and_end_gcode(capsys, tmp_path, good, write, fake_orca):
    f = write(good)
    sha = packs.sha256(f.read_bytes())
    assert run(capsys, "pack", "import", f, "--accept", sha[:12])[0] == 0
    model = tmp_path / "part.stl"
    model.write_bytes(STL)
    rc, _, err = run(capsys, "slice", model, "--printer", "pack:test-k2", "--material", "PLA",
                     "--slicer", "orcaslicer", "--json")
    assert rc == 0, err
    machine = json.loads(fake_orca.read_text())["machine"]
    # The exact text the person reviewed, with real line breaks, as Orca's own machine profiles hold it.
    assert machine["machine_start_gcode"] == good["printer"]["start_gcode"]
    assert machine["machine_end_gcode"] == good["printer"]["end_gcode"]
    assert "\\" not in machine["machine_start_gcode"]
    # A built-in profile still uses the bundled Orca machine's own G-code.
    from gantrel_printer import slicing
    k2 = slicing.PRINTERS["creality-k2"]
    machine, _, _ = slicing.orca_overrides(slicing.settings(k2, slicing.material("PLA")), k2)
    assert "machine_start_gcode" not in machine and "machine_end_gcode" not in machine


@pytest.mark.parametrize("where, before, secret", [
    ("notes", "Questions to ", "maker@example.com"),
    ("notes", "printer password: ", "Hunter2Secret"),
    ("notes", "my key ", "ghp_A1b2C3d4E5f6G7h8I9j0K1l2"),
    ("start_gcode", "LOAD_MATERIAL ; host ", "10.0.0.7"),
])
def test_show_withholds_a_pack_that_holds_private_data(capsys, good, write, where, before, secret):
    if where == "notes":
        good["notes"] += f"\n{before}{secret}"
    else:
        good["printer"]["start_gcode"] += f"\n{before}{secret}"
    f = write(good)
    rc, text, err = run(capsys, "pack", "show", f)
    assert rc == 1 and secret not in text + err
    assert "REFUSAL" in text and "withheld" in text and "accept code" not in text and "--accept" not in text
    assert "Tuned on a K2" not in text   # none of the pack's contents, not only the private line
    rc, text, err = run(capsys, "pack", "show", f, "--json")
    d = json.loads(text)
    assert rc == 1 and secret not in text + err
    assert d["pack"] is None and "accept" not in d and d["refusals"]
    rc, text, err = run(capsys, "pack", "check", f, "--json")
    assert rc == 1 and secret not in text + err


def test_reimporting_an_accepted_pack_is_a_no_op(capsys, tmp_path, good, write, fake_slicer):
    good["printer"]["start_gcode"] += "\nLOAD_MATERIAL"
    f = write(good)
    sha = packs.sha256(f.read_bytes())
    d = packs.packs_dir() / "test-k2"
    want = sorted([f"{sha}.json", f"{sha}.findings-accepted.json"])
    for extra in ([], [], ["--replace"]):
        rc, text, err = run(capsys, "pack", "import", f, "--accept", sha[:12], "--accept-findings", *extra)
        assert rc == 0, err
        assert sorted(p.name for p in d.iterdir()) == want
    rc, text, _ = run(capsys, "pack", "list", "--json")
    assert [r["id"] for r in json.loads(text)["packs"]] == ["test-k2"]
    model = tmp_path / "part.stl"
    model.write_bytes(STL)
    rc, _, err = run(capsys, "slice", model, "--printer", "pack:test-k2", "--material", "PLA")
    assert rc == 0, err


@pytest.mark.parametrize("body", [b"null", b"null\n", b"0", b"true", b'"pack"', b"[]"])
def test_a_file_that_is_not_one_json_object_is_refused(capsys, write, body):
    f = write(body)
    sha = packs.sha256(body)
    rep = packs.check_bytes(body)[1]
    assert rep.result == "refused" and "one JSON object" in rep.refusals[0].reason
    rc, text, _ = run(capsys, "pack", "check", f)
    assert rc == 1 and "REFUSED" in text and "one JSON object" in text
    rc, _, err = run(capsys, "pack", "import", f, "--accept", sha[:12])
    assert rc == 3 and "one JSON object" in err
