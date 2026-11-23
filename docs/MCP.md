# Gantrel's MCP server

`gantrel-mcp` lets an assistant that speaks MCP (Local Studio's agents, Claude Code, Codex, Grok Bot)
plan, slice, upload and watch prints on the person's paired printers, and ask the person to start
one. It runs over stdio only. Source: `src/gantrel_mcp/`. Launcher: `bin/gantrel-mcp`.

The owner's rule (Donovan, 2026-11-22): Gantrel must print to printers, and "this process needs to be
secure and reviewable". So a print starts only when the person clicks Start print on a card in Local
Studio, and every attempt is written to a log the person can read.

## Tools

Every tool returns JSON text with a `next` field saying what to do next. An error is one plain
sentence: `REFUSED: ...` for a safety rule (do not retry around it), `ERROR (<code>): ...` for
anything else.

| Tool | Arguments | What it does |
|---|---|---|
| `printers` | none | Paired printers: name, type, address, model. Read-only. |
| `printer_status` | `printer` | State, file, progress, temperatures. Read-only. |
| `filaments` | `printer` | Loaded filament in each slot: family, color, amount left. Read-only. |
| `jobs` | none | Files uploaded with Gantrel (job records). Read-only. |
| `camera_snapshot` | `printer` | One camera frame, as an image (8 MB cap). Read-only. |
| `plan` | `path`, `printer`, `material`, `goal`, `load_axis` | Orientation, supports, settings, time, filament and check for an STL or 3MF. Returns a `plan_id`. Contacts no printer. |
| `check` | `settings`, `printer`, `material`, `model_path`, `goal` | Checks a settings object against the printer, filament and part. Pass the whole `plan` result to check the part turned the plan's way. Contacts no printer. |
| `slice` | `path`, `printer`, `material`, `quality` | Slices with the printer's profile; writes the G-code next to the model. |
| `upload` | `path`, `printer`, `material`, `plan_id` | Sends a sliced file and records the job. Never starts it. With `plan_id`, the job keeps that plan's summary. |
| `pause` | `printer` | Pauses now. Needs no permission. |
| `cancel` | `printer` | Cancels the current print. |
| `emergency_stop` | `printer` | The strongest stop the printer offers over the network. |
| `print_start` | `job_id` | Asks the person to start the job, on a Local Studio card. |
| `print_resume` | `printer` | Asks the person to resume a paused print Gantrel uploaded, on a Local Studio card. |

File arguments (`path`, `model_path`) must name an existing regular file whose real location, after
following links, is inside the person's home folder or the server's working folder (never `/` itself).
A link inside home that points outside it is refused. Models are capped at 200 MB, sliced files at
500 MB.

## How a print starts

`print_start(job_id)`, in `src/gantrel_mcp/start.py`:

1. Load the job record and connect to its printer. Refuse unless the printer is idle (or complete or
   cancelled), the printer name still points at the printer the file went to, the local file is
   unchanged, and the printer's copy has the size and time recorded at upload. Printers whose API
   can't start a stored file (Snapmaker, UltiMaker) are refused here.
2. If `LOCAL_STUDIO_ASK`, `LOCAL_STUDIO_ASK_TOKEN` or `LOCAL_STUDIO_ASK_PID` is missing, stop. The
   reply is "not confirmed: Gantrel starts a print only after the person confirms it in Local Studio"
   with where to start it on the printer.
3. Build the card from Gantrel's own records only: the paired printer's name and model, the file name
   on the printer, the first 12 hex of the file's SHA-256, the material and slot recorded at upload,
   and the plan summary (time range, grams, check pass, warn and fail counts) when the upload named a
   plan. Control characters are removed and each field is capped. It ends "Check the bed is clear and
   the right filament is loaded." The choices are exactly Start print and Don't start.
4. Connect to Local Studio's ask socket, check that the process serving it is `LOCAL_STUDIO_ASK_PID`
   (SO_PEERCRED on Linux, LOCAL_PEERPID on macOS) before sending anything, send the question, and wait.
   One 50 second budget covers the whole call, under the 60 seconds an MCP call gets: the wait gets
   what is left after the step 1 checks, minus 15 seconds kept for the checks and the start after the
   answer. If the step 1 checks leave no time to wait, no card is shown. On a timeout the connection
   closes, which takes the card away.
5. Only the exact answer Start print goes on. Typed text, Don't start, Stop (cancelled), a refusal, a
   different process on the socket, or no answer: nothing starts.
6. Check step 1 again, because the printer may have changed while the card was open. The start
   repeats those checks, so if the re-check took longer than what is left of the budget, nothing
   starts and the outcome is `timeout`. Then mint a single-use token in this process, bound to this
   job, its file's SHA-256 and the printer's identity, and spend it at once on the start. The token
   is never returned, printed or logged.
7. Append one line to `prints.log`.

While a card is open, the server still answers other calls: mcp 2.3 runs each sync tool on a worker
thread, so `pause`, `cancel` and `emergency_stop` never wait behind a card.

`print_resume(printer)` is the same flow for a paused print. The printer must be paused on a file
Gantrel uploaded (a job record whose content-hash name matches); a print started some other way can't
be resumed here. Its card asks "Resume this paused print?" with the choices Resume print and Don't
resume, and its token binds to that job record.

The upload names the file on the printer from the file's own name (letters, digits, dot, dash and
underscore only, up to 100 characters) plus the content hash. That name is the one card field the
assistant can shape, by choosing the file; a name with anything else in it is refused at upload. The
plan summary on the card is Gantrel's own numbers, but which plan is attached is the assistant's
choice at upload; upload refuses a plan made for another printer model or another filament.

