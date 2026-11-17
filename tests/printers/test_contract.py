"""One contract suite, run against every adapter's fake printer (docs/PRINTERS.md, Adapter contract)."""

from __future__ import annotations

import hashlib
import socket
import ssl
from pathlib import Path
from dataclasses import dataclass, field

import pytest

from gantrel_printer import safety
from gantrel_printer.adapters import ADAPTERS, build
from gantrel_printer.adapters.base import resume_job_id
from gantrel_printer.config import PrinterConfig
from gantrel_printer.adapters.elegoo import ElegooAdapter
from gantrel_printer.adapters.usb_serial import card_lines
from gantrel_printer.errors import AuthFailed, SafetyError, Unreachable, Unsupported

import fakes

SECRET = "s3cret-code"
GCODE = b"; filament_type = PLA\n; estimated printing time (normal mode) = 1h 2m 3s\nG28\nG1 X10\n"


@dataclass
class Rig:
    type: str
    brain: fakes.Brain
    cfg: PrinterConfig
    secret: str | None
    closers: list = field(default_factory=list)
    stored: object = lambda data: data  # what the printer keeps for an uploaded file

    def adapter(self, secret="__same__"):
        return build(self.cfg, self.secret if secret == "__same__" else secret)


def make_rig(kind: str, tls_cert) -> Rig:
    brain = fakes.Brain(secret=SECRET)
    name = f"fake-{kind}"
    if kind == "moonraker":
        srv = fakes.serve_http(brain, fakes.moonraker_routes)
        cfg = PrinterConfig(name=name, type=kind, host="127.0.0.1", port=srv.server_address[1])
        return Rig(kind, brain, cfg, SECRET, [srv.shutdown])
    if kind == "octoprint":
        srv = fakes.serve_http(brain, fakes.octoprint_routes)
        cfg = PrinterConfig(name=name, type=kind, host="127.0.0.1", port=srv.server_address[1])
        return Rig(kind, brain, cfg, SECRET, [srv.shutdown])
    if kind == "prusalink":
        srv = fakes.serve_http(brain, fakes.prusalink_routes)
        cfg = PrinterConfig(name=name, type=kind, host="127.0.0.1", port=srv.server_address[1])
        return Rig(kind, brain, cfg, SECRET, [srv.shutdown])
    if kind == "creality":
        brain.secret = None
        fc = fakes.FakeCreality(brain)
        http = fc.http.server_address[1]
        cfg = PrinterConfig(name=name, type=kind, host="127.0.0.1", port=fc.port,
                            camera_url=f"http://127.0.0.1:{http}/", extra={"http_port": http})
        return Rig(kind, brain, cfg, None, [fc.close])
    if kind == "bambu":
        fb = fakes.FakeBambu(brain, "01S00FAKE", *tls_cert)
        # As after pair: the MQTT certificate is pinned.
        cfg = PrinterConfig(name=name, type=kind, host="127.0.0.1", port=fb.mqtt, serial="01S00FAKE",
                            model="bambu-p1s", tls_fingerprint=cert_fp(tls_cert[0]),
                            extra={"ftp_port": fb.ftp, "camera_port": fb.cam,
                                   "tls_pins": {str(p): cert_fp(tls_cert[0]) for p in (fb.mqtt, fb.ftp, fb.cam)}})
        rig = Rig(kind, brain, cfg, SECRET)
        rig.fake = fb
        return rig
    if kind == "elegoo":
        brain.secret = None
        fe = fakes.FakeElegoo(brain)
        http = fe.http.server_address[1]
        old_udp = ElegooAdapter.udp_port
        ElegooAdapter.udp_port = fe.udp_port  # the probe's UDP hello goes to the fake, never to port 3000

        def close():
            ElegooAdapter.udp_port = old_udp
            fe.close()

        cfg = PrinterConfig(name=name, type=kind, host="127.0.0.1", port=fe.port, model="elegoo-centauri-carbon",
                            extra={"http_port": http, "camera_port": http, "udp_port": fe.udp_port})
        rig = Rig(kind, brain, cfg, None, [close])
        rig.fake = fe
        return rig
    if kind in ("duet", "duet-dsf"):
        dsf = kind == "duet-dsf"
        if dsf:
            brain.secret = None  # DSF's default: no board password
        fd = fakes.FakeDuet(brain, dsf=dsf)
        cfg = PrinterConfig(name=name, type="duet", host="127.0.0.1", port=fd.port,
                            camera_url=f"http://127.0.0.1:{fd.port}/cam.jpg")
        rig = Rig("duet", brain, cfg, None if dsf else SECRET, [fd.close])
        rig.fake = fd
        return rig
    if kind == "flashforge":
        ff = fakes.FakeFlashForge(brain)
        cfg = PrinterConfig(name=name, type=kind, host="127.0.0.1", port=ff.port, serial=ff.SERIAL,
                            model="flashforge-adventurer-5m", extra={"legacy_port": ff.legacy_port})
        rig = Rig(kind, brain, cfg, SECRET, [ff.close])
        rig.fake = ff
        return rig
    if kind == "snapmaker":
        fs = fakes.FakeSnapmaker(brain)
        cfg = PrinterConfig(name=name, type=kind, host="127.0.0.1", port=fs.port, model="snapmaker-a350",
                            camera_url=f"http://127.0.0.1:{fs.port}/cam.jpg", extra={"has_secret": True})
        rig = Rig(kind, brain, cfg, SECRET, [fs.close])
        rig.fake = fs
        return rig
    if kind == "ultimaker":
        fu = fakes.FakeUltimaker(brain)
        cfg = PrinterConfig(name=name, type=kind, host="127.0.0.1", port=fu.port, model="ultimaker-s5",
                            extra={"auth_id": "fakeid", "camera_port": fu.port, "has_secret": True})
        rig = Rig(kind, brain, cfg, SECRET, [fu.close])
        rig.fake = fu
        return rig
    if kind == "repetier":
        fr = fakes.FakeRepetier(brain)
        cfg = PrinterConfig(name=name, type=kind, host="127.0.0.1", port=fr.port, serial=fr.SLUG)
        rig = Rig(kind, brain, cfg, SECRET, [fr.close])
        rig.fake = fr
        return rig
    if kind == "serial":
        brain.secret = None
        fm = fakes.FakeMarlin(brain)
        cam = fakes.serve_jpeg()
        cfg = PrinterConfig(name=name, type=kind, host=fm.path, port=115200, model="generic-marlin-220",
                            camera_url=f"http://127.0.0.1:{cam.server_address[1]}/cam.jpg")
        # Marlin keeps each line without its comment, ending in CR LF.
        rig = Rig(kind, brain, cfg, None, [fm.close, cam.shutdown],
                  stored=lambda data: b"".join(ln.encode() + b"\r\n" for ln in card_lines(data)))
        rig.fake = fm
        return rig
    raise AssertionError(kind)


