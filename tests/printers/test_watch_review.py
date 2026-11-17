"""Regression tests for the tier 3 review of the watch (PR #9): sol-F1 to sol-F4 and second-F6.

- F1, F6: a failed notice never shows the notice URL's path, query or user, in the log or watch.log.
- F2: a frame or key goes only to the address a check just found local, per request (DNS rebinding).
- F3: only a whole JSON object with a known verdict counts; arrays or prose around one never pause.
- F4: the guard runs through Fonsviva with Gantrel's app key from the keyring, and records where it ran.
"""

from __future__ import annotations

import io
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import keyring
import pytest

from gantrel_printer import cli, config, guard as vguard, net, notify
from gantrel_printer.config import PrinterConfig

import fakes
from filekeyring import FileKeyring
from test_contract import make_rig, start_print, upload_job
from test_watch import FAILED_JSON, GOOD, OK_JSON, forbid, moon, run_watch, vision  # noqa: F401  (fixtures)

# Markers standing in for the token parts of a notice URL; none of them may ever reach a log.
PATH_MARK = "hook-path-mark-7f3a"
QUERY_MARK = "query-mark-91bd"
USER_MARK = "user-mark-55c0"
PUBLIC = "8.8.8.8"  # a public address the DNS answer changes to; connections to it are redirected locally


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def marks_in(text: str) -> list[str]:
    return [x for x in (PATH_MARK, QUERY_MARK, USER_MARK) if x in text]


# ---------------------------------------------------------------- sol-F1, second-F6: notice errors hide the URL


NOTICE_URLS = {
    "webhook": "http://{user}:pw@127.0.0.1:{port}/hook/{path}?token={query}",
    "home-assistant": "http://127.0.0.1:{port}/api/webhook/{path}?k={query}",
    "ntfy": "http://127.0.0.1:{port}/{path}?auth={query}",
}


@pytest.mark.parametrize("target", sorted(NOTICE_URLS))
def test_a_failed_notice_to_a_closed_port_never_shows_the_url_token(moon, target):
    port = closed_port()
    url = NOTICE_URLS[target].format(user=USER_MARK, port=port, path=PATH_MARK, query=QUERY_MARK)
    n = notify.Notifier(url, "ntfy" if target == "ntfy" else "webhook")
    res = run_watch(moon, notifier=n, n_stop=1)
    log = "\n".join(res.lines)
    assert res.outcome == "finished"
    assert "notice not sent" in log and f"127.0.0.1:{port}" in log
    assert marks_in(log) == []
    for f in res.job_dir.rglob("*"):
        if f.is_file():
            assert marks_in(f.read_text(errors="replace")) == [], f


def test_a_notice_target_that_echoes_its_path_in_an_error_page_never_shows_it(moon):
    hook = fakes.FakeHook(status=500)
    try:
        n = notify.Notifier(f"{hook.base}/api/webhook/{PATH_MARK}?k={QUERY_MARK}", "webhook")
        res = run_watch(moon, notifier=n, n_stop=1)
    finally:
        hook.close()
    log = "\n".join(res.lines)
    assert len(hook.got) == 1 and "notice not sent" in log and "HTTP 500" in log
    assert marks_in(log) == []


def test_a_background_watch_never_writes_the_notice_url_to_watch_log(tls_cert, tmp_path, capsys, monkeypatch):
    # The background watch is its own process, so it reads the notice URL from a keyring file both share.
    monkeypatch.setenv("GANTREL_TEST_KEYRING_FILE", str(tmp_path / "keyring.json"))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "filekeyring.FileKeyring")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(Path(__file__).parent),
                                                      os.environ.get("PYTHONPATH", "")]))
    keyring.set_keyring(FileKeyring())
    port = closed_port()
    config.set_secret(config.NOTIFY_KEY, f"http://127.0.0.1:{port}/api/webhook/{PATH_MARK}?k={QUERY_MARK}")
    rig = make_rig("moonraker", tls_cert)
    rig.brain.secret = None
    job_dir = None
    try:
        job = upload_job(rig, tmp_path)
        start_print(rig, rig.adapter(), job)
        config.save_all({rig.cfg.name: PrinterConfig(name=rig.cfg.name, type="moonraker", host="127.0.0.1",
                                                     port=rig.cfg.port)})
        assert cli.main(["watch", rig.cfg.name, "--interval", "0.2", "--background", "--notify", "--json"]) == 0
        info = json.loads(capsys.readouterr().out)
        job_dir = Path(info["job_dir"])
        deadline = time.monotonic() + 30
        while not list((job_dir / "frames").glob("*.jpg")) and time.monotonic() < deadline:
            time.sleep(0.1)
        rig.brain.state = "complete"
        log = ""
        while time.monotonic() < deadline:
            log = (job_dir / "watch.log").read_text()
            if "notice not sent" in log:
                break
            time.sleep(0.1)
    finally:
        for c in rig.closers:
            c()
    assert "notice not sent" in log, log
    assert f"127.0.0.1:{port}" in log
    assert marks_in(log) == []
    for f in job_dir.rglob("*"):
        if f.is_file():
            assert marks_in(f.read_text(errors="replace")) == [], f


