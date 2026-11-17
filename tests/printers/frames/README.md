# Guard frames

Labeled camera frames for the print failure guard: `good/` (a print going normally) and `failed/`
(spaghetti, a part knocked loose or dragged by the nozzle, an empty bed during a print). The labels,
the failure kind and the printer status the guard is told for each frame are in `labels*.json`.

- `labels.synthetic.json`: rendered by `render_frames.py` with Blender (`blender -b -P
  tests/printers/frames/render_frames.py -- tests/printers/frames`, Blender 5.2.1, fixed seeds). They
  are original renders and ship under the repository's MIT license.
- `labels.real.json`: `good/k2-real-finished-vase.jpg`, a read-only snapshot from the maintainer's own
  Creality K2 chamber camera (through go2rtc) on 2026-11-17, shared by the maintainer under the same
  license.

`benchmarks/guard_vision.py` runs a vision model over every frame; the results are in
`benchmarks/results/guard-vision/` and the table in docs/PRINTERS.md. The set is small and the
renders are cleaner than real camera frames, so a model that does well here is a candidate, not
proof that it catches real failures.
