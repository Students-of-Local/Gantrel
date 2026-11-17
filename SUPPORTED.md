# What Gantrel supports

This file is the honest list of what Gantrel can and cannot do today. Only a situation with a passing test suite in `tests/golden/` may be listed as supported.

## Supported

No design tasks yet; `tests/golden/` has no cases.

### Local Studio plugin packaging

These are packaging checks, not design results. Each one passed on 2026-11-16 with Local Studio at `baney75/local-studio` `plugin-smoke-params` (slice 1 plus the smoke parameters).

- `plugin.toml` loads in Local Studio: `local-studio plugin validate ~/.local-studio/plugins/gantrel` prints `OK gantrel 0.1.0 (1 skills, 0 prompts, 1 commands)` on parish1, chapel1 and abbey1.
- `/gantrel` shows in the Local Studio palette with source `plugin:gantrel`: the core `plugin_smoke` example with `PLUGIN_SMOKE_ID=gantrel PLUGIN_SMOKE_COMMAND=gantrel` prints `ok   check 2 core: /gantrel listed, source plugin:gantrel, kind Plugin` on parish1, chapel1 and abbey1, and the Linux app's `16-composer-plugin-installed` snapshot shows the row with its `plugin:gantrel` badge.
- `/gantrel` sends the smoothstone skill to the model: `plugin_smoke` prints `ok   the /gantrel prompt reached the model expanded`.
- The vendored skill matches smoothstone at the pinned commit plus Gantrel's one step: `tests/plugin/check.sh` (also in CI).

## Not supported yet

- Making a part through `/gantrel` on a local model. On LFM2.5-8B-A1B the model runs `command -v stone` and then loses the skill's file paths and writes its next tool call as text instead of calling it; the stock smoothstone plugin fails the same way on that model. LFM2.5-24B-A2B did not answer within the fleet gateway's timeout on the hardware available.
- The skill's missing-`stone` stop is untested.
- Everything in the README's planned list.
