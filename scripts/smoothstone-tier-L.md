- In Gantrel the plugin ships this card, not the recipe sources. To write a new recipe, clone
  them first (`git clone https://github.com/baney75/smoothstone && git -C smoothstone checkout @SHA@`),
  write the recipe in `smoothstone/recipes/`, run `uv run python tools/sheet.py <name>` from the `smoothstone` folder (not `recipes/`), and build with
  `STONE_RECIPES=$PWD/smoothstone/recipes stone build`.
