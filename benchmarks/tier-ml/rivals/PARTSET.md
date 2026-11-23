# Gantrel rival check: fixed part set

Prepared 2026-11-17 by the research delegate for the flagship plugins lead. This is the fixed set of 12 parts EVAL-PLAN.md calls for ("Rival check"). Every contender gets the exact request text below, once, with no follow-up hints, and must hand back one STL per part in its print orientation (the face meant for the bed at z = 0, millimeters).

Status: prepared. The checks below are written; `baseline/measure.py` implements them for the direct-OpenSCAD baseline and was run on it (results in STUDY.md). It assumes the print orientation the baseline used for a few parts (noted per part), so the lead hardens it before scoring rivals that orient parts differently.

## Rules for every part

- **Tolerance.** A stated dimension passes within 0.3 mm or 1% of the request, whichever is larger (EVAL-PLAN.md), unless a check states its own range. Fit checks (a rod, a shaft, a phone, a mating gear) use the ranges given, because "close" is not "fits".
- **Geometry.** `stone check part.stl` must print all PASS: watertight, one body, a real first layer, walls of at least 0.8 mm, at most 5% of the surface past 45 degrees, flat base. Size is checked here, not by stone.
- **Slicer.** The EVAL-PLAN.md slicer check (pinned PrusaSlicer or OrcaSlicer CLI, 0.4 mm nozzle PLA, supports off, exit 0, no empty layer, non-manifold or floating-part warnings). Not run in this study: no slicer is installed on chapel1 (see STUDY.md, "Slicer check needs").
- **One shot.** One request, one answer. A contender that asks a question gets "Use your best judgment." once.
- **Inputs.** Part 12 uses `parts-inputs/lithophane_photo.png` (300 x 375 px, 8-bit grayscale, SHA-256 `a15337eb8895ff098f53b4cd8694e6244d4a62f07feb9e2c85641f6391861219`), made from NASA photo S99-00858 (official portrait of Eileen M. Collins, public domain as a NASA work), resized with `magick S99-00858.jpg -colorspace Gray -resize 300x -strip`.

## How looks are judged

