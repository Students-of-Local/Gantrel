"""Starting and resuming: the CLI has none; the token-gated adapter calls kept for the app's confirm
sheet still refuse without a valid token (docs/PRINTERS.md)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from gantrel_printer import cli, config, safety
from gantrel_printer.adapters import build
from gantrel_printer.config import PrinterConfig
from gantrel_printer.errors import SafetyError

import fakes

GCODE = b"; filament_type = PLA\n; estimated printing time (normal mode) = 12m 5s\nG28\n"


@pytest.fixture
def printer(tmp_path):
    """A fake Moonraker printer paired through the CLI (no secret, so subprocesses need no keyring)."""
    brain = fakes.Brain(secret=None)
    srv = fakes.serve_http(brain, fakes.moonraker_routes)
    port = srv.server_address[1]
    assert cli.main(["pair", "moon", "--type", "moonraker", "--host", "127.0.0.1", "--port", str(port)]) == 0
    yield brain
    srv.shutdown()


def upload(tmp_path, name="part.gcode", data=GCODE, printer="moon") -> safety.Job:
    f = tmp_path / name
    f.write_bytes(data)
    assert cli.main(["upload", str(f), "--printer", printer]) == 0
    return next(j for j in safety.list_jobs() if j.remote_name.startswith(name.rsplit(".", 1)[0] + "-"))


def app_start(job_id, token=None):
    """What the app's confirm sheet will do after a person confirms. Not reachable from the CLI."""
    try:
        job = safety.load_job(job_id)
        build(config.get(job.printer)).start(job, token)
        return 0
    except SafetyError as err:
        print(f"REFUSED: {err}", file=sys.stderr)
        return cli.EXIT_SAFETY


def app_resume(name, token=None):
    try:
        build(config.get(name)).resume(token)
        return 0
    except SafetyError as err:
        print(f"REFUSED: {err}", file=sys.stderr)
        return cli.EXIT_SAFETY


def test_start_refuses_without_a_token(printer, tmp_path, capsys):
    job = upload(tmp_path)
    assert app_start(job.id) == cli.EXIT_SAFETY
    assert "needs a confirm token" in capsys.readouterr().err
    assert not printer.started


def test_start_refuses_a_made_up_token(printer, tmp_path):
    job = upload(tmp_path)
    assert app_start(job.id, "aaaaaaaaaaaaaaaaaaaaaaaa") == cli.EXIT_SAFETY
    assert not printer.started


def test_token_for_a_different_file_is_refused_and_voided(printer, tmp_path, capsys):
    a = upload(tmp_path, "a.gcode")
    b = upload(tmp_path, "b.gcode", GCODE + b"G1 X5\n")
    token_a = safety.mint_token("start", "moon", a.id, a.sha256, a.identity)
    assert app_start(b.id, token_a) == cli.EXIT_SAFETY
    assert "different job" in capsys.readouterr().err
    # The failed use voided it: it can't start job a either.
    assert app_start(a.id, token_a) == cli.EXIT_SAFETY
    assert not printer.started


def test_token_bound_to_the_file_hash(printer, tmp_path):
    a = upload(tmp_path, "a.gcode")
    forged = safety.mint_token("start", "moon", a.id, "0" * 64, a.identity)
    with pytest.raises(SafetyError, match="different file"):
        safety.consume_token(forged, "start", "moon", a.id, a.sha256, a.identity)
    assert not printer.started


def test_token_for_a_different_printer_is_refused(printer, tmp_path, capsys):
    job = upload(tmp_path)
    other = safety.mint_token("start", "other-printer", job.id, job.sha256, job.identity)
    assert app_start(job.id, other) == cli.EXIT_SAFETY
    assert "different printer" in capsys.readouterr().err
    assert not printer.started


