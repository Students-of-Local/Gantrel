"""solid_build and solid_measure through the real gantrel-mcp over stdio JSON-RPC, against the real CLI."""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

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
    elsewhere = tmp_path / "elsewhere.json"
    shutil.copy(DESIGNS / "plate.json", elsewhere)
    err, text = mcp.call("solid_build", {"design": str(elsewhere), "job": "mcp-job"})
    assert err and "outside your home folder" in text, text
    assert not (tmp_path / "outside").exists() and not (solid.work / "mcp-job").exists()
