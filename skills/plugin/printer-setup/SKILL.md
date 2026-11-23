---
name: printer-setup
description: Set up a new 3D printer with the gantrel-printer CLI from start to finish (find it, pair it, check its camera and filament, run calibration prints, plan, slice and upload), then save the working setup as a printer pack and, only with the person's yes, share it with other owners of the same printer. Also imports a pack someone else shared, after the person reviews it. Use when someone has a new printer, wants their printer set up, wants to share or save their printer settings, or wants to use printer settings someone else shared.
---

# printer-setup

## Goal

The person's printer goes from new to a good print, and its working setup is saved as a printer
pack. If the person says yes, the pack is shared with other owners of the same printer.

Or: a pack someone else shared is reviewed with the person and, if they say yes, imported.

A pack holds only printer settings: no addresses, serials, codes, names or paths. A pack never
connects to a printer. You never start or resume a print. The person starts every print on the
printer.

## Inputs

- The printer's make and model. Ask if you don't know it.
- The filament the person prints with. Ask if you don't know it. Default PLA.
- For sharing: the person's yes, given after they saw exactly what goes out.

## Steps: a new printer

Do the steps in order. Stop at any step that fails and tell the person what it said.

1. Run `command -v gantrel-printer`. If it prints nothing, tell the person to install it with
   `uv tool install git+https://github.com/baney75/Gantrel`, then stop. Never install it yourself.
2. Find the printer: `gantrel-printer discover`. If nothing is found, ask the person for the
   printer's IP address (the printer's screen, Network).
3. Pick the model: `gantrel-printer profiles`. Use the key that matches the printer, such as
   `creality-k2`.
4. Pair: `gantrel-printer pair <name> --type <type> --host <ip> --model <model>`.
   - Use the --type that discover printed. The printers skill has the table of types.
   - If the printer needs an access code, API key or password, the person types it. Ask them to
     run the pair command themselves in a terminal. Never ask them to tell you the code. Never
     repeat, print or save a code.
5. Status: `gantrel-printer status <name>`. It must say idle, complete or cancelled.
6. Camera: `gantrel-printer camera <name> --out cam.jpg`. Look at the image or show it to the
   person. The bed must be empty.
7. Filament: `gantrel-printer filaments <name>`. If it lists nothing, ask the person what is loaded.
8. Calibration prints, one at a time, in this order: `first-layer`, then `temp-tower`, then
   `retraction`, then `flow`.

       gantrel-printer calibrate <name> <kind> --material <filament> --upload

   - Uploading never starts a print. Tell the person the file name, and ask them to check the bed
     is empty and start it on the printer.
   - When it is done, read them the NEXT line: what to look for and what to change. Ask which band
     or patch looks best. Write down the answer and the date; you need them for the pack.
9. Plan and check a real part:

       gantrel-printer plan <file.stl> --printer <name> --material <filament>

   If it says `REFUSED:`, tell the person why and stop. Check settings they already have with
   `gantrel-printer check <settings.json> --printer <name> --material <filament>`.
10. Slice and upload:

        gantrel-printer slice <file.stl> --printer <name> --material <filament>
        gantrel-printer upload <file.gcode> --printer <name> --material <filament>

    Tell the person the file name and to start it on the printer.
11. Ask the person how the print came out. If it was good, go to "Save the setup as a pack".

## Save the setup as a pack

1. Export:

       gantrel-printer pack export <name> --material <filament> --author "<name the person wants shown>"

   Ask the person how they want to be named. Never use an email address. Add `--id` only if they
   want a different id.
2. Gantrel does not record calibration results, so every `calibrated` list in the file is empty.
   If the person wants their results in the pack, add each one to the material's `calibrated` list
   as `{"test": "temp-tower", "date": "2026-11-22", "result": "220 C band was cleanest"}`, with
   their words. Add nothing else by hand.
3. Check it: `gantrel-printer pack check <file>`. If it says REFUSED, fix each REFUSED line or
   remove what it names, then check again.
4. Show it: `gantrel-printer pack show <file>`. Show the person all of it: the printer, every
   material row, every G-code line, and every FINDING line. A FINDING is a G-code line Gantrel does
   not know, often one of the printer's own macros.
5. The pack is now saved in that file. Stop here unless the person asks to share it.

## Share a pack

Sharing publishes the file on GitHub under the person's own account. It cannot be taken back once
others have copied it. So:

1. Ask the person, in these words or close to them: "Sharing opens a pull request to
   baney75/Gantrel from your GitHub account with this one file: <file>. Everything in it is what
   pack show printed. Do you want to share it?" Do nothing more without a clear yes.
2. Run `gh auth status`. If it is not signed in, tell the person to sign in with `gh auth login`
   themselves, then stop. Never sign in for them.
3. With their yes, run these one at a time:

       gh repo fork baney75/Gantrel --clone
       cd Gantrel
       git checkout -b pack-<id>
       cp <file> profiles/community/<id>.json
       gantrel-printer pack check profiles/community/<id>.json
       git add profiles/community/<id>.json
       git commit -m "Pack: <id>"
       git push -u origin pack-<id>
       gh pr create --repo baney75/Gantrel --title "Pack: <id>" --body "<body>"

   The body has the `result:` line and the accept code from pack show, and every FINDING line with
   one sentence from the person on what it does.
4. Tell the person the pull request link. Reviewers read it with pack show before it is merged.

## Use a pack someone else shared

1. Show it: `gantrel-printer pack show <file>`.
2. If it says REFUSED, tell the person each REFUSED line and stop. Never import it.
3. Tell the person what it sets: the printer, the temperatures, and every FINDING line in plain
   words. Say the proof lines are the author's word and were not checked.
4. Ask: "Do you want to import this pack?" Only with their yes:

       gantrel-printer pack import <file> --accept <accept code>

   Use the accept code pack show printed. If pack show listed findings and the person said yes to
   them, add `--accept-findings`. If an older pack has the same id, ask before adding `--replace`.
5. Slice with it: `gantrel-printer slice <file.stl> --printer pack:<id> --material <filament>`, or
   plan with `gantrel-printer plan <file.stl> --printer pack:<id> --material <filament>`. Upload
   still goes to the person's paired printer: `gantrel-printer upload <file.gcode> --printer <name>`.
6. See installed packs with `gantrel-printer pack list`. Remove one with
   `gantrel-printer pack remove <id>`.

## Rules and checks

- Never start or resume a print, by any route. The person starts every print on the printer.
- Never ask for, repeat, print or save an access code, API key or password. The person types it.
- Never share a pack without the person's yes, given after they saw pack show.
- Never import a pack that says REFUSED. Never import with `--accept-findings` unless the person
  heard every FINDING and said yes.
- Never edit a pack to get around a REFUSED line. Remove what it names instead.
- Never use `--accept` with a code you did not read from pack show for that same file.
- Every gantrel-printer reply ends with `NEXT:`. Follow it. If a command prints ERROR or
  REFUSED, tell the person what it said.

## Sources

- docs/PRINTERS.md in this repo: "Sharing a printer setup" (the pack format and G-code policy),
  "Planning a print", and the safety rules.
- profiles/community/README.md: how packs are added and reviewed.
- The printers skill: printer types, pairing details and watching a print.
