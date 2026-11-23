"""Run: python3 -I benchmarks/tier-ml/slicer/test_slicecheck.py (no slicer needed; a fake stands in).

A slice that exits 0 but writes no result or statistics must fail the part, even when the solid
slice's volume matches the mesh (review r2 sol-sol-F2).
"""
import os, struct, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE = """#!/bin/sh
case "$1" in --help) echo "OrcaSlicer-2.5.0:"; exit 0;; esac
out=""; solid=0; prev=""
for a in "$@"; do [ "$prev" = "--outputdir" ] && out=$a; case "$a" in *process_solid.json*) solid=1;; esac; prev=$a; done
if [ $solid = 1 ] || [ "$FAKE_PINNED" = 1 ]; then
  echo '{"warnings":[],"sliced_plates":[{"warning_message":""}]}' > "$out/result.json"
  echo '{"summary":{"volume":1000.0}}' > "$out/plate_1.statistics.json"
fi
exit 0
"""


def cube_stl(path, s=10.0):
    v = [(x, y, z) for x in (0, s) for y in (0, s) for z in (0, s)]
    faces = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 4), (1, 5, 4),
             (2, 6, 3), (3, 6, 7), (0, 4, 2), (2, 4, 6), (1, 3, 5), (3, 7, 5)]
    with open(path, "wb") as f:
        f.write(b"\0" * 80 + struct.pack("<I", len(faces)))
        for a, b, c in faces:
            f.write(struct.pack("<12fH", 0, 0, 0, *v[a], *v[c], *v[b], 0))  # outward winding


def run(tmp, pinned_reports):
    part = os.path.join(tmp, "cube")
    os.makedirs(part, exist_ok=True)
    cube_stl(os.path.join(part, "part.stl"))
    env = dict(os.environ, ORCA_SLICER=os.path.join(tmp, "orca"), FAKE_PINNED="1" if pinned_reports else "0")
    return subprocess.run([sys.executable, "-I", os.path.join(HERE, "slicecheck.py"), "--json",
                           os.path.join(tmp, "out.json"), os.path.join(part, "part.stl")],
                          env=env, capture_output=True, text=True).stdout


with tempfile.TemporaryDirectory() as tmp:
    with open(os.path.join(tmp, "orca"), "w") as f:
        f.write(FAKE)
    os.chmod(os.path.join(tmp, "orca"), 0o755)
    missing = run(tmp, pinned_reports=False)
    present = run(tmp, pinned_reports=True)
assert missing.startswith("FAIL cube") and "no result.json" in missing, missing
assert present.startswith("PASS cube"), present
print("ok: a slice with no reports fails; the same part with reports passes")
