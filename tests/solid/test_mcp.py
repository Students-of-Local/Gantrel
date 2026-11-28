"""solid_build and solid_measure through the real gantrel-mcp over stdio JSON-RPC, against the real CLI."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from gantrel_mcp import server
from gantrel_printer.errors import PrinterError, Refused

DESIGNS = Path(__file__).parent / "designs"



class Stdio:
    """python -m gantrel_mcp in the working folder, spoken to line by line as an MCP client does."""

    def __init__(self, cwd: Path, env: dict, log: Path):
        self.log = open(log, "w")
        self.p = subprocess.Popen([sys.executable, "-m", "gantrel_mcp"], cwd=cwd, env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=self.log, text=True)
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=lambda: [self.lines.put(ln) for ln in self.p.stdout], daemon=True).start()
        self.n = 0

    def send(self, msg: dict):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def ask(self, method: str, params: dict | None = None) -> dict:
        self.n += 1
        self.send({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
        while True:
            msg = json.loads(self.lines.get(timeout=120))
            if msg.get("id") == self.n:
                return msg

    def call(self, name: str, args: dict) -> tuple[bool, str]:
        r = self.ask("tools/call", {"name": name, "arguments": args})["result"]
        return bool(r.get("isError")), r["content"][0]["text"]

    def close(self):
        self.p.stdin.close()
        try:
            self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
        self.log.close()


@pytest.fixture
def mcp(solid, tmp_path):
    s = Stdio(solid.work, solid.env, tmp_path / "mcp.log")
    init = s.ask("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "gantrel-tests", "version": "0"}})
    assert init["result"]["serverInfo"]["name"] == "Gantrel", init
    s.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    yield s
    s.close()


def named(result: dict, work: Path) -> dict:
    out = {k: v for k, v in result.items() if k != "next"}
    for key in ("files",):
        out[key] = [Path(f).name for f in out.get(key, [])]
    if "file" in out:
        out["file"] = str(Path(out["file"]).relative_to(work))
    return out


def test_solid_build_over_stdio_matches_the_cli(solid, mcp):
    design = solid.work / "plate.json"
    shutil.copy(DESIGNS / "plate.json", design)
    code, cli = solid("build", str(design), "--job", "cli-job")
    assert code == 0, cli
    tools = {t["name"] for t in mcp.ask("tools/list")["result"]["tools"]}
    assert {"solid_build", "solid_measure"} <= tools
    err, text = mcp.call("solid_build", {"design": str(design), "job": "mcp-job"})
    assert not err, text
    got = json.loads(text)
    assert (solid.work / "mcp-job" / "build" / "part.step").is_file()
    assert named(got, solid.work) == named(cli, solid.work)
    code, cli_m = solid("measure", "cli-job/build/part.step")
    assert code == 0, cli_m
    err, text = mcp.call("solid_measure", {"path": str(solid.work / "mcp-job" / "build" / "part.step")})
    assert not err, text
    got_m = json.loads(text)
    assert {k: v for k, v in got_m.items() if k not in ("file", "next")} == {k: v for k, v in cli_m.items() if k != "file"}
    assert got_m["size_mm"] == pytest.approx([80, 40, 5], abs=0.1)


def test_solid_build_over_stdio_refuses_what_the_cli_refuses(solid, mcp, tmp_path):
    design = solid.work / "plate.json"
    shutil.copy(DESIGNS / "plate.json", design)
    err, text = mcp.call("solid_build", {"design": str(design), "job": "../outside"})
    assert err and "uses '..'" in text, text
    assert not (solid.work.parent / "outside").exists()
    away = tmp_path / "away"
    away.mkdir()
    err, text = mcp.call("solid_build", {"design": str(design), "job": str(away)})
    assert err and "outside the working folder" in text, text
    (solid.work / "link").symlink_to(away)
    err, text = mcp.call("solid_build", {"design": str(design), "job": "link"})
    assert err and "goes through the symlink" in text, text
    assert list(away.iterdir()) == []
    elsewhere = tmp_path / "elsewhere.json"
    shutil.copy(DESIGNS / "plate.json", elsewhere)
    err, text = mcp.call("solid_build", {"design": str(elsewhere), "job": "mcp-job"})
    assert err and "outside your home folder" in text, text
    assert not (tmp_path / "outside").exists() and not (solid.work / "mcp-job").exists()


FAKE = """import json, os, subprocess, sys, time
cmd = sys.argv[1]
if cmd == "sleep":
    child = subprocess.Popen(["sleep", "60"])
    open(os.environ["FAKE_PIDS"], "w").write(f"{os.getpid()} {child.pid}")
    time.sleep(60)
elif cmd == "env":
    print(json.dumps({"ok": True, "ask": sorted(k for k in os.environ if k.startswith("LOCAL_STUDIO_ASK"))}))
elif cmd == "list":
    print("[]")
else:
    sys.exit(1)
"""


@pytest.fixture
def fake_solid(tmp_path, monkeypatch):
    pkg = tmp_path / "fake" / "gantrel_solid"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text(FAKE)
    monkeypatch.setenv("PYTHONPATH", str(pkg.parent))
    monkeypatch.setenv("FAKE_PIDS", str(tmp_path / "pids"))
    return tmp_path / "pids"


def gone(pid: int) -> bool:
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def test_a_solid_call_past_its_limit_is_stopped_with_its_processes(fake_solid, monkeypatch):
    monkeypatch.setattr(server, "SOLID_S", 1.0)
    started = time.monotonic()
    with pytest.raises(Refused, match="took longer than 1 s"):
        server.solid("sleep")
    assert time.monotonic() - started < 5
    assert all(gone(int(pid)) for pid in fake_solid.read_text().split())


@pytest.mark.parametrize("cmd", ["crash", "list"])
def test_a_solid_call_with_no_result_is_an_error_not_a_refusal(fake_solid, cmd):
    with pytest.raises(PrinterError, match="printed no result") as e:
        server.solid(cmd)
    assert not isinstance(e.value, Refused)


def test_the_solid_child_gets_no_ask_variables(fake_solid, monkeypatch):
    for k in ("LOCAL_STUDIO_ASK", "LOCAL_STUDIO_ASK_TOKEN", "LOCAL_STUDIO_ASK_PID"):
        monkeypatch.setenv(k, "x")
    assert server.solid("env") == {"ok": True, "ask": []}
