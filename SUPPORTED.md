# What Gantrel supports

This file is the honest list of what Gantrel can and cannot do today. Only a situation with a passing test suite in `tests/golden/` may be listed as supported.

## Supported

No design tasks yet; `tests/golden/` has no cases.

### Local Studio plugin packaging

These are packaging checks, not design results. Each one passed on 2026-11-16 with Local Studio at `baney75/local-studio` `plugin-smoke-params` (slice 1 plus the smoke parameters).

- `plugin.toml` loads in Local Studio: `local-studio plugin validate ~/.local-studio/plugins/gantrel` prints `OK gantrel 0.1.0 (2 skills, 0 prompts, 2 commands)` on parish1, chapel1 and abbey1.
- `/gantrel` shows in the Local Studio palette with source `plugin:gantrel`: the core `plugin_smoke` example with `PLUGIN_SMOKE_ID=gantrel PLUGIN_SMOKE_COMMAND=gantrel` prints `ok   check 2 core: /gantrel listed, source plugin:gantrel, kind Plugin` on parish1, chapel1 and abbey1, and the Linux app's `16-composer-plugin-installed` snapshot shows the row with its `plugin:gantrel` badge.
- `/gantrel` sends the smoothstone skill to the model: `plugin_smoke` prints `ok   the /gantrel prompt reached the model expanded`.
- A real part from a local model, on LFM3-1.7B only: `plugin_smoke` on `local-fleet/hf.co/LiquidAI/LFM3-1.7B-GGUF:Q4_K_M` with the request "a fan guard for a 40 mm fan" ran 8 stone tool calls (`stone status`, `new`, `recipes`, `show fan_guard`, `set fan_guard size=40`, `build`, `status`) and printed `ok   check 3: .../stone-job/fan_guard.stl passes stone check` and `PASS plugin smoke`, once straight to Ollama on parish1 and once through the fleet controller. The STL is 40.0 x 40.0 x 3.0 mm, watertight, one body, all stone checks PASS.
- The vendored skill matches smoothstone at the pinned commit plus Gantrel's two steps: `tests/plugin/check.sh` (also in CI).

### Gantrel Studio

These are checks, not design results; nothing here is in `tests/golden/`. Checked on 2026-11-18 on Linux with smoothstone `45e05ce` and the real `stone` building the `fan_guard` recipe:

- `tests/studio/` passes (38 tests): the token, Host and Origin gates, the param checks, the build queue, live events, STL and 3MF export, `--detach` reuse and `--stop`, and one build through the real `stone`.
- The page in headless Chromium (Playwright 1.64.1) at 1280 and 390 px wide: it loads the real part with no console errors and no sideways scroll; setting Size to 60 rebuilds to `60 × 60 × 3 mm` with all 11 checks passing; switching Style to Honeycomb hides the rings-only settings; the 3MF export downloads as `model/3mf`. The web design check reports no flags at either width.

Not checked yet: the page inside Local Studio's own browser panel (WebKitGTK on Linux, WKWebView on macOS), and a local model running `/studio` or step 4 of `/gantrel` by itself.

## Printer connections

`gantrel-printer` has an adapter for each printer family below. "Fake" means the shared contract suite in `tests/printers/` passed against a fake printer built from public protocol notes, speaking the protocol over real sockets (a pseudo terminal for USB). It does not mean the adapter has worked with a real printer. Details are in [docs/PRINTERS.md](docs/PRINTERS.md).

| Printers | `--type` | Proven |
|---|---|---|
| Klipper with Moonraker, Creality K2 Plus, Anycubic Kobra with Rinkhals or Kobra Klipper | `moonraker` | Fake, plus read-only status and camera on a real Creality K2 |
| Creality stock firmware (K1, K2, Ender-3 V3, Hi) | `creality` | Fake, plus read-only status on a real Creality K2 |
| OctoPrint | `octoprint` | Fake |
| Prusa with PrusaLink (API key or digest password) | `prusalink` | Fake |
| Bambu Lab, LAN mode | `bambu` | Fake |
| Elegoo Centauri Carbon, Saturn and Mars (SDCP v3) | `elegoo` | Fake |
| Duet (RepRapFirmware, DSF) | `duet` | Fake |
| FlashForge Adventurer 5M, 5M Pro, AD5X | `flashforge` | Fake |
| Snapmaker 2.0 | `snapmaker` | Fake |
| UltiMaker UM3 and S series (no upload) | `ultimaker` | Fake |
| Repetier-Server | `repetier` | Fake |
| Marlin over USB, including other Anycubic printers | `serial` | Fake |

Calibration prints (`gantrel-printer calibrate <printer> first-layer|temp-tower|retraction|flow`) are checked by tests for every filament printer profile: moves inside the bed, nozzle temperatures inside the material's range, tower bands as stated. None has been printed on a real printer yet.

### Planning a print

