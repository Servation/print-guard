"""The real detector on simulator frames, and the Claude judge's request/verdict handling."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from print_guard.config import JudgeConfig
from print_guard.sim import frames
from print_guard.vision import judge as judge_mod
from print_guard.vision.judge import ClaudeJudge, JudgeRequest, JudgeUnavailable, build_content
from print_guard.vision.model import Detection, FailureDetector, decode_jpeg, in_roi, nms

MODEL = Path(__file__).resolve().parents[1] / "models" / "obico-failure-detector.onnx"
needs_model = pytest.mark.skipif(not MODEL.exists(), reason="run `print-guard download-model` first")


@needs_model
def test_detector_separates_clean_and_spaghetti_frames():
    det = FailureDetector(MODEL)
    clean = det.detect(decode_jpeg(frames.render(0.4, 1.0)))
    messy = det.detect(decode_jpeg(frames.render(0.4, 1.0, failure_age=20)))
    assert sum(d.confidence for d in clean) < 0.2
    assert sum(d.confidence for d in messy) > 1.0


def test_nms_and_roi():
    boxes = np.array([[0.1, 0.1, 0.3, 0.3], [0.11, 0.11, 0.31, 0.31], [0.6, 0.6, 0.8, 0.8]], np.float32)
    scores = np.array([0.5, 0.4, 0.3], np.float32)
    assert sorted(nms(boxes, scores, 0.45)) == [0, 2]
    d = Detection(0.5, (0.1, 0.1, 0.3, 0.3))
    assert in_roi(d, [0.0, 0.0, 0.5, 0.5]) and not in_roi(d, [0.5, 0.5, 1.0, 1.0])


def test_request_includes_reference_frame_and_context():
    jpeg = frames.render(0.3, 0.0)
    content = build_content(JudgeRequest("periodic", jpeg, jpeg, 10.0,
                                         context={"job": "mask", "layer": 80, "total_layers": 400, "percent": 20,
                                                  "reference_layer": 50}))
    kinds = [c["type"] for c in content]
    assert kinds == ["text", "image", "text", "image", "text"]
    assert "layer 50" in content[0]["text"]
    assert "layer 80 of 400" in content[-1]["text"]


class _FakeMessages:
    def __init__(self, payload: dict | None = None, stop_reason: str = "end_turn") -> None:
        self.payload, self.stop_reason, self.calls = payload, stop_reason, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason=self.stop_reason, model=kwargs["model"],
            content=[SimpleNamespace(type="thinking", thinking=""),
                     SimpleNamespace(type="text", text=json.dumps(self.payload or {}))],
            usage=SimpleNamespace(input_tokens=3000, output_tokens=500, cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0),
        )


def _judge_with(messages: _FakeMessages, **cfg) -> ClaudeJudge:
    j = ClaudeJudge(JudgeConfig(**cfg))
    j._client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return j


def test_verdict_parsing_cost_and_request_shape():
    msgs = _FakeMessages({"image_usable": True, "failing": True, "failure_type": "detached_or_moved_part",
                          "confidence": 0.97, "unrecoverable": False, "evidence": "part lying on its side"})
    j = _judge_with(msgs)
    v = j.judge(JudgeRequest("periodic", frames.render(0.3, 0.0)))
    assert v.failing and v.unrecoverable  # detached parts can't be resumed
    assert v.cost_usd == pytest.approx(3000 * 4 / 1e6 + 500 * 20 / 1e6)
    call = msgs.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert "temperature" not in call and "thinking" not in call


def test_unusable_image_is_never_failing():
    j = _judge_with(_FakeMessages({"image_usable": False, "failing": True, "failure_type": "spaghetti",
                                   "confidence": 0.9, "unrecoverable": False, "evidence": "dark"}))
    assert not j.judge(JudgeRequest("periodic", frames.render(0.3, 0.0))).failing


def test_refusal_and_rate_limit_raise_unavailable():
    j = _judge_with(_FakeMessages({}, stop_reason="refusal"), max_calls_per_hour=1)
    with pytest.raises(JudgeUnavailable, match="declined"):
        j.judge(JudgeRequest("periodic", frames.render(0.3, 0.0)))
    with pytest.raises(JudgeUnavailable, match="rate limit"):
        j.judge(JudgeRequest("periodic", frames.render(0.3, 0.0)))


def test_haiku_gets_no_fallback_parameter():
    msgs = _FakeMessages({"image_usable": True, "failing": False, "failure_type": "none", "confidence": 0.1,
                          "unrecoverable": False, "evidence": "fine"})
    _judge_with(msgs, model="claude-haiku-5-5").judge(JudgeRequest("periodic", frames.render(0.3, 0.0)))
    assert "fallbacks" not in msgs.calls[0]
    assert judge_mod.PRICES["claude-haiku-5-5"] == (0.10, 0.50)
