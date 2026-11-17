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
| Bambu Lab LAN adapter | Fake only (MQTT over TLS, implicit FTPS, port 6000 camera). Pair needs --model, pins each service's TLS certificate (MQTT, FTPS, camera) over connections that send no credentials, and every later connection must show its pinned certificate before the access code is sent; an unpinned service is refused. No network emergency stop: estop sends stop and heaters off and says so. X1-series RTSPS video needs a go2rtc relay. |
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
| Slicing | Profiles for 24 printer models and 5 materials (the new ones cite their spec pages in `slicing.py`; resin profiles exist only so pre-sliced .ctb and .goo uploads know the printer is resin, and slicing them is refused), with rules (ABS and ASA need an enclosure, TPU needs direct drive; `--force` overrides with a printed warning). Slicing needs the printer's --model; it is never guessed from the printer type. PrusaSlicer arguments are checked against a fake slicer. No real slicer has run yet; the OrcaSlicer path is untested. |
| Starting and resuming | Not in the tools. `gantrel-printer` has no start, resume or confirm command; after an upload it prints the file name on the printer and where a person starts it (the printer's screen, Mainsail or Fluidd, OctoPrint, PrusaLink, Bambu Studio, Creality Print, Duet Web Control, Repetier-Server). Snapmaker and UltiMaker prints are always started on the printer's touchscreen, because their APIs either can't start a stored file or start every file they receive. Files on the printer are named with their content hash and uploads never replace a file. The adapters' start and resume calls and the confirm-token code stay for the app's confirm sheet; nothing calls them yet, and only the fake-printer tests exercise them. |

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

## Agent-written adapters

An agent can write a new adapter by reading a device's docs or API reference. Before it is used:

1. It runs in its own sandboxed process that can reach only the device's address and its own folder (bubblewrap on Linux, sandbox-exec on macOS).
2. It must pass the contract tests against a fake device built from the docs, then a read-only status check against the real device.
3. The user sees the device, the addresses it can reach, the test results and the code, and approves or rejects it.
4. Gantrel stores the approved adapter's hash. Any change disables it until the user approves again.
5. The confirm-to-start rule still applies; an approved adapter can't start a print on its own.