1. **Fixed renders.** Each STL is rendered by OpenSCAD 2026.11.14 from an `import("part.stl");` file, Manifold backend, color scheme Cornfield, 1024 x 1024 px, perspective, `--autocenter --viewall`, at three cameras: iso `--camera=0,0,0,55,0,25,0`, top `--camera=0,0,0,0,0,0,0`, front `--camera=0,0,0,90,0,0,0`. Same program, same lighting (OpenSCAD's fixed two-light setup), same color for every contender, so material and color do not vote. Script: `baseline/views.sh`.
2. **Blind pairwise judge.** For each part and each pair of contenders, a judge model (Opus 5.5, low thinking, fixed prompt) sees the request text and the three views of A and of B, labeled only A and B, and answers which one a person who typed the request would rather print and show, or "tie". The pair is judged twice with A and B swapped; a win counts only when both orders agree, otherwise it is a tie. The judge never sees file names, tool names or check results.
3. **Score.** Wins per part across pairs. EVAL-PLAN.md's bar: Gantrel tier L wins at least 8 of 12 parts against the best measured rival.

## The 12 parts

Catalog column: the smoothstone recipe that covers it (from `stone recipes` and the catalog listing), or **off-catalog** where no recipe does. Off-catalog parts are 7 and 8.

| # | Part | Kind | Catalog |
| --- | --- | --- | --- |
| 1 | Fan guard | functional | fan_guard |
| 2 | Wall hook for a dowel | functional | hook |
| 3 | Gridfinity bin | functional | gridfinity_bin |
| 4 | Cable comb | functional | cable_comb |
| 5 | Spur gear | functional | spur_gear |
| 6 | Phone stand | functional | phone_stand |
| 7 | Raspberry Pi 5 lid | functional | **off-catalog** |
| 8 | Shelf L bracket | functional | **off-catalog** |
| 9 | Twisted vase | decorative | vase |
| 10 | Ribbed planter | decorative | planter |
| 11 | Name keychain | decorative | keychain |
| 12 | Lithophane | decorative | lithophane |

### 1. Fan guard

Request: `Make a finger guard for a 120 mm PC fan. Holes for the fan's screws are 105 mm apart, 4.3 mm across. 2.5 mm thick, honeycomb grille, and no gap a finger could get through.`

Checks:
- Outline 120 x 120 mm (+-1.2); thickness 2.5 (+-0.3).
- Four holes, 4.3 mm (+-0.3), centers on a 105 mm square (+-1.05).
- Largest circle that passes any grille opening under 12.0 mm (the 12 mm jointed test finger, IEC 61032 probe B).
- Open area inside a 110 mm circle at least 50% (airflow; judgment bar).
- Looks: honeycomb pattern clean at the rim, no slivers.

### 2. Wall hook for a dowel

Request: `A wall hook that holds a 1 inch round wooden dowel, 30 mm wide, with two screw holes for #8 wood screws.`

Checks:
- Width 30 mm (+-0.3).
- The largest circle that fits the cradle is 25.7 to 28.4 mm (a 25.4 mm dowel plus 0.3 to 3.0 mm).
- The tip rises at least to the dowel's center line (holds it in).
- Two screw holes through the back plate, 4.2 to 4.8 mm (#8 shank is 0.164 in, 4.17 mm).
- Prints without supports (stone overhang check).

### 3. Gridfinity bin

Request: `A Gridfinity bin, 2 units wide by 1 unit deep and 6 units tall, with the stacking lip, a label shelf and one divider in the middle.`

Checks (Gridfinity constants from kennetek/gridfinity-rebuilt-openscad `standard.scad`, which follows gridfinity.xyz):
- Footprint 83.5 x 41.5 mm (+-0.3); each foot bottom 35.6 x 35.6 (+-0.3) (41.5 top minus the 0.8 + 2.15 mm chamfers).
- Height 46.4 mm (+-0.5): 6 x 7 = 42 plus the 4.4 mm stacking lip.
- One divider: two cavities in a cut at 20 mm.
- Label shelf: the back of the cavity is at least 4 mm shallower in a cut 4 mm below the wall top.
- Lead's extra check (not in the baseline harness): zero intersection volume with a reference 2 x 1 baseplate from the same library, and with a second copy of the bin stacked on top.

### 4. Cable comb

Request: `A cable comb for six 6 mm cables, 10 mm apart, so the cables click in.`

Checks:
- Six slots, centers 10 mm apart (+-0.3).
- Each seat holds a 6.0 mm circle: largest inscribed circle 6.0 to 6.6 mm.
- Click-in: the narrowest opening between seat and edge is 5.2 to 5.9 mm.

### 5. Spur gear

Request: `A spur gear, module 1.5, 24 teeth, 20 degree pressure angle, 8 mm thick, with a bore for a 5 mm D shaft (4.5 mm across the flat).`

Checks:
- Thickness 8 (+-0.3); tip diameter 39.0 (+-0.39) = m(z + 2); root diameter 32.25 (+-0.4) = m(z - 2.5); 24 teeth.
- Tooth thickness on the 36 mm pitch circle 2.356 mm (+-0.15) = pi m / 2.
- Meshes with a copy of itself at 36.0 mm center distance through one tooth pitch with under 0.05 mm2 overlap.
- Bore: 5.05 to 5.4 mm across the round, 4.55 to 4.8 mm across the flat.

### 6. Phone stand

Request: `A phone stand for a phone that is 9 mm thick in its case. It should lean back at 70 degrees, be 80 mm wide, and have a slot for the charging cable.`

Checks:
- Width 80 mm (+-0.8).
- Back rest at 70 degrees (+-2) from the table (straightest-fit angle of the rest face).
- A 9.0 mm slab dropped against the rest seats on a ledge and can slide forward no more than 3 mm (it will not slip off).
- A cable slot: the mid-width section has material removed where the cable runs (over 20 mm2 of profile).
- Harness note: the baseline harness finds the width axis by size and assumes the profile is in the other two axes (printed on its side). A stand printed upright needs the section taken across the width instead.

### 7. Raspberry Pi 5 lid (off-catalog)

Request: `A top lid for a Raspberry Pi 5. It screws to the board's four mounting holes (M2.5, 58 x 49 mm apart) on 14 mm standoffs so the Active Cooler fits under it, with a vent grille over the cooler. 2 mm top plate that sticks out 3 mm past the 85 x 56 mm board on every side.`

Checks (board and hole layout from the Raspberry Pi 5 mechanical drawing):
- Plate 91 x 62 mm (+-0.91, +-0.62); plate thickness 2.0 (+-0.3); overall height 16.0 (+-0.3).
- Four standoff holes, 2.5 to 3.0 mm, 58.0 (+-0.58) by 49.0 (+-0.49) apart.
- Vent open area at least 600 mm2, centered within 5 mm of the hole pattern center.
- Prints plate down without supports.
- Lead's extra check: the hole pattern sits 3.5 + 3 = 6.5 mm from the plate's short-side edge, matching the board.

### 8. Shelf L bracket (off-catalog)

Request: `An L bracket for a shelf. Both legs 50 mm long on the outside, 25 mm wide and 5 mm thick, with a triangular gusset on each side. Two 4.5 mm holes in each leg, 15 mm and 40 mm from the outside corner, countersunk on the inside for #8 flat-head screws.`

Checks:
- Legs 50 mm outside (+-0.5); width 25 (+-0.3).
- Four holes 4.5 mm (+-0.3), centers 15 and 40 mm (+-0.3) from the outside corner, on the center line (+-0.3).
- Countersinks on the inside faces, 8.4 to 9.5 mm across at the face.
- Gussets on both sides.
- Harness note: the baseline harness assumes one leg flat on the bed along +x and the other along +z.

### 9. Twisted vase

Request: `A twisted vase about 180 mm tall and 80 mm across at the widest point, made to print in spiral vase mode.`

Checks:
- Height 180 (+-1.8); widest diameter 80 (+-0.8).
- Solid body (one loop in a mid-height cut), as spiral vase mode needs.
- Base contact at least 1500 mm2; overhang check passes.
- Looks: judged.

### 10. Ribbed planter

Request: `A ribbed planter that a 4 inch nursery pot (102 mm across the top, 90 mm tall) drops into, with a drain hole.`

Checks:
- Inner rim diameter 104 to 112 mm; inner depth at least 91 mm.
- Drain hole at least 8 mm.
- Looks: judged.

### 11. Name keychain

Request: `A keychain that says EMMA in bold letters, about 50 mm wide, with a hole for a key ring.`

Checks:
- Width 45 to 55 mm ("about").
- Tesseract (`--psm 7`) reads EMMA from a top-down cut 0.3 mm below the part's highest point, rasterized to `ocr_cut.png` (a shaded render read as "_" in the baseline run, so the harness reads the cut instead).
- Key ring hole at least 4.0 mm.
- Letters stand at least 0.8 mm proud (or are cut at least 0.8 mm deep).
- Looks: judged.

### 12. Lithophane

Request: `Make a lithophane of this photo (lithophane_photo.png), 100 mm wide, 0.8 mm thick at the brightest and 3 mm thick at the darkest, that stands up on its own.`

Checks:
- Picture width 100 mm (+-1).
- In the picture area, 1st percentile thickness 0.8 (+-0.15), 99th percentile 3.0 (+-0.15).
- Thickness map against inverted luminance: Pearson r at least 0.8 after registering the frame, and higher than against the mirrored picture (reads the right way round from the front).
- Stands: a foot on the bed at least 20 mm deep; prints upright.
- Looks: judged (backlit look is not rendered; the judge sees the geometry).
- Harness note: the baseline harness assumes the picture stands in the XZ plane with thickness along y.
