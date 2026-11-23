# Gantrel tier M and L evals: plan and bars

Prepared 2026-11-17 by the flagship plugins lead, before any M or L run, and moved here at the handoff (Gantrel#16, main at f3571cb). Runs use smoothstone's harness (`evals/run.py --card M|L`, drivers `openai` for local models and `claude` for tier L on the person's own sign-in), then the slicer check below. The rival study and its 12 part baseline are in [rivals/](rivals/STUDY.md).

The held-out sets stay sealed outside the repo until the skill is frozen, so no tuning run can see them; their hashes are below and in Gantrel#10, and they are committed with the run that uses them.

## Protocol (from smoothstone's blind sets)

- A fresh writer who sees only the recipe catalog writes each held-out set. The lead never reads the specs; it records their SHA-256 here before any tuning run, and each set runs once, after the skill is frozen.
- Spec format: smoothstone's (`id`, `request`, optional picture, `expect` with `recipe` and parameter ranges). Off-catalog specs for tier L use `expect` with `new_recipe: true`, `dims` (bounding box ranges in mm) and named features instead of a recipe.
- A run passes when the model reached `stone done`, the part passes `stone check`, the recipe or new recipe matches `expect`, every expected parameter or dimension is in range, and the exported STL slices cleanly (below).

## Sets

| Set | Writer brief | Size |
| --- | --- | --- |
| M1 | Catalog requests a person would type, harder than the S sets: several constraints in one request, sizes in inches or by a named object ("fits an AA battery"), look words that map to parameters, two picture-to-part requests | 10 specs |
| L1 | Half catalog requests with constraints no tier S model handles, half off-catalog parts a recipe must be written for (a bracket from a sketch with dimensions, an enclosure with cutouts, a part that mates with a catalog part) | 10 specs |

## Runs and bars

5 seeds a spec. Tier M on two models (the 8B the plugin lead tunes, and one 20B to 30B model); tier L on Haiku 5.5 and Opus 5.5 at low thinking.

| Measure | M bar | L bar |
| --- | --- | --- |
| Runs passing the spec | 45 of 50 | 47 of 50 |
| Compacted after step 3, runs passing | 42 of 50 | 45 of 50 |
| Off-catalog runs whose new recipe passes `tools/sheet.py` and `stone check` | n/a | 22 of 25 |
| Slicer check on every passing part | 100% | 100% |
| Picture-to-part specs passing | 8 of 10 | 9 of 10 |

## Slicer check (print-ready export)

The exported STL slices with a pinned PrusaSlicer or OrcaSlicer command line (run as a separate program, AGPL, never linked), a fixed 0.4 mm nozzle PLA profile, supports off: exit 0, no warnings about empty layers, non-manifold or floating parts, and the G-code's printed volume within 5% of the mesh volume times the profile's infill model. Stays geometry evidence, not a physical print; a K2 test print of three parts is the last step, with Donovan's yes for the printer.

## Rival check (shared with the rival study)

One fixed part set of 12 parts (functional and decorative, two off-catalog) is made by Gantrel at each tier and by each rival that can be run without an account or money (a frontier model writing OpenSCAD directly, a frontier model writing CadQuery, open-source text-to-CAD tools). Scored on: slices cleanly, dimensions within 0.3 mm or 1% of the request, `stone check` style geometry checks, and looks by a blind pairwise judge on renders from a fixed camera. Account-only rivals (Zoo, Adam, Meshy, Tripo, Bambu MakerLab) are compared from their published outputs, docs and prices, marked not measured hands-on unless Donovan approves accounts.

Gantrel wins if, at tier L, it passes the slicer and dimension checks on more parts than every measured rival and wins the blind looks comparison on at least 8 of 12 parts against the best of them. Where it does not, the study says so.

## Held-out hashes

(written when the sets are sealed)

Sealed 2026-11-17 by a fresh Opus writer that read only catalog/catalog.txt (smoothstone fefce74). The lead has not read them. Nothing has run on them. Files are read-only. SHA-256:
```
d84e40b5e0b20aec21876aba8552ce01544525320c0a861c5e6259acf2a04e5e  heldout-L1/q10_anchor.json
864a9cc1c0151d0b5a45117b68b8ced8aa58963f4410d6711d0bfd53b33dd18a  heldout-L1/q1_falcon.json
05b7eca3c5485ea9aa524f9f653539e881e2eeea180f74cad447dec65d97cfec  heldout-L1/q2_orchard.json
b73ceef0ca9459610fdd00672075a9710a4e8c20c7f09af013cc31ad146437b2  heldout-L1/q3_pebble.json
bc6f435142ad13e86aae73977a4cde7e96dc89f332e69bf73bef72677c2a98a5  heldout-L1/q4_mosaic.json
ccd38dc252c2c2aaadea4f4724d68dfcdc2fb1daf4b592270671388b6b9a0f8f  heldout-L1/q5_tundra.json
0d67d5628d9442f70a6fbe1ff559beacf2dc56e4eb52b589fc2558db47f6513d  heldout-L1/q6_saddle.json
b16ef4361cade214fa0cb932dab19cb9bb85911b3db0e1d858920a7f9abd9352  heldout-L1/q7_ridge.json
481962b9f9185845781a5e7474d3897e392723f287deb173c1c4b2bdf72424d5  heldout-L1/q8_violet.json
bf549047706543f79911fcb8011bf2464f44d3a1ca5bcda50d3ceab40edea99a  heldout-L1/q9_lattice.json
8fa6fe3d7d7c7cafd4215ace152d34fafba741e4d50e6e67ee657afa46f4de5e  heldout-M1/q10_juniper.json
6b986c7ce0c60841e939e86d3f5bf8380374e349ce259d53b0771e6d608ce666  heldout-M1/q10_juniper.png
8317044e63da384fbd652a2c6e7c49b8a13a312502a4ce977eeab48ab36fa45c  heldout-M1/q1_meadow.json
e962905e0e3431f054ad2c0aaece8abd0221f71ba156187df4d19d62c42f947d  heldout-M1/q2_harbor.json
1b495c96f94612ad7f9d93b725cba8193ce9d9838b1dc117ce005845fc4d3dde  heldout-M1/q3_willow.json
d53b62efdf19fe698dee3edd12e0fc088df4e7f461994f5c155439ab4404d770  heldout-M1/q4_cinder.json
cdb6e48f5a8ebf43d6f880ac41cc4de22aa95a75f07c6fd923e54aeb05eb1ea0  heldout-M1/q5_lantern.json
288fe89e569af19a1a4529ce51ebd85a506231521708aaeab5aeb67aa98193bb  heldout-M1/q6_quarry.json
6418439e8560168493478baf144f9c13ece2f7f9e2a5294eb8f53d9f5e75b72b  heldout-M1/q7_thistle.json
ff9b708d11f82ed4f16f5c0337afb80457548f081392a91151136db70cd5357a  heldout-M1/q8_beacon.json
2c3c9a2c0a0967ee163f317f84a445f0777efe56fc59ec1648f3a5e80e78f5bb  heldout-M1/q9_ember.json
cc2afa9348a19174c8345585fca85fabaa140cc241d73b52fccd7125fb6bedfa  heldout-M1/q9_ember.png
```

## Slicer check, pinned (2026-11-22)

Pinned to OrcaSlicer 2.5.0 (Linux AppImage, SHA-256 d13eb0a9cd618ed080ab6b82c64c0a60c4501aac91e88d5ac2c3b526fb141a7c, matches GitHub's digest), run as a separate program. Profiles and runner live in `slicer/`: `machine.json` (0.4 mm nozzle, 300 mm bed, Klipper), `process.json` (0.20 mm layers, 2 walls, 15% grid infill, supports off), `filament.json` (PLA 1.75 mm), `slicecheck.py`.

"The profile's infill model" is made concrete: the same part is sliced a second time at 100% rectilinear infill, and that slice's extruded volume must be within 5% of the mesh volume. A part passes when the pinned slice exits 0 with no warnings (OrcaSlicer reports floating cantilevers and empty layers there) and the solid slice is within 5%.

Baseline (frontier model writing OpenSCAD in one pass): 10 of 12 slice cleanly. p06 phone stand fails on a floating cantilever warning; p01 fan guard fails at 0.946 solid/mesh (grid bars too thin for a 0.4 mm nozzle). p12 lithophane slices cleanly although it fails `stone check` (124 bodies, below the bed), which is why the slicer check is one gate among several and never the only one. Results: `slicer/slicecheck.json`.
