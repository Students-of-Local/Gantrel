"""Helpers shared by the studio tests: an HTTP client that sets Host and the token, and a server starter."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

from gantrel_studio import server


class Client:
    def __init__(self, httpd):
        self.httpd = httpd
        self.port = httpd.port
        self.key = httpd.studio.launch_id

    def request(self, method, path, body=None, headers=None, token=True, host=None):
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            h["X-Gantrel-Token"] = self.key
        h.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        payload = r.read()
        conn.close()
        return r.status, dict(r.getheaders()), payload

    def json(self, method, path, body=None, **kw):
        code, headers, payload = self.request(method, path, body, **kw)
        return code, json.loads(payload) if payload else None


def start(job_dir: Path) -> server.StudioServer:
    httpd = server.bind(job_dir, port=0)
    httpd.studio.poll = 0.05
    httpd.studio.start()
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return httpd
