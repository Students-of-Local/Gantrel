"""Failure guard: ask a local vision model whether a camera frame shows a failed print.

All compute goes through Fonsviva (AGENTS.md): the model runs behind Fonsviva's OpenAI-compatible
/v1/chat/completions endpoint, by default http://127.0.0.1:7341/v1, and Gantrel sends its own
Fonsviva app key as the bearer. The key lives in the OS keyring (watch-config --fonsviva-key-stdin),
never in a file or a log. Frames are pictures of the person's home, so every request resolves the
host once, checks each address is on this machine or the local network, and connects to that
address (no second DNS lookup can send a frame elsewhere). Each verdict records where it ran.

The guard only judges frames. The monitor decides what to do with the verdicts: it pauses after
`consecutive` failed frames in a row while the printer is printing, and never resumes, starts or
cancels anything. Anything that is not a clean answer (bad JSON, an unknown verdict, an unreachable
model) counts as suspect, never failed.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config, net
from .errors import AuthFailed, ConfigError, PrinterError
from .model import Status, printable

DEFAULT_URL = "http://127.0.0.1:7341/v1"  # Fonsviva's OpenAI-compatible API on this machine
# Measured on 2026-11-17 against tests/printers/frames (docs/PRINTERS.md, "Failure guard"). No local
# model passed the gate (80% right on failed vs not failed, no good print judged failed), so the guard
# runs only when the person asks for it. The default is the model that never judged a good frame failed:
# a wrong pause stops a good print, a miss leaves things as they were without the guard.
DEFAULT_MODEL = "gemma3:4b"
EXPERIMENTAL = ("The failure guard is experimental: no local vision model Gantrel measured caught enough "
                "failed prints to trust (docs/PRINTERS.md), so keep checking the camera yourself.")
DEFAULT_CONSECUTIVE = 2
DEFAULT_TIMEOUT = 180.0
FONSVIVA_HINT = ("Check Fonsviva with: fonsviva status (start it with fonsviva start). Then run the watch again, "
                 "or run it without --guard")
VERDICTS = ("ok", "suspect", "failed")

PROMPT = """You are checking one camera picture from inside a 3D printer.
{status}
A normal print is a solid object with smooth sides on the bed, with the nozzle on top of it.
These are print failures:
- spaghetti: loose, tangled strands of plastic instead of a solid part
- the part has come loose from the bed, fallen over, moved, or is stuck to the nozzle
- a big blob of plastic stuck around the nozzle
- layer shift: the upper layers are offset sideways from the lower ones
- nozzle clog or no extrusion: the print stopped growing, or thin broken lines
- an empty bed even though the print is well under way
First say in one short sentence what you actually see on the bed. Then choose the verdict:
- "failed" only when you clearly see one of these failures in the picture
- "suspect" when something might be wrong, or the picture is too dark or blurry to tell
- "ok" when the print looks normal
Answer with JSON only, no other text:
{{"reason": "what you see", "verdict": "ok" or "suspect" or "failed"}}"""


@dataclass
class Verdict:
    verdict: str          # ok, suspect or failed
    reason: str
    source: str           # model, malformed or unreachable
    latency_s: float = 0.0
    raw: str = ""         # the model's answer, shortened, for the person to read
    ran_on: dict = field(default_factory=dict)  # where the model ran: endpoint, model asked, model served

    def to_dict(self) -> dict:
        return asdict(self)


def status_line(st: Status | None) -> str:
    if st is None:
        return "The printer is printing."
    bits = [f"The printer reports: {st.state}"]
    if st.progress is not None:
        bits.append(f"{st.progress * 100:.0f}% done")
    if st.layer is not None and st.total_layers:
        bits.append(f"layer {st.layer} of {st.total_layers}")
    return printable(", ".join(bits)) + "."


_FENCE = re.compile(r"\A```[a-zA-Z]*[ \t]*\n(.*)\n[ \t]*```\Z", re.S)


def parse_verdict(text: str) -> tuple[str, str, str]:
    """(verdict, reason, source). Anything but a JSON object with a known verdict is suspect.

    One complete Markdown code fence around the answer is allowed; nothing else is. The whole answer
    must be the object: an array, or prose around an object, is malformed, so it can never pause.
    """
    cleaned = (text or "").strip()
    fenced = _FENCE.match(cleaned)
    if fenced:
        cleaned = fenced.group(1).strip()
    if not cleaned:
        return "suspect", "the model's answer was empty", "malformed"
    try:
        data = json.loads(cleaned)
    except ValueError:
        return "suspect", "the model's answer was not JSON", "malformed"
    if not isinstance(data, dict):
        return "suspect", "the model's answer was not a JSON object", "malformed"
    verdict = data.get("verdict")
    if not isinstance(verdict, str) or verdict.strip().lower() not in VERDICTS:
        return "suspect", "the model gave no known verdict", "malformed"
    reason = data.get("reason", "")
    reason = printable(reason if isinstance(reason, str) else json.dumps(reason))[:300]
    return verdict.strip().lower(), reason, "model"


def check_local(url: str) -> None:
    if not url.startswith(("http://", "https://")):
        raise ConfigError(f"vision URL {url!r} must start with http:// or https://")
    if not net.host_is_local(url):
        raise ConfigError("the vision model URL is not on this machine or the local network",
                          "Camera frames show the person's home, so Gantrel sends them only to Fonsviva on their "
                          f"own network, by default {DEFAULT_URL}")


class VisionGuard:
    """direct=True is for benchmarks/guard_vision.py only: a model server with no Fonsviva key."""

    def __init__(self, url: str = DEFAULT_URL, model: str = DEFAULT_MODEL, *, timeout: float = DEFAULT_TIMEOUT,
                 consecutive: int = DEFAULT_CONSECUTIVE, key: str | None = None, direct: bool = False):
        if consecutive < 1:
            raise ConfigError("--consecutive must be 1 or more")
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.consecutive = consecutive
        self.key = key
        self.direct = direct
        self._json_mode = True

    @property
    def host(self) -> str:
        """Where inference runs, as host:port (no path, no key)."""
        return net.origin(self.url).split("://", 1)[-1]

    @classmethod
    def from_settings(cls, url: str | None = None, model: str | None = None,
                      consecutive: int | None = None) -> "VisionGuard":
        v = config.load_watch_settings().get("vision", {})
        return cls(url or v.get("url") or DEFAULT_URL, model or v.get("model") or DEFAULT_MODEL,
                   timeout=float(v.get("timeout_s") or DEFAULT_TIMEOUT),
                   consecutive=int(consecutive if consecutive is not None else
                                   v.get("consecutive") or DEFAULT_CONSECUTIVE),
                   key=config.get_secret(config.FONSVIVA_KEY))

    def check(self) -> None:
        """Before a watch: the URL is local, the key is set, Fonsviva answers, and it lists the model."""
        check_local(self.url)
        if not self.direct and not self.key:
            raise ConfigError("no Fonsviva key for Gantrel is saved, so the failure guard cannot run",
                              "Check Fonsviva with: fonsviva status and fonsviva keys list (gantrel must be active), "
                              "then save Gantrel's key with: gantrel-printer watch-config --fonsviva-key-stdin. "
                              "Or run the watch without --guard")
        what = f"{'the vision model server' if self.direct else 'Fonsviva'} at {self.host}"
        try:
            _, _, data = net.request("GET", self.url + "/models", headers=self._headers(), timeout=10, what=what,
                                     local_only=True, hide_url=True)
        except AuthFailed:
            raise ConfigError(f"{what} refused Gantrel's key",
                              "Check it with: fonsviva status and fonsviva keys list, then save the key again with: "
                              "gantrel-printer watch-config --fonsviva-key-stdin. Or run the watch without --guard"
                              ) from None
        except PrinterError as err:
            raise ConfigError(f"{what} did not answer ({self._scrub(err.message)})", FONSVIVA_HINT) from None
        try:
            names = {str(m.get("id", "")) for m in json.loads(data).get("data", [])}
        except (ValueError, AttributeError, TypeError):
            return  # a server that doesn't list models in the usual shape; the first frame will tell
        if names and self.model not in names and f"{self.model}:latest" not in names:
            raise ConfigError(f"{what} has no model named {self.model}",
                              "See the models Gantrel may use with: fonsviva models list --app gantrel. "
                              "Or pick one it has: " + ", ".join(sorted(names)[:8]))

    def _scrub(self, text: str) -> str:
        """Never keep Gantrel's Fonsviva key in anything logged or saved, even if a server echoes it."""
        # A real key is long; a short test key would also eat ordinary words.
        return text.replace(self.key, "[key]") if self.key and len(self.key) >= 8 else text

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.key:
            h["Authorization"] = "Bearer " + self.key
        return h

    def _ask(self, image: bytes, kind: str, st: Status | None) -> str:
        payload = {
            "model": self.model,
            "temperature": 0,
            # Room for models that reason before they answer (Qwen3-VL ran out at 200 and sent no JSON).
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT.format(status=status_line(st))},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/{kind};base64," + base64.b64encode(image).decode()}},
            ]}],
        }
        if self._json_mode:
            payload["response_format"] = {"type": "json_object"}
        what = f"the vision model at {self.host}"
        try:
            # local_only: one DNS lookup per frame, checked, and the frame goes to that address only.
            _, headers, data = net.request("POST", self.url + "/chat/completions", headers=self._headers(),
                                           body=json.dumps(payload).encode(), timeout=self.timeout, what=what,
                                           local_only=True, hide_url=True)
        except PrinterError as err:
            # Some servers (LM Studio) reject json_object; ask once more without it.
            if self._json_mode and "HTTP 400" in str(err):
                self._json_mode = False
                return self._ask(image, kind, st)
            raise
        reply = net.parse_json(data, what)
        self._served = {k.lower(): self._scrub(printable(str(v)))[:120] for k, v in headers.items()
                        if k.lower().startswith("x-fonsviva-")}
        if isinstance(reply.get("model"), str):
            self._served["model"] = self._scrub(printable(reply["model"]))[:120]
        try:
            return str(reply["choices"][0]["message"].get("content") or "")
        except (KeyError, IndexError, TypeError, AttributeError):
            return ""

    def ran_on(self) -> dict:
        out = {"via": "direct" if self.direct else "fonsviva", "host": self.host, "model": self.model}
        served = getattr(self, "_served", None)
        if served:
            out["served"] = dict(served)
        return out

    def classify(self, frame: Path, st: Status | None = None) -> Verdict:
        data = Path(frame).read_bytes()
        kind = net.is_image(data) or "jpeg"
        t0 = time.monotonic()
        self._served = {}
        try:
            text = self._ask(data, kind, st)
        except PrinterError as err:
            return Verdict("suspect", f"no answer from the vision model ({self._scrub(err.message)})", "unreachable",
                           round(time.monotonic() - t0, 2), ran_on=self.ran_on())
        verdict, reason, source = parse_verdict(text)
        reason, text = self._scrub(reason), self._scrub(text)
        return Verdict(verdict, reason, source, round(time.monotonic() - t0, 2), printable(text)[:500],
                       ran_on=self.ran_on())
