"""Optional second opinion from Claude on camera frames.

The local detector only knows "spaghetti-like texture". Claude can also judge
things it can't: a part knocked off the plate, a nozzle printing in the air (the
part stops growing while the layer count climbs), a blob on the nozzle, a first
layer that isn't sticking. It's used three ways:

  * confirm: before acting on a local-model failure, to cut false positives;
  * periodic: every few minutes, comparing now with a frame from earlier;
  * first layers: an adhesion check at chosen early layers.

Every call is rate limited and its estimated cost is tracked. Without Anthropic
credentials the judge is simply off and the local detector works alone.
"""

from __future__ import annotations

import base64
import collections
import json
import logging
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import cv2

from print_guard.config import JudgeConfig
from print_guard.credentials import resolve_anthropic_key
from print_guard.vision.model import decode_jpeg, encode_jpeg

log = logging.getLogger(__name__)

# USD per million tokens (input, output), for the running cost estimate shown in the dashboard.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.0, 50.0),
}
# Models that accept the server-side refusal fallback ("default" form).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-opus-5"}

FAILURE_TYPES = [
    "none",
    "spaghetti",
    "detached_or_moved_part",
    "not_extruding",
    "blob_on_nozzle",
    "first_layer_adhesion",
    "warping_or_lifting",
    "support_failure",
    "layer_shift",
    "other",
]
UNRECOVERABLE_TYPES = {"detached_or_moved_part", "layer_shift"}

SCHEMA = {
    "type": "object",
    "properties": {
        "image_usable": {"type": "boolean"},
        "failing": {"type": "boolean"},
        "failure_type": {"type": "string", "enum": FAILURE_TYPES},
        "confidence": {"type": "number"},
        "unrecoverable": {"type": "boolean"},
        "evidence": {"type": "string"},
    },
    "required": ["image_usable", "failing", "failure_type", "confidence", "unrecoverable", "evidence"],
    "additionalProperties": False,
}

SYSTEM = """You inspect camera frames from inside an enclosed Bambu Lab P1S 3D printer and decide \
whether the print is failing. A wrong "failing" verdict pauses a print that may have hours left, and a \
missed failure wastes filament and can damage the printer, so be calibrated.

About the camera: it is mounted high in the front-left corner of the chamber and looks down at the \
build plate at an angle, with a wide-angle lens, modest resolution and warm LED light. The toolhead \
moves constantly and often hides part of the print. Purged filament waste near the back of the \
chamber, thin wisps of stringing, support structures, and the textured plate pattern are normal.

Failing means at least one of:
- spaghetti: loose, tangled strands of filament piling up or hanging from the nozzle;
- detached_or_moved_part: the part is knocked over, shifted, or lying loose on the plate;
- not_extruding: the layer count advanced but the part did not visibly grow (nozzle printing in air);
- blob_on_nozzle: a large lump of plastic stuck around the nozzle or toolhead;
- first_layer_adhesion: first-layer lines lifting, curling, dragged around or not sticking;
- warping_or_lifting: corners or edges clearly lifting off the plate;
- support_failure: supports collapsed or knocked down so overhangs print into air;
- layer_shift: upper layers visibly offset from lower ones.

Rules:
- If you cannot judge the print (too dark, blurry, view blocked, plate not visible), set image_usable \
false, failing false, failure_type "none", confidence 0.
- confidence is your probability (0 to 1) that the print is failing in the way you name. Use 0.9 or \
more only when the failure is unmistakable.
- unrecoverable is true when resuming cannot save the print (the part moved or detached, a layer \
shift, or a mess that has already ruined the part).
- evidence: one or two short sentences naming what you see and where."""


@dataclass
class JudgeRequest:
    reason: str  # "confirm", "periodic", "first_layer"
    current_jpeg: bytes
    reference_jpeg: bytes | None = None
    reference_minutes_ago: float | None = None
    context: dict = field(default_factory=dict)


@dataclass
class Verdict:
    image_usable: bool
    failing: bool
    failure_type: str
    confidence: float
    unrecoverable: bool
    evidence: str
    reason: str
    model: str
    seconds: float
    cost_usd: float

    @property
    def summary(self) -> str:
        state = "FAILING" if self.failing else "ok"
        if not self.image_usable:
            state = "cannot see the print"
        return f"{state} ({self.failure_type}, {self.confidence:.0%}): {self.evidence}"


class JudgeUnavailable(RuntimeError):
    pass


def credentials_available() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    if resolve_anthropic_key():
        return True
    profile_dir = Path(os.environ.get("ANTHROPIC_CONFIG_DIR", Path.home() / ".config" / "anthropic"))
    return profile_dir.exists() and any(profile_dir.iterdir())


