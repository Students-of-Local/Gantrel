"""The printers skill must match the CLI it teaches and keep the safety rules."""

import re
from pathlib import Path

from gantrel_printer.cli import parser

SKILL = Path(__file__).resolve().parents[3] / "skills" / "plugin" / "printers" / "SKILL.md"


def test_frontmatter_and_sections():
    text = SKILL.read_text()
    assert re.match(r"^---\nname: printers\ndescription: .+\n---\n", text)
    for section in ("## Goal", "## Inputs", "## Steps to print", "## Rules and checks", "## Sources"):
        assert section in text


def test_every_command_in_the_skill_exists():
    text = SKILL.read_text()
    subs = set(parser()._subparsers._group_actions[0].choices)
    used = set(re.findall(r"(?:`|^ {4,})gantrel-printer ([a-z]+)", text, re.M))
    assert used and used <= subs, used - subs


def test_skill_keeps_the_safety_rules():
    text = SKILL.read_text()
    assert "You never start or resume a print yourself" in text
    assert "Never start or resume a print by any route but `print_start` and `print_resume`" in text
    assert "Never say the print started\n     unless the tool's reply says `started`" in text
    assert "a card will appear" in text
    for gone in ("gantrel-printer confirm", "gantrel-printer start", "gantrel-printer resume", "--token"):
        assert gone not in text
    assert "Pause needs no permission" in text
    assert "command -v gantrel-printer" in text
    assert chr(0x2014) not in text and chr(0x2013) not in text  # no em or en dashes


def test_stop_requests_come_before_the_print_workflow():
    """sol-F6: a stop request must not be blocked by the print steps (step 4 used to say 'stop')."""
    text = SKILL.read_text()
    stop = text.index("## First: is this a stop request?")
    steps = text.index("## Steps to print")
    assert stop < steps
    first = text[stop:steps]
    for cmd in ("gantrel-printer estop <name>", "gantrel-printer pause <name>", "gantrel-printer cancel <name>"):
        assert cmd in first
    assert "tell the person and stop." not in text


def test_skill_teaches_every_calibration_kind_and_that_it_never_starts():
    from gantrel_printer.calibrate import KINDS

    text = SKILL.read_text()
    assert "gantrel-printer calibrate <name> <kind>" in text
    for kind in KINDS:
        assert f"| {kind} |" in text
    section = text[text.index("## Calibration prints"):text.index("## Rules and checks")]
    assert "It never starts a print" in section and "--upload" in section


def test_watch_section_uses_real_flags_and_keeps_the_guard_honest():
    text = SKILL.read_text()
    assert "gantrel-printer watch <name> --guard --timelapse --background" in text
    # No measured model passed the gate, so the default step leaves the guard off (docs/PRINTERS.md).
    step = text[text.index("9. Watch"):text.index("10. ")]
    assert "--guard" not in step and "--timelapse --background" in step
    assert "experimental" in text
    watch = parser()._subparsers._group_actions[0].choices["watch"]
    flags = {o for a in watch._actions for o in a.option_strings}
    used = set(re.findall(r"gantrel-printer watch <name>((?: --[a-z-]+)+)", text))
    assert used
    for group in used:
        assert set(group.split()) <= flags, set(group.split()) - flags
    assert "It never resumes, starts or cancels" in text
    assert "## Watching a print" in text
