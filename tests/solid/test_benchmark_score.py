"""The SolidCraft benchmark's scorer, on designs with known faults, against the real SolidCraft CLI."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BENCH = Path(__file__).parent / "designs" / "bench"
spec = importlib.util.spec_from_file_location("solid_bench", ROOT / "benchmarks" / "solid" / "run.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)
ASKS = {a["id"]: a for a in json.loads((ROOT / "benchmarks" / "solid" / "asks.json").read_text())}


@pytest.mark.parametrize("ask, design, failed", [
    ("spacer", "spacer", []),
    ("spacer", "spacer_turned", []),
    ("spacer", "spacer_blind", ["features"]),
    ("washer", "washer", []),
    ("open-box", "open-box", []),
    ("open-box", "open-box_turned", []),
    ("open-box", "open-box_two_corners", ["features"]),
    ("washer", "washer_off_center", ["features"]),
    ("washer", "washer_6mm", ["features", "volume"]),
])
def test_score_fails_exactly_the_gates_a_design_breaks(solid, tmp_path, monkeypatch, ask, design, failed):
    if not shutil.which("gantrel-solid"):
        pytest.skip("gantrel-solid is not on PATH; the scorer links it into its scratch folder")
    monkeypatch.setenv("GANTREL_SOLIDCRAFT", solid.env["GANTREL_SOLIDCRAFT"])
    work = tmp_path / "ask"
    (work / "solid-job").mkdir(parents=True)
    shutil.copy(BENCH / f"{design}.json", work / "solid-job" / "design.json")
    r = bench.score(ASKS[ask], work)
    assert sorted(g for g, ok in r["gates"].items() if not ok) == failed, r
    assert r["pass"] == (failed == [])


def test_bash_output_that_is_not_utf8_reaches_the_model(tmp_path):
    out = bench.bash(r"printf 'part\377\376'", tmp_path, {"PATH": "/usr/bin:/bin"})
    assert out.startswith("part") and "�" in out


def test_an_asks_file_without_feature_points_stops_before_any_model(tmp_path, monkeypatch):
    asks = tmp_path / "asks.json"
    asks.write_text('[{"id": "old", "ask": "a cube", "size_mm": [1, 1, 1], "volume_mm3": 1}]')
    monkeypatch.setenv("GANTREL_SOLIDCRAFT", "unused")
    monkeypatch.setattr("sys.argv", ["run.py", "--url", "http://127.0.0.1:9", "--model", "m", "--host", "h",
                                     "--asks", str(asks)])
    monkeypatch.setattr(bench, "run_ask", lambda *a: pytest.fail("a model ran"))
    with pytest.raises(SystemExit, match="ask old has no empty_mm, solid_mm"):
        bench.main()
