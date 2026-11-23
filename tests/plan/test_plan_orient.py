"""Orientation and supports on golden shapes whose answers are known exactly."""

import math

import numpy as np
import pytest

import shapes
from gantrel_printer import materials, slicing
from gantrel_printer.plan import geometry, orient

PLA = materials.get("PLA")
K2 = slicing.PRINTERS["creality-k2"]
MK4 = slicing.PRINTERS["prusa-mk4"]
UP = np.array([0.0, 0.0, -1.0])   # the file's own bottom faces the bed


def best(m, printer=K2, load_axis=None, goal="quality"):
    return orient.rank(m, printer, PLA, goal, load_axis)[0]


def test_support_volume_matches_the_exact_value():
    for m, exact in ((shapes.mushroom(), shapes.MUSHROOM_UPRIGHT_SUPPORT_MM3),
                     (shapes.arch(), shapes.ARCH_UPRIGHT_SUPPORT_MM3)):
        c = orient.evaluate(m, UP, K2, PLA)
        assert c.support_volume_mm3 == pytest.approx(exact, rel=0.15), m.name
        assert c.needs_support


def test_support_volume_on_a_ledge_that_lands_on_the_part():
    # A 20 x 20 x 5 slab, a 5 x 5 x 10 post, a 20 x 20 x 5 roof: support under the roof lands on the slab.
    m = shapes.boxes((0, 20, 0, 20, 0, 5), (7.5, 12.5, 7.5, 12.5, 5, 15), (0, 20, 0, 20, 15, 20))
    c = orient.evaluate(m, UP, K2, PLA)
    assert c.support_volume_mm3 == pytest.approx((400 - 25) * 10, rel=0.15)
    assert c.support_contact_mm2 == pytest.approx(2 * 375, rel=0.15)


def test_cube_needs_nothing():
    c = best(shapes.cube())
    assert not c.needs_support and c.bed_contact_mm2 == pytest.approx(400) and c.height_mm == pytest.approx(20)


def test_mushroom_flips_onto_its_cap():
    c = best(shapes.mushroom())
    assert not c.needs_support
    assert c.bed_contact_mm2 == pytest.approx(900)
    assert c.down == pytest.approx([0, 0, 1])


def test_arch_lies_on_its_side_and_upright_has_a_bridge():
    c = best(shapes.arch())
    assert not c.needs_support and c.height_mm == pytest.approx(10)
    up = orient.evaluate(shapes.arch(), UP, K2, PLA)
    assert len(up.bridges) == 1 and up.bridges[0]["span_mm"] == pytest.approx(30, rel=0.15)


def test_mushroom_cap_is_a_cantilever_not_a_bridge():
    assert orient.evaluate(shapes.mushroom(), UP, K2, PLA).bridges == []


def test_l_bracket_lies_on_its_plate():
    c = best(shapes.l_bracket())
    assert not c.needs_support and c.bed_contact_mm2 == pytest.approx(1200) and c.height_mm == pytest.approx(35)


def test_pin_stands_without_a_load_and_lies_down_with_one():
    standing = best(shapes.pin())
    assert standing.height_mm == pytest.approx(42)
    lying = best(shapes.pin(), load_axis="z")
    assert lying.strength_penalty == pytest.approx(0, abs=1e-9)
    assert lying.height_mm == pytest.approx(20)
    assert "layers run along the load" in lying.reason


def test_bar_fits_only_turned():
    m = shapes.long_bar()
    flat = orient.evaluate(m, UP, MK4, PLA)
    assert flat.fits and flat.z_rotation_deg == 90 and flat.footprint_mm == pytest.approx((230, 40))
    standing = orient.evaluate(m, np.array([0.0, 1.0, 0.0]), MK4, PLA)
    assert not standing.fits                    # 230 mm tall on a 220 mm printer
    c = best(m, MK4)
    assert c.fits and c.z_rotation_deg == 90
    assert "turned 90 degrees" in c.reason


def test_candidates_are_capped_and_unique():
    d = orient.candidate_directions(shapes.cylinder(sides=200))
    assert len(d) <= orient.MAX_CANDIDATES
    dots = d @ d.T - np.eye(len(d)) * 2
    assert dots.max() < math.cos(math.radians(orient.DUPLICATE_DEG))


def test_rotation_outputs_agree():
    for c in orient.rank(shapes.l_bracket(), K2, PLA):
        r = c.rotation
        assert np.allclose(r @ r.T, np.eye(3)) and np.linalg.det(r) == pytest.approx(1)
        s = c.summary()
        x, y, z = (math.radians(a) for a in s["rotate_xyz_deg"])
        rx = np.array([[1, 0, 0], [0, math.cos(x), -math.sin(x)], [0, math.sin(x), math.cos(x)]])
        ry = np.array([[math.cos(y), 0, math.sin(y)], [0, 1, 0], [-math.sin(y), 0, math.cos(y)]])
        assert np.allclose(geometry.rot_z(math.degrees(z)) @ ry @ rx, r, atol=1e-4)
        axis, ang = np.array(s["axis_angle"]["axis"]), math.radians(s["axis_angle"]["degrees"])
        k = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
        assert np.allclose(np.eye(3) + math.sin(ang) * k + (1 - math.cos(ang)) * k @ k, r, atol=1e-4)
        # The turned mesh really has that direction facing the bed.
        assert np.allclose(r @ np.array(c.down), [0, 0, -1], atol=1e-9)


def test_layer_areas_and_highest_below():
    m = shapes.mushroom()
    z = np.array([10.0, 22.0])
    # 4096 rays over a 30 x 30 part: cells about 0.47 mm wide, so a 10 mm section reads within 10 percent.
    assert geometry.layer_areas(m.tris, z) == pytest.approx([100, 900], rel=0.1)
    pts = np.array([[2.0, 2.0, 19.0], [15.0, 15.0, 30.0]])
    assert geometry.highest_below(pts, m.tris) == pytest.approx([0, 25])


def test_thin_walls():
    thick = orient.thinnest_wall(shapes.cube())
    thin = orient.thinnest_wall(shapes.boxes((0, 20, 0, 0.3, 0, 10)))
    assert thick > 5 and thin == pytest.approx(0.3, abs=0.01)


def test_unknown_goal_and_axis():
    with pytest.raises(ValueError):
        orient.rank(shapes.cube(), K2, PLA, "fastest")
    with pytest.raises(ValueError):
        orient.rank(shapes.cube(), K2, PLA, "quality", "w")
