#!/usr/bin/env bash
# Render each one-pass part with OpenSCAD (Manifold) and run stone check. Waits while 1-minute load > 8.
B=$(cd "$(dirname "$0")" && pwd)
for d in "$B"/p*/; do
  d=${d%/}; n=$(basename "$d")
  [ -s "$d/part.stl" ] && [ -s "$d/check.txt" ] && continue
  while :; do l=$(cut -d' ' -f1 /proc/loadavg); awk "BEGIN{exit !($l>8)}" || break; echo "$(date +%T) $n waiting, load $l"; sleep 30; done
  echo "$(date +%T) $n render (load $(cut -d' ' -f1 /proc/loadavg))"
  s=$(date +%s.%N)
  ( cd "$d" && timeout 900 nice ~/.local/bin/openscad --backend=manifold -o part.stl part.scad ) > "$d/render.log" 2>&1
  rc=$?; e=$(date +%s.%N)
  echo "exit=$rc seconds=$(awk "BEGIN{printf \"%.1f\", $e-$s}")" >> "$d/render.log"
  if [ -s "$d/part.stl" ]; then ( cd "$d" && stone --job "$d/.stone-job" check part.stl ) > "$d/check.txt" 2>&1; fi
  echo "$(date +%T) $n done rc=$rc"
done
echo ALL DONE
