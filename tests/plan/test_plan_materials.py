"""The filament table: complete, cited, and in agreement with slicing.py's set points."""

import dataclasses

import pytest

from gantrel_printer import materials, slicing
from gantrel_printer.errors import ConfigError

FAMILIES = ("PLA", "PLA-CF", "PETG", "PETG-CF", "ABS", "ASA", "TPU", "PA", "PA-CF", "PC", "PVA", "HIPS")


def test_every_family_is_there_with_sane_ranges():
    assert set(materials.FAMILIES) == set(FAMILIES)
    for f in materials.FAMILIES.values():
        assert 150 <= f.nozzle_min_c <= f.nozzle_max_c <= 350, f.name
        assert 0 <= f.bed_min_c <= f.bed_max_c <= 130, f.name
        assert 0.9 < f.density_g_cm3 < 1.5, f.name
        assert 0 < f.max_flow_standard_mm3s <= f.max_flow_high_flow_mm3s, f.name
        assert 0 <= f.fan_min_pct <= f.fan_max_pct <= 100, f.name
        assert 0 < f.max_overhang_deg < 90 and f.min_layer_time_s > 0, f.name


def test_every_family_cites_real_urls_and_says_how():
    for f in materials.FAMILIES.values():
        assert f.sources, f.name
        assert all(u.startswith("https://") for u in f.source_urls()), f.name
        assert f.basis, f.name


def test_flags():
    get = materials.get
    assert get("ABS").needs_enclosure and get("ASA").needs_enclosure and not get("PLA").needs_enclosure
    assert get("TPU").flexible and not get("PETG").flexible
    assert {n for n, f in materials.FAMILIES.items() if f.abrasive} == {"PLA-CF", "PETG-CF", "PA-CF"}


def test_aliases_and_unknown():
    assert materials.get("nylon").name == "PA"
    assert materials.get("tpu 95a").name == "TPU"
    assert materials.get("PA12-CF").name == "PA-CF"
    with pytest.raises(ConfigError):
        materials.get("unobtainium")


@pytest.mark.parametrize("name", list(slicing.MATERIALS))
def test_slicing_set_points_are_inside_the_cited_ranges(name):
    m, f = slicing.MATERIALS[name], materials.get(name)
    for t in (m.nozzle, m.nozzle_first, *m.nozzle_range):
        assert f.nozzle_min_c <= t <= f.nozzle_max_c, (name, t)
    for t in (m.bed, m.bed_first):
        assert f.bed_min_c <= t <= f.bed_max_c, (name, t)
    # The facts both modules hold come from the table.
    assert m.needs_enclosure == f.needs_enclosure and m.flexible == f.flexible


def test_k2_limits_are_cited():
    k2, plus = slicing.PRINTERS["creality-k2"], slicing.PRINTERS["creality-k2-plus"]
    assert (k2.hotend_max_c, k2.bed_max_c, k2.max_flow, k2.max_accel) == (300, 100, 23, 20000)
    assert k2.hardened_nozzle and not k2.chamber_heater and not k2.high_flow
    assert (plus.hotend_max_c, plus.bed_max_c, plus.max_flow, plus.max_accel) == (350, 120, 40, 30000)
    assert plus.hardened_nozzle and plus.chamber_heater and plus.high_flow
    for p in (k2, plus):
        for k in ("hotend_max_c", "bed_max_c", "max_flow", "hardened_nozzle", "max_accel", "chamber_heater"):
            assert p.is_cited(k), (p.key, k)
        assert "creality.com" in p.limits_source and "OrcaSlicer" in p.limits_source


# What each vendor publishes (the profile comments in slicing.py name the page and its words).
CITED = {
    "bambu-x1c": {"hotend_max_c": 300, "bed_max_c": 110, "hardened_nozzle": True},
    "bambu-p1s": {"hotend_max_c": 300, "bed_max_c": 100, "hardened_nozzle": False},
    "bambu-p1p": {"hotend_max_c": 300, "bed_max_c": 100, "hardened_nozzle": False},
    "bambu-a1": {"hotend_max_c": 300, "bed_max_c": 100, "hardened_nozzle": False},
    "bambu-a1-mini": {"hotend_max_c": 300, "bed_max_c": 80, "hardened_nozzle": False},
    "creality-k1-max": {"hotend_max_c": 300, "hardened_nozzle": True},
    "creality-ender3-v3": {"hotend_max_c": 300, "bed_max_c": 110, "hardened_nozzle": True},
    "prusa-mk4": {"hotend_max_c": 290, "bed_max_c": 120, "hardened_nozzle": False},
}
DEFAULTS = {"hotend_max_c": slicing.DEFAULT_HOTEND_MAX_C, "bed_max_c": slicing.DEFAULT_BED_MAX_C,
            "max_flow": slicing.DEFAULT_MAX_FLOW, "hardened_nozzle": False, "chamber_heater": False, "high_flow": False,
            "top_speed": 0}
# Hardware top speeds, mm/s, from the vendor's spec page, its OrcaSlicer machine profile or its PrusaSlicer profile.
TOP_SPEED = {"creality-k2": 800, "creality-k2-plus": 800, "creality-k1": 600, "creality-k1-max": 800,
             "creality-ender3-v3": 600, "bambu-x1c": 500, "bambu-p1s": 500, "bambu-p1p": 500, "bambu-a1": 500,
             "bambu-a1-mini": 500, "prusa-mk4": 300, "prusa-mk3": 200}


def test_top_speeds_are_cited_and_above_gantrels_caps():
    for key, v in TOP_SPEED.items():
        p = slicing.PRINTERS[key]
        assert p.top_speed == v and p.is_cited("top_speed") and p.limits_source, key
        assert p.max_speed <= p.top_speed and p.accel <= p.max_accel, key


@pytest.mark.parametrize("key", sorted(CITED))
def test_vendor_limits_are_cited(key):
    p = slicing.PRINTERS[key]
    for k, v in CITED[key].items():
        assert getattr(p, k) == v and p.is_cited(k), (key, k)
    assert p.limits_source.startswith("https://")


def test_uncited_limits_are_marked_defaults():
    """A value not in `cited` is Gantrel's default, and only a cited value may differ from it."""
    for p in slicing.PRINTERS.values():
        for k, default in DEFAULTS.items():
            if not p.is_cited(k):
                assert getattr(p, k) == default, (p.key, k)
        if p.cited:
            assert p.limits_source, p.key
    # A printer named in no vendor source keeps every default.
    for key in ("generic-marlin", "generic-klipper", "creality-k1", "prusa-mk3", "elegoo-centauri-carbon"):
        assert not {"hotend_max_c", "bed_max_c", "hardened_nozzle"} & set(slicing.PRINTERS[key].cited), key


def test_printer_profile_is_backward_compatible():
    p = slicing.PrinterProfile("x", "X", (200, 200), 200, "marlin2", accel=3000)
    assert p.max_accel == 3000 and p.hotend_max_c == slicing.DEFAULT_HOTEND_MAX_C and p.cited == ()
    q = dataclasses.replace(p, max_accel=9000)
    assert q.max_accel == 9000
