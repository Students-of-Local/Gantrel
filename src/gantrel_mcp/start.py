"""print_start and print_resume: the only path in Gantrel that starts or resumes a print.

The flow (docs/MCP.md, "How a print starts"):

1. Load the job record and connect to its printer. Refuse unless the printer is ready, it is the same
   printer the file was uploaded to, the local file is unchanged and the printer's copy is the
   confirmed bytes (check_remote).
2. Outside Local Studio (no LOCAL_STUDIO_ASK, LOCAL_STUDIO_ASK_TOKEN or LOCAL_STUDIO_ASK_PID) stop:
   not confirmed, and say how the person starts it on the printer.
3. Build the card only from records Gantrel holds: the paired printer's name and model, the file name
   on the printer, the first 12 hex of the file's SHA-256, the job's material and slot, and its plan
   summary. No tool argument reaches the card.
4. Ask through Local Studio's socket (gantrel_mcp.ask), which checks the peer pid before it sends.
5. Only on the exact answer "Start print" ("Resume print" for a resume): check step 1 again, mint a
   token in this process and spend it at once. The token is never returned or logged.
6. Log the attempt to prints.log (gantrel_printer.printlog), whatever the outcome.
"""

from __future__ import annotations

import os
import threading

from gantrel_printer import config, printlog, safety, slicing
from gantrel_printer.adapters.base import resume_binding
from gantrel_printer.cli import open_printer
from gantrel_printer.errors import ConfigError, PrinterError, Refused, SafetyError
from gantrel_printer.model import READY_STATES, bounded, fmt_duration

from . import ask

START_CHOICES = ["Start print", "Don't start"]
RESUME_CHOICES = ["Resume print", "Don't resume"]
CARD_END = "Check the bed is clear and the right filament is loaded."
CARD_MAX = 2000
LABEL_MAX, FILE_MAX, SHORT_MAX = 64, 120, 32
NOT_IN_LOCAL_STUDIO = "not confirmed: Gantrel starts a print only after the person confirms it in Local Studio"
TIMEOUT = "not confirmed (no answer in time); ask again when the person is ready"

# One start or resume at a time in this server, from the re-check to the printer's reply, so two cards
# answered together can't both pass the "printer is idle" check.
_act = threading.Lock()


class Declined(Exception):
    """Not confirmed: the outcome and the sentence to return."""

    def __init__(self, outcome: str, message: str):
        super().__init__(message)
        self.outcome = outcome
        self.message = message


def field(value, cap: int) -> str:
    """A card field: line breaks and tabs become spaces, other control and format characters are removed,
    runs of spaces collapse, and the result is cut to cap characters."""
    text = "".join(" " if c.isspace() else c for c in str(value) if c.isspace() or c.isprintable())
    return " ".join(text.split())[:cap]


def printer_label(adapter) -> str:
    cfg = adapter.cfg
    prof = slicing.PRINTERS.get(cfg.model)
    return field(f"{cfg.name} ({prof.label if prof else adapter.label})", LABEL_MAX)


def plan_line(plan) -> str:
    if not isinstance(plan, dict):
        return ""
    low, high = bounded(plan.get("low_s"), 0, 1e7), bounded(plan.get("high_s"), 0, 1e7)
    grams = bounded(plan.get("grams"), 0, 1e6)
    counts = [bounded(plan.get(k), 0, 1000) for k in ("pass", "warn", "fail")]
    parts = []
    if low is not None and high is not None:
        parts.append(f"about {fmt_duration(low)} to {fmt_duration(high)}")
    if grams is not None:
        parts.append(f"about {grams:.0f} g of filament")
    if None not in counts:
        parts.append(f"check {counts[0]:.0f} pass, {counts[1]:.0f} warn, {counts[2]:.0f} fail")
    return "Gantrel plan: " + ", ".join(parts) if parts else ""