## What is logged

`prints.log` in Gantrel's config folder (`~/.config/gantrel-printer/`, or `$GANTREL_PRINTER_HOME`),
mode 0600, one JSON line per attempt: time (UTC), action (start or resume), job id, printer, the first
12 hex of the file's SHA-256, the outcome (`started`, `declined`, `timeout`, `not_in_local_studio`,
`refused`, `error`), a short reason, and `LOCAL_STUDIO_CALLER` when Local Studio set one. A job id that
isn't one is logged as `(not a job id)`, never as the text the assistant sent. No token, ask token or
printer secret is ever written.

Read it with `gantrel-printer prints` (a table) or `gantrel-printer prints --json`.

## What this does not guarantee

From Local Studio's own guarantee (local-studio `docs/plugins.md`, "Questions from plugins" and
"Caller identity"): the protocol gives the model no way to answer the card, because the socket takes
only questions and only the person's click or typed text, in the app or on a paired phone, settles
one. A server the model puts in place of the socket gets no token and gives no answer to a plugin
that checks the peer pid, which Gantrel does.

Until Local Studio's per-session sandboxing ([local-studio#33](https://github.com/baney75/local-studio/issues/33))
lands, the model's bash runs as the same user as Local Studio and Gantrel. A model that attacks the
system itself is outside this guarantee: one that drives the desktop app, pairs a device, kills or
traces processes, reads the server's environment (`ps eww`) to take its ask token and raise questions
as Gantrel, writes Gantrel's state files, or calls the printer's own API with curl. The card protects
the honest path and makes every start reviewable; it is not a sandbox.

The card also can't tell what is on the bed. The person checks that.

## Registering the server

Point the client at the absolute path of `bin/gantrel-mcp` in your Gantrel checkout. It needs `uv`
on `PATH`; it keeps its Python environment in `${XDG_CACHE_HOME:-~/.cache}/gantrel/venv` and writes
nothing in the checkout.

- **Local Studio:** nothing to do. The plugin's `plugin.toml` asks for `mcp-server` and `ask-user`;
  grant both on the consent sheet, and Local Studio starts the server for each thread with the ask
  variables. This is the only place `print_start` can start a print.
- **Claude Code:** `claude mcp add --scope user gantrel -- /path/to/Gantrel/bin/gantrel-mcp`
- **Codex:** `codex mcp add gantrel -- /path/to/Gantrel/bin/gantrel-mcp`
- **Grok Bot (grok CLI):** `grok mcp add --scope user gantrel /path/to/Gantrel/bin/gantrel-mcp`

In Claude Code, Codex and Grok Bot every tool works except that `print_start` and `print_resume`
answer "not confirmed": those clients have no Local Studio card, so the person starts the print on the
printer's screen or web page, as the reply says.

There is no HTTP transport. Reaching Gantrel from away from home waits on Local Studio's public-tools
consent ([local-studio#13](https://github.com/baney75/local-studio/issues/13)) and the tunnel rule in
[DIRECTION.md](DIRECTION.md).

## Proof

`tests/mcp/` drives the real server through the MCP SDK's in-process client against the fake printers
in `tests/printers/fakes.py` and a fake Local Studio ask socket. What is proven, and how, is in
[SUPPORTED.md](../SUPPORTED.md#mcp-server).
