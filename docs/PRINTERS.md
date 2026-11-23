# Printers and cameras

This is the plan for how Gantrel connects to 3D printers and their cameras. The first part exists as
a command line tool, `gantrel-printer` (source in `src/gantrel_printer/`, skill in
`skills/plugin/printers/`). The status below says exactly what has been checked and how.

## Status (2026-11-17)

"Fake" means the shared contract suite in `tests/printers/` passed against a fake printer that speaks
the protocol over real sockets (a pseudo terminal for USB serial). "Real" means a check against a
real printer. Every adapter added on 2026-11-17 (Elegoo, Duet, FlashForge, Snapmaker, UltiMaker,
Repetier-Server, USB serial) is fake only: each fake was written from public protocol notes, so a
mistake in those notes is a mistake in both the adapter and its fake.

| Part | Status |
|---|---|
| Moonraker adapter | Fake: full contract. Real: read-only status on a Creality K2 at 192.168.50.4:7125. Upload, start, pause, resume, cancel and emergency stop are not tried on real hardware. |
| Creality stock firmware adapter | Fake: full contract. Real: read-only status over WebSocket 9999 on the same K2 (model code F021, DWIN 1.1.7.0). Only state 0 (idle) was seen live; the other state codes and every write command are unverified. |
| OctoPrint adapter | Fake only. |
| PrusaLink adapter | Fake only. Takes the API key or the PrusaLink password: every call sends the X-Api-Key header and answers HTTP digest auth (realm "Printer API", user `maker` unless `--user` says otherwise), only to the printer's own address. PrusaLink has no network emergency stop, so estop stops the job and says so. |
| Bambu Lab LAN adapter | Fake only (MQTT over TLS, implicit FTPS, port 6000 camera). Pair needs --model, pins each service's TLS certificate (MQTT, FTPS, camera) over connections that send no credentials, and every later connection must show its pinned certificate before the access code is sent; an unpinned service is refused. No network emergency stop: estop sends stop and heaters off and says so. X1-series RTSPS video needs a go2rtc relay. Bambu keeps `gcode_state` FAILED after a person stops a print, until the next print; with `print_error` 0 or a user cancel code (0300400C, 0500400E) and no HMS entry, status reports that as cancelled, and any other `print_error` or an HMS entry stays an error. |
| Elegoo SDCP adapter (`--type elegoo`) | Fake only. Centauri Carbon (FDM) and the Saturn and Mars resin printers that speak SDCP v3: WebSocket 3030, UDP 3000 discovery ("M99999"), chunked HTTP upload, MJPEG camera on 3031. Sends only the six Cmds the protocol notes list as confirmed (0, 128, 129, 130, 131, 258), because the notes say the Centauri Carbon firmware can crash on others. Resin printers take pre-sliced .ctb and .goo files; Gantrel does not slice resin. |
| Duet adapter (`--type duet`) | Fake only. RepRapFirmware's rr_ API and DSF's /machine API (detected at pair), optional board password. Pause is M25; cancel pauses first, waits for paused, then sends M0; estop is M112. Duet has no camera API, so cameras need `--camera-url`. |
| FlashForge adapter (`--type flashforge`) | Fake only. Adventurer 5M, 5M Pro and AD5X over HTTP 8898 with the serial number and check code. Uploads send printNow false. When HTTP doesn't answer, status falls back to the legacy TCP API on 8899 (status, pause and cancel only). Camera is MJPEG on 8080. |
| Snapmaker adapter (`--type snapmaker`) | Fake only. Snapmaker 2.0 (A150, A250, A350) HTTP API on 8080. Pair asks the printer for a token and the person accepts it on the touchscreen. Its API cannot start a stored file by name, so every print is started by hand from the touchscreen. The J1 and Artisan may speak only SACP on newer firmware; untested. No camera API. |
| UltiMaker adapter (`--type ultimaker`) | Fake only. UM3, S3, S5 and S7 `/api/v1`. Pair requests credentials and the person allows them on the printer's screen; writes use digest auth. **No upload**: the printer's API starts printing as soon as it receives a file, so Gantrel refuses to upload to it. Status, camera, pause and cancel work; estop sends abort and says there is no network emergency stop. |
| Repetier-Server adapter (`--type repetier`) | Fake only. REST API with the x-api-key header. Upload stores the file in the model list without queueing it. A server running several printers needs `--serial <slug>`. |
| USB serial adapter (`--type serial`) | Fake only (a pseudo terminal running a Marlin fake). `--host` is the device path, `--port` the baud rate (default 115200). Status from M105 and M27. Uploads go to the printer's SD card with M28/M29 under an 8.3 name, only when M115 reports `Cap:SDCARD:1`; otherwise upload is refused, because Gantrel never streams a print over USB. Estop is M112. Opening the port restarts boards with Arduino style auto-reset, which would kill a running print: pair only while the printer is idle. The adapter turns off hang-up-on-close so later opens don't reset the board, but a board that resets on every open will still restart. |
| Anycubic | No adapter of its own. A Kobra running [Rinkhals](https://github.com/jbatonnet/Rinkhals) or Kobra Klipper exposes Moonraker, so use `--type moonraker`; any other Anycubic FDM printer works over USB with `--type serial`. Anycubic's own cloud and app protocol is not supported. |
| Cameras | Snapshot from the printer's API, or from `--camera-url` for cameras the API doesn't list. Real: K2 chamber camera through the fleet's go2rtc relay (the K2 serves only WebRTC on port 8000). |
| Discovery | Known-port scan and probes checked against every fake (`gantrel-printer discover --host 127.0.0.1` finds each one in the tests) and the real K2. mDNS, Bambu SSDP listening and the Elegoo UDP broadcast are built but not tested on a real network. USB serial printers are listed from `/dev/serial/by-id` without opening the port. |
| Slicing | Profiles for 24 printer models and 5 materials (the new ones cite their spec pages in `slicing.py`; resin profiles exist only so pre-sliced .ctb and .goo uploads know the printer is resin, and slicing them is refused), with rules (ABS and ASA need an enclosure, TPU needs direct drive; `--force` overrides with a printed warning). Slicing needs the printer's --model; it is never guessed from the printer type. Klipper profiles retract with plain G1 E moves of the printed `retract_mm`, never G10/G11 firmware retraction, so a printer.cfg without `[firmware_retraction]` still retracts. PrusaSlicer arguments are checked against a fake slicer. No real slicer has run yet; the OrcaSlicer path is untested. |
| Calibration prints | `gantrel-printer calibrate <printer> <kind>` makes first-layer, temp-tower, retraction or flow G-code for a paired printer (or a profile key) and saves it locally. The G-code is written by `calibrate.py` from the printer's slicing profile (bed, nozzle, filament diameter, start and end G-code) and the material, so no slicer is needed; Klipper profiles use their START_PRINT macro. It never starts a print and uploads only with `--upload`, through the same path as `gantrel-printer upload`. Tests check every filament profile and material: every move inside the bed, every nozzle temperature inside the material's range, temp-tower bands 5 C apart and 5 mm tall, retraction bands retracting their stated distance. No calibration print has run on a real printer yet. |
| Watch, guard, timelapse, notices | Fake: `tests/printers/test_watch.py` takes frames and status from every adapter's fake, pauses a fake printer after K failed verdicts from a fake vision server, never pauses on suspect or malformed answers, builds an MP4 that ffprobe reads, and posts to a fake ntfy topic and webhook. Real: a read-only 62 s watch of the K2 at 10 s intervals (5 frames and a timelapse, 2026-11-17); the guard has never paused a real printer. No local vision model passed the guard gate, so the guard is off unless asked for (see Which model). See Watching a print. |
| Starting and resuming | Not in the tools. `gantrel-printer` has no start, resume or confirm command; after an upload it prints the file name on the printer and where a person starts it (the printer's screen, Mainsail or Fluidd, OctoPrint, PrusaLink, Bambu Studio, Creality Print, Duet Web Control, Repetier-Server). Snapmaker and UltiMaker prints are always started on the printer's touchscreen, because their APIs either can't start a stored file or start every file they receive. Files on the printer are named with their content hash and uploads never replace a file. The adapters' start and resume calls and the confirm-token code stay for the app's confirm sheet; nothing calls them yet, and only the fake-printer tests exercise them. Start and resume spend the token before any other check, so a refused or mismatched use voids it. Start tokens bind to the job record and its file's SHA-256; resume tokens bind to the job record of the print the printer is paused on, so a print Gantrel didn't upload can't be resumed with a token, and a file deleted and uploaded again gets a new record that an old token doesn't match. |

Gantrel's tools never start or resume a print; a person does that on the printer. This is not a
security boundary: an agent with shell access can still reach a printer's API directly (for example
with curl), and a terminal prompt can be faked. The real boundary comes later, with Local Studio
capability grants and the in-app confirm sheet.

## Safety rules

- **A print never starts without the user's confirm.** Start needs a confirm token that only the confirm sheet in the app creates, showing the file, printer, material and time. Until that sheet exists, Gantrel's tools have no start or resume at all: the person starts and resumes prints on the printer itself.
- **Emergency stop is always visible and works in one press.**
- **Pause needs no confirm**, because it's a safe action. Resume does.
- Pairing credentials live in the OS keychain, never in plain config.
- Gantrel talks to printers on the local network. Cloud services (PrusaConnect, Bambu Cloud, Creality Cloud) come after v0 and only as an opt-in.

## Adapter contract

Every printer type is an adapter that implements the same calls: discover, pair, status stream, upload, start (with confirm token), pause, resume, cancel, emergency stop, camera streams, and errors. One contract test suite runs against a fake printer for every adapter, and against real printers on the "planned first" list in [SUPPORTED.md](../SUPPORTED.md).

## Planned adapters

| Adapter | How it talks | License note | Build |
|---|---|---|---|
| Klipper with [Moonraker](https://github.com/Arksine/moonraker) | HTTP and WebSocket API | Moonraker and Klipper are GPL-3.0; Gantrel only calls the API. | 5 |
| [OctoPrint](https://github.com/OctoPrint/OctoPrint) | REST API with an app key the user approves in OctoPrint | AGPL-3.0; API only. | 6 |
| [PrusaLink](https://github.com/prusa3d/Prusa-Link) | Local API with the printer's API key | GPL-3.0; API only. PrusaConnect is cloud, after v0. | 6 |
| Bambu Lab, LAN mode | MQTT over TLS (port 8883) with the access code, FTPS upload, camera stream | Protocol references: [ha-bambulab](https://github.com/greghesp/ha-bambulab) (MIT), [bambulabs_api](https://github.com/acse-ci223/bambulabs_api) (MIT). Newer firmware needs LAN-only mode with Developer Mode on for third-party control; the adapter checks and explains. | 7 |
| Creality stock firmware (K1, K2, Ender-3 V3, Hi) | Local WebSocket on port 9999 and HTTP upload, the same API Creality Print uses | Not officially documented and may change with firmware. Reference: [crlancli](https://github.com/axellse/crlancli) (MIT). Record the firmware version tested. | 7 |
| Creality K2 Plus, rooted K1 | Moonraker (the K2 Plus ships it behind port 4408) | Uses the Moonraker adapter. Creality publishes its Klipper sources ([K1](https://github.com/CrealityOfficial/K1_Series_Klipper), [K2](https://github.com/CrealityOfficial/K2_Series_Klipper), GPL-3.0). | 7 |
| Elegoo (SDCP v3) | WebSocket 3030, UDP 3000 discovery, HTTP upload | Built (fake only). References: [SDCP spec](https://github.com/cbd-tech/SDCP), [elegoo-homeassistant](https://github.com/danielcherubini/elegoo-homeassistant) (MIT), [pycentauri protocol notes](https://github.com/bjan/pycentauri/blob/main/docs/PROTOCOL.md). | 9 |
| Duet (RepRapFirmware, DSF) | rr_ HTTP API or DSF /machine API | Built (fake only). RRF and DSF are GPL-3.0; API only. [HTTP requests](https://github.com/Duet3D/RepRapFirmware/wiki/HTTP-requests). | 9 |
| FlashForge Adventurer 5M, 5M Pro, AD5X | HTTP 8898, legacy TCP 8899 | Built (fake only). Reference: [ff-5mp-api-ts](https://github.com/GhostTypes/ff-5mp-api-ts) (MIT). | 9 |
| Snapmaker 2.0 | HTTP 8080 with a touchscreen-approved token | Built (fake only). [Luban](https://github.com/Snapmaker/Luban) is AGPL-3.0; read for the API only. | 9 |
| UltiMaker UM3 and S series | `/api/v1` with digest credentials approved on the screen | Built (fake only), no upload. Cura's network plugin is LGPL-3.0; read for the API only. | 9 |
| [Repetier-Server](https://www.repetier-server.com/manuals/programming/API/index.html) | REST API with an API key | Built (fake only). | 9 |
| Marlin over USB | Serial G-code through pyserial | Built (fake only). SD card upload only; never streams. | 9 |

Slicing uses the OrcaSlicer, PrusaSlicer or Creality Print command line (all AGPL-3.0, run as separate programs).

`gantrel-printer` links only permissive libraries: websockets (BSD-3-Clause), paho-mqtt (EPL-2.0 or
EDL-1.0; used under EDL-1.0, which is BSD-3-Clause), python-zeroconf (LGPL-2.1), keyring (MIT) and pyserial (BSD-3-Clause).
HTTP uses the Python standard library, which keeps certifi (MPL-2.0) out.

## Cameras

| Part | License | Use |
|---|---|---|
| [go2rtc](https://github.com/AlexxIT/go2rtc) | MIT | Turns MJPEG, RTSP and WebRTC streams, including Bambu's, into one low-latency stream in the app. |
| [crowsnest](https://github.com/mainsail-crew/crowsnest), [moonraker-timelapse](https://github.com/mainsail-crew/moonraker-timelapse) | GPL-3.0 | Run on Klipper printers; Gantrel only reads their streams and files. |
| FFmpeg | LGPL-2.1 or GPL | Builds timelapses for printers that don't. |
| [Obico](https://github.com/TheSpaghettiDetective/obico-server) | AGPL-3.0 | Optional local failure detection that pauses the print. Run unmodified as its own service. |

## Watching a print

`gantrel-printer watch <name>` follows a print the person started. Every `--interval` seconds
(default 30) it saves a camera frame and a status line to a job folder under the state dir
(`~/.local/state/gantrel-printer/watch/<name>/<UTC time>/`), and it ends when the print does. It
waits up to `--start-timeout` seconds (default 600) for the print to start, and it gives up after
five status reads in a row fail. `--any-state` takes frames whatever the printer is doing and
`--duration` stops after a set time; together they check a camera before a print. `--background`
runs the watch as its own process and returns at once, so an agent or chat is not blocked for hours.
Only one watch runs per printer. `gantrel-printer frames <name>` lists the job folders, how each
ended and why the guard paused, and `frames <name> --timelapse <job>` makes a job's timelapse again.

| File in the job folder | What it holds |
|---|---|
| `job.json` | Printer, start and end, how it ended (finished, ended, failed, cancelled, paused_by_guard, lost, not_started, time_up, stopped), frame count, timelapse, guard settings. |
| `status.jsonl` | One status line per tick, with the frame it took and the guard's verdict. |
| `frames/` | `000001.jpg`, `000002.jpg`, ... |
| `guard/`, `guard.jsonl` | The last 12 frames the guard judged, each with its verdict in `<frame>.json`, and every verdict. |
| `pause.json` | Only when the guard paused: the failed frames, the model's reasons and the model used. |
| `timelapse.mp4` | With `--timelapse`, made by ffmpeg when the print ends. Without ffmpeg the watch says so and tells the person how to make it later. |

Duet, OctoPrint, Repetier-Server and Marlin printers go back to idle after a print whether it
finished or was stopped. The watch calls that `finished` only when the last progress it read was 98%
or more, and `ended` otherwise.

### Failure guard (`--guard`)

With `--guard`, each frame taken while the printer reports printing goes to a vision model through
[Fonsviva](https://github.com/baney75/Fonsviva), like all of Gantrel's compute: Fonsviva's
OpenAI-compatible `/v1/chat/completions` endpoint, by default `http://127.0.0.1:7341/v1`. Gantrel
sends its own Fonsviva app key as the bearer. The guard is measured (below), off by default, and has
not yet been run through a real Fonsviva install: every guard test uses a fake Fonsviva that requires
the key.
The prompt (in `src/gantrel_printer/guard.py`) gives the printer's state, progress and layer, lists
spaghetti, a part loose from the bed, blobs, layer shift, a clog or no extrusion, and an empty bed
during a print, and asks for `{"reason": "...", "verdict": "ok|suspect|failed"}`.

- The guard pauses only after `failed` on K frames in a row (`--consecutive`, default 2), only while
  the printer reports printing, and only after reading the status once more just before the pause.
  Pause is a safe action (see Safety rules), so it needs no confirm.
- Anything that is not a clean answer counts as `suspect`, never `failed`: prose, bad JSON, an
  unknown verdict, an empty reply, or a model that doesn't answer. Suspect frames never pause.
- The watch reaches the printer through `monitor.PrinterHandle`, which exposes status, frames and
  pause only. It has no start, resume or cancel, and the tests fail if the watch calls any of them.
- Frames are pictures of the person's home, so the vision URL must resolve to this machine or the
  local network (loopback, private ranges, link-local, or Tailscale's 100.64.0.0/10). Each request
  looks the host up once, checks every address, and connects to that address, so a DNS answer that
  changes between the check and the connection cannot send a frame or the key elsewhere. Over https
  the certificate is still checked against the host name.
- Gantrel's Fonsviva app key lives in the OS keyring, never in a file or a log. The person sets it
  with `gantrel-printer watch-config --fonsviva-key-stdin` (approve the app first with
  `fonsviva keys approve gantrel`; see [Connect to Fonsviva](setup/connect-fonsviva.md)).
- Each verdict records where it ran: Fonsviva's host and port, the model asked for, and the model
  and any `X-Fonsviva-*` headers Fonsviva sent back. They go in `guard.jsonl`, in `job.json` (the
  last 50 verdicts under `guard.verdicts`), in `pause.json`, and in the watch log line.
- If Fonsviva does not answer, refuses the key, has no key saved, or lacks the model, `watch --guard`
  stops before it starts with an ERROR that names `fonsviva status`; run the watch again without
  `--guard`, as the skill does.
- Only a whole JSON object counts as an answer, with at most one Markdown code fence around it. An
  array, or prose around the object, is malformed (suspect), so it never pauses.
- If the pause call fails, the watch keeps going, retries on the next failed frame, and sends a
  `pause_failed` notice once.

Set the model once with `gantrel-printer watch-config --vision-model <model>` (saved in `watch.json`
next to `printers.json`), or per run with `--vision-model`. `--vision-url` is only for Fonsviva at
another address on the local network, such as another machine's Tailscale name.

#### Which model (measured 2026-11-17)

**The guard is off unless the person asks for it.** The skill and the NEXT lines leave out `--guard`,
and a watch with `--guard` prints that it is experimental, because no local vision model passed the
gate: 80% or more right on failed vs not failed over the labeled frames in `tests/printers/frames/`,
with no good frame judged failed. The set has 13 good frames and 15 failed ones (7 spaghetti, 6 parts
knocked loose or dragged, 2 empty beds during a print): 27 Blender renders and one real K2 frame (see
the folder's README). Every model ran through the same `VisionGuard.classify()` the watch uses, with
the printer status in each frame's label (`benchmarks/guard_vision.py`; every answer is in
`benchmarks/results/guard-vision/`).

<!-- guard-table: uv run python benchmarks/guard_vision.py --table -->
| Model | Accuracy | Failed frames caught | Good frames judged failed | Malformed | Median / max latency | Passes the gate | Host | Date |
|---|---|---|---|---|---|---|---|---|
| `qwen2.5vl:3b` | 79% | 12/15 (detached 5/6, empty_bed 0/2, spaghetti 7/7) | 3 of 13 | 0 | 33.3 s / 181.5 s | no | abbey1 (GTX 1650 SUPER 4 GB, Ollama 0.12.6) | 2026-11-17 |
| `qwen2.5vl:7b` | 75% | 13/15 (detached 4/6, empty_bed 2/2, spaghetti 7/7) | 5 of 13 | 0 | 52.2 s / 71.5 s | no | abbey1 (GTX 1650 SUPER 4 GB, Ollama 0.12.6) | 2026-11-17 |
| `gemma3:4b` | 71% | 7/15 (detached 0/6, empty_bed 0/2, spaghetti 7/7) | 0 of 13 | 0 | 127.9 s / 486.9 s | no | abbey1 (GTX 1650 SUPER 4 GB, Ollama 0.40.15) | 2026-11-17 |
| `hf.co/LiquidAI/LFM2-VL-1.6B-GGUF:Q8_0` | 46% | 0/15 (detached 0/6, empty_bed 0/2, spaghetti 0/7) | 0 of 13 | 26 | 2.4 s / 49.6 s | no | abbey1 (GTX 1650 SUPER 4 GB, Ollama 0.40.15) | 2026-11-17 |
<!-- /guard-table -->

- The default model is `gemma3:4b` because it never judged a good frame failed, so in these tests a
  watch with `--guard` does not pause a good print. It catches only spaghetti: it called every part
  knocked loose and every empty bed ok or suspect.
- Both Qwen2.5-VL models catch more failures but read a small part half hidden behind the toolhead
  as an empty bed, and call a good print failed. Two such frames in a row would pause a good print.
- LFM2-VL 1.6B put its verdict word in the `reason` field, so the guard counted the answers as
  malformed (suspect) and never paused, which is the safe way to fail.
- `qwen3-vl:4b` reasons before it answers, and those tokens count against the answer limit. At the
  old limit of 200 tokens it sent no JSON on most frames, so the guard now allows 1024 (the four rows
  above ran at 200, and none of those answers was cut off). Two probe
  frames at the new limit came back right (spaghetti `failed` in 118 s, a good print `ok` in 499 s),
  but it has no full-set row yet: on the shared GPU a full run takes hours.
- Times are from abbey1, whose 4 GB GPU was shared with another model during the runs, so they are
  slower than on a free GPU. The watch waits for each answer and then takes the next frame, so a slow
  model stretches the time between frames; it never piles up requests.
- The renders are cleaner than a real chamber camera. A model that passes here is a candidate, not
  proof that it catches real failures.

Measure another model the same way and add its row. The benchmark is a developer tool: it talks to a
model server directly (the rows above ran against Ollama), with no Fonsviva key, so a model can be
measured before it is offered through Fonsviva. The watch never does this.

    uv run python benchmarks/guard_vision.py --url <model server>/v1 --model <model> --host "<machine and GPU>"
    uv run python benchmarks/guard_vision.py --table

One read-only real check: frame 5 of the K2 run (state complete, 100%, a finished vase on the bed)
went through the guard with `gemma3:4b`: `suspect`, "The bed shows a dark, partially printed object
with some visible supports.", in 250 s. Suspect never pauses.

### Notices (`--notify`)

When a watch ends, `--notify` sends one notice: finished, ended, failed, cancelled, paused by the
guard, or lost contact. A failed pause sends `pause_failed` at once. Gantrel has no default target.
Set one with:

    printf '%s\n' 'https://ntfy.sh/<your-topic>' | gantrel-printer watch-config --notify-url-stdin
    printf '%s\n' 'http://192.168.1.20:8123/api/webhook/<id>' | gantrel-printer watch-config --notify-url-stdin --notify-kind webhook

The URL goes to the OS keyring, not a file, because webhook URLs often carry a token; `watch.json`
keeps only the kind and the host. A notice that fails is logged with only `scheme://host:port` and
the kind of error, never the path, query or user of the URL, in the foreground and in a background
watch's `watch.log`. An ntfy topic gets a POST with Title, Priority and Tags headers, or
a PUT with the last frame as an attachment. A webhook gets a JSON POST:
`{"event", "title", "message", "printer", "file", "time", "frame": {"name", "content_type", "base64"}}`.
The frame goes along only when the target is on the local network (decided on each send, from the
same DNS answer the send connects to), or after
`watch-config --attach-frame`: anyone who knows a public ntfy topic's name can read it, so use a
long random topic name or a self-hosted ntfy server. `--no-attach-frame` turns frames off, and
`--clear-notify` forgets the target. A notice that fails is logged and never stops the watch.

## Loaded filament

`gantrel-printer filaments <name> [--json]` lists what is loaded right now, one row per slot: the
slot id, whether it is empty, the material family (PLA, PETG, ABS, ASA, TPU, PA, PC, PLA-CF,
PETG-CF, PVA, HIPS, other, or unknown when the printer doesn't say), the printer's own material word
or code, the brand and product name when the printer gives them, the color as `#rrggbb`, percent or
grams left when known, the nozzle temperature range when the printer reports one, and `source`, the
API field the row came from. Agents use it to pick the slot whose family matches the print's
filament, and ask the person when two slots match or none does.

It only reads. Every reply is capped at 1 MiB before it is parsed (Bambu reports at 4 MiB), and
every field is treated as untrusted: strings are cut to 64 printable characters, colors must be
6-digit hex, and numbers outside a sane range are dropped. A reply in a shape Gantrel can't read is
a plain `protocol` error, never a crash.

| Adapter | What it reads | Slots | Proven |
|---|---|---|---|
| Moonraker on a Creality K-series with a CFS | Klipper's `box` object (`/printer/objects/query?box`): per unit T1 to T4, `material_type`, `color_value`, `remain_len`, `dry_and_humidity`, `temperature` | `T1A` to `T4D`; `ext` when there is no box or it is not connected | Fixture. Real: one read-only read on the owner's K2 (2026-11-22), see below |
| Creality stock firmware (`--type creality`) | The same `box` object, from the K2's own Moonraker on port 7125 (`extra.moonraker_port` changes it). The stock socket on 9999 has no call for it that Gantrel knows. | as above | Fixture and fake. On a K1, Ender-3 V3 or Hi without Moonraker it says it can't report filament |
| Moonraker with Spoolman | `/server/spoolman/status`, `/server/spoolman/spool_id`, then the active spool through Moonraker's proxy (`POST /server/spoolman/proxy` asking Spoolman for `GET /v1/spool/<id>`): material, vendor, name, color, remaining weight | `ext` | Fake only |
| Moonraker without Spoolman or a CFS | Nothing to read | one `ext` slot, family unknown | Fake only |
| Bambu Lab LAN | The `print.ams` and `print.vt_tray` parts of the pushall report status already asks for: `tray_type`, `tray_sub_brands`, `tray_color`, `remain` (-1 means unknown), `nozzle_temp_min` and `nozzle_temp_max` | `A1` to `A4` for the first AMS, `B1` to `B4` for the next, `ext` for the external spool | Fake only |
| Every other adapter | PrusaLink's `/api/v1/status` has no filament (MMU is out of scope); OctoPrint, Elegoo, Duet, FlashForge, Snapmaker, UltiMaker, Repetier-Server and USB serial have no filament report Gantrel reads | none: an `unsupported` error saying so | Tested |

**The K2 read.** On 2026-11-22 a read-only tool queried `box` on the owner's K2 once and reported
a summary: unit T1 connected, humidity 41, temperature 25; T1A empty; T1B code 008001, color ffffff,
100 percent; T1C empty; T1D code 008001, color 000000, 0 percent. The fixture in
`tests/printers/test_filaments.py` is rebuilt from that summary, not from the raw bytes, and the
parser reproduces exactly those rows from it.

**Creality material codes.** `box` reports a 6-character Creality code per slot ("-1" when empty).
Creality's firmware takes it from the spool's RFID text
([Hi_Klipper `external_material_wrapper.py` at 646b6aa](https://github.com/CrealityOfficial/Hi_Klipper/blob/646b6aaaa4dc1fe9c4f773f1a425ea4666e5ed9b/klippy/extras/external_material_wrapper.py)).
Creality Print does not publish these codes. At
[CrealityPrint 6262848](https://github.com/CrealityOfficial/CrealityPrint/tree/6262848e302b8c0d593f2b341cd01310aa2f58a1)
its filament profiles carry 5-character `filament_id` values (08001 is Ender-PLA in
`resources/profiles/Creality/filament/Ender-PLA @Creality K2 0.4 nozzle.json`), and it downloads
its full material list from Creality's cloud at run time (`src/slic3r/GUI/MaterialListDownloader.cpp`).
Mapping 008001 to 08001 by dropping the first character would be a guess, so
`src/gantrel_printer/data/creality_materials.json` lists only 008001 as PLA, seen on the owner's K2,
with no brand, product name or temperatures. Every other code shows as family unknown with the code
in the material field, and the agent asks the person.

## Agent-written adapters

An agent can write a new adapter by reading a device's docs or API reference. Before it is used:

1. It runs in its own sandboxed process that can reach only the device's address and its own folder (bubblewrap on Linux, sandbox-exec on macOS).
2. It must pass the contract tests against a fake device built from the docs, then a read-only status check against the real device.
3. The user sees the device, the addresses it can reach, the test results and the code, and approves or rejects it.
4. Gantrel stores the approved adapter's hash. Any change disables it until the user approves again.
5. The confirm-to-start rule still applies; an approved adapter can't start a print on its own.
