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
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError, PrinterError, Unsupported


@dataclass(frozen=True)
class PrinterProfile:
    key: str
    label: str
    bed: tuple[int, int]
    height: int
    flavor: str            # PrusaSlicer gcode_flavor
    nozzle: float = 0.4
    max_speed: int = 300   # mm/s cap for travel and infill
    accel: int = 5000      # mm/s^2
    direct_drive: bool = True
    enclosed: bool = False
    start_gcode: str = ""
    end_gcode: str = ""
    orca_machine: str = ""  # OrcaSlicer system machine profile name
    upload_ext: str = ".gcode"
    filament_mm: float = 1.75
    resin: bool = False    # resin printers: Gantrel never slices, it only uploads a pre-sliced .ctb or .goo
    note: str = ""


KLIPPER_START = "START_PRINT EXTRUDER_TEMP={first_layer_temperature[0]} BED_TEMP={first_layer_bed_temperature[0]}"
CREALITY_K_START = ("START_PRINT EXTRUDER_TEMP={first_layer_temperature[0]} BED_TEMP={first_layer_bed_temperature[0]}"
                    " CHAMBER_TEMP={chamber_temperature[0]}")
MARLIN_START = ("M140 S{first_layer_bed_temperature[0]}\\nM104 S150\\nG28\\nM190 S{first_layer_bed_temperature[0]}\\n"
                "M109 S{first_layer_temperature[0]}\\nG92 E0\\nG1 Z0.3 F3000\\nG1 X5 Y20 F6000\\n"
                "G1 X5 Y120 E10 F1200\\nG92 E0")
MARLIN_END = "M104 S0\\nM140 S0\\nG91\\nG1 Z10 F600\\nG90\\nM107\\nM84"

