# What Gantrel supports

This file is the honest list of what Gantrel can and cannot do today. Only a situation with a passing test suite in `tests/golden/` may be listed as supported.

## Supported

No design tasks yet; `tests/golden/` has no cases.

### Local Studio plugin packaging

These are packaging checks, not design results. Each one passed on 2026-11-16 with Local Studio at `baney75/local-studio` `plugin-smoke-params` (slice 1 plus the smoke parameters).

- `plugin.toml` loads in Local Studio: `local-studio plugin validate ~/.local-studio/plugins/gantrel` prints `OK gantrel 0.1.0 (2 skills, 0 prompts, 2 commands)` on parish1, chapel1 and abbey1.
- `/gantrel` shows in the Local Studio palette with source `plugin:gantrel`: the core `plugin_smoke` example with `PLUGIN_SMOKE_ID=gantrel PLUGIN_SMOKE_COMMAND=gantrel` prints `ok   check 2 core: /gantrel listed, source plugin:gantrel, kind Plugin` on parish1, chapel1 and abbey1, and the Linux app's `16-composer-plugin-installed` snapshot shows the row with its `plugin:gantrel` badge.
- `/gantrel` sends the smoothstone skill to the model: `plugin_smoke` prints `ok   the /gantrel prompt reached the model expanded`.
- A real part from a local model, on LFM3-1.7B only: `plugin_smoke` on `local-fleet/hf.co/LiquidAI/LFM3-1.7B-GGUF:Q4_K_M` with the request "a fan guard for a 40 mm fan" ran 8 stone tool calls (`stone status`, `new`, `recipes`, `show fan_guard`, `set fan_guard size=40`, `build`, `status`) and printed `ok   check 3: .../stone-job/fan_guard.stl passes stone check` and `PASS plugin smoke`, once straight to Ollama on parish1 and once through the fleet controller. The STL is 40.0 x 40.0 x 3.0 mm, watertight, one body, all stone checks PASS.
- The vendored skill matches smoothstone at the pinned commit plus Gantrel's one step: `tests/plugin/check.sh` (also in CI).

## Not supported yet

- Making a part through `/gantrel` on LFM2.5-8B-A1B or LFM2.5-24B-A2B. In 5 runs the 8B always ran `stone status` first, then lost the request: three times it asked the user what to make, once it ran `stone new` and then wandered through the job files, once it printed the NEXT line as text instead of running it. The 24B on parish1 (split across CPU and GPU) never sent a token within Eukhe's 300 and 600 s stream timeouts, in 2 runs straight to Ollama and 2 through the controller.
- The skill's missing-`stone` stop is untested.
- Everything in the README's planned list.