# Every adapter, plus Duet on DSF (a Duet with a single-board computer speaks a different API).
RIG_KINDS = sorted(ADAPTERS) + ["duet-dsf"]
# Adapters that can't read back a file's details on the printer; the content-hash name is their guard.
NO_REMOTE_INFO = {"creality", "flashforge", "snapmaker", "ultimaker"}


@pytest.fixture(params=RIG_KINDS)
def rig(request, tls_cert):
    r = make_rig(request.param, tls_cert)
    yield r
    for c in r.closers:
        c()


def cert_fp(pem_path: str) -> str:
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(Path(pem_path).read_text())).hexdigest()


def upload_job(rig, tmp_path, name="part.gcode", data=GCODE):
    """What `gantrel-printer upload` does: content-hash name, upload, record the printer's copy.

    UltiMaker's API can't store a file without printing it, so its adapter refuses to upload; there the
    person copies the file over on a USB stick, which the fake's put() stands in for.
    """
    a = rig.adapter()
    f = tmp_path / name
    f.write_bytes(data)
    sha = safety.file_sha256(f)
    remote = f"{f.stem}-{sha[:12]}.gcode"
    if getattr(a, "uploads", True):
        remote = a.upload(f, remote)
    else:
        with pytest.raises(Unsupported):
            a.upload(f, remote)
        rig.brain.put(remote, data)
    info = a.remote_info(remote) if a.verifies_remote else {}
    return safety.new_job(rig.cfg.name, f, remote, material="PLA", identity=a.identity(), sha256=sha,
                          remote_size=info.get("size"), remote_mtime=info.get("mtime"))


def start_token(job):
    return safety.mint_token("start", job.printer, job.id, job.sha256, job.identity)


