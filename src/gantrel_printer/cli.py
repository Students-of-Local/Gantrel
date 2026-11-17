"""gantrel-printer: one CLI for every printer Gantrel supports.

Every human-readable reply ends with one line, NEXT: <what to do>, so a small model that loses its
context can carry on. --json replies carry the same hint in a "next" field.

This CLI never starts or resumes a print. A person does that on the printer's screen or its own web
UI. Pause, cancel and emergency stop are here because they only make a printer safer.
"""

from __future__ import annotations

import argparse
import copy
import getpass
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, calibrate, config, discover as disc, guard as vguard, monitor, notify, safety, slicing
from .adapters import ADAPTERS, adapter_class, build
from .errors import ConfigError, PrinterError, SafetyError
from .model import fmt_duration

EXIT_ERROR, EXIT_USAGE, EXIT_SAFETY = 1, 2, 3
REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(gcode|bgcode|3mf|ctb|goo)$")
RESIN_EXT = ("ctb", "goo")  # pre-sliced resin files; only adapters with accepts_resin take them


def out(args, data: dict, text: str, nxt: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps({**data, "next": nxt}, indent=2, default=str))
    else:
        if text:
            print(text)
        print(f"NEXT: {nxt}")


def open_printer(name: str):
    cfg = config.get(name)
    cls = adapter_class(cfg.type)
    secret = config.get_secret(name) if (cls.needs_secret or cfg.extra.get("has_secret")) else None
    return build(cfg, secret)


def save_if_changed(adapter, before: dict) -> None:
    extra_before = {k: v for k, v in before.items() if k != "__fp"}
    if adapter.cfg.extra != extra_before or adapter.cfg.tls_fingerprint != before.get("__fp"):
        printers = config.load_all()
        printers[adapter.cfg.name] = adapter.cfg
        config.save_all(printers)


def snapshot_cfg(adapter) -> dict:
    d = copy.deepcopy(adapter.cfg.extra)
    d["__fp"] = adapter.cfg.tls_fingerprint
    return d


# ---------------------------------------------------------------- commands


def cmd_discover(args):
    hosts = args.host or None
    found = disc.discover(subnet=args.subnet, hosts=hosts, seconds=args.seconds, use_mdns=not args.no_mdns)
    lines = [f"{r['type']:10} {r['host']}:{r['port']}  {r.get('detail', '')}" for r in found]
    if not found:
        nxt = "Nothing found. Ask the person for the printer's IP address (printer screen: Network), then pair it"
    else:
        # A USB serial device may be any gadget (a Zigbee stick, a UPS cable), so never suggest
        # pairing one by itself; network hits come first.
        net_hits = [r for r in found if r.get("via") != "usb" and r.get("type") != "serial"]
        if net_hits:
            r = net_hits[0]
            nxt = (f"gantrel-printer pair <name> --type {r['type']} --host {r['host']} --port {r['port']}"
                   " --model <model>")
        else:
            nxt = ("Only USB serial devices were found, and any of them may not be a printer. Ask the person which "
                   "one is the printer and that it is idle, then: gantrel-printer pair <name> --type serial "
                   "--host <device path> --model <model>")
    out(args, {"printers": found}, "\n".join(lines) or "No printers found.", nxt)


