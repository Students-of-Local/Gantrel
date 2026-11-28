"""Slicing with a per-printer, per-material profile.

PrusaSlicer, OrcaSlicer and Creality Print are AGPL-3.0, so they only ever run as separate programs.
Settings below are conservative starting points: they favor a first print that sticks and comes out
clean over speed. Every value used is printed so the person can see and change it.

PrusaSlicer gets every setting on its command line, so the profile here is the whole profile.
OrcaSlicer builds on its bundled system profile for the printer and gets the same overrides.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import materials
from .errors import ConfigError, PrinterError, Unsupported

# Printer limits the planner's math needs (gantrel_printer/plan). A profile that does not cite a value
# gets these defaults. They are Gantrel's conservative guesses, not printer specs, and the planner
# says so whenever it uses one (see PrinterProfile.cited). Only a cited limit can fail a check or refuse a
# plan; a setting past a default limit is a warning that tells the person to check their printer.
DEFAULT_HOTEND_MAX_C = 250   # hotends with a PTFE-lined heat break are commonly limited to about 240-250 C
DEFAULT_BED_MAX_C = 100
DEFAULT_MAX_FLOW = 12.0      # mm3/s: OrcaSlicer's Generic PLA cap for a standard direct drive hotend

CREALITY_K2_SPECS = "https://www.creality.com/products/k2-series-3d-printer"
ORCA_PROFILES = "https://github.com/OrcaSlicer/OrcaSlicer/tree/a31c95e273d3423abf185a444d70beae4941c697/resources/profiles"
# Vendor spec pages read for the limits below (fetched 2026-11-23). Each profile's comment names the value and
# the words on the page it came from.
BAMBU_X1_SPECS = "https://bambulab.com/en/x1"
BAMBU_P1_SPECS = "https://us.store.bambulab.com/products/p1s"     # the P1P and P1S comparison table
BAMBU_A1_SPECS = "https://bambulab.com/en/a1/tech-specs"
BAMBU_A1_MINI_SPECS = "https://bambulab.com/en/a1-mini/tech-specs"
CREALITY_K1_MAX_SPECS = "https://www.creality.com/products/creality-k1-max-3d-printer"
CREALITY_ENDER3_V3_SPECS = "https://www.creality.com/products/creality-ender-3-v3"
PRUSA_MK4S_SPECS = "https://www.prusa3d.com/product/original-prusa-mk4s-3d-printer-5/"
# Prusa's own printer profiles in PrusaSlicer, at a pinned release.
PRUSASLICER_PRUSA = "https://github.com/prusa3d/PrusaSlicer/blob/version_2.9.7/resources/profiles/PrusaResearch.ini"


@dataclass(frozen=True)
class PrinterProfile:
    key: str
    label: str
    bed: tuple[int, int]
    height: int
    flavor: str            # PrusaSlicer gcode_flavor
    nozzle: float = 0.4
    max_speed: int = 300   # mm/s: Gantrel's chosen cap for travel and infill, not a printer spec
    accel: int = 5000      # mm/s^2: Gantrel's chosen acceleration, not a printer spec
    direct_drive: bool = True
    enclosed: bool = False
    start_gcode: str = ""
    end_gcode: str = ""
    orca_machine: str = ""  # OrcaSlicer system machine profile name
    upload_ext: str = ".gcode"
    filament_mm: float = 1.75
    resin: bool = False    # resin printers: Gantrel never slices, it only uploads a pre-sliced .ctb or .goo
    note: str = ""
    # Hardware limits for the planner. max_speed and accel above are Gantrel's chosen caps; these are what
    # the printer itself can do. Unless a field is named in `cited`, its value is a default, not a spec.
    hotend_max_c: int = DEFAULT_HOTEND_MAX_C
    bed_max_c: int = DEFAULT_BED_MAX_C
    max_flow: float = DEFAULT_MAX_FLOW   # hotend volumetric limit, mm3/s
    high_flow: bool = False              # use the materials' high-flow hotend column
    hardened_nozzle: bool = False        # abrasive filaments (CF, GF) need one
    max_accel: int = 0                   # mm/s^2; 0 means the same as accel
    top_speed: int = 0                   # mm/s, the fastest the toolhead moves; 0 when no source gives it
    chamber_heater: bool = False
    cited: tuple[str, ...] = ()          # which of the limits above come from limits_source
    limits_source: str = ""
    # Set only on a profile built from a printer pack (packs.py): the pack's own material settings,
    # and where they came from ("pack <id> <first 12 hex of its sha256>").
    pack_materials: tuple[Material, ...] = ()
    source: str = ""

    def __post_init__(self):
        if not self.max_accel:
            object.__setattr__(self, "max_accel", self.accel)

    def is_cited(self, name: str) -> bool:
        return name in self.cited


KLIPPER_START = "START_PRINT EXTRUDER_TEMP={first_layer_temperature[0]} BED_TEMP={first_layer_bed_temperature[0]}"
CREALITY_K_START = ("START_PRINT EXTRUDER_TEMP={first_layer_temperature[0]} BED_TEMP={first_layer_bed_temperature[0]}"
                    " CHAMBER_TEMP={chamber_temperature[0]}")
MARLIN_START = ("M140 S{first_layer_bed_temperature[0]}\\nM104 S150\\nG28\\nM190 S{first_layer_bed_temperature[0]}\\n"
                "M109 S{first_layer_temperature[0]}\\nG92 E0\\nG1 Z0.3 F3000\\nG1 X5 Y20 F6000\\n"
                "G1 X5 Y120 E10 F1200\\nG92 E0")
MARLIN_END = "M104 S0\\nM140 S0\\nG91\\nG1 Z10 F600\\nG90\\nM107\\nM84"

PRINTERS: dict[str, PrinterProfile] = {p.key: p for p in (
    # K2 limits: Creality's K2 series comparison table (300 C nozzle, 100 C bed, no chamber heater, 20000 mm/s^2,
    # hardened steel nozzle) and Orca's Creality K2 machine profile (20000 mm/s^2, hardened_steel, machine_max_speed_x
    # 800 mm/s). Creality rates
    # the 40 mm3/s hotend with the K2 Plus's 100 W heater; the K2 has 70 W, so its flow is the highest flow
    # Creality's own K2 filament presets use (Hyper PLA @K2-all: 23 mm3/s), not a hotend rating.
    PrinterProfile("creality-k2", "Creality K2", (260, 260), 260, "klipper", max_speed=300, accel=10000,
                   enclosed=True, start_gcode=CREALITY_K_START, end_gcode="END_PRINT",
                   orca_machine="Creality K2 0.4 nozzle",
                   hotend_max_c=300, bed_max_c=100, max_flow=23, hardened_nozzle=True, max_accel=20000,
                   top_speed=800,
                   cited=("hotend_max_c", "bed_max_c", "max_flow", "hardened_nozzle", "max_accel", "chamber_heater",
                          "top_speed"),
                   limits_source=f"{CREALITY_K2_SPECS} ; {ORCA_PROFILES}/Creality/machine/Creality%20K2%200.4%20nozzle.json"
                                 f" ; {ORCA_PROFILES}/Creality/filament/Hyper%20PLA%20%40K2-all.json"),
    # K2 Plus limits: Creality's K2 Plus page and K2 series table (350 C nozzle, 120 C bed, actively heated chamber
    # to 60 C, 30000 mm/s^2, "40 mm3/s high-flow hotend" tested with ABS at 280 C) and Orca's K2 Plus machine
    # profile (30000 mm/s^2, hardened_steel, support_chamber_temp_control 1, machine_max_speed_x 800 mm/s).
    PrinterProfile("creality-k2-plus", "Creality K2 Plus", (350, 350), 350, "klipper", max_speed=300, accel=10000,
                   enclosed=True, start_gcode=CREALITY_K_START, end_gcode="END_PRINT",
                   orca_machine="Creality K2 Plus 0.4 nozzle",
                   hotend_max_c=350, bed_max_c=120, max_flow=40, high_flow=True, hardened_nozzle=True,
                   max_accel=30000, chamber_heater=True, top_speed=800,
                   cited=("hotend_max_c", "bed_max_c", "max_flow", "high_flow", "hardened_nozzle", "max_accel",
                          "chamber_heater", "top_speed"),
                   limits_source=f"https://www.creality.com/products/creality-k2-plus-cfs-combo ; {CREALITY_K2_SPECS}"
                                 f" ; {ORCA_PROFILES}/Creality/machine/Creality%20K2%20Plus%200.4%20nozzle.json"),
    # K1 and K1C limits: Orca's K1C machine profile (machine_max_acceleration_x 20000, machine_max_speed_x 600 mm/s).
    # The K1 ships with a brass nozzle and the K1C with a hardened steel one, so the nozzle stays Gantrel's default.
    PrinterProfile("creality-k1", "Creality K1 / K1C", (220, 220), 250, "klipper", max_speed=300, accel=8000,
                   enclosed=True, start_gcode=KLIPPER_START, end_gcode="END_PRINT",
                   orca_machine="Creality K1C 0.4 nozzle", max_accel=20000, top_speed=600,
                   cited=("max_accel", "top_speed"),
                   limits_source=f"{ORCA_PROFILES}/Creality/machine/Creality%20K1C%200.4%20nozzle.json"),
    # K1 Max limits: Creality's K1 Max page ("a hardened steel nozzle, working well in up to 300 C", "The travel
    # speed is 800mm/s", 20000 mm/s^2) and Orca's K1 Max machine profile (machine_max_acceleration_x 20000,
    # machine_max_speed_x 800). Orca's nozzle_type there says brass; the vendor's
    # page wins. The page gives no bed maximum, so bed_max_c stays Gantrel's default.
    PrinterProfile("creality-k1-max", "Creality K1 Max", (300, 300), 300, "klipper", max_speed=300, accel=8000,
                   enclosed=True, start_gcode=KLIPPER_START, end_gcode="END_PRINT",
                   orca_machine="Creality K1 Max (0.4 nozzle)", hotend_max_c=300, hardened_nozzle=True,
                   max_accel=20000, top_speed=800,
                   cited=("hotend_max_c", "hardened_nozzle", "max_accel", "top_speed"),
                   limits_source=f"{CREALITY_K1_MAX_SPECS} ; "
                                 f"{ORCA_PROFILES}/Creality/machine/Creality%20K1%20Max%20(0.4%20nozzle).json"),
    # Ender-3 V3 limits: Creality's Ender-3 V3 page ("Nozzle Temperature <=300 C", "Heatbed Temperature <=110 C",
    # "Quick-swap nozzle (hardened steel tip)", PLA-CF and PETG-CF supported, "600mm/s Max printing speed") and
    # Orca's machine profile (nozzle_type hardened_steel, machine_max_acceleration_x 20000, machine_max_speed_x 600).
    PrinterProfile("creality-ender3-v3", "Creality Ender-3 V3", (220, 220), 250, "klipper", max_speed=250,
                   accel=6000, start_gcode=KLIPPER_START, end_gcode="END_PRINT",
                   orca_machine="Creality Ender-3 V3 0.4 nozzle", hotend_max_c=300, bed_max_c=110,
                   hardened_nozzle=True, max_accel=20000, top_speed=600,
                   cited=("hotend_max_c", "bed_max_c", "hardened_nozzle", "max_accel", "top_speed"),
                   limits_source=f"{CREALITY_ENDER3_V3_SPECS} ; "
                                 f"{ORCA_PROFILES}/Creality/machine/Creality%20Ender-3%20V3%200.4%20nozzle.json"),
    # X1 Carbon limits: Bambu Lab's X1 page ("All-Metal Hotend 300 C", "Hardened Steel Nozzle 50 HRC",
    # its spec table lists the X1C nozzle as hardened steel and PA, PC and carbon or glass fibre
    # filled filament as Ideal) and Orca's X1C machine profile (machine_max_acceleration_x 20000,
    # machine_max_speed_x 500). Orca's Bambu machine profiles set no nozzle_type. Bed: Bambu's X1-Carbon tech
    # specs PDF (public-cdn.bambulab.com/store/X1-Carbon%20tech%20specs.pdf) gives "Max Build Plate Temperature
    # 110 @220 V, 120 @110 V", so 110 C, which holds on either supply. (The X1 page's "Hotbed 120 C" is the 110 V figure.)
    PrinterProfile("bambu-x1c", "Bambu Lab X1 Carbon", (256, 256), 256, "marlin2", max_speed=300, accel=10000,
                   enclosed=True, orca_machine="Bambu Lab X1 Carbon 0.4 nozzle", hotend_max_c=300, bed_max_c=110,
                   hardened_nozzle=True, max_accel=20000, top_speed=500,
                   cited=("hotend_max_c", "bed_max_c", "hardened_nozzle", "max_accel", "top_speed"),
                   limits_source=f"{BAMBU_X1_SPECS} ; "
                                 f"{ORCA_PROFILES}/BBL/machine/Bambu%20Lab%20X1%20Carbon%200.4%20nozzle.json"),
    # P1S and P1P limits: the P1P and P1S comparison table on Bambu Lab's P1S store page ("Nozzle 0.4 mm Stainless
    # Steel", "Max Hot End Temperature 300 C", "Max Build Plate Temperature 100 C", "Max Speed of Tool Head
    # 500 mm/s"); stainless steel is not a hardened nozzle, and Bambu's P1 page says not to print fibre filled
    # filament before a hotend upgrade. Orca's machine profiles: machine_max_acceleration_x 20000,
    # machine_max_speed_x 500.
    PrinterProfile("bambu-p1s", "Bambu Lab P1S", (256, 256), 256, "marlin2", max_speed=300, accel=10000,
                   enclosed=True, orca_machine="Bambu Lab P1S 0.4 nozzle", hotend_max_c=300, bed_max_c=100,
                   max_accel=20000, top_speed=500,
                   cited=("hotend_max_c", "bed_max_c", "hardened_nozzle", "max_accel", "top_speed"),
                   limits_source=f"{BAMBU_P1_SPECS} ; "
                                 f"{ORCA_PROFILES}/BBL/machine/Bambu%20Lab%20P1S%200.4%20nozzle.json"),
    PrinterProfile("bambu-p1p", "Bambu Lab P1P", (256, 256), 256, "marlin2", max_speed=300, accel=10000,
                   orca_machine="Bambu Lab P1P 0.4 nozzle", hotend_max_c=300, bed_max_c=100, max_accel=20000,
                   top_speed=500, cited=("hotend_max_c", "bed_max_c", "hardened_nozzle", "max_accel", "top_speed"),
                   limits_source=f"{BAMBU_P1_SPECS} ; "
                                 f"{ORCA_PROFILES}/BBL/machine/Bambu%20Lab%20P1P%200.4%20nozzle.json"),
    # A1 limits: Bambu Lab's A1 tech specs ("Nozzle: Stainless Steel", "Max Hot End Temperature: 300 C",
    # "Max Build Plate Temperature: 100 C", "Max Speed of Toolhead: 500 mm/s"; carbon or glass fibre filled
    # filament "Not Recommended") and Orca's A1 machine profile (machine_max_acceleration_x 12000; the spec page
    # says 10000 mm/s^2).
    PrinterProfile("bambu-a1", "Bambu Lab A1", (256, 256), 256, "marlin2", max_speed=250, accel=10000,
                   orca_machine="Bambu Lab A1 0.4 nozzle", hotend_max_c=300, bed_max_c=100, max_accel=12000,
                   top_speed=500, cited=("hotend_max_c", "bed_max_c", "hardened_nozzle", "max_accel", "top_speed"),
                   limits_source=f"{BAMBU_A1_SPECS} ; "
                                 f"{ORCA_PROFILES}/BBL/machine/Bambu%20Lab%20A1%200.4%20nozzle.json"),
    # A1 mini limits: Bambu Lab's A1 mini tech specs ("Nozzle: Stainless Steel", "Max Hot End Temperature: 300 C",
    # "Max Build Plate Temperature: 80 C", "Max Speed of Toolhead: 500 mm/s", "Max Acceleration of Toolhead:
    # 10000 mm/s^2"; fibre filled filament "Not Recommended").
    PrinterProfile("bambu-a1-mini", "Bambu Lab A1 mini", (180, 180), 180, "marlin2", max_speed=250, accel=10000,
                   orca_machine="Bambu Lab A1 mini 0.4 nozzle", hotend_max_c=300, bed_max_c=80, max_accel=10000,
                   top_speed=500, cited=("hotend_max_c", "bed_max_c", "hardened_nozzle", "max_accel", "top_speed"),
                   limits_source=BAMBU_A1_MINI_SPECS),
    # MK4 and MK4S limits: Prusa's MK4S page ("Max nozzle temperature 290 C", "Max heatbed temperature 120 C",
    # "High-flow Prusa Nozzle brass CHT"; its comparison table gives 290 C / 120 C and a brass nozzle for every
    # column), Orca's MK4 machine profile (machine_max_acceleration_x 4000) and Prusa's own PrusaSlicer profiles
    # for the MK4 Input Shaper and MK4S (machine_max_feedrate_x 300 mm/s). Orca's nozzle_type there says
    # hardened_steel; the vendor's page wins.
    PrinterProfile("prusa-mk4", "Original Prusa MK4/MK4S", (250, 210), 220, "marlin2", max_speed=200, accel=4000,
                   orca_machine="Prusa MK4 0.4 nozzle", start_gcode=MARLIN_START, end_gcode=MARLIN_END,
                   upload_ext=".bgcode", hotend_max_c=290, bed_max_c=120, max_accel=4000, top_speed=300,
                   cited=("hotend_max_c", "bed_max_c", "hardened_nozzle", "max_accel", "top_speed"),
                   limits_source=f"{PRUSA_MK4S_SPECS} ; {ORCA_PROFILES}/Prusa/machine/Prusa%20MK4%200.4%20nozzle.json"
                                 f" ; {PRUSASLICER_PRUSA}"),
    # MK3S+ limits: Prusa's own PrusaSlicer profile "Original Prusa i3 MK3S & MK3S+" (machine_max_feedrate_x
    # 200 mm/s, machine_max_acceleration_extruding 1250) and Orca's MK3S machine profile (machine_max_speed_x 200).
    PrinterProfile("prusa-mk3", "Original Prusa MK3S+", (250, 210), 210, "marlin2", max_speed=150, accel=1250,
                   direct_drive=True, orca_machine="Prusa MK3S 0.4 nozzle", start_gcode=MARLIN_START,
                   end_gcode=MARLIN_END, max_accel=1250, top_speed=200, cited=("max_accel", "top_speed"),
                   limits_source=f"{PRUSASLICER_PRUSA} ; {ORCA_PROFILES}/Prusa/machine/Prusa%20MK3S%200.4%20nozzle.json"),
    PrinterProfile("generic-klipper", "Generic Klipper printer", (220, 220), 250, "klipper", max_speed=200,
                   accel=3000, start_gcode=KLIPPER_START, end_gcode="END_PRINT"),
    PrinterProfile("generic-marlin", "Generic Marlin printer", (220, 220), 250, "marlin2", max_speed=100,
                   accel=1000, direct_drive=False, start_gcode=MARLIN_START, end_gcode=MARLIN_END),
    # The common 220 mm bed slinger class (Ender-3 style): 220 x 220 x 250 mm, Bowden.
    # https://www.creality.com/products/ender-3-v2-3d-printer
    PrinterProfile("generic-marlin-220", "Generic Marlin printer, 220 mm bed", (220, 220), 250, "marlin2",
                   max_speed=100, accel=1000, direct_drive=False, start_gcode=MARLIN_START, end_gcode=MARLIN_END),
    # 256 x 256 x 256 mm, enclosed CoreXY, direct drive. https://us.elegoo.com/products/centauri-carbon
    PrinterProfile("elegoo-centauri-carbon", "Elegoo Centauri Carbon", (256, 256), 256, "marlin2", max_speed=300,
                   accel=10000, enclosed=True, start_gcode=MARLIN_START, end_gcode=MARLIN_END),
    # Resin (MSLA). 218.88 x 122.88 x 220 mm. https://global.elegoo.com/pages/elegoo-saturn-4-ultra
    PrinterProfile("elegoo-saturn-4-ultra", "Elegoo Saturn 4 Ultra (resin)", (218, 122), 220, "", direct_drive=False,
                   upload_ext=".goo", resin=True),
    # Resin (MSLA). 153.36 x 77.76 x 165 mm. https://www.elegoo.com/pages/elegoo-mars-5-series
    PrinterProfile("elegoo-mars-5-ultra", "Elegoo Mars 5 Ultra (resin)", (153, 77), 165, "", direct_drive=False,
                   upload_ext=".goo", resin=True),
    # 220 x 220 x 220 mm, open frame CoreXY, direct drive. https://www.flashforge.com/products/adventurer-5m-3d-printer
    PrinterProfile("flashforge-adventurer-5m", "FlashForge Adventurer 5M", (220, 220), 220, "marlin2", max_speed=300,
                   accel=10000, start_gcode=MARLIN_START, end_gcode=MARLIN_END),
    # 220 x 220 x 220 mm, fully enclosed, direct drive. https://www.flashforge.com/products/adventurer-5m-pro-3d-printer
    PrinterProfile("flashforge-adventurer-5m-pro", "FlashForge Adventurer 5M Pro", (220, 220), 220, "marlin2",
                   max_speed=300, accel=10000, enclosed=True, start_gcode=MARLIN_START, end_gcode=MARLIN_END),
    # 220 x 220 x 220 mm, open frame CoreXY, direct drive, four color IFS. https://www.flashforge.com/products/flashforge-ad5x
    PrinterProfile("flashforge-ad5x", "FlashForge AD5X", (220, 220), 220, "marlin2", max_speed=300, accel=10000,
                   start_gcode=MARLIN_START, end_gcode=MARLIN_END),
    # 300 x 200 x 200 mm, enclosed IDEX, direct drive. https://www.snapmaker.com/snapmaker-j1s
    PrinterProfile("snapmaker-j1", "Snapmaker J1 / J1s", (300, 200), 200, "marlin2", max_speed=250, accel=8000,
                   enclosed=True, start_gcode=MARLIN_START, end_gcode=MARLIN_END,
                   note="Profile is for the left head only; IDEX duplication and mirror modes are set on the printer."),
    # 320 x 350 x 330 mm, open frame (enclosure sold separately), direct drive.
    # https://www.snapmaker.com/snapmaker-2-0
    PrinterProfile("snapmaker-a350", "Snapmaker 2.0 A350", (320, 350), 330, "marlin2", max_speed=100, accel=1000,
                   start_gcode=MARLIN_START, end_gcode=MARLIN_END),
    # 330 x 240 x 300 mm, Bowden, 2.85 mm filament, front doors and an open top.
    # https://ultimaker.com/3d-printers/s-series/ultimaker-s5/
    PrinterProfile("ultimaker-s5", "UltiMaker S5", (330, 240), 300, "marlin2", max_speed=150, accel=2000,
                   direct_drive=False, filament_mm=2.85, start_gcode=MARLIN_START, end_gcode=MARLIN_END,
                   note="UltiMaker printers expect Cura's Griffin header and may warn about G-code from other "
                        "slicers; confirm the material on the printer's screen."),
)}



@dataclass(frozen=True)
class Material:
    name: str
    nozzle: int
    nozzle_first: int
    bed: int
    bed_first: int
    fan_min: int
    fan_max: int
    max_speed: int        # mm/s cap for outer walls and the material's flow limit
    chamber: int = 0
    retract_mm: float = 0.8
    note: str = ""
    # The nozzle temperatures worth trying for this material, low to high (calibrate temp-tower).
    # Always inside the family's cited range in materials.json (tests/plan/test_materials.py checks it).
    nozzle_range: tuple[int, int] = (190, 230)
    # Set only for a material from a printer pack (packs.py); Gantrel's own defaults leave them empty.
    label: str = ""                       # brand and name, like "Generic PLA"
    max_flow: float = 0.0                 # mm3/s this filament ran at on this printer
    retract_speed: float = 0.0            # mm/s
    pressure_advance: float | None = None
    flow_ratio: float | None = None

    @property
    def family(self) -> materials.Filament:
        """The cited property table entry: the one source of truth for facts both modules hold."""
        return materials.get(self.name)

    @property
    def needs_enclosure(self) -> bool:
        return self.family.needs_enclosure

    @property
    def flexible(self) -> bool:
        return self.family.flexible


MATERIALS: dict[str, Material] = {m.name: m for m in (
    Material("PLA", 215, 220, 60, 65, 100, 100, 300, note="Easiest. Keep the lid or door open on enclosed printers."),
    # PETG and ABS temp-tower ranges started at 220 and 235 C, below the cited family minimum of 240 C
    # (materials.json); they now start at 240 C.
    Material("PETG", 240, 245, 75, 80, 30, 60, 200, retract_mm=1.0, nozzle_range=(240, 255),
             note="Strings a little. Use a textured or PEI bed; glue on smooth PEI so it doesn't bond too hard."),
    Material("ABS", 255, 260, 100, 100, 0, 20, 200, chamber=45, nozzle_range=(240, 265),
             note="Needs an enclosed printer and good ventilation; fumes."),
    Material("ASA", 260, 265, 100, 100, 0, 30, 200, chamber=45, nozzle_range=(240, 270),
             note="UV resistant ABS. Needs an enclosure and ventilation."),
    Material("TPU", 225, 225, 45, 45, 60, 100, 30, retract_mm=0.4, nozzle_range=(210, 235),
             note="Flexible. Print slowly; direct drive printers only."),
)}

QUALITY = {
    "draft": {"layer_height": 0.28, "perimeters": 2, "infill": 10},
    "standard": {"layer_height": 0.20, "perimeters": 3, "infill": 15},
    "fine": {"layer_height": 0.12, "perimeters": 3, "infill": 15},
    "strong": {"layer_height": 0.20, "perimeters": 5, "infill": 40},
}


def profile_for(model: str, type_name: str) -> PrinterProfile:
    """The printer's profile. Never guessed from the adapter type: bed size and enclosure differ by model."""
    key = model
    if not key:
        raise ConfigError("this printer has no model set, so its bed size and enclosure are unknown",
                          f"Unpair and pair it again with --model, one of: {', '.join(PRINTERS)}")
    if key not in PRINTERS:
        raise ConfigError(f"no slicing profile for model {key!r}",
                          f"Pair the printer with --model set to one of: {', '.join(PRINTERS)}")
    return PRINTERS[key]


