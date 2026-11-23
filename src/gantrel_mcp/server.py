"""Gantrel's MCP server, stdio only (docs/MCP.md).

Read tools: printers, printer_status, filaments, jobs, camera_snapshot, plan, check.
Safe write tools: slice, upload (never starts), pause, cancel, emergency_stop.
print_start and print_resume start or resume a print only after the person answers a Local Studio
card (gantrel_mcp.start). Every tool returns JSON text; an error is one plain sentence.

Nothing here writes to stdout except the MCP SDK: stdout is the protocol.
"""

from __future__ import annotations

import functools
import json
import logging
import tempfile
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from gantrel_printer import cli, config, safety, slicing
from gantrel_printer.adapters import adapter_class
from gantrel_printer.errors import PrinterError, SafetyError

from . import __version__, files, start

SNAPSHOT_MAX = 8 * 1024 * 1024
GOALS = ("quality", "balanced", "speed")

INSTRUCTIONS = """Gantrel: plan, slice, upload and watch 3D prints on the person's paired printers.
Stop requests come first: for fire, smoke, a crash or "stop now" call emergency_stop; for anything
else wrong call pause (no permission needed); "cancel the print" is cancel.
A print starts only through print_start, which shows the person a card in Local Studio; it starts
only when they choose Start print there. Never say a print started unless print_start returned
outcome "started". Outside Local Studio print_start answers "not confirmed": tell the person how to
start it on the printer. Never try to start a print any other way."""

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
STOP = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)
ACT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)


def dumps(data: dict) -> str:
    return json.dumps(data, indent=2, default=str)


def sentence(err: Exception) -> str:
    if isinstance(err, SafetyError):
        return f"REFUSED: {err}. Do not retry around this; tell the person what was refused."
    if isinstance(err, PrinterError):
        return f"ERROR ({err.code}): {err}. Tell the person this and follow its advice."
    return f"ERROR: {err}"


