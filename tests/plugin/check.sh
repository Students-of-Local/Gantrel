#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"

python3 - <<'PY'
import pathlib, re, sys, tomllib
m = tomllib.loads(pathlib.Path("plugin.toml").read_text())
allowed = {"id", "version", "description", "skills", "prompts", "commands", "capabilities"}
assert set(m) <= allowed, f"unknown keys: {set(m) - allowed}"
assert m["id"] == "gantrel", m["id"]
assert re.fullmatch(r"\d+\.\d+\.\d+", m["version"]), m["version"]
assert m["description"].strip()
assert m["capabilities"] == [], m["capabilities"]
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
print(f"ok   plugin.toml: id gantrel, skills {sorted(names)}, commands {sorted('/' + n for n in cmds)}")
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
