"""gantrel-solid build and measure against the real SolidCraft CLI and smoothstone's stone."""

from __future__ import annotations

import json
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


def test_a_design_that_reaches_files_is_refused(run):
    d = run.work / "d.json"
    d.write_text('{"commands": [{"command": "solid.box", "params": {"length": 1, "width": 1, "height": 1}},'
                 ' {"command": "file.export", "params": {"path": "/tmp/x.stl"}}]}')
    code, out = run("build", str(d))
    assert code == 2 and out["error"].startswith("commands[1] file.export") and not (run.work / "solid-job").exists()


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