def test_job_for_another_printer_is_refused(printer, tmp_path):
    job = upload(tmp_path)
    printers = config.load_all()
    printers["moon2"] = PrinterConfig(name="moon2", type="moonraker", host="127.0.0.1",
                                      port=printers["moon"].port)
    config.save_all(printers)
    token = safety.mint_token("start", "moon2", job.id, job.sha256, job.identity)
    with pytest.raises(SafetyError, match="is for printer moon"):
        build(config.get("moon2")).start(job, token)
    assert not printer.started


def test_token_is_single_use(printer, tmp_path, capsys):
    job = upload(tmp_path)
    token = safety.mint_token("start", "moon", job.id, job.sha256, job.identity)
    assert app_start(job.id, token) == 0
    assert printer.events == [f"start:{job.remote_name}"]
    assert cli.main(["cancel", "moon"]) == 0
    capsys.readouterr()
    assert app_start(job.id, token) == cli.EXIT_SAFETY
    assert "already used" in capsys.readouterr().err
    assert printer.events == [f"start:{job.remote_name}", "cancel"]


def test_token_expires(printer, tmp_path, monkeypatch):
    job = upload(tmp_path)
    token = safety.mint_token("start", "moon", job.id, job.sha256, job.identity)
    real = time.time
    monkeypatch.setattr(safety.time, "time", lambda: real() + safety.TOKEN_TTL_S + 1)
    with pytest.raises(SafetyError, match="expired"):
        build(config.get("moon")).start(job, token)
    assert not printer.started


def test_file_changed_after_upload_is_refused(printer, tmp_path):
    job = upload(tmp_path)
    token = safety.mint_token("start", "moon", job.id, job.sha256, job.identity)
    (tmp_path / "part.gcode").write_bytes(GCODE + b"M104 S300\n")
    with pytest.raises(SafetyError, match="changed"):
        build(config.get("moon")).start(job, token)
    assert not printer.started


def test_resume_needs_a_token_but_pause_does_not(printer, tmp_path):
    job = upload(tmp_path)
    assert app_start(job.id, safety.mint_token("start", "moon", job.id, job.sha256, job.identity)) == 0
    assert cli.main(["pause", "moon"]) == 0
    assert printer.state == "paused"
    assert app_resume("moon") == cli.EXIT_SAFETY
    assert app_resume("moon", "nope") == cli.EXIT_SAFETY
    assert printer.state == "paused"


def test_klipper_running_a_macro_is_busy_and_start_is_refused(printer, tmp_path):
    """Seen live on the K2: print_stats says standby while a pre-print sequence heats and homes."""
    job = upload(tmp_path)
    printer.running_macro = True
    adapter = build(config.get("moon"))
    assert adapter.status().state == "busy"
    from gantrel_printer.errors import Refused

    with pytest.raises(Refused, match="is busy"):
        adapter.start(job, safety.mint_token("start", "moon", job.id, job.sha256, job.identity))
    assert not printer.started


def test_pair_refuses_to_replace_an_existing_name(printer, capsys):
    """sol-F5, second-G1: re-pairing an alias to another printer must not happen silently."""
    assert cli.main(["pair", "moon", "--type", "moonraker", "--host", "127.0.0.9", "--no-check"]) == cli.EXIT_SAFETY
    assert "already paired" in capsys.readouterr().err
    assert config.get("moon").host == "127.0.0.1"


def test_token_is_bound_to_the_printer_not_its_name(printer, tmp_path, capsys):
    """sol-F5, second-G1: confirm for k2, point k2 at another device, start must refuse."""
    job = upload(tmp_path)
    token = safety.mint_token("start", "moon", job.id, job.sha256, job.identity)
    printers = config.load_all()
    printers["moon"].port = printers["moon"].port + 1  # the name now means another device
    config.save_all(printers)
    assert app_start(job.id, token) == cli.EXIT_SAFETY
    assert "different printer" in capsys.readouterr().err
    # Even a job re-bound to the new device can't use a token confirmed for the old one.
    with pytest.raises(SafetyError, match="different printer"):
        safety.consume_token(safety.mint_token("start", "moon", job.id, job.sha256, job.identity), "start",
                             "moon", job.id, job.sha256, build(config.get("moon")).identity())
    assert not printer.started


