"""Confirm-to-start safety: no agent or script can start or resume a print (docs/PRINTERS.md)."""

from __future__ import annotations

import os
import pty
import re
import select
import subprocess
import sys
import time

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


def run_cli(*args, **kw):
    return subprocess.run([sys.executable, "-m", "gantrel_printer.cli", *args], capture_output=True, text=True,
                          env=os.environ.copy(), timeout=60, **kw)


def test_start_refuses_without_a_token(printer, tmp_path, capsys):
    job = upload(tmp_path)
    assert cli.main(["start", job.id]) == cli.EXIT_SAFETY
    assert "needs a confirm token" in capsys.readouterr().err
    assert not printer.started


def test_start_refuses_a_made_up_token(printer, tmp_path):
    job = upload(tmp_path)
    assert cli.main(["start", job.id, "--token", "aaaaaaaaaaaaaaaaaaaaaaaa"]) == cli.EXIT_SAFETY
    assert not printer.started


def test_token_for_a_different_file_is_refused_and_voided(printer, tmp_path, capsys):
    a = upload(tmp_path, "a.gcode")
    b = upload(tmp_path, "b.gcode", GCODE + b"G1 X5\n")
    token_a = safety.mint_token("start", "moon", a.id, a.sha256, a.identity)
    assert cli.main(["start", b.id, "--token", token_a]) == cli.EXIT_SAFETY
    assert "different job" in capsys.readouterr().err
    # The failed use voided it: it can't start job a either.
    assert cli.main(["start", a.id, "--token", token_a]) == cli.EXIT_SAFETY
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
    assert cli.main(["start", job.id, "--token", other]) == cli.EXIT_SAFETY
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
    assert cli.main(["start", job.id, "--token", token]) == 0
    assert printer.events == [f"start:{job.remote_name}"]
    assert cli.main(["cancel", "moon"]) == 0
    capsys.readouterr()
    assert cli.main(["start", job.id, "--token", token]) == cli.EXIT_SAFETY
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
    assert cli.main(["start", job.id, "--token", safety.mint_token("start", "moon", job.id, job.sha256, job.identity)]) == 0
    assert cli.main(["pause", "moon"]) == 0
    assert printer.state == "paused"
    assert cli.main(["resume", "moon"]) == cli.EXIT_SAFETY
    assert cli.main(["resume", "moon", "--token", "nope"]) == cli.EXIT_SAFETY
    assert printer.state == "paused"


def test_confirm_refuses_without_a_tty(printer, tmp_path):
    job = upload(tmp_path)
    # Like an agent's bash tool: stdin and stdout are pipes. It must refuse before asking anything.
    r = run_cli("confirm", job.id, input="moon\n")
    assert r.returncode == cli.EXIT_SAFETY, r
    assert "REFUSED" in r.stderr and "person at a terminal" in r.stderr
    assert not list((config.state_dir() / "tokens").glob("*.json"))
    r = run_cli("confirm", "--resume", "moon", input="moon\n")
    assert r.returncode == cli.EXIT_SAFETY


def test_confirm_refuses_without_a_controlling_terminal():
    # New session, so /dev/tty can't be opened, as for a daemon or a detached agent.
    code = ("from gantrel_printer import safety\n"
            "try:\n    safety.open_tty()\nexcept safety.SafetyError as e:\n    print('refused:', e)\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, start_new_session=True,
                       stdin=subprocess.DEVNULL, timeout=30)
    assert "refused: no terminal is attached" in r.stdout, r