def pack_material(printer: PrinterProfile, family: str) -> Material | None:
    """The pack's own settings for this filament family, when the profile came from a pack."""
    return next((m for m in printer.pack_materials if m.name == family), None)


def pack_materials_family(printer: PrinterProfile) -> str | None:
    """The filament family a pack profile was resolved for (its first material), or None for other profiles."""
    return printer.pack_materials[0].name if printer.pack_materials else None


def material(name: str) -> Material:
    m = MATERIALS.get(name.upper())
    if not m:
        raise ConfigError(f"no defaults for material {name!r}", f"Use one of: {', '.join(MATERIALS)}")
    return m


def settings(printer: PrinterProfile, mat: Material, quality: str = "standard", force: bool = False) -> dict:
    if printer.resin:
        raise Unsupported(f"Gantrel does not slice for resin printers like the {printer.label}",
                          "Slice it in CHITUBOX or VOXELDANCE TANGO, then upload the .goo or .ctb file")
    if quality not in QUALITY:
        raise ConfigError(f"unknown quality {quality!r}", f"Use one of: {', '.join(QUALITY)}")
    warnings = []
    if mat.needs_enclosure and not printer.enclosed:
        warnings.append(f"{mat.name} needs an enclosed printer and {printer.label} is open: expect warping, "
                        "cracked layers and fumes in the room")
    if mat.flexible and not printer.direct_drive:
        warnings.append(f"{mat.name} is flexible and needs a direct drive extruder, and {printer.label} is Bowden: "
                        "it will likely jam")
    if warnings and not force:
        raise ConfigError(warnings[0], "Use PLA or PETG on this printer. If the person has an enclosure or "
                                       "accepts the risk, they can ask for --force")
    q = QUALITY[quality]
    speed = min(printer.max_speed, mat.max_speed)
    from .plan import estimate   # plan imports this module, so not at the top
    # The volumetric flow cap the planner uses for this filament on this printer: a pack's own max_flow when the
    # profile came from a pack, else the vendor preset or Gantrel's default. Both slicers are held to it.
    flow = round(estimate.flow_cap(printer, mat.family), 2)
    return {
        "printer": printer.label, "material": mat.name, "quality": quality, "nozzle_mm": printer.nozzle,
        "layer_height_mm": q["layer_height"], "first_layer_height_mm": 0.2 if q["layer_height"] <= 0.2 else 0.25,
        "perimeters": q["perimeters"], "top_layers": max(4, round(0.8 / q["layer_height"])),
        "bottom_layers": max(3, round(0.6 / q["layer_height"])), "infill_percent": q["infill"],
        "infill_pattern": "gyroid",
        "nozzle_c": mat.nozzle, "nozzle_first_layer_c": mat.nozzle_first,
        "bed_c": mat.bed, "bed_first_layer_c": mat.bed_first, "chamber_c": mat.chamber,
        "fan_percent": [mat.fan_min, mat.fan_max],
        "outer_wall_speed_mms": min(speed, 150) if mat.name != "TPU" else speed,
        "inner_wall_speed_mms": min(speed, 250), "infill_speed_mms": speed, "first_layer_speed_mms": min(speed, 30),
        "travel_speed_mms": printer.max_speed, "accel_mms2": printer.accel, "max_flow_mm3s": flow,
        "retract_mm": mat.retract_mm,
        "supports": "auto, for overhangs flatter than 50 degrees from horizontal", "brim": "auto for small footprints",
        "bed_mm": list(printer.bed), "max_height_mm": printer.height,
        "note": " ".join(n for n in (mat.note, printer.note) if n), "filament_mm": printer.filament_mm,
        **({"warning": "; ".join(warnings)} if warnings else {}),
        **pack_settings(printer, mat),
    }


