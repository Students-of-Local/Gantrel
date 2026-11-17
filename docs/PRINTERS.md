# Printers and cameras

This is the plan for how Gantrel connects to 3D printers and their cameras. The first part exists as
a command line tool, `gantrel-printer` (source in `src/gantrel_printer/`, skill in
`skills/plugin/printers/`). The status below says exactly what has been checked and how.

## Status (2026-11-16)

"Fake" means the shared contract suite in `tests/printers/` passed against a fake printer that speaks
the protocol over real sockets. "Real" means a check against a real printer.

| Part | Status |
|---|---|
| Moonraker adapter | Fake: full contract. Real: read-only status on a Creality K2 at 192.168.50.4:7125. Upload, start, pause, resume, cancel and emergency stop are not tried on real hardware. |
| Creality stock firmware adapter | Fake: full contract. Real: read-only status over WebSocket 9999 on the same K2 (model code F021, DWIN 1.1.7.0). Only state 0 (idle) was seen live; the other state codes and every write command are unverified. |
| OctoPrint adapter | Fake only. |
| PrusaLink adapter | Fake only. API key auth only; HTTP digest auth is not built. PrusaLink has no network emergency stop, so estop stops the job and says so. |
| Bambu Lab LAN adapter | Fake only (MQTT over TLS, implicit FTPS, port 6000 camera). Pins the TLS certificate on first pair and checks the pin inside the handshake of every connection, before the access code is sent. No network emergency stop: estop sends stop and heaters off and says so. X1-series RTSPS video needs a go2rtc relay. |
| Cameras | Snapshot from the printer's API, or from `--camera-url` for cameras the API doesn't list. Real: K2 chamber camera through the fleet's go2rtc relay (the K2 serves only WebRTC on port 8000). |
| Discovery | Known-port scan and probes checked against every fake and the real K2. mDNS and Bambu SSDP listening are built but not tested. |
| Slicing | Profiles for 14 printer models and 5 materials, with rules (ABS and ASA need an enclosure, TPU needs direct drive). PrusaSlicer arguments are checked against a fake slicer. No real slicer has run yet; the OrcaSlicer path is untested. |
| Confirm to start | Built and tested: `gantrel-printer confirm` needs a person at a real terminal and mints a single-use, 10-minute token bound to the action, job, file hash and the printer's identity (type, host, port, serial, TLS pin), so re-pairing a name voids it. Files on the printer are named with their content hash, uploads never replace a file, and start re-checks the printer's copy (size and modification time; Creality stock firmware can't report these). The confirm sheet strips control characters. Tested through a real pseudo-terminal. |

The confirm step stops an agent from starting a print by itself, because an agent's shell has no
terminal. A hostile program running as the same user could fake a terminal or edit the token files;
the in-app confirm sheet is the stronger boundary still planned.

## Safety rules

- **A print never starts without the user's confirm.** Start needs a confirm token that only the confirm sheet in the app creates, showing the file, printer, material and time. No agent, skill or script can start a print. Until the app exists, `gantrel-printer confirm` at a real terminal is that sheet.
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
| Elegoo (SDCP) and others | Written by an agent from the device's docs (see below) | Reference: [elegoo-homeassistant](https://github.com/danielcherubini/elegoo-homeassistant) (MIT). | 9 |

Slicing uses the OrcaSlicer, PrusaSlicer or Creality Print command line (all AGPL-3.0, run as separate programs).

`gantrel-printer` links only permissive libraries: websockets (BSD-3-Clause), paho-mqtt (EPL-2.0 or
EDL-1.0; used under EDL-1.0, which is BSD-3-Clause), python-zeroconf (LGPL-2.1) and keyring (MIT).
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