def cmd_pair(args):
    cls = adapter_class(args.type)
    if args.name in config.load_all():
        raise SafetyError(f"a printer named {args.name!r} is already paired",
                          f"Run gantrel-printer unpair {args.name} first; that also removes its job records")
    if args.model and args.model not in slicing.PRINTERS:
        raise ConfigError(f"unknown model {args.model!r}", f"Use one of: {', '.join(slicing.PRINTERS)}")
    if args.type == "bambu":
        bambu_models = [k for k in slicing.PRINTERS if k.startswith("bambu-")]
        if args.model not in bambu_models:
            # Bed size, enclosure and camera all differ by model; never guess (an A1 mini is not a P1S).
            raise ConfigError("Bambu Lab printers need --model", f"Use one of: {', '.join(bambu_models)}")
        if args.no_check:
            raise ConfigError("Bambu Lab pairing must reach the printer to pin its TLS certificates",
                              "Pair without --no-check while the printer is on")
    cfg = config.PrinterConfig(name=args.name, type=args.type, host=args.host, port=args.port, model=args.model or "",
                               camera_url=args.camera_url or "", stream_url=args.stream_url or "",
                               serial=args.serial or "")
    if args.user:
        cfg.extra["user"] = args.user
    by_approval = getattr(cls, "pairs_by_approval", False)
    if by_approval and args.no_check:
        raise ConfigError(f"{cls.label} pairing must reach the printer so a person can allow it on its screen",
                          "Pair without --no-check while the printer is on")
    secret = None
    if by_approval:
        print(cls.approval_prompt, file=sys.stderr)
    elif args.secret_stdin:
        secret = sys.stdin.readline().strip() or None
    elif cls.asks_secret and sys.stdin.isatty():
        # Optional (a Duet board password, a Moonraker key): an empty answer means it has none.
        secret = getpass.getpass(f"{cls.secret_label}: ").strip() or None
    elif cls.needs_secret:
        if not sys.stdin.isatty():
            raise ConfigError(f"{cls.label} needs its {cls.secret_label}",
                              "Pipe it in with --secret-stdin, or run pair in a terminal to be asked for it")
        secret = getpass.getpass(f"{cls.secret_label}: ").strip() or None
    if cls.needs_secret and not secret:
        raise ConfigError(f"{cls.label} needs its {cls.secret_label}")
    adapter = cls(cfg, secret)
    facts = {} if args.no_check else adapter.pair()
    # Approval pairing (Snapmaker, UltiMaker) gets its secret from the printer during pair().
    secret = adapter.secret or secret
    if secret:
        adapter.cfg.extra["has_secret"] = True
        config.set_secret(args.name, secret)
    printers = config.load_all()
    printers[args.name] = adapter.cfg
    config.save_all(printers)
    shown = ", ".join(f"{k} {v}" for k, v in facts.items())
    out(args, {"paired": args.name, "type": args.type, "host": args.host, **facts},
        f"Paired {args.name}: {cls.label} at {args.host}" + (f" ({shown})" if shown else "")
        + (". Secret saved in the OS keyring." if secret else "."),
        f"gantrel-printer status {args.name}")


def cmd_unpair(args):
    printers = config.load_all()
    if args.name not in printers:
        raise ConfigError(f"no printer named {args.name!r}")
    del printers[args.name]
    config.save_all(printers)
    config.delete_secret(args.name)
    safety.forget_printer(args.name)
    out(args, {"unpaired": args.name}, f"Removed {args.name}, its saved secret and its job records.",
        "gantrel-printer list")


def cmd_list(args):
    printers = config.load_all()
    rows = [{"name": p.name, "type": p.type, "host": p.host, "port": p.port or adapter_class(p.type).default_port,
             "model": p.model, "camera": bool(p.camera_url) or p.type != "moonraker"} for p in printers.values()]
    text = "\n".join(f"{r['name']:12} {r['type']:10} {r['host']}:{r['port']}  {r['model']}" for r in rows)
    out(args, {"printers": rows}, text or "No printers paired.",
        "gantrel-printer status <name>" if rows else "gantrel-printer discover")


def cmd_status(args):
    adapter = open_printer(args.name)
    before = snapshot_cfg(adapter)
    count = None if args.watch else 1
    for st in adapter.watch(interval=args.watch or 5.0, count=count):
        if st.state == "error":
            nxt = f"Tell the person: {args.name} reports an error. Check the camera: gantrel-printer camera {args.name} --out cam.jpg"
        elif st.state in ("printing",):
            nxt = f"Watch it: gantrel-printer camera {args.name} --out cam.jpg. If it looks wrong: gantrel-printer pause {args.name}"
        elif st.state == "paused":
            nxt = f"Tell the person it is paused. Only they can resume it: {adapter.start_by_hand()}"
        else:
            nxt = f"Check the bed is clear: gantrel-printer camera {args.name} --out cam.jpg"
        out(args, st.to_dict(), st.summary(), nxt)
        sys.stdout.flush()
    save_if_changed(adapter, before)


