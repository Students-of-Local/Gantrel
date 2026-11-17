---
name: printers
description: Find, check, slice for and watch the person's 3D printers and their cameras with the gantrel-printer CLI (Klipper/Moonraker, Creality K1 and K2, Bambu Lab LAN mode, Prusa with PrusaLink, OctoPrint, Elegoo, Duet, FlashForge, Snapmaker, UltiMaker, Repetier-Server, Anycubic, Marlin over USB). Use when someone wants to print a file, see a printer's status or camera, slice a model for their printer, or stop a print.
---

# printers

## Goal

The model is sliced with the right profile for this printer and material, the file is on the
printer, and the person knows its name and where to start it. Or: the person sees their printer's
status and a camera image. Or: a print that looks wrong is paused or stopped.

You never start or resume a print. gantrel-printer has no command for it on purpose. The person
starts and resumes prints on the printer's screen or its own web page. Never try to start one
another way, such as calling the printer's API with curl.

## Inputs

- What the person wants: print a file, check a printer, look at the camera, or stop a print.
- The model file (STL or 3MF) or a sliced file (.gcode), if printing.
- The material loaded in the printer. Ask if you don't know it. Default PLA.

## First: is this a stop request?

If the person says stop, pause, cancel, or something is wrong (fire, smoke, a crash, spaghetti, a
loose part), do this at once, at any point, before anything else in this skill:

- Fire, smoke, a crash, or "stop now": run `gantrel-printer estop <name>`.
- Anything else wrong: run `gantrel-printer pause <name>`. Pause needs no permission.
- "Cancel the print": run `gantrel-printer cancel <name>`.
- If you don't know the printer name, run `gantrel-printer list` first. If only one printer is
  listed, use it.

Then tell the person what you did. Do not wait for any other step.

For "how is my printer" or "show me the camera", do steps 1, 2, 4 and 5 only, then stop.

## Steps to print

1. Run `command -v gantrel-printer`. If it prints nothing, tell the person to install it with this
   command, then stop:

       uv tool install git+https://github.com/baney75/Gantrel

   Never install it yourself.
2. Run `gantrel-printer list`. If the printer is there, go to step 4.
3. Pair the printer:
   - Run `gantrel-printer discover`. It lists printers it finds.
   - If nothing is found, ask the person for the printer's IP address.
   - Run `gantrel-printer profiles` and pick the --model that matches the printer.
   - Run `gantrel-printer pair <name> --type <type> --host <ip> --model <model>`. Always give
     --model; Bambu Lab printers require it and slicing needs it. Use the --type that discover
     printed, or pick it from the table below.
   - If it needs an API key or access code, ask the person to run the pair command
     themselves in a terminal so they type the secret. Never ask them to paste a secret to you.
   - For Snapmaker and UltiMaker, tell the person to stand at the printer before you run pair.
     Pair waits up to 2 minutes for them to accept Gantrel on the printer's screen.

   | Printer | --type | What the person reads off the printer or does |
   |---|---|---|
   | Klipper (Mainsail, Fluidd), Creality K2 Plus, Anycubic Kobra with Rinkhals or Kobra Klipper | moonraker | Only the IP address. An API key only if Moonraker asks for one. |
   | Creality K1, K2, Ender-3 V3, Hi on stock firmware | creality | Only the IP address. |
   | OctoPrint | octoprint | An application key from OctoPrint's settings (Application Keys). |
   | Prusa with PrusaLink | prusalink | The PrusaLink password or API key: printer menu Settings, Network, PrusaLink. Add --user if the login name is not maker. |
   | Bambu Lab | bambu | --serial (Settings, Device) and the LAN access code (Settings, Network). LAN-only mode and Developer Mode on. |
   | Elegoo Centauri Carbon, Saturn, Mars | elegoo | Only the IP address. The mainboard ID is found automatically. |
   | Duet | duet | The board password, only if one was set (most have none). |
   | FlashForge Adventurer 5M, 5M Pro, AD5X | flashforge | --serial (Settings, About) and the check code shown in network settings once LAN-only mode is on. |
   | Snapmaker 2.0 | snapmaker | No code: accept the request on the touchscreen during pair. |
   | UltiMaker S3, S5, S7, UM3 | ultimaker | No code: allow Gantrel on the screen during pair. Gantrel can't upload to UltiMaker; the person sends the file from Cura or a USB stick. |
   | Repetier-Server | repetier | The API key from the server's web page (Global Settings, Connectivity). With several printers, --serial set to the printer's slug. |
   | Marlin over USB (Ender 3, other Anycubic, many others) | serial | --host is the USB device such as /dev/ttyUSB0 (discover lists them). Pair only while the printer is idle: plugging in can restart it. |
