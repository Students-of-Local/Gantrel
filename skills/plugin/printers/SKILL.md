---
name: printers
description: Find, check, slice for and watch the person's 3D printers and their cameras with the gantrel-printer CLI (Klipper/Moonraker, Creality K1 and K2, Bambu Lab LAN mode, Prusa with PrusaLink, OctoPrint). Use when someone wants to print a file, see a printer's status or camera, slice a model for their printer, or stop a print.
---

# printers

## Goal

The model is sliced with the right profile for this printer and material, the file is on the
printer, and the person has the exact confirm command to start it. Or: the person sees their
printer's status and a camera image. Or: a print that looks wrong is paused or stopped.

You can never start or resume a print. Only the person can, by running a confirm command in
their own terminal. Your shell has no terminal, so confirm refuses if you run it.

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
   - Run `gantrel-printer pair <name> --type <type> --host <ip> --model <model>`.
   - If it needs an API key or access code, ask the person to run the pair command
     themselves in a terminal so they type the secret. Never ask them to paste a secret to you.
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
   It prints a job id. Uploading never starts the print.
8. Give the person the exact line from NEXT, for example:

       gantrel-printer confirm 1a2b3c4d

   Tell them: "Run this in your own terminal. It shows the file, printer, material and time,
   and asks you to type the printer's name. Then it gives you a start command." Do not run it.
9. If the person pastes back a `gantrel-printer start <job> --token <token>` line, you may run it
   exactly as given. Never make up, change or reuse a token.
10. Watch: run `gantrel-printer status <name>` and `gantrel-printer camera <name> --out cam.jpg`
    every few minutes during the first layers.
11. Trouble you see on the camera: follow "First: is this a stop request?" above. Tell the person
    what you did and what you saw. To resume, they run `gantrel-printer confirm --resume <name>`
    themselves.

## Rules and checks

- Every gantrel-printer reply ends with `NEXT: <what to do>`. Follow it. If you lose track,
  run `gantrel-printer status <name>` and read its NEXT line.
- Never run `confirm`. Never try to get around a REFUSED message. Tell the person what it said.
- Never print, log or repeat an API key, access code or token you were not handed for a command.
- Check status and the camera before every print, and the camera again after the first layer.
- ABS and ASA need an enclosed printer; TPU needs direct drive. If slice refuses, say why.
- If a command prints ERROR, tell the person the error and its advice in plain words.

## Sources

- docs/PRINTERS.md in this repo: safety rules and the adapter contract.
- Moonraker API: https://moonraker.readthedocs.io/en/latest/web_api/
- OctoPrint API: https://docs.octoprint.org/en/master/api/
- PrusaLink API: https://github.com/prusa3d/Prusa-Link-Web/blob/master/spec/openapi.yaml
- Bambu Lab LAN protocol notes: https://github.com/greghesp/ha-bambulab (MIT)
- Creality LAN protocol notes: https://github.com/axellse/crlancli (MIT)