def pack_settings(printer: PrinterProfile, mat: Material) -> dict:
    """The extra numbers a pack material carries, and the pack named as their source. Empty otherwise."""
    s: dict = {}
    if printer.source:
        s["source"] = printer.source
    if mat.label:
        s["material_label"] = mat.label
    if mat.retract_speed:
        s["retract_speed_mms"] = mat.retract_speed
    if mat.flow_ratio is not None:
        s["flow_ratio"] = mat.flow_ratio
    if mat.pressure_advance is not None:
        # PrusaSlicer has no pressure advance setting; OrcaSlicer gets it below. On Klipper it can also be set
        # in printer.cfg ([extruder] pressure_advance), which the person does on the printer.
        s["pressure_advance"] = mat.pressure_advance
    return s


def prusa_args(s: dict, printer: PrinterProfile) -> list[str]:
    w, d = printer.bed
    a = [
        "--gcode-flavor", printer.flavor, "--nozzle-diameter", str(s["nozzle_mm"]),
        "--bed-shape", f"0x0,{w}x0,{w}x{d},0x{d}", "--max-print-height", str(printer.height),
        "--layer-height", str(s["layer_height_mm"]), "--first-layer-height", str(s["first_layer_height_mm"]),
        "--perimeters", str(s["perimeters"]), "--top-solid-layers", str(s["top_layers"]),
        "--bottom-solid-layers", str(s["bottom_layers"]), "--fill-density", f"{s['infill_percent']}%",
        "--fill-pattern", s["infill_pattern"],
        "--temperature", str(s["nozzle_c"]), "--first-layer-temperature", str(s["nozzle_first_layer_c"]),
        "--bed-temperature", str(s["bed_c"]), "--first-layer-bed-temperature", str(s["bed_first_layer_c"]),
        "--min-fan-speed", str(s["fan_percent"][0]), "--max-fan-speed", str(s["fan_percent"][1]),
        "--external-perimeter-speed", str(s["outer_wall_speed_mms"]),
        "--perimeter-speed", str(s["inner_wall_speed_mms"]), "--infill-speed", str(s["infill_speed_mms"]),
        "--solid-infill-speed", str(s["infill_speed_mms"]), "--first-layer-speed", str(s["first_layer_speed_mms"]),
        "--travel-speed", str(s["travel_speed_mms"]), "--max-volumetric-speed", str(s["max_flow_mm3s"]),
        "--retract-length", str(s["retract_mm"]),
        "--filament-type", s["material"], "--support-material", "--support-material-auto",
        "--support-material-threshold", "50", "--brim-width", "0", "--skirts", "1",
        "--center", f"{w // 2},{d // 2}", "--ensure-on-bed",
    ]
    if printer.filament_mm != 1.75:
        a += ["--filament-diameter", str(printer.filament_mm)]
    if "retract_speed_mms" in s:
        a += ["--retract-speed", str(s["retract_speed_mms"])]
    if "flow_ratio" in s:
        a += ["--extrusion-multiplier", str(s["flow_ratio"])]
    if printer.flavor == "klipper":
        # Plain G1 E retracts, never G10/G11 firmware retraction: the retract_mm printed above is then
        # what the printer does, and a printer.cfg without [firmware_retraction] still retracts.
        a += ["--use-relative-e-distances", "--max-print-speed",
              str(s["infill_speed_mms"]), "--machine-max-acceleration-extruding", str(s["accel_mms2"])]
    # A pack profile always passes both, an empty block too: left out, PrusaSlicer writes its own default start
    # G-code, which no one reviewed.
    if printer.start_gcode or printer.source:
        a += ["--start-gcode", printer.start_gcode.replace("{chamber_temperature[0]}", str(s["chamber_c"]))]
    if printer.end_gcode or printer.source:
        a += ["--end-gcode", printer.end_gcode]
    return a


