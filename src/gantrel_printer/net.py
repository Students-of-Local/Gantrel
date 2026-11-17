"""Small stdlib HTTP client for LAN printers.

Never follows redirects: a printer address can move on DHCP, and a redirect could point a write at
some other device or a loopback service.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .errors import AuthFailed, NotFound, ProtocolError, Refused, Unreachable

MAX_REPLY = 32 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))


def request(method: str, url: str, *, headers: dict | None = None, body: bytes | None = None,
            timeout: float = 10.0, what: str = "the printer") -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with _opener.open(req, timeout=timeout) as reply:
            data = reply.read(MAX_REPLY + 1)
            if len(data) > MAX_REPLY:
                raise ProtocolError(f"{what} sent a reply that is too large")
            return reply.status, dict(reply.headers), data
    except urllib.error.HTTPError as err:
        data = err.read(65536) if err.fp else b""
        if err.code in (401, 403):
            raise AuthFailed(f"{what} refused the credentials (HTTP {err.code})",
                             "Pair the printer again with the right API key or access code") from None
        if 300 <= err.code < 400:
            raise ProtocolError(f"{what} tried to redirect to another address",
                                "Check the printer's address; redirects are never followed") from None
        if err.code == 404:
            raise NotFound(f"{what} has no such item (HTTP 404)") from None
        detail = data.decode("utf-8", "replace").strip()[:200]
        raise Refused(f"{what} refused it (HTTP {err.code})" + (f": {detail}" if detail else "")) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as err:
        reason = getattr(err, "reason", err)
        raise Unreachable(f"{what} did not answer at {url.split('?')[0]} ({reason})",
                          "Check it is on, on the same network, and the address is right") from None


def get_json(url: str, **kw) -> dict:
    _, _, data = request("GET", url, **kw)
    return parse_json(data, kw.get("what", "the printer"))


def send_json(method: str, url: str, payload: dict | None = None, *, headers: dict | None = None, **kw) -> dict:
    h = {"Content-Type": "application/json", **(headers or {})}
    body = json.dumps(payload or {}).encode()
    _, _, data = request(method, url, headers=h, body=body, **kw)
    return parse_json(data, kw.get("what", "the printer")) if data.strip() else {}


def parse_json(data: bytes, what: str = "the printer") -> dict:
    try:
        out = json.loads(data)
    except ValueError:
        raise ProtocolError(f"{what} sent a reply that is not JSON") from None
    if not isinstance(out, dict):
        raise ProtocolError(f"{what} sent an unexpected reply")
    return out


def multipart(fields: dict[str, str], file_field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "gantrel" + uuid.uuid4().hex
    out = bytearray()
    for name, value in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode()
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode()
    out += content + f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def same_origin(url: str, scheme: str, host: str, port: int) -> bool:
    """True when url points at exactly scheme://host:port (default ports filled in)."""
    u = urllib.parse.urlsplit(url)
    default = {"http": 80, "https": 443}.get(u.scheme)
    try:
        uport = u.port or default
    except ValueError:
        return False
    return u.scheme == scheme and (u.hostname or "").lower() == host.lower() and uport == port


def is_image(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return None
