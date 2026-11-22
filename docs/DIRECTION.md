# Direction

The owner's standing direction for Gantrel. Every plan, skill and pull request is checked against it.

Donovan Baney, 2026-11-22, in his words:

> REMEMBER smoothstone and GANTREL are to be to better and they need to integrate very well and easily with things such as Creality CLI and other openstandards for connecting to 3D printers, and later other types of engineering devices, as gantrel is also for product design I want to be able to design my products in it, of course I will be using larger models, but over time we will "distill" through skills so lesser models can learn how to do said things too, and generalize widely and when I mean small I mean
>
> - 1.7B-31B for
> - then up and higher and that
>
> Unto caesar will work in a similar way one can use their sub but the goal is that eventually it will get so Good that local models can easily do taxes for people revolutionizing and helping the world, and I mean the world, write down what I have writted of course yet, ensure this will be done to a high standard


And in the next message:

> It needs an MCP to act with grokbot, and again will have a waitlist for tunnels unless they use dev mode and use their own
> as we will need to either charge or negotiate with them

## What it means for Gantrel

### 1. Printers and devices: very well and easily

- Gantrel connects through Creality's own tools (the Creality Print command line for slicing, the stock firmware's local API for upload and status) and through the open standards: Klipper with Moonraker, OctoPrint, PrusaLink, Bambu Lab LAN mode, G-code, and 3MF with its print settings.
- "Easily" is measured: a person goes from a new printer to a sliced, uploaded part with one pairing step and no config files, and `gantrel-printer discover` finds the printer on its own.
- "Very well" is measured: an adapter counts as supported only when it passes the contract suite against a real printer, not just its fake ([PRINTERS.md](PRINTERS.md) Status). Each adapter moves from Fake to Real on hardware, starting with the Creality K2.
- Other kinds of engineering devices come later, through the same adapter contract, the same safety rules (Gantrel never starts a job on its own) and the same fake-then-real proof.

### 2. Product design

- Gantrel is for product design as well as printing. Donovan designs his own products in it.
- Product work starts on larger models. Output is parametric source (build123d or CadQuery) plus the files a maker or manufacturer needs, with units and tolerances checked, not assumed.

### 3. Distill through skills to small models

- Every capability ships as a skill with an eval. When a larger model does something well, the run is turned into skill material (recipes, typed parameters, worked examples, checks), so a smaller model can do it too.
- Small means 1.7B to 31B parameters. Larger models sit above that. A skill is benchmarked across that ladder ([BENCHMARKS.md](../BENCHMARKS.md)), starting at the bottom (LFM3-1.7B today), and the results are published.
- "Generalize widely" is measured on held-out requests the skill was never tuned on. Passing only the tuning set does not count.

### 4. An MCP server for Grok Bot

- Gantrel has an MCP server so Grok Bot, and any assistant that speaks MCP (claude.ai, ChatGPT, grok.com), can act through it: make or change a part, slice it, check a printer and its camera. The printer safety rules hold over MCP too: no tool starts or resumes a print.
- Reaching it from away from home follows Local Studio's tunnel rule ([local-studio docs/tunnel.md](https://github.com/baney75/local-studio/blob/main/docs/tunnel.md)): hosted tunnels are waitlist-only, because running them costs money and Donovan will either charge for them or negotiate with the provider. Anyone on the dev channel can use their own tunnel today (their own Cloudflare account or Tailscale).
- A server reached through a tunnel needs auth on every path and the public-tools consent ([local-studio#13](https://github.com/baney75/local-studio/issues/13)).

### 5. A high standard

- No claim without a passing test ([TESTING.md](../TESTING.md)); [SUPPORTED.md](../SUPPORTED.md) says only what is proven.
- A fault the owner reports gets a check that fails on it before the fix merges.

Progress is tracked in the "Direction" issue on this repository.