def test_unpair_voids_jobs_and_tokens(printer, tmp_path):
    job = upload(tmp_path)
    safety.mint_token("start", "moon", job.id, job.sha256, job.identity)
    assert cli.main(["unpair", "moon"]) == 0
    assert safety.list_jobs() == []
    assert not list((config.state_dir() / "tokens").glob("*.json"))


def test_remote_names_carry_the_content_hash_and_never_overwrite(printer, tmp_path, capsys):
    """first-F1, sol-F1: two different files with the same requested name get different names on the
    printer, and uploading a name that already exists is refused."""
    a = upload(tmp_path, "part.gcode")
    (tmp_path / "other").mkdir()
    f = tmp_path / "other" / "part.gcode"
    f.write_bytes(GCODE + b"M104 S300\n")
    assert cli.main(["upload", str(f), "--printer", "moon"]) == 0
    names = sorted(printer.files)
    assert len(names) == 2 and a.remote_name in names
    assert all(n.startswith("part-") and n.endswith(".gcode") for n in names)
    capsys.readouterr()
    assert cli.main(["upload", str(tmp_path / "part.gcode"), "--printer", "moon"]) == cli.EXIT_SAFETY
    assert "never replace" in capsys.readouterr().err
    # Job a still starts exactly its own content.
    assert app_start(a.id, safety.mint_token("start", "moon", a.id, a.sha256, a.identity)) == 0
    assert printer.files[printer.current] == GCODE


def test_material_must_be_a_known_material(printer, tmp_path, capsys):
    """second-G2: --material reaches the confirm sheet, so only known names are stored."""
    f = tmp_path / "x.gcode"
    f.write_bytes(GCODE)
    bad = "PLA\x1b[2A\x1b[2KFile:     calibration-cube.gcode"
    assert cli.main(["upload", str(f), "--printer", "moon", "--material", bad]) == cli.EXIT_ERROR
    assert printer.files == {}
    # A file that names no material takes the argument, stored as the known name.
    plain = tmp_path / "plain.gcode"
    plain.write_bytes(b"G28\nG1 X10\n")
    assert cli.main(["upload", str(plain), "--printer", "moon", "--material", "petg"]) == 0
    assert safety.list_jobs()[0].material == "PETG"


def test_the_cli_has_no_way_to_start_or_resume_a_print(capsys):
    """Round 2 lead decision (first-F2, sol-sol-F8): the tools never start or resume a print."""
    subs = set(cli.parser()._subparsers._group_actions[0].choices)
    assert not subs & {"start", "resume", "confirm"}
    assert {"pause", "cancel", "estop"} <= subs
    for argv in (["start", "x"], ["resume", "moon"], ["confirm", "x"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 2
    import gantrel_printer.cli as mod
    src = Path(mod.__file__).read_text()
    assert "mint_token" not in src and ".start(" not in src and ".resume(" not in src


def test_upload_says_where_a_person_starts_it_and_starts_nothing(printer, tmp_path, capsys):
    f = tmp_path / "part.gcode"
    f.write_bytes(GCODE)
    assert cli.main(["upload", str(f), "--printer", "moon"]) == 0
    text = capsys.readouterr().out
    remote = next(iter(printer.files))
    assert "It has NOT started." in text
    assert f"Start {remote} on the printer's screen or in its web page at http://127.0.0.1:" in text
    assert "token" not in text and "confirm" not in text
    assert not printer.started


def test_paused_status_tells_the_person_to_resume_on_the_printer(printer, tmp_path, capsys):
    job = upload(tmp_path)
    assert app_start(job.id, safety.mint_token("start", "moon", job.id, job.sha256, job.identity)) == 0
    assert cli.main(["pause", "moon"]) == 0
    capsys.readouterr()
    assert cli.main(["status", "moon"]) == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert "Only they can resume it" in last and "confirm" not in last
    assert printer.state == "paused"
