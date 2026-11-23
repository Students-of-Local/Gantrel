---
name: printers
description: Find, check, slice for and watch the person's 3D printers and their cameras with the gantrel-printer CLI (Klipper/Moonraker, Creality K1 and K2, Bambu Lab LAN mode, Prusa with PrusaLink, OctoPrint, Elegoo, Duet, FlashForge, Snapmaker, UltiMaker, Repetier-Server, Anycubic, Marlin over USB). Use when someone wants to print a file, see a printer's status or camera, slice a model for their printer, make a calibration print (first layer, temperature, retraction, flow), or stop a print.
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
For a calibration print, or prints that come out badly (not sticking, stringing, rough or gappy
surfaces), follow "Calibration prints" below.

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
   | Duet | duet | The board password, only if one was set (most have none). Pair in a terminal asks for it; press Enter when there is none. |
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
9. Watch: after the person says the print started, run

       gantrel-printer watch <name> --timelapse --background

   See "Watching a print" below. Then tell the person Gantrel is watching.
10. Trouble you see on the camera: follow "First: is this a stop request?" above. Tell the person
    what you did and what you saw. Only they can resume it, on the printer.

## Loaded filament

Before slicing, when the person hasn't said which filament is loaded, run
`gantrel-printer filaments <name> --json`. Each slot has `slot`, `empty`, `family` (PLA, PETG, ABS
and so on, or unknown), `color` and the amount left. Pick the slot that isn't empty and whose
family matches the print's filament, and slice with that material. If two slots match, none does,
or the family is unknown, ask the person which slot and filament to use. If it says the printer
doesn't report loaded filament, ask the person.

## Calibration prints

Use these when prints don't stick, string between parts, look rough or have gaps, or when the
person asks to tune a printer or a new filament. Do them in this order, one at a time, and only
the ones the problem needs:

| Problem | Kind | What it prints |
|---|---|---|
| Doesn't stick, first layer looks wrong | first-layer | Five one-layer squares: center and each corner |
| Strings, blobs, weak layers, new filament | temp-tower | Two pillars in 5 mm bands, hottest at the bottom, 5 C cooler per band |
| Strings or hairs between parts | retraction | Two pillars in 5 mm bands, more retraction per band |
| Gaps or ridges on top surfaces | flow | Five flat patches at 90, 95, 100, 105 and 110% flow, left to right |

1. Do steps 1 to 4 of "Steps to print" so the printer is paired with --model and idle.
2. Run `gantrel-printer calibrate <name> <kind> --material <material>`. It saves a G-code file on
   this computer and prints its path, the bands, and a NEXT line. It never starts a print, and it
   sends nothing to the printer unless you add `--upload`. Calibration prints are plain G-code
   made by Gantrel for that printer's bed, nozzle and material, so no slicer is needed.
3. If the person wants it on the printer, run it again with `--upload` (or run
   `gantrel-printer upload <file> --printer <name>`). Then do steps 5, 8 and 9 of "Steps to print":
   check the bed is empty, tell the person the file name and where to start it, and watch.
4. When it is done, tell the person exactly what the NEXT line says to look for, and which setting
   to change. For towers, band 1 is the bottom; for flow, patch 1 is the left one, marked by a
   small tab. Ask them which band or patch looks best, or look at a camera image with them.
5. Tell them the value to use and where it goes (the setting named in the NEXT line). Gantrel's own
   slice defaults don't change by themselves; say so.

## Watching a print

- `gantrel-printer watch <name> --timelapse --background` returns at once. It saves a camera frame
  every 30 seconds until the print ends, then makes a timelapse video.
- The failure guard is off unless the person asks for it. Then run
  `gantrel-printer watch <name> --guard --timelapse --background` instead. It sends each frame to a
  local vision model through Fonsviva, with Gantrel's Fonsviva key. If two frames in a row show a
  failed print, it pauses the printer. It never resumes, starts or cancels. Tell the person it is
  experimental: it was measured and misses many failed prints, so they should still check the
  camera. The guard has not yet run through a real Fonsviva install.
- If watch prints ERROR about Fonsviva or the vision model (no answer, no key, key refused, or no
  such model), tell the person what it said, including the `fonsviva status` check it names. Then run
  the same command without `--guard` and say the failure guard is off.
- Never set the Fonsviva key yourself; the person pipes it in with `watch-config --fonsviva-key-stdin`.
- Add `--notify` only if `gantrel-printer watch-config` shows a Notices line that is not "not set".
  Never set a notice URL yourself; the person pipes it in with `watch-config --notify-url-stdin`.
- When the person asks how the print is going, run `gantrel-printer frames <name>`. The newest job
  is first. Look at the last image in its folder's `frames/`, or show it to the person.
- If a job says `paused_by_guard`, tell the person it was paused and the "paused because" reason.
  Only they can resume it, on the printer, after they look at the print.
- If watch says the printer is already being watched, don't start another watch.

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
- A calibration print is still a print: the person starts it on the printer, with an empty bed.

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
