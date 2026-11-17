"""Repetier-Server through its REST API (https://www.repetier-server.com/manuals/programming/API/index.html).

The API key goes in the x-api-key header (never the URL), only to the server. Repetier-Server can
run several printers; the one to use is its slug, saved at pair time (--serial picks one when the
server has more than one). Uploads go to the model store (/printer/model/<slug>?a=upload), which
neither queues nor starts a print.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path

from .. import net
from ..errors import AuthFailed, ConfigError, ProtocolError, Refused
from ..model import Camera, Status
from .base import Adapter


class RepetierAdapter(Adapter):
    type_name = "repetier"
    label = "Repetier-Server"
    default_port = 3344
    needs_secret = True
    secret_label = "Repetier-Server API key (web page: Global Settings, Connectivity, API key)"

    @property
    def base(self) -> str:
        return f"http://{self.cfg.host}:{self.port}"

    def _h(self) -> dict:
        return {"x-api-key": self.secret or ""}

    @staticmethod
    def _check(reply, what: str):
        if isinstance(reply, dict) and "error" in reply:
            err = str(reply["error"])
            if "auth" in err.lower() or "permission" in err.lower():
                raise AuthFailed(f"{what} refused the API key ({err[:80]})", "Pair again with the right API key")
            raise Refused(f"{what} refused it ({err[:120]})")
        return reply

    def _api(self, action: str, data: dict | None = None, slug: str | None = None):
        q = {"a": action}
        if data is not None:
            q["data"] = net_json(data)
        url = f"{self.base}/printer/api/{urllib.parse.quote(slug or self.slug)}?" + urllib.parse.urlencode(q)
        _, _, body = net.request("GET", url, headers=self._h(), what=self.cfg.name)
        return self._check(_loads(body, self.cfg.name), self.cfg.name)

    @property
    def slug(self) -> str:
        if self.cfg.serial:
            return self.cfg.serial
        printers = self._printers()
        if len(printers) != 1:
            raise ConfigError(f"{self.cfg.name} runs {len(printers)} printers",
                              "Pair again with --serial set to one of: " + ", ".join(p.get("slug", "") for p in printers))
        self.cfg.serial = str(printers[0].get("slug", ""))
        return self.cfg.serial

    def _printers(self) -> list:
        _, _, body = net.request("GET", f"{self.base}/printer/list", headers=self._h(), what=self.cfg.name)
        r = self._check(_loads(body, self.cfg.name), self.cfg.name)
        data = r.get("data") if isinstance(r, dict) else r
        if not isinstance(data, list):
            raise ProtocolError(f"{self.cfg.name} sent an unexpected printer list")
        return data

    # ------------------------------------------------------------ contract

    @classmethod
    def probe(cls, host, port=None, timeout=2.0):
        """/printer/info answers without a key. Its reply can hold the API key; that is never kept or shown."""
        port = port or cls.default_port
        try:
            info = net.get_json(f"http://{host}:{port}/printer/info", timeout=timeout)
        except Exception:
            return None
        printers = info.get("printers")
        if isinstance(printers, list) and "version" in info:
            slugs = ", ".join(str(p.get("slug", "")) for p in printers if isinstance(p, dict))
            return {"type": cls.type_name, "host": host, "port": port,
                    "detail": f"Repetier-Server {info.get('version', '')}, printers: {slugs or 'none'}"}
        return None

    def pair(self):
        st = self.status()
        return {"state": st.state, "slug": self.slug}

    def status(self) -> Status:
        me = next((p for p in self._printers() if p.get("slug") == self.slug), None)
        if me is None:
            raise ConfigError(f"{self.cfg.name} has no printer {self.slug!r}", "Pair again with the right --serial")
        state_all = self._api("stateList")
        st = (state_all.get(self.slug) if isinstance(state_all, dict) else None) or {}
        job = str(me.get("job") or "none")
        if not me.get("online"):
            state = "offline"
        elif job != "none":
            state = "paused" if me.get("paused") else "printing"
        else:
            state = "idle"
        ext = (st.get("extruder") or [{}])[0] or {}
        beds = st.get("heatedBeds") or ([st["heatedBed"]] if isinstance(st.get("heatedBed"), dict) else [{}])
        chambers = st.get("heatedChambers") or [{}]
        busy = state in ("printing", "paused")
        done = me.get("done")
        return Status(
            printer=self.cfg.name, type=self.type_name, state=state, file=job if busy else "",
            progress=done / 100 if busy and isinstance(done, (int, float)) else None,
            nozzle_c=ext.get("tempRead"), nozzle_target_c=ext.get("tempSet"),
            bed_c=(beds[0] or {}).get("tempRead"), bed_target_c=(beds[0] or {}).get("tempSet"),
            chamber_c=(chambers[0] or {}).get("tempRead"),
            elapsed_s=me.get("printedTimeComp") if busy else None, layer=st.get("layer") if busy else None,
        )

    def upload(self, local_path: Path, remote_name: str) -> str:
        body, ctype = net.multipart({}, "filename", remote_name, Path(local_path).read_bytes())
        url = f"{self.base}/printer/model/{urllib.parse.quote(self.slug)}?" + urllib.parse.urlencode(
            {"a": "upload", "name": remote_name})
        _, _, reply = net.request("POST", url, body=body, timeout=300, headers={"Content-Type": ctype, **self._h()},
                                  what=self.cfg.name)
        if reply.strip():
            self._check(_loads(reply, self.cfg.name), self.cfg.name)
        return remote_name

    def _model(self, remote_name: str) -> dict | None:
        stem = remote_name.rsplit(".", 1)[0]
        models = (self._api("listModels") or {}).get("data") or []
        hits = [m for m in models if m.get("name") in (remote_name, stem)]
        if len(hits) > 1:
            raise Refused(f"{self.cfg.name} holds {len(hits)} files named {remote_name}",
                          "Delete the extra copies in Repetier-Server, then try again")
        return hits[0] if hits else None

    def remote_info(self, remote_name: str) -> dict | None:
        m = self._model(remote_name)
        return None if m is None else {"size": m.get("length"), "mtime": m.get("created")}

    def _start(self, remote_name: str) -> None:
        m = self._model(remote_name)
        if m is None:
            raise Refused(f"{remote_name} is not on {self.cfg.name}")
        self._api("copyModel", {"id": m["id"], "autostart": True})

    def _resume(self) -> None:
        self._api("continueJob")

    def pause(self) -> None:
        self._api("send", {"cmd": "@pause"})

    def cancel(self) -> None:
        self._api("stopJob")

    def emergency_stop(self) -> str:
        self._api("send", {"cmd": "M112"})
        return ("Sent M112 emergency stop through Repetier-Server: the printer's heaters and motors are off. "
                "Reset or power cycle the printer before using it again")

    def web_ui(self) -> str:
        return f"{self.base}/ (Repetier-Server)"

    def camera(self) -> Camera:
        if self.cfg.camera_url:
            return Camera(self.cfg.camera_url, self.cfg.stream_url)
        try:
            cam = (self._api("getPrinterConfig", {"printer": self.slug}) or {}).get("webcam") or {}
        except Exception:
            cam = {}
        snap = urllib.parse.urljoin(self.base + "/", str(cam.get("staticUrl") or "")) if cam.get("staticUrl") else ""
        stream = urllib.parse.urljoin(self.base + "/", str(cam.get("dynamicUrl") or "")) if cam.get("dynamicUrl") else ""
        return Camera(snap, self.cfg.stream_url or stream)

    def camera_headers(self) -> dict:
        return self._h()  # base.snapshot sends these only when the camera is on the server itself


def net_json(data: dict) -> str:
    import json

    return json.dumps(data, separators=(",", ":"))


def _loads(body: bytes, what: str):
    import json

    try:
        return json.loads(body) if body.strip() else {}
    except ValueError:
        raise ProtocolError(f"{what} sent a reply that is not JSON") from None
