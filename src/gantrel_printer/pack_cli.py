"""gantrel-printer pack: check, show, export, import, list and remove printer packs.

A pack is a printer setup someone can share and someone else can review (packs.py). Nothing here
pairs with, contacts or starts a printer.
"""

from __future__ import annotations

from pathlib import Path

from . import packs
from .errors import ConfigError, SafetyError

EXIT_REFUSED = 1


def _out(args, data, text, nxt):
    from .cli import out
    out(args, data, text, nxt)


def _item_lines(rep: packs.Report, levels=("refusal", "finding", "note")) -> list[str]:
    lines = []
    for lv in levels:
        for i in rep.of(lv):
            lines.append(f"  {lv.upper()} {i.where()}: {i.reason}" + (f"\n      {i.text}" if i.text else ""))
    return lines


def cmd_check(args):
    results, lines, worst = [], [], 0
    for f in args.files:
        pack, rep, _ = packs.check_file(f)
        results.append({"file": f, **rep.to_dict()})
        head = {"pass": "PASS", "findings": f"PASS with {len(rep.findings)} finding(s) that need acceptance",
                "refused": f"REFUSED ({len(rep.refusals)} refusal(s), {len(rep.findings)} finding(s))"}[rep.result]
        lines.append(f"{f}: {head}" + (f" (sha256 {rep.sha256}, accept code {rep.sha256[:12]})" if rep.sha256 else ""))
        lines += _item_lines(rep)
        if rep.refusals or (args.strict and rep.findings):
            worst = EXIT_REFUSED
    one = args.files[0] if len(args.files) == 1 else "<file>"
    if worst:
        nxt = "Do not import or share a refused pack. Tell the person each REFUSED line and fix the pack"
    elif any(r["findings"] for r in results):
        nxt = (f"gantrel-printer pack show {one}, then read every FINDING to the person; import only with their yes")
    else:
        nxt = f"gantrel-printer pack show {one}, and show it to the person before importing or sharing it"
    _out(args, {"files": results, "ok": not worst}, "\n".join(lines), nxt)
    return worst


def cmd_show(args):
    pack, rep, data = packs.check_file(args.file)
    lines = packs.show_lines(pack, rep, data)
    if rep.result == "refused":
        nxt = "Do not import or share it. Tell the person each REFUSED line"
    else:
        flag = " --accept-findings" if rep.findings else ""
        nxt = (f"Tell the person what this pack sets{' and read them every FINDING' if rep.findings else ''}. "
               f"Import it only with their yes: gantrel-printer pack import {args.file} --accept {rep.sha256[:12]}{flag}")
    data = {"pack": pack, **rep.to_dict()}
    if rep.private:
        # Private data is withheld: none of the pack's strings, and no accept code for a file no one may import.
        data["pack"] = None
        data["withheld"] = "the pack holds private data; see refusals"
        del data["accept"]
        nxt = ("Do not import or share it, and do not open the file to read it out. Tell the person each REFUSED "
               "line (the private part is hidden) so they can take it out")
    _out(args, data, "\n".join(lines), nxt)
    return EXIT_REFUSED if rep.result == "refused" else 0


def cmd_export(args):
    pack, left_out = packs.export(args.printer, args.id, args.name, args.author or "", args.notes or "", args.material)
    data = packs.dumps(pack)
    checked, rep = packs.check_bytes(data)
    dest = Path(args.out or f"{pack['id']}.json").expanduser()
    lines = []
    if rep.refusals:
        lines.append(f"Not written: the pack fails its own check ({len(rep.refusals)} refusal(s)).")
        lines += _item_lines(rep, ("refusal",))
        _out(args, {"written": False, **rep.to_dict()}, "\n".join(lines),
             "Tell the person each REFUSED line; fix the text you passed (--name, --author, --notes) and export again")
        return EXIT_REFUSED
    if dest.exists():
        raise ConfigError(f"{dest} already exists", "Use another --out, or delete the old file first")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    lines.append(f"Wrote {dest} ({len(data)} bytes): pack {pack['id']} for {pack['printer']['model']}, "
                 f"materials {', '.join(m['family'] for m in pack['materials'])}.")
    lines.append(f"sha256 {rep.sha256}")
    lines.append(f"accept code (first 12 hex): {rep.sha256[:12]}")
    lines.append("Calibration results: Gantrel does not record them yet, so every calibrated list is empty. "
                 "Add what the person measured, then run pack check again (the hash changes).")
    lines += left_out
    lines += _item_lines(rep, ("finding", "note"))
    _out(args, {"written": True, "file": str(dest), "left_out": left_out, **rep.to_dict()}, "\n".join(lines),
         f"gantrel-printer pack show {dest}, and show the person everything in it. Share it only with their yes")
    return 0


