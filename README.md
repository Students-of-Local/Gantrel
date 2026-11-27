# Gantrel

Gantrel is an open-source 3D design studio built for local AI. It starts with 3D printing and grows into product design, for hobbyists up to professionals.

Think of it as precise parametric CAD (like FreeCAD), freeform modeling and rendering (like Blender), and an agent coding workspace (like t3code) combined into one easy app that a local model can drive.

**First version (planned for Feb 1, 2027):** an agent workspace for 3D printing. Gantrel ships first as a plugin for [Local Studio](https://github.com/baney75/local-studio), the native local-LLM workspace, and the first piece of that plugin works today (see [Local Studio plugin](#local-studio-plugin)). A local model writes and edits parts using existing CAD libraries (build123d or CadQuery), shows them in a 3D viewer with an inline mode, slices them, and sends them to your printer after you confirm. It connects to Klipper, OctoPrint, PrusaLink, Bambu Lab and Creality printers and their cameras, and agents can write adapters for other devices with your approval. Product design comes next. Architecture and building design are out of scope for now.

The parts and licenses are in [docs/STACK.md](docs/STACK.md), and the printer plan and safety rules are in [docs/PRINTERS.md](docs/PRINTERS.md).

## Local Studio plugin

This repo is a Local Studio plugin package: `plugin.toml` at the root, with its skills in `skills/plugin/`. It adds these commands to the Local Studio palette:

- `/gantrel <what you want>` asks the model for a printable part. It loads the [smoothstone](https://github.com/Students-of-Local/smoothstone) skill, so the model drives the `stone` CLI: it picks a tested parametric recipe, sets its sizes, builds the STL with OpenSCAD and checks that it prints (watertight, one piece, flat base, walls, overhangs, size). The part lands in `stone-job/` in your project. On the local fleet, LFM3-1.7B has made a 40 mm fan guard this way that passes every stone check; the LFM2.5 8B and 24B have not. See [SUPPORTED.md](SUPPORTED.md) for what is proven.
- `/studio` opens Gantrel Studio on the part: a 3D view with soft studio light and a 220 mm build plate, every recipe size as a slider and a typed value, a rebuild in a few seconds, the print checks with what to fix, an overhang overlay, and STL or 3MF export. It follows the job folder, so a change the model makes from chat shows up without a reload. After stone builds a part, `/gantrel` also starts the studio and gives you its link. See [docs/STUDIO.md](docs/STUDIO.md).
- `/solid <what you want>` designs a part with exact sizes in [SolidCraft](https://github.com/tangency-labs/solidcraft), an open-source parametric CAD program. The model writes the design as a list of modeling commands, and `gantrel-solid build` turns it into `part.step`, `part.stl` and `part.3mf` in `solid-job/build/` and runs the print checks; the model then measures the part against the asked sizes. Install SolidCraft's CLI with `gantrel-solid fetch --yes` (Linux), or the steps `gantrel-solid doctor` prints. See [docs/SOLIDCRAFT.md](docs/SOLIDCRAFT.md).
- `/print` loads the printers skill, so the model uses the `gantrel-printer` CLI to check a printer's status and camera, slice with that printer's profile, upload, pause, cancel or stop. Gantrel's tools never start or resume a print: you start it on the printer. See [docs/PRINTERS.md](docs/PRINTERS.md).

Install:

1. Install `stone` (it needs uv and OpenSCAD 2026 or newer). Install it from a clone: stone reads its recipes from the source folder, so a plain `uv tool install git+...` finds no recipes ([smoothstone#12](https://github.com/Students-of-Local/smoothstone/issues/12)):

   ```
   git clone https://github.com/Students-of-Local/smoothstone ~/.local/share/smoothstone
   git -C ~/.local/share/smoothstone checkout 45e05ce28634216c6d029fa1be26ee85ae9b6b15
   uv tool install -e ~/.local/share/smoothstone
   ```

   Check it: `stone recipes` lists 17 recipes.

2. Install Gantrel's own tools, which include `gantrel-studio`:

   ```
   uv tool install git+https://github.com/Students-of-Local/Gantrel
   ```

3. Put this repo in Local Studio's plugins folder. The folder must be named `gantrel`, the plugin id, so clone or copy it there; a symlink to a folder with another name fails the id check:

   ```
   git clone https://github.com/Students-of-Local/Gantrel ~/.local-studio/plugins/gantrel
   ```

4. Check it: `local-studio plugin validate ~/.local-studio/plugins/gantrel` prints `OK gantrel 0.5.0 (6 skills, 0 prompts, 6 commands)`.

The skill's first action is `stone status`; if that prints `stone: command not found`, the model is told to point you to these install steps and stop without installing anything. That step is not tested yet on a machine without `stone`.

The skill is a copy of smoothstone's `skill/` folder, pinned to one commit. Pinned smoothstone: `45e05ce28634216c6d029fa1be26ee85ae9b6b15` ([skill at that commit](https://github.com/Students-of-Local/smoothstone/tree/45e05ce28634216c6d029fa1be26ee85ae9b6b15/skill)). Gantrel adds two steps to it: first run `stone status` and follow its NEXT line, and once the part is ready, start Gantrel Studio and give the person its link. To move to another smoothstone commit:

```
scripts/sync-smoothstone.sh <sha or ref>
```

It re-imports the skill, adds the same step again, and writes the commit to `skills/plugin/smoothstone/UPSTREAM` and this README. `tests/plugin/check.sh` checks the manifest and that a sync at the pinned commit changes nothing.

## What it will do (planned)

Nothing below is built yet. See [SUPPORTED.md](SUPPORTED.md) for what works today.


- Lets the user describe what they want and has the model design it.
- Produces printable parts first, then product models.
- Takes photos from the user as reference. Hold a standard ruler up in the photo and Gantrel uses it to get real-world dimensions. They're accurate only near the ruler's plane, so the app shows its error.
- Researches on its own with built-in search (SearXNG and more) and browser automation to find specs, standards, materials and references.
- Connects to 3D printers on your network and shows their cameras. A print never starts without your confirm.
- Makes renders and short videos of a design.

## Skills are the core

Gantrel's skills are standardized, precise and tested. Every skill follows one strict format with clear instructions and specifics that force the model to do the work right: correct units, valid geometry and printable tolerances. A weak skill is a bug.

## Autoresearch matters

Autoresearch is central to Gantrel, not an add-on. Good design depends on real information like part dimensions, material limits and existing designs. Gantrel's models are expected to research before they design and to cite what they found. Contributors should treat research quality as seriously as geometry quality.

## Local first

Gantrel runs on local models, so designs, photos and research stay on the user's machine.

## Setup

Gantrel runs on [Fonsviva](https://github.com/Students-of-Local/Fonsviva), the shared local compute layer. If Fonsviva is already installed, Gantrel uses it and you pick models inside the app. If not, the app installs it for you and walks you through setup. Cloud providers are set up only in Fonsviva, for personal use only.

Agents setting this up for someone: start at [docs/agents.md](docs/agents.md) or [llms.txt](llms.txt). Site: https://students-of-local.github.io/Gantrel/

## Status

Early planning. The Local Studio plugin above is the first piece that runs; [SUPPORTED.md](SUPPORTED.md) lists what is proven. Contributions and ideas are welcome.

## License

MIT
