"""Watching a print: frames and status for every adapter, the failure guard, timelapse and notices.

Every guard test runs against the fakes: a fake printer, a fake camera serving the labeled frames
in tests/printers/frames, and a fake OpenAI-compatible vision server answering from a script.
"""

from __future__ import annotations

import base64
import io
import json
import shutil
import socket
import subprocess
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from gantrel_printer import cli, config, guard as vguard, monitor, notify
from gantrel_printer.adapters.base import Adapter
from gantrel_printer.config import PrinterConfig
from gantrel_printer.errors import ConfigError, Refused

import fakes
from test_contract import RIG_KINDS, make_rig, start_print, upload_job

FRAMES = Path(__file__).parent / "frames"
GOOD = sorted((FRAMES / "good").glob("good-*.jpg"))
FAILED = sorted((FRAMES / "failed").glob("spaghetti-*.jpg"))
FAILED_JSON = '{"verdict": "failed", "reason": "spaghetti on the bed"}'
OK_JSON = '{"verdict": "ok", "reason": "the part looks normal"}'
SUSPECT_JSON = '{"verdict": "suspect", "reason": "too dark to tell"}'
IDLE_AFTER_PRINT = {"duet", "duet-dsf", "octoprint", "repetier", "serial"}
FORBIDDEN = ("start", "_start", "resume", "_resume", "cancel", "emergency_stop")


@pytest.fixture
def forbid(monkeypatch):
    """forbid() makes any later start, resume, cancel or emergency stop on any adapter fail the test.

    Call it after the test's own setup, which starts the fake print the app's way."""
    calls = []

    def arm():
        for name in FORBIDDEN:
            def boom(self, *a, _n=name, **k):
                calls.append(_n)
                raise AssertionError(f"the watch called {_n}")
            for cls in [Adapter, *Adapter.__subclasses__()]:
                if name in vars(cls):
                    monkeypatch.setattr(cls, name, boom)
        return calls
    return arm


def opts(**kw):
    return monitor.WatchOptions(**{"interval": 0.05, "start_timeout": 5, **kw})


def stop_after(brain, n, state="complete"):
    """on_tick that ends the print (as the printer would) once n frames were taken."""
    def tick(t):
        if t.n >= n:
            with brain.lock:
                brain.state = state
    return tick


def printing_rig(kind, tls_cert, tmp_path):
    rig = make_rig(kind, tls_cert)
    job = upload_job(rig, tmp_path)
    start_print(rig, rig.adapter(), job)
    assert rig.adapter().status().state == "printing"
    rig.brain.events.clear()
    return rig


@pytest.fixture
def moon(tmp_path, tls_cert):
    """A printing Moonraker fake whose camera serves the labeled frames (spaghetti after 2 good)."""
    cam = fakes.FakeFrameCamera(GOOD[:2] + FAILED)
    rig = make_rig("moonraker", tls_cert)
    rig.cfg.camera_url = cam.url
    rig.closers.append(cam.close)
    job = upload_job(rig, tmp_path)
    start_print(rig, rig.adapter(), job)
    rig.brain.events.clear()
    rig.cam = cam
    yield rig
    for c in rig.closers:
        c()


def vision(replies, **kw):
    k = kw.pop("consecutive", 2)
    v = fakes.FakeVision(replies, **kw)
    return v, vguard.VisionGuard(v.url, "fake-vl", timeout=10, consecutive=k, key=v.key)


def run_watch(rig, *, guard=None, notifier=None, n_stop=None, on_tick=None, **kw):
    tick = on_tick or (stop_after(rig.brain, n_stop) if n_stop else None)
    lines = []
    res = monitor.run(rig.adapter(), opts(guard=bool(guard), **kw), guard=guard, notifier=notifier,
                      log=lines.append, on_tick=tick)
    res.lines = lines
    return res


def read_lines(path):
    return [json.loads(x) for x in path.read_text().splitlines()]


