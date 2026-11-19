"""A fake stone on PATH and a running studio server for each test."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from studiokit import Client, start  # noqa: E402

# The fake stone records every argv as one JSON line in $FAKE_STONE_LOG and writes job files in the
# shape a real stone job has (status.json, plan.json, brief.md, build/params.json, build/check.json,
# build/part.stl). $FAKE_STONE_DELAY makes build slow; $FAKE_STONE_FAIL makes build exit 1.
FAKE_STONE = r'''
import json, os, struct, sys, time
from pathlib import Path

SCHEMA = [
    {"name": "size", "type": "number", "default": 120, "min": 25, "max": 200, "unit": "mm", "help": "fan size"},
    {"name": "style", "type": "choice", "default": "rings", "choices": ["rings", "honeycomb", "spiral", "grid"]},
    {"name": "thickness", "type": "number", "default": 3, "min": 1.6, "max": 10, "unit": "mm"},
    {"name": "rings", "type": "int", "default": 3, "min": 1, "max": 12},
    {"name": "chamfer", "type": "bool", "default": False},
    {"name": "label", "type": "text", "default": ""},
    {"name": "icon", "type": "image", "default": ""},
]

argv = sys.argv[1:]
with open(os.environ["FAKE_STONE_LOG"], "a") as f:
    f.write(json.dumps(argv) + "\n")
job = Path(argv[argv.index("--job") + 1]) if "--job" in argv else Path("stone-job")
words = [a for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or argv[i - 1] != "--job")]
cmd = words[0]

def rj(p, d=None):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return d

def wj(p, d):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=2))
    os.replace(tmp, p)

def status(**kw):
    s = rj(job / "status.json", {}) or {}
    s.update(kw)
    wj(job / "status.json", s)

def box_stl(x, y, z):
    v = [(0, 0, 0), (x, 0, 0), (x, y, 0), (0, y, 0), (0, 0, z), (x, 0, z), (x, y, z), (0, y, z)]
    f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
         (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    out = bytearray(b"fake stone".ljust(80, b" ") + struct.pack("<I", len(f)))
    for a, b, c in f:
        out += struct.pack("<12fH", 0, 0, 0, *v[a], *v[b], *v[c], 0)
    return bytes(out)

if cmd == "new":
    job.mkdir(parents=True, exist_ok=True)
    (job / "brief.md").write_text("# Request\n\n" + words[1] + "\n")
    status(stage="new", last_command="", last_result="job created", next="stone recipes", builds=0, failed_builds=0, passed=False)
    print("NEXT: stone recipes")
elif cmd == "show":
    if words[1] != "fan_guard":
        print(json.dumps({"error": "no recipe", "closest": []}))
        sys.exit(1)
    print(json.dumps({"name": "fan_guard", "title": "Fan guard", "summary": "Grille for a PC fan.", "tier": "S",
                      "params": SCHEMA, "from_request": {}, "current": {}}))
elif cmd == "set":
    plan = rj(job / "plan.json", {}) or {}
    rest = words[1:]
    recipe = plan.get("recipe")
    if rest and "=" not in rest[0]:
        recipe = rest.pop(0)
    params = {} if "--reset" in argv or plan.get("recipe") != recipe else dict(plan.get("params", {}))
    for it in rest:
        k, v = it.split("=", 1)
        try:
            params[k] = float(v)
        except ValueError:
            params[k] = v
    wj(job / "plan.json", {"recipe": recipe, "params": params})
    status(stage="plan", last_command="stone set", next="stone build")
    print("NEXT: stone build")
elif cmd == "build":
    time.sleep(float(os.environ.get("FAKE_STONE_DELAY", "0")))
    plan = rj(job / "plan.json")
    merged = {p["name"]: p["default"] for p in SCHEMA}
    merged.update(plan["params"])
    if os.environ.get("FAKE_STONE_FAIL"):
        print("\n".join(f"line {i}" for i in range(30)))
        print("FIX: thickness too thin")
        print("NEXT: stone set fan_guard thickness=3")
        sys.exit(1)
    size, t = float(merged["size"]), float(merged["thickness"])
    wj(job / "build" / "params.json", {"recipe": plan["recipe"], "params": merged})
    if os.environ.get("FAKE_STONE_CHECKFAIL"):
        wj(job / "build" / "check.json", {"passed": False, "size_mm": [size, size, t],
           "results": [{"name": "bodies", "ok": False, "value": 2, "detail": "2 bodies (max 1)"}]})
        (job / "build" / "part.stl").write_bytes(box_stl(size, size, t))
        print("FIX: bodies 2 bodies (max 1)")
        sys.exit(1)
    wj(job / "build" / "check.json", {"passed": True, "size_mm": [size, size, t],
       "results": [{"name": "watertight", "ok": True, "value": True, "detail": "closed surface"}]})
    (job / "build" / "part.stl").write_bytes(box_stl(size, size, t))
    (job / "build" / "preview.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    status(stage="done", last_command="stone build", last_result=f"build passed, {size} x {size} x {t} mm",
           next="none, the part is ready", passed=True)
    print("NEXT: none, the part is ready")
else:
    print("NEXT: stone status")
'''


@pytest.fixture(autouse=True)
def printer_home(tmp_path, monkeypatch):
    """Never read the real printer config."""
    monkeypatch.setenv("GANTREL_PRINTER_HOME", str(tmp_path / "gp-home"))


@pytest.fixture
def fake_stone(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "stone"
    exe.write_text(f"#!{sys.executable}\n" + FAKE_STONE)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "stone-argv.jsonl"
    log.touch()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_STONE_LOG", str(log))
    monkeypatch.delenv("FAKE_STONE_DELAY", raising=False)
    monkeypatch.delenv("FAKE_STONE_FAIL", raising=False)
    monkeypatch.delenv("FAKE_STONE_CHECKFAIL", raising=False)

    class Fake:
        path = exe

        def calls(self) -> list[list[str]]:
            return [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]

        def builds(self) -> int:
            return sum(1 for c in self.calls() if c and c[0] == "build")

        def clear(self) -> None:
            log.write_text("")

    return Fake()


@pytest.fixture
def job(tmp_path, fake_stone):
    """A job with a plan and a first build, made by the fake stone the way stone would."""
    import subprocess

    j = tmp_path / "stone-job"
    for args in (["new", "fan guard for a 40mm fan", "--job", str(j)],
                 ["set", "--job", str(j), "fan_guard", "size=40"],
                 ["build", "--job", str(j)]):
        subprocess.run([str(fake_stone.path), *args], check=True, capture_output=True)
    fake_stone.clear()
    return j


@pytest.fixture
def studio(job):
    httpd = start(job)
    yield Client(httpd)
    httpd.shutdown()
    httpd.server_close()