def start_print(rig, a, job):
    """Start with a confirm token, the app's path. Snapmaker and UltiMaker can't start a stored file over
    the network: there the adapter refuses even with a token, and the person starts it on the screen."""
    if getattr(a, "network_start", True):
        a.start(job, start_token(job))
        return
    with pytest.raises(Unsupported):
        a.start(job, start_token(job))
    assert not rig.brain.started
    assert rig.brain.start(job.remote_name)  # the person, at the printer


def test_every_adapter_has_a_fake():
    assert sorted(ADAPTERS) == ["bambu", "creality", "duet", "elegoo", "flashforge", "moonraker", "octoprint",
                                "prusalink", "repetier", "serial", "snapmaker", "ultimaker"]


def test_every_new_adapter_is_marked_fake_proven():
    for t in ("elegoo", "duet", "flashforge", "snapmaker", "ultimaker", "repetier", "serial"):
        assert ADAPTERS[t].proven == "fake"


def test_probe_finds_its_own_fake_and_not_the_others(rig, tls_cert):
    cls = ADAPTERS[rig.type]
    info = cls.probe(rig.cfg.host, rig.cfg.port, timeout=3)
    assert info and info["type"] == rig.type
    for other in sorted(ADAPTERS):
        if other == rig.type:
            continue
        o = make_rig(other, tls_cert)
        try:
            assert cls.probe(o.cfg.host, o.cfg.port, timeout=2) is None, f"{rig.type} probe matched {other}"
        finally:
            for c in o.closers:
                c()


def test_pair_and_status_idle(rig):
    a = rig.adapter()
    facts = a.pair()
    assert facts["state"] == "idle"
    st = a.status()
    assert st.state == "idle" and st.type == rig.type and st.printer == rig.cfg.name
    assert st.nozzle_c == 215.0 and st.bed_c == 60.0


def test_wrong_secret_is_refused(rig):
    if rig.secret is None:
        # Creality stock firmware has no LAN credentials; the adapter must not pretend to need one.
        assert not ADAPTERS[rig.type].needs_secret
        return
    with pytest.raises(AuthFailed):
        rig.adapter(secret="wrong").status()


