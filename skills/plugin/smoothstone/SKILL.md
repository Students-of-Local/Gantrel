---
name: smoothstone
description: Make 3D printable parts that look designed, from a text request or a picture, by choosing and setting tested parametric recipes with the stone CLI. Works for small local models (tier S), mid models (M) and large models (L), and keeps its state on disk so it survives context compaction. Use when someone asks for a printable part, an STL, a 3D model for a printer, or something to 3D print from a photo or drawing.
---

# smoothstone

The person's request is the text after `</skill>` at the end of this message. Keep it in mind
the whole time: every step below works toward that one part.

1. Run `stone status` with the bash tool.
2. Do what its NEXT line says. When NEXT shows `<what to make>`, write the person's request
   there, for example `stone new "a wall hook for a 20 mm rod"`. Never run a NEXT line that
   still has `<` and `>` in it.
3. Keep going, one stone command per bash call, until stone says the part is ready. Run every
   command in the foreground and read its output before the next one: never run stone in the
   background, and use only the bash tool (there is no wait or jobs tool).
4. When stone says the part is ready, run `gantrel-studio --job ./stone-job --detach` once and give
   the person the link it prints, exactly as printed, so they can turn the part and adjust its
   sizes. If that command is not found, skip this step.

If bash prints `stone: command not found`, tell the person stone is not installed, give them
the install steps at https://github.com/baney75/Gantrel#local-studio-plugin (smoothstone commit
`45e05ce28634216c6d029fa1be26ee85ae9b6b15`), then stop. Never install it yourself.

stone turns a request into a print-ready STL. The model picks a recipe and sets its numbers;
stone builds the part with OpenSCAD, checks that it prints (watertight, clean for a slicer, one piece,
flat base, walls, overhangs, size), and keeps every step in a job folder. Every stone message ends with one
line, NEXT: <command>, and `stone status` repeats it, so a model that loses its context goes on
from where it was.

## Pick your tier and follow that card

- Tier S, about 3B parameters and under: tiers/S.md
- Tier M, about 4B to 30B: tiers/M.md
- Tier L, frontier models: tiers/L.md

Set the tier with STONE_TIER=S|M|L or `tier = "S"` in stone.toml. stone status prints the
card's rules on every call, so they come back after a compaction.

## Research first

When the request names a real thing (a phone to fit, a person to look like, a famous statue),
follow research.md before stone set: exact sizes from the maker's own drawings, a list of what
makes a person recognizable, a real museum scan before a generated one. Keep each fact and its
source with `stone note "<fact>, <source>"`; stone status shows the notes again after a
compaction. Never make up a size a part has to fit.

## Pictures

`stone new "<request>" --image photo.png` (or `stone image photo.png`) traces outlines for
keychains, cookie cutters and signs, and makes a height map for lithophanes and reliefs.

## Figures of people, pets and things

The figure recipe makes a mini figure on a round plinth with a name on it. Ask for it plainly:
`stone new 'A mini figure of Jensen Huang, 90 mm tall, with "JENSEN" on the base'`. With no
photo, stone build says to run `stone find "<name>"`, which downloads free-licensed photos from
Wikimedia Commons and writes credits.txt. With your own photo, run `stone image <photo>` instead.
Then stone build runs three local models (describe, style, lift) and the print checks. Run
tools/figure/setup.sh once first.

## Colors

After a passing build of a figure, `stone colors` splits it into color regions from the colors
of its style picture, lists them largest first with a part name each (base, shoes, pants, jacket or
top, hands, face, hair, glasses, other; the words of the request and look.txt, so a dress is
"dress"), and draws them numbered in regions.png. A part name or a region number picks a region:
`stone colors jacket=black rest=white` (or `2=black`) then writes a 3MF next to the STL with each triangle painted
with its filament. Orca, Bambu Studio and Creality Print open it with the paint, and PrusaSlicer
reads Bambu paint too. The loaded filaments, in slot order, go in stone.toml:
`[filaments]` with `1 = "white"` and `2 = "black"` (the default), in the job folder or once per
machine in ~/.config/smoothstone/stone.toml. When a figure's request names colors, the build's
NEXT line is stone colors, and the mapping it suggests follows those colors. Other parts print in
one color, and the build names the filament to load. Before it writes the 3MF, stone cuts each color
edge as a smooth curve through the triangles, and the color_edges check fails any edge that still
zigzags: FIX "color boundary is jagged", no file, and a NEXT line that gives that region the rest color.
The hem, neck and wrists are lines of the shape: a color edge within 3 mm of one moves onto it, and
where the shape has a sculpted groove there (a collar, a cuff) the edge follows the groove, slipped to
the side a viewer sees least. The ledges check fails a color edge with up-facing faces of the wrong
color seen beside it, such as a white strip on top of a collar: FIX, no file.

## Check every angle

The numbers are the gate: every check must pass. Then open build/views.png (and build/boundaries.png
after stone colors) and look at it panel by panel, all ten sides, not just the front. A part that holds
a phone, carries a load or fits a hole must be right from every side, because a fault on one side
breaks it as surely as one on the front. Look for a feature missing on one side, a wall that thins,
text cut off, and a color edge that zigzags. If a panel shows a fault, say what and where, and fix it.

## Measuring from a photo

When a photo has a ruler, a bank card or a US quarter beside the object, `stone measure
<photo>` (with `--ref card` or `--ref coin-us-quarter` for those) finds the object's real size,
and stone show fills it in. `--known 120mm` works when you know the long side.

## Start

stone status
