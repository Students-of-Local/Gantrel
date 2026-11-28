"""Deterministic contract and geometry verification tests for skills/runtime/parametric-cad-dfm."""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pytest

from gantrel_printer.plan import cad_verify

ROOT = Path(__file__).resolve().parents[3]
SKILL = ROOT / "skills" / "runtime" / "parametric-cad-dfm" / "SKILL.md"


def _box_tris(x0: float, x1: float, y0: float, y1: float, z0: float, z1: float) -> np.ndarray:
    """Outward-wound watertight 12-triangle axis-aligned box."""
    v = np.array(
        [
            [x0, y0, z0],
            [x1, y0, z0],
            [x1, y1, z0],
            [x0, y1, z0],
            [x0, y0, z1],
            [x1, y0, z1],
            [x1, y1, z1],
            [x0, y1, z1],
        ],
        dtype=np.float64,
    )
    # Counter-clockwise outward triangles (positive signed volume)
    faces = [
        (0, 3, 2), (0, 2, 1),  # bottom (-z)
        (4, 5, 6), (4, 6, 7),  # top (+z)
        (0, 1, 5), (0, 5, 4),  # front (-y)
        (1, 2, 6), (1, 6, 5),  # right (+x)
        (2, 3, 7), (2, 7, 6),  # back (+y)
        (3, 0, 4), (3, 4, 7),  # left (-x)
    ]
    return np.array([[v[a], v[b], v[c]] for a, b, c in faces], dtype=np.float64)


def test_frontmatter_sections_and_style():
    text = SKILL.read_text(encoding="utf-8")
    assert re.match(r"^---\nname: parametric-cad-dfm\ndescription: .+\n---\n", text)
    for section in ("## Goal", "## Inputs", "## Steps", "## Rules and checks", "## Sources"):
        assert section in text, f"missing {section}"
    # Gantrel skill style rule: no em/en dashes or invisible Unicode control chars
    assert chr(0x2014) not in text and chr(0x2013) not in text
    assert not re.search(r"[\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069]", text)


def test_engineering_tables_and_manifold_rules_present():
    text = SKILL.read_text(encoding="utf-8")
    # ISO 286 & FDM/SLA radial clearance classes
    for token in ("H7/p6", "H7/g6", "H9/d9", "0.05 to 0.15 mm", "0.20 to 0.30 mm", "0.40 to 0.50 mm"):
        assert token in text, token
    # ISO 273 metric clearance table
    for screw, dia in (("M2", "2.6"), ("M3", "3.6"), ("M4", "4.7"), ("M5", "5.7"), ("M6", "6.8")):
        assert screw in text and dia in text, (screw, dia)
    # CNC Kitchen heat-set insert bosses
    for token in ("3.3", "4.1", "5.7", "6.5", "8.1", "6.7", "9.1", "13.7"):
        assert token in text, token
    # Manifold / DFM invariants
    for token in ("eps = 0.1", "signed_volume > 0", "teardrop_2d", "45-degree bottom chamfer", "1.2"):
        assert token in text, token


def test_cad_verify_manifold_winding_and_open_mesh():
    good = _box_tris(-10, 10, -10, 10, 0, 10)
    m = cad_verify.analyze_mesh(good)
    assert m["is_watertight"] is True
    assert m["is_manifold"] is True
    assert m["boundary_edges"] == 0
    assert m["multi_manifold_edges"] == 0
    assert m["winding_mismatches"] == 0
    assert m["bodies"] == 1
    assert abs(m["signed_volume"] - 4000.0) < 1e-3
    assert abs(m["min_z"]) < 1e-4
    assert m["overhang_fraction"] == 0.0

    # Inverted winding -> negative volume -> MUST fail is_manifold
    inverted = good[:, [0, 2, 1], :]
    m_inv = cad_verify.analyze_mesh(inverted)
    assert m_inv["is_manifold"] is False
    assert m_inv["signed_volume"] < 0

    # Missing triangle -> open surface -> MUST fail is_watertight
    open_mesh = good[:-1]
    m_open = cad_verify.analyze_mesh(open_mesh)
    assert m_open["is_watertight"] is False
    assert m_open["boundary_edges"] == 3


def test_cad_verify_overhang_and_clash_detection():
    # Raised horizontal slab at z=10..14 has bottom overhang > 45 deg
    floating_slab = _box_tris(-20, 20, -20, 20, 10, 14)
    pillar = _box_tris(-3, 3, -3, 3, 0, 10)
    # Combine two separated/touching shells to test overhang detection
    t_shape = np.concatenate([pillar, floating_slab], axis=0)
    m = cad_verify.analyze_mesh(t_shape)
    assert m["overhang_fraction"] > 0.25

    # Two separated bodies -> no clash
    a = _box_tris(-20, -10, -5, 5, 0, 10)
    b = _box_tris(10, 20, -5, 5, 0, 10)
    assert cad_verify.check_two_body_clash(a, b) is False

    # Two overlapping bodies -> clash detected
    c = _box_tris(-12, 2, -5, 5, 0, 10)
    assert cad_verify.check_two_body_clash(a, c) is True


def test_cad_verify_slice_loops_and_dimensions():
    box = _box_tris(-20, 20, -15, 15, 0, 10)
    loops = cad_verify.slice_loops(box, "z", 5.0)
    outers, holes = cad_verify.classify_loops(loops)
    assert len(outers) == 1 and len(holes) == 0
    assert abs(outers[0]["area"] - 1200.0) < 1e-2
    assert math.isclose(float(outers[0]["bbox"][0]), 40.0, abs_tol=1e-2)
    assert math.isclose(float(outers[0]["bbox"][1]), 30.0, abs_tol=1e-2)


@pytest.mark.skipif(shutil.which("openscad") is None, reason="openscad CLI not installed")
def test_cad_verify_openscad_manifold_plate_and_boss():
    scad = """
$fn = 64; eps = 0.1;
difference() {
    translate([-20, -20, 0]) cube([40, 40, 4]);
    for (x = [-15, 15], y = [-15, 15])
        translate([x, y, -eps]) cylinder(d=3.6, h=4 + 2*eps);
}
"""
    openscad = shutil.which("openscad")
    assert openscad is not None
    with tempfile.TemporaryDirectory() as td:
        scad_path = Path(td) / "part.scad"
        stl_path = Path(td) / "part.stl"
        scad_path.write_text(scad, encoding="utf-8")
        proc = subprocess.run(
            [openscad, "--backend=manifold", "--export-format", "binstl", "-o", str(stl_path), str(scad_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        tris = cad_verify.load_stl(stl_path)
    m = cad_verify.analyze_mesh(tris)
    assert m["is_manifold"] is True and m["bodies"] == 1 and m["overhang_fraction"] <= 0.05
    outers, holes = cad_verify.classify_loops(cad_verify.slice_loops(tris, "z", 2.0))
    assert len(outers) == 1 and len(holes) == 4
    for h in holes:
        assert 3.5 <= h["eq_diameter"] <= 3.7
        assert cad_verify.min_wall_around_hole(h, outers) >= 3.0
