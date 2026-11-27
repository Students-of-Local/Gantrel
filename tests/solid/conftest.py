"""Run the real gantrel-solid in a scratch HOME and working folder, with a stub release server.

Build and measure need the real SolidCraft CLI ($GANTREL_SOLIDCRAFT, e.g. from `gantrel-solid fetch --yes`
in CI) and smoothstone's stone on PATH; without them those tests skip and say why.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

class Run:
    def __init__(self, home: Path, work: Path):
        self.home, self.work = home, work
        self.env = {k: v for k, v in os.environ.items() if k not in ("GANTREL_SOLIDCRAFT", "GANTREL_SOLIDCRAFT_RELEASE")}
        self.env["HOME"] = str(home)

    def __call__(self, *args: str, cwd: Path | None = None) -> tuple[int, dict]:
        p = subprocess.run([sys.executable, "-m", "gantrel_solid", *args], cwd=cwd or self.work, env=self.env,
                           capture_output=True, text=True, timeout=600)
        lines = p.stdout.strip().splitlines()
        assert lines, p.stderr
        return p.returncode, json.loads(lines[-1])


@pytest.fixture
def run(tmp_path) -> Run:
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    return Run(home, work)


@pytest.fixture
def solid(run) -> Run:
    exe = os.environ.get("GANTREL_SOLIDCRAFT")
    if not exe or not Path(exe).is_file():
        pytest.skip("GANTREL_SOLIDCRAFT does not name solidcraft-cli (run gantrel-solid fetch --yes and set it)")
    if not shutil.which("stone"):
        pytest.skip("stone (smoothstone) is not on PATH; build runs its print checks")
    run.env["GANTREL_SOLIDCRAFT"] = exe
    return run


class Release:
    """A release folder served on 127.0.0.1; requests lists every path asked for. before(path) runs before a
    file is served, and a path in cut is sent with a Content-Length past its real end."""

    def __init__(self, root: Path):
        self.root, self.requests, self.before, self.cut = root, [], None, set()
        outer = self

        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, *a):
                outer.requests.append(self.path)

            def do_GET(self):
                if outer.before:
                    outer.before(self.path)
                if self.path not in outer.cut:
                    return super().do_GET()
                body = (outer.root / self.path.lstrip("/")).read_bytes()[:10]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body) + 1000))
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(root)))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"


@pytest.fixture
def release(tmp_path):
    root = tmp_path / "release"
    root.mkdir()
    r = Release(root)
    yield r
    r.httpd.shutdown()
    r.httpd.server_close()
