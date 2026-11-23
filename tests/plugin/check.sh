#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"

python3 - <<'PY'
import os, pathlib, re, sys, tomllib
m = tomllib.loads(pathlib.Path("plugin.toml").read_text())
allowed = {"id", "name", "version", "description", "skills", "prompts", "commands", "capabilities", "mcp"}
assert set(m) <= allowed, f"unknown keys: {set(m) - allowed}"
assert m["id"] == "gantrel", m["id"]
assert m["name"] == "Gantrel", m["name"]
assert re.fullmatch(r"\d+\.\d+\.\d+", m["version"]), m["version"]
assert m["description"].strip()
# The MCP server, and the ask-user grant its print_start card needs (docs/MCP.md). Nothing else.
assert m["capabilities"] == ["mcp-server", "ask-user"], m["capabilities"]
mcp = m["mcp"]
assert set(mcp) <= {"command", "args", "env"}, f"unknown [mcp] keys: {set(mcp) - {'command', 'args', 'env'}}"
assert mcp["command"] == "bin/gantrel-mcp", mcp["command"]
assert not mcp.get("env"), "[mcp] env would keep Local Studio from registering the ask variables"
cmd = pathlib.Path(mcp["command"])
assert cmd.is_file() and not cmd.is_symlink() and os.access(cmd, os.X_OK), f"{cmd} must be an executable file"
assert cmd.resolve().is_relative_to(pathlib.Path.cwd().resolve()), f"{cmd} must be inside the plugin"
launcher = cmd.read_text()
assert launcher.startswith("#!/bin/sh\n"), "the launcher is POSIX sh"
for need in ("UV_PROJECT_ENVIRONMENT=", "PYTHONDONTWRITEBYTECODE=1", "uv run --frozen --no-dev --project",
             "command -v uv"):
    assert need in launcher, f"launcher lacks {need}"
for shipped in ("pyproject.toml", "uv.lock", "src/gantrel_mcp/server.py", "docs/PRINTERS.md"):  # readme: the build reads it
    assert pathlib.Path(shipped).is_file(), f"the launcher needs {shipped}"
skills = pathlib.Path(m.get("skills", "skills"))
names = {}
for d in sorted(p for p in skills.iterdir() if p.is_dir()):
    f = d / "SKILL.md"
    assert f.is_file(), f"missing {f}"
    name = re.search(r"^name:\s*(\S+)", f.read_text(), re.M).group(1)
    names[name] = f
cmds = {c["name"]: c for c in m["commands"]}
assert cmds["gantrel"]["skill"] == "smoothstone", cmds
for c in cmds.values():
    assert c.get("skill") in names, f"command {c['name']}: missing skill {c.get('skill')}"
print(f"ok   plugin.toml: id gantrel, skills {sorted(names)}, commands {sorted('/' + n for n in cmds)}, "
      f"capabilities {m['capabilities']}, mcp {mcp['command']}")
PY

if [ -n "$(git status --porcelain -- skills/plugin README.md 2>/dev/null)" ]; then
  echo "FAIL skills/plugin or README.md has uncommitted changes; commit or stash them first (this check resyncs over them)"
  exit 1
fi
sha="$(sed -n 2p skills/plugin/smoothstone/UPSTREAM)"
digest() { find skills/plugin README.md -type f -print0 | sort -z | xargs -0 sha256sum; }
before="$(digest)"
scripts/sync-smoothstone.sh "$sha" >/dev/null
after="$(digest)"
if [ "$before" != "$after" ]; then
  diff <(echo "$before") <(echo "$after") || true
  echo "FAIL sync at $sha changed files"
  exit 1
fi
echo "ok   sync-smoothstone.sh $sha changes nothing"
