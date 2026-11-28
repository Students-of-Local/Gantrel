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
if TARGET not in pin.FILES:
    pytest.skip(f"SolidCraft {pin.VERSION} has no build for {TARGET[0]} {TARGET[1]}", allow_module_level=True)


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


def test_a_failed_fetch_removes_only_the_folders_it_made(run, release):
    name = serve(release, tarball("#!/bin/sh\necho solidcraft-cli 0.50.0\n"), pin.FILES[TARGET][1])
    keep = run.home / ".local" / "bin" / "keep"

    def other_program(path):
        if path == f"/{name}":
            keep.parent.mkdir(parents=True)
            keep.write_text("not fetch's")

    release.before = other_program
    run.env["GANTREL_SOLIDCRAFT_RELEASE"] = release.url
    code, out = run("fetch", "--yes")
    assert code == 1 and out["ok"] is False and "not the pinned" in out["error"]
    assert sorted(p.relative_to(run.home).as_posix() for p in installed(run.home)) == [
        ".local", ".local/bin", ".local/bin/keep"]


@pytest.mark.parametrize("how", ["bad-url", "cut-sums", "cut-tarball"])
def test_a_broken_fetch_prints_its_error_and_leaves_nothing(run, release, how):
    name = serve(release, tarball("#!/bin/sh\necho solidcraft-cli 0.50.0\n"), pin.FILES[TARGET][1])
    release.cut = {"cut-sums": {f"/{pin.SUMS}"}, "cut-tarball": {f"/{name}"}}.get(how, set())
    run.env["GANTREL_SOLIDCRAFT_RELEASE"] = "not-a-url" if how == "bad-url" else release.url
    code, out = run("fetch", "--yes")
    assert code == 1 and out["ok"] is False and out["error"]
    assert installed(run.home) == []


def fake_cli(path, version):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho solidcraft-cli {version}\n")
    path.chmod(0o755)
    return path


@pytest.mark.parametrize("on_path", ["0.49.0", pin.VERSION])
def test_doctor_uses_the_pinned_copy_unless_path_has_the_pinned_version(run, tmp_path, on_path):
    other = fake_cli(tmp_path / "bin" / "solidcraft-cli", on_path)
    own = fake_cli(run.home / ".local/share/gantrel/solidcraft" / pin.VERSION / "solidcraft-cli", pin.VERSION)
    run.env["PATH"] = f"{other.parent}:/usr/bin:/bin"
    code, out = run("doctor", "--json")
    want = other if on_path == pin.VERSION else own
    assert code == 0 and out == {"ok": True, "path": str(want), "version": pin.VERSION, "pinned": True}


def test_on_windows_doctor_names_and_finds_the_exe(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "target", lambda: ("windows", "x86_64"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.delenv("GANTREL_SOLIDCRAFT", raising=False)
    assert any("solidcraft-cli.exe" in s for s in cli.install_steps()["steps"])
    exe = cli.data_dir() / "solidcraft-cli.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert cli.find() == str(exe)


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
    except urllib.error.HTTPError as e:
        pytest.fail(f"the release's {pin.SUMS} answered {e.code}: {e.reason}")
    except (urllib.error.URLError, TimeoutError) as e:
        pytest.skip(f"offline, could not read the release's {pin.SUMS}: {e}")
    sums = {parts[1].lstrip("*"): parts[0].lower() for parts in map(str.split, text.splitlines()) if len(parts) == 2}
    assert {name: sums.get(name) for name, _ in pin.FILES.values()} == dict(pin.FILES.values())
