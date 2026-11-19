"""Gantrel Studio server: security gates, param checks, build queue, live events, exports."""

from __future__ import annotations

import http.client
import io
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from gantrel_studio import server, threemf

from studiokit import Client, start

NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}


def wait_for(cond, timeout=10.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("timed out")


def settled(c: Client, timeout=15.0) -> dict:
    return wait_for(lambda: (s := c.json("GET", "/api/state")[1]) and not s["building"] and s, timeout)


# security


def test_api_needs_the_token(studio):
    assert studio.request("GET", "/api/state", token=False)[0] == 403
    assert studio.request("GET", "/api/state?t=wrong", token=False)[0] == 403
    assert studio.request("GET", "/api/state", headers={"X-Gantrel-Token": "wrong"}, token=False)[0] == 403
    assert studio.request("GET", f"/api/state?t={studio.key}", token=False)[0] == 200
    assert studio.request("GET", "/api/state")[0] == 200
    for path in ("/api/part.stl", "/api/preview.png", "/api/export/part.stl", "/api/export/part.3mf",
                 "/api/printers", "/api/events"):
        assert studio.request("GET", path, token=False)[0] == 403, path
    for path in ("/api/params", "/api/reset"):
        assert studio.request("POST", path, {"params": {"size": 60}}, token=False)[0] == 403, path


def test_foreign_host_is_refused_even_with_the_token(studio):
    for host in ("evil.example:%d" % studio.port, "127.0.0.1", f"127.0.0.1:{studio.port + 1}",
                 f"attacker.localhost:{studio.port}", f"localhost.evil.example:{studio.port}"):
        assert studio.request("GET", "/api/state", host=host)[0] == 403, host
        assert studio.request("GET", "/", host=host)[0] == 403, host
    assert studio.request("GET", "/api/state", host=f"localhost:{studio.port}")[0] == 200


def test_post_with_a_foreign_origin_is_refused_and_never_builds(studio, fake_stone):
    for origin in ("http://evil.example", f"http://127.0.0.1:{studio.port + 1}", "null",
                   f"https://127.0.0.1:{studio.port}"):
        code, body = studio.json("POST", "/api/params", {"params": {"size": 60}}, headers={"Origin": origin})
        assert code == 403 and body["error"] == "forbidden origin", origin
    assert studio.json("POST", "/api/reset", headers={"Origin": "http://evil.example"})[0] == 403
    time.sleep(0.3)
    assert fake_stone.builds() == 0 and not any(c[0] == "set" for c in fake_stone.calls())
    code, _ = studio.json("POST", "/api/params", {"params": {"size": 60}},
                          headers={"Origin": f"http://localhost:{studio.port}"})
    assert code == 202


def test_binds_loopback_and_skips_a_taken_port(job):
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    port = taken.getsockname()[1]
    try:
        httpd = server.bind(job, port=port)
        try:
            assert httpd.server_address[0] == "127.0.0.1"
            assert port < httpd.port <= port + 10
        finally:
            httpd.server_close()
    finally:
        taken.close()


def test_cli_prints_one_line_and_never_logs_the_token(job, fake_stone):
    p = subprocess.Popen([sys.executable, "-m", "gantrel_studio.cli", "--job", str(job), "--port", "0", "--no-open"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = p.stdout.readline().strip()
        m = re.fullmatch(r"Gantrel Studio: http://127\.0\.0\.1:(\d+)/\?t=([A-Za-z0-9_-]{22})", line)
        assert m, line
        port, key = int(m.group(1)), m.group(2)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", f"/api/state?t={key}", headers={"Host": f"127.0.0.1:{port}"})
        assert conn.getresponse().status == 200
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", f"/api/nothing?t={key}", headers={"Host": f"127.0.0.1:{port}"})
        assert conn.getresponse().status == 404
        conn.close()
    finally:
        p.terminate()
        out, err = p.communicate(timeout=10)
    assert key not in out and key not in err


# params


@pytest.mark.parametrize("params", [
    {"size": 300},             # above max
    {"size": 10},              # below min
    {"size": "60"},            # a string for a number
    {"size": True},            # a bool for a number
    {"rings": 2.5},            # a fraction for an int
    {"style": "stars"},        # not a choice
    {"chamfer": "yes"},        # not a bool
    {"label": "x" * 201},      # too long
    {"label": "a\nb"},         # control character
    {"label": "a\x00b"},
    {"icon": "/etc/passwd"},   # image params name files: never from the page
    {"nope": 1},               # unknown
    {"size": 60, "--reset": True},
    [1, 2],
    "size=60",
])
def test_bad_params_are_rejected_before_stone_runs(studio, fake_stone, params):
    code, body = studio.json("POST", "/api/params", {"params": params})
    assert code == 400, body
    time.sleep(0.2)
    assert [c for c in fake_stone.calls() if c[0] in ("set", "build")] == []


def test_good_params_reach_stone_set_as_an_argv_list(studio, fake_stone, job):
    code, state = studio.json("POST", "/api/params",
                              {"params": {"size": 60, "style": "honeycomb", "rings": 4, "chamfer": True,
                                          "label": "fan; rm -rf ~ $(id)"}})
    assert code == 202 and state["building"] is True
    final = settled(studio)
    sets = [c for c in fake_stone.calls() if c[0] == "set"]
    assert sets == [["set", "--job", str(job), "fan_guard", "size=60", "style=honeycomb", "rings=4",
                     "chamfer=true", "label=fan; rm -rf ~ $(id)"]]
    assert final["params"]["size"] == 60 and final["part"]["size_mm"] == [60.0, 60.0, 3.0]
    assert final["error"] is None


def test_two_quick_posts_build_at_most_twice_and_the_second_wins(studio, fake_stone, monkeypatch):
    monkeypatch.setenv("FAKE_STONE_DELAY", "0.8")
    assert studio.json("POST", "/api/params", {"params": {"size": 50}})[0] == 202
    wait_for(lambda: fake_stone.builds() >= 1 or any(c[0] == "set" for c in fake_stone.calls()))
    assert studio.json("POST", "/api/params", {"params": {"size": 70}})[0] == 202
    assert studio.json("POST", "/api/params", {"params": {"size": 80}})[0] == 202
    final = settled(studio, timeout=20)
    time.sleep(0.3)
    assert fake_stone.builds() <= 2
    assert final["params"]["size"] == 80
    assert final["part"]["size_mm"] == [80.0, 80.0, 3.0]


def test_reset_runs_set_reset_then_build(studio, fake_stone, job):
    assert studio.json("POST", "/api/reset")[0] == 202
    final = settled(studio)
    calls = [c for c in fake_stone.calls() if c[0] in ("set", "build")]
    assert calls == [["set", "--job", str(job), "--reset", "fan_guard"], ["build", "--job", str(job)]]
    assert final["params"]["size"] == 120


def test_a_failed_build_reports_the_last_20_lines(studio, monkeypatch):
    monkeypatch.setenv("FAKE_STONE_FAIL", "1")
    assert studio.json("POST", "/api/params", {"params": {"thickness": 2}})[0] == 202
    final = settled(studio)
    lines = final["error"].splitlines()
    assert len(lines) == 20 and lines[-1] == "NEXT: stone set fan_guard thickness=3"


# state and events


def test_state_has_the_job_schema_and_part(studio, job, fake_stone):
    code, s = studio.json("GET", "/api/state")
    assert code == 200
    assert s["job"] == str(job.resolve()) and s["exists"] is True
    assert s["request"] == "fan guard for a 40mm fan"
    assert s["recipe"] == "fan_guard" and s["title"] == "Fan guard" and s["summary"]
    assert [p["name"] for p in s["schema"]][:3] == ["size", "style", "thickness"]
    assert s["params"]["size"] == 40 and s["params"]["style"] == "rings"
    assert s["status"]["stage"] == "done" and s["check"]["passed"] is True
    assert s["part"]["size_mm"] == [40.0, 40.0, 3.0] and s["part"]["faces"] == 12
    assert re.fullmatch(r"/api/part\.stl\?v=\d+", s["part"]["url"])
    assert s["building"] is False and s["error"] is None and isinstance(s["version"], int)
    studio.json("GET", "/api/state")
    assert sum(1 for c in fake_stone.calls() if c[0] == "show") == 1  # schema cached per recipe
    code, headers, body = studio.request("GET", s["part"]["url"])
    assert code == 200 and headers["Content-Type"] == "model/stl" and threemf.face_count(body) == 12
    code, headers, body = studio.request("GET", "/api/preview.png")
    assert code == 200 and headers["Content-Type"] == "image/png" and body.startswith(b"\x89PNG")


def test_state_for_a_missing_job(tmp_path, fake_stone):
    httpd = start(tmp_path / "no-job")
    try:
        c = Client(httpd)
        code, s = c.json("GET", "/api/state")
        assert code == 200 and s["exists"] is False and s["recipe"] is None and s["part"] is None
        assert c.json("POST", "/api/params", {"params": {"size": 60}})[0] == 409
        assert c.request("GET", "/api/part.stl")[0] == 404
        assert c.request("GET", "/api/export/part.3mf")[0] == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def read_events(resp, until, timeout=10.0):
    """Read SSE events until until(state) is true; returns (states, saw_keepalive)."""
    states, ping, buf = [], False, b""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        line = resp.fp.readline()
        if not line:
            break
        if line.startswith(b":"):
            ping = True
        buf += line
        if line == b"\n" and buf.strip():
            for ln in buf.splitlines():
                if ln.startswith(b"data: "):
                    states.append(json.loads(ln[6:]))
            buf = b""
            if states and until(states[-1]):
                return states, ping
    raise AssertionError(f"no matching event; got {len(states)} states")


def test_events_follow_edits_made_on_disk(studio, job):
    studio.httpd.studio.keepalive = 0.2
    conn = http.client.HTTPConnection("127.0.0.1", studio.port, timeout=10)
    conn.request("GET", f"/api/events?t={studio.key}", headers={"Host": f"127.0.0.1:{studio.port}"})
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Type") == "text/event-stream"
    first, _ = read_events(resp, lambda s: True)
    assert first[0]["params"]["size"] == 40
    time.sleep(0.5)  # long enough for a keepalive comment
    # what `stone set` does when the agent changes a size from chat
    plan = json.loads((job / "plan.json").read_text())
    plan["params"]["size"] = 55
    (job / "plan.json").write_text(json.dumps(plan))
    states, ping = read_events(resp, lambda s: s["params"]["size"] == 55)
    assert ping and states[-1]["version"] > first[0]["version"]
    assert studio.httpd.studio.sse_clients == 1
    resp.close()
    conn.close()
    # the page went away: a keepalive write fails and the handler lets go of its thread
    wait_for(lambda: studio.httpd.studio.sse_clients == 0, timeout=5)


def test_events_report_a_build_start_and_end(studio):
    conn = http.client.HTTPConnection("127.0.0.1", studio.port, timeout=10)
    conn.request("GET", "/api/events", headers={"Host": f"127.0.0.1:{studio.port}", "X-Gantrel-Token": studio.key})
    resp = conn.getresponse()
    read_events(resp, lambda s: True)
    studio.json("POST", "/api/params", {"params": {"size": 90}})
    states, _ = read_events(resp, lambda s: not s["building"] and s["params"]["size"] == 90)
    assert any(s["building"] for s in states)
    assert states[-1]["part"]["size_mm"] == [90.0, 90.0, 3.0]
    conn.close()


# exports


def test_export_stl_and_3mf_downloads(studio, job):
    code, headers, body = studio.request("GET", "/api/export/part.stl")
    assert code == 200 and headers["Content-Disposition"] == 'attachment; filename="fan_guard.stl"'
    assert body == (job / "build" / "part.stl").read_bytes()
    code, headers, body = studio.request("GET", "/api/export/part.3mf")
    assert code == 200 and headers["Content-Disposition"] == 'attachment; filename="fan_guard.3mf"'
    assert headers["Content-Type"] == "model/3mf"
    verts, tris = parse_3mf(body)
    assert len(verts) == 8 and len(tris) == 12


def parse_3mf(data: bytes):
    z = zipfile.ZipFile(io.BytesIO(data))
    assert z.testzip() is None
    assert {"[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model"} <= set(z.namelist())
    ct = ET.fromstring(z.read("[Content_Types].xml"))
    assert any(d.get("Extension") == "model" for d in ct)
    rels = ET.fromstring(z.read("_rels/.rels"))
    assert rels[0].get("Target") == "/3D/3dmodel.model"
    model = ET.fromstring(z.read("3D/3dmodel.model"))
    assert model.get("unit") == "millimeter"
    verts = [(float(v.get("x")), float(v.get("y")), float(v.get("z"))) for v in model.iterfind(".//m:vertex", NS)]
    tris = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3"))) for t in model.iterfind(".//m:triangle", NS)]
    assert all(len(set(t)) == 3 and max(t) < len(verts) for t in tris)
    assert len(model.findall("m:build/m:item", NS)) == 1
    return verts, tris


def binary_stl(tris) -> bytes:
    out = bytearray(b"\0" * 80 + struct.pack("<I", len(tris)))
    for t in tris:
        out += struct.pack("<12fH", 0, 0, 0, *t[0], *t[1], *t[2], 0)
    return bytes(out)


def ascii_stl(tris) -> bytes:
    lines = ["solid test"]
    for t in tris:
        lines += ["  facet normal 0 0 0", "    outer loop"]
        lines += [f"      vertex {x:e} {y:e} {z:e}" for x, y, z in t]
        lines += ["    endloop", "  endfacet"]
    return ("\n".join(lines + ["endsolid test"]) + "\n").encode()


def tetra_grid(n: int):
    """n x n tetrahedra sharing nothing, plus one degenerate triangle that must be dropped."""
    tris = []
    for i in range(n):
        for j in range(n):
            a, b, c, d = (i, j, 0.0), (i + 1, j, 0.0), (i, j + 1, 0.0), (i + 0.5, j + 0.5, 1.25)
            tris += [(a, c, b), (a, b, d), (b, c, d), (c, a, d)]
    tris.append(((0, 0, 0), (0, 0, 0), (1, 0, 0)))
    return tris


@pytest.mark.parametrize("encode", [binary_stl, ascii_stl])
def test_3mf_round_trips_vertex_and_triangle_counts(encode):
    tris = tetra_grid(5)
    unique = {v for t in tris for v in t}
    good = [t for t in tris if len(set(t)) == 3]
    verts, faces = parse_3mf(threemf.stl_to_3mf(encode(tris), title="grid <&>"))
    assert len(verts) == len(unique) and len(faces) == len(good) == 100
    assert {tuple(round(c, 6) for c in v) for v in verts} == {tuple(float(c) for c in v) for v in unique}
    back = sorted(tuple(sorted(verts[i] for i in f)) for f in faces)
    want = sorted(tuple(sorted(tuple(float(c) for c in v) for v in t)) for t in good)
    assert back == want


def test_a_real_stone_part_converts(job):
    data = (job / "build" / "part.stl").read_bytes()
    assert threemf.face_count(data) == 12 and threemf.bbox_size(threemf.triangles(data)) == [40.0, 40.0, 3.0]


# static files and printers


def test_static_serves_the_page_and_blocks_traversal(studio, tmp_path, monkeypatch):
    web = tmp_path / "web"
    (web / "js").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><title>t</title>")
    (web / "js" / "app.mjs").write_text("export {}")
    (web / "style.css").write_text("body{}")
    (tmp_path / "secret.txt").write_text("do not serve")
    (web / "link.txt").symlink_to(tmp_path / "secret.txt")
    monkeypatch.setattr(server, "WEB_ROOT", web)
    code, headers, body = studio.request("GET", "/", token=False)
    assert code == 200 and headers["Content-Type"].startswith("text/html") and body.startswith(b"<!doctype")
    code, headers, _ = studio.request("GET", "/static/js/app.mjs", token=False)
    assert code == 200 and headers["Content-Type"].startswith("text/javascript")
    assert studio.request("GET", "/static/style.css", token=False)[1]["Content-Type"].startswith("text/css")
    for path in ("/static/../secret.txt", "/static/%2e%2e/secret.txt", "/static/js/../../secret.txt",
                 "/static/..%2fsecret.txt", "/static/link.txt", "/static//etc/passwd", "/static/%2fetc%2fpasswd",
                 "/static/..\\secret.txt", "/../secret.txt"):
        code, _, body = studio.request("GET", path, token=False)
        assert code == 404 and b"do not serve" not in body, path


def test_placeholder_page_before_the_ui_exists(studio, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "WEB_ROOT", tmp_path / "empty")
    code, headers, body = studio.request("GET", "/", token=False)
    assert code == 200 and b"Gantrel Studio" in body and headers["Content-Type"].startswith("text/html")


def test_printers_lists_configured_printers_read_only(studio, tmp_path, monkeypatch):
    home = tmp_path / "gp-home"
    monkeypatch.setenv("GANTREL_PRINTER_HOME", str(home))
    assert studio.json("GET", "/api/printers") == (200, [])
    home.mkdir()
    cfg = home / "printers.json"
    cfg.write_text(json.dumps({"printers": {"k2": {"name": "k2", "type": "creality", "host": "10.0.0.5"}}}))
    before = cfg.read_text()
    assert studio.json("GET", "/api/printers") == (200, [{"name": "k2", "kind": "creality"}])
    assert cfg.read_text() == before
    cfg.write_text("{ not json")
    assert studio.json("GET", "/api/printers") == (200, [])


# real stone


def _real_stone() -> str | None:
    exe = shutil.which("stone")
    if not exe:
        return None
    try:
        p = subprocess.run([exe, "show", "fan_guard", "--json"], capture_output=True, text=True, timeout=60)
        json.loads(p.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return exe


@pytest.mark.skipif(_real_stone() is None, reason="needs stone with show --json on PATH")
def test_real_stone_builds_through_the_studio(tmp_path):
    exe = _real_stone()
    j = tmp_path / "stone-job"
    env = {**os.environ, "STONE_JOB": str(j)}
    subprocess.run([exe, "new", "fan guard for a 40mm fan", "--job", str(j)], check=True, capture_output=True, env=env)
    subprocess.run([exe, "set", "fan_guard", "size=40", "--job", str(j)], check=True, capture_output=True, env=env)
    httpd = start(j)
    try:
        c = Client(httpd)
        s = c.json("GET", "/api/state")[1]
        assert s["recipe"] == "fan_guard" and any(p["name"] == "size" for p in s["schema"]) and s["part"] is None
        assert c.json("POST", "/api/params", {"params": {"size": 60}})[0] == 202
        final = settled(c, timeout=600)
        assert final["error"] is None, final["error"]
        assert final["part"]["size_mm"] == [60.0, 60.0, 3.0] and final["part"]["faces"] > 100
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_part_that_fails_a_print_check_is_not_a_failed_build(studio, monkeypatch):
    monkeypatch.setenv("FAKE_STONE_CHECKFAIL", "1")
    assert studio.json("POST", "/api/params", {"params": {"size": 60}})[0] == 202
    final = settled(studio)
    assert final["error"] is None
    assert final["check"]["passed"] is False
    assert final["check"]["results"][0]["name"] == "bodies"
    assert final["part"]["size_mm"][0] == 60


# background studio


def run_cli(*args, env):
    return subprocess.run([sys.executable, "-m", "gantrel_studio.cli", *args], capture_output=True, text=True,
                          timeout=30, env=env)


def test_detach_returns_a_link_reuses_it_and_stop_ends_it(job, fake_stone, tmp_path):
    env = {**os.environ, "XDG_CACHE_HOME": str(tmp_path / "cache")}
    first = run_cli("--job", str(job), "--detach", "--port", "0", env=env)
    try:
        assert first.returncode == 0, first.stderr
        m = re.fullmatch(r"Gantrel Studio: (http://127\.0\.0\.1:(\d+)/\?t=([A-Za-z0-9_-]{22}))\n"
                         r"Give the person this link: \[Open Gantrel Studio\]\(\1\)\n", first.stdout)
        assert m, first.stdout
        port, key = int(m.group(2)), m.group(3)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", f"/api/state?t={key}", headers={"Host": f"127.0.0.1:{port}"})
        assert conn.getresponse().status == 200
        conn.close()
        cache = tmp_path / "cache" / "gantrel-studio"
        assert cache.stat().st_mode & 0o777 == 0o700
        for f in cache.iterdir():
            assert f.stat().st_mode & 0o777 == 0o600, f
            if f.suffix == ".log":
                assert key not in f.read_text()
        again = run_cli("--job", str(job), "--detach", "--port", "0", env=env)
        assert again.returncode == 0 and again.stdout == first.stdout
    finally:
        stopped = run_cli("--job", str(job), "--stop", env=env)
    assert stopped.returncode == 0 and "Stopped" in stopped.stdout
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    assert not list((tmp_path / "cache" / "gantrel-studio").glob("*.json"))
    assert "No studio" in run_cli("--job", str(job), "--stop", env=env).stdout


def test_a_stone_set_from_chat_shows_before_it_builds_and_survives_a_page_edit(studio, job, fake_stone):
    subprocess.run([str(fake_stone.path), "set", "--job", str(job), "fan_guard", "thickness=4"], check=True,
                   capture_output=True)
    assert wait_for(lambda: studio.json("GET", "/api/state")[1]["params"]["thickness"] == 4)
    assert studio.json("POST", "/api/params", {"params": {"size": 60}})[0] == 202
    final = settled(studio)
    assert final["params"]["size"] == 60 and final["params"]["thickness"] == 4