def card(action: str, adapter, job: safety.Job) -> str:
    head = "Start this print?" if action == "start" else "Resume this paused print?"
    lines = [head, f"Printer: {printer_label(adapter)}", f"File: {field(job.remote_name, FILE_MAX)}",
             f"File SHA-256 starts: {field(job.sha256[:12], 12)}"]
    if job.material:
        lines.append(f"Material: {field(job.material, SHORT_MAX)}"
                     + (f", slot {field(job.slot, SHORT_MAX)}" if job.slot else ""))
    plan = plan_line(job.plan)
    if plan:
        lines.append(field(plan, 200))
    lines.append(CARD_END)
    text = "\n".join(lines)
    assert len(text) < CARD_MAX
    return text


def ready_to_start(adapter, job: safety.Job) -> None:
    """Step 1 for a start. Raises SafetyError or Refused."""
    if job.printer != adapter.cfg.name:
        raise SafetyError(f"job {job.id} is for printer {job.printer}, not {adapter.cfg.name}")
    if job.identity != adapter.identity():
        raise SafetyError(f"printer name {adapter.cfg.name} now points at a different printer than job {job.id}",
                          "Upload again for this printer")
    if not getattr(adapter, "network_start", True):
        raise Refused(f"{adapter.label} printers can't start a stored file over the network",
                      adapter.start_by_hand(job.remote_name))
    safety.verify_job_file(job)
    adapter.check_remote(job)
    st = adapter.status()
    if st.state not in READY_STATES:
        raise Refused(f"{adapter.cfg.name} is {st.state}, so it can't start a new print",
                      "Wait until it is idle, then ask again")


def paused_job(adapter) -> safety.Job:
    """Step 1 for a resume: the printer is paused on a print Gantrel uploaded. Raises SafetyError or Refused."""
    st = adapter.status()
    if st.state != "paused":
        raise Refused(f"{adapter.cfg.name} is {st.state}, not paused")
    return safety.job_for_print(adapter.cfg.name, adapter.identity(), st.file)


def confirm(text: str, choices: list[str]) -> None:
    """Steps 2 and 4. Returns only when the person chose choices[0]; raises Declined otherwise."""
    if not ask.in_local_studio():
        raise Declined("not_in_local_studio", NOT_IN_LOCAL_STUDIO)
    res = ask.ask(text, choices)
    status = res.get("status")
    if status == "answered" and res.get("answer") == choices[0]:
        return
    if status == "answered":
        raise Declined("declined", f"not confirmed: the person did not choose {choices[0]}")
    if status == "cancelled":
        raise Declined("declined", "not confirmed: the question was stopped or closed")
    if status == "timeout":
        raise Declined("timeout", TIMEOUT)
    raise Declined("refused", "not confirmed: " + field(res.get("error") or "Local Studio refused the question", 200))


def caller() -> str | None:
    return os.environ.get("LOCAL_STUDIO_CALLER") or None


def _log(action: str, outcome: str, job: str, printer: str, sha: str, reason: str = "") -> None:
    printlog.append(action, outcome, job=job, printer=printer, sha256=sha, reason=reason, caller=caller())


def _shown_id(value) -> str:
    return value if isinstance(value, str) and safety.JOB_ID_RE.match(value) else "(not a job id)"


