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
    assert "Students-of-Local/Gantrel" in text and "profiles/community/" in text
    assert "Never sign in for them" in text


def test_plugin_command():
    m = tomllib.loads((ROOT / "plugin.toml").read_text())
    cmds = {c["name"]: c for c in m["commands"]}
    assert cmds["setup-printer"]["skill"] == "printer-setup"


def section(text, head):
    return text.split(head, 1)[1].split("\n## ", 1)[0]


def test_save_steps_put_in_the_numbers_calibration_measured():
    # temp-tower sets the nozzle temperature, retraction the retraction length, flow the flow ratio (calibrate.py);
    # a pack that can hold only the defaults plus a note is not a tuned setup.
    save = section(SKILL.read_text(), "## Save the setup as a pack")
    for field in ("`nozzle_c`", "`first_layer_nozzle_c`", "`retraction_mm`", "`flow_ratio`", "`calibrated`"):
        assert field in save, field
    assert "Add nothing else by hand" not in save
    assert "never change a number just to pass the check" in save
    readme = (ROOT / "profiles" / "community" / "README.md").read_text()
    for field in ("`nozzle_c`", "`retraction_mm`", "`flow_ratio`"):
        assert field in readme, field


def test_share_steps_copy_the_file_by_its_full_path_and_handle_an_existing_fork():
    # pack export writes <id>.json in the current folder and prints the path as given, so after cd a relative
    # <file> no longer resolves and cp fails.
    share = section(SKILL.read_text(), "## Share a pack")
    block = [ln.strip() for ln in share.splitlines() if ln.startswith("       ") and ln.strip()]
    first_cd = next(i for i, ln in enumerate(block) if ln.startswith("cd "))
    assert any(ln.startswith("realpath ") for ln in block[:first_cd]), block
    cp = next(ln for ln in block if ln.startswith("cp "))
    assert cp.startswith("cp <full path> "), cp
    assert "already exists" in share