`gantrel-printer plan` and `gantrel-printer check`. Details and formulas in [docs/PRINTERS.md](docs/PRINTERS.md#planning-a-print). Everything below is proven by unit tests on synthetic meshes built in code (`tests/plan/`); no planned part has been sliced or printed for real yet.

| Part | Proven |
|---|---|
| Orientation | Unit tests on synthetic meshes: the mushroom flips onto its cap, the arch lies on its side, the L bracket lies on its plate, the pin lies down when a load axis is given, a 230 mm bar is turned 90 degrees to fit a 250 x 210 bed. No real print. |
| Supports | Unit tests on synthetic meshes: support volume within 15 percent of the exact value (mushroom 16000 mm3, arch 6000 mm3). Overhang angle 45 degrees from vertical for every filament. No slicer comparison, no real print. |
| Time and filament estimate | Unit tests on a 20 mm cube against the formulas worked by hand. Never compared with a slicer's estimate or a real print time; treat it as a rough guide. The slicer G-code reader is tested on PrusaSlicer and OrcaSlicer comment formats only. |
| Settings check | Unit tests: every rule has a passing and a failing case. The material numbers are cited (materials.json); printer limits are cited for the Creality K2 and K2 Plus and are Gantrel's marked defaults elsewhere. |
| Optimizer | Unit tests: for every printer, filament and goal it either passes its own check or refuses with the reason. Not tuned against real prints. |
| Mesh files | Unit tests: binary and ASCII STL round trip, the first object of a 3MF, malformed files refused, size limits enforced. |

### Watching a print

`gantrel-printer watch` (frames, status lines, timelapse, notices) and its failure guard. Details in [docs/PRINTERS.md](docs/PRINTERS.md#watching-a-print).

| Part | Proven |
|---|---|
| Frames and status every N seconds, ending with the print | Fake, for all 12 adapter types (`tests/printers/test_watch.py`). Real: one read-only 62 s run on the Creality K2 through go2rtc, 5 frames, with the printer idle after a finished print. Never watched a real print from start to end. |
| Failure guard pauses after K failed frames, never on suspect or malformed answers, never resumes, starts or cancels | Fake only: a fake printer and a fake vision server. The guard has never paused a real printer. |
| Guard runs through Fonsviva with Gantrel's app key from the OS keyring, sends frames only to a checked local address, and records where each verdict ran | Fake only: a fake Fonsviva that requires the key (`tests/printers/test_watch_review.py`). Fonsviva has not been run on a real host for this feature yet. Off by default. |
| Vision models for the guard | Measured on 27 renders and 1 real K2 frame (table in [docs/PRINTERS.md](docs/PRINTERS.md#which-model-measured-2026-11-17)). None passed the gate (80% right, no good frame judged failed), so the guard is off unless the person asks for it and says it is experimental. The default, `gemma3:4b`, judged no good frame failed but caught only spaghetti (7 of 15 failed frames). |
| Timelapse MP4 | Fake frames checked with ffprobe; real: the K2 run's 5-frame H.264 MP4. Needs ffmpeg. |
| Notices to ntfy or a JSON webhook | Fake ntfy topic and webhook on 127.0.0.1 only. No real ntfy server has been tried. A failed notice logs only `scheme://host:port`, never the URL's token parts (tested in the foreground and in `watch.log`). |

### Loaded filament

`gantrel-printer filaments <name> [--json]` lists each slot's material family, color, amount left and nozzle range, read-only. Details in [docs/PRINTERS.md](docs/PRINTERS.md#loaded-filament).

| Part | Proven |
|---|---|
| Creality CFS parser (`box` object), through `--type moonraker` and `--type creality` | Recorded fixture and fake (`tests/printers/test_filaments.py`). Real: one read-only read on the owner's K2 (2026-11-22). The fixture is rebuilt from a summary of that reply, and the parser reproduces its four slots exactly. Only code 008001 (PLA) is known; every other Creality code shows as unknown. |
| Creality single external spool (no CFS, or not connected) | Fake only. Reports one `ext` slot with material unknown. |
| Moonraker with Spoolman (active spool through Moonraker's proxy) | Fake only. |
| Moonraker without Spoolman | Fake only. One `ext` slot with material unknown. |
| Bambu Lab AMS and external spool (`vt_tray`) | Fake only, from public protocol notes. |
| Every other printer type | Says it does not report loaded filament (tested). |

## Not supported yet

- Making a part through `/gantrel` on LFM2.5-8B-A1B or LFM2.5-24B-A2B. In 5 runs the 8B always ran `stone status` first, then lost the request: three times it asked the user what to make, once it ran `stone new` and then wandered through the job files, once it printed the NEXT line as text instead of running it. The 24B on parish1 (split across CPU and GPU) never sent a token within Eukhe's 300 and 600 s stream timeouts, in 2 runs straight to Ollama and 2 through the controller.
- The skill's missing-`stone` stop is untested.
- Gantrel Studio inside Local Studio's browser panel, and on macOS.
- Everything in the README's planned list.