def plain_errors(fn):
    """Turn the package's errors into one plain sentence the model reads (is_error on the result)."""
    @functools.wraps(fn)
    def run(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except (PrinterError, ValueError) as err:
            raise ToolError(sentence(err)) from None
    return run


def _printer(name: str):
    adapter = cli.open_printer(name)
    return adapter, cli.snapshot_cfg(adapter)


def plan_summary(p: dict) -> dict:
    e, c = p["estimate"], p["check"]
    return {"printer": p["printer"], "material": p["material"], "low_s": e.get("low_seconds"),
            "high_s": e.get("high_seconds"), "grams": e.get("filament_g"),
            "pass": sum(r["status"] == "pass" for r in c["rules"]), "warn": len(c["warns"]),
            "fail": len(c["fails"])}


def build() -> MCPServer:
    mcp = MCPServer("Gantrel", instructions=INSTRUCTIONS, version=__version__, log_level="WARNING")

    def tool(annotations):
        def wrap(fn):
            mcp.tool(annotations=annotations, structured_output=False)(plain_errors(fn))
            return fn
        return wrap

    # ------------------------------------------------------------ read

    @tool(READ)
    def printers() -> str:
        """List the paired printers: name, type, address and slicing model."""
        rows = [{"name": p.name, "type": p.type, "host": p.host, "port": p.port or adapter_class(p.type).default_port,
                 "model": p.model} for p in config.load_all().values()]
        return dumps({"printers": rows, "next": "printer_status <name>" if rows else
                      "No printers are paired. The person pairs one with: gantrel-printer pair"})

    @tool(READ)
    def printer_status(printer: str) -> str:
        """Read-only status of one paired printer: state, file, progress, temperatures."""
        adapter, before = _printer(printer)
        st = adapter.status()
        cli.save_if_changed(adapter, before)
        if st.state == "paused":
            nxt = "It is paused. Only the person resumes it: print_resume shows them a card in Local Studio"
        elif st.state == "printing":
            nxt = "Watch it with camera_snapshot. If it looks wrong: pause"
        else:
            nxt = "Check the bed is clear with camera_snapshot"
        return dumps({**st.to_dict(), "summary": st.summary(), "next": nxt})

    @tool(READ)
    def filaments(printer: str) -> str:
        """What filament is loaded in each slot of a printer (read-only): family, color, amount left."""
        adapter, before = _printer(printer)
        slots = adapter.filaments()
        cli.save_if_changed(adapter, before)
        return dumps({"printer": adapter.cfg.name, "slots": [s.to_dict() for s in slots],
                      "next": "Pick the slot whose family matches the print's filament; if two match, none does, "
                              "or the family is unknown, ask the person"})

    @tool(READ)
    def jobs() -> str:
        """Files uploaded with Gantrel (job records): job id, printer, file name, material, plan."""
        return dumps({"jobs": [j.__dict__ for j in safety.list_jobs()],
                      "next": "print_start <job id> asks the person to confirm a start in Local Studio"})

    @tool(READ)
    def camera_snapshot(printer: str) -> Image:
        """One camera frame from the printer, as an image."""
        adapter, before = _printer(printer)
        with tempfile.TemporaryDirectory(prefix="gantrel-cam-") as tmp:
            saved = adapter.snapshot(Path(tmp) / "frame.jpg")
            cli.save_if_changed(adapter, before)
            size = saved.stat().st_size
            if size > SNAPSHOT_MAX:
                raise PrinterError(f"the camera sent {size // 1024} KB, over Gantrel's {SNAPSHOT_MAX // 1024} KB "
                                   "limit for one frame")
            data = saved.read_bytes()
        return Image(data=data, format="png" if saved.suffix == ".png" else "jpeg")

    @tool(READ)
    def plan(path: str, printer: str, material: str, goal: str = "quality", load_axis: str | None = None) -> str:
        """Plan a print of an STL or 3MF: orientation, supports, settings, time, filament and a settings check.

        printer is a paired printer name or a profile key; material is PLA, PLA-CF, PETG, PETG-CF, ABS, ASA,
        TPU, PA, PA-CF, PC, PVA or HIPS. The result has a plan_id; pass it to upload so the person's start
        card shows the plan. Contacts no printer.
        """
        from gantrel_printer.plan import mesh as pmesh, optimize

        if goal not in GOALS:
            raise ValueError(f"goal must be one of {', '.join(GOALS)}")
        if load_axis not in (None, "x", "y", "z"):
            raise ValueError("load_axis must be x, y or z")
        model = files.checked(path, "model")
        prof = cli._plan_printer(printer)
        m = pmesh.load(model)
        try:
            p = optimize.plan(m, prof, material, goal, load_axis)
        except optimize.PlanRefused as err:
            return dumps({"refused": True, "reason": err.message, "fix": err.hint, **err.result,
                          "next": "Tell the person this printer and filament do not go together, and why"})
        p["plan_id"] = safety.save_plan(plan_summary(p))
        p["next"] = (f"Tell the person the orientation, supports, time and filament. Slice, then upload with "
                     f"plan_id {p['plan_id']}")
        return dumps(p)

    @tool(READ)
    def check(settings: dict, printer: str, material: str, model_path: str | None = None,
              goal: str | None = None) -> str:
        """Check print settings (the settings object from slice or plan) against the printer, the filament
        and, with model_path, the part. Contacts no printer.

        settings: a settings object, or the whole plan result, whose orientations turn the part the plan's way.
        """
        if goal is not None and goal not in GOALS:
            raise ValueError(f"goal must be one of {', '.join(GOALS)}")
        s = settings.get("settings", settings) if isinstance(settings, dict) else None
        if not isinstance(s, dict):
            raise ValueError("settings must be a settings object, like the one slice or plan returns")
        model = str(files.checked(model_path, "model")) if model_path else None
        prof, fil, g, turned, c = cli.check_settings(s, printer, material, model, goal, settings)
        return dumps({"printer": prof.key, "material": fil.name, "goal": g, "part_orientation": turned, **c,
                      "next": "Fix every failed rule as its advice says, then check again" if not c["ok"] else
                      "The settings pass; tell the person about any warnings"})

    # ------------------------------------------------------------ write, never starts

    @tool(WRITE)
    def slice(path: str, printer: str, material: str = "PLA", quality: str = "standard") -> str:
        """Slice an STL or 3MF for a paired printer with its profile. Writes the G-code next to the model."""
        if quality not in slicing.QUALITY:
            raise ValueError(f"quality must be one of {', '.join(slicing.QUALITY)}")
        model = files.checked(path, "model")
        cfg = config.get(printer)
        prof = slicing.profile_for(cfg.model, cfg.type)
        mat = slicing.material(material)
        s = slicing.settings(prof, mat, quality)
        gcode, used, _ = slicing.slice_model(model, model.parent, prof, s)
        facts = slicing.gcode_facts(gcode)
        return dumps({"gcode": str(gcode), "slicer": used, "settings": s, **facts,
                      "next": f"upload {gcode} to {cfg.name} (add the plan_id if you planned it)"})

    @tool(WRITE)
    def upload(path: str, printer: str, material: str | None = None, plan_id: str | None = None) -> str:
        """Send a sliced file to a paired printer and record the job. Never starts the print.

        material: the filament it was sliced for, if the file doesn't say. plan_id: from the plan tool, so
        the person's start card shows the plan.
        """
        gcode = files.checked(path, "gcode")
        summary = safety.load_plan(plan_id) if plan_id else None
        job, adapter = cli.upload_file(printer, gcode, None, material, plan=summary, record_slot=True)
        return dumps({"job": job.id, "printer": job.printer, "remote_name": job.remote_name,
                      "sha256": job.sha256, "material": job.material, "slot": job.slot, "plan": job.plan,
                      "started": False,
                      "next": f"It has NOT started. In Local Studio, call print_start {job.id}: the person sees a "
                              f"card and starts it there. Elsewhere: {adapter.start_by_hand(job.remote_name)}"})

    @tool(STOP)
    def pause(printer: str) -> str:
        """Pause the print now. Safe: needs no permission."""
        adapter, _ = _printer(printer)
        adapter.pause()
        return dumps({"paused": adapter.cfg.name, "next": "Tell the person it is paused. Only they resume it "
                      "(print_resume shows them a card in Local Studio)"})

    @tool(STOP)
    def cancel(printer: str) -> str:
        """Cancel the current print."""
        adapter, _ = _printer(printer)
        adapter.cancel()
        return dumps({"cancelled": adapter.cfg.name, "next": f"printer_status {adapter.cfg.name}"})

    @tool(STOP)
    def emergency_stop(printer: str) -> str:
        """Emergency stop: the strongest stop the printer offers over the network. For fire, smoke, a crash."""
        adapter, _ = _printer(printer)
        did = adapter.emergency_stop()
        return dumps({"estop": adapter.cfg.name, "result": did, "next": "Tell the person what happened now"})

    # ------------------------------------------------------------ start and resume: the person decides
    # These stay plain def: mcp 2.3 runs a sync tool on a worker thread, so a card held open here never
    # delays pause, cancel or emergency_stop. An async def would run on the event loop and block them.

    @tool(ACT)
    def print_start(job_id: str) -> str:
        """Ask the person to start an uploaded job. In Local Studio a card appears; the print starts only if
        they choose Start print there. Never say it started unless the result's outcome is "started"."""
        return dumps(start.print_start(job_id))

    @tool(ACT)
    def print_resume(printer: str) -> str:
        """Ask the person to resume a paused print Gantrel uploaded. In Local Studio a card appears; the print
        resumes only if they choose Resume print there."""
        return dumps(start.print_resume(printer))

    return mcp


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    build().run("stdio")
    return 0
