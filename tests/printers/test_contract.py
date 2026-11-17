"""One contract suite, run against every adapter's fake printer (docs/PRINTERS.md, Adapter contract)."""

from __future__ import annotations

import socket
from dataclasses import dataclass, field

import pytest

from gantrel_printer import safety
from gantrel_printer.adapters import ADAPTERS, build
from gantrel_printer.adapters.base import resume_job_id
from gantrel_printer.config import PrinterConfig
from gantrel_printer.errors import AuthFailed, SafetyError, Unreachable

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
        cfg = PrinterConfig(name=name, type=kind, host="127.0.0.1", port=fb.mqtt, serial="01S00FAKE",
                            model="bambu-p1s", extra={"ftp_port": fb.ftp, "camera_port": fb.cam})
        rig = Rig(kind, brain, cfg, SECRET)
        rig.fake = fb
        return rig
    raise AssertionError(kind)


@pytest.fixture(params=sorted(ADAPTERS))
def rig(request, tls_cert):
    r = make_rig(request.param, tls_cert)
    yield r
    for c in r.closers:
        c()


def upload_job(rig, tmp_path, name="part.gcode", data=GCODE):
    f = tmp_path / name
    f.write_bytes(data)
    remote = rig.adapter().upload(f, name)
    return safety.new_job(rig.cfg.name, f, remote, material="PLA")


def start_token(job):
    return safety.mint_token("start", job.printer, job.id, job.sha256)


def test_every_adapter_has_a_fake():
    assert sorted(ADAPTERS) == ["bambu", "creality", "moonraker", "octoprint", "prusalink"]


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
    cfg = PrinterConfig(name="gone", type=rig.type, host="127.0.0.1", port=port, serial="X")
    with pytest.raises(Unreachable):
        build(cfg, SECRET).status()


def test_upload_stores_the_exact_file_and_never_starts(rig, tmp_path):
    job = upload_job(rig, tmp_path)
    assert rig.brain.files[job.remote_name] == GCODE
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
    a.start(job, start_token(job))
    assert rig.brain.events == [f"start:{job.remote_name}"]
    st = a.status()
    assert st.state == "printing" and st.file == job.remote_name and st.progress == pytest.approx(0.1)

    a.pause()  # safe: no token needed
    assert a.status().state == "paused"

    with pytest.raises(SafetyError):
        a.resume(None)
    assert a.status().state == "paused"
    st = a.status()
    a.resume(safety.mint_token("resume", rig.cfg.name, resume_job_id(st), ""))
    assert a.status().state == "printing"

    a.cancel()
    # OctoPrint and Bambu report idle after a cancel; the others report cancelled.
    assert a.status().state in ("cancelled", "idle")
    assert rig.brain.events[-1] == "cancel"


def test_start_refused_while_printing_even_with_a_token(rig, tmp_path):
    a = rig.adapter()
    first = upload_job(rig, tmp_path, "one.gcode")
    a.start(first, start_token(first))
    second = upload_job(rig, tmp_path, "two.gcode", GCODE + b"G1 X20\n")
    from gantrel_printer.errors import Refused

    with pytest.raises(Refused):
        a.start(second, start_token(second))
    assert rig.brain.current == "one.gcode"


def test_emergency_stop_stops_the_print(rig, tmp_path):
    a = rig.adapter()
    job = upload_job(rig, tmp_path)
    a.start(job, start_token(job))
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
