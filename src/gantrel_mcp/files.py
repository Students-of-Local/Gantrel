"""Rules for every file path a tool takes from the model.

A path must name an existing regular file whose real location (symlinks followed) is inside the
person's home folder or the server's working folder, and it must be under the size cap for its kind.
A symlink inside home that points out of it is refused, because its real location is outside.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from gantrel_printer.errors import ConfigError
from gantrel_printer.model import printable

MB = 1024 * 1024
LIMITS = {"model": 200 * MB, "gcode": 500 * MB, "design": 1 * MB}
NAMES = {"model": "model", "gcode": "sliced file", "design": "design"}


def roots() -> list[Path]:
    """Where tool files may live: home and the working folder, never the filesystem root itself."""
    out = []
    for p in (Path.home(), Path.cwd()):
        try:
            real = Path(os.path.realpath(p))
        except OSError:
            continue
        if real != Path(real.anchor) and real not in out:
            out.append(real)
    return out


def shown(arg) -> str:
    return printable(str(arg))[:160]


def checked(arg, kind: str) -> Path:
    """The real path of a file the model named, or ConfigError with a plain sentence."""
    if kind not in LIMITS:
        raise ValueError(kind)
    what = NAMES[kind]
    if not isinstance(arg, str) or not arg.strip() or "\x00" in arg:
        raise ConfigError(f"the {what} path is empty or not a path", "Pass the full path to the file")
    p = Path(arg).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    real = Path(os.path.realpath(p))
    allowed = roots()
    if not any(real == r or real.is_relative_to(r) for r in allowed):
        raise ConfigError(f"{shown(arg)} is outside your home folder and the working folder",
                          "Gantrel reads only files under your home folder or the folder it runs in; "
                          "a link that points outside them is refused too")
    try:
        st = os.stat(real)
    except FileNotFoundError:
        raise ConfigError(f"no file at {shown(arg)}", "Check the path") from None
    except OSError as err:
        raise ConfigError(f"can't read {shown(arg)} ({err.strerror})") from None
    if not stat.S_ISREG(st.st_mode):
        raise ConfigError(f"{shown(arg)} is not a regular file", "Pass a file, not a folder or a device")
    if st.st_size > LIMITS[kind]:
        raise ConfigError(f"{shown(arg)} is {st.st_size / MB:.0f} MB, over Gantrel's {LIMITS[kind] // MB} MB "
                          f"limit for a {what}")
    return real
