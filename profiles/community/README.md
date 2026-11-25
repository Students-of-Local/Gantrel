# Community printer packs

A printer pack is one person's working setup for a printer model: its size and limits, its start
and end G-code, and the filament settings that printed well on it. Anyone with the same printer can
import it and slice with it. A pack is data only. It never holds code, and it never holds anything
that identifies a person's machine or network: no host, IP address, serial, access code, TLS pin,
MAC address, Wi-Fi name, user name, file path, email or camera URL. `gantrel-printer pack check`
refuses a pack that has any of these.

The format, the G-code policy and every check are described in
[docs/PRINTERS.md](../../docs/PRINTERS.md#sharing-a-printer-setup).

## Add a pack

1. Get the printer working in Gantrel first: paired with `--model`, a few good prints, and the
   calibration prints the filament needed.
2. Export it:

       gantrel-printer pack export <printer> --id <maker-model-material> --author "<how you want to be named>"

   The file holds Gantrel's defaults. Edit it only to put in what you measured: the numbers your
   calibration prints set for that material (`nozzle_c`, and `first_layer_nozzle_c` by the same
   step, from a temp tower; `retraction_mm` from a retraction tower; `flow_ratio` from the flow
   patches), the `calibrated` lists, `proof` and `notes`. Gantrel does not record calibration
   results yet, so it leaves `calibrated` empty. Run `pack check` after each change.
3. Check it and read it all:

       gantrel-printer pack check <file>
       gantrel-printer pack show <file>

   Fix every REFUSED line. A FINDING is a G-code line Gantrel does not know (often one of your
   printer's own macros); keep it only if you know what it does.
4. Copy the file into this folder as `<id>.json` and open a pull request to Students-of-Local/Gantrel. In the
   description, paste the `pack show` result line and list every FINDING with one sentence on what
   that line does on your printer. A pull request publishes the file under your GitHub account.

## Review a pack

Reviewers run `gantrel-printer pack show <file>` on the pull request's file and read all of it:

- the summary, the sha256 and the accept code,
- every material row against the filament maker's ranges,
- every start and end G-code line, with the findings marked,
- the proof lines, which are the author's word and are not checked.

CI (`.github/workflows/printers.yml`) runs `gantrel-printer pack check` on every file here. Any
refusal fails the build. Findings are printed; the reviewer checks that each one is listed and
explained in the pull request, and asks about any that is not.

## Packs here

| File | Printer | Materials | Tested |
|---|---|---|---|
| `creality-k2-pla.json` | Creality K2 | PLA | Built from Gantrel's own K2 profile and PLA defaults as an example. Passes `pack check`; not printed as a pack yet. |
