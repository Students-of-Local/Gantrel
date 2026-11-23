"""One start, resume or upload at a time on one printer, across every Gantrel MCP server on this computer.

Local Studio starts one server per thread, so a lock inside one process is not enough. Each paired
printer has a lock file in Gantrel's state folder (config.state_dir()/locks/<name>.lock), and the
holder keeps an fcntl.flock on it. flock belongs to the open file, so two processes, or two calls in
one process that each open the file, exclude each other. The kernel drops it when the holder's
process exits, so a server that crashes never leaves a printer locked.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import time
from pathlib import Path

from gantrel_printer import config
from gantrel_printer.errors import ConfigError

POLL_S = 0.05


class Busy(Exception):
    """The printer's lock was not free in time."""


def path(printer: str) -> Path:
    if not isinstance(printer, str) or not config.NAME_RE.match(printer):
        raise ConfigError("not a printer name")
    d = config.state_dir() / "locks"
    d.mkdir(mode=0o700, exist_ok=True)
    return d / f"{printer}.lock"


def acquire(printer: str, wait_s: float = 0.0) -> int:
    """Take printer's lock, waiting up to wait_s for it. Returns the descriptor to pass to release.
    Raises Busy when another holder keeps it past wait_s."""
    fd = os.open(path(printer), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    end = time.monotonic() + max(0.0, wait_s)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                left = end - time.monotonic()
                if left <= 0:
                    raise Busy(printer) from None
                time.sleep(min(POLL_S, left))
    except BaseException:
        os.close(fd)
        raise


def release(fd: int) -> None:
    os.close(fd)  # closing the only descriptor releases the flock


@contextlib.contextmanager
def held(printer: str, wait_s: float = 0.0):
    """Hold printer's lock for the with block. Waits up to wait_s for it, then raises Busy."""
    fd = acquire(printer, wait_s)
    try:
        yield
    finally:
        release(fd)
