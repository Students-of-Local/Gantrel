"""gantrel-solid: SolidCraft's headless CLI as Gantrel's third parametric path.

doctor finds solidcraft-cli and says how to get the pinned release; fetch downloads it only with --yes and
only when its SHA256 matches the pin. Nothing here installs system software or touches a printer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from . import pin

CLI = "solidcraft-cli"
VERSION_TIMEOUT = 30
NET_TIMEOUT = 60


class Refused(Exception):
    pass


def emit(data: dict) -> None:
    print(json.dumps(data), flush=True)


def target() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    return (system, "universal") if system == "darwin" else (system, machine)


def data_dir() -> Path:
    return Path.home() / ".local" / "share" / "gantrel" / "solidcraft" / pin.VERSION


def find() -> str | None:
    """GANTREL_SOLIDCRAFT, then solidcraft-cli on PATH, then the folder fetch fills."""
    env = os.environ.get("GANTREL_SOLIDCRAFT")
    if env:
        return env if Path(env).is_file() else shutil.which(env)
    found = shutil.which(CLI)
    if found:
        return found
    own = data_dir() / CLI
    return str(own) if own.is_file() else None


def version_of(exe: str) -> str | None:
    try:
        p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=VERSION_TIMEOUT,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.match(r"solidcraft-cli (\S+)", p.stdout.strip())
    return m.group(1) if p.returncode == 0 and m else None


def install_steps() -> dict:
    t = target()
    if t not in pin.FILES:
        return {"url": f"{pin.RELEASE}/{pin.SUMS}", "sha256": None,
                "steps": [f"SolidCraft {pin.VERSION} has no build Gantrel knows for {t[0]} {t[1]}"]}
    name, sha = pin.FILES[t]
    url = f"{pin.RELEASE}/{name}"
    if t[0] == "linux":
        steps = ["gantrel-solid fetch --yes", f"or by hand: download {url}",
                 f"check its SHA256 is {sha}",
                 f"unpack bin/{CLI} from it into {data_dir()}"]
    else:
        steps = [f"download {url}", f"check its SHA256 is {sha}",
                 f"put {CLI} from it in {data_dir()}, or set GANTREL_SOLIDCRAFT to its path"]
    return {"url": url, "sha256": sha, "steps": steps}


def doctor(args) -> int:
    exe = find()
    version = version_of(exe) if exe else None
    out = {"ok": version is not None, "path": exe, "version": version, "pinned": version == pin.VERSION}
    if version is None:
        out["install"] = install_steps()
    if args.json:
        emit(out)
    elif version:
        print(f"{CLI} {version} at {exe}" + ("" if out["pinned"] else f" (Gantrel is tested with {pin.VERSION})"))
    else:
        print(f"{CLI} not found" if not exe else f"{exe} did not answer --version")
        print("\n".join(out["install"]["steps"]))
    return 0 if out["ok"] else 1


def release_base() -> str:
    # Tests point this at a stub on 127.0.0.1; the pinned SHA256 is checked whatever it serves.
    return os.environ.get("GANTREL_SOLIDCRAFT_RELEASE", pin.RELEASE).rstrip("/")


def download(url: str, dest: Path) -> str:
    h = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=NET_TIMEOUT) as r, dest.open("wb") as f:
        while chunk := r.read(1 << 20):
            h.update(chunk)
            f.write(chunk)
    return h.hexdigest()


def release_sum(name: str) -> str | None:
    with urllib.request.urlopen(f"{release_base()}/{pin.SUMS}", timeout=NET_TIMEOUT) as r:
        text = r.read(1 << 20).decode("utf-8", "replace")
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return parts[0].lower()
    return None


def unpack_cli(tarball: Path, dest: Path) -> None:
    with tarfile.open(tarball, "r:gz") as tf:
        member = next((m for m in tf if m.isfile() and m.name.endswith(f"/bin/{CLI}")
                       and ".." not in m.name.split("/")), None)
        if member is None:
            raise Refused(f"the release file has no bin/{CLI}")
        src = tf.extractfile(member)
        with dest.open("wb") as f:
            shutil.copyfileobj(src, f)
    dest.chmod(0o755)


def fetch(args) -> int:
    t = target()
    if t[0] != "linux" or t not in pin.FILES:
        emit({"ok": False, "error": f"fetch unpacks the Linux tarballs only; on {t[0]} install by hand",
              "install": install_steps()})
        return 1
    name, sha = pin.FILES[t]
    if not args.yes:
        emit({"ok": False, "error": "nothing downloaded: run gantrel-solid fetch --yes to download it",
              "url": f"{release_base()}/{name}", "sha256": sha})
        return 2
    final = data_dir()
    made = next((p for p in reversed((final, *final.parents)) if not p.exists()), None)
    final.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".fetch-", dir=final))
    try:
        listed = release_sum(name)
        if listed != sha:
            raise Refused(f"the release's {pin.SUMS} lists {listed} for {name}, not the pinned {sha}")
        got = download(f"{release_base()}/{name}", work / name)
        if got != sha:
            raise Refused(f"{name} has SHA256 {got}, not the pinned {sha}")
        unpack_cli(work / name, work / CLI)
        version = version_of(str(work / CLI))
        if version != pin.VERSION:
            raise Refused(f"the unpacked {CLI} reports version {version}, not {pin.VERSION}")
        os.replace(work / CLI, final / CLI)
    except (Refused, OSError, tarfile.TarError) as e:
        shutil.rmtree(work, ignore_errors=True)
        if made:
            shutil.rmtree(made, ignore_errors=True)
        emit({"ok": False, "error": str(e)})
        return 1
    shutil.rmtree(work)
    emit({"ok": True, "path": str(final / CLI), "version": pin.VERSION, "sha256": sha})
    return 0


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gantrel-solid", description="Parametric parts from SolidCraft, checked for printing.")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor", help=f"find {CLI} and say how to get SolidCraft {pin.VERSION}")
    d.add_argument("--json", action="store_true")
    f = sub.add_parser("fetch", help=f"download SolidCraft {pin.VERSION}'s CLI into {data_dir()}")
    f.add_argument("--yes", action="store_true", help="the person agreed to the download")
    b = sub.add_parser("build", help="build a design into the job folder and check it prints")
    b.add_argument("design", nargs="?", help="design JSON (default <job>/design.json)")
    b.add_argument("--job", default="./solid-job", help="job folder inside the working folder (default ./solid-job)")
    m = sub.add_parser("measure", help="bounding box (mm) and volume of a STEP, STL or 3MF")
    m.add_argument("file")
    return p


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    if args.cmd in ("build", "measure"):
        from . import build
        return getattr(build, args.cmd)(args)
    return {"doctor": doctor, "fetch": fetch}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
