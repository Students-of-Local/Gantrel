"""Failure guard: ask a local vision model whether a camera frame shows a failed print.

The model is any OpenAI-compatible /v1/chat/completions endpoint on this machine or the local
network (default: Ollama at http://127.0.0.1:11434/v1). Frames are pictures of the person's home, so
they are never sent to an address outside the local network. Gantrel stores no credential for it; a
server that wants a bearer credential gets it from the GANTREL_VISION_BEARER environment variable at
run time.

The guard only judges frames. The monitor decides what to do with the verdicts: it pauses after
`consecutive` failed frames in a row while the printer is printing, and never resumes, starts or
cancels anything. Anything that is not a clean answer (bad JSON, an unknown verdict, an unreachable
model) counts as suspect, never failed.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config, net
from .errors import ConfigError, PrinterError
from .model import Status, printable

DEFAULT_URL = "http://127.0.0.1:11434/v1"
# Measured on 2026-11-17 against tests/printers/frames (docs/PRINTERS.md, "Failure guard"). No local
# model passed the gate (80% right on failed vs not failed, no good print judged failed), so the guard
# runs only when the person asks for it. The default is the model that never judged a good frame failed:
# a wrong pause stops a good print, a miss leaves things as they were without the guard.
DEFAULT_MODEL = "gemma3:4b"
EXPERIMENTAL = ("The failure guard is experimental: no local vision model Gantrel measured caught enough "
                "failed prints to trust (docs/PRINTERS.md), so keep checking the camera yourself.")
DEFAULT_CONSECUTIVE = 2
DEFAULT_TIMEOUT = 180.0
BEARER_ENV = "GANTREL_VISION_BEARER"
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


_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def parse_verdict(text: str) -> tuple[str, str, str]:
    """(verdict, reason, source). Anything but a JSON object with a known verdict is suspect."""
    cleaned = _FENCE.sub("", (text or "").strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return "suspect", "the model's answer was not JSON", "malformed"
    try:
        data = json.loads(cleaned[start:end + 1])
    except ValueError:
        return "suspect", "the model's answer was not valid JSON", "malformed"
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
                          "Camera frames show the person's home, so Gantrel sends them only to a model on their "
                          "own network, such as Ollama at http://127.0.0.1:11434/v1")


class VisionGuard:
    def __init__(self, url: str = DEFAULT_URL, model: str = DEFAULT_MODEL, *, timeout: float = DEFAULT_TIMEOUT,
                 consecutive: int = DEFAULT_CONSECUTIVE):
        if consecutive < 1:
            raise ConfigError("--consecutive must be 1 or more")
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.consecutive = consecutive
        self._json_mode = True

    @classmethod
    def from_settings(cls, url: str | None = None, model: str | None = None,
                      consecutive: int | None = None) -> "VisionGuard":
        v = config.load_watch_settings().get("vision", {})
        return cls(url or v.get("url") or DEFAULT_URL, model or v.get("model") or DEFAULT_MODEL,
                   timeout=float(v.get("timeout_s") or DEFAULT_TIMEOUT),
                   consecutive=int(consecutive if consecutive is not None else
                                   v.get("consecutive") or DEFAULT_CONSECUTIVE))

    def check(self) -> None:
        """Before a watch: the URL is local, the server answers, and it lists the model if it lists any."""
        check_local(self.url)
        what = f"the vision model server at {self.url}"
        try:
            _, _, data = net.request("GET", self.url + "/models", headers=self._headers(), timeout=10, what=what)
        except PrinterError as err:
            raise ConfigError(f"{what} did not answer ({err.message})",
                              "Start Ollama (ollama serve) or the model server, or set the URL with "
                              "gantrel-printer watch-config --vision-url, or watch without --guard") from None
        try:
            names = {str(m.get("id", "")) for m in json.loads(data).get("data", [])}
        except (ValueError, AttributeError, TypeError):
            return  # a server that doesn't list models in the usual shape; the first frame will tell
        if names and self.model not in names and f"{self.model}:latest" not in names:
            raise ConfigError(f"{what} has no model named {self.model}",
                              f"Get it with: ollama pull {self.model}. Or pick one it has: "
                              + ", ".join(sorted(names)[:8]))

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        bearer = os.environ.get(BEARER_ENV)
        if bearer:
            h["Authorization"] = "Bearer " + bearer
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
        what = f"the vision model at {self.url}"
        try:
            _, _, data = net.request("POST", self.url + "/chat/completions", headers=self._headers(),
                                     body=json.dumps(payload).encode(), timeout=self.timeout, what=what)
        except PrinterError as err:
            # Some servers (LM Studio) reject json_object; ask once more without it.
            if self._json_mode and "HTTP 400" in str(err):
                self._json_mode = False
                return self._ask(image, kind, st)
            raise
        reply = net.parse_json(data, what)
        try:
            return str(reply["choices"][0]["message"].get("content") or "")
        except (KeyError, IndexError, TypeError, AttributeError):
            return ""

    def classify(self, frame: Path, st: Status | None = None) -> Verdict:
        data = Path(frame).read_bytes()
        kind = net.is_image(data) or "jpeg"
        t0 = time.monotonic()
        try:
            text = self._ask(data, kind, st)
        except PrinterError as err:
            return Verdict("suspect", f"no answer from the vision model ({err.message})", "unreachable",
                           round(time.monotonic() - t0, 2))
        verdict, reason, source = parse_verdict(text)
        return Verdict(verdict, reason, source, round(time.monotonic() - t0, 2), printable(text)[:500])
