"""gantrel-studio: serve the 3D viewport for one smoothstone job on this computer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import server

DETACH_WAIT = 15.0


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gantrel-studio", description="Local 3D viewport for a smoothstone job.")
    p.add_argument("--job", default="./stone-job", help="job folder (default ./stone-job)")
    p.add_argument("--port", type=int, default=7342, help="first port to try; the next 10 are tried if it is taken")
    p.add_argument("--host", default="127.0.0.1", help="address to bind (default 127.0.0.1)")
    p.add_argument("--no-open", action="store_true", help="do not open a browser")
    p.add_argument("--detach", action="store_true", help="run in the background, print the link and return at once; reuses a studio already running for this job")
    p.add_argument("--stop", action="store_true", help="stop the background studio for this job")
    p.add_argument("--state-file", help=argparse.SUPPRESS)
    return p


def state_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "gantrel-studio"


def state_path(job: Path) -> Path:
    key = hashlib.sha256(str(job.resolve()).encode()).hexdigest()[:16]
    return state_dir() / f"{key}.json"


def url_for(port: int, launch_id: str) -> str:
    return f"http://127.0.0.1:{port}/?t={launch_id}"


def read_state(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and {"pid", "port", "launch_id", "job"} <= set(data) else None


def write_state(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def alive(data: dict, job: Path) -> bool:
    if data.get("job") != str(job.resolve()):
        return False
    try:
        os.kill(int(data["pid"]), 0)
    except (OSError, ValueError):
        return False
    req = urllib.request.Request(
        f"http://127.0.0.1:{int(data['port'])}/api/state",
        headers={"X-Gantrel-Token": str(data["launch_id"])},
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except OSError:
        return False


def detach(args: argparse.Namespace, job: Path) -> int:
    path = state_path(job)
    data = read_state(path)
    if data and alive(data, job):
        print(f"Gantrel Studio: {url_for(int(data['port']), str(data['launch_id']))}", flush=True)
        return 0
    path.unlink(missing_ok=True)
    state_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    log = state_dir() / f"{path.stem}.log"
    cmd = [
        sys.executable, "-m", "gantrel_studio.cli",
        "--job", str(job.resolve()), "--port", str(args.port), "--host", args.host,
        "--no-open", "--state-file", str(path),
    ]
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "ab") as out:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True, close_fds=True)
    deadline = time.monotonic() + DETACH_WAIT
    while time.monotonic() < deadline:
        data = read_state(path)
        if data and data.get("pid") == proc.pid:
            print(f"Gantrel Studio: {url_for(int(data['port']), str(data['launch_id']))}", flush=True)
            return 0
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    print(f"gantrel-studio: the background studio did not start; see {log}", file=sys.stderr)
    return 1


def stop(job: Path) -> int:
    path = state_path(job)
    data = read_state(path)
    if not data or not alive(data, job):
        path.unlink(missing_ok=True)
        print("No studio is running for this job.")
        return 0
    os.kill(int(data["pid"]), signal.SIGTERM)
    for _ in range(50):
        if not path.exists():
            break
        time.sleep(0.1)
    print("Stopped the studio for this job.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    job = Path(args.job)
    if args.stop:
        return stop(job)
    if args.detach:
        return detach(args, job)
    try:
        httpd = server.bind(job, host=args.host, port=args.port)
    except OSError as e:
        print(f"gantrel-studio: {e}", file=sys.stderr)
        return 1
    httpd.studio.start()
    url = url_for(httpd.port, httpd.studio.launch_id)
    state = Path(args.state_file) if args.state_file else None
    if state:
        write_state(state, {"pid": os.getpid(), "port": httpd.port, "launch_id": httpd.studio.launch_id, "job": str(job.resolve())})
        signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=httpd.shutdown, daemon=True).start())
    else:
        print(f"Gantrel Studio: {url}", flush=True)
    if not args.no_open:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        httpd.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if state:
            current = read_state(state)
            if current and current.get("pid") == os.getpid():
                state.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
