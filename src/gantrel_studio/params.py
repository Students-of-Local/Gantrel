"""Check viewport edits against the recipe schema before anything runs.

The schema is the recipe.json param list that `stone show <recipe> --json` prints. Each value comes back
as the text `stone set` takes after `name=`, so stone only ever sees values that already passed here.
"""

from __future__ import annotations

import math

MAX_TEXT = 200
MAX_PARAMS = 100
NUMBER_TYPES = ("number", "int")
TEXT_TYPES = ("text", "string")


class ParamProblem(ValueError):
    """A value the studio will not pass to stone. The message is safe to show the person."""


def _has_control(s: str) -> bool:
    return any(ord(c) < 32 or 127 <= ord(c) < 160 or c in "  " for c in s)


def _number_text(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else repr(float(v))


def check_one(p: dict, value) -> str:
    name, kind = p.get("name"), p.get("type", "number")
    if kind in NUMBER_TYPES:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParamProblem(f"{name} needs a number")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ParamProblem(f"{name} needs a finite number")
        if kind == "int" and not float(value).is_integer():
            raise ParamProblem(f"{name} needs a whole number")
        lo, hi = p.get("min"), p.get("max")
        if lo is not None and value < lo:
            raise ParamProblem(f"{name}={value} is below the range {lo}..{hi}")
        if hi is not None and value > hi:
            raise ParamProblem(f"{name}={value} is above the range {lo}..{hi}")
        return _number_text(value)
    if kind == "choice":
        choices = [str(c) for c in p.get("choices") or []]
        if not isinstance(value, (str, int, float)) or isinstance(value, bool) or str(value) not in choices:
            raise ParamProblem(f"{name} must be one of: {', '.join(choices)}")
        return str(value)
    if kind == "bool":
        if not isinstance(value, bool):
            raise ParamProblem(f"{name} needs true or false")
        return "true" if value else "false"
    if kind in TEXT_TYPES:
        if not isinstance(value, str):
            raise ParamProblem(f"{name} needs text")
        if len(value) > MAX_TEXT:
            raise ParamProblem(f"{name} is longer than {MAX_TEXT} characters")
        if _has_control(value):
            raise ParamProblem(f"{name} has a control character")
        return value
    # image parameters name files on disk: those are set from chat with stone image, never from the page
    raise ParamProblem(f"{name} ({kind}) can't be changed in the studio; ask in chat")


def validate(schema: list[dict], given) -> dict[str, str]:
    """{name: value} from the page -> {name: text for stone set}. Raises ParamProblem."""
    if not isinstance(given, dict):
        raise ParamProblem("params must be an object of name: value")
    if len(given) > MAX_PARAMS:
        raise ParamProblem("too many params")
    by_name = {p.get("name"): p for p in schema if isinstance(p, dict)}
    out = {}
    for name, value in given.items():
        p = by_name.get(name)
        if p is None:
            raise ParamProblem(f"no parameter {name!r} in this recipe")
        out[name] = check_one(p, value)
    return out
