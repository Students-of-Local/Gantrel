#!/usr/bin/env bash
# Fixed renders for the blind looks judge: iso, top, front, 1024 px, Cornfield, OpenSCAD lighting.
# Usage: views.sh <part_dir>...   (reads part.stl, writes view_iso.png, view_top.png, view_front.png)
for d in "$@"; do
  d=$(cd "$d" && pwd)
  [ -s "$d/part.stl" ] || continue
  printf 'import("%s");\n' "$d/part.stl" > "$d/.view.scad"
  for v in "iso 55,0,25" "top 0,0,0" "front 90,0,0"; do
    set -- $v
    nice ~/.local/bin/openscad --backend=manifold --render --colorscheme=Cornfield --projection=p \
      --imgsize=1024,1024 --autocenter --viewall --camera=0,0,0,$2,0 \
      -o "$d/view_$1.png" "$d/.view.scad" >/dev/null 2>&1
  done
  rm -f "$d/.view.scad"
done