def _prepare_image(jpeg: bytes, max_side: int = 1280) -> str:
    """Downscale large frames (cost and latency) and return base64 JPEG."""
    image = decode_jpeg(jpeg)
    h, w = image.shape[:2]
    if max(h, w) > max_side:
        scale = max_side / max(h, w)
        image = cv2.resize(image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        jpeg = encode_jpeg(image, 85)
    return base64.standard_b64encode(jpeg).decode("ascii")


def build_content(req: JudgeRequest) -> list[dict]:
    ctx = req.context
    lines = []
    if ctx.get("job"):
        lines.append(f"Print job: {ctx['job']}")
    if ctx.get("layer") is not None:
        lines.append(f"Printer reports layer {ctx['layer']} of {ctx.get('total_layers', '?')}, "
                     f"{ctx.get('percent', '?')}% done.")
    reason_text = {
        "confirm": f"A local detector flagged possible spaghetti (score {ctx.get('score', '?')}). "
                   "Confirm or reject it.",
        "periodic": "Routine check during the print.",
        "first_layer": "Early-layer check: look closely at first-layer adhesion.",
    }.get(req.reason, req.reason)
    lines.append(reason_text)

    content: list[dict] = []
    if req.reference_jpeg:
        ref_layer = ctx.get("reference_layer")
        label = f"Frame A: about {req.reference_minutes_ago:.0f} minutes ago"
        if ref_layer is not None:
            label += f" (layer {ref_layer})"
        content.append({"type": "text", "text": label + "."})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                     "data": _prepare_image(req.reference_jpeg)}})
        content.append({"type": "text", "text": "Frame B: now."})
        lines.append("Compare A and B: the part should have grown between them if layers advanced.")
    content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": _prepare_image(req.current_jpeg)}})
    content.append({"type": "text", "text": "\n".join(lines)})
    return content


class ClaudeJudge:
    def __init__(self, cfg: JudgeConfig) -> None:
        self.cfg = cfg
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="judge")
        self._calls: collections.deque[float] = collections.deque()
        self._lock = threading.Lock()
        self._client = None
        self.disabled_reason = ""
        self.total_cost = 0.0
        self.total_calls = 0
        self.last_verdict: Verdict | None = None

    @property
    def enabled(self) -> bool:
        return not self.disabled_reason

    def _get_client(self):
        if self._client is None:
            import anthropic

            key = resolve_anthropic_key()
            self._client = anthropic.Anthropic(api_key=key, max_retries=2) if key else \
                anthropic.Anthropic(max_retries=2)
        return self._client

    def calls_last_hour(self) -> int:
        cutoff = time.time() - 3600
        with self._lock:
            while self._calls and self._calls[0] < cutoff:
                self._calls.popleft()
            return len(self._calls)

    def submit(self, req: JudgeRequest) -> Future:
        return self._executor.submit(self.judge, req)

    def judge(self, req: JudgeRequest) -> Verdict:
        if not self.enabled:
            raise JudgeUnavailable(self.disabled_reason)
        if self.calls_last_hour() >= self.cfg.max_calls_per_hour:
            raise JudgeUnavailable(f"rate limit of {self.cfg.max_calls_per_hour} calls per hour reached")
        import anthropic

        with self._lock:
            self._calls.append(time.time())
        started = time.monotonic()
        kwargs: dict = {}
        if self.cfg.model in FALLBACK_MODELS:
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        try:
            response = self._get_client().beta.messages.create(
                model=self.cfg.model,
                max_tokens=16000,
                system=SYSTEM,
                output_config={"effort": self.cfg.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{"role": "user", "content": build_content(req)}],
                timeout=self.cfg.timeout_seconds,
                **kwargs,
            )
        except anthropic.AuthenticationError as e:
            self.disabled_reason = "Anthropic credentials were rejected"
            raise JudgeUnavailable(self.disabled_reason) from e
        except anthropic.PermissionDeniedError as e:
            self.disabled_reason = "the API key lacks permission for this model"
            raise JudgeUnavailable(self.disabled_reason) from e
        except anthropic.NotFoundError as e:
            self.disabled_reason = f"model {self.cfg.model!r} not found"
            raise JudgeUnavailable(self.disabled_reason) from e
        except anthropic.RateLimitError as e:
            raise JudgeUnavailable("Anthropic rate limit hit") from e
        except anthropic.APIStatusError as e:
            raise JudgeUnavailable(f"Anthropic API error {e.status_code}") from e
        except anthropic.APIConnectionError as e:
            raise JudgeUnavailable("cannot reach the Anthropic API") from e

        seconds = time.monotonic() - started
        cost = self._cost(response)
        with self._lock:
            self.total_cost += cost
            self.total_calls += 1
        if response.stop_reason == "refusal":
            raise JudgeUnavailable("Claude declined to judge this frame")
        if response.stop_reason == "max_tokens":
            raise JudgeUnavailable("verdict was cut off (max_tokens)")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise JudgeUnavailable("verdict was not valid JSON") from e
        failure_type = data.get("failure_type") if data.get("failure_type") in FAILURE_TYPES else "other"
        verdict = Verdict(
            image_usable=bool(data.get("image_usable")),
            failing=bool(data.get("failing")) and bool(data.get("image_usable")),
            failure_type=failure_type,
            confidence=max(0.0, min(1.0, float(data.get("confidence") or 0.0))),
            unrecoverable=bool(data.get("unrecoverable")) or failure_type in UNRECOVERABLE_TYPES,
            evidence=str(data.get("evidence") or "")[:400],
            reason=req.reason,
            model=getattr(response, "model", self.cfg.model),
            seconds=seconds,
            cost_usd=cost,
        )
        self.last_verdict = verdict
        return verdict

    def _cost(self, response) -> float:
        price = PRICES.get(self.cfg.model)
        usage = getattr(response, "usage", None)
        if not price or usage is None:
            return 0.0
        inp = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        inp += 0.1 * (getattr(usage, "cache_read_input_tokens", 0) or 0)
        return inp * price[0] / 1e6 + (usage.output_tokens or 0) * price[1] / 1e6

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
