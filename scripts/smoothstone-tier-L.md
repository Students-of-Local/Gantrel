- In Gantrel the plugin ships this card, not the recipe sources. To write a new recipe, put a
  copy of them in a fixed cache folder outside the person's project:
  `src="${XDG_CACHE_HOME:-$HOME/.cache}/gantrel/smoothstone-@SHA@"; [ -d "$src" ] || git clone -q https://github.com/baney75/smoothstone "$src" && git -C "$src" checkout -q @SHA@`.
  Write the recipe in `$src/recipes/`, run `uv run python tools/sheet.py <name>` from `$src` (not `recipes/`),
  and build with `STONE_RECIPES=$src/recipes stone build`. Delete `$src` to clean up; nothing else uses it.
