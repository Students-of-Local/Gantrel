"""Watch a print: camera frames, status lines, the failure guard, a timelapse and notices.

    gantrel-printer watch <name> [--interval S] [--guard] [--timelapse] [--notify]

Each watch gets a job folder under the state dir (watch/<printer>/<UTC time>/):

    job.json        what was watched, how it ended, the timelapse and any guard pause
    status.jsonl    one status line per tick, with the frame and the guard's verdict
    frames/         000001.jpg, 000002.jpg, ... one every --interval seconds while a print runs
    guard/          the last frames the guard judged, each with its verdict as <frame>.json
    guard.jsonl     every verdict
    pause.json      written only when the guard paused the printer: the frames and reasons why
    timelapse.mp4   with --timelapse, when ffmpeg is installed

The watch can only read status, take frames and pause: it reaches the printer through PrinterHandle,
which has no start, resume or cancel. The guard pauses only after `consecutive` failed verdicts in a
row, only while the printer reports printing, and only after reading the status again just before.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .errors import PrinterError, Refused
from .model import Status

ACTIVE = ("printing", "paused")   # a print is under way
ENDED = {"complete": "finished", "error": "failed", "cancelled": "cancelled"}
JOB_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?$")  # job folder names


class PrinterHandle:
    """The only printer calls a watch can make. There is deliberately no start, resume or cancel."""

    __slots__ = ("_a",)

    def __init__(self, adapter):
        object.__setattr__(self, "_a", adapter)

    def __setattr__(self, *_):
        raise AttributeError("PrinterHandle is read-only")

    @property
    def name(self) -> str:
        return self._a.cfg.name

    @property
    def type(self) -> str:
        return self._a.cfg.type

    def watch(self, interval: float) -> Iterator[Status]:
        return self._a.watch(interval=interval)

    def status(self) -> Status:
        return self._a.status()

    def snapshot(self, out: Path) -> Path:
        return self._a.snapshot(out)

    def pause(self) -> None:
        self._a.pause()


@dataclass
class WatchOptions:
    interval: float = 30.0
    guard: bool = False
    timelapse: bool = False
    notify: bool = False
    start_timeout: float = 600.0  # how long to wait for the person to start the print
    duration: float | None = None  # stop after this many seconds, whatever the printer does
    any_state: bool = False       # take frames in any state (camera checks); the guard still judges only printing
    lost_after: int = 5           # status failures in a row before the watch gives up
    ring: int = 12                # frames kept with their verdicts in guard/


@dataclass
class Tick:
    n: int
    status: Status
    frame: Path | None = None
    verdict: object = None


@dataclass
class Result:
    job_dir: Path
    outcome: str = "running"
    frames: int = 0
    timelapse: str = ""
    timelapse_note: str = ""
    pause: dict = field(default_factory=dict)
    last_status: dict = field(default_factory=dict)
    job: dict = field(default_factory=dict)  # job.json as written; the guard adds each verdict to it


def now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def jobs_root(printer: str) -> Path:
    return config.state_dir() / "watch" / printer


def new_job_dir(printer: str) -> Path:
    root = jobs_root(printer)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d-%H%M%S")
    d, n = root / stamp, 1
    while d.exists():
        n += 1
        d = root / f"{stamp}-{n}"
    d.mkdir(mode=0o700)
    return d


class PrinterLock:
    """One watch per printer, so two guards never both pause it or both send notices."""

    def __init__(self, printer: str):
        self.path = jobs_root(printer) / ".lock"
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fh = open(self.path, "a+")
        try:
            import fcntl
        except ImportError:  # Windows: no lock, one watch at a time is up to the person
            return self
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            raise Refused(f"{self.path.parent.name} is already being watched",
                          f"See it with: gantrel-printer frames {self.path.parent.name}") from None
        return self

    def __exit__(self, *exc):
        if self.fh:
            self.fh.close()


def is_locked(printer: str) -> bool:
    path = jobs_root(printer) / ".lock"
    if not path.exists():
        return False
    try:
        import fcntl
    except ImportError:
        return False
    with open(path, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
    return False


def statuses(handle: PrinterHandle, interval: float, lost_after: int,
             sleep: Callable[[float], None] = time.sleep) -> Iterator[Status | None]:
    """adapter.watch(), restarted after a failed status read. Yields None once the printer is lost."""
    fails = 0
    while True:
        try:
            for st in handle.watch(interval):
                fails = 0
                yield st
            return
        except PrinterError:
            fails += 1
            if fails >= lost_after:
                yield None
                return
            sleep(interval)


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str) + "\n")
    os.replace(tmp, path)


def append_line(path: Path, data: dict) -> None:
    with open(path, "a") as fh:
        fh.write(json.dumps(data, default=str) + "\n")


def keep_ring(guard_dir: Path, frame: Path, verdict: dict, size: int) -> None:
    guard_dir.mkdir(exist_ok=True)
    shutil.copy2(frame, guard_dir / frame.name)
    write_json(guard_dir / (frame.name + ".json"), verdict)
    kept = sorted(p for p in guard_dir.iterdir() if p.suffix in (".jpg", ".png"))
    for old in kept[:-size] if size > 0 else kept:
        old.unlink(missing_ok=True)
        (guard_dir / (old.name + ".json")).unlink(missing_ok=True)


# ---------------------------------------------------------------- timelapse


def make_timelapse(job_dir: Path, fps: float | None = None) -> tuple[Path | None, str]:
    """Join the job's frames into timelapse.mp4 with ffmpeg. Returns (path or None, plain note)."""
    frames = sorted((job_dir / "frames").glob("*.jpg"))
    if not frames:
        return None, "no frames were saved, so there is no timelapse"
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None, ("ffmpeg is not installed, so no timelapse was made. Install ffmpeg, then run: "
                      f"gantrel-printer frames {job_dir.parent.name} --timelapse {job_dir.name}")
    fps = fps or max(1.0, min(30.0, round(len(frames) / 10)))
    listing = job_dir / "frames.txt"
    lines = []
    for f in frames:
        quoted = str(f.resolve()).replace("'", "'\\''")  # the concat list's own quoting
        lines += [f"file '{quoted}'", f"duration {1 / fps:.6f}"]
    listing.write_text("\n".join(lines) + "\n")
    encoders = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    codec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"] if "libx264" in encoders else ["-c:v", "mpeg4",
                                                                                                    "-q:v", "4"]
    out = job_dir / "timelapse.mp4"
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
           "-vf", f"fps={fps},scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p", *codec, "-movflags", "+faststart",
           str(out)]
    run = subprocess.run(cmd, capture_output=True, text=True)
    listing.unlink(missing_ok=True)
    if run.returncode != 0 or not out.is_file():
        return None, "ffmpeg could not make the timelapse: " + (run.stderr.strip().splitlines() or ["no output"])[-1]
    return out, f"timelapse of {len(frames)} frames at {fps:g} fps"