def print_start(job_id) -> dict:
    """Start one uploaded job after the person confirms it on a Local Studio card.

    Returns a result dict for "started" and every "not confirmed" outcome. Raises PrinterError for a
    refusal (the printer is busy, the file changed, no such job), after logging it.
    """
    rec = {"job": _shown_id(job_id), "printer": "", "sha": ""}
    adapter = job = None
    try:
        job = safety.load_job(job_id)
        rec.update(job=job.id, printer=job.printer, sha=job.sha256)
        adapter = open_printer(job.printer)
        ready_to_start(adapter, job)
        confirm(card("start", adapter, job), START_CHOICES)
        with _act:
            ready_to_start(adapter, job)  # the printer may have changed while the card was open
            adapter.start(job, safety.mint_token("start", job.printer, job.id, job.sha256, adapter.identity()))
    except Declined as d:
        _log("start", d.outcome, rec["job"], rec["printer"], rec["sha"], d.message)
        how = adapter.start_by_hand(job.remote_name) if adapter and job else ""
        return {"started": False, "outcome": d.outcome, "job": rec["job"], "printer": rec["printer"],
                "message": d.message, "start_by_hand": how,
                "next": ("Tell the person the print did not start. " + how) if how else
                "Tell the person the print did not start"}
    except (SafetyError, Refused, ConfigError) as err:
        _log("start", "refused", rec["job"], rec["printer"], rec["sha"], str(err))
        raise
    except PrinterError as err:
        _log("start", "error", rec["job"], rec["printer"], rec["sha"], str(err))
        raise
    except Exception as err:
        _log("start", "error", rec["job"], rec["printer"], rec["sha"], type(err).__name__)
        raise PrinterError("the start failed unexpectedly", "Check the printer, then ask again") from None
    _log("start", "started", job.id, job.printer, job.sha256)
    return {"started": True, "outcome": "started", "job": job.id, "printer": job.printer,
            "file": job.remote_name,
            "message": f"started: {job.remote_name} on {job.printer}, after the person chose Start print",
            "next": f"Tell the person it started, then watch it: printer_status {job.printer} and camera_snapshot"}


def print_resume(printer) -> dict:
    """Resume the paused print on printer after the person confirms it on a Local Studio card.

    Only a print uploaded with Gantrel (one with a job record) can be resumed here.
    """
    name = printer if isinstance(printer, str) and config.NAME_RE.match(printer) else "(not a printer name)"
    rec = {"job": "", "printer": name, "sha": ""}
    adapter = job = None
    try:
        cfg = config.get(printer)
        adapter = open_printer(cfg.name)
        job = paused_job(adapter)
        rec.update(job=job.id, sha=job.sha256)
        confirm(card("resume", adapter, job), RESUME_CHOICES)
        with _act:
            st = adapter.status()
            if st.state != "paused":
                raise Refused(f"{cfg.name} is {st.state} now, not paused", "Nothing was resumed")
            binding = resume_binding(adapter, st)
            if binding != ("resume:" + job.id, job.sha256):
                raise SafetyError(f"{cfg.name} is paused on a different print than the one the person confirmed",
                                  "Ask again")
            adapter.resume(safety.mint_token("resume", cfg.name, *binding, adapter.identity()))
    except Declined as d:
        _log("resume", d.outcome, rec["job"], rec["printer"], rec["sha"], d.message)
        how = adapter.start_by_hand() if adapter else ""
        return {"resumed": False, "outcome": d.outcome, "job": rec["job"], "printer": rec["printer"],
                "message": d.message.replace("starts a print", "resumes a print"), "resume_by_hand": how,
                "next": ("Tell the person the print is still paused. " + how) if how else
                "Tell the person the print is still paused"}
    except (SafetyError, Refused, ConfigError) as err:
        _log("resume", "refused", rec["job"], rec["printer"], rec["sha"], str(err))
        raise
    except PrinterError as err:
        _log("resume", "error", rec["job"], rec["printer"], rec["sha"], str(err))
        raise
    except Exception as err:
        _log("resume", "error", rec["job"], rec["printer"], rec["sha"], type(err).__name__)
        raise PrinterError("the resume failed unexpectedly", "Check the printer, then ask again") from None
    _log("resume", "started", job.id, cfg.name, job.sha256)
    return {"resumed": True, "outcome": "started", "job": job.id, "printer": cfg.name, "file": job.remote_name,
            "message": f"resumed: {job.remote_name} on {cfg.name}, after the person chose Resume print",
            "next": f"Tell the person it resumed, then watch it: printer_status {cfg.name} and camera_snapshot"}
