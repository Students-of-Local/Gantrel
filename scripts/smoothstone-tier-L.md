- In Gantrel the plugin ships this card, not the recipe sources. To write a new recipe, put a
  copy of them in a fixed cache folder outside the person's project. Each bash call may start a
  fresh shell, so write the full path every time instead of keeping it in a variable:
  `git clone -q https://github.com/Students-of-Local/smoothstone ~/.cache/gantrel/smoothstone-@SHA@ 2>/dev/null; git -C ~/.cache/gantrel/smoothstone-@SHA@ checkout -q @SHA@`.
  Write the recipe in `~/.cache/gantrel/smoothstone-@SHA@/recipes/`, run
  `cd ~/.cache/gantrel/smoothstone-@SHA@ && uv run python tools/sheet.py <name>` (from that folder, not `recipes/`),
  and build with `STONE_RECIPES=~/.cache/gantrel/smoothstone-@SHA@/recipes stone build`.
  Delete `~/.cache/gantrel/smoothstone-@SHA@` to clean up; nothing else uses it.