# ---------------------------------------------------------------- the watch


def run(adapter, opts: WatchOptions, *, guard=None, notifier=None, job_dir: Path | None = None,
        log: Callable[[str], None] = print, on_tick: Callable[[Tick], None] | None = None,
        sleep: Callable[[float], None] = time.sleep) -> Result:
    handle = PrinterHandle(adapter)
    with PrinterLock(handle.name):
        job_dir = job_dir or new_job_dir(handle.name)
        (job_dir / "frames").mkdir(parents=True, exist_ok=True)
        res = Result(job_dir)
        job = {"printer": handle.name, "type": handle.type, "started": now_iso(), "ended": "", "outcome": "running",
               "pid": os.getpid(), "interval_s": opts.interval, "guard": None, "timelapse": opts.timelapse,
               "notify": opts.notify, "frames": 0}
        if guard:
            job["guard"] = {"model": guard.model, "url": guard.url, "consecutive": guard.consecutive,
                            "ran_on": guard.ran_on(), "verdicts": []}
        res.job = job
        write_json(job_dir / "job.json", job)
        try:
            _loop(handle, opts, guard, notifier, res, log, on_tick, sleep)
        except KeyboardInterrupt:
            res.outcome = "stopped"
        finally:
            job.update(ended=now_iso(), outcome=res.outcome, frames=res.frames, last_status=res.last_status,
                       pause=res.pause or None)
            write_json(job_dir / "job.json", job)
        if opts.timelapse:
            path, res.timelapse_note = make_timelapse(job_dir)
            res.timelapse = str(path) if path else ""
            log(res.timelapse_note)
            job.update(timelapse=res.timelapse or False, timelapse_note=res.timelapse_note)
            write_json(job_dir / "job.json", job)
        if notifier and res.outcome in ("finished", "ended", "failed", "cancelled", "paused_by_guard", "lost"):
            frames = sorted((job_dir / "frames").glob("*.jpg"))
            notifier.try_send(res.outcome, handle.name, notice_text(res), frame=frames[-1] if frames else None,
                              file=res.last_status.get("file", ""), log=log)
        return res


def notice_text(res: Result) -> str:
    st = res.last_status
    bits = [{"finished": "The print finished.", "failed": "The printer reports an error.",
             "cancelled": "The print was cancelled.", "lost": "The printer stopped answering.",
             "paused_by_guard": "The failure guard paused the print. Only you can resume it, on the printer."}
            .get(res.outcome, res.outcome)]
    if st.get("file"):
        bits.append(f"File: {st['file']}.")
    if res.pause:
        bits.append(f"Why: {res.pause.get('reason', '')}")
    bits.append(f"Frames: {res.job_dir}")
    return " ".join(bits)