def orca_overrides(s: dict, printer: PrinterProfile) -> tuple[dict, dict, dict]:
    machine = {"type": "machine", "name": "gantrel-machine", "inherits": printer.orca_machine, "from": "User",
               # Slicer retracts (G1 E), never G10/G11, at the retract_mm Gantrel prints.
               "use_firmware_retraction": "0", "retraction_length": [str(s["retract_mm"])]}
    process = {"type": "process", "name": "gantrel-process", "from": "User",
               "inherits": f"{s['layer_height_mm']:.2f}mm Standard @{printer.orca_machine.split(' 0.4')[0]}",
               "layer_height": str(s["layer_height_mm"]), "initial_layer_print_height": str(s["first_layer_height_mm"]),
               "wall_loops": str(s["perimeters"]), "top_shell_layers": str(s["top_layers"]),
               "bottom_shell_layers": str(s["bottom_layers"]), "sparse_infill_density": f"{s['infill_percent']}%",
               "sparse_infill_pattern": "gyroid", "enable_support": "1", "support_type": "tree(auto)",
               "support_threshold_angle": "50", "outer_wall_speed": str(s["outer_wall_speed_mms"]),
               "inner_wall_speed": str(s["inner_wall_speed_mms"]), "sparse_infill_speed": str(s["infill_speed_mms"]),
               "initial_layer_speed": str(s["first_layer_speed_mms"])}
    filament = {"type": "filament", "name": f"gantrel-{s['material']}", "from": "User",
                "inherits": f"Generic {s['material']}", "filament_type": [s["material"]],
                "nozzle_temperature": [str(s["nozzle_c"])], "nozzle_temperature_initial_layer": [str(s["nozzle_first_layer_c"])],
                "hot_plate_temp": [str(s["bed_c"])], "hot_plate_temp_initial_layer": [str(s["bed_first_layer_c"])],
                "textured_plate_temp": [str(s["bed_c"])], "textured_plate_temp_initial_layer": [str(s["bed_first_layer_c"])],
                "fan_min_speed": [str(s["fan_percent"][0])], "fan_max_speed": [str(s["fan_percent"][1])],
                "filament_max_volumetric_speed": [str(s["max_flow_mm3s"])]}
    if printer.source:
        # A pack profile: Orca writes the pack's reviewed start and end G-code, not the bundled machine's.
        # Orca's machine profiles hold real line breaks; the profile carries \n escapes (packs.to_profile), and a
        # pack's G-code may hold no backslash of its own, so turning them back gives exactly the reviewed text.
        # Both are always set, an empty block too: left out, Orca would run the inherited machine's G-code, which
        # no one reviewed.
        for key, text in (("machine_start_gcode", printer.start_gcode), ("machine_end_gcode", printer.end_gcode)):
            machine[key] = text.replace("\\n", "\n")
        # The pack's own printer, not the inherited machine's: a pack may keep a K2's orca_machine for a smaller
        # bed, another nozzle or another firmware. Key names and forms as Orca's machine profiles hold them.
        w, d = printer.bed
        machine.update(printable_area=["0x0", f"{w}x0", f"{w}x{d}", f"0x{d}"], printable_height=str(printer.height),
                       nozzle_diameter=[str(printer.nozzle)], gcode_flavor=printer.flavor)
        filament["filament_diameter"] = [str(printer.filament_mm)]
    if "retract_speed_mms" in s:
        machine["retraction_speed"] = [str(s["retract_speed_mms"])]
    if "flow_ratio" in s:
        filament["filament_flow_ratio"] = [str(s["flow_ratio"])]
    if "pressure_advance" in s:
        filament["enable_pressure_advance"] = ["1"]
        filament["pressure_advance"] = [str(s["pressure_advance"])]
    return machine, process, filament