def cmd_camera(args):
    adapter = open_printer(args.name)
    before = snapshot_cfg(adapter)
    cam = adapter.camera()
    saved = adapter.snapshot(Path(args.out).expanduser())
    save_if_changed(adapter, before)
    size = saved.stat().st_size
    out(args, {"printer": args.name, "file": str(saved.resolve()), "bytes": size, "stream_url": cam.stream_url},
        f"Saved {saved.resolve()} ({size} bytes)." + (f" Live stream: {cam.stream_url}" if cam.stream_url else ""),
        f"Look at the image: is the bed clear and the print OK? If not: gantrel-printer pause {args.name}")


def cmd_profiles(args):
    data = {"printers": {k: p.label for k, p in slicing.PRINTERS.items()},
            "materials": {k: m.note for k, m in slicing.MATERIALS.items()}, "quality": list(slicing.QUALITY)}
    text = "Printer models (--model):\n" + "\n".join(f"  {k:20} {v}" for k, v in data["printers"].items()) \
        + "\nMaterials (--material):\n" + "\n".join(f"  {k:6} {v}" for k, v in data["materials"].items()) \
        + "\nQuality (--quality): " + ", ".join(data["quality"])
    out(args, data, text, "gantrel-printer slice <model.stl> --printer <name> --material PLA")


def cmd_slice(args):
    cfg = config.get(args.printer)
    printer = slicing.profile_for(cfg.model, cfg.type)
    mat = slicing.material(args.material)
    s = slicing.settings(printer, mat, args.quality, force=args.force)
    model = Path(args.model).expanduser().resolve()
    if not model.is_file():
        raise ConfigError(f"no file at {model}")
    out_dir = Path(args.out_dir).expanduser() if args.out_dir else model.parent
    try:
        gcode, used, _ = slicing.slice_model(model, out_dir, printer, s, prefer=args.slicer)
    except PrinterError:
        if not args.json:
            print(f"Settings chosen for {cfg.name} ({printer.label}), {mat.name}, {args.quality}:")
            print("\n".join(f"  {k}: {v}" for k, v in s.items()))
        raise
    facts = slicing.gcode_facts(gcode)
    lines = [f"Sliced {model.name} for {cfg.name} ({printer.label}) with {used}: {gcode}",
             "Settings used:"] + [f"  {k}: {v}" for k, v in s.items()]
    if facts.get("est_seconds"):
        lines.append(f"Estimated print time: {fmt_duration(facts['est_seconds'])}")
    out(args, {"gcode": str(gcode), "slicer": used, "settings": s, **facts}, "\n".join(lines),
        f"gantrel-printer upload {gcode} --printer {cfg.name} --material {mat.name}")


def upload_file(printer: str, path: Path, name: str | None = None, material_name: str | None = None):
    """Send a file to a paired printer and record the job. Never starts it. Returns (job, adapter)."""
    if not path.is_file():
        raise ConfigError(f"no file at {path}")
    requested = name or path.name
    if not REMOTE_RE.match(requested):
        raise ConfigError(f"{requested!r} is not a safe printer file name",
                          "Use letters, digits, . _ - only, ending in .gcode, .bgcode, .3mf, .ctb or .goo")
    material = ""
    if material_name:
        material = slicing.material(material_name).name  # only known materials are stored
    sha = safety.file_sha256(path)
    stem, ext = requested.rsplit(".", 1)
    # The name on the printer carries the content hash, so a different file never shares a job's name.
    remote = f"{stem[:100]}-{sha[:12]}.{ext}"
    adapter = open_printer(printer)
    if ext.lower() in RESIN_EXT and not getattr(adapter, "accepts_resin", False):
        raise ConfigError(f"{printer} is not a resin printer Gantrel can send .{ext} files to",
                          "Send .ctb or .goo files only to an Elegoo resin printer paired with a resin --model")
    before = snapshot_cfg(adapter)
    facts = slicing.gcode_facts(path)
    if adapter.verifies_remote and adapter.remote_info(remote) is not None:
        raise SafetyError(f"{remote} is already on {printer}; uploads never replace a file",
                          "Use its existing job (gantrel-printer jobs), or delete it on the printer first")
    remote_name = adapter.upload(path, remote)
    info = adapter.remote_info(remote_name) if adapter.verifies_remote else {}
    if adapter.verifies_remote and info is None:
        raise PrinterError(f"{printer} did not list {remote_name} after the upload", "Try the upload again")
    save_if_changed(adapter, before)
    if safety.file_sha256(path) != sha:
        raise SafetyError("the file changed during upload", "Upload it again")
    gfacts_material = facts.get("material", "")
    material = material or (gfacts_material if gfacts_material.upper() in slicing.MATERIALS else "")
    job = safety.new_job(printer, path, remote_name, material=material, est_seconds=facts.get("est_seconds"),
                         identity=adapter.identity(), sha256=sha, remote_size=info.get("size"),
                         remote_mtime=info.get("mtime"))
    return job, adapter


