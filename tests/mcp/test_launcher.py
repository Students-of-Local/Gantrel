"""bin/gantrel-mcp keys its Python environment on the plugin root, so two checkouts (Local Studio's
plugin folder and a Claude Code checkout) never share one venv and never re-point each other's editable
install. A stub uv on PATH prints what the launcher would run; no environment is built."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[2] / "bin" / "gantrel-mcp"
STUB = '#!/bin/sh\nprintf "%s\\n" "$UV_PROJECT_ENVIRONMENT" "$*"\n'


@pytest.fixture
def run(tmp_path):
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "uv").write_text(STUB)
    (stub / "uv").chmod(0o755)
    cache = tmp_path / "cache"
    env = {"PATH": f"{stub}:/usr/bin:/bin", "HOME": str(tmp_path), "XDG_CACHE_HOME": str(cache)}

    def go(root: Path, shell: str | None = None) -> tuple[str, str]:
        cmd = [shell, str(root / "bin" / "gantrel-mcp")] if shell else [str(root / "bin" / "gantrel-mcp")]
        out = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True).stdout.splitlines()
        return out[0], out[1]

    go.cache = cache
    return go


def checkout(where: Path) -> Path:
    (where / "bin").mkdir(parents=True)
    shutil.copy2(LAUNCHER, where / "bin" / "gantrel-mcp")
    return where


def test_each_checkout_gets_its_own_venv(run, tmp_path):
    a, b = checkout(tmp_path / "a"), checkout(tmp_path / "plugins" / "gantrel")
    venv_a, args_a = run(a)
    venv_b, args_b = run(b)
    assert venv_a != venv_b
    for venv in (venv_a, venv_b):
        assert Path(venv).parent == run.cache / "gantrel" and Path(venv).name.startswith("venv-")
    assert args_a == f"run --frozen --no-dev --project {a.resolve()} gantrel-mcp"
    assert args_b == f"run --frozen --no-dev --project {b.resolve()} gantrel-mcp"
    # Stable for one root, and a link to a checkout is the same checkout.
    assert run(a)[0] == venv_a
    os.symlink(a, tmp_path / "link")
    assert run(tmp_path / "link")[0] == venv_a


DASH = next((p for p in ("/bin/dash", "/usr/bin/dash") if os.access(p, os.X_OK)), None)


@pytest.mark.skipif(DASH is None, reason="no /bin/dash (CI's /bin/sh on Ubuntu)")
def test_the_launcher_runs_under_dash(run, tmp_path):
    a = checkout(tmp_path / "a")
    assert run(a, DASH) == run(a)