4. Run `gantrel-printer status <name>`. The state must be idle, complete or cancelled to start a
   new print. If it is printing, paused, busy or error, tell the person and don't start a new print.
   Stop requests still go through the section above.
5. Run `gantrel-printer camera <name> --out cam.jpg` and look at the image, or show it to the
   person. The bed must be empty. If you can't tell, ask the person to check.
6. Slice: `gantrel-printer slice <model.stl> --printer <name> --material <PLA|PETG|ABS|ASA|TPU>`.
   Use `--quality draft|standard|fine|strong` if the person asked for speed, detail or strength.
   Tell the person the main settings it printed (layer height, temperatures, infill, time).
   If it says no slicer is installed, pass on its install advice and stop.
7. Upload: `gantrel-printer upload <file.gcode> --printer <name> --material <material>`.
   It prints a job id. Uploading never starts the print. Resin printers (Elegoo Saturn and Mars)
   are not sliced here: upload the .ctb or .goo file the person sliced in their resin slicer.
   If upload is refused (UltiMaker, or a USB printer with no SD card), tell the person what it
   said and how to get the file to the printer.
8. Tell the person the file name the upload printed and where to start it (the upload message
   says: the printer's screen, its web page, or the maker's app). Ask them to check the bed is
   empty first. Then wait until they say it started.
9. Watch: run `gantrel-printer status <name>` and `gantrel-printer camera <name> --out cam.jpg`
    every few minutes during the first layers.
10. Trouble you see on the camera: follow "First: is this a stop request?" above. Tell the person
    what you did and what you saw. Only they can resume it, on the printer.

## Rules and checks

- Every gantrel-printer reply ends with `NEXT: <what to do>`. Follow it. If you lose track,
  run `gantrel-printer status <name>` and read its NEXT line.
- Never start or resume a print, by any route. Never try to get around a REFUSED message. Tell
  the person what it said.
- Never print, log or repeat an API key, access code or token you were not handed for a command.
- Check status and the camera before every print, and the camera again after the first layer.
- ABS and ASA need an enclosed printer; TPU needs direct drive. If slice refuses, say why. Use
  --force only when the person asks for it after hearing the warning.
- If a command prints ERROR, tell the person the error and its advice in plain words.

## Sources

- docs/PRINTERS.md in this repo: safety rules and the adapter contract.
- Moonraker API: https://moonraker.readthedocs.io/en/latest/web_api/
- OctoPrint API: https://docs.octoprint.org/en/master/api/
- PrusaLink API: https://github.com/prusa3d/Prusa-Link-Web/blob/master/spec/openapi.yaml
- Bambu Lab LAN protocol notes: https://github.com/greghesp/ha-bambulab (MIT)
- Creality LAN protocol notes: https://github.com/axellse/crlancli (MIT)
- Elegoo SDCP: https://github.com/cbd-tech/SDCP
- Duet HTTP API: https://github.com/Duet3D/RepRapFirmware/wiki/HTTP-requests
- FlashForge 5M API notes: https://github.com/GhostTypes/ff-5mp-api-ts (MIT)
- Repetier-Server API: https://www.repetier-server.com/manuals/programming/API/index.html
- The adapters for Elegoo, Duet, FlashForge, Snapmaker, UltiMaker, Repetier-Server and USB serial
  are tested only against fake printers so far. If one misbehaves, pause or stop the print and
  tell the person.
