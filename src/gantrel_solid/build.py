"""gantrel-solid build and measure: one SolidCraft design into a job folder, checked for printing.

build runs the design headless into the job's build folder, in the layout Gantrel Studio follows, and runs
smoothstone's print checks (stone check) and the 220 mm plate check; measure gives a part's size and volume.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from gantrel_printer.plan import mesh as meshes
from gantrel_printer.slicing import run_slicer

from .cli import CLI, Refused, emit, find

RUN_TIMEOUT = 120
CHECK_TIMEOUT = 120
PLATE_MM = 220.0
PARTS = ("part.step", "part.stl", "part.3mf")
# Only commands that model the open design; SolidCraft's file, document, app and plugin commands are refused.
MODELING = ("sketch.", "solid.", "construct.", "face.", "surface.", "body.", "parameters.change", "material.",
            "appearance.", "plastic.")
REACHES_FILES = re.compile(r"insert|import|export|save|open|load")


def job_dir(arg: str) -> Path:
    """The job folder, inside the working folder, reached through no '..' and no symlink."""
    raw = Path(arg)
    if ".." in raw.parts:
        raise Refused(f"--job {arg} uses '..'; give a folder inside the working folder")
    cwd = Path.cwd().resolve()
    full = raw if raw.is_absolute() else cwd / raw
    if not full.is_relative_to(cwd):
        raise Refused(f"--job {arg} is outside the working folder {cwd}")
    p = cwd
    for part in (*full.relative_to(cwd).parts, "build"):
        p = p / part
        if p.is_symlink():
            raise Refused(f"--job {arg} goes through the symlink {p}")
    return full


def read_design(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise Refused(f"cannot read the design {path}: {e}") from None
    cmds = data.get("commands") if isinstance(data, dict) else data
    if not isinstance(cmds, list) or not cmds:
        raise Refused('the design must be {"commands": [{"command": ..., "params": {...}}, ...]}')
    for i, c in enumerate(cmds):
        if not isinstance(c, dict) or not isinstance(c.get("command"), str) or not isinstance(c.get("params", {}), dict):
            raise Refused(f'commands[{i}] must be {{"command": "<id>", "params": {{...}}}}')
        cid = c["command"]
        if not cid.startswith(MODELING) or REACHES_FILES.search(cid):
            raise Refused(f"commands[{i}] {cid} reaches outside the design; build exports the files itself")
    sources = data.get("sources") if isinstance(data, dict) else None
    return {"sources": sources, "commands": cmds} if isinstance(sources, list) else {"commands": cmds}


def stone_checks(stl: Path) -> list[dict]:
    """smoothstone's print checks (watertight, one piece, flat base, walls, overhangs, size) on the STL."""
    stone = shutil.which("stone")
    if not stone:
        raise Refused("stone is not installed; the print checks are smoothstone's (see the README's plugin section)")
    try:
        code, out, err = run_slicer([stone, "check", str(stl)], CHECK_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise Refused(f"stone check took longer than {CHECK_TIMEOUT} s") from None
    results = []
    for line in out.splitlines():
        if m := re.match(r"PASS (\w+): (.*)", line):
            results.append({"name": m[1], "ok": True, "detail": m[2], "advice": ""})
        elif m := re.match(r"FIX: (\w+) (.*?); (.*)", line):
            results.append({"name": m[1], "ok": False, "detail": m[2], "advice": m[3]})
    if code not in (0, 1) or not results:
        raise Refused(f"stone check failed: {(err or out).strip()[:300]}")
    return results


def plate_check(size: list[float]) -> dict:
    ok = size[0] <= PLATE_MM and size[1] <= PLATE_MM
    return {"name": "plate", "ok": ok, "detail": f"{size[0]:g} x {size[1]:g} mm on a {PLATE_MM:g} mm plate",
            "advice": "" if ok else f"make the part fit within {PLATE_MM:g} x {PLATE_MM:g} mm"}


def build(args) -> int:
    try:
        job = job_dir(args.job)
        design = read_design(Path(args.design) if args.design else job / "design.json")
        exe = find()
        if not exe:
            raise Refused(f"{CLI} is not installed; run gantrel-solid doctor")
    except Refused as e:
        emit({"ok": False, "error": str(e)})
        return 2
    out = job / "build"
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".solid-", dir=out))
    try:
        return _build(exe, design, work, out)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _build(exe: str, design: dict, work: Path, out: Path) -> int:
    (work / "design.json").write_text(json.dumps(design, indent=1) + "\n")
    argv = [exe, "run", str(work / "design.json"), "--quiet", "--results", "--json-out", str(work / "results.json")]
    for f in PARTS:
        argv += ["--out", str(work / f)]
    try:
        code, _, err = run_slicer(argv, RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        emit({"ok": False, "error": f"SolidCraft took longer than {RUN_TIMEOUT} s and was stopped", "files": []})
        return 1
    if code != 0:
        failed = {}
        try:
            failed = json.loads((work / "results.json").read_text()).get("error") or {}
        except (OSError, ValueError, AttributeError):
            pass
        if failed.get("step"):
            i = int(failed["step"]) - 1
            emit({"ok": False, "index": i, "command": failed.get("command"), "files": [],
                  "error": f"commands[{i}] {failed.get('command')}: {failed.get('error')}"})
        else:
            emit({"ok": False, "files": [], "error": f"SolidCraft failed: {err.strip()[-300:]}"})
        return 1
    missing = [f for f in PARTS if not (work / f).is_file()]
    if missing:
        emit({"ok": False, "files": [], "error": f"SolidCraft wrote no {', '.join(missing)}"})
        return 1
    try:
        size = [round(float(v), 3) for v in meshes.load(work / "part.stl").size]
        checks = stone_checks(work / "part.stl") + [plate_check(size)]
    except (Refused, meshes.MeshError) as e:
        emit({"ok": False, "files": [], "error": str(e)})
        return 1
    ok = all(c["ok"] for c in checks)
    (work / "check.json").write_text(json.dumps({"results": checks, "size_mm": size, "passed": ok}, indent=1) + "\n")
    for f in ("design.json", *PARTS, "check.json"):
        os.replace(work / f, out / f)
    fixes = [f"{c['name']}: {c['detail']}; {c['advice']}" for c in checks if not c["ok"]]
    emit({"ok": ok, "files": [str(out / f) for f in PARTS], "size_mm": size,
          "checks": {c["name"]: c["ok"] for c in checks}, "fixes": fixes})
    return 0 if ok else 1


def measure(args) -> int:
    f = Path(args.file)
    try:
        if f.suffix.lower() in (".step", ".stp"):
            exe = find()
            if not exe:
                raise Refused(f"{CLI} is not installed; run gantrel-solid doctor")
            p = subprocess.run([exe, "eval", str(f)], capture_output=True, text=True, timeout=RUN_TIMEOUT,
                               stdin=subprocess.DEVNULL)
            if p.returncode != 0:
                raise Refused(f"SolidCraft could not read {f}: {p.stderr.strip()[-300:]}")
            ev = json.loads(p.stdout)
            lo = [min(b["bbox"]["min"][k] for b in ev["bodies"]) for k in range(3)]
            hi = [max(b["bbox"]["max"][k] for b in ev["bodies"]) for k in range(3)]
            volume, bodies = float(ev["total"]["volume_mm3"]), int(ev["body_count"])
        else:
            m = meshes.load(f)
            lo, hi = (list(map(float, v)) for v in m.bbox)
            volume, bodies = abs(m.volume), None
    except (Refused, meshes.MeshError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
        emit({"ok": False, "error": str(e)})
        return 1
    emit({"ok": True, "file": str(f), "size_mm": [round(b - a, 3) for a, b in zip(lo, hi)],
          "min": [round(v, 3) for v in lo], "max": [round(v, 3) for v in hi],
          "volume_mm3": round(volume, 1), "bodies": bodies})
    return 0
