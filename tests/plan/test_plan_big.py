"""A million-triangle part plans in seconds and in bounded memory, and gets the orientation an exact run on
a coarse copy of the same shape gets.

Before the fix (review r2 first-F1), every one of the 40 candidates kept a turned copy of the whole mesh and
ran per-facet Python loops: on the machine this was measured on, this part ran 355 s and then failed to get
memory under an 8 GB cap. After it, the same plan took about 10 s and under 0.6 GB.
"""

import time
import tracemalloc

import numpy as np

import shapes
from gantrel_printer import slicing
from gantrel_printer.plan import mesh as pmesh, optimize, orient

BUDGET_S = 60          # the old planner needed over 355 s for this part; the new one about 10 s under load
PEAK_BYTES = 2 << 30   # the old one needed over 8 GB


def test_a_million_triangle_part_plans_fast_and_matches_the_coarse_plan():
    printer = slicing.PRINTERS["creality-k2"]
    coarse = optimize.plan(shapes.terrain(20), printer, "PLA", "quality")
    assert np.allclose(coarse["orientations"][0]["down_vector"], shapes.TERRAIN_BED_DOWN, atol=1e-3)

    m = pmesh.read_stl(pmesh.stl_bytes(shapes.terrain_tris(500)))   # through the STL reader, as the CLI loads it
    total = len(m.tris)
    assert 1_000_000 <= total <= pmesh.MAX_TRIANGLES

    tracemalloc.start()
    t0 = time.perf_counter()
    p = optimize.plan(m, printer, "PLA", "quality")
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert elapsed < BUDGET_S, f"planning {total} triangles took {elapsed:.0f} s"
    assert peak < PEAK_BYTES, f"planning {total} triangles peaked at {peak / 2**30:.1f} GB"

    best = p["orientations"][0]
    assert np.allclose(best["down_vector"], coarse["orientations"][0]["down_vector"], atol=1e-3)
    assert p["settings"]["supports"] == coarse["settings"]["supports"] == "off"
    # The plan says what it scored on, and the numbers it reports for the part come from the full mesh.
    assert coarse["orientation_scoring"]["simplified"] is False
    sc = p["orientation_scoring"]
    assert sc["simplified"] and sc["triangles"] == total and 0 < sc["scored_on_triangles"] < total
    assert f"scored on {sc['scored_on_triangles']} of {total} triangles" in sc["note"]
    assert best["scored_on_triangles"] == total            # the chosen one was measured again on the full mesh
    assert abs(p["part"]["volume_mm3"] - 80 * 60 * 12) < 1
    assert abs(best["height_mm"] - 15.0) < 0.01             # 12 mm block plus 3 mm waves
    assert abs(best["bed_contact_mm2"] - 80 * 60) < 1        # the whole flat bottom


def test_the_simplified_copy_is_deterministic_and_keeps_the_shape():
    m = shapes.terrain(200)
    a, b = m.simplified(20_000), m.simplified(20_000)
    assert np.array_equal(a.tris, b.tris)
    assert len(a.tris) <= 1.25 * 20_000
    assert 0.5 * 20_000 <= len(a.tris)
    assert abs(a.volume - m.volume) < 0.03 * m.volume      # edges round off by about one grid cube
    assert np.allclose(a.size, m.size, atol=1.0)


def test_scoring_on_a_copy_picks_what_full_scoring_picks(monkeypatch):
    """Forcing the copy on a mid-size part gives the same best orientation as scoring it in full."""
    printer = slicing.PRINTERS["creality-k2"]
    m = shapes.mushroom()
    full = optimize.plan(m, printer, "PLA", "quality")
    monkeypatch.setattr(orient, "SCORE_FULL_MAX", 10)
    monkeypatch.setattr(orient, "SCORE_PROXY_TRIANGLES", 40)
    proxied = optimize.plan(m, printer, "PLA", "quality")
    assert proxied["orientations"][0]["down_vector"] == full["orientations"][0]["down_vector"]
    assert proxied["orientations"][0]["support_volume_mm3"] == full["orientations"][0]["support_volume_mm3"]