def _loop(handle, opts, guard, notifier, res, log, on_tick, sleep):
    t0 = time.monotonic()
    seen_active = False
    streak: list[dict] = []
    pause_failed_sent = False
    n = offline = 0
    last_progress = 0.0
    for st in statuses(handle, opts.interval, opts.lost_after, sleep):
        if opts.duration is not None and time.monotonic() - t0 > opts.duration:
            res.outcome = "time_up"
            log(f"--duration {opts.duration:g} s is up; the watch ends")
            return
        if st is None:
            res.outcome = "lost" if seen_active else "unreachable"
            log(f"{handle.name} stopped answering")
            return
        res.last_status = st.to_dict()
        line = {"time": now_iso(), **st.to_dict()}
        offline = offline + 1 if st.state == "offline" else 0
        if offline >= opts.lost_after:
            append_line(res.job_dir / "status.jsonl", line)
            res.outcome = "lost" if seen_active else "unreachable"
            log(f"{handle.name} is offline")
            return
        # Once a print is under way, busy (homing, a macro) and a passing offline don't end the watch.
        active = st.state in ACTIVE or (seen_active and st.state in ("busy", "offline")) or opts.any_state
        if not active:
            append_line(res.job_dir / "status.jsonl", line)
            if seen_active:
                # Some printers (Duet, OctoPrint, Repetier, Marlin) go back to idle after a print, finished
                # or not; only a last progress near 100% says it finished.
                res.outcome = ENDED.get(st.state) or ("finished" if last_progress >= 0.98 else "ended")
                log(f"{handle.name} is {st.state}; the watch ends")
                return
            if time.monotonic() - t0 > opts.start_timeout:
                res.outcome = "not_started"
                log(f"{handle.name} did not start a print within {opts.start_timeout:.0f} s")
                return
            log(f"{handle.name} is {st.state}; waiting for the print to start")
            continue
        seen_active = seen_active or st.state in ACTIVE
        if st.state == "offline":
            append_line(res.job_dir / "status.jsonl", line)
            continue
        if st.progress is not None:
            last_progress = st.progress
        n += 1
        frame = None
        try:
            frame = handle.snapshot(res.job_dir / "frames" / f"{res.frames + 1:06d}.jpg")
            res.frames += 1
            line["frame"] = f"frames/{frame.name}"
        except PrinterError as err:
            line["frame_error"] = str(err)
            log(f"no frame: {err}")
        verdict = None
        if guard and frame is not None and st.state == "printing":
            verdict = guard.classify(frame, st).to_dict()
            verdict.update(frame=f"frames/{frame.name}", time=line["time"])
            line["verdict"] = verdict
            append_line(res.job_dir / "guard.jsonl", verdict)
            _record_verdict(res, verdict)
            keep_ring(res.job_dir / "guard", frame, verdict, opts.ring)
            streak = streak + [verdict] if verdict["verdict"] == "failed" else []
        elif st.state != "printing":
            streak = []  # a person paused it; only failures while printing count
        append_line(res.job_dir / "status.jsonl", line)
        log(_tick_text(st, frame, verdict))
        if on_tick:
            on_tick(Tick(n, st, frame, verdict))
        if guard and len(streak) >= guard.consecutive:
            try:
                now = handle.status()
            except PrinterError as err:
                log(f"could not re-read status before pausing: {err}")
                continue
            if now.state != "printing":
                streak = []
                continue
            try:
                handle.pause()
            except PrinterError as err:
                log(f"the guard could not pause {handle.name}: {err}")
                if notifier and not pause_failed_sent:
                    frames = sorted((res.job_dir / "frames").glob("*.jpg"))
                    notifier.try_send("pause_failed", handle.name,
                                      f"The failure guard saw a failed print but the pause did not work ({err}). "
                                      f"Check the printer now. Why: {streak[-1].get('reason', '')}",
                                      frame=frames[-1] if frames else None, log=log)
                    pause_failed_sent = True
                continue
            res.pause = {"time": now_iso(), "reason": streak[-1].get("reason", ""), "frames": streak,
                         "model": guard.model, "ran_on": streak[-1].get("ran_on") or guard.ran_on(),
                         "consecutive": guard.consecutive}
            write_json(res.job_dir / "pause.json", res.pause)
            res.outcome = "paused_by_guard"
            log(f"PAUSED {handle.name}: {res.pause['reason']}")
            return


def _record_verdict(res: Result, verdict: dict, keep: int = 50) -> None:
    """The last `keep` verdicts, each with where it ran, in job.json next to the guard settings."""
    g = res.job.get("guard")
    if not isinstance(g, dict):
        return
    keys = ("time", "frame", "verdict", "reason", "source", "latency_s", "ran_on")
    g["verdicts"] = g.get("verdicts", [])[-(keep - 1):] + [{k: verdict.get(k) for k in keys}]
    write_json(res.job_dir / "job.json", res.job)


def _tick_text(st: Status, frame: Path | None, verdict: dict | None) -> str:
    t = datetime.datetime.now().strftime("%H:%M:%S")
    bits = [t, st.state]
    if st.progress is not None:
        bits.append(f"{st.progress * 100:.0f}%")
    if st.layer is not None and st.total_layers:
        bits.append(f"layer {st.layer}/{st.total_layers}")
    bits.append(f"frame {frame.name}" if frame else "no frame")
    if verdict:
        bits.append(f"guard {verdict['verdict']}" + (f" ({verdict['reason']})" if verdict.get("reason") else ""))
        ran = verdict.get("ran_on") or {}
        if ran:
            bits.append(f"[ran on {ran.get('via', '?')} at {ran.get('host', '?')}, model "
                        f"{(ran.get('served') or {}).get('model') or ran.get('model', '?')}]")
    return " ".join(bits)

