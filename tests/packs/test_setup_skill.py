"""The printer-setup skill must match the CLI it teaches and keep the safety and sharing rules."""

import re
import tomllib
from pathlib import Path

from gantrel_printer.cli import parser

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "plugin" / "printer-setup" / "SKILL.md"


def subcommands(p):
    return p._subparsers._group_actions[0].choices


def flags(p):
    return {s for a in p._actions for s in a.option_strings}


def test_frontmatter_and_sections():
    text = SKILL.read_text()
    assert re.match(r"^---\nname: printer-setup\ndescription: .+\n---\n", text)
    for section in ("## Goal", "## Inputs", "## Steps: a new printer", "## Save the setup as a pack", "## Share a pack",
                    "## Use a pack someone else shared", "## Rules and checks", "## Sources"):
        assert section in text, section


def test_every_command_and_flag_exists():
    text = SKILL.read_text()
    subs = subcommands(parser())
    used = set(re.findall(r"(?:`|^ {4,})gantrel-printer ([a-z]+)", text, re.M))
    assert used <= set(subs), used - set(subs)
    assert {"discover", "pair", "status", "camera", "filaments", "calibrate", "plan", "check", "slice", "upload",
            "pack"} <= used
    for line in re.findall(r"(?:`|^ {4,})gantrel-printer ([a-z]+(?: [a-z]+)?)([^\n`]*)", text, re.M):
        cmd, rest = line
        words = cmd.split()
        p = subs[words[0]]
        if words[0] == "pack":
            assert len(words) == 2 and words[1] in subcommands(p), cmd
            p = subcommands(p)[words[1]]
        for flag in re.findall(r"--[a-z-]+", rest):
            assert flag in flags(p), (cmd, flag)
    for sub in ("export", "check", "show", "import", "list", "remove"):
        assert f"gantrel-printer pack {sub}" in text, sub
    for flag in ("--accept", "--accept-findings", "--replace", "--author", "--upload"):
        assert flag in text, flag


def test_skill_keeps_the_rules():
    text = SKILL.read_text()
    assert "You never start or resume a print" in text and "Never start or resume a print, by any route" in text
    assert "The person types it" in text and "Never ask them to tell you the code" in text
    assert "Never share a pack without the person's yes" in text
    assert "Never import a pack that says REFUSED" in text
    assert "baney75/Gantrel" in text and "profiles/community/" in text
    assert "Never sign in for them" in text


def test_plugin_command():
    m = tomllib.loads((ROOT / "plugin.toml").read_text())
    cmds = {c["name"]: c for c in m["commands"]}
    assert cmds["setup-printer"]["skill"] == "printer-setup"
