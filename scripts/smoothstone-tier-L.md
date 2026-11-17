- In Gantrel the plugin ships this card, not the recipe sources. To write a new recipe, clone
  them first (`git clone https://github.com/baney75/smoothstone && git -C smoothstone checkout @SHA@`),
  write the recipe in `smoothstone/recipes/`, run the sheet from that folder, and build with
  `STONE_RECIPES=$PWD/smoothstone/recipes stone build`.
