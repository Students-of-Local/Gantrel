"""Checks for the printer families added after v0 that the shared contract suite can't express:
discovery of every fake, pairing by on-screen approval, PrusaLink digest auth, FlashForge's legacy
port, Duet over DSF, resin uploads, and Marlin on USB serial. Every printer here is a fake on
127.0.0.1 or a pseudo terminal; nothing reaches a real printer.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from gantrel_printer import cli, config
from gantrel_printer import discover as disc
from gantrel_printer.adapters import ADAPTERS, build
from gantrel_printer.adapters.repetier import RepetierAdapter
from gantrel_printer.config import PrinterConfig
from gantrel_printer.errors import AuthFailed, ConfigError, ProtocolError, Refused, Unsupported

import fakes
from test_contract import GCODE, RIG_KINDS, SECRET, make_rig, start_token, upload_job


@pytest.fixture
def no_broadcast(monkeypatch):
    """Discovery tests must never send a broadcast or open a real serial port."""

    def boom(*a, **k):
        raise AssertionError("discovery tried a broadcast or a USB listing while --host was given")

    monkeypatch.setattr(disc, "elegoo_udp", boom)
    monkeypatch.setattr(disc, "serial_hints", boom)
    monkeypatch.setattr(disc, "mdns", boom)
    monkeypatch.setattr(disc, "bambu_ssdp", boom)


# ---------------------------------------------------------------- discover finds every fake


@pytest.mark.parametrize("kind", RIG_KINDS)
def test_discover_with_host_finds_each_fake(kind, tls_cert, monkeypatch, capsys, no_broadcast):
    rig = make_rig(kind, tls_cert)
    try:
        if rig.type == "serial":
            host = rig.cfg.host  # a /dev/pts path: only a named /dev/ path is ever opened
        else:
            host = "127.0.0.1"
            monkeypatch.setattr(disc, "KNOWN_PORTS", [(rig.cfg.port, [rig.type])])
        assert cli.main(["discover", "--host", host, "--no-mdns", "--json"]) == 0
        found = json.loads(capsys.readouterr().out)["printers"]
    finally:
        for c in rig.closers:
            c()
    assert any(r["type"] == rig.type and r["host"] == host for r in found), found


def test_known_ports_cover_every_network_adapter():
    listed = {t for _, types in disc.KNOWN_PORTS for t in types}
    assert set(ADAPTERS) - listed == {"serial"}  # serial ports are never scanned


def test_serial_probe_ignores_network_hosts():
    assert ADAPTERS["serial"].probe("127.0.0.1", 80) is None


# ---------------------------------------------------------------- pairing by approval on the printer's screen


def _pair(name, kind, port, *extra):
    return cli.main(["pair", name, "--type", kind, "--host", "127.0.0.1", "--port", str(port), *extra])


def test_snapmaker_pairs_when_the_person_accepts_on_the_touchscreen(capsys):
    brain = fakes.Brain()
    fs = fakes.FakeSnapmaker(brain, approve=True)
    try:
        assert _pair("sm", "snapmaker", fs.port, "--model", "snapmaker-a350") == 0
        out = capsys.readouterr()
        token = config.get_secret("sm")
        assert token and fs.tokens[token] is True
        assert "touchscreen" in out.err  # told to look at the screen
        assert token not in out.out + out.err  # the token is never printed
        assert token not in config.config_path().read_text()  # only in the keyring
        assert cli.main(["status", "sm", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["state"] == "idle"
    finally:
        fs.close()


def test_snapmaker_pair_fails_when_nobody_accepts(monkeypatch, capsys):
    monkeypatch.setattr(ADAPTERS["snapmaker"], "approval_seconds", 0.5)
    fs = fakes.FakeSnapmaker(fakes.Brain(), approve=False)
    try:
        assert _pair("sm", "snapmaker", fs.port) == 1
        assert "accept" in capsys.readouterr().err.lower()
        assert "sm" not in config.load_all() and config.get_secret("sm") is None
    finally:
        fs.close()


def test_approval_pairing_refuses_no_check(capsys):
    assert _pair("sm", "snapmaker", 1, "--no-check") == 1
    assert "sm" not in config.load_all()


def test_ultimaker_pairs_with_digest_credentials_from_the_screen(capsys):
    fu = fakes.FakeUltimaker(fakes.Brain(), approve=True)
    try:
        assert _pair("um", "ultimaker", fu.port, "--model", "ultimaker-s5") == 0
        out = capsys.readouterr()
        key = config.get_secret("um")
        cfg = config.get("um")
        assert key and fu.creds[cfg.extra["auth_id"]] == key and cfg.extra["auth_id"] in fu.approved
        assert key not in out.out + out.err and key not in config.config_path().read_text()
        assert cli.main(["status", "um", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["state"] == "idle"
    finally:
        fu.close()


def test_ultimaker_pair_refused_on_the_screen_saves_nothing(capsys):
    fu = fakes.FakeUltimaker(fakes.Brain(), deny=True)
    try:
        assert _pair("um", "ultimaker", fu.port) == 1
        assert "not allowed" in capsys.readouterr().err
        assert "um" not in config.load_all() and config.get_secret("um") is None
    finally:
        fu.close()


def test_ultimaker_never_uploads_because_its_api_would_start_the_print(tmp_path, tls_cert):
    rig = make_rig("ultimaker", tls_cert)
    try:
        f = tmp_path / "part.gcode"
        f.write_bytes(GCODE)
        with pytest.raises(Unsupported, match="USB stick"):
            rig.adapter().upload(f, "part-0123456789ab.gcode")
        assert not rig.brain.files and not rig.brain.started
    finally:
        for c in rig.closers:
            c()


# ---------------------------------------------------------------- PrusaLink digest auth


@pytest.fixture
def prusa_digest():
    brain = fakes.Brain(secret=None)
    srv = fakes.serve_http(brain, fakes.prusalink_routes, digest_users={"maker": SECRET})
    cfg = PrinterConfig(name="mk4", type="prusalink", host="127.0.0.1", port=srv.server_address[1])
    yield brain, srv, cfg
    srv.shutdown()


def test_prusalink_digest_status_upload_and_pause(prusa_digest, tmp_path):
    brain, _, cfg = prusa_digest
    a = build(cfg, SECRET)
    assert a.status().state == "idle"
    f = tmp_path / "part.gcode"
    f.write_bytes(GCODE)
    a.upload(f, "part-0123456789ab.gcode")
    assert brain.files["part-0123456789ab.gcode"] == GCODE and not brain.started
    brain.start("part-0123456789ab.gcode")
    a.pause()
    assert a.status().state == "paused"
    assert a.snapshot(tmp_path / "cam.jpg").read_bytes()[:3] == b"\xff\xd8\xff"


def test_prusalink_digest_wrong_password_and_custom_user(prusa_digest):
    _, _, cfg = prusa_digest
    with pytest.raises(AuthFailed):
        build(cfg, "wrong").status()
    cfg.extra["user"] = "someone"
    with pytest.raises(AuthFailed):
        build(cfg, SECRET).status()


def test_prusalink_probe_finds_a_digest_printer(prusa_digest):
    _, srv, _ = prusa_digest
    info = ADAPTERS["prusalink"].probe("127.0.0.1", srv.server_address[1])
    assert info and info["type"] == "prusalink"


def test_prusalink_digest_answer_never_goes_to_another_host(prusa_digest, tmp_path):
    """A --camera-url relay that asks for digest auth must get no answer and no API key."""
    _, _, cfg = prusa_digest
    seen = []

    def relay(h, method):
        seen.append(dict(h.headers))
        fakes.digest_challenge(h, "Printer API")

    srv = fakes.serve_http(fakes.Brain(), relay)
    try:
        cfg.camera_url = f"http://localhost:{srv.server_address[1]}/snap.jpg"
        with pytest.raises(AuthFailed):
            build(cfg, SECRET).snapshot(tmp_path / "x.jpg")
    finally:
        srv.shutdown()
    assert seen
    for headers in seen:
        assert "Authorization" not in headers and SECRET not in str(headers)


# ---------------------------------------------------------------- FlashForge


def test_flashforge_upload_says_print_now_false(tls_cert, tmp_path):
    rig = make_rig("flashforge", tls_cert)
    try:
        upload_job(rig, tmp_path)
        (h,) = rig.fake.upload_headers
        assert h["printnow"] == "false" and not rig.brain.started
    finally:
        for c in rig.closers:
            c()


def test_flashforge_status_falls_back_to_the_legacy_port(tls_cert):
    rig = make_rig("flashforge", tls_cert)
    try:
        rig.fake.http.shutdown()
        rig.fake.http.server_close()
        st = rig.adapter().status()
        assert st.state == "idle" and st.nozzle_c == 215.0 and st.bed_c == 60.0
        assert "legacy port" in st.message
    finally:
        rig.fake.tcp.shutdown()
        rig.fake.tcp.server_close()


def test_flashforge_paired_on_the_legacy_port_reads_and_pauses_but_never_uploads(tls_cert, tmp_path):
    rig = make_rig("flashforge", tls_cert)
    try:
        rig.cfg.port = rig.fake.legacy_port
        a = rig.adapter()
        assert a.pair()["state"] == "idle"
        assert rig.cfg.extra["api"] == "legacy"
        f = tmp_path / "part.gcode"
        f.write_bytes(GCODE)
        with pytest.raises(Unsupported, match="legacy port"):
            a.upload(f, "part-0123456789ab.gcode")
        rig.brain.put("x.gcode", GCODE)
        rig.brain.start("x.gcode")
        assert a.status().state == "printing"
        a.pause()
        assert a.status().state == "paused"
        assert ADAPTERS["flashforge"].probe("127.0.0.1", rig.fake.legacy_port)["type"] == "flashforge"
    finally:
        for c in rig.closers:
            c()


def test_flashforge_needs_the_serial_number(tls_cert):
    rig = make_rig("flashforge", tls_cert)
    try:
        rig.cfg.serial = ""
        with pytest.raises(ProtocolError, match="serial"):
            rig.adapter().status()
    finally:
        for c in rig.closers:
            c()


# ---------------------------------------------------------------- Duet


@pytest.mark.parametrize("kind,api", [("duet", "rr"), ("duet-dsf", "dsf")])
def test_duet_detects_its_api_and_closes_every_session(kind, api, tls_cert):
    rig = make_rig(kind, tls_cert)
    try:
        a = rig.adapter()
        assert a.pair()["api"] == api
        a.status()
        assert rig.fake.sessions == set()  # every short session was closed again
    finally:
        for c in rig.closers:
            c()


def test_duet_cancel_pauses_first_then_sends_m0(tls_cert, tmp_path):
    rig = make_rig("duet", tls_cert)
    try:
        a = rig.adapter()
        job = upload_job(rig, tmp_path)
        a.start(job, start_token(job))
        a.cancel()
        assert [c for c in rig.fake.codes if c in ("M25", "M0")] == ["M25", "M0"]
    finally:
        for c in rig.closers:
            c()


# ---------------------------------------------------------------- Repetier


def test_repetier_probe_never_keeps_the_key_from_printer_info(tls_cert, capsys, monkeypatch, no_broadcast):
    rig = make_rig("repetier", tls_cert)
    try:
        info = RepetierAdapter.probe("127.0.0.1", rig.cfg.port)
        assert info and rig.fake.GLOBAL_KEY not in json.dumps(info)
        monkeypatch.setattr(disc, "KNOWN_PORTS", [(rig.cfg.port, ["repetier"])])
        assert cli.main(["discover", "--host", "127.0.0.1", "--no-mdns"]) == 0
        assert rig.fake.GLOBAL_KEY not in capsys.readouterr().out
    finally:
        for c in rig.closers:
            c()


def test_repetier_with_several_printers_asks_for_the_slug(tls_cert):
    brain = fakes.Brain(secret=SECRET)
    fr = fakes.FakeRepetier(brain, printers=2)
    try:
        cfg = PrinterConfig(name="rep", type="repetier", host="127.0.0.1", port=fr.port)
        with pytest.raises(ConfigError, match="--serial") as err:
            build(cfg, SECRET).status()
        assert "Other_1" in str(err.value)
    finally:
        fr.close()


# ---------------------------------------------------------------- Elegoo: resin, payloads, known Cmds only


SDCP_CONFIRMED = {0, 128, 129, 130, 131, 258}


def test_elegoo_sends_only_confirmed_commands(tls_cert, tmp_path):
    rig = make_rig("elegoo", tls_cert)
    try:
        a = rig.adapter()
        job = upload_job(rig, tmp_path)
        a.start(job, start_token(job))
        a.pause()
        a.cancel()
        a.emergency_stop()
        assert set(rig.fake.cmds) <= SDCP_CONFIRMED
        (payload,) = rig.fake.starts
        assert payload == {"Filename": f"/local/{job.remote_name}", "StartLayer": 0, "Calibration_switch": 0,
                           "PrintPlatformType": 0, "Tlp_Switch": 0}
    finally:
        for c in rig.closers:
            c()


@pytest.fixture
def resin_rig(tls_cert):
    rig = make_rig("elegoo", tls_cert)
    rig.fake.resin = True
    rig.cfg.model = "elegoo-saturn-4-ultra"
    yield rig
    for c in rig.closers:
        c()


def test_resin_printer_takes_a_presliced_goo_and_refuses_gcode(resin_rig, tmp_path):
    a = resin_rig.adapter()
    goo = tmp_path / "ring.goo"
    goo.write_bytes(b"GOO-FAKE-PRESLICED" * 100)
    a.upload(goo, "ring-0123456789ab.goo")
    assert resin_rig.brain.files["ring-0123456789ab.goo"] == goo.read_bytes()
    assert not resin_rig.brain.started
    g = tmp_path / "part.gcode"
    g.write_bytes(GCODE)
    with pytest.raises(Refused, match="resin printer"):
        a.upload(g, "part-0123456789ab.gcode")
    assert a.status().state == "idle"
    with pytest.raises(ProtocolError, match="no camera"):
        a.snapshot(tmp_path / "cam.jpg")


def test_resin_start_payload_has_no_fdm_fields(resin_rig, tmp_path):
    from gantrel_printer import safety

    a = resin_rig.adapter()
    goo = tmp_path / "ring.goo"
    goo.write_bytes(b"GOO" * 50)
    sha = safety.file_sha256(goo)
    remote = a.upload(goo, f"ring-{sha[:12]}.goo")
    info = a.remote_info(remote)
    job = safety.new_job(resin_rig.cfg.name, goo, remote, identity=a.identity(), sha256=sha,
                         remote_size=info["size"], remote_mtime=info["mtime"])
    a.start(job, start_token(job))
    assert resin_rig.fake.starts == [{"Filename": f"/local/{remote}", "StartLayer": 0}]


def test_fdm_elegoo_refuses_resin_files(tls_cert, tmp_path):
    rig = make_rig("elegoo", tls_cert)
    try:
        f = tmp_path / "ring.ctb"
        f.write_bytes(b"CTB")
        with pytest.raises(Refused, match="FDM"):
            rig.adapter().upload(f, "ring-0123456789ab.ctb")
        assert not rig.brain.files
    finally:
        for c in rig.closers:
            c()


def test_cli_refuses_resin_files_for_non_resin_adapters(tmp_path, capsys, monkeypatch):
    import io

    brain = fakes.Brain(secret=SECRET)
    srv = fakes.serve_http(brain, fakes.moonraker_routes)
    try:
        monkeypatch.setattr("sys.stdin", io.StringIO(SECRET + "\n"))
        assert cli.main(["pair", "k", "--type", "moonraker", "--host", "127.0.0.1", "--port",
                         str(srv.server_address[1]), "--secret-stdin"]) == 0
        capsys.readouterr()
        f = tmp_path / "ring.goo"
        f.write_bytes(b"GOO")
        assert cli.main(["upload", str(f), "--printer", "k"]) == 1
        assert "resin" in capsys.readouterr().err
        assert not brain.files
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- Marlin on USB serial


@pytest.fixture
def marlin(request):
    opts = getattr(request, "param", {})
    brain = fakes.Brain()
    fm = fakes.FakeMarlin(brain, **opts)
    cfg = PrinterConfig(name="ender", type="serial", host=fm.path, port=115200, model="generic-marlin-220")
    yield brain, fm, cfg
    fm.close()


@pytest.mark.parametrize("marlin", [{"sd": False}], indirect=True)
def test_serial_without_sd_refuses_upload_and_never_streams(marlin, tmp_path):
    brain, fm, cfg = marlin
    a = build(cfg)
    assert a.status().state == "idle"
    assert a.pair()["sd_card"] is False
    f = tmp_path / "part.gcode"
    f.write_bytes(GCODE)
    with pytest.raises(Unsupported, match="never streams"):
        a.upload(f, "part-0123456789ab.gcode")
    assert not brain.files and not brain.started
    assert not any(ln.startswith(("G28", "G1", "M28")) for ln in fm.received)  # no G-code was streamed


def test_serial_upload_never_overwrites(marlin, tmp_path):
    brain, _, cfg = marlin
    a = build(cfg)
    f = tmp_path / "part.gcode"
    f.write_bytes(GCODE)
    name = a.upload(f, "part-0123456789ab.gcode")
    assert name == "01234567.GCO"
    before = dict(brain.files)
    with pytest.raises(Refused, match="never replace"):
        a.upload(f, "part-0123456789ab.gcode")
    assert brain.files == before


@pytest.mark.parametrize("marlin", [{"corrupt_line": 2}], indirect=True)
def test_serial_upload_resends_a_corrupted_line(marlin, tmp_path):
    brain, fm, cfg = marlin
    f = tmp_path / "part.gcode"
    f.write_bytes(GCODE)
    name = build(cfg).upload(f, "part-0123456789ab.gcode")
    assert fm.resends == 1
    assert brain.files[name] == b"G28\r\nG1 X10\r\n"


def test_serial_pause_marker_clears_when_resumed_at_the_printer(marlin, tmp_path):
    brain, _, cfg = marlin
    a = build(cfg)
    f = tmp_path / "part.gcode"
    f.write_bytes(GCODE)
    name = a.upload(f, "part-0123456789ab.gcode")
    brain.start(name)
    a.pause()
    assert a.status().state == "paused"
    # The person resumes it on the printer's screen, and Marlin's SD position moves past the paused one.
    brain.act("resume")
    a._marker.write_text(json.dumps({"file": name, "pos": 3}))
    assert a.status().state == "printing"
    assert not a._marker.exists()


def test_serial_unplugged_is_unreachable(tmp_path):
    from gantrel_printer.errors import Unreachable

    cfg = PrinterConfig(name="gone", type="serial", host=str(tmp_path / "ttyUSB9"), port=115200)
    with pytest.raises(Unreachable):
        build(cfg).status()


def test_serial_estop_works_while_the_board_is_heating(marlin):
    # Inside M109/M190 Marlin sends no ok for minutes; estop must not wait for a handshake.
    brain, fm, cfg = marlin
    fm.heating = True
    t0 = time.monotonic()
    msg = build(cfg).emergency_stop()
    assert time.monotonic() - t0 < 5
    assert fm.halted and "M112" in msg
    assert fm.received[0] == "M112"  # the first thing on the wire, before any M105


def test_serial_pause_waits_out_a_heating_board(marlin, tmp_path):
    brain, fm, cfg = marlin
    a = build(cfg)
    f = tmp_path / "part.gcode"
    f.write_bytes(GCODE)
    name = a.upload(f, "part-0123456789ab.gcode")
    brain.start(name)
    fm.heating = True
    threading.Timer(2.5, fm.finish_heating).start()
    a.pause()  # the handshake reads busy lines as alive and waits for the ok instead of giving up
    assert brain.state == "paused"


def test_serial_upload_during_an_sd_print_is_refused_and_the_print_goes_on(marlin, tmp_path):
    brain, fm, cfg = marlin
    a = build(cfg)
    f = tmp_path / "part.gcode"
    f.write_bytes(GCODE)
    name = a.upload(f, "part-0123456789ab.gcode")
    brain.start(name)
    g = tmp_path / "next.gcode"
    g.write_bytes(GCODE + b"G1 X20\n")
    with pytest.raises(Refused, match="printing from its SD card"):
        a.upload(g, "next-0123456789ab.gcode")
    assert brain.state == "printing"
    assert not any(ln.split()[0] in ("M21", "M28") for ln in fm.received[-5:])


def test_prusalink_digest_password_is_never_sent_as_an_api_key(prusa_digest, tmp_path, monkeypatch):
    from gantrel_printer import net

    brain, _, cfg = prusa_digest
    sent = []
    real = net.request

    def spy(method, url, *, headers=None, **kw):
        sent.append(dict(headers or {}))
        return real(method, url, headers=headers, **kw)

    monkeypatch.setattr(net, "request", spy)
    a = build(cfg, SECRET)
    assert a.status().state == "idle"
    f = tmp_path / "part.gcode"
    f.write_bytes(GCODE)
    a.upload(f, "part-0123456789ab.gcode")
    assert sent and all("X-Api-Key" not in h for h in sent)
    assert cfg.extra["auth"] == "digest"
