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


def test_other_printers_use_marked_defaults():
    for p in slicing.PRINTERS.values():
        if p.key.startswith("creality-k2"):
            continue
        assert not p.is_cited("hotend_max_c") and p.hotend_max_c == slicing.DEFAULT_HOTEND_MAX_C
        assert not p.is_cited("max_flow") and p.max_flow == slicing.DEFAULT_MAX_FLOW
        assert not p.hardened_nozzle and not p.chamber_heater
        if p.cited:
            assert p.limits_source


def test_printer_profile_is_backward_compatible():
    p = slicing.PrinterProfile("x", "X", (200, 200), 200, "marlin2", accel=3000)
    assert p.max_accel == 3000 and p.hotend_max_c == slicing.DEFAULT_HOTEND_MAX_C and p.cited == ()
    q = dataclasses.replace(p, max_accel=9000)
    assert q.max_accel == 9000