def _confirm_on_a_pty(args: list[str], answer: str) -> tuple[int, str]:
    """Run confirm the way a person would: a real terminal that is the process's controlling tty."""
    master, slave = pty.openpty()
    # The child makes the pty its controlling terminal, then execs the CLI. No preexec_fn: running
    # Python between fork and exec is unsafe while the fake printer's threads are alive.
    boot = ("import fcntl, os, sys, termios; fcntl.ioctl(0, termios.TIOCSCTTY, 0); "
            "os.execv(sys.executable, [sys.executable, '-m', 'gantrel_printer.cli', *sys.argv[1:]])")
    p = subprocess.Popen([sys.executable, "-c", boot, *args], stdin=slave, stdout=slave, stderr=slave,
                         start_new_session=True, env=os.environ.copy())
    os.close(slave)
    out, sent = b"", False
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        r, _, _ = select.select([master], [], [], 0.5)
        if r:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        if not sent and b"Anything else cancels" in out:
            os.write(master, answer.encode() + b"\n")
            sent = True
        if p.poll() is not None and not r:
            break
    os.close(master)
    return p.wait(10), out.decode("utf-8", "replace")


def test_confirm_at_a_real_terminal_mints_a_working_token(printer, tmp_path):
    job = upload(tmp_path)
    code, text = _confirm_on_a_pty(["confirm", job.id], "moon")
    assert code == 0, text
    for want in ("Printer:  moon", f"File:     {job.remote_name}", "Material: PLA", "Time:     12m 5s estimated"):
        assert want in text, text
    m = re.search(rf"gantrel-printer start {job.id} --token (\S+)", text)
    assert m, text
    assert not printer.started  # confirm alone never starts anything
    assert cli.main(["start", job.id, "--token", m.group(1)]) == 0
    assert printer.events == [f"start:{job.remote_name}"]


def test_confirm_with_the_wrong_name_mints_nothing(printer, tmp_path):
    job = upload(tmp_path)
    code, text = _confirm_on_a_pty(["confirm", job.id], "yes")
    assert code == cli.EXIT_SAFETY, text
    assert "not confirmed" in text
    assert "--token" not in text
    assert not list((config.state_dir() / "tokens").glob("*.json"))
    assert not printer.started


def test_confirm_resume_at_a_terminal(printer, tmp_path):
    job = upload(tmp_path)
    assert cli.main(["start", job.id, "--token", safety.mint_token("start", "moon", job.id, job.sha256, job.identity)]) == 0
    assert cli.main(["pause", "moon"]) == 0
    code, text = _confirm_on_a_pty(["confirm", "--resume", "moon"], "moon")
    assert code == 0, text
    token = re.search(r"gantrel-printer resume moon --token (\S+)", text).group(1)
    assert cli.main(["resume", "moon", "--token", token]) == 0
    assert printer.state == "printing"


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
    assert cli.main(["start", job.id, "--token", token]) == cli.EXIT_SAFETY
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
    assert cli.main(["start", a.id, "--token", safety.mint_token("start", "moon", a.id, a.sha256, a.identity)]) == 0
    assert printer.files[printer.current] == GCODE


def test_material_must_be_a_known_material(printer, tmp_path, capsys):
    """second-G2: --material reaches the confirm sheet, so only known names are stored."""
    f = tmp_path / "x.gcode"
    f.write_bytes(GCODE)
    bad = "PLA\x1b[2A\x1b[2KFile:     calibration-cube.gcode"
    assert cli.main(["upload", str(f), "--printer", "moon", "--material", bad]) == cli.EXIT_ERROR
    assert printer.files == {}
    assert cli.main(["upload", str(f), "--printer", "moon", "--material", "petg"]) == 0
    assert safety.list_jobs()[0].material == "PETG"


def test_confirm_sheet_strips_control_characters(printer, tmp_path):
    """second-G2: a path holding escape codes or newlines can't rewrite the confirm sheet."""
    evil = tmp_path / "a\x1b[2A\x1b[2Kb\nFile:     fake.gcode"
    evil.mkdir()
    f = evil / "part.gcode"
    f.write_bytes(GCODE)
    assert cli.main(["upload", str(f), "--printer", "moon"]) == 0
    job = safety.list_jobs()[0]
    code, text = _confirm_on_a_pty(["confirm", job.id], "no")
    assert code == cli.EXIT_SAFETY, text
    assert "\x1b" not in text
    assert not any(line.startswith("File:     fake.gcode") for line in text.splitlines())
    assert "a?[2A?[2Kb?File:" in text
