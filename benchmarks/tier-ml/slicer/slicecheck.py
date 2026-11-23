"""Slicer check for Gantrel parts: pinned OrcaSlicer 2.5.0, 0.4 mm nozzle PLA, supports off.

A part passes when the pinned slice exits 0, writes its result and statistics, and reports no
warnings, the mesh is not inside out (positive signed volume), and a 100% infill slice with the same
profile does the same and extrudes within 5% of the mesh volume.
Usage: python3 -I slicecheck.py [--json PATH] <part.stl>... ; prints one line per part and writes
PATH (default out/slicecheck.json; the committed slicecheck.json is the recorded 12 part run).
ORCA_SLICER names the OrcaSlicer 2.5.0 binary (default: the extracted AppImage under ~/.local/opt).
"""
import json, os, shutil, struct, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = "OrcaSlicer-2.5.0"
ORCA = os.environ.get("ORCA_SLICER") or os.path.expanduser("~/.local/opt/orcaslicer-2.5.0/squashfs-root/AppRun")


def mesh_volume(path):
    data = open(path, "rb").read()
    tris = []
    if data[:5] == b"solid" and b"facet" in data[:400]:
        vs = [tuple(map(float, l.split()[1:4])) for l in data.decode().splitlines() if l.strip().startswith("vertex")]
        tris = [vs[i:i + 3] for i in range(0, len(vs), 3)]
    else:
        n = struct.unpack("<I", data[80:84])[0]
        for i in range(n):
            f = struct.unpack("<12f", data[84 + 50 * i:84 + 50 * i + 48])
            tris.append((f[3:6], f[6:9], f[9:12]))
    v = 0.0
    for a, b, c in tris:
        v += (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0]) + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6
    return v


def check_version():
    head = subprocess.run([ORCA, "--help"], capture_output=True, text=True, timeout=120).stdout.splitlines()[:1]
    if head != [VERSION + ":"]:
        sys.exit(f"slicecheck: {ORCA} is {head}, not the pinned {VERSION}")


def slice_part(stl, process, out):
    shutil.rmtree(out, ignore_errors=True)  # a slice that writes nothing must not inherit old reports
    os.makedirs(out)
    cmd = [ORCA, "--datadir", os.path.join(HERE, "datadir"),
           "--load-settings", f"{HERE}/machine.json;{HERE}/{process}",
           "--load-filaments", f"{HERE}/filament.json",
           "--arrange", "1", "--orient", "0", "--slice", "0", "--export-statistics", "--outputdir", out, stl]
    with open(os.path.join(out, "log.txt"), "w") as log:
        rc = subprocess.run(["nice"] + cmd, stdout=log, stderr=subprocess.STDOUT, timeout=900).returncode
    result = os.path.join(out, "result.json")
    res = json.load(open(result)) if os.path.exists(result) else {}
    warns = list(res.get("warnings", [])) + [p.get("warning_message") for p in res.get("sliced_plates", []) if p.get("warning_message")]
    if not res:
        warns.append("no result.json in the output folder")
    stats = os.path.join(out, "plate_1.statistics.json")
    vol = json.load(open(stats))["summary"]["volume"] if os.path.exists(stats) else None
    return rc, warns, vol


def main(args):
    out = os.path.join(HERE, "out", "slicecheck.json")
    if args[:1] == ["--json"]:
        out, args = args[1], args[2:]
    check_version()
    rows = []
    stls = args
    for stl in stls:
        name = os.path.basename(os.path.dirname(os.path.abspath(stl)))
        rc, warns, vol = slice_part(stl, "process.json", os.path.join(HERE, "out", name, "pinned"))
        rc2, warns2, solid = slice_part(stl, "process_solid.json", os.path.join(HERE, "out", name, "solid"))
        mv = mesh_volume(stl)
        ratio = solid / abs(mv) if solid and mv else None
        ok = (rc == 0 and not warns and vol is not None and rc2 == 0 and not warns2 and mv > 0
              and ratio is not None and abs(ratio - 1) <= 0.05)
        rows.append({"part": name, "pass": ok, "exit": rc, "warnings": warns, "pinned_volume": vol,
                     "solid_exit": rc2, "solid_volume": solid, "mesh_volume": round(mv, 2),
                     "solid_over_mesh": round(ratio, 4) if ratio else None})
        inside_out = ", inside out (negative signed volume)" if mv <= 0 else ""
        print(f"{'PASS' if ok else 'FAIL'} {name}: exit {rc}, warnings {warns or 'none'}, solid/mesh {rows[-1]['solid_over_mesh']}{inside_out}", flush=True)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    json.dump(rows, open(out, "w"), indent=1)
    print(f"{sum(r['pass'] for r in rows)} of {len(rows)} parts pass")


if __name__ == "__main__":
    main(sys.argv[1:])
