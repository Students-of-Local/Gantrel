"""Measure vision models for the print failure guard on the labeled frames in tests/printers/frames.

    uv run python benchmarks/guard_vision.py --url http://127.0.0.1:11434/v1 --model qwen2.5vl:7b \
        --host "abbey1 (GTX 1650 SUPER 4 GB)"
    uv run python benchmarks/guard_vision.py --table    # the Markdown table for docs/PRINTERS.md

Sends every frame through the same VisionGuard.classify() the watch uses, with the printer status in
its label, and writes benchmarks/results/guard-vision/<model>.json. The gate for shipping a model as
the default: accuracy of 80% or more on failed vs not failed, and no good frame judged failed.

This is a developer tool: it talks to a model server directly (such as Ollama), with no Fonsviva
key, so models can be measured before they are offered through Fonsviva. The watch itself never
does this; its guard runs only through Fonsviva (src/gantrel_printer/guard.py).
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gantrel_printer.guard import VisionGuard  # noqa: E402
from gantrel_printer.model import Status  # noqa: E402

FRAMES = ROOT / "tests" / "printers" / "frames"
RESULTS = ROOT / "benchmarks" / "results" / "guard-vision"


def labels() -> list[dict]:
    out = []
    for f in sorted(FRAMES.glob("labels*.json")):
        out += json.loads(f.read_text())
    return out


def table() -> str:
    lines = ["| Model | Accuracy | Failed frames caught | Good frames judged failed | Malformed | "
             "Median / max latency | Passes the gate | Host | Date |",
             "|---|---|---|---|---|---|---|---|---|"]
    runs = [json.loads(f.read_text())["summary"] for f in sorted(RESULTS.glob("*.json"))]
    for s in sorted(runs, key=lambda s: (-s["accuracy"], s["false_failed_on_good"])):
        kinds = ", ".join(f"{k} {v}" for k, v in s["by_kind"].items())
        lines.append(f"| `{s['model']}` | {s['accuracy'] * 100:.0f}% | {s['failed_caught']} ({kinds}) | "
                     f"{s['false_failed_on_good']} of {s['frames']['good']} | {s['malformed']} | "
                     f"{s['latency_s']['median']} s / {s['latency_s']['max']} s | "
                     f"{'yes' if s['passes_gate'] else 'no'} | {s['host']} | {s['date']} |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", action="store_true", help="print the results table and stop")
    ap.add_argument("--url")
    ap.add_argument("--model")
    ap.add_argument("--host", help="where the model ran, for the record")
    ap.add_argument("--timeout", type=float, default=600)
    args = ap.parse_args()
    if args.table:
        print(table())
        return 0
    if not (args.url and args.model and args.host):
        ap.error("--url, --model and --host are needed to run a measurement")
    guard = VisionGuard(args.url, args.model, timeout=args.timeout, direct=True)
    guard.check()
    rows = []
    for item in labels():
        st = Status(printer="bench", type="bench", **item["status"])
        v = guard.classify(FRAMES / item["file"], st)
        if v.source == "unreachable":  # a server error while the model loads is not a judgment; try once more
            v = guard.classify(FRAMES / item["file"], st)
        rows.append({"file": item["file"], "label": item["label"], "kind": item["kind"], **v.to_dict()})
        print(f"{item['label']:6} {v.verdict:7} {v.latency_s:6.1f}s {item['file']}: {v.reason[:90]}", flush=True)
    good = [r for r in rows if r["label"] == "ok"]
    bad = [r for r in rows if r["label"] == "failed"]
    tp = sum(r["verdict"] == "failed" for r in bad)
    tn = sum(r["verdict"] != "failed" for r in good)
    lat = [r["latency_s"] for r in rows if r["source"] != "unreachable"]
    summary = {
        "model": args.model, "host": args.host, "date": datetime.date.today().isoformat(),
        "frames": {"good": len(good), "failed": len(bad)},
        "accuracy": round((tp + tn) / len(rows), 3),
        "failed_caught": f"{tp}/{len(bad)}",
        "false_failed_on_good": sum(r["verdict"] == "failed" for r in good),
        "good_called_ok": sum(r["verdict"] == "ok" for r in good),
        "malformed": sum(r["source"] == "malformed" for r in rows),
        "unreachable": sum(r["source"] == "unreachable" for r in rows),
        "latency_s": {"median": round(statistics.median(lat), 1) if lat else None,
                      "max": round(max(lat), 1) if lat else None},
        "by_kind": {k: f"{sum(r['verdict'] == 'failed' for r in bad if r['kind'] == k)}/"
                       f"{sum(r['kind'] == k for r in bad)}" for k in sorted({r['kind'] for r in bad})},
    }
    summary["passes_gate"] = summary["accuracy"] >= 0.8 and summary["false_failed_on_good"] == 0
    out = RESULTS / (re.sub(r"[^A-Za-z0-9._-]", "_", args.model) + ".json")
    out.write_text(json.dumps({"summary": summary, "frames": rows}, indent=1) + "\n")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
