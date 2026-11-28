# Benchmarking local models

We need to know which local models can actually do Gantrel's work, on which hardware. Guessing is not allowed.

## Built on the Local AI registry

Model launches come from Sybil Solutions' [Local AI registry](https://github.com/sybil-solutions/local-ai-registry) ([local.sybilsolutions.ai](https://local.sybilsolutions.ai)). Each registry recipe is a validated way to run a model on a specific GPU that passed its general gates: it loads, chats, reasons, calls tools, holds context and decodes at 15 tok/s or more. This works on any device the registry covers.

Gantrel adds its own task gates on top. A model only counts as working for Gantrel when it passes them.

## Gantrel task gates

- **Geometry validity:** manifold, correctly scaled, exportable.
- **Dimension accuracy:** within tolerance of the spec or measured ground truth.
- **Printability:** slices cleanly with expected settings.
- **Research quality:** finds correct specs and cites real sources; invents none.
- **Photo measurement:** ruler-in-photo dimensions within the published error bound.

## How a run works

1. Pick a registry recipe for the hardware and launch it.
2. Run the task gates in `tests/` against it.
3. Save the result to `benchmarks/results/<model>/<card>.json` with the recipe ID, date, hardware, pass rate per gate and a log hash.
4. The app offers a model for a task only where a passing result exists for that hardware.

## Per-model instructions

Each model that passes gets a folder in `models/<model>/` (copy `models/_template/`) with the prompt format, settings and skill wording that model needs, plus its own tests. Skills stay shared; only model-specific adjustments live here.

## Results

### SolidCraft parts (`/solid`)

`benchmarks/solid/run.py` gives the model the solidcraft skill and one bash tool, then asks for three parts that are not in the skill (`benchmarks/solid/asks.json`). It rebuilds the design the model wrote, in a folder of its own, and scores five gates: it built, every print check passed, the size is within 0.1 mm, the volume within 1 percent, and the features where the ask puts them (points on each hole's axis and in each opening must be empty, points in the material around them solid, measured from the part's lowest corner in the ask's x, y and z). `tests/solid/test_benchmark_score.py` checks the scorer: an off-center washer hole fails only the feature gate. Temperature 0, 24 turns at most, 600 s for each reply.

| Model | Host | Date | Spacer 30 x 20 x 12, 5 mm hole | Open box 50 x 30 x 20, 2 mm walls, r3 corners | Washer 20 x 2, 8.4 mm hole | Pass rate |
|---|---|---|---|---|---|---|
| LFM3-1.7B (`hf.co/LiquidAI/LFM3-1.7B-GGUF:Q4_K_M`) | abbey1, GTX 1650 SUPER 4 GB, Ollama 0.40.15 | 2026-11-27 | pass, 14 bash calls, 925 s | fail: no reply in 600 s, nothing written | pass, 7 bash calls, 848 s | 2/3 |

Each result, its bash calls and a hash of its transcript are in `benchmarks/results/solidcraft/`. One run per ask, so these are not pass rates over repeats. The 2026-11-27 run was scored before the feature gate; its two passing designs, taken from the recorded bash calls, pass all five gates in `tests/solid/test_benchmark_score.py`.