# ---------------------------------------------------------------- frames and status, every adapter


@pytest.mark.parametrize("kind", RIG_KINDS)
def test_watch_saves_frames_and_status_lines_for_every_adapter(kind, tls_cert, tmp_path, forbid):
    rig = printing_rig(kind, tls_cert, tmp_path)
    calls = forbid()
    try:
        res = run_watch(rig, n_stop=3)
    finally:
        for c in rig.closers:
            c()
    # Printers that go back to idle after a print can't say whether it finished (fake progress is 10%).
    assert res.outcome == ("ended" if kind in IDLE_AFTER_PRINT else "finished")
    assert res.frames == 3
    frames = sorted((res.job_dir / "frames").iterdir())
    assert [f.name for f in frames] == ["000001.jpg", "000002.jpg", "000003.jpg"]
    assert all(f.read_bytes()[:3] == b"\xff\xd8\xff" for f in frames)
    lines = read_lines(res.job_dir / "status.jsonl")
    assert [x["state"] for x in lines] == ["printing"] * 3 + ["idle" if kind in IDLE_AFTER_PRINT else "complete"]
    assert [x.get("frame") for x in lines[:3]] == ["frames/000001.jpg", "frames/000002.jpg", "frames/000003.jpg"]
    job = json.loads((res.job_dir / "job.json").read_text())
    assert job["outcome"] == res.outcome and job["frames"] == 3 and job["printer"] == rig.cfg.name
    assert res.job_dir.parent == config.state_dir() / "watch" / rig.cfg.name
    assert rig.brain.events == []  # nothing was sent to the printer
    assert calls == []


def test_watch_waits_for_the_print_to_start_then_gives_up(tls_cert):
    rig = make_rig("moonraker", tls_cert)
    try:
        res = monitor.run(rig.adapter(), opts(start_timeout=0.3), log=lambda _: None)
    finally:
        for c in rig.closers:
            c()
    assert res.outcome == "not_started" and res.frames == 0


def test_any_state_takes_frames_of_an_idle_printer_until_the_duration_is_up(tls_cert, forbid):
    rig = make_rig("moonraker", tls_cert)
    cam = fakes.FakeFrameCamera(FAILED)
    rig.cfg.camera_url = cam.url
    rig.closers.append(cam.close)
    calls = forbid()
    v, g = vision([FAILED_JSON])
    try:
        res = monitor.run(rig.adapter(), opts(any_state=True, duration=0.5, guard=True), guard=g,
                          log=lambda _: None)
    finally:
        v.close()
        for c in rig.closers:
            c()
    assert res.outcome == "time_up" and res.frames >= 2
    assert v.requests == []  # the guard judges only frames taken while printing
    assert rig.brain.events == [] and calls == []


def test_watch_ends_as_lost_when_the_printer_stops_answering(moon):
    stopped = []

    def tick(t):
        if t.n == 2 and not stopped:
            for c in moon.closers:
                c()
            stopped.append(1)

    res = monitor.run(moon.adapter(), opts(lost_after=3), log=lambda _: None, on_tick=tick)
    moon.closers.clear()
    assert res.outcome == "lost" and res.frames == 2


def test_a_second_watch_on_the_same_printer_is_refused(moon):
    with monitor.PrinterLock(moon.cfg.name):
        with pytest.raises(Refused, match="already being watched"):
            monitor.run(moon.adapter(), opts(), log=lambda _: None)


# ---------------------------------------------------------------- the guard


