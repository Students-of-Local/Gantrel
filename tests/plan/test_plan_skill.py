"""The print-plan skill must match the CLI it teaches."""

import re
import tomllib
from pathlib import Path

from gantrel_printer.cli import parser

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "plugin" / "print-plan" / "SKILL.md"


def test_frontmatter_and_sections():
    text = SKILL.read_text()
    assert re.match(r"^---\nname: print-plan\ndescription: .+\n---\n", text)
    for section in ("## Goal", "## Inputs", "## Steps", "## Rules and checks", "## Sources"):
        assert section in text


def test_every_command_and_flag_exists():
    text = SKILL.read_text()
    subs = parser()._subparsers._group_actions[0].choices
    used = set(re.findall(r"(?:`|^ {4,})gantrel-printer ([a-z]+)", text, re.M))
    assert {"plan", "check"} <= used and used <= set(subs), used - set(subs)
    for cmd in ("plan", "check"):
        flags = {s for a in subs[cmd]._actions for s in a.option_strings}
        for line in re.findall(rf"gantrel-printer {cmd} [^\n`]+", text):
            for flag in re.findall(r"--[a-z-]+", line):
                assert flag in flags, (cmd, flag)
    for flag in ("--load-axis", "--write-oriented", "--goal"):
        assert flag in text


def test_skill_names_every_filament_and_keeps_the_rules():
    from gantrel_printer import materials
    text = SKILL.read_text()
    for fam in materials.FAMILIES:
        assert fam in text, fam
    assert "never starts a print" in text and "Never start or resume a print" in text
    assert "--force" in text and "Do not use `--force`" in text
    assert "command -v gantrel-printer" in text
    assert chr(0x2014) not in text and chr(0x2013) not in text


def test_plugin_command_points_at_the_skill():
    m = tomllib.loads((ROOT / "plugin.toml").read_text())
    cmds = {c["name"]: c for c in m["commands"]}
    assert cmds["plan"]["skill"] == "print-plan"
