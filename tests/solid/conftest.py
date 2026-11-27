"""Run the real gantrel-solid in a scratch HOME and working folder, with a stub release server."""

from __future__ import annotations

import json
import os
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


class Release:
    """A release folder served on 127.0.0.1; requests lists every path asked for."""

    def __init__(self, root: Path):
        self.root, self.requests = root, []
        outer = self

        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, *a):
                outer.requests.append(self.path)

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