def test_guard_pauses_after_k_failed_frames_and_keeps_why(moon, forbid):
    calls = forbid()
    v, g = vision([FAILED_JSON])
    try:
        res = run_watch(moon, guard=g)
    finally:
        v.close()
    assert res.outcome == "paused_by_guard"
    assert moon.brain.state == "paused"
    assert moon.brain.events == ["pause"]  # exactly one pause; no start, resume or cancel
    assert calls == []
    assert len(v.requests) == 2 and res.frames == 2
    assert v.images()[0] == (res.job_dir / "frames" / "000001.jpg").read_bytes()
    pause = json.loads((res.job_dir / "pause.json").read_text())
    assert pause["reason"] == "spaghetti on the bed" and pause["consecutive"] == 2
    assert [f["frame"] for f in pause["frames"]] == ["frames/000001.jpg", "frames/000002.jpg"]
    ring = sorted(p.name for p in (res.job_dir / "guard").iterdir())
    assert ring == ["000001.jpg", "000001.jpg.json", "000002.jpg", "000002.jpg.json"]
    assert json.loads((res.job_dir / "guard" / "000002.jpg.json").read_text())["verdict"] == "failed"
    assert json.loads((res.job_dir / "job.json").read_text())["outcome"] == "paused_by_guard"


def test_guard_needs_k_in_a_row(moon):
    v, g = vision([FAILED_JSON, FAILED_JSON, OK_JSON, FAILED_JSON, SUSPECT_JSON, FAILED_JSON, FAILED_JSON,
                   FAILED_JSON], consecutive=3)
    try:
        res = run_watch(moon, guard=g)
    finally:
        v.close()
    assert res.outcome == "paused_by_guard"
    assert len(v.requests) == 8  # the run of three ends on frame 8
    assert moon.brain.events == ["pause"]


@pytest.mark.parametrize("reply", [
    SUSPECT_JSON,
    "The print has failed: spaghetti everywhere.",            # prose, no JSON
    "failed",
    '"failed"',
    '["failed"]',
    '{"verdict": "FAILED!", "reason": "x"}',                  # unknown verdict
    '{"verdict": ["failed"]}',
    '{"verdict": "failed", "reason": "spaghetti"',            # cut off
    '{"status": "failed"}',
    "",
])
def test_guard_never_pauses_on_suspect_or_malformed_output(moon, reply, forbid):
    calls = forbid()
    v, g = vision([reply])
    try:
        res = run_watch(moon, guard=g, n_stop=4)
    finally:
        v.close()
    assert res.outcome == "finished"
    assert len(v.requests) == 4
    assert moon.brain.events == [] and calls == []
    verdicts = [json.loads(x)["verdict"] for x in (res.job_dir / "guard.jsonl").read_text().splitlines()]
    assert verdicts == ["suspect"] * 4


def test_unreachable_vision_model_is_suspect_and_never_pauses(moon):
    g = vguard.VisionGuard("http://127.0.0.1:9/v1", "none", timeout=1)
    res = run_watch(moon, guard=g, n_stop=3)
    assert res.outcome == "finished" and moon.brain.events == []
    v = [json.loads(x) for x in (res.job_dir / "guard.jsonl").read_text().splitlines()]
    assert {x["verdict"] for x in v} == {"suspect"} and {x["source"] for x in v} == {"unreachable"}


def test_guard_ignores_frames_while_a_person_has_it_paused(moon, forbid):
    calls = forbid()
    moon.brain.act("pause")  # the person paused it at the printer
    moon.brain.events.clear()
    v, g = vision([FAILED_JSON])
    try:
        res = run_watch(moon, guard=g, n_stop=4)
    finally:
        v.close()
    assert res.outcome == "finished" and res.frames == 4
    assert v.requests == []  # paused frames are saved but never judged
    assert moon.brain.events == [] and calls == []


def test_guard_reads_status_again_and_does_not_pause_a_print_that_just_ended(moon):
    v, g = vision([FAILED_JSON])
    try:
        res = run_watch(moon, guard=g, n_stop=2)  # the print completes right after the 2nd failed frame
    finally:
        v.close()
    assert res.outcome == "finished"
    assert moon.brain.events == []


def test_guard_retries_without_json_mode_for_servers_that_reject_it(moon):
    v, g = vision([FAILED_JSON], reject_json_mode=True)
    try:
        res = run_watch(moon, guard=g)
    finally:
        v.close()
    assert res.outcome == "paused_by_guard"
    assert all("response_format" not in r for r in v.requests)