PRINTERS: dict[str, PrinterProfile] = {p.key: p for p in (
    PrinterProfile("creality-k2", "Creality K2", (260, 260), 260, "klipper", max_speed=300, accel=10000,
                   enclosed=True, start_gcode=CREALITY_K_START, end_gcode="END_PRINT",
                   orca_machine="Creality K2 0.4 nozzle"),
    PrinterProfile("creality-k2-plus", "Creality K2 Plus", (350, 350), 350, "klipper", max_speed=300, accel=10000,
                   enclosed=True, start_gcode=CREALITY_K_START, end_gcode="END_PRINT",
                   orca_machine="Creality K2 Plus 0.4 nozzle"),
    PrinterProfile("creality-k1", "Creality K1 / K1C", (220, 220), 250, "klipper", max_speed=300, accel=8000,
                   enclosed=True, start_gcode=KLIPPER_START, end_gcode="END_PRINT",
                   orca_machine="Creality K1C 0.4 nozzle"),
    PrinterProfile("creality-k1-max", "Creality K1 Max", (300, 300), 300, "klipper", max_speed=300, accel=8000,
                   enclosed=True, start_gcode=KLIPPER_START, end_gcode="END_PRINT",
                   orca_machine="Creality K1 Max (0.4 nozzle)"),
    PrinterProfile("creality-ender3-v3", "Creality Ender-3 V3", (220, 220), 250, "klipper", max_speed=250,
                   accel=6000, start_gcode=KLIPPER_START, end_gcode="END_PRINT",
                   orca_machine="Creality Ender-3 V3 0.4 nozzle"),
    PrinterProfile("bambu-x1c", "Bambu Lab X1 Carbon", (256, 256), 256, "marlin2", max_speed=300, accel=10000,
                   enclosed=True, orca_machine="Bambu Lab X1 Carbon 0.4 nozzle"),
    PrinterProfile("bambu-p1s", "Bambu Lab P1S", (256, 256), 256, "marlin2", max_speed=300, accel=10000,
                   enclosed=True, orca_machine="Bambu Lab P1S 0.4 nozzle"),
    PrinterProfile("bambu-p1p", "Bambu Lab P1P", (256, 256), 256, "marlin2", max_speed=300, accel=10000,
                   orca_machine="Bambu Lab P1P 0.4 nozzle"),
    PrinterProfile("bambu-a1", "Bambu Lab A1", (256, 256), 256, "marlin2", max_speed=250, accel=10000,
                   orca_machine="Bambu Lab A1 0.4 nozzle"),
    PrinterProfile("bambu-a1-mini", "Bambu Lab A1 mini", (180, 180), 180, "marlin2", max_speed=250, accel=10000,
                   orca_machine="Bambu Lab A1 mini 0.4 nozzle"),
    PrinterProfile("prusa-mk4", "Original Prusa MK4/MK4S", (250, 210), 220, "marlin2", max_speed=200, accel=4000,
                   orca_machine="Prusa MK4 0.4 nozzle", start_gcode=MARLIN_START, end_gcode=MARLIN_END,
                   upload_ext=".bgcode"),
    PrinterProfile("prusa-mk3", "Original Prusa MK3S+", (250, 210), 210, "marlin2", max_speed=150, accel=1250,
                   direct_drive=True, orca_machine="Prusa MK3S 0.4 nozzle", start_gcode=MARLIN_START,
                   end_gcode=MARLIN_END),
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
    needs_enclosure: bool = False
    retract_mm: float = 0.8
    note: str = ""


MATERIALS: dict[str, Material] = {m.name: m for m in (
    Material("PLA", 215, 220, 60, 65, 100, 100, 300, note="Easiest. Keep the lid or door open on enclosed printers."),
    Material("PETG", 240, 245, 75, 80, 30, 60, 200, retract_mm=1.0,
             note="Strings a little. Use a textured or PEI bed; glue on smooth PEI so it doesn't bond too hard."),
    Material("ABS", 255, 260, 100, 100, 0, 20, 200, chamber=45, needs_enclosure=True,
             note="Needs an enclosed printer and good ventilation; fumes."),
    Material("ASA", 260, 265, 100, 100, 0, 30, 200, chamber=45, needs_enclosure=True,
             note="UV resistant ABS. Needs an enclosure and ventilation."),
    Material("TPU", 225, 225, 45, 45, 60, 100, 30, retract_mm=0.4,
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
    if mat.name == "TPU" and not printer.direct_drive:
        warnings.append(f"TPU needs a direct drive extruder and {printer.label} is Bowden: it will likely jam")
    if warnings and not force:
        raise ConfigError(warnings[0], "Use PLA or PETG on this printer. If the person has an enclosure or "
                                       "accepts the risk, they can ask for --force")
    q = QUALITY[quality]
    speed = min(printer.max_speed, mat.max_speed)
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
        "travel_speed_mms": printer.max_speed, "accel_mms2": printer.accel, "retract_mm": mat.retract_mm,
        "supports": "auto, for overhangs flatter than 50 degrees from horizontal", "brim": "auto for small footprints",
        "bed_mm": list(printer.bed), "max_height_mm": printer.height,
        "note": " ".join(n for n in (mat.note, printer.note) if n), "filament_mm": printer.filament_mm,
        **({"warning": "; ".join(warnings)} if warnings else {}),
    }


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
        "--travel-speed", str(s["travel_speed_mms"]), "--retract-length", str(s["retract_mm"]),
        "--filament-type", s["material"], "--support-material", "--support-material-auto",
        "--support-material-threshold", "50", "--brim-width", "0", "--skirts", "1",
        "--center", f"{w // 2},{d // 2}", "--ensure-on-bed",
    ]
    if printer.filament_mm != 1.75:
        a += ["--filament-diameter", str(printer.filament_mm)]
    if printer.flavor == "klipper":
        a += ["--use-relative-e-distances", "--use-firmware-retraction", "--max-print-speed",
              str(s["infill_speed_mms"]), "--machine-max-acceleration-extruding", str(s["accel_mms2"])]
    if printer.start_gcode:
        a += ["--start-gcode", printer.start_gcode.replace("{chamber_temperature[0]}", str(s["chamber_c"]))]
    if printer.end_gcode:
        a += ["--end-gcode", printer.end_gcode]
    return a


def orca_overrides(s: dict, printer: PrinterProfile) -> tuple[dict, dict, dict]:
    machine = {"type": "machine", "name": "gantrel-machine", "inherits": printer.orca_machine, "from": "User"}
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
                "fan_min_speed": [str(s["fan_percent"][0])], "fan_max_speed": [str(s["fan_percent"][1])]}
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


def slice_model(model_path: Path, out_dir: Path, printer: PrinterProfile, s: dict,
                prefer: str | None = None, timeout: int = 900) -> tuple[Path, str, list[str]]:
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
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", model_path.stem)
    ext = ".bgcode" if kind == "prusaslicer" and printer.upload_ext == ".bgcode" else ".gcode"
    final = out_dir / f"{stem}_{printer.key}_{s['material']}{ext}"
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
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise PrinterError(f"{kind} took longer than {timeout}s", "Try a smaller model or the draft quality") from None
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout).strip().splitlines()[-5:]
            raise PrinterError(f"{kind} failed (exit {proc.returncode})", " | ".join(tail)[:400])
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
