"""Print notices: an ntfy topic or a plain JSON webhook, set by the person with watch-config.

The URL lives in the OS keyring, not in a file, because webhook URLs often carry a token, and no
error or log line ever shows more of it than scheme://host:port. Gantrel ships no default target.
The last camera frame goes with a notice only when the target is on the local network, or when the
person turned attach_frame on: a public ntfy topic can be read by anyone who knows its name, and a
frame is a picture of their home. Each send looks the host up once, decides from that answer whether
the frame may go, and connects to that same address.
"""

from __future__ import annotations

import base64
import datetime
import json
import urllib.parse
from pathlib import Path

from . import config, net
from .errors import ConfigError, PrinterError
from .model import printable

EVENTS = {
    "finished": ("Print finished", "3", "white_check_mark"),
    "ended": ("Print ended", "3", "information_source"),
    "failed": ("Print failed", "4", "x"),
    "cancelled": ("Print cancelled", "3", "stop_sign"),
    "paused_by_guard": ("Gantrel paused the print", "5", "warning"),
    "pause_failed": ("Gantrel could not pause the print", "5", "rotating_light"),
    "lost": ("Lost contact with the printer", "4", "question"),
}
KINDS = ("ntfy", "webhook")


def guess_kind(url: str) -> str:
    return "ntfy" if "ntfy" in (urllib.parse.urlsplit(url).hostname or "") else "webhook"


def _header(text: str) -> str:
    """HTTP headers are Latin-1; ntfy reads RFC 2047 encoded words for anything else."""
    text = printable(text)
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(text.encode()).decode() + "?="


def shown(url: str) -> str:
    """The URL without its path or query, which may hold a token."""
    u = urllib.parse.urlsplit(url)
    return f"{u.scheme}://{u.hostname or ''}" + (f":{u.port}" if u.port else "") + "/..."


class Notifier:
    def __init__(self, url: str, kind: str = "auto", attach_frame: bool | None = None):
        if not url.startswith(("http://", "https://")):
            raise ConfigError("the notice URL must start with http:// or https://")
        self.url = url
        self.kind = guess_kind(url) if kind in ("", "auto", None) else kind
        if self.kind not in KINDS:
            raise ConfigError(f"unknown notice kind {kind!r}", "Use ntfy or webhook")
        self.attach_frame = attach_frame  # None: only to a local address, decided on each send

    @classmethod
    def from_settings(cls) -> "Notifier":
        url = config.get_secret(config.NOTIFY_KEY)
        if not url:
            raise ConfigError("no notice target is set",
                              "Set one with: gantrel-printer watch-config --notify-url-stdin (paste the ntfy topic "
                              "or webhook URL), or run watch without --notify")
        n = config.load_watch_settings().get("notify", {})
        return cls(url, n.get("kind") or "auto", n.get("attach_frame"))

    def send(self, event: str, printer: str, message: str, frame: Path | None = None, file: str = "") -> None:
        what = f"the notice target {shown(self.url)}"
        try:
            self._send(event, printer, message, frame, file, what)
        except PrinterError:
            raise
        except Exception as err:  # noqa: BLE001  any other error text could echo the URL
            raise PrinterError(f"{what} could not be sent to ({err.__class__.__name__})") from None

    def _send(self, event, printer, message, frame, file, what) -> None:
        title, priority, tag = EVENTS[event]
        title = f"{title}: {printer}"
        dest = net.resolve(self.url, what=what, hide_url=True)  # one lookup: the frame check and the send agree
        allowed = self.attach_frame if self.attach_frame is not None else dest.local
        frame = frame if (frame and allowed and Path(frame).is_file()) else None
        kw = {"timeout": 30, "what": what, "hide_url": True, "dest": dest}
        if self.kind == "ntfy":
            headers = {"Title": _header(title), "Priority": priority, "Tags": tag}
            if frame:
                headers.update({"Filename": Path(frame).name, "Message": _header(message)})
                net.request("PUT", self.url, headers=headers, body=Path(frame).read_bytes(), **kw)
            else:
                net.request("POST", self.url, headers=headers, body=printable(message).encode(), **kw)
            return
        payload = {"event": event, "title": title, "message": printable(message), "printer": printer,
                   "file": printable(file), "time": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")}
        if frame:
            data = Path(frame).read_bytes()
            payload["frame"] = {"name": Path(frame).name, "content_type": f"image/{net.is_image(data) or 'jpeg'}",
                                "base64": base64.b64encode(data).decode()}
        # Webhooks answer with anything (often plain "ok"), so the reply body is not parsed.
        net.request("POST", self.url, headers={"Content-Type": "application/json"},
                    body=json.dumps(payload).encode(), **kw)

    def _scrub(self, text: str) -> str:
        """Belt and braces: no part of the URL past its host ever reaches a log line."""
        u = urllib.parse.urlsplit(self.url)
        for part in sorted({self.url, u.path, u.query, u.netloc.rpartition("@")[0], u.username or "",
                            u.password or ""} | set(p for p in u.path.split("/") if len(p) >= 4)
                           | set(q for q in urllib.parse.unquote(u.query).replace("&", "=").split("=") if len(q) >= 4),
                           key=len, reverse=True):
            if part and part not in ("/", "http", "https"):
                text = text.replace(part, "...")
        return text

    def try_send(self, *args, log=print, **kw) -> bool:
        """send(), but a failed notice never stops the watch; it is logged instead, with the URL hidden."""
        try:
            self.send(*args, **kw)
            return True
        except (PrinterError, OSError) as err:
            log(self._scrub(f"notice not sent: {err}"))
            return False