# ---------------------------------------------------------------- sol-F2: one DNS answer, checked, then used


class Rebinder:
    """getaddrinfo for `name` answers 127.0.0.1 until flip(), then a public address. Connections to the
    public address are recorded and sent to `evil_port` on 127.0.0.1 (the attacker's server)."""

    def __init__(self, monkeypatch, name: str, evil_port: int):
        self.name, self.evil_port, self.public = name, evil_port, False
        self.public_connects: list = []
        real_gai, real_connect = socket.getaddrinfo, socket.socket.connect
        rb = self

        def gai(host, port, *a, **k):
            if host == rb.name:
                return real_gai(PUBLIC if rb.public else "127.0.0.1", port, *a, **k)
            return real_gai(host, port, *a, **k)

        def connect(sock, sa):
            if isinstance(sa, tuple) and sa and sa[0] == PUBLIC:
                rb.public_connects.append(sa)
                return real_connect(sock, ("127.0.0.1", rb.evil_port))
            return real_connect(sock, sa)

        monkeypatch.setattr(socket, "getaddrinfo", gai)
        monkeypatch.setattr(socket.socket, "connect", connect)

    def flip(self):
        self.public = True


def _port(url: str) -> int:
    return int(url.split("://")[1].split("/")[0].rsplit(":", 1)[1])


def test_guard_frames_never_follow_a_dns_answer_that_changed_to_a_public_address(monkeypatch):
    good, evil = fakes.FakeVision([OK_JSON], key=None), fakes.FakeVision([FAILED_JSON], key=None)
    rb = Rebinder(monkeypatch, "vision.test", _port(evil.url))
    try:
        g = vguard.VisionGuard(f"http://vision.test:{_port(good.url)}/v1", "fake-vl", timeout=5)
        g.key = "k"  # FakeVision(key=None) takes any key
        g.check()
        first = g.classify(GOOD[0])
        rb.flip()  # the name now answers a public address: the next frame must not go there
        second = g.classify(GOOD[0])
    finally:
        good.close()
        evil.close()
    assert first.verdict == "ok" and len(good.requests) == 1, first.to_dict()
    assert evil.requests == [] and evil.auth == []
    assert rb.public_connects == []
    assert second.verdict == "suspect" and second.source == "unreachable"


def test_notice_frame_eligibility_is_decided_on_each_send_from_the_address_it_connects_to(monkeypatch, tmp_path):
    local, evil = fakes.FakeHook(), fakes.FakeHook()
    rb = Rebinder(monkeypatch, "hook.test", evil.port)
    frame = tmp_path / "000001.jpg"
    frame.write_bytes(GOOD[0].read_bytes())
    try:
        n = notify.Notifier(f"http://hook.test:{local.port}/topic", "webhook")
        n.send("finished", "k2", "first", frame=frame)
        rb.flip()
        n.send("finished", "k2", "second", frame=frame)
    finally:
        local.close()
        evil.close()
    assert len(local.got) == 1 and "frame" in json.loads(local.got[0]["body"])
    assert len(evil.got) == 1
    assert "frame" not in json.loads(evil.got[0]["body"])  # the public answer got the notice, never the frame


def _cert(tmp_path, name: str):
    cert, key = tmp_path / f"{name}.pem", tmp_path / f"{name}.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", f"/CN={name}",
                    "-addext", f"subjectAltName=DNS:{name}", "-keyout", str(key), "-out", str(cert)],
                   check=True, capture_output=True)
    return str(cert), str(key)