def cmd_upload(args):
    path = Path(args.file).expanduser().resolve()
    job, adapter = upload_file(args.printer, path, args.name, args.material)
    remote_name = job.remote_name
    out(args, {"job": job.id, "printer": args.printer, "remote_name": remote_name, "sha256": job.sha256},
        f"Uploaded {path.name} to {args.printer} as {remote_name}. It has NOT started.\n"
        f"Only a person starts a print. {adapter.start_by_hand(remote_name)}",
        f"Tell the person the file name ({remote_name}) and where to start it, then wait for them to say it "
        f"started. Then watch it: gantrel-printer camera {args.printer} --out cam.jpg")


def cmd_calibrate(args):
    """Make a calibration print for one printer. Saves it locally; uploads only with --upload."""
    try:
        cfg = config.get(args.printer)
        printer = slicing.profile_for(cfg.model, cfg.type)
        name = cfg.name
    except ConfigError:
        if args.printer not in slicing.PRINTERS or args.upload:
            raise
        printer, name = slicing.PRINTERS[args.printer], None  # a profile key: make the file, nothing to send to
    mat = slicing.material(args.material)
    cal = calibrate.make(args.kind, printer, mat, force=args.force)
    out_dir = Path(args.out_dir).expanduser() if args.out_dir else Path.cwd() / "gantrel-calibration"
    path = calibrate.save(cal, out_dir.resolve(), calibrate.file_name(args.kind, printer, mat))
    lines = [f"Made a {args.kind} calibration print for {name or printer.key} ({printer.label}), {mat.name}: {path}"]
    lines += calibrate.describe(cal)
    data = {"gcode": str(path), "kind": args.kind, "printer": name or printer.key, "model": printer.key,
            "material": mat.name, "bands": [b.__dict__ for b in cal.bands], "look_for": cal.look_for,
            "change": cal.change, "est_seconds": cal.est_seconds}
    judge = f"When it is done: {cal.look_for}. {cal.change}."
    if args.upload:
        job, adapter = upload_file(name, path, material_name=mat.name)
        data.update(job=job.id, remote_name=job.remote_name)
        lines.append(f"Uploaded to {name} as {job.remote_name}. It has NOT started. Only a person starts a print. "
                     + adapter.start_by_hand(job.remote_name))
        nxt = f"Tell the person to start {job.remote_name} on the printer. {judge}"
    else:
        lines.append("Nothing was sent to the printer.")
        send = (f"gantrel-printer upload {path} --printer {name}" if name
                else "pair the printer, then gantrel-printer upload it")
        nxt = f"Get it to the printer ({send}, or copy it to the printer's USB stick or SD card); a person starts it. {judge}"
    out(args, data, "\n".join(lines), nxt)


def cmd_jobs(args):
    jobs = safety.list_jobs()
    text = "\n".join(f"{j.id}  {j.printer:10} {j.remote_name}  {j.material or '?'}  {fmt_duration(j.est_seconds)}"
                     for j in jobs)
    out(args, {"jobs": [j.__dict__ for j in jobs]}, text or "No uploads.",
        "A person starts a print on the printer itself; see the upload message")


def cmd_pause(args):
    open_printer(args.name).pause()
    out(args, {"paused": args.name}, f"Paused {args.name}.",
        f"Tell the person it is paused. Only they can resume it: {open_printer(args.name).start_by_hand()}")


def cmd_cancel(args):
    open_printer(args.name).cancel()
    out(args, {"cancelled": args.name}, f"Cancelled the print on {args.name}.", f"gantrel-printer status {args.name}")


def cmd_estop(args):
    did = open_printer(args.name).emergency_stop()
    out(args, {"estop": args.name, "result": did}, did + ".",
        f"Tell the person what happened now. Then: gantrel-printer status {args.name}")


