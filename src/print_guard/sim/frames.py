"""Synthetic camera frames for the simulator: a P1S-like view of a part growing on the
plate, and spaghetti that piles up after a failure. The real detector scores these
frames like real ones (clean ~0, spaghetti well above the failure line), which is
what makes the simulator a genuine end-to-end test of the vision path."""

from __future__ import annotations

import cv2
import numpy as np

W, H = 1280, 720
PLATE = np.array([[260, 690], [1180, 690], [1010, 300], [420, 300]], np.int32)


def _background(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.zeros((H, W, 3), np.uint8)
    img[:] = (38, 40, 44)
    cv2.rectangle(img, (0, 0), (W, 120), (60, 66, 72), -1)
    cv2.fillPoly(img, [PLATE], (52, 52, 56))
    mask = np.zeros((H, W), np.uint8)
    cv2.fillPoly(mask, [PLATE], 255)
    noise = np.repeat(rng.integers(0, 12, (H, W, 1), dtype=np.uint8), 3, 2)
    return np.where(mask[..., None] > 0, cv2.add(img, noise), img)


_BG = _background(7)


def render(progress: float, t: float, failure_age: float | None = None, light: bool = True,
           seed: int = 0) -> bytes:
    """progress 0..1 of the print; failure_age seconds since spaghetti started (None = healthy)."""
    rng = np.random.default_rng(seed)
    img = _BG.copy()
    height = int(260 * max(0.02, progress))
    x0, x1, base = 610, 830, 560
    cv2.rectangle(img, (x0, base - height), (x1, base), (225, 225, 220), -1)
    cv2.rectangle(img, (x0, base - height), (x1, base), (190, 190, 185), 2)
    for y in range(base - height, base, 6):
        cv2.line(img, (x0, y), (x1, y), (205, 205, 200), 1)
    # The toolhead sweeps across the part like a real print head.
    hx = int(650 + 120 * np.sin(t * 1.7))
    cv2.rectangle(img, (hx, base - height - 160), (hx + 140, base - height - 40), (30, 30, 30), -1)
    if failure_age is not None:
        strands = int(min(220, 25 + failure_age * 6))
        frng = np.random.default_rng(1234)  # same strands every frame so the pile only grows
        for _ in range(strands):
            cx, cy = int(frng.integers(500, 950)), int(frng.integers(380, 620))
            pts = [(cx, cy)]
            for _ in range(int(frng.integers(6, 14))):
                cx += int(frng.integers(-60, 61))
                cy += int(frng.integers(-40, 41))
                pts.append((cx, cy))
            thickness = int(frng.integers(2, 4))
            cv2.polylines(img, [np.array(pts, np.int32)], False, (230, 230, 225), thickness, cv2.LINE_AA)
        img = cv2.GaussianBlur(img, (3, 3), 0)
    if not light:
        img = (img * 0.08).astype(np.uint8)
    # Sensor noise so consecutive frames differ like a real camera's.
    img = cv2.add(img, rng.integers(0, 4, img.shape, dtype=np.uint8))
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 82])
    assert ok
    return buf.tobytes()
