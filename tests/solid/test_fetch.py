"""gantrel-solid doctor and fetch: the pinned release, the --yes gate and the hash checks."""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
import urllib.error
import urllib.request

import pytest

from gantrel_solid import cli, pin

TARGET = cli.target()


def installed(home):
    return [p for p in home.rglob("*")]


def test_doctor_with_nothing_installed_names_the_pinned_file(run, tmp_path):
    empty = tmp_path / "empty-path"
    empty.mkdir()
    run.env["PATH"] = str(empty)
    code, out = run("doctor", "--json")
    assert code == 1 and out["ok"] is False and out["path"] is None and out["pinned"] is False
    name, sha = pin.FILES[TARGET]
    assert out["install"]["url"] == f"{pin.RELEASE}/{name}" and out["install"]["sha256"] == sha
    assert any(sha in s for s in out["install"]["steps"])
    assert installed(run.home) == []


def tarball(cli_text: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = cli_text.encode()
        info = tarfile.TarInfo(f"solidcraft-{pin.VERSION}-x/bin/solidcraft-cli")
        info.size, info.mode = len(data), 0o755
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def serve(release, body: bytes, listed: str):
    name, _ = pin.FILES[TARGET]
    (release.root / name).write_bytes(body)
    (release.root / pin.SUMS).write_text(f"{listed}  {name}\n")
    return name


def test_fetch_without_yes_downloads_nothing(run, release):
    serve(release, tarball("x"), pin.FILES[TARGET][1])
    run.env["GANTREL_SOLIDCRAFT_RELEASE"] = release.url
    code, out = run("fetch")
    assert code == 2 and out["ok"] is False and "--yes" in out["error"]
    assert release.requests == [] and installed(run.home) == []


@pytest.mark.parametrize("listed", ["pin", "tampered"])
def test_fetch_refuses_a_file_that_is_not_the_pinned_one(run, release, listed):
    body = tarball("#!/bin/sh\necho solidcraft-cli 0.50.0\n")
    sha = pin.FILES[TARGET][1] if listed == "pin" else hashlib.sha256(body).hexdigest()
    name = serve(release, body, sha)
    run.env["GANTREL_SOLIDCRAFT_RELEASE"] = release.url
    code, out = run("fetch", "--yes")
    assert code == 1 and out["ok"] is False and pin.FILES[TARGET][1] in out["error"]
    assert installed(run.home) == []
    assert (f"/{name}" in release.requests) == (listed == "pin")


def test_fetch_installs_the_pinned_cli(run, release):
    real = os.environ.get("GANTREL_SOLID_TARBALL")
    if not real or not os.path.isfile(real):
        pytest.skip(f"GANTREL_SOLID_TARBALL does not name a copy of {pin.FILES[TARGET][0]}")
    serve(release, open(real, "rb").read(), pin.FILES[TARGET][1])
    run.env["GANTREL_SOLIDCRAFT_RELEASE"] = release.url
    code, out = run("fetch", "--yes")
    want = run.home / ".local/share/gantrel/solidcraft" / pin.VERSION / "solidcraft-cli"
    assert code == 0 and out["ok"] and out["path"] == str(want) and out["version"] == pin.VERSION
    assert sorted(p.name for p in want.parent.iterdir()) == ["solidcraft-cli"]
    run.env["PATH"] = str(run.home)
    code, out = run("doctor", "--json")
    assert code == 0 and out == {"ok": True, "path": str(want), "version": pin.VERSION, "pinned": True}


def test_pinned_hashes_match_the_release_sums():
    try:
        with urllib.request.urlopen(f"{pin.RELEASE}/{pin.SUMS}", timeout=60) as r:
            text = r.read().decode()
    except (urllib.error.URLError, TimeoutError) as e:
        pytest.skip(f"offline, could not read the release's {pin.SUMS}: {e}")
    sums = {name.lstrip("*"): sha for sha, name in (line.split() for line in text.splitlines() if line.strip())}
    assert {name: sums.get(name) for name, _ in pin.FILES.values()} == dict(pin.FILES.values())