def cmd_watch(args):
    if args.interval <= 0:
        raise ConfigError("--interval must be more than 0 seconds")
    if args.consecutive is not None and args.consecutive < 1:
        raise ConfigError("--consecutive must be 1 or more")
    adapter = open_printer(args.name)
    opts = monitor.WatchOptions(interval=args.interval, guard=args.guard, timelapse=args.timelapse,
                                notify=args.notify, start_timeout=args.start_timeout, duration=args.duration,
                                any_state=args.any_state)
    guard = None
    if args.guard:
        guard = vguard.VisionGuard.from_settings(args.vision_url, args.vision_model, args.consecutive)
        guard.check()
    notifier = notify.Notifier.from_settings() if args.notify else None
    if args.background:
        return watch_in_background(args)
    job_dir = None
    if args.job_dir:  # set only by watch --background for its own child process
        job_dir = Path(args.job_dir).resolve()
        if job_dir.parent != monitor.jobs_root(args.name).resolve() or not monitor.JOB_RE.match(job_dir.name):
            raise ConfigError("--job-dir must be a job folder made by watch --background")
    quiet = getattr(args, "json", False)

    def log(line):
        if not quiet:
            print(line, flush=True)

    if guard and not quiet:
        print(f"Guard on: {guard.model} through Fonsviva at {guard.host}; pauses after {guard.consecutive} "
              "failed frames in a row.\n"
              + vguard.EXPERIMENTAL, flush=True)
    res = monitor.run(adapter, opts, guard=guard, notifier=notifier, job_dir=job_dir, log=log)
    nxt = {
        "finished": "Tell the person the print finished" + (f"; the timelapse is {res.timelapse}" if res.timelapse
                                                           else "") + ". Ask them to check the part",
        "paused_by_guard": f"Tell the person the guard paused {args.name} and why (see pause.json in {res.job_dir}). "
                           "Only they can resume it, on the printer, after looking at the print",
        "failed": f"Tell the person {args.name} reports an error. Then: gantrel-printer status {args.name}",
        "cancelled": "Tell the person the print was cancelled",
        "ended": f"Tell the person the print on {args.name} is no longer running, and ask whether it finished",
        "lost": f"Tell the person Gantrel lost contact with {args.name}. Then: gantrel-printer status {args.name}",
        "unreachable": f"gantrel-printer status {args.name}",
        "not_started": f"Ask the person to start the print on the printer, then run gantrel-printer watch {args.name} "
                       "again",
        "stopped": f"gantrel-printer frames {args.name}",
        "time_up": f"Look at the frames in {res.job_dir}, or list jobs: gantrel-printer frames {args.name}",
    }.get(res.outcome, f"gantrel-printer status {args.name}")
    text = f"Watch of {args.name} ended: {res.outcome}. {res.frames} frames in {res.job_dir}."
    if res.pause:
        text += f"\nPaused because: {res.pause['reason']}"
    if args.timelapse:
        text += f"\nTimelapse: {res.timelapse or res.timelapse_note}"
    out(args, {"printer": args.name, "outcome": res.outcome, "job_dir": str(res.job_dir), "frames": res.frames,
               "timelapse": res.timelapse, "timelapse_note": res.timelapse_note, "pause": res.pause or None},
        text, nxt)


def watch_in_background(args):
    """Start the same watch as its own process and return at once, so a chat or agent isn't blocked for hours."""
    if monitor.is_locked(args.name):
        raise SafetyError(f"{args.name} is already being watched", f"See it with: gantrel-printer frames {args.name}")
    job_dir = monitor.new_job_dir(args.name)
    argv = [sys.executable, "-m", "gantrel_printer.cli", "watch", args.name, "--interval", str(args.interval),
            "--start-timeout", str(args.start_timeout), "--job-dir", str(job_dir)]
    if args.duration:
        argv += ["--duration", str(args.duration)]
    for flag in ("guard", "timelapse", "notify", "any_state"):
        if getattr(args, flag):
            argv.append("--" + flag.replace("_", "-"))
    for flag, val in (("--vision-url", args.vision_url), ("--vision-model", args.vision_model),
                      ("--consecutive", args.consecutive)):
        if val:
            argv += [flag, str(val)]
    log = open(job_dir / "watch.log", "a")
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True, env=os.environ.copy())
    time.sleep(1.5)
    if proc.poll() not in (None, 0):
        raise PrinterError(f"the background watch stopped at once: {(job_dir / 'watch.log').read_text()[-300:]}")
    out(args, {"printer": args.name, "job_dir": str(job_dir), "pid": proc.pid, "background": True},
        f"Watching {args.name} in the background (process {proc.pid}). Frames and log: {job_dir}",
        f"Tell the person Gantrel is watching. Check on it any time with: gantrel-printer frames {args.name}")


