"""The Gantrel Studio HTTP server: one smoothstone job, a 3D viewport page and a small JSON API.

Security model (docs/STUDIO.md): it binds 127.0.0.1, answers only Host 127.0.0.1:<port> or
localhost:<port> (DNS rebinding), every /api/* call carries the launch token, POSTs with a foreign
Origin are refused, params are checked against the recipe schema before stone runs, and stone runs
from an argv list, never a shell. The token is never logged.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import re
import secrets
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import params as paramcheck
from . import threemf

WEB_ROOT = Path(__file__).resolve().parent / "web"
PLACEHOLDER = b"<!doctype html><title>Gantrel Studio</title><p>Gantrel Studio is running; the page is not built yet.</p>\n"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json",
}
WATCHED = ("brief.md", "status.json", "plan.json", "build/params.json", "build/check.json", "build/part.stl")
POLL_SECONDS = 0.25
KEEPALIVE_SECONDS = 15.0
BUILD_TIMEOUT = 900
SHOW_TIMEOUT = 60
MAX_BODY = 64 * 1024
ERROR_LINES = 20


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def tail(text: str, n: int = ERROR_LINES) -> str:
    return "\n".join(text.rstrip().splitlines()[-n:])


def new_launch_id() -> str:
    """The per-launch value every /api/* call must carry. New each launch, never written to disk."""
    return secrets.token_urlsafe(16)


class Studio:
    """The job, its watched files, the build queue and the state version the page follows."""

    def __init__(self, job: Path, stone: str | None = None):
        self.job = Path(job).resolve()
        self.stone_override = stone
        self.launch_id = new_launch_id()
        self.cond = threading.Condition()
        self.version = 1
        self.stopped = False
        self.running = False
        self.pending: dict | None = None  # {"reset": bool, "params": {name: text}}
        self.error: str | None = None
        self.builds = 0
        self.sse_clients = 0
        self.keepalive = KEEPALIVE_SECONDS
        self.poll = POLL_SECONDS
        self._schemas: dict[str, dict] = {}
        self._part_cache: tuple | None = None
        self._3mf_cache: tuple | None = None
        self._snapshot = self._stat()
        self._threads = [
            threading.Thread(target=self._watch, name="studio-watch", daemon=True),
            threading.Thread(target=self._builder, name="studio-build", daemon=True),
        ]

    # lifecycle
    def start(self) -> None:
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        with self.cond:
            self.stopped = True
            self.cond.notify_all()

    def bump(self) -> None:
        with self.cond:
            self.version += 1
            self.cond.notify_all()

    # stone
    def stone(self) -> str:
        exe = self.stone_override or shutil.which("stone")
        if not exe:
            raise FileNotFoundError("stone is not on PATH")
        return exe

    def run_stone(self, *args: str, timeout: float = BUILD_TIMEOUT) -> tuple[int, str]:
        try:
            argv = [self.stone(), *args]
        except FileNotFoundError as e:
            return 127, str(e)
        try:
            p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=timeout)
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"").decode("utf-8", "replace")
            return 124, out + f"\nstone {args[0]} took longer than {int(timeout)} s and was stopped"
        except OSError as e:
            return 127, f"could not run stone: {e}"
        return p.returncode, p.stdout.decode("utf-8", "replace")

    def recipe_info(self, recipe: str) -> dict | None:
        """{"name","title","summary","tier","params"} from stone show --json, cached per recipe."""
        with self.cond:
            hit = self._schemas.get(recipe)
        if hit is not None:
            return hit
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}", recipe):
            return None
        code, out = self.run_stone("show", recipe, "--json", "--job", str(self.job), timeout=SHOW_TIMEOUT)
        if code != 0:
            return None
        try:
            data = json.loads(out)
        except ValueError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("params"), list):
            return None
        info = {k: data.get(k) for k in ("name", "title", "summary", "tier", "params")}
        with self.cond:
            self._schemas[recipe] = info
        return info

    # job files
    def _stat(self) -> tuple:
        snap = []
        for rel in WATCHED:
            try:
                st = (self.job / rel).stat()
                snap.append((rel, st.st_mtime_ns, st.st_size))
            except OSError:
                snap.append((rel, None, None))
        return tuple(snap)

    def _watch(self) -> None:
        while True:
            with self.cond:
                if self.cond.wait_for(lambda: self.stopped, timeout=self.poll):
                    return
            snap = self._stat()
            if snap != self._snapshot:
                self._snapshot = snap
                self.bump()

    def plan(self) -> dict | None:
        p = read_json(self.job / "plan.json")
        if isinstance(p, dict) and p.get("recipe"):
            return p
        return None

    def recipe(self) -> str | None:
        plan = self.plan()
        if plan:
            return str(plan["recipe"])
        built = read_json(self.job / "build" / "params.json")
        if isinstance(built, dict) and built.get("recipe"):
            return str(built["recipe"])
        return None

    def request_text(self) -> str:
        try:
            text = (self.job / "brief.md").read_text()
        except OSError:
            return ""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        return " ".join(lines)

    def current_params(self, recipe: str | None, schema: list[dict]) -> dict:
        built = read_json(self.job / "build" / "params.json")
        if recipe and isinstance(built, dict) and built.get("recipe") == recipe and isinstance(built.get("params"), dict):
            return built["params"]
        plan = self.plan() or {}
        if recipe and plan.get("recipe") == recipe:
            out = {p["name"]: p.get("default") for p in schema if isinstance(p, dict) and "name" in p}
            out.update(plan.get("params") or {})
            return out
        return {}

    def part_info(self) -> dict | None:
        f = self.job / "build" / "part.stl"
        try:
            st = f.stat()
        except OSError:
            return None
        check = read_json(self.job / "build" / "check.json")
        size = check.get("size_mm") if isinstance(check, dict) else None
        key = (st.st_mtime_ns, st.st_size, json.dumps(size))
        cache = self._part_cache
        if cache and cache[0] == key:
            return cache[1]
        try:
            data = f.read_bytes()
            faces = threemf.face_count(data)
            if not (isinstance(size, list) and len(size) == 3):
                size = threemf.bbox_size(threemf.triangles(data))
        except (OSError, threemf.StlProblem, ValueError):
            return None
        info = {"url": f"/api/part.stl?v={st.st_mtime_ns}", "size_mm": size, "faces": faces}
        self._part_cache = (key, info)
        return info

    def state(self) -> dict:
        with self.cond:
            version, building, error = self.version, self.running or self.pending is not None, self.error
        recipe = self.recipe()
        info = self.recipe_info(recipe) if recipe else None
        schema = (info or {}).get("params") or []
        status = read_json(self.job / "status.json")
        return {
            "job": str(self.job),
            "exists": (self.job / "brief.md").is_file() or (self.job / "status.json").is_file(),
            "request": self.request_text(),
            "recipe": recipe,
            "title": (info or {}).get("title"),
            "summary": (info or {}).get("summary"),
            "schema": schema,
            "params": self.current_params(recipe, schema),
            "status": status if isinstance(status, dict) else {},
            "check": read_json(self.job / "build" / "check.json"),
            "part": self.part_info(),
            "building": building,
            "error": error,
            "version": version,
        }

    # builds
    def queue(self, params: dict[str, str] | None = None, reset: bool = False) -> None:
        """Ask for a build. While one runs, later requests merge into the one build that follows it."""
        with self.cond:
            if reset:
                self.pending = {"reset": True, "params": {}}
            elif self.pending is None:
                self.pending = {"reset": False, "params": dict(params or {})}
            else:
                self.pending["params"].update(params or {})
            self.version += 1
            self.cond.notify_all()

    def _builder(self) -> None:
        while True:
            with self.cond:
                self.cond.wait_for(lambda: self.stopped or self.pending is not None)
                if self.stopped:
                    return
                job, self.pending = self.pending, None
                self.running = True
                self.error = None
                self.version += 1
                self.cond.notify_all()
            try:
                error = self._build(job)
            except Exception as e:  # keep the builder alive whatever stone does
                error = f"build failed: {type(e).__name__}: {e}"
            with self.cond:
                self.running = False
                self.error = error
                self.builds += 1
                self.version += 1
                self.cond.notify_all()

    def _build(self, job: dict) -> str | None:
        recipe = self.recipe()
        jobarg = ("--job", str(self.job))
        if job["reset"] or job["params"]:
            args = ["set", *jobarg]
            if job["reset"]:
                args.append("--reset")
            if recipe:
                args.append(recipe)
            args += [f"{k}={v}" for k, v in job["params"].items()]
            code, out = self.run_stone(*args)
            if code != 0:
                return tail(out)
        code, out = self.run_stone("build", *jobarg)
        return tail(out) if code != 0 else None

    def threemf_bytes(self) -> bytes | None:
        f = self.job / "build" / "part.stl"
        try:
            st = f.stat()
            key = (st.st_mtime_ns, st.st_size)
            if self._3mf_cache and self._3mf_cache[0] == key:
                return self._3mf_cache[1]
            data = threemf.stl_to_3mf(f.read_bytes(), title=self.recipe() or "part")
        except (OSError, threemf.StlProblem):
            return None
        self._3mf_cache = (key, data)
        return data


def printers() -> list[dict]:
    """Configured printers from gantrel-printer, read only. Any problem reads as none configured."""
    try:
        from gantrel_printer import config as pconfig

        return [{"name": c.name, "kind": c.type} for c in pconfig.load_all().values()]
    except Exception:  # a broken printer config must not take the viewport down
        return []


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, studio: Studio):
        self.studio = studio
        super().__init__(addr, Handler)

    @property
    def port(self) -> int:
        return self.server_address[1]

    def server_close(self):
        self.studio.stop()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server: StudioServer
    server_version = "GantrelStudio"
    sys_version = ""

    # logging: off, so the token in a query string never reaches a log
    def log_message(self, format, *args):  # noqa: A002
        return

    # helpers
    @property
    def studio(self) -> Studio:
        return self.server.studio

    def _allowed_hosts(self) -> set[str]:
        port = self.server.port
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, data) -> None:
        self._send(code, json.dumps(data).encode(), "application/json")

    def _fail(self, code: int, message: str) -> None:
        self._json(code, {"error": message})

    def _guard(self) -> str | None:
        """Host check for everything, token check for /api/*. Returns the path, or None after a 403."""
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self._allowed_hosts():
            self._fail(403, "forbidden host")
            return None
        parts = urlsplit(self.path)
        if parts.path.startswith("/api/"):
            given = self.headers.get("X-Gantrel-Token") or (parse_qs(parts.query).get("t") or [""])[0]
            if not given or not hmac.compare_digest(given.encode(), self.studio.launch_id.encode()):
                self._fail(403, "missing or wrong token")
                return None
        return parts.path

    # GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self._guard()
        if path is None:
            return
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        routes = {
            "/api/state": lambda: self._json(200, self.studio.state()),
            "/api/part.stl": lambda: self._file(self.studio.job / "build" / "part.stl", "model/stl"),
            "/api/preview.png": lambda: self._file(self.studio.job / "build" / "preview.png", "image/png"),
            "/api/events": self._events,
            "/api/export/part.stl": lambda: self._export("stl"),
            "/api/export/part.3mf": lambda: self._export("3mf"),
            "/api/printers": lambda: self._json(200, printers()),
        }
        fn = routes.get(path)
        if fn is None:
            return self._fail(404, "not found")
        fn()

    def _static(self, rel: str) -> None:
        rel = unquote(rel)
        if "\\" in rel or "\x00" in rel or any(seg in ("..", "") for seg in rel.split("/")):
            return self._fail(404, "not found")
        root = WEB_ROOT.resolve()
        f = (root / rel).resolve()
        if not f.is_relative_to(root) or not f.is_file():
            if rel == "index.html":
                return self._send(200, PLACEHOLDER, CONTENT_TYPES[".html"])
            return self._fail(404, "not found")
        ctype = CONTENT_TYPES.get(f.suffix.lower()) or mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        self._send(200, f.read_bytes(), ctype)

    def _file(self, f: Path, ctype: str, extra: dict | None = None) -> None:
        try:
            data = f.read_bytes()
        except OSError:
            return self._fail(404, f"no {f.name} yet")
        self._send(200, data, ctype, extra)

    def _export(self, kind: str) -> None:
        name = re.sub(r"[^A-Za-z0-9_-]", "", self.studio.recipe() or "") or "part"
        disp = {"Content-Disposition": f'attachment; filename="{name}.{kind}"'}
        if kind == "stl":
            return self._file(self.studio.job / "build" / "part.stl", "model/stl", disp)
        data = self.studio.threemf_bytes()
        if data is None:
            return self._fail(404, "no part to export yet")
        self._send(200, data, "model/3mf", disp)

    def _events(self) -> None:
        st = self.studio
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        with st.cond:
            st.sse_clients += 1
        last = None
        next_ping = time.monotonic() + st.keepalive
        try:
            while True:
                with st.cond:
                    st.cond.wait_for(lambda: st.stopped or st.version != last,
                                     timeout=max(0.0, next_ping - time.monotonic()))
                    if st.stopped:
                        return
                    changed = st.version != last
                if changed:
                    state = st.state()
                    last = state["version"]
                    self.wfile.write(b"event: state\ndata: " + json.dumps(state).encode() + b"\n\n")
                    next_ping = time.monotonic() + st.keepalive
                elif time.monotonic() >= next_ping:
                    self.wfile.write(b": keepalive\n\n")
                    next_ping = time.monotonic() + st.keepalive
                self.wfile.flush()
        except OSError:  # the page closed: broken pipe or reset
            return
        finally:
            with st.cond:
                st.sse_clients -= 1

    # POST
    def do_POST(self):
        path = self._guard()
        if path is None:
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin.strip().lower() not in {f"http://{h}" for h in self._allowed_hosts()}:
            return self._fail(403, "forbidden origin")
        if path == "/api/params":
            return self._post_params()
        if path == "/api/reset":
            if not self.studio.recipe():
                return self._fail(409, "no recipe chosen yet; pick one in chat first")
            self.studio.queue(reset=True)
            return self._json(202, self.studio.state())
        self._fail(404, "not found")

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return None

    def _post_params(self) -> None:
        body = self._body()
        if not isinstance(body, dict):
            return self._fail(400, 'send JSON like {"params": {"size": 60}}')
        recipe = self.studio.recipe()
        if not recipe:
            return self._fail(409, "no recipe chosen yet; pick one in chat first")
        info = self.studio.recipe_info(recipe)
        if info is None:
            return self._fail(503, f"could not read the {recipe} schema from stone show --json")
        try:
            checked = paramcheck.validate(info["params"], body.get("params"))
        except paramcheck.ParamProblem as e:
            return self._fail(400, str(e))
        self.studio.queue(params=checked)
        self._json(202, self.studio.state())


def bind(job: Path, host: str = "127.0.0.1", port: int = 7342, tries: int = 11, stone: str | None = None) -> StudioServer:
    """Bind the first free port from port to port + tries - 1 (port 0 asks the OS)."""
    studio = Studio(job, stone=stone)
    last: OSError | None = None
    for p in ([0] if port == 0 else range(port, port + tries)):
        try:
            return StudioServer((host, p), studio)
        except OSError as e:
            last = e
    raise OSError(f"no free port in {port}..{port + tries - 1}: {last}")
