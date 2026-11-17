"""The printers skill must match the CLI it teaches and keep the safety rules."""

import re
from pathlib import Path

from gantrel_printer.cli import parser

SKILL = Path(__file__).resolve().parents[3] / "skills" / "plugin" / "printers" / "SKILL.md"


def test_frontmatter_and_sections():
    text = SKILL.read_text()
    assert re.match(r"^---\nname: printers\ndescription: .+\n---\n", text)
    for section in ("## Goal", "## Inputs", "## Steps", "## Rules and checks", "## Sources"):
        assert section in text


def test_every_command_in_the_skill_exists():
    text = SKILL.read_text()
    subs = set(parser()._subparsers._group_actions[0].choices)
    used = set(re.findall(r"(?:`|^ {4,})gantrel-printer ([a-z]+)", text, re.M))
    assert used and used <= subs, used - subs


def test_skill_keeps_the_safety_rules():
    text = SKILL.read_text()
    assert "You can never start or resume a print" in text
    assert "Never run `confirm`" in text
    assert "Pause needs no permission" in text
    assert "command -v gantrel-printer" in text
    assert chr(0x2014) not in text and chr(0x2013) not in text  # no em or en dashes
