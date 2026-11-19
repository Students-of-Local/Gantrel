---
name: studio
description: Open Gantrel Studio, the live 3D view of the part in this project, where the person can turn it, check overhangs, change its sizes with sliders and export STL or 3MF. Use when someone wants to see, look at, view, adjust or export the part, or asks for the studio or the 3D view.
---

# studio

## Goal

The person has a working link to Gantrel Studio for this project's part, and knows what it does.

## Steps

1. Run this one command with the bash tool, in the foreground:

   `gantrel-studio --job ./stone-job --detach`

2. It prints one line that starts with `Gantrel Studio: http://127.0.0.1:`. Give the person that
   whole link exactly as printed. It carries a private key, so never shorten or change it.
3. Tell the person, in one or two sentences: open the link in Local Studio's browser pane or any
   browser on this computer. Turning the sliders rebuilds the part in about two seconds, and the
   changes you make from the chat show up there too.

If there is no part yet, the studio still opens and shows how to start one. Make the part with
`/gantrel` and the studio updates on its own.

If bash prints `gantrel-studio: command not found`, tell the person to install it with
`uv tool install git+https://github.com/baney75/Gantrel`, then stop. Never install it yourself.

Run `gantrel-studio` only with `--detach`. Without it the command never returns.