def job_rows(name: str) -> list[dict]:
    root = monitor.jobs_root(name)
    rows = []
    running = monitor.is_locked(name)
    for d in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True) if root.is_dir() else []:
        try:
            job = json.loads((d / "job.json").read_text())
        except (OSError, ValueError):
            job = {}
        outcome = job.get("outcome", "unknown")
        if outcome == "running" and not running:
            outcome = "stopped unexpectedly"
        rows.append({"job": d.name, "dir": str(d), "started": job.get("started", ""), "outcome": outcome,
                     "frames": len(list((d / "frames").glob("*"))) if (d / "frames").is_dir() else 0,
                     "timelapse": str(d / "timelapse.mp4") if (d / "timelapse.mp4").is_file() else "",
                     "paused_because": (job.get("pause") or {}).get("reason", "")})
    return rows


def cmd_frames(args):
    config.get(args.name)
    if args.timelapse:
        d = monitor.jobs_root(args.name) / args.timelapse
        if not monitor.JOB_RE.match(args.timelapse) or not d.is_dir():
            raise ConfigError(f"no watch job {args.timelapse!r} for {args.name}", f"See them: gantrel-printer frames {args.name}")
        path, note = monitor.make_timelapse(d)
        out(args, {"job": args.timelapse, "timelapse": str(path or ""), "note": note},
            f"Timelapse: {path}" if path else note, f"gantrel-printer frames {args.name}")
        return
    rows = job_rows(args.name)
    lines = [f"{r['job']}  {r['outcome']:22} {r['frames']:5} frames  {r['dir']}"
             + (f"\n    timelapse {r['timelapse']}" if r["timelapse"] else "")
             + (f"\n    paused because: {r['paused_because']}" if r["paused_because"] else "") for r in rows]
    out(args, {"printer": args.name, "jobs": rows}, "\n".join(lines) or f"No watch jobs for {args.name} yet.",
        f"Open a job folder to see its frames, or watch a print: gantrel-printer watch {args.name} --timelapse"
        if rows else f"After the person starts a print: gantrel-printer watch {args.name} --timelapse --background")


def cmd_watch_config(args):
    data = config.load_watch_settings()
    vision = data.setdefault("vision", {})
    note = data.setdefault("notify", {})
    if args.vision_url:
        vguard.check_local(args.vision_url)
        vision["url"] = args.vision_url
    if args.vision_model:
        vision["model"] = args.vision_model
    if args.consecutive is not None:
        if args.consecutive < 1:
            raise ConfigError("--consecutive must be 1 or more")
        vision["consecutive"] = args.consecutive
    if args.fonsviva_key_stdin and args.notify_url_stdin:
        raise ConfigError("--fonsviva-key-stdin and --notify-url-stdin both read stdin",
                          "Run watch-config once for each")
    if args.fonsviva_key_stdin:
        key = sys.stdin.readline().strip()
        if not key:
            raise ConfigError("no Fonsviva key on stdin", "Pipe Gantrel's Fonsviva app key in; it is never printed")
        config.set_secret(config.FONSVIVA_KEY, key)
    url = None
    if args.notify_url_stdin:
        url = sys.stdin.readline().strip()
    if url:
        n = notify.Notifier(url, args.notify_kind or "auto")
        config.set_secret(config.NOTIFY_KEY, url)
        note["kind"] = n.kind
        note["target"] = notify.shown(url)
    if args.notify_kind and not url:
        note["kind"] = args.notify_kind
    if args.attach_frame is not None:
        note["attach_frame"] = args.attach_frame
    if args.clear_notify:
        config.delete_secret(config.NOTIFY_KEY)
        data["notify"] = note = {}
    config.save_watch_settings(data)
    has_url = bool(config.get_secret(config.NOTIFY_KEY))
    has_key = bool(config.get_secret(config.FONSVIVA_KEY))
    text = (f"Vision model: {vision.get('model', vguard.DEFAULT_MODEL)} through Fonsviva at "
            f"{vision.get('url', vguard.DEFAULT_URL)}, pause after "
            f"{vision.get('consecutive', vguard.DEFAULT_CONSECUTIVE)} failed frames in a row.\n"
            f"Fonsviva key: {'saved in the OS keyring' if has_key else 'not set'}.\n"
            + (f"Notices: {note.get('kind', 'auto')} to {note.get('target', 'a saved URL')}, frame attached: "
               f"{ {True: 'yes', False: 'no', None: 'only to a local address'}[note.get('attach_frame')] }."
               if has_url else "Notices: not set."))
    out(args, {"vision": vision, "fonsviva_key": has_key, "notify": {**note, "set": has_url}}, text,
        "gantrel-printer watch <name> --timelapse --background" + (" --notify" if has_url else "")
        + " (add --guard only if the person asks for the experimental failure guard)")