# ---------------------------------------------------------------- slicer programs

SLICERS = {
    "prusaslicer": {"bins": ("prusa-slicer", "PrusaSlicer", "prusaslicer"), "flatpak": "com.prusa3d.PrusaSlicer",
                    "appimage": "PrusaSlicer*.AppImage"},
    "orcaslicer": {"bins": ("orca-slicer", "OrcaSlicer", "orcaslicer"), "flatpak": "io.github.softfever.OrcaSlicer",
                   "appimage": "OrcaSlicer*.AppImage"},
}
MAC_APPS = {"prusaslicer": "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer",
            "orcaslicer": "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"}


def find_slicers() -> dict[str, list[str]]:
    """Installed slicers and the argv prefix to run each. Looks only; never installs."""
    found: dict[str, list[str]] = {}
    for kind, spec in SLICERS.items():
        override = os.environ.get(f"GANTREL_{kind.upper()}")
        if override:
            found[kind] = [override]
            continue
        for b in spec["bins"]:
            p = shutil.which(b)
            if p:
                found[kind] = [p]
                break
        if kind in found:
            continue
        if Path(MAC_APPS[kind]).exists():
            found[kind] = [MAC_APPS[kind]]
            continue
        flatpak = shutil.which("flatpak")
        if flatpak and subprocess.run([flatpak, "info", spec["flatpak"]], capture_output=True).returncode == 0:
            found[kind] = [flatpak, "run", "--filesystem=host", spec["flatpak"]]
            continue
        for d in (Path.home() / "Applications", Path.home() / ".local" / "bin", Path.home() / "Downloads"):
            hits = sorted(d.glob(spec["appimage"])) if d.is_dir() else []
            if hits and os.access(hits[-1], os.X_OK):
                found[kind] = [str(hits[-1])]
                break
    return found


