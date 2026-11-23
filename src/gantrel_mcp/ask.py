"""Ask the person a question through Local Studio's ask socket (local-studio docs/plugins.md,
"Questions from plugins"), and return the person's answer, never the model's.

Before anything is sent, the process at the other end of the socket must be LOCAL_STUDIO_ASK_PID:
SO_PEERCRED on Linux, LOCAL_PEERPID on macOS. On a mismatch the connection closes with nothing sent.
The wait is capped at ASK_TIMEOUT_S, under the 60 s an MCP call gets; closing the connection on a
timeout withdraws the card.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import time

ENV = ("LOCAL_STUDIO_ASK", "LOCAL_STUDIO_ASK_TOKEN", "LOCAL_STUDIO_ASK_PID")
ASK_TIMEOUT_S = 55.0
CONNECT_TIMEOUT_S = 5.0
REPLY_MAX = 64 * 1024


def in_local_studio() -> bool:
    path, token, pid = (os.environ.get(n) for n in ENV)
    return bool(path and token and pid and pid.isdigit())


def peer_pid(conn: socket.socket) -> int | None:
    if sys.platform == "darwin":
        return struct.unpack("i", conn.getsockopt(0, 2, 4))[0]  # SOL_LOCAL, LOCAL_PEERPID
    if sys.platform.startswith("linux"):
        return struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
    return None


def ask(text: str, choices: list[str], timeout: float | None = None) -> dict:
    """{"status": "answered", "answer": ...}, {"status": "cancelled"}, {"status": "timeout"} or
    {"status": "refused", "error": ...}. The ask token is sent only to a socket Local Studio serves."""
    path, token, pid = (os.environ.get(n) for n in ENV)
    if not (path and token and pid and pid.isdigit()):
        return {"status": "refused", "error": "not running under Local Studio"}
    wait = ASK_TIMEOUT_S if timeout is None else timeout
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.settimeout(CONNECT_TIMEOUT_S)
        conn.connect(path)
        peer = peer_pid(conn)
        if peer is None:
            return {"status": "refused", "error": "Gantrel can't check who serves the question socket on this system"}
        if peer != int(pid):
            return {"status": "refused", "error": "the question socket is not served by Local Studio"}
        request = {"token": token, "text": text, "choices": choices}
        thread = os.environ.get("LOCAL_STUDIO_THREAD")
        if thread:
            request["thread"] = thread
        conn.sendall((json.dumps(request) + "\n").encode())
        deadline = time.monotonic() + wait
        reply = b""
        while not reply.endswith(b"\n"):
            left = deadline - time.monotonic()
            if left <= 0:
                return {"status": "timeout"}
            conn.settimeout(left)
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                return {"status": "timeout"}
            if not chunk:
                return {"status": "cancelled"}
            reply += chunk
            if len(reply) > REPLY_MAX:
                return {"status": "refused", "error": "Local Studio's reply was too long"}
        try:
            data = json.loads(reply)
        except ValueError:
            return {"status": "refused", "error": "Local Studio's reply was not JSON"}
        return data if isinstance(data, dict) else {"status": "refused", "error": "Local Studio's reply was not an object"}
    except OSError as err:
        return {"status": "refused", "error": f"could not reach Local Studio's question socket ({err.strerror or err})"}
    finally:
        conn.close()