def test_unreachable_printer(rig):
    s = socket.create_server(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    cfg = PrinterConfig(name="gone", type=rig.type, host="127.0.0.1", port=port, serial="X", tls_fingerprint="0" * 64)
    with pytest.raises(Unreachable):
        build(cfg, SECRET).status()


def test_upload_stores_the_exact_file_and_never_starts(rig, tmp_path):
    a = rig.adapter()
    if not getattr(a, "uploads", True):
        f = tmp_path / "part.gcode"
        f.write_bytes(GCODE)
        with pytest.raises(Unsupported, match="never starts a print"):
            a.upload(f, "part-0123456789ab.gcode")
        assert rig.brain.files == {} and not rig.brain.started
        return
    job = upload_job(rig, tmp_path)
    assert rig.brain.files[job.remote_name] == rig.stored(GCODE)
    assert not rig.brain.started
    assert rig.adapter().status().state == "idle"


def test_start_without_token_is_refused_and_nothing_starts(rig, tmp_path):
    job = upload_job(rig, tmp_path)
    with pytest.raises(SafetyError):
        rig.adapter().start(job, None)
    with pytest.raises(SafetyError):
        rig.adapter().start(job, "made-up-token")
    assert not rig.brain.started


def test_start_with_confirm_token_then_pause_resume_cancel(rig, tmp_path):
    a = rig.adapter()
    job = upload_job(rig, tmp_path)
    start_print(rig, a, job)
    assert rig.brain.events == [f"start:{job.remote_name}"]
    st = a.status()
    assert st.state == "printing" and st.file == job.remote_name and st.progress == pytest.approx(0.1)

    a.pause()  # safe: no token needed
    assert a.status().state == "paused"

    with pytest.raises(SafetyError):
        a.resume(None)
    assert a.status().state == "paused"
    st = a.status()
    a.resume(safety.mint_token("resume", rig.cfg.name, resume_job_id(st), "", a.identity()))
    assert a.status().state == "printing"

    a.cancel()
    # OctoPrint and Bambu report idle after a cancel; the others report cancelled.
    assert a.status().state in ("cancelled", "idle")
    assert rig.brain.events[-1] == "cancel"


def test_start_refused_while_printing_even_with_a_token(rig, tmp_path):
    a = rig.adapter()
    first = upload_job(rig, tmp_path, "one.gcode")
    start_print(rig, a, first)
    from gantrel_printer.errors import Refused

    if rig.type == "serial":
        # Marlin on USB refuses the upload itself during an SD print (touching the card could stop it).
        with pytest.raises(Refused):
            upload_job(rig, tmp_path, "two.gcode", GCODE + b"G1 X20\n")
        assert rig.brain.current == first.remote_name and rig.brain.state == "printing"
        return
    second = upload_job(rig, tmp_path, "two.gcode", GCODE + b"G1 X20\n")

    with pytest.raises(Refused):
        a.start(second, start_token(second))
    assert rig.brain.current == first.remote_name


def test_emergency_stop_stops_the_print(rig, tmp_path):
    a = rig.adapter()
    job = upload_job(rig, tmp_path)
    start_print(rig, a, job)
    said = a.emergency_stop()
    assert said and isinstance(said, str)
    assert a.status().state not in ("printing", "paused")
    assert rig.brain.events[-1] in ("estop", "cancel")


def test_camera_snapshot_is_a_jpeg(rig, tmp_path):
    a = rig.adapter()
    out = a.snapshot(tmp_path / "cam.jpg")
    assert out.read_bytes()[:3] == b"\xff\xd8\xff"
    cam = a.camera()
    assert SECRET not in cam.stream_url and SECRET not in cam.snapshot_url


def test_start_refused_when_the_printers_copy_was_replaced(rig, tmp_path):
    """first-F1, sol-F1: the token must not start different content stored under the job's name."""
    a = rig.adapter()
    job = upload_job(rig, tmp_path)
    token = start_token(job)
    rig.brain.put(job.remote_name, b"G28\nM104 S300\n; not what the person confirmed\n")
    if not a.verifies_remote:
        # These can't report file details; the content-hash name is the only guard.
        assert rig.type in NO_REMOTE_INFO
        return
    with pytest.raises(SafetyError, match="changed after it was uploaded"):
        a.start(job, token)
    assert not rig.brain.started


def test_same_size_replacement_is_caught_by_mtime(rig, tmp_path):
    a = rig.adapter()
    if not a.verifies_remote:
        return
    job = upload_job(rig, tmp_path)
    if job.remote_mtime is None:
        # Marlin's M20 lists sizes but no times. Same-size swaps need someone to rewrite the SD card by
        # hand under the content-hash name; the size check above still catches any other change.
        assert rig.type == "serial"
        return
    rig.brain.put(job.remote_name, rig.stored(GCODE.replace(b"X10", b"X99")))  # same size, new content
    with pytest.raises(SafetyError, match="changed"):
        a.start(job, start_token(job))
    assert not rig.brain.started


def test_camera_override_never_gets_printer_credentials(rig, tmp_path):
    """sol-F3: a --camera-url on another host (a go2rtc relay) must not receive the API key."""
    seen = []

    def relay(h, method):
        seen.append(dict(h.headers))
        h._send(200, fakes.JPEG, "image/jpeg")

    srv = fakes.serve_http(fakes.Brain(), relay)
    try:
        rig.cfg.camera_url = f"http://localhost:{srv.server_address[1]}/api/frame.jpeg?src=cam"
        rig.adapter().snapshot(tmp_path / "relay.jpg")
    finally:
        srv.shutdown()
    assert seen
    for headers in seen:
        assert "X-Api-Key" not in headers and SECRET not in str(headers)


def test_identity_changes_with_the_address(rig):
    a = rig.adapter()
    before = a.identity()
    rig.cfg.host = "127.0.0.2"
    assert rig.adapter().identity() != before


def test_bambu_checks_the_pin_on_every_connection_that_carries_the_access_code(tmp_path, tls_cert, tls_cert2):
    """sol-F2, first-F2: an interceptor that shows the real certificate on some connections and its own
    on others must never receive the access code on one of its own."""
    brain = fakes.Brain(secret=SECRET)
    fb = fakes.FakeBambu(brain, "01S00FAKE", *tls_cert, alt=tls_cert2)
    pin = cert_fp(tls_cert[0])
    cfg = PrinterConfig(name="bambu", type="bambu", host="127.0.0.1", port=fb.mqtt, serial="01S00FAKE",
                        tls_fingerprint=pin, model="bambu-p1s",
                        extra={"ftp_port": fb.ftp, "camera_port": fb.cam,
                               "tls_pins": {str(fb.ftp): pin, str(fb.cam): pin}})
    a = build(cfg, SECRET)
    f = tmp_path / "x.gcode"
    f.write_bytes(GCODE)
    refused = 0
    for call in (a.status, a.status, lambda: a.upload(f, "x.gcode"), lambda: a.upload(f, "x.gcode"),
                 lambda: a.snapshot(tmp_path / "c.jpg"), lambda: a.snapshot(tmp_path / "c.jpg")):
        try:
            call()
        except AuthFailed as err:
            assert "certificate" in str(err)
            refused += 1
    assert refused >= 3  # every connection that drew the impostor certificate was refused
    assert fb.logins, "the genuine connections should have logged in"
    assert all(idx == 0 for _, idx in fb.logins), fb.logins  # no secret ever went to the impostor


def test_bambu_wrong_pin_sends_nothing(tmp_path, tls_cert):
    brain = fakes.Brain(secret=SECRET)
    fb = fakes.FakeBambu(brain, "01S00FAKE", *tls_cert)
    cfg = PrinterConfig(name="bambu", type="bambu", host="127.0.0.1", port=fb.mqtt, serial="01S00FAKE",
                        tls_fingerprint="0" * 64, extra={"ftp_port": fb.ftp, "camera_port": fb.cam,
                                                         "tls_pins": {str(fb.ftp): "0" * 64, str(fb.cam): "0" * 64}})
    a = build(cfg, SECRET)
    for call in (a.status, lambda: a.snapshot(tmp_path / "c.jpg"), lambda: a.remote_info("x.gcode")):
        with pytest.raises(AuthFailed, match="certificate"):
            call()
    assert fb.logins == []


def test_bambu_pair_pins_every_service_before_any_credential(tmp_path, tls_cert, tls_cert2):
    """sol-sol-F2: start from the config pair produces. If FTPS or the camera later show another
    certificate, the access code must not be sent there."""
    brain = fakes.Brain(secret=SECRET)
    fb = fakes.FakeBambu(brain, "01S00FAKE", *tls_cert)
    cfg = PrinterConfig(name="bambu", type="bambu", host="127.0.0.1", port=fb.mqtt, serial="01S00FAKE",
                        model="bambu-p1s", extra={"ftp_port": fb.ftp, "camera_port": fb.cam})
    a = build(cfg, SECRET)
    a.pair()
    pin = cert_fp(tls_cert[0])
    assert cfg.tls_fingerprint == pin
    assert cfg.extra["tls_pins"] == {str(fb.mqtt): pin, str(fb.ftp): pin, str(fb.cam): pin}
    logins_after_pair = list(fb.logins)
    fb.ctxs = [fakes._tls_server_ctx(*tls_cert2)]  # an interceptor answers from now on
    f = tmp_path / "x.gcode"
    f.write_bytes(GCODE)
    for call in (a.status, lambda: a.upload(f, "x.gcode"), lambda: a.snapshot(tmp_path / "c.jpg")):
        with pytest.raises(AuthFailed, match="certificate"):
            call()
    assert fb.logins == logins_after_pair


def test_bambu_refuses_a_service_with_no_pin(tmp_path, tls_cert):
    brain = fakes.Brain(secret=SECRET)
    fb = fakes.FakeBambu(brain, "01S00FAKE", *tls_cert)
    pin = cert_fp(tls_cert[0])
    cfg = PrinterConfig(name="bambu", type="bambu", host="127.0.0.1", port=fb.mqtt, serial="01S00FAKE",
                        model="bambu-p1s", tls_fingerprint=pin,
                        extra={"ftp_port": fb.ftp, "camera_port": fb.cam, "tls_pins": {str(fb.mqtt): pin}})
    a = build(cfg, SECRET)
    f = tmp_path / "x.gcode"
    f.write_bytes(GCODE)
    for call in (lambda: a.upload(f, "x.gcode"), lambda: a.snapshot(tmp_path / "c.jpg")):
        with pytest.raises(AuthFailed, match="no pinned TLS certificate"):
            call()
    assert [svc for svc, _ in fb.logins] == []
