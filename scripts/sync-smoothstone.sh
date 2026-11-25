#!/usr/bin/env bash
set -euo pipefail

ref="${1:?usage: scripts/sync-smoothstone.sh <sha|ref>}"
repo_url="${SMOOTHSTONE_REPO:-https://github.com/Students-of-Local/smoothstone}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dest="$root/skills/plugin/smoothstone"
preamble="$root/scripts/smoothstone-preamble.md"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

git clone --quiet "$repo_url" "$tmp/src"
git -C "$tmp/src" -c advice.detachedHead=false checkout --quiet "$ref"
sha="$(git -C "$tmp/src" rev-parse HEAD)"

# Build the new skill in a staging folder and swap it in only after every step passed,
# so a failed sync never leaves a half-synced skill behind.
stage="$tmp/stage"
mkdir -p "$stage"
cp -R "$tmp/src/skill/." "$stage/"

python3 - "$stage/SKILL.md" "$preamble" "$sha" <<'PY'
import sys
skill, preamble, sha = sys.argv[1:]
lines = open(skill, encoding="utf-8").read().splitlines(keepends=True)
block = open(preamble, encoding="utf-8").read().replace("@SHA@", sha).rstrip("\n") + "\n"
heading = next((i for i, line in enumerate(lines) if line.startswith("# ")), None)
if heading is None:
    sys.exit("sync-smoothstone: no '# ' heading in SKILL.md to insert after")
lines[heading + 1:heading + 1] = ["\n", block]
open(skill, "w", encoding="utf-8").write("".join(lines))
PY

python3 - "$stage/tiers/L.md" "$root/scripts/smoothstone-tier-L.md" "$sha" <<'PY'
import sys
card, note, sha = sys.argv[1:]
lines = open(card, encoding="utf-8").read().splitlines(keepends=True)
block = open(note, encoding="utf-8").read().replace("@SHA@", sha).rstrip("\n") + "\n"
at = next((i for i, line in enumerate(lines) if "tools/sheet.py" in line), None)
if at is None:
    sys.exit("sync-smoothstone: no tools/sheet.py line in tiers/L.md to note after")
lines[at + 1:at + 1] = [block]
open(card, "w", encoding="utf-8").write("".join(lines))
PY

printf '%s\n%s\n' "$repo_url" "$sha" > "$stage/UPSTREAM"
rm -rf "$dest"
mkdir -p "$(dirname "$dest")"
mv "$stage" "$dest"
sed -i -E "s/(smoothstone[@/](tree\/)?)[0-9a-f]{40}/\1$sha/g; s/(Pinned smoothstone: \`)[0-9a-f]{40}/\1$sha/; s/(smoothstone checkout )[0-9a-f]{40}/\1$sha/" "$root/README.md"
echo "smoothstone skill synced at $sha"
