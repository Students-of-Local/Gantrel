---
name: solidcraft
description: Design a precise part with exact sizes in SolidCraft, the open-source parametric CAD program, and get STEP, STL and 3MF files that pass the print checks. Use when someone asks for a part with exact dimensions, a plate with holes, a box or enclosure, a collar, a bracket, a spacer, a STEP file or a CAD model, or says /solid.
---

# solidcraft

The person's request is the text after `</skill>` at the end of this message. Every step below works
toward that one part. All sizes are in millimeters.

## Steps

1. Research first. When the request names a real thing the part must fit (a phone, a bearing, a
   screw, a board), find its exact sizes on the maker's drawing or in the standard, and write each one
   with its source in the design's `"sources"` list. Never make up a size a part has to fit. When the
   request gives every size, use those sizes and skip this step.
2. Write the design to `solid-job/design.json` with one bash call:

   ```
   mkdir -p solid-job && cat > solid-job/design.json <<'EOF'
   {"sources": [], "commands": [ ... ]}
   EOF
   ```

   Copy the closest worked design below and change its numbers.
3. Run `gantrel-solid build --job ./solid-job` in the foreground and read the JSON line it prints.
   - `"ok": true`: go to step 4.
   - `"index"`: the command at that position (counting from 0) failed. Read `error`, fix that one
     command, write the design again and build again.
   - `"fixes"`: the part built but a print check failed. Each fix says what to change. Change the
     design and build again.
4. Run `gantrel-solid measure solid-job/build/part.step`. Every number in `size_mm` must be within
   0.1 mm of the size asked for. If one is not, fix the design and go back to step 3.
5. Run `gantrel-studio --job ./solid-job --detach` once. It prints `Give the person this link:
   [Open Gantrel Studio](...)`. Put that Markdown link in your reply exactly as printed. If the
   command is not found, skip this step.
6. Tell the person the part is ready: its size, the three files in `solid-job/build/` (`part.step`
   for CAD, `part.stl` and `part.3mf` for the slicer) and the sources you used.

If bash prints `gantrel-solid: command not found`, or build says `solidcraft-cli is not installed`,
tell the person to run `gantrel-solid doctor`, which prints the install steps, then stop. Never
install anything yourself. `gantrel-solid fetch --yes` downloads SolidCraft only after the person
says yes.

## The design format

A design is `{"commands": [...]}`. Each command is `{"command": "<id>", "params": {...}}` and they
run in order. The floor of the part is z = 0, so it prints flat on the plate.

| Command | Params | What it does |
|---|---|---|
| `sketch.create` | `plane`: `"XY"` (the floor), `"XZ"` or `"YZ"`, `name` | start a sketch |
| `sketch.rectangle.two_point` | `p0`, `p1`: opposite corners `[x, y]` | a rectangle in the sketch |
| `sketch.circle.center` | `center`: `[x, y]`, `diameter` | a circle in the sketch |
| `sketch.finish` | `{}` | end the sketch |
| `solid.extrude` | `distance`, `operation`: `"cut"` to cut, `body_name` | push the last sketch up into a solid, or cut it |
| `solid.box` | `length` (x), `width` (y), `height` (z), `corner`: `[x, y, z]`, `body_name` | a box |
| `solid.cylinder` | `diameter`, `height`, `base`: `[x, y, z]`, `axis`: `"X"`, `"Y"` or `"Z"`, `operation`: `"cut"`, `targets`: `[body]` | a cylinder, or a round hole when it cuts |
| `solid.fillet` | `edges`: a point `[x, y, z]` on each edge, `radius`, `body` | round edges |
| `solid.shell` | `faces`: a point on each face to open, `thickness`, `body` | hollow a body out, walls this thick |
| `solid.combine` | `target`, `tools`: `[body]`, `operation`: `"join"` | join bodies into one |

Only modeling commands are allowed. Build writes the files itself, so a command that opens, saves,
imports or exports anything is refused.

## Worked designs

A plate 80 x 40 x 5 mm with four 5 mm holes, 60 mm apart along x and 20 mm apart along y:

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

An enclosure 60 x 40 x 30 mm with 4 mm rounded corners, 2 mm walls, open at the top, and a lip 1 mm
thick and 3 mm tall around the top for a lid to sit on (33 mm tall with the lip):

```json
{"commands": [
  {"command": "solid.box", "params": {"length": 60, "width": 40, "height": 30, "corner": [0, 0, 0], "body_name": "Box"}},
  {"command": "solid.fillet", "params": {"edges": [[0, 0, 15], [60, 0, 15], [60, 40, 15], [0, 40, 15]], "radius": 4, "body": "Box"}},
  {"command": "solid.shell", "params": {"faces": [[30, 20, 30]], "thickness": 2, "body": "Box"}},
  {"command": "solid.box", "params": {"length": 58, "width": 38, "height": 3, "corner": [1, 1, 30], "body_name": "Lip"}},
  {"command": "solid.fillet", "params": {"edges": [[1, 1, 31.5], [59, 1, 31.5], [59, 39, 31.5], [1, 39, 31.5]], "radius": 3, "body": "Lip"}},
  {"command": "solid.shell", "params": {"faces": [[30, 20, 33], [30, 20, 30]], "thickness": 1, "body": "Lip"}},
  {"command": "solid.combine", "params": {"target": "Box", "tools": ["Lip"], "operation": "join"}}
]}
```

A shaft collar for a 10 mm shaft, 24 mm across and 10 mm wide, with a 3.3 mm hole across one side
for tapping an M4 set screw:

```json
{"commands": [
  {"command": "solid.cylinder", "params": {"diameter": 24, "height": 10, "body_name": "Collar"}},
  {"command": "solid.cylinder", "params": {"diameter": 10, "height": 10, "operation": "cut", "targets": ["Collar"]}},
  {"command": "solid.cylinder", "params": {"diameter": 3.3, "height": 13, "base": [0, 0, 5], "axis": "X", "operation": "cut", "targets": ["Collar"]}}
]}
```

A hole is a cylinder that cuts: for a vertical hole use `axis` `"Z"` and `base` `[x, y, 0]`; for a
hole across a part use `axis` `"X"` or `"Y"`.
