"""Filament properties by family, with a cited source for every number.

The numbers live in data/materials.json. Each family lists the URLs it was taken from and a
"basis" line per field saying which source gave which value and how disagreements were settled
(the conservative value wins). This module only loads and serves them.

slicing.py keeps its own set points (the temperature it prints at, its fan and retraction), but the
facts it shares with this table (does the material need an enclosure, is it flexible) are read
from here, so there is one source of truth.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

DATA = Path(__file__).with_name("data") / "materials.json"

# Other names people use for a family.
ALIASES = {"NYLON": "PA", "PA6": "PA", "PA12": "PA", "TPU95A": "TPU", "TPU 95A": "TPU", "TPU-95A": "TPU",
           "PA6-CF": "PA-CF", "PA12-CF": "PA-CF", "NYLON-CF": "PA-CF", "PLA+": "PLA", "PETG+": "PETG"}


@dataclass(frozen=True)
class Filament:
    name: str
    nozzle_min_c: int
    nozzle_max_c: int
    bed_min_c: int
    bed_max_c: int
    density_g_cm3: float
    max_flow_standard_mm3s: float
    max_flow_high_flow_mm3s: float
    fan_min_pct: int
    fan_max_pct: int
    needs_enclosure: bool
    max_chamber_c: int
    abrasive: bool
    flexible: bool
    glass_transition_c: float | None
    dry_c: int | None
    dry_hours: float | None
    max_overhang_deg: float
    min_layer_time_s: float
    sources: tuple[str, ...] = ()
    basis: dict = field(default_factory=dict, compare=False, hash=False)

    def max_flow(self, high_flow_hotend: bool) -> float:
        """The material's volumetric flow cap in mm3/s on this kind of hotend."""
        return self.max_flow_high_flow_mm3s if high_flow_hotend else self.max_flow_standard_mm3s

    def source_urls(self) -> list[str]:
        return [SOURCES[s] for s in self.sources]


def _load() -> tuple[dict[str, Filament], dict[str, str]]:
    raw = json.loads(DATA.read_text())
    sources = raw["sources"]
    fams = {}
    for name, d in raw["families"].items():
        d = dict(d)
        missing = [s for s in d["sources"] if s not in sources]
        if missing:
            raise ValueError(f"materials.json: {name} cites unknown sources {missing}")
        d["sources"] = tuple(d["sources"])
        fams[name] = Filament(name=name, **d)
    return fams, sources


FAMILIES, SOURCES = _load()

VENDOR_FLOW_DATA = DATA.with_name("printer_material_flow.json")


def _load_vendor_flow() -> tuple[dict, str]:
    raw = json.loads(VENDOR_FLOW_DATA.read_text())
    return raw["printers"], raw["commit"]


VENDOR_FLOW, VENDOR_FLOW_COMMIT = _load_vendor_flow()


def vendor_flow(printer_key: str, family: str) -> dict | None:
    """The printer vendor's own Generic filament preset for this family in OrcaSlicer, if there is one:
    {"mm3s", "preset", "file", "url", ...}. See data/printer_material_flow.json."""
    entry = VENDOR_FLOW.get(printer_key, {}).get("families", {}).get(get(family).name)
    return dict(entry) if entry else None


def get(name: str) -> Filament:
    key = name.strip().upper()
    key = ALIASES.get(key, key)
    if key not in FAMILIES:
        raise ConfigError(f"no data for filament {name!r}", f"Use one of: {', '.join(FAMILIES)}")
    return FAMILIES[key]