def test_guard_sends_the_status_and_the_failure_list(moon):
    v, g = vision([OK_JSON])
    try:
        run_watch(moon, guard=g, n_stop=1)
    finally:
        v.close()
    text = v.requests[0]["messages"][0]["content"][0]["text"]
    assert "The printer reports: printing, 10% done" in text
    for word in ("spaghetti", "nozzle", "layer shift", "empty bed", '"verdict"'):
        assert word in text
    assert v.requests[0]["model"] == "fake-vl" and v.requests[0]["temperature"] == 0


def test_the_watch_has_no_way_to_start_resume_or_cancel():
    h = monitor.PrinterHandle(object())
    for name in FORBIDDEN:
        assert not hasattr(h, name)
    with pytest.raises(AttributeError):
        h._a = None
    src = "\n".join((Path(monitor.__file__).parent / f).read_text() for f in ("monitor.py", "guard.py", "notify.py"))
    for call in (".start(", "._start(", ".resume(", "._resume(", ".cancel(", ".emergency_stop("):
        assert call not in src


@pytest.mark.parametrize("text,want", [
    (FAILED_JSON, ("failed", "spaghetti on the bed", "model")),
    ('```json\n{"verdict": "OK", "reason": "fine"}\n```', ("ok", "fine", "model")),
    ('Sure. {"verdict": "suspect", "reason": "dark"}', ("suspect", "the model's answer was not JSON", "malformed")),
    ('{"verdict": "failed"}', ("failed", "", "model")),
    ("nope", ("suspect", "the model's answer was not JSON", "malformed")),
])
def test_parse_verdict(text, want):
    assert vguard.parse_verdict(text) == want


def test_vision_url_must_be_on_the_local_network(capsys):
    with pytest.raises(ConfigError, match="not on this machine or the local network"):
        vguard.VisionGuard("http://8.8.8.8/v1", "x").check()
    vguard.check_local("http://127.0.0.1:7341/v1")
    vguard.check_local("http://100.104.23.40:18091/v1")  # Tailscale address
    assert cli.main(["watch-config", "--vision-url", "http://8.8.8.8/v1"]) == cli.EXIT_ERROR
    assert not config.watch_settings_path().exists()


def test_guard_check_finds_a_missing_server_or_model_before_the_watch_starts():
    v = fakes.FakeVision([OK_JSON])
    try:
        vguard.VisionGuard(v.url, "fake-vl", key=v.key).check()
        with pytest.raises(ConfigError, match="fonsviva models list --app gantrel"):
            vguard.VisionGuard(v.url, "qwen-missing", key=v.key).check()
    finally:
        v.close()
    with pytest.raises(ConfigError, match="did not answer.*fonsviva status"):
        vguard.VisionGuard("http://127.0.0.1:9/v1", "x", key="k").check()


# ---------------------------------------------------------------- timelapse


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg is not installed")
def test_timelapse_is_an_mp4_that_ffprobe_reads(moon):
    res = run_watch(moon, n_stop=6, timelapse=True)
    mp4 = res.job_dir / "timelapse.mp4"
    assert res.timelapse == str(mp4)
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json", str(mp4)],
        capture_output=True, text=True, check=True).stdout)
    video = [s for s in probe["streams"] if s["codec_type"] == "video"]
    assert len(video) == 1
    assert (video[0]["width"], video[0]["height"]) == (640, 360)
    assert int(video[0]["nb_read_frames"]) == 6
    assert "mp4" in probe["format"]["format_name"]
    assert json.loads((res.job_dir / "job.json").read_text())["timelapse"] == str(mp4)


