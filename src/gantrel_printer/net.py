"""Small stdlib HTTP client for LAN printers.

Never follows redirects: a printer address can move on DHCP, and a redirect could point a write at
some other device or a loopback service.
"""

from __future__ import annotations

import http.client
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


def _digest_opener(url: str, user: str, password: str):
    """An opener that answers HTTP digest challenges, only for URLs on url's own scheme, host and port."""
    u = urllib.parse.urlsplit(url)
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, f"{u.scheme}://{u.netloc}/", user, password)
    return urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}),
                                       urllib.request.HTTPDigestAuthHandler(mgr))


def request(method: str, url: str, *, headers: dict | None = None, body: bytes | None = None,
            timeout: float = 10.0, what: str = "the printer",
            digest: tuple[str, str] | None = None) -> tuple[int, dict, bytes]:
    """One HTTP request. digest=(user, password) answers a digest challenge from that same origin only."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    opener = _digest_opener(url, *digest) if digest else _opener
    try:
        with opener.open(req, timeout=timeout) as reply:
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
    except http.client.HTTPException as err:
        raise ProtocolError(f"{what} did not answer in HTTP at {url.split('?')[0]} ({err.__class__.__name__})",
                            "Check the port; this may be a different service") from None


def challenge(url: str, timeout: float = 2.0) -> str:
    """The WWW-Authenticate header of a 401 reply to a plain GET, or "". Sends no credentials."""
    try:
        with _opener.open(urllib.request.Request(url, method="GET"), timeout=timeout):
            return ""
    except urllib.error.HTTPError as err:
        return str(err.headers.get("WWW-Authenticate") or "") if err.code == 401 else ""
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError):
        return ""


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


def mjpeg_frame(url: str, *, timeout: float = 15.0, what: str = "the camera",
                max_bytes: int = 8 * 1024 * 1024) -> bytes:
    """One JPEG frame from an MJPEG stream (multipart/x-mixed-replace) or a plain JPEG URL.

    Reads only until the first complete frame, so an endless stream never hangs the call.
    """
    req = urllib.request.Request(url, method="GET")
    try:
        with _opener.open(req, timeout=timeout) as reply:
            buf = bytearray()
            while len(buf) < max_bytes:
                chunk = reply.read1(65536) if hasattr(reply, "read1") else reply.read(65536)
                if not chunk:
                    break
                buf += chunk
                start = buf.find(b"\xff\xd8\xff")
                if start >= 0:
                    end = buf.find(b"\xff\xd9", start + 3)
                    if end >= 0:
                        return bytes(buf[start:end + 2])
    except urllib.error.HTTPError as err:
        raise Unreachable(f"{what} refused the stream (HTTP {err.code})",
                          "Check the camera is on; some printers turn it off when idle") from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as err:
        reason = getattr(err, "reason", err)
        raise Unreachable(f"{what} did not answer at {url.split('?')[0]} ({reason})",
                          "Check the printer is on and its camera is turned on") from None
    raise ProtocolError(f"{what} sent no complete JPEG frame")


def is_image(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return None


_CGNAT = None


def is_local_address(addr: str) -> bool:
    """Loopback, private (RFC 1918, ULA), link-local, or the 100.64.0.0/10 range Tailscale uses."""
    import ipaddress

    global _CGNAT
    if _CGNAT is None:
        _CGNAT = ipaddress.ip_network("100.64.0.0/10")
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local or (ip.version == 4 and ip in _CGNAT)


def host_is_local(url: str, timeout: float = 3.0) -> bool:
    """True when every address the URL's host resolves to is on this machine or the local network."""
    host = urllib.parse.urlsplit(url).hostname or ""
    if not host:
        return False
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return False
    finally:
        socket.setdefaulttimeout(old)
    addrs = {i[4][0] for i in infos}
    return bool(addrs) and all(is_local_address(a) for a in addrs)
