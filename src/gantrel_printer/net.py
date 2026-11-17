"""Small stdlib HTTP client for LAN printers.

Never follows redirects: a printer address can move on DHCP, and a redirect could point a write at
some other device or a loopback service.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .errors import AuthFailed, NotFound, NotLocal, PrinterError, ProtocolError, Refused, Unreachable

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


def origin(url: str) -> str:
    """scheme://host:port only: no user, password, path or query, any of which may hold a token."""
    u = urllib.parse.urlsplit(url)
    host = u.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    try:
        port = u.port
    except ValueError:
        port = None
    return f"{u.scheme}://{host}" + (f":{port}" if port else "")


def _where(url: str, hide_url: bool) -> str:
    return origin(url) if hide_url else url.split("?")[0]


def _why(reason, hide_url: bool) -> str:
    """The error text, or only its kind when the URL is secret (an error's text can echo the URL)."""
    return reason.__class__.__name__ if hide_url and not isinstance(reason, str) else (
        "error" if hide_url else str(reason))


def _status_error(code: int, data: bytes, what: str, hide_url: bool) -> PrinterError:
    if code in (401, 403):
        return AuthFailed(f"{what} refused the credentials (HTTP {code})",
                          "Pair the printer again with the right API key or access code")
    if 300 <= code < 400:
        return ProtocolError(f"{what} tried to redirect to another address",
                             "Check the printer's address; redirects are never followed")
    if code == 404:
        return NotFound(f"{what} has no such item (HTTP 404)")
    detail = "" if hide_url else data.decode("utf-8", "replace").strip()[:200]
    return Refused(f"{what} refused it (HTTP {code})" + (f": {detail}" if detail else ""))


def request(method: str, url: str, *, headers: dict | None = None, body: bytes | None = None,
            timeout: float = 10.0, what: str = "the printer",
            digest: tuple[str, str] | None = None, hide_url: bool = False,
            dest: "Destination | None" = None, local_only: bool = False,
            context: ssl.SSLContext | None = None) -> tuple[int, dict, bytes]:
    """One HTTP request. digest=(user, password) answers a digest challenge from that same origin only.

    hide_url keeps the URL's path, query and user out of every error (notice URLs carry tokens).
    local_only resolves the host once, refuses unless every address is on this machine or the local
    network, then connects to that address (see resolve); dest does the same with a lookup already made.
    """
    if local_only and dest is None:
        dest = resolve(url, what=what, hide_url=hide_url)
        if not dest.local:
            raise NotLocal(f"{what} is not on this machine or the local network",
                           "Camera frames and keys go only to an address on the person's own network")
    if dest is not None:
        return _pinned_request(method, url, dest, headers=headers or {}, body=body, timeout=timeout, what=what,
                               hide_url=hide_url, context=context)
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
        raise _status_error(err.code, data, what, hide_url) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError, ValueError) as err:
        reason = getattr(err, "reason", err)
        raise Unreachable(f"{what} did not answer at {_where(url, hide_url)} ({_why(reason, hide_url)})",
                          "Check it is on, on the same network, and the address is right") from None
    except http.client.HTTPException as err:
        raise ProtocolError(f"{what} did not answer in HTTP at {_where(url, hide_url)} ({err.__class__.__name__})",
                            "Check the port; this may be a different service") from None


class Destination:
    """One DNS answer for a URL's host, checked once, so the connection goes where the check looked."""

    def __init__(self, host: str, port: int, infos: list):
        self.host, self.port, self.infos = host, port, infos
        self.local = bool(infos) and all(is_local_address(i[4][0]) for i in infos)


def resolve(url: str, *, what: str = "the server", hide_url: bool = False) -> Destination:
    u = urllib.parse.urlsplit(url)
    host = u.hostname or ""
    try:
        port = u.port or {"http": 80, "https": 443}[u.scheme]
    except (ValueError, KeyError):
        raise Unreachable(f"{what} has an address that is not a valid http or https URL") from None
    if not host:
        raise Unreachable(f"{what} has no host in its address")
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except (OSError, UnicodeError) as err:
        raise Unreachable(f"{what} did not answer at {_where(url, hide_url)} ({_why(err, hide_url)})",
                          "Check it is on, on the same network, and the address is right") from None
    return Destination(host, port, infos)


def _connect(dest: Destination, timeout: float) -> socket.socket:
    last: OSError | None = None
    for family, type_, proto, _, sockaddr in dest.infos:
        sock = socket.socket(family, type_, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(sockaddr)
            return sock
        except OSError as err:
            sock.close()
            last = err
    raise last or OSError("no address to connect to")


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, dest: Destination, timeout: float):
        super().__init__(dest.host, dest.port, timeout=timeout)
        self._dest = dest

    def connect(self):
        self.sock = _connect(self._dest, self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, dest: Destination, timeout: float, context: ssl.SSLContext | None):
        self._ctx = context or ssl.create_default_context()
        super().__init__(dest.host, dest.port, timeout=timeout, context=self._ctx)
        self._dest = dest

    def connect(self):
        # The certificate is checked against the URL's host name, not the pinned address.
        self.sock = self._ctx.wrap_socket(_connect(self._dest, self.timeout), server_hostname=self._dest.host)


def _pinned_request(method: str, url: str, dest: Destination, *, headers: dict, body: bytes | None, timeout: float,
                    what: str, hide_url: bool, context: ssl.SSLContext | None) -> tuple[int, dict, bytes]:
    u = urllib.parse.urlsplit(url)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    conn = _PinnedHTTPS(dest, timeout, context) if u.scheme == "https" else _PinnedHTTP(dest, timeout)
    try:
        conn.request(method, path, body=body, headers=headers)
        reply = conn.getresponse()
        data = reply.read(MAX_REPLY + 1)
        if len(data) > MAX_REPLY:
            raise ProtocolError(f"{what} sent a reply that is too large")
        if reply.status >= 300:
            raise _status_error(reply.status, data[:65536], what, hide_url)
        return reply.status, dict(reply.headers), data
    except (socket.timeout, TimeoutError, ConnectionError, OSError) as err:
        raise Unreachable(f"{what} did not answer at {_where(url, hide_url)} ({_why(err, hide_url)})",
                          "Check it is on, on the same network, and the address is right") from None
    except http.client.HTTPException as err:
        raise ProtocolError(f"{what} did not answer in HTTP at {_where(url, hide_url)} ({err.__class__.__name__})",
                            "Check the port; this may be a different service") from None
    finally:
        conn.close()


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
