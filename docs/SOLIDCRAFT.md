# SolidCraft parts

[SolidCraft](https://github.com/tangency-labs/solidcraft) is an open-source parametric CAD program in
Rust. Gantrel drives its command line, `solidcraft-cli`, headless: the model writes a design as a list
of modeling commands, and `gantrel-solid` builds it into STEP, STL and 3MF files and checks that the
part prints. It is a third way to make a part, next to smoothstone's OpenSCAD recipes (`/gantrel`)
and build123d. Gantrel ships none of SolidCraft's name, logo or UI kit (its `LICENSE-BRAND`).

Pinned: SolidCraft v0.50.0, tag commit `eeb366c2ff276987a15c043d63b9072b58274703`. The pin (version,
file per platform and SHA256) is in `src/gantrel_solid/pin.py`, and a test checks it against the
release's `SHA256SUMS.txt`.

## Install

    gantrel-solid doctor

finds `solidcraft-cli` (`GANTREL_SOLIDCRAFT`, then `PATH` when that one is 0.50.0, then
`~/.local/share/gantrel/solidcraft/0.50.0/`, then `PATH` at any version) and prints its path and
version. On Windows the file is `solidcraft-cli.exe`. When it is missing, it
prints the steps for this computer: the pinned file's URL and its SHA256. It installs nothing.

    gantrel-solid fetch --yes

on Linux (x86_64 or aarch64) downloads the pinned tarball, checks its SHA256 against the pin and the
release's `SHA256SUMS.txt`, unpacks only `bin/solidcraft-cli` into
`~/.local/share/gantrel/solidcraft/0.50.0/` and runs `solidcraft-cli --version`. A file that doesn't
match, a broken download or a bad release URL is refused, and only what fetch made is removed. Without `--yes` it downloads nothing. It never runs an
installer, asks for sudo or sets up the desktop app. On macOS and Windows, `doctor` prints the steps
to do by hand: the macOS file is the CLI-only `solidcraft-cli-0.50.0-macos-universal.zip`.

`build` also needs smoothstone's `stone` on `PATH` for the print checks (see the README).

## A design

A design is a JSON file: `{"sources": [...], "commands": [...]}`. Each command is
`{"command": "<id>", "params": {...}}`, and they run in order. Sizes are in millimeters and the floor
of the part is z = 0. `sources` lists where each real-world size came from. This plate is 80 x 40 x 5
mm with four 5 mm holes:

```json
{"sources": ["every size is in the request"],
 "commands": [
  {"command": "sketch.create", "params": {"plane": "XY", "name": "Plate"}},
  {"command": "sketch.rectangle.two_point", "params": {"p0": [0, 0], "p1": [80, 40]}},
  {"command": "sketch.finish", "params": {}},
  {"command": "solid.extrude", "params": {"distance": 5, "body_name": "Plate"}},
  {"command": "sketch.create", "params": {"plane": "XY", "name": "Holes"}},
  {"command": "sketch.circle.center", "params": {"center": [10, 10], "diameter": 5}},
  {"command": "sketch.circle.center", "params": {"center": [70, 10], "diameter": 5}},
  {"command": "sketch.circle.center", "params": {"center": [10, 30], "diameter": 5}},
  {"command": "sketch.circle.center", "params": {"center": [70, 30], "diameter": 5}},
  {"command": "sketch.finish", "params": {}},
  {"command": "solid.extrude", "params": {"distance": 5, "operation": "cut"}}
]}
```

The skill (`skills/plugin/solidcraft/SKILL.md`) has this one, a filleted enclosure with a lid lip
and a shaft collar with a set-screw hole, and the table of commands they use. `solidcraft-cli
commands` lists all 930 of SolidCraft's commands, and `solidcraft-cli commands --id <id>` gives
one's params.

## Build and measure

    gantrel-solid build [design.json] --job ./solid-job

runs `solidcraft-cli run` on the design (default `<job>/design.json`) and, only when every command
succeeded, writes `part.step`, `part.stl`, `part.3mf`, `design.json` and `check.json` into
`<job>/build/`. It runs `stone check` on the STL (watertight, one body, resting flat, walls,
overhangs and the rest of smoothstone's checks) plus a check that the part fits a 220 x 220 mm plate.
It prints one JSON line:

- `{"ok": true, "files": [...], "size_mm": [x, y, z], "checks": {...}, "fixes": []}` when it built and
  every check passed;
- `"ok": false` with `"fixes"` when it built but a check failed, each fix saying what to change;
- `"ok": false` with `"index"` (counting from 0), `"command"` and `"error"` when a command failed.
  Then nothing is written.

`check.json` is what Gantrel Studio reads, so `gantrel-studio --job ./solid-job` shows the part, its
size and its checks.

    gantrel-solid measure solid-job/build/part.step

prints the bounding box size in mm, its corners and the volume in mm3 of a STEP (through
`solidcraft-cli eval`), STL or 3MF.

## Limits

- The job folder must be inside the working folder: a `--job` with `..`, outside it, or through a
  symlink (also `<job>/build`) is refused.
- Only modeling commands run (`sketch.`, `solid.`, `construct.`, `face.`, `surface.`, `body.`,
  `parameters.change`, `material.`, `appearance.`, `plastic.`). A command that opens, saves, imports,
  exports or loads anything is refused, since `build` writes the files itself.
- A run longer than 120 s is stopped.
- No SolidCraft window and no `--connect` to a running SolidCraft app: the app's socket waits until
  its access rules are read.
- Gantrel Studio shows the part and its checks but not sliders: those come from smoothstone recipes,
  and its status line still reads "No part yet" for a SolidCraft job.

## MCP tools

`gantrel-mcp` has `solid_build` (`design`, `job`) and `solid_measure` (`path`), which run the same
commands and return the same JSON plus a `next` field. The design file follows the MCP file rules
(inside home or the working folder, 1 MB at most). A build or measure still running after 50 s is
stopped and refused; run `gantrel-solid build` in a terminal for a part that takes longer. Neither tool
touches a printer.

## Benchmark

`benchmarks/solid/run.py` runs the skill on a local model over three held-out asks
(`benchmarks/solid/asks.json`) and scores what it built by rebuilding its design: it built, every
check passed, the size within 0.1 mm and the volume within 1 percent. On LFM3-1.7B (abbey1, GTX 1650
SUPER 4 GB) it passed the spacer and the washer and failed the open box, where the model gave no reply
within 600 s. Results are in `benchmarks/results/solidcraft/` and [BENCHMARKS.md](../BENCHMARKS.md).