def test_timelapse_without_ffmpeg_says_so(moon, monkeypatch):
    monkeypatch.setattr(monitor.shutil, "which", lambda _: None)
    res = run_watch(moon, n_stop=2, timelapse=True)
    assert res.timelapse == ""
    assert "ffmpeg is not installed" in res.timelapse_note
    assert f"gantrel-printer frames {moon.cfg.name} --timelapse {res.job_dir.name}" in res.timelapse_note


# ---------------------------------------------------------------- notices


def set_notify(url, *extra):
    import sys
    old = sys.stdin
    sys.stdin = io.StringIO(url + "\n")
    try:
        with redirect_stdout(io.StringIO()):
            assert cli.main(["watch-config", "--notify-url-stdin", *extra]) == 0
    finally:
        sys.stdin = old


def test_webhook_gets_finished_with_the_last_frame_and_the_url_stays_out_of_files(moon):
    hook = fakes.FakeHook()
    url = hook.base + "/hook/s3cret-path-token"
    try:
        set_notify(url)
        res = run_watch(moon, notifier=notify.Notifier.from_settings(), n_stop=2)
    finally:
        hook.close()
    assert res.outcome == "finished"
    assert len(hook.got) == 1
    got = hook.got[0]
    assert got["method"] == "POST" and got["path"] == "/hook/s3cret-path-token"
    body = json.loads(got["body"])
    assert body["event"] == "finished" and body["printer"] == moon.cfg.name
    assert base64.b64decode(body["frame"]["base64"]) == (res.job_dir / "frames" / "000002.jpg").read_bytes()
    assert "s3cret-path-token" not in config.watch_settings_path().read_text()
    for f in res.job_dir.rglob("*"):
        if f.is_file():
            assert b"s3cret-path-token" not in f.read_bytes()


def test_ntfy_gets_paused_by_guard_with_the_frame_attached(moon):
    hook = fakes.FakeHook()
    v, g = vision([FAILED_JSON])
    try:
        set_notify(hook.base + "/printer-topic", "--notify-kind", "ntfy")
        res = run_watch(moon, guard=g, notifier=notify.Notifier.from_settings())
    finally:
        hook.close()
        v.close()
    assert res.outcome == "paused_by_guard"
    got = hook.got[0]
    assert got["method"] == "PUT" and got["path"] == "/printer-topic"
    assert got["headers"]["Title"] == f"Gantrel paused the print: {moon.cfg.name}"
    assert got["headers"]["Priority"] == "5" and got["headers"]["Filename"] == "000002.jpg"
    assert "spaghetti on the bed" in got["headers"]["Message"]
    assert got["body"] == (res.job_dir / "frames" / "000002.jpg").read_bytes()


def test_a_target_outside_the_network_gets_no_frame_unless_asked(monkeypatch, tmp_path):
    """Whether the frame may go is decided on each send, from the one DNS answer that send connects to."""
    hook = fakes.FakeHook()
    frame = tmp_path / "f.jpg"
    frame.write_bytes(GOOD[0].read_bytes())
    real_connect = socket.socket.connect
    # 8.8.8.8 stands for a public target; its connection is sent to the local fake hook instead.
    monkeypatch.setattr(socket.socket, "connect",
                        lambda self, sa: real_connect(self, ("127.0.0.1", hook.port) if sa[0] == "8.8.8.8" else sa))
    try:
        notify.Notifier(f"http://8.8.8.8:{hook.port}/topic", "webhook").send("finished", "k2", "m", frame=frame)
        notify.Notifier(f"http://8.8.8.8:{hook.port}/topic", "webhook", attach_frame=True).send(
            "finished", "k2", "m", frame=frame)
        notify.Notifier(f"{hook.base}/topic", "webhook").send("finished", "k2", "m", frame=frame)
    finally:
        hook.close()
    assert ["frame" in json.loads(g["body"]) for g in hook.got] == [False, True, True]
    assert notify.shown("https://hooks.example.com/services/T0/B0/xyz") == "https://hooks.example.com/..."


