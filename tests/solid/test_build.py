"""gantrel-solid build and measure against the real SolidCraft CLI and smoothstone's stone."""

from __future__ import annotations

import json
import shutil
import struct
from pathlib import Path

import pytest

from gantrel_solid import build as solid_build

DESIGNS = Path(__file__).parent / "designs"

SIZES = {"plate": [80, 40, 5], "enclosure": [60, 40, 33], "collar": [24, 24, 10]}


def build(solid, design, job="solid-job"):
    return solid("build", str(DESIGNS / f"{design}.json"), "--job", job)


@pytest.mark.parametrize("design", sorted(SIZES))
def test_build_writes_checked_parts_of_the_asked_size(solid, design):
    code, out = build(solid, design)
    assert code == 0 and out["ok"], out
    job = solid.work / "solid-job" / "build"
    assert out["files"] == [str(job / f) for f in solid_build.PARTS] and out["fixes"] == []
    assert {"watertight", "bodies", "resting", "plate"} <= set(out["checks"]) and all(out["checks"].values())
    assert json.loads((job / "design.json").read_text()) == json.loads((DESIGNS / f"{design}.json").read_text())
    assert json.loads((job / "check.json").read_text())["passed"] is True
    for f in ("part.step", "part.stl", "part.3mf"):
        code, m = solid("measure", str(job / f))
        assert code == 0 and m["size_mm"] == pytest.approx(SIZES[design], abs=0.1), (f, m)


def test_a_bad_command_names_its_index_and_writes_nothing(solid):
    code, out = build(solid, "bad_edge")
    assert code == 1 and out["ok"] is False and out["files"] == []
    assert out["index"] == 1 and out["command"] == "solid.fillet" and out["error"].startswith("commands[1] solid.fillet")
    assert not (solid.work / "solid-job" / "build").exists() or [
        p.name for p in (solid.work / "solid-job" / "build").iterdir()] == []


def test_two_bodies_fail_with_the_one_piece_fix(solid):
    code, out = build(solid, "two_bodies")
    assert code == 1 and out["ok"] is False and out["checks"]["bodies"] is False
    assert any(f.startswith("bodies:") and "separate pieces" in f for f in out["fixes"])


@pytest.mark.parametrize("cid", ["file.export", "solid.import_mesh"])
def test_a_design_that_reaches_files_is_refused(run, cid):
    d = run.work / "d.json"
    d.write_text('{"commands": [{"command": "solid.box", "params": {"length": 1, "width": 1, "height": 1}},'
                 f' {{"command": "{cid}", "params": {{"path": "/tmp/x.stl"}}}}]}}')
    code, out = run("build", str(d))
    assert code == 2 and out["error"].startswith(f"commands[1] {cid}") and not (run.work / "solid-job").exists()


def test_a_failed_build_leaves_no_earlier_parts(solid):
    assert build(solid, "plate")[0] == 0
    code, out = build(solid, "bad_edge")
    assert code == 1 and out["files"] == []
    assert [p.name for p in (solid.work / "solid-job" / "build").iterdir()] == []


@pytest.mark.parametrize("printed, exit_code", [
    ("PASS watertight: closed\nFIX bodies two pieces\n", 1),
    ("PASS watertight: closed\n", 1),
    ("PASS watertight: closed\nFIX: bodies two pieces; join them\n", 0),
])
def test_a_stone_verdict_its_lines_do_not_show_is_refused(tmp_path, monkeypatch, printed, exit_code):
    stone = tmp_path / "bin" / "stone"
    stone.parent.mkdir()
    stone.write_text(f"#!/bin/sh\nprintf '{printed}'\nexit {exit_code}\n")
    stone.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stone.parent}:/usr/bin:/bin")
    with pytest.raises(solid_build.Refused):
        solid_build.stone_checks(tmp_path / "part.stl")


@pytest.mark.parametrize("how", ["dotdot", "absolute", "symlink", "build-symlink"])
def test_build_refuses_a_job_outside_the_working_folder(run, tmp_path, how):
    outside = tmp_path / "outside"
    outside.mkdir()
    job = {"dotdot": "../outside", "absolute": str(outside), "symlink": "link", "build-symlink": "job"}[how]
    if how == "symlink":
        (run.work / "link").symlink_to(outside)
    if how == "build-symlink":
        (run.work / "job").mkdir()
        (run.work / "job" / "build").symlink_to(outside)
    code, out = run("build", str(DESIGNS / "plate.json"), "--job", job)
    assert code == 2 and out["ok"] is False and "--job" in out["error"]
    assert list(outside.iterdir()) == []


def refused_design(work):
    d = work / "d.json"
    d.write_text('{"commands": [{"command": "file.export", "params": {"path": "/tmp/x.stl"}}]}')
    return d