# ---------------------------------------------------------------- parser


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gantrel-printer", description=__doc__.split("\n")[0])
    p.add_argument("--version", action="version", version=f"gantrel-printer {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, json_=True):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        if json_:
            sp.add_argument("--json", action="store_true", help="machine-readable output")
        return sp

    sp = add("discover", cmd_discover, "find printers on the local network (read-only)")
    sp.add_argument("--subnet", help="network to scan, like 192.168.1.0/24 (default: this machine's /24)")
    sp.add_argument("--host", action="append", help="probe only this address (repeatable)")
    sp.add_argument("--seconds", type=float, default=3.0, help="how long to listen for mDNS and SSDP")
    sp.add_argument("--no-mdns", action="store_true")

    sp = add("pair", cmd_pair, "save a printer; secrets go to the OS keyring")
    sp.add_argument("name")
    sp.add_argument("--type", required=True, choices=list(ADAPTERS))
    sp.add_argument("--host", required=True)
    sp.add_argument("--port", type=int)
    sp.add_argument("--model", help="slicing profile, see: gantrel-printer profiles")
    sp.add_argument("--serial", help="Bambu Lab or FlashForge serial number, Repetier-Server printer slug, "
                    "or Elegoo mainboard ID")
    sp.add_argument("--user", help="login name for digest auth (PrusaLink default: maker)")
    sp.add_argument("--camera-url", help="JPEG snapshot URL, for cameras the printer API doesn't list")
    sp.add_argument("--stream-url", help="live stream URL (no passwords in it)")
    sp.add_argument("--secret-stdin", action="store_true", help="read the API key or access code from stdin")
    sp.add_argument("--no-check", action="store_true", help="save without contacting the printer")

    sp = add("unpair", cmd_unpair, "forget a printer and delete its secret")
    sp.add_argument("name")
    add("list", cmd_list, "paired printers")
    sp = add("status", cmd_status, "read-only status")
    sp.add_argument("name")
    sp.add_argument("--watch", type=float, metavar="SECONDS", help="repeat every SECONDS until Ctrl-C")
    sp = add("camera", cmd_camera, "save one camera frame")
    sp.add_argument("name")
    sp.add_argument("--out", required=True, help="where to save the JPEG")
    add("profiles", cmd_profiles, "printer models, materials and quality levels for slicing")
    sp = add("slice", cmd_slice, "slice a model with the printer's profile")
    sp.add_argument("model")
    sp.add_argument("--printer", required=True)
    sp.add_argument("--material", default="PLA")
    sp.add_argument("--quality", default="standard", choices=list(slicing.QUALITY))
    sp.add_argument("--slicer", choices=list(slicing.SLICERS))
    sp.add_argument("--out-dir")
    sp.add_argument("--force", action="store_true",
                    help="slice a material the printer can't print well (ABS on an open printer); prints a warning")
    sp = add("upload", cmd_upload, "send a file to the printer (never starts it)")
    sp.add_argument("file")
    sp.add_argument("--printer", required=True)
    sp.add_argument("--material")
    sp.add_argument("--name", help="file name on the printer")
    sp = add("calibrate", cmd_calibrate, "make a calibration print (saved locally; never starts a print)")
    sp.add_argument("printer", help="a paired printer, or a profile key from: gantrel-printer profiles")
    sp.add_argument("kind", choices=list(calibrate.KINDS))
    sp.add_argument("--material", default="PLA")
    sp.add_argument("--out-dir", help="where to save the G-code (default: ./gantrel-calibration)")
    sp.add_argument("--upload", action="store_true", help="also send it to the printer (never starts it)")
    sp.add_argument("--force", action="store_true",
                    help="make it for a material the printer can't print well; prints a warning")
    add("jobs", cmd_jobs, "files uploaded with this tool")
    for name, fn, h in (("pause", cmd_pause, "pause now (safe)"),
                        ("cancel", cmd_cancel, "cancel the current print"),
                        ("estop", cmd_estop, "emergency stop")):
        sp = add(name, fn, h)
        sp.add_argument("name")
    sp = add("watch", cmd_watch, "watch a running print: frames, status, failure guard, timelapse, notices")
    sp.add_argument("name")
    sp.add_argument("--interval", type=float, default=30.0, help="seconds between frames (default 30)")
    sp.add_argument("--guard", action="store_true",
                    help="experimental: check each frame with a local vision model and pause after repeated failures")
    sp.add_argument("--timelapse", action="store_true", help="make timelapse.mp4 when the print ends (needs ffmpeg)")
    sp.add_argument("--notify", action="store_true", help="send a notice when it ends (set up with watch-config)")
    sp.add_argument("--vision-url", help=f"Fonsviva's OpenAI-compatible endpoint on your network "
                                         f"(default {vguard.DEFAULT_URL})")
    sp.add_argument("--vision-model", help=f"vision model name (default {vguard.DEFAULT_MODEL})")
    sp.add_argument("--consecutive", type=int, help="failed frames in a row before the guard pauses (default 2)")
    sp.add_argument("--start-timeout", type=float, default=600.0,
                    help="seconds to wait for the print to start (default 600)")
    sp.add_argument("--duration", type=float, metavar="SECONDS", help="stop after this many seconds")
    sp.add_argument("--any-state", action="store_true",
                    help="take frames even when no print is running (to check the camera); the guard still "
                         "judges only frames taken while printing")
    sp.add_argument("--background", action="store_true", help="run as its own process and return at once")
    sp.add_argument("--job-dir", help=argparse.SUPPRESS)
    sp = add("frames", cmd_frames, "list a printer's watch jobs (frames, timelapse, guard pauses)")
    sp.add_argument("name")
    sp.add_argument("--timelapse", metavar="JOB", help="(re)make the timelapse for one job")
    sp = add("watch-config", cmd_watch_config, "set the vision model and where notices go")
    sp.add_argument("--vision-url", help=f"Fonsviva's OpenAI-compatible endpoint (default {vguard.DEFAULT_URL})")
    sp.add_argument("--vision-model")
    sp.add_argument("--consecutive", type=int)
    sp.add_argument("--fonsviva-key-stdin", action="store_true",
                    help="read Gantrel's Fonsviva app key from stdin; it is kept in the OS keyring and never shown")
    sp.add_argument("--notify-url-stdin", action="store_true",
                    help="read the ntfy topic or webhook URL from stdin; it is kept in the OS keyring")
    sp.add_argument("--notify-kind", choices=list(notify.KINDS))
    sp.add_argument("--attach-frame", dest="attach_frame", action="store_true", default=None,
                    help="send the last frame with notices, even to a target outside your network")
    sp.add_argument("--no-attach-frame", dest="attach_frame", action="store_false")
    sp.add_argument("--clear-notify", action="store_true", help="forget the notice target")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        args.fn(args)
    except SafetyError as err:
        print(f"REFUSED: {err}", file=sys.stderr)
        print("NEXT: do not retry around this; tell the person what was refused", file=sys.stderr)
        return EXIT_SAFETY
    except PrinterError as err:
        print(f"ERROR ({err.code}): {err}", file=sys.stderr)
        print("NEXT: tell the person this error and follow its advice", file=sys.stderr)
        return EXIT_ERROR
    except ValueError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
