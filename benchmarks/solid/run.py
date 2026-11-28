"""Run the solidcraft skill on a local model over the held-out asks in asks.json and score what it built.

    GANTREL_SOLIDCRAFT=<solidcraft-cli> uv run python benchmarks/solid/run.py --url http://127.0.0.1:11434/v1 \
        --model <model> --host "<machine and GPU>"

The model gets the skill the way Local Studio sends a plugin command (the skill, then the request after
</skill>) and one bash tool. Each ask runs in a fresh scratch folder that is also its HOME, with only
gantrel-solid and stone on PATH (no gantrel-studio, so no server is left running). Each ask is scored on
what is on disk afterwards, never on the model's words: the job built, every print check passed, the
part's size within 0.1 mm of the ask, its volume within 1 percent, and its features where the ask
puts them: each point in the ask's empty_mm must be outside the part and each in solid_mm inside it,
measured from the part's lowest x, y and z corner. Writes
benchmarks/results/solidcraft/<model>.json.

This is a developer tool: it talks to a model server directly (such as Ollama), and the model's bash
commands run unsandboxed as you, so run it on a machine and account you can spare.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import numpy as np

from gantrel_printer.plan import mesh as meshes

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SKILL = ROOT / "skills" / "plugin" / "solidcraft" / "SKILL.md"
RESULTS = ROOT / "benchmarks" / "results" / "solidcraft"
TURNS = 24
REPLY_S = 600
BASH_TIMEOUT = 180
OUTPUT_CHARS = 4000
TOOLS = [{"type": "function", "function": {
    "name": "bash", "description": "Run one bash command in the project folder and return its output.",
    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}]


def chat(base: str, model: str, messages: list) -> dict:
    body = json.dumps({"model": model, "messages": messages, "tools": TOOLS, "temperature": 0}).encode()
    req = urllib.request.Request(f"{base}/chat/completions", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=REPLY_S) as r:
        return json.loads(r.read())["choices"][0]["message"]


def bash(command: str, cwd: Path, env: dict) -> str:
    try:
        p = subprocess.run(["bash", "-c", command], cwd=cwd, env=env, capture_output=True, text=True,
                           errors="replace", timeout=BASH_TIMEOUT, stdin=subprocess.DEVNULL)
        out = p.stdout + p.stderr + ("" if p.returncode == 0 else f"\n(exit {p.returncode})")
    except subprocess.TimeoutExpired:
        out = f"(stopped after {BASH_TIMEOUT} s)"
    return out[-OUTPUT_CHARS:] or "(no output)"


def solid(cwd: Path, env: dict, *args: str) -> dict | None:
    p = subprocess.run([sys.executable, "-m", "gantrel_solid", *args], cwd=cwd, env=env, capture_output=True,
                       text=True, timeout=300)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def inside(tris: np.ndarray, points: np.ndarray) -> list[bool]:
    """Whether each point is inside the closed mesh: an odd number of triangles crossed by a ray from it."""
    d = np.array([1.0, 0.0013, 0.0007])
    v0, e1, e2 = tris[:, 0], tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]
    h = np.cross(d, e2)
    a = (e1 * h).sum(1)
    f = np.where(np.abs(a) > 1e-12, 1 / np.where(a == 0, 1, a), 0)
    out = []
    for p in points:
        s = p - v0
        u = f * (s * h).sum(1)
        q = np.cross(s, e1)
        v = f * (q @ d)
        t = f * (e2 * q).sum(1)
        out.append(bool(((f != 0) & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 1e-9)).sum() % 2))
    return out


def features(ask: dict, stl: Path) -> bool:
    try:
        tris = meshes.load(stl).tris
    except meshes.MeshError:
        return False
    lo = tris.reshape(-1, 3).min(0)
    empty = inside(tris, lo + np.array(ask["empty_mm"], float).reshape(-1, 3))
    solid = inside(tris, lo + np.array(ask["solid_mm"], float).reshape(-1, 3))
    return not any(empty) and all(solid)


def score(ask: dict, work: Path) -> dict:
    """Rebuild the model's solid-job/design.json in a folder of our own and measure that, so only the design
    counts, never files the model wrote."""
    judge = Path(tempfile.mkdtemp(prefix="solid-score-"))
    env = scratch_env(judge)
    design = work / "solid-job" / "design.json"
    built = None
    if design.is_file() and not design.is_symlink():
        shutil.copyfile(design, judge / "design.json")
        built = solid(judge, env, "build", "design.json", "--job", "job")
    step = judge / "job" / "build" / "part.step"
    m = solid(judge, env, "measure", str(step)) if step.is_file() else None
    ok = bool(m and m.get("ok"))
    size = sorted(m["size_mm"]) if ok else None
    gates = {
        "built": ok,
        "prints": bool(built and built.get("ok")),
        "size": size is not None and all(abs(a - b) <= 0.1 for a, b in zip(size, sorted(ask["size_mm"]))),
        "volume": ok and abs(m["volume_mm3"] - ask["volume_mm3"]) <= 0.01 * ask["volume_mm3"],
        "features": ok and features(ask, judge / "job" / "build" / "part.stl"),
    }
    shutil.rmtree(judge, ignore_errors=True)
    return {"gates": gates, "pass": all(gates.values()), "size_mm": m and m.get("size_mm"),
            "volume_mm3": m and m.get("volume_mm3"), "fixes": (built or {}).get("fixes")}


def scratch_env(work: Path) -> dict:
    bin_dir = work / ".bin"
    bin_dir.mkdir()
    for tool in ("gantrel-solid", "stone"):
        found = shutil.which(tool)
        if not found:
            sys.exit(f"{tool} is not on PATH")
        (bin_dir / tool).symlink_to(found)
    env = {"HOME": str(work), "PATH": f"{bin_dir}:/usr/bin:/bin", "LANG": "C.UTF-8"}
    if os.environ.get("GANTREL_SOLIDCRAFT"):
        env["GANTREL_SOLIDCRAFT"] = os.environ["GANTREL_SOLIDCRAFT"]
    return env


def run_ask(base: str, model: str, ask: dict) -> dict:
    work = Path(tempfile.mkdtemp(prefix="solid-ask-"))
    env = scratch_env(work)
    skill = SKILL.read_text()
    messages = [{"role": "user", "content": f'<skill name="solidcraft">\n{skill}\n</skill>\n\n{ask["ask"]}'}]
    calls, started, stopped = [], time.monotonic(), None
    for _ in range(TURNS):
        try:
            msg = chat(base, model, messages)
        except (OSError, ValueError, KeyError) as e:
            stopped = f"no reply: {e}"
            break
        messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
        if not msg.get("tool_calls"):
            break
        for call in msg["tool_calls"]:
            raw = call["function"].get("arguments") or {}
            try:
                command = str((json.loads(raw) if isinstance(raw, str) else raw).get("command", ""))
            except (ValueError, AttributeError):
                command = ""
            out = bash(command, work, env) if command else "(no command given)"
            calls.append(command.replace(str(work), "<work>"))
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": out})
    result = score(ask, work)
    log = json.dumps(messages, sort_keys=True)
    return {"id": ask["id"], "ask": ask["ask"], **result, "ran_build": any("gantrel-solid build" in c for c in calls),
            "stopped": stopped,
            "tool_calls": calls, "seconds": round(time.monotonic() - started),
            "log_sha256": hashlib.sha256(log.encode()).hexdigest(), "work": str(work), "transcript": messages}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True, help="OpenAI-compatible base URL, such as http://127.0.0.1:11434/v1")
    p.add_argument("--model", required=True)
    p.add_argument("--host", required=True, help="the machine and GPU, for the results file")
    p.add_argument("--asks", default=str(HERE / "asks.json"))
    p.add_argument("--transcripts", help="folder for each ask's full transcript")
    args = p.parse_args()
    if not os.environ.get("GANTREL_SOLIDCRAFT"):
        sys.exit("set GANTREL_SOLIDCRAFT to solidcraft-cli (gantrel-solid fetch --yes puts it in ~/.local/share/gantrel)")
    asks = json.loads(Path(args.asks).read_text())
    RESULTS.mkdir(parents=True, exist_ok=True)
    dest = RESULTS / (re.sub(r"[^A-Za-z0-9.-]+", "_", args.model) + ".json")
    results = []
    for ask in asks:
        r = run_ask(args.url.rstrip("/"), args.model, ask)
        if args.transcripts:
            Path(args.transcripts).mkdir(parents=True, exist_ok=True)
            (Path(args.transcripts) / f"{ask['id']}.json").write_text(json.dumps(r["transcript"], indent=1))
        r.pop("transcript")
        print(json.dumps({k: r[k] for k in ("id", "pass", "gates", "size_mm", "seconds")}), flush=True)
        results.append(r)
        gates = sorted(results[0]["gates"])
        out = {"skill": "solidcraft", "model": args.model, "host": args.host,
               "date": datetime.date.today().isoformat(), "asks": len(results),
               "pass_rate": sum(r["pass"] for r in results) / len(results),
               "gate_pass_rate": {g: sum(r["gates"][g] for r in results) / len(results) for g in gates},
               "results": [{k: v for k, v in r.items() if k != "work"} for r in results]}
        dest.write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {dest.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