def test_a_refused_build_leaves_no_earlier_parts(run):
    out = run.work / "solid-job" / "build"
    out.mkdir(parents=True)
    for f in ("design.json", *solid_build.PARTS, "check.json"):
        (out / f).write_text("from an earlier build")
    code, res = run("build", str(refused_design(run.work)))
    assert code == 2 and res["error"].startswith("commands[0] file.export")
    assert list(out.iterdir()) == []


def test_a_clear_that_fails_still_removes_the_earlier_passing_check(run):
    out = run.work / "solid-job" / "build"
    out.mkdir(parents=True)
    (out / "check.json").write_text('{"passed": true}')
    (out / "part.step").mkdir()
    code, res = run("build", str(DESIGNS / "plate.json"))
    assert code == 1 and res["ok"] is False and "cannot use the job folder" in res["error"], res
    assert not (out / "check.json").exists()


def cube_stl(path, mm=10.0):
    v = [(x, y, z) for x in (0, mm) for y in (0, mm) for z in (0, mm)]
    faces = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 4), (1, 5, 4),
             (2, 6, 3), (3, 6, 7), (0, 4, 2), (2, 4, 6), (1, 3, 5), (3, 7, 5)]
    data = b"\0" * 80 + struct.pack("<I", len(faces))
    for f in faces:
        data += struct.pack("<3f", 0, 0, 0) + b"".join(struct.pack("<3f", *v[i]) for i in f) + b"\0\0"
    path.write_bytes(data)


def fake_solidcraft(tmp_path, after: str = "") -> Path:
    """A solidcraft-cli that writes a 10 mm cube for each --out, then runs `after` with $job set."""
    stl = tmp_path / "cube.stl"
    cube_stl(stl)
    exe = tmp_path / "fake" / "solidcraft-cli"
    exe.parent.mkdir()
    exe.write_text(f'#!/bin/sh\n[ "$1" = --version ] && {{ echo solidcraft-cli 0.50.0; exit 0; }}\n'
                   f'for a; do case "$a" in */part.*) last="$a"; cp {stl} "$a";; esac; done\n'
                   f'work=$(dirname "$last"); job=$(dirname "$(dirname "$work")")\n{after}\n')
    exe.chmod(0o755)
    return exe


def test_a_job_folder_that_is_a_file_prints_one_json_line(run, tmp_path):
    run.env["GANTREL_SOLIDCRAFT"] = str(fake_solidcraft(tmp_path))
    run.env["PATH"] = f"{tmp_path / 'fake'}:{run.env['PATH']}"
    (tmp_path / "fake" / "stone").write_text("#!/bin/sh\nexit 1\n")
    (tmp_path / "fake" / "stone").chmod(0o755)
    (run.work / "notes.txt").write_text("mine")
    code, out = run("build", str(DESIGNS / "plate.json"), "--job", "notes.txt")
    assert code == 1 and out["ok"] is False and out["files"] == [] and "notes.txt" in out["error"]
    assert (run.work / "notes.txt").read_text() == "mine"


def test_a_build_folder_swapped_for_a_symlink_during_the_run_gets_nothing(run, tmp_path):
    if not shutil.which("stone"):
        pytest.skip("stone (smoothstone) is not on PATH; build runs its print checks")
    outside, victim = tmp_path / "outside", tmp_path / "victim.txt"
    victim.write_text("mine")
    swap = (f'mv "$job/build" "$job/build.real"; mkdir -p {outside}; '
            f'cp -r "$job/build.real/$(basename "$work")" {outside}/; '
            f'ln -s {victim} {outside}/"$(basename "$work")"/check.json; ln -s {outside} "$job/build"')
    run.env["GANTREL_SOLIDCRAFT"] = str(fake_solidcraft(tmp_path, swap))
    code, out = run("build", str(DESIGNS / "plate.json"))
    assert code == 1 and out["ok"] is False and out["files"] == [] and "changed during the run" in out["error"], out
    assert victim.read_text() == "mine"
    [planted] = outside.iterdir()
    assert sorted(p.name for p in planted.iterdir()) == sorted(("check.json", "design.json", *solid_build.PARTS))
    assert (planted / "check.json").is_symlink()
    assert list((run.work / "solid-job" / "build.real").iterdir()) == []


def test_a_rebuild_from_the_last_builds_own_design_succeeds(solid):
    assert build(solid, "plate")[0] == 0
    design = solid.work / "solid-job" / "build" / "design.json"
    code, out = solid("build", str(design), "--job", "solid-job")
    assert code == 0 and out["ok"], out
    assert json.loads(design.read_text()) == json.loads((DESIGNS / "plate.json").read_text())


def test_a_job_named_through_a_symlinked_working_folder_is_inside_it(run, tmp_path):
    link = tmp_path / "proj"
    link.symlink_to(run.work)
    run.env["PWD"] = str(link)
    code, out = run("build", str(refused_design(run.work)), "--job", f"{link}/job", cwd=link)
    assert code == 2 and out["error"].startswith("commands[0] file.export"), out
