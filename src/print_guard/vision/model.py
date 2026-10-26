"""Local failure detector: Obico's open "spaghetti" model, run with onnxruntime.

The network (a YOLOv4 variant with one class, "failure", 416x416 RGB input) was
trained by Obico (formerly The Spaghetti Detective) on several hundred thousand
webcam frames of failing prints. It returns candidate boxes with confidences;
the per-frame score is the sum of the confidences that survive thresholding and
non-maximum suppression, which is how Obico scores frames too.

The weights are not bundled. `print-guard download-model` fetches them from the
URL Obico's own server repository points to and checks the published SHA-256.
"""

from __future__ import annotations

import hashlib
import logging
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

MODEL_URL = "https://www.obico.io/static_pub/ml-models/model-weights-5a6b1be1fa.onnx"
MODEL_SHA256 = "0a6ebd8e30dbf6a450c50f9c0a5406f04ba7eb1c99fd5996e888c78bb383b9aa"


@dataclass(frozen=True)
class Detection:
    confidence: float
    # Box corners as fractions of the frame (x0, y0, x1, y1).
    box: tuple[float, float, float, float]

    @property
    def center(self) -> tuple[float, float]:
        return ((self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2)


def decode_jpeg(jpeg: bytes) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("could not decode JPEG frame")
    return image


def brightness(image: np.ndarray) -> float:
    """Mean luma 0-255; low values mean the chamber light is off."""
    small = cv2.resize(image, (64, 36), interpolation=cv2.INTER_AREA)
    return float(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).mean())


def nms(boxes: np.ndarray, scores: np.ndarray, threshold: float) -> list[int]:
    """Greedy non-maximum suppression; boxes are (N, 4) x0,y0,x1,y1."""
    order = scores.argsort()[::-1]
    x0, y0, x1, y1 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0.0, x1 - x0) * np.maximum(0.0, y1 - y0)
    keep: list[int] = []
    while order.size:
        i = int(order[0])
        keep.append(i)
        rest = order[1:]
        ix0 = np.maximum(x0[i], x0[rest])
        iy0 = np.maximum(y0[i], y0[rest])
        ix1 = np.minimum(x1[i], x1[rest])
        iy1 = np.minimum(y1[i], y1[rest])
        inter = np.maximum(0.0, ix1 - ix0) * np.maximum(0.0, iy1 - iy0)
        union = areas[i] + areas[rest] - inter
        iou = np.where(union > 0, inter / np.maximum(union, 1e-9), 0.0)
        order = rest[iou <= threshold]
    return keep


class FailureDetector:
    def __init__(self, model_path: Path, detection_threshold: float = 0.08, nms_threshold: float = 0.45,
                 threads: int | None = None) -> None:
        import onnxruntime as ort

        if not model_path.exists():
            raise FileNotFoundError(
                f"failure-detection model not found at {model_path}. Run `print-guard download-model`."
            )
        options = ort.SessionOptions()
        # Leave CPU for the rest of the system; one frame every 10 s needs very little.
        options.intra_op_num_threads = threads or max(1, min(4, (os.cpu_count() or 2) // 2))
        options.log_severity_level = 3
        self._session = ort.InferenceSession(str(model_path), options, providers=["CPUExecutionProvider"])
        inp = self._session.get_inputs()[0]
        self._input_name = inp.name
        self._height, self._width = int(inp.shape[2]), int(inp.shape[3])
        self.detection_threshold = detection_threshold
        self.nms_threshold = nms_threshold

    def detect(self, image: np.ndarray) -> list[Detection]:
        """Run the model on a BGR image and return surviving detections."""
        resized = cv2.resize(image, (self._width, self._height), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        tensor = np.transpose(rgb, (2, 0, 1)).astype(np.float32)[None] / 255.0
        boxes, confs = self._session.run(None, {self._input_name: tensor})
        # boxes: [1, N, 1, 4] as fractions x0,y0,x1,y1; confs: [1, N, classes]
        boxes = boxes[0, :, 0, :]
        scores = confs[0].max(axis=1)
        mask = scores > self.detection_threshold
        boxes, scores = boxes[mask], scores[mask]
        if not len(scores):
            return []
        keep = nms(boxes, scores, self.nms_threshold)
        return [
            Detection(float(scores[i]), tuple(float(np.clip(v, 0.0, 1.0)) for v in boxes[i]))  # type: ignore[arg-type]
            for i in keep
        ]


def in_roi(det: Detection, roi: list[float]) -> bool:
    cx, cy = det.center
    return roi[0] <= cx <= roi[2] and roi[1] <= cy <= roi[3]


STRONG = (0, 0, 230)  # BGR red: confidence >= 0.3
WEAK = (0, 165, 255)  # BGR orange


def _overlaps(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def annotate(image: np.ndarray, detections: list[Detection], roi: list[float] | None = None,
             caption: str = "") -> np.ndarray:
    """Draw detections (and the ROI) on a copy of the frame for snapshots and alerts.

    Boxes are drawn first and their confidence tags afterwards, strongest first, each
    tag nudged down until it doesn't cover another, so every number stays readable."""
    out = image.copy()
    h, w = out.shape[:2]
    font, scale = cv2.FONT_HERSHEY_SIMPLEX, 0.5
    if roi and roi != [0.0, 0.0, 1.0, 1.0]:
        cv2.rectangle(out, (int(roi[0] * w), int(roi[1] * h)), (int(roi[2] * w), int(roi[3] * h)),
                      (255, 200, 0), 1)
    ordered = sorted(detections, key=lambda d: d.confidence, reverse=True)
    boxes = [(int(d.box[0] * w), int(d.box[1] * h), int(d.box[2] * w), int(d.box[3] * h)) for d in ordered]
    for d, (x0, y0, x1, y1) in zip(ordered, boxes, strict=True):
        cv2.rectangle(out, (x0, y0), (x1, y1), STRONG if d.confidence >= 0.3 else WEAK, 2)
    placed: list[tuple[int, int, int, int]] = []
    for d, (x0, y0, _x1, y1) in zip(ordered, boxes, strict=True):
        text = f"{d.confidence:.2f}"
        (tw, th), _ = cv2.getTextSize(text, font, scale, 1)
        tag = (x0, y0, x0 + tw + 8, y0 + th + 8)
        while any(_overlaps(tag, p) for p in placed) and tag[3] + th + 8 < y1:
            tag = (tag[0], tag[1] + th + 8, tag[2], tag[3] + th + 8)
        placed.append(tag)
        strong = d.confidence >= 0.3
        cv2.rectangle(out, tag[:2], tag[2:], STRONG if strong else WEAK, -1)
        cv2.putText(out, text, (tag[0] + 4, tag[3] - 4), font, scale, (255, 255, 255) if strong else (0, 0, 0),
                    1, cv2.LINE_AA)
    if caption:
        cv2.rectangle(out, (0, 0), (w, 26), (0, 0, 0), -1)
        cv2.putText(out, caption, (8, 18), font, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
    return out


def encode_jpeg(image: np.ndarray, quality: int = 88) -> bytes:
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("JPEG encoding failed")
    return buf.tobytes()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_model(dest: Path, progress=None) -> Path:
    """Download the detector weights to `dest`, verifying the SHA-256 before keeping them."""
    if dest.exists() and sha256_file(dest) == MODEL_SHA256:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    request = urllib.request.Request(MODEL_URL, headers={"User-Agent": "print-guard/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response, tmp.open("wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while chunk := response.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    digest = sha256_file(tmp)
    if digest != MODEL_SHA256:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"downloaded model checksum mismatch ({digest}); not using it")
    tmp.replace(dest)
    return dest