def cmd_import(args):
    path = Path(args.file).expanduser()
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise ConfigError(f"no file at {path}") from None
    if size > packs.MAX_FILE:
        raise SafetyError(f"{path} is {size} bytes; packs are at most {packs.MAX_FILE} bytes")
    data = path.read_bytes()   # read once: these exact bytes are hashed, checked and installed
    pack, rep, dest = packs.install(data, args.accept, args.accept_findings, args.replace)
    fam = pack["materials"][0]["family"]
    _out(args, {"installed": pack["id"], "sha256": rep.sha256, "file": str(dest),
                "findings_accepted": len(rep.findings)},
         f"Installed pack {pack['id']} {pack['version']} ({pack['printer']['model']}) as {dest}."
         + (f" {len(rep.findings)} finding(s) accepted." if rep.findings else ""),
         f"gantrel-printer slice <model.stl> --printer pack:{pack['id']} --material {fam} (the pack sets the "
         "profile; upload still goes to a paired printer)")
    return 0


def cmd_list(args):
    rows = packs.installed()
    lines = []
    for r in rows:
        if r["ok"]:
            lines.append(f"{r['id']:24} {r['version']:8} {r['accept']}  {r['model']}: {', '.join(r['materials'])}"
                         + (f"  ({r['findings_accepted']} finding(s) accepted)" if r["findings_accepted"] else ""))
        else:
            lines.append(f"{r['id']:24} PROBLEM {r['accept']}: {r['problem']}")
    _out(args, {"packs": rows}, "\n".join(lines) or "No packs installed.",
         "gantrel-printer slice <model.stl> --printer pack:<id> --material <family>" if rows
         else "Import a reviewed pack: gantrel-printer pack show <file>, then pack import <file> --accept <code>")
    return 0


def cmd_remove(args):
    gone = packs.remove(args.id)
    if not gone:
        raise ConfigError(f"no pack {args.id!r} is installed", "Run gantrel-printer pack list")
    _out(args, {"removed": args.id}, f"Removed pack {args.id}.", "gantrel-printer pack list")
    return 0


def register(sub, add) -> None:
    sp = add("pack", cmd_check, "share a printer setup as a reviewable pack (data only; never contacts a printer)",
             json_=False)
    ps = sp.add_subparsers(dest="pack_cmd", required=True)

    def padd(name, fn, help_):
        p = ps.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        p.add_argument("--json", action="store_true", help="machine-readable output")
        return p

    p = padd("check", cmd_check, "check pack files: schema, ranges, filament rules, privacy and the G-code policy")
    p.add_argument("files", nargs="+", metavar="file")
    p.add_argument("--strict", action="store_true", help="also fail on findings")
    p = padd("show", cmd_show, "everything a reviewer needs: settings, every G-code line with findings, the sha256")
    p.add_argument("file")
    p = padd("export", cmd_export, "write a pack from a paired printer (or a profile key) and Gantrel's settings")
    p.add_argument("printer", help="a paired printer, or a profile key from: gantrel-printer profiles")
    p.add_argument("--id", help="pack id: 3 to 64 lowercase letters, digits and hyphens (default: the profile key)")
    p.add_argument("--name", help="a title for the pack")
    p.add_argument("--author", help="how the author wants to be named (no email)")
    p.add_argument("--notes", help="what the person wants others to know")
    p.add_argument("--material", action="append", help="only this material (repeatable; default: every one that "
                                                        "passes on this printer)")
    p.add_argument("--out", help="file to write (default: <id>.json here)")
    p = padd("import", cmd_import, "install a reviewed pack; needs --accept with the code pack show printed")
    p.add_argument("file")
    p.add_argument("--accept", metavar="CODE", help="the first 12 hex digits of the file's sha256, from pack show")
    p.add_argument("--accept-findings", action="store_true", help="the person read every finding and said yes")
    p.add_argument("--replace", action="store_true", help="replace a different file installed under the same id")
    padd("list", cmd_list, "installed packs")
    p = padd("remove", cmd_remove, "uninstall a pack")
    p.add_argument("id")
