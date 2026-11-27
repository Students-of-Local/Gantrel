"""The solidcraft skill's worked designs are the tested designs, /solid loads it, and Studio shows the part."""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from pathlib import Path

import pytest

DESIGNS = Path(__file__).parent / "designs"
ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "plugin" / "solidcraft" / "SKILL.md"
WORKED = ("plate", "enclosure", "collar")


def designs() -> list[dict]:
    return [json.loads((DESIGNS / f"{n}.json").read_text()) for n in WORKED]


def test_the_worked_designs_are_the_tested_ones():
    blocks = re.findall(r"```json\n(.*?)\n```", SKILL.read_text(), re.S)
    assert [json.loads(b) for b in blocks] == designs()


def test_the_command_table_is_what_the_designs_use():
    table = set(re.findall(r"^\| `([a-z_.]+)` \|", SKILL.read_text(), re.M))
    used = {c["command"] for d in designs() for c in d["commands"]}
    assert table == used


def test_solid_loads_the_solidcraft_skill():
    m = tomllib.loads((ROOT / "plugin.toml").read_text())
    cmds = {c["name"]: c for c in m["commands"]}
    assert cmds["solid"]["skill"] == "solidcraft"
    assert re.search(r"^name: solidcraft$", SKILL.read_text(), re.M)


def test_studio_shows_a_solid_job(solid):
    from gantrel_studio.server import Studio

    job = solid.work / "solid-job"
    job.mkdir()
    shutil.copy(DESIGNS / "plate.json", job / "design.json")
    code, out = solid("build", "--job", "solid-job")
    assert code == 0, out
    state = Studio(job).state()
    assert state["part"] and state["part"]["size_mm"] == pytest.approx([80, 40, 5], abs=0.1)
    assert state["check"]["passed"] is True
    assert {r["name"] for r in state["check"]["results"]} >= {"watertight", "bodies", "plate"}
