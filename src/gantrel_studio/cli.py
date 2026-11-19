"""gantrel-studio: serve the 3D viewport for one smoothstone job on this computer."""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

from . import server


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gantrel-studio", description="Local 3D viewport for a smoothstone job.")
    p.add_argument("--job", default="./stone-job", help="job folder (default ./stone-job)")
    p.add_argument("--port", type=int, default=7342, help="first port to try; the next 10 are tried if it is taken")
    p.add_argument("--host", default="127.0.0.1", help="address to bind (default 127.0.0.1)")
    p.add_argument("--no-open", action="store_true", help="do not open a browser")
    return p


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        httpd = server.bind(Path(args.job), host=args.host, port=args.port)
    except OSError as e:
        print(f"gantrel-studio: {e}", file=sys.stderr)
        return 1
    httpd.studio.start()
    url = f"http://127.0.0.1:{httpd.port}/?t={httpd.studio.launch_id}"
    print(f"Gantrel Studio: {url}", flush=True)
    if not args.no_open:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        httpd.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
