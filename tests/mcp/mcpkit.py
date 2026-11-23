"""Helpers for the MCP tests: drive the real server through the SDK's in-process client, and a fake
Local Studio ask socket (a Unix socket server in a thread of this test process)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import tempfile
import threading

from mcp import Client

from gantrel_mcp.server import build


class Result:
    def __init__(self, raw):
        self.raw = raw
        self.is_error = bool(raw.is_error)
        self.text = "\n".join(getattr(c, "text", "") for c in raw.content)

    @property
    def data(self) -> dict:
        assert not self.is_error, self.text
        return json.loads(self.text)


def call(name: str, args: dict | None = None) -> Result:
    async def go():
        async with Client(build()) as c:
            return await c.call_tool(name, args or {})
    return Result(asyncio.run(go()))


def tool_names() -> list[str]:
    async def go():
        async with Client(build()) as c:
            return [t.name for t in (await c.list_tools()).tools]
    return asyncio.run(go())


TOKEN = "ab" * 32


class FakeAsk:
    """Local Studio's ask socket, as docs/plugins.md describes it.

    reply: the JSON reply to send, or None to never reply (hold until the client closes).
    on_question(request): runs after the question arrives and before the reply.
    """

    def __init__(self, reply: dict | None = None, on_question=None):
        self.reply = reply
        self.on_question = on_question
        self.dir = tempfile.mkdtemp(prefix="ga")  # short: a Unix socket path must fit in 108 bytes
        self.path = os.path.join(self.dir, "ask.sock")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        self.sock.listen(4)
        self.connections = 0
        self.received: list[bytes] = []
        self.questions: list[dict] = []
        self.withdrawn = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            self.received.append(data)
            if not data.endswith(b"\n"):
                self.withdrawn.set()
                return
            req = json.loads(data)
            self.questions.append(req)
            if self.on_question:
                self.on_question(req)
            if self.reply is None:
                if conn.recv(1) == b"":
                    self.withdrawn.set()
                return
            conn.sendall((json.dumps(self.reply) + "\n").encode())

    def env(self, monkeypatch, pid: int | None = None, caller: str | None = "local:1000"):
        monkeypatch.setenv("LOCAL_STUDIO_ASK", self.path)
        monkeypatch.setenv("LOCAL_STUDIO_ASK_TOKEN", TOKEN)
        monkeypatch.setenv("LOCAL_STUDIO_ASK_PID", str(os.getpid() if pid is None else pid))
        monkeypatch.setenv("LOCAL_STUDIO_THREAD", "thread-1")
        if caller:
            monkeypatch.setenv("LOCAL_STUDIO_CALLER", caller)
        return self

    def close(self):
        self.sock.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def answered(answer: str) -> dict:
    return {"status": "answered", "answer": answer}