MISSING_HELP = ("Gantrel uses PrusaSlicer or OrcaSlicer from the command line (free, AGPL-3.0, run "
                "as separate programs). Ask the person to install one: PrusaSlicer from https://www.prusa3d.com/"
                "prusaslicer/ or their package manager (flatpak install flathub com.prusa3d.PrusaSlicer), or "
                "OrcaSlicer from https://github.com/SoftFever/OrcaSlicer/releases. Then run slice again")


class SliceTimeout(PrinterError):
    """The slicer ran past its time limit and was stopped."""

    code = "timeout"


def run_slicer(argv: list[str], timeout: float, env: dict | None = None) -> tuple[int, str, str]:
    """Run a slicer in its own process group. On a timeout, kill the whole group (a flatpak or AppImage
    slicer runs as a child of what we start) and raise SliceTimeout. Returns (exit code, stdout, stderr)."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            text=True, start_new_session=True, env=env)
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException:
        # Killed before the leader is reaped, so its pid still names this group and no other.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        for pipe in (proc.stdout, proc.stderr):
            pipe.close()  # never wait on a child that left the group and still holds the pipe
        raise
    return proc.returncode, out, err


def slice_model(model_path: Path, out_dir: Path, printer: PrinterProfile, s: dict,
                prefer: str | None = None, timeout: float = 900) -> tuple[Path, str, list[str]]:
    """Run the slicer. Return (output file, slicer used, argv)."""
    found = find_slicers()
    if not found:
        raise Unsupported("no slicer is installed", MISSING_HELP)
    order = [prefer] if prefer else []
    # PrusaSlicer first: every setting is passed explicitly, so the result does not depend on bundled profiles.
    order += ["prusaslicer", "orcaslicer"]
    kind = next((k for k in order if k in found), None)
    if kind is None:
        raise Unsupported(f"{prefer} is not installed", f"Installed: {', '.join(found)}")
    out_dir.mkdir(parents=True, exist_ok=True)
    # Every part of the name gets the same cleaning, so it is a safe file and printer name (a pack's key is
    # pack:<id>, and upload refuses a colon).
    stem, key, mat = (re.sub(r"[^A-Za-z0-9_.-]+", "_", t) for t in (model_path.stem, printer.key, s["material"]))
    ext = ".bgcode" if kind == "prusaslicer" and printer.upload_ext == ".bgcode" else ".gcode"
    final = out_dir / f"{stem}_{key}_{mat}{ext}"
    if kind == "orcaslicer" and not printer.orca_machine:
        raise Unsupported(f"OrcaSlicer has no system profile mapped for {printer.label}",
                          "Install PrusaSlicer, or pair with a model that has an Orca profile")
    # Each run writes into its own new, empty folder, and only a file this run made is accepted.
    # An old G-code in out_dir can never be reported as this slice.
    with tempfile.TemporaryDirectory(prefix="gantrel-slice-") as tmp:
        work = Path(tmp)
        if kind == "prusaslicer":
            out = work / final.name
            argv = found[kind] + ["--export-gcode"] + (["--binary-gcode"] if ext == ".bgcode" else []) \
                + prusa_args(s, printer) + ["--output", str(out), str(model_path)]
        else:
            machine, process, filament = orca_overrides(s, printer)
            cfg_dir = work / "settings"
            cfg_dir.mkdir()
            files = []
            for name, data in (("machine", machine), ("process", process), ("filament", filament)):
                f = cfg_dir / f"gantrel-{name}.json"
                f.write_text(json.dumps(data, indent=2))
                files.append(str(f))
            outdir = work / "out"
            outdir.mkdir()
            argv = found[kind] + ["--slice", "0", "--load-settings", f"{files[0]};{files[1]}", "--load-filaments",
                                  files[2], "--outputdir", str(outdir), str(model_path)]
        try:
            code, stdout, stderr = run_slicer(argv, timeout)
        except subprocess.TimeoutExpired:
            raise SliceTimeout(f"{kind} took longer than {timeout:g}s and was stopped",
                               "Try a smaller model or the draft quality") from None
        if code != 0:
            tail = (stderr or stdout).strip().splitlines()[-5:]
            raise PrinterError(f"{kind} failed (exit {code})", " | ".join(tail)[:400])
        if kind == "orcaslicer":
            made = sorted(outdir.glob("*.gcode"))
            if len(made) != 1:
                raise PrinterError(f"{kind} made {len(made)} G-code files, expected exactly 1",
                                   "Check the model is a valid STL or 3MF with one plate")
            out = made[0]
        if not out.is_file() or out.stat().st_size == 0:
            raise PrinterError(f"{kind} finished but wrote no output file", "Check the model is a valid STL or 3MF")
        shutil.move(str(out), final)
    return final, kind, argv


# ---------------------------------------------------------------- reading gcode

_TIME_RE = re.compile(r"^;\s*(?:estimated printing time \(normal mode\)|total estimated time|estimated printing time)"
                      r"\s*[=:]\s*(.+)$", re.I | re.M)
_FIL_RE = re.compile(r"^;\s*filament_type\s*=\s*([A-Za-z0-9+-]+)", re.M)


def parse_duration(text: str) -> float | None:
    total, found = 0.0, False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", text):
        total += float(num) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
        found = True
    return total if found else None


def gcode_facts(path: Path) -> dict:
    """Estimated time and material from the slicer's comments. Reads only the head and tail."""
    p = Path(path)
    if p.suffix.lower() not in (".gcode", ".gco", ".g"):
        return {}
    size = p.stat().st_size
    with open(p, "rb") as fh:
        head = fh.read(256 * 1024)
        fh.seek(max(0, size - 256 * 1024))
        tail = fh.read()
    text = (head + b"\n" + tail).decode("utf-8", "replace")
    facts: dict = {}
    m = _TIME_RE.search(text)
    if m:
        facts["est_seconds"] = parse_duration(m.group(1))
    m = _FIL_RE.search(text)
    if m:
        facts["material"] = m.group(1).split(";")[0]
    return facts