def _https_server(cert, key) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps({"host": self.headers.get("Host")}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_https_to_a_pinned_address_still_checks_the_certificate_against_the_host_name(monkeypatch, tmp_path):
    good_cert, good_key = _cert(tmp_path, "vision.test")
    other_cert, other_key = _cert(tmp_path, "other.test")
    Rebinder(monkeypatch, "vision.test", 1)
    srv, wrong = _https_server(good_cert, good_key), _https_server(other_cert, other_key)
    try:
        ctx = ssl.create_default_context(cafile=good_cert)
        ctx.load_verify_locations(other_cert)
        port = srv.server_address[1]
        status, _, data = net.request("GET", f"https://vision.test:{port}/v1/models", local_only=True, context=ctx)
        assert status == 200 and json.loads(data)["host"] == f"vision.test:{port}"
        with pytest.raises(net.Unreachable):
            net.request("GET", f"https://vision.test:{wrong.server_address[1]}/v1/models", local_only=True,
                        context=ctx)
    finally:
        srv.shutdown()
        wrong.shutdown()


# ---------------------------------------------------------------- sol-F3: the whole answer is the object


WRAPPED_FAILED = [
    '[{"verdict": "failed", "reason": "spaghetti"}]',
    'I see spaghetti. {"verdict": "failed", "reason": "spaghetti"}',
    '{"verdict": "failed", "reason": "spaghetti"} That is my answer.',
    '{"verdict": "ok"} {"verdict": "failed", "reason": "spaghetti"}',
    '```json\n[{"verdict": "failed", "reason": "spaghetti"}]\n```',
    'Here:\n```json\n{"verdict": "failed", "reason": "spaghetti"}\n```',
]


@pytest.mark.parametrize("text", WRAPPED_FAILED)
def test_a_failed_verdict_inside_an_array_or_prose_is_malformed(text):
    verdict, _, source = vguard.parse_verdict(text)
    assert (verdict, source) == ("suspect", "malformed")


@pytest.mark.parametrize("text", [
    '```json\n{"verdict": "failed", "reason": "spaghetti"}\n```',
    '```\n{"verdict": "failed", "reason": "spaghetti"}\n```',
    '  {"verdict": "FAILED", "reason": "spaghetti"}\n',
])
def test_a_whole_object_with_or_without_one_fence_still_parses(text):
    assert vguard.parse_verdict(text) == ("failed", "spaghetti", "model")


@pytest.mark.parametrize("reply", WRAPPED_FAILED[:2])
def test_wrapped_failed_verdicts_never_pause_a_print(moon, forbid, reply):
    calls = forbid()
    v, g = vision([reply])
    try:
        res = run_watch(moon, guard=g, n_stop=4)
    finally:
        v.close()
    assert res.outcome == "finished" and moon.brain.events == [] and calls == []
    assert {json.loads(x)["source"] for x in (res.job_dir / "guard.jsonl").read_text().splitlines()} == {"malformed"}


# ---------------------------------------------------------------- sol-F4: through Fonsviva, with its app key


def test_the_default_vision_url_is_fonsviva_on_this_machine():
    assert vguard.DEFAULT_URL == "http://127.0.0.1:7341/v1"
    assert "11434" not in Path(vguard.__file__).read_text()


def test_guard_sends_gantrels_fonsviva_key_and_records_where_each_verdict_ran(moon):
    v = fakes.FakeVision([OK_JSON])
    config.set_secret(config.FONSVIVA_KEY, v.key)
    try:
        g = vguard.VisionGuard.from_settings(url=v.url, model="fake-vl")
        g.check()
        res = run_watch(moon, guard=g, n_stop=2)
    finally:
        v.close()
    host = v.url.split("://")[1].split("/")[0]
    assert v.auth and set(v.auth) == {"Bearer " + v.key}
    job = json.loads((res.job_dir / "job.json").read_text())
    assert job["guard"]["ran_on"] == {"via": "fonsviva", "host": host, "model": "fake-vl"}
    assert len(job["guard"]["verdicts"]) == 2
    for rec in job["guard"]["verdicts"]:
        assert rec["verdict"] == "ok"
        assert rec["ran_on"] == {"via": "fonsviva", "host": host, "model": "fake-vl",
                                 "served": {"x-fonsviva-machine": "fake-fonsviva-host", "model": "served/fake-vl"}}
    assert any(f"ran on fonsviva at {host}, model served/fake-vl" in line for line in res.lines)
    for f in res.job_dir.rglob("*"):
        if f.is_file():
            assert v.key.encode() not in f.read_bytes()


def run_cli(argv, stdin: str | None = None):
    old = sys.stdin
    sys.stdin = io.StringIO(stdin or "")
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
    finally:
        sys.stdin = old
    return code, out.getvalue(), err.getvalue()


def test_fonsviva_key_is_read_from_stdin_kept_in_the_keyring_and_never_shown():
    piped = "fv-app-key-never-printed-4242"
    code, out, err = run_cli(["watch-config", "--fonsviva-key-stdin"], piped + "\n")
    assert code == 0, err
    assert config.get_secret(config.FONSVIVA_KEY) == piped
    assert piped not in out + err and "Fonsviva key: saved in the OS keyring" in out
    assert piped not in config.watch_settings_path().read_text()
    code, out, _ = run_cli(["watch-config", "--json"])
    assert json.loads(out)["fonsviva_key"] is True and piped not in out


def _paired(rig):
    rig.cfg.extra["has_secret"] = True
    config.set_secret(rig.cfg.name, rig.secret)
    config.save_all({rig.cfg.name: rig.cfg})


def test_guard_without_a_fonsviva_key_says_to_check_fonsviva(moon):
    _paired(moon)
    v = fakes.FakeVision([OK_JSON])
    try:
        code, _, err = run_cli(["watch", moon.cfg.name, "--guard", "--vision-url", v.url, "--vision-model", "fake-vl"])
    finally:
        v.close()
    assert code == cli.EXIT_ERROR
    assert "ERROR" in err and "fonsviva status" in err and "--fonsviva-key-stdin" in err
    assert v.requests == []


def test_guard_with_fonsviva_not_running_says_fonsviva_status(moon):
    _paired(moon)
    config.set_secret(config.FONSVIVA_KEY, "k")
    code, _, err = run_cli(["watch", moon.cfg.name, "--guard", "--vision-url", f"http://127.0.0.1:{closed_port()}/v1"])
    assert code == cli.EXIT_ERROR
    assert "ERROR" in err and "Fonsviva" in err and "fonsviva status" in err


def test_guard_with_a_key_fonsviva_refuses_says_so(moon):
    _paired(moon)
    config.set_secret(config.FONSVIVA_KEY, "wrong-app-key")
    v = fakes.FakeVision([OK_JSON])
    try:
        code, _, err = run_cli(["watch", moon.cfg.name, "--guard", "--vision-url", v.url, "--vision-model", "fake-vl"])
    finally:
        v.close()
    assert code == cli.EXIT_ERROR
    assert "refused Gantrel's key" in err and "fonsviva keys list" in err and "wrong-app-key" not in err


# ---------------------------------------------------------------- review round 2 (PR #9)


def test_guard_never_pauses_a_new_print_with_frames_from_the_last_one(moon):
    # sol-sol-F5: while the 2nd failed frame of print A is being judged, the person cancels A and
    # starts print B. The streak belongs to A, so B must not be paused on A's evidence.
    from test_watch import FAILED_JSON, vision, run_watch

    v, g = vision([FAILED_JSON])
    real = g.classify
    calls = {"n": 0}

    def classify(frame, st=None):
        out = real(frame, st)
        calls["n"] += 1
        if calls["n"] == 2:
            moon.brain.current = "print-b.gcode"  # A cancelled, B started, all during the long classify
        return out

    g.classify = classify
    try:
        res = run_watch(moon, guard=g, n_stop=3)
    finally:
        v.close()
    if res.outcome == "paused_by_guard":
        pause = json.loads((res.job_dir / "pause.json").read_text())
        assert all(f.get("print") == "print-b.gcode" for f in pause["frames"]), pause["frames"]
    else:
        assert moon.brain.events == []


def test_a_vision_error_that_echoes_the_key_never_keeps_it(tmp_path):
    # sol-sol-F6: an error reply that names the rejected bearer must not land in a verdict or an error.
    from gantrel_printer import guard as vguard
    from gantrel_printer.errors import ConfigError

    key = "fv-test-SECRET-key-0123456789"

    def routes(h, method):
        return h._send(500, {"error": f"backend failed for Authorization: Bearer {key}"})

    srv = fakes.serve_http(fakes.Brain(), routes)
    url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
    frame = tmp_path / "f.jpg"
    frame.write_bytes(fakes.JPEG)
    try:
        g = vguard.VisionGuard(url, "fake-vl", timeout=5, key=key)
        verdict = g.classify(frame)
        assert verdict.verdict == "suspect"
        assert key not in json.dumps(verdict.to_dict())
        with pytest.raises(ConfigError) as err:
            g.check()
        assert key not in str(err.value) and key not in str(getattr(err.value, "hint", ""))
    finally:
        srv.shutdown()
