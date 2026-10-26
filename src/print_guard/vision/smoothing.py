"""Turns noisy per-frame detector scores into a stable "is this print failing?" signal.

A single frame can show a false positive (a purge blob, a reflection), so the
decision follows the scheme Obico tuned for one frame every 10 seconds:

  * EWM: an exponentially weighted mean that reacts within a couple of minutes;
  * short mean: roughly the last 50 minutes, for "is this a sudden change?".

Obico compares the EWM with a long-term camera baseline that is close to zero
for most views. That breaks on prints whose healthy look already scores high:
dense tree supports measured 2.9 +/- 0.7 on a real P1S, where spaghetti adds
roughly +1.5 to +3.5. So each print is judged against its own normal look:

  * base: this print's normal score, learned slowly (about 15 minutes) and
    frozen while something looks wrong, so a failure can't teach itself to
    look normal. A fresh print starts from the camera's long-term level (capped);
    attaching mid-print or resuming calibrates on the current look instead;
  * noise margin: a few standard deviations of the EWM's own noise, so a busy,
    flickery scene needs a bigger jump than a calm one.

The print is "warning" when (EWM - base) * sensitivity - margin clears the low
threshold and either clears the high threshold or rises well above the short
mean. "failure" is the same test with the score divided by the escalating
factor, so pausing needs clearly stronger evidence than alerting. The first
`warmup_frames` frames (5 minutes) never trigger.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

from print_guard.config import VisionConfig

log = logging.getLogger(__name__)


class Level(StrEnum):
    OK = "ok"
    WARMUP = "warmup"
    WARNING = "warning"
    FAILURE = "failure"


@dataclass
class ScoreState:
    current_p: float = 0.0
    frame: int = 0  # frames scored in this print (or since re-arming)
    lifetime_frames: int = 0  # frames scored by this camera, ever
    ewm_mean: float = 0.0
    short_mean: float = 0.0
    long_mean: float = 0.0  # the camera's typical score across prints (persisted)
    base: float = 0.0  # this print's normal score
    base_var: float = 0.0  # spread of frames around it
    calibrating: bool = False
    cal_sum: float = 0.0
    cal_sumsq: float = 0.0


# The camera's long-term level starts from a prior of "no detections" worth an hour of
# frames, so a print failing in its first minutes on a fresh install still stands out.
BASELINE_PRIOR_FRAMES = 360
# A fresh print starts its base from the camera's long-term level, but never above this:
# a support-heavy previous print must not hide the next print's early failures.
INITIAL_BASE_CAP = 1.0
# Floor for the frame-to-frame noise estimate, so the margin never collapses to zero.
MIN_SIGMA = 0.05
# How fast the base learns, by the current level: freeze while failing, slow while unsure.
LEARN_RATE = {Level.OK: 1.0, Level.WARMUP: 1.0, Level.WARNING: 0.25, Level.FAILURE: 0.0}


def _rolling(mean: float, p: float, count: int, window: int) -> float:
    """Streaming approximation of a moving average over `window` samples."""
    return mean + (p - mean) / float(window if window <= count else count + 1)


class FailureScore:
    def __init__(self, cfg: VisionConfig, baseline_file: Path | None = None) -> None:
        self.cfg = cfg
        self.state = ScoreState()
        self._baseline_file = baseline_file
        self._load_baseline()

    @property
    def _alpha(self) -> float:
        return 2.0 / (self.cfg.ewm_span + 1)

    @property
    def _base_frames(self) -> float:
        return max(1.0, self.cfg.baseline_minutes * 60.0 / self.cfg.interval_seconds)

    # Persistence of the camera's long-term level --------------------------------------
    def _load_baseline(self) -> None:
        if not self._baseline_file or not self._baseline_file.exists():
            return
        try:
            data = json.loads(self._baseline_file.read_text(encoding="utf-8"))
            self.state.long_mean = float(data.get("long_mean", 0.0))
            self.state.lifetime_frames = int(data.get("lifetime_frames", 0))
            log.info("vision baseline loaded: %.3f over %d frames", self.state.long_mean,
                     self.state.lifetime_frames)
        except (OSError, ValueError, TypeError) as e:
            log.warning("ignoring unreadable vision baseline %s: %s", self._baseline_file, e)

    def save_baseline(self) -> None:
        if not self._baseline_file:
            return
        try:
            self._baseline_file.parent.mkdir(parents=True, exist_ok=True)
            data = {"long_mean": self.state.long_mean, "lifetime_frames": self.state.lifetime_frames}
            tmp = self._baseline_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self._baseline_file)
        except OSError as e:
            log.warning("could not save vision baseline: %s", e)

    # Updates -------------------------------------------------------------------------------
    def update(self, p: float) -> Level:
        s, c = self.state, self.cfg
        before = self.level()
        s.current_p = p
        s.frame += 1
        s.lifetime_frames += 1
        if s.calibrating:
            # Exact mean and spread of the calibration frames: "this is what normal looks like".
            if s.frame == 1:
                s.ewm_mean = p
            s.cal_sum += p
            s.cal_sumsq += p * p
            s.base = s.cal_sum / s.frame
            s.base_var = max(0.0, s.cal_sumsq / s.frame - s.base * s.base)
            if s.frame >= c.warmup_frames:
                s.calibrating = False
                log.info("vision calibrated on this print's current look: score %.2f +/- %.2f",
                         s.base, math.sqrt(s.base_var))
        else:
            rate = LEARN_RATE[before] / self._base_frames
            resid = p - s.base
            s.base += rate * resid
            s.base_var = (1 - rate) * s.base_var + rate * resid * resid
        s.ewm_mean = p * self._alpha + s.ewm_mean * (1 - self._alpha)
        s.short_mean = _rolling(s.short_mean, p, s.frame, c.rolling_win_short)
        if before in (Level.OK, Level.WARMUP):
            prior = min(BASELINE_PRIOR_FRAMES, c.rolling_win_long)
            s.long_mean = _rolling(s.long_mean, p, max(s.lifetime_frames, prior), c.rolling_win_long)
        return self.level()

    def new_print(self, attached: bool = False) -> None:
        """Start a print's history. attached=True: the print is already under way, so
        calibrate on its current look; otherwise start from the camera's usual level."""
        old = self.state
        self.state = ScoreState(long_mean=old.long_mean, lifetime_frames=old.lifetime_frames)
        if attached:
            self.state.calibrating = True
        else:
            self.state.base = self.state.ewm_mean = min(old.long_mean, INITIAL_BASE_CAP)

    def rearm(self) -> None:
        """After a resume: warm up again and treat the resumed print's look as normal."""
        s = self.state
        s.frame = 0
        s.calibrating = True
        s.cal_sum = s.cal_sumsq = 0.0
        s.short_mean = 0.0

    def absorb(self) -> None:
        """A reviewer (Claude) said the current look is fine: make it the new normal."""
        s = self.state
        if s.ewm_mean > s.base:
            s.base_var = max(s.base_var, ((s.ewm_mean - s.base) / 2) ** 2)
            s.base = s.ewm_mean

    # Decisions -------------------------------------------------------------------------------
    @property
    def margin(self) -> float:
        """Noise allowance: noise_margin_sigmas standard deviations of the EWM itself."""
        sigma = max(MIN_SIGMA, math.sqrt(self.state.base_var))
        return self.cfg.noise_margin_sigmas * sigma * math.sqrt(self._alpha / (2 - self._alpha))

    @property
    def adjusted(self) -> float:
        """How far the recent score sits above this print's normal look, beyond the noise."""
        return (self.state.ewm_mean - self.state.base) * self.cfg.sensitivity - self.margin

    def _failing(self, escalating_factor: float) -> bool:
        s, c = self.state, self.cfg
        if s.frame < c.warmup_frames:
            return False
        score = self.adjusted / escalating_factor
        if score < c.threshold_low:
            return False
        if score > c.threshold_high:
            return True
        return score > (s.short_mean - s.base) * c.short_mean_multiple

    def level(self) -> Level:
        if self.state.frame < self.cfg.warmup_frames:
            return Level.WARMUP
        if self._failing(self.cfg.escalating_factor):
            return Level.FAILURE
        if self._failing(1.0):
            return Level.WARNING
        return Level.OK

    def normalized(self) -> float:
        """0..1 gauge for the dashboard: 1/3 = warning line, 2/3 = failure line."""
        s, c = self.state, self.cfg
        warn = min(c.threshold_high, max(c.threshold_low, (s.short_mean - s.base) * c.short_mean_multiple))
        fail = warn * c.escalating_factor
        p = self.adjusted

        def scale(v: float, lo: float, hi: float, out_lo: float, out_hi: float) -> float:
            if hi <= lo:
                return out_lo
            return min(out_hi, max(out_lo, out_lo + (v - lo) * (out_hi - out_lo) / (hi - lo)))

        if p > fail:
            return scale(p, fail, fail * 1.5, 2 / 3, 1.0)
        if p > warn:
            return scale(p, warn, fail, 1 / 3, 2 / 3)
        return scale(p, 0.0, warn, 0.0, 1 / 3)

    def as_dict(self) -> dict:
        return {**asdict(self.state), "margin": self.margin, "adjusted": self.adjusted,
                "level": self.level().value}