def test_a_failed_notice_never_stops_the_watch(moon):
    n = notify.Notifier("http://127.0.0.1:9/hook", "webhook")
    res = run_watch(moon, notifier=n, n_stop=1)
    assert res.outcome == "finished"
    assert any("notice not sent" in line for line in res.lines)


def test_notify_without_a_target_says_how_to_set_one():
    with pytest.raises(ConfigError, match="watch-config --notify-url-stdin"):
        notify.Notifier.from_settings()


# ---------------------------------------------------------------- CLI


def pair_moon(rig):
    config.save_all({rig.cfg.name: rig.cfg})


def test_cli_watch_then_frames(moon, capsys):
    moon.cfg.extra["has_secret"] = True
    config.set_secret(moon.cfg.name, moon.secret)
    pair_moon(moon)
    v, _ = vision([OK_JSON])
    config.set_secret(config.FONSVIVA_KEY, v.key)
    timer = threading.Timer(0.8, lambda: setattr(moon.brain, "state", "complete"))
    timer.start()
    try:
        code = cli.main(["watch", moon.cfg.name, "--interval", "0.1", "--guard", "--vision-url", v.url,
                         "--vision-model", "fake-vl"])
    finally:
        timer.cancel()
        v.close()
    out = capsys.readouterr().out
    assert code == 0, out
    assert "Guard on: fake-vl through Fonsviva" in out and "guard ok" in out and "experimental" in out
    assert v.key not in out
    assert "ended: finished" in out and out.rstrip().splitlines()[-1].startswith("NEXT: ")
    assert cli.main(["frames", moon.cfg.name, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["jobs"]) == 1 and data["jobs"][0]["outcome"] == "finished" and data["jobs"][0]["frames"] >= 2


def test_cli_frames_with_no_jobs(moon, capsys):
    pair_moon(moon)
    assert cli.main(["frames", moon.cfg.name]) == 0
    out = capsys.readouterr().out
    assert "No watch jobs" in out and f"gantrel-printer watch {moon.cfg.name} --timelapse --background" in out


def test_cli_rejects_a_job_dir_outside_the_printers_folder(moon, tmp_path, capsys):
    moon.cfg.extra["has_secret"] = True
    config.set_secret(moon.cfg.name, moon.secret)
    pair_moon(moon)
    assert cli.main(["watch", moon.cfg.name, "--job-dir", str(tmp_path)]) == cli.EXIT_ERROR
    assert "--job-dir must be a job folder" in capsys.readouterr().err


def test_cli_background_watch_returns_at_once_and_finishes_on_its_own(tls_cert, tmp_path, capsys):
    rig = make_rig("moonraker", tls_cert)
    rig.brain.secret = None  # the child process has no access to the test's in-memory keyring
    try:
        job = upload_job(rig, tmp_path)
        start_print(rig, rig.adapter(), job)
        config.save_all({rig.cfg.name: PrinterConfig(name=rig.cfg.name, type="moonraker", host="127.0.0.1",
                                                     port=rig.cfg.port)})
        t0 = time.monotonic()
        assert cli.main(["watch", rig.cfg.name, "--interval", "0.2", "--background", "--json"]) == 0
        assert time.monotonic() - t0 < 10
        info = json.loads(capsys.readouterr().out)
        job_dir = Path(info["job_dir"])
        deadline = time.monotonic() + 20
        while not list((job_dir / "frames").glob("*.jpg")) and time.monotonic() < deadline:
            time.sleep(0.1)
        rig.brain.state = "complete"
        done = {}
        while time.monotonic() < deadline:
            try:
                done = json.loads((job_dir / "job.json").read_text())
            except (OSError, ValueError):
                pass
            if done.get("outcome") not in (None, "running"):
                break
            time.sleep(0.1)
        assert done.get("outcome") == "finished", (job_dir / "watch.log").read_text()
        assert rig.brain.events.count("pause") == 0
    finally:
        for c in rig.closers:
            c()
